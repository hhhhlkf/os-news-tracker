"""AgentCrawlFetcher — Agent Crawl 抓取器（Handoff Chain 编排器）。

实现 Fetcher Protocol，将 PlanAgent → CrawlDAG → QualityWorkerPool → SummaryWorkerPool
四个阶段串联为完整的抓取管线。通过 asyncio.run() 驱动异步管线，
对外暴露同步的 fetch() 接口。

Agent crawl 条目绕过 LLM Enricher，直接以 status=agent_enriched 存入数据库。
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
import urllib.parse
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.agent.crawl_dag import CrawlDAG
from app.agent.plan_agent import PlanAgent
from app.agent.quality_pool import QualityWorkerPool
from app.agent.schemas import AgentItem, AgentSourceConfig, CrawlPlan, PlanUrl, RawPage
from app.agent.site_memory import SiteMemory
from app.agent.summary_pool import SummaryWorkerPool
from app.enums import ItemStatus
from app.fetchers.rss import RssFetcher
from app.models import AgentCrawlRun, AgentSourceConfig as AgentSourceConfigModel, Source
from app.repository import Repository
from app.run_logs import append_run_log, clear_run_logs
from app.schemas import AgentCrawlRunRequest, RawItem

logger = logging.getLogger(__name__)


def _looks_like_feed_url(url: str) -> bool:
    path = urlparse(url).path.lower().rstrip("/")
    return (
        path.endswith((".rss", ".xml", ".atom"))
        or path.endswith("/feed")
        or path.endswith("/rss")
        or "/rss/" in path
        or "/feed/" in path
    )


_PAGINATION_QUERY_PARAMS = (
    "page", "pageNo", "pageNum", "currentPage", "current", "p", "pageIndex",
    "pageSize", "size", "limit", "per_page", "perPage", "count", "rows",
)


def _is_stale_paginated_probe(probe: object) -> bool:
    """识别上一轮旧代码产出的「脏 probe」：URL 含分页 query 参数但无 pagination 配置。

    这类 probe 是 _build_probe 还不会生成 pagination 时缓存进 DB 的，会导致
    运行侧分页引擎不触发、永远只爬第一页。命中即应触发重新探测覆盖。
    """
    if not isinstance(probe, dict):
        return False
    url = probe.get("url")
    if not isinstance(url, str) or not url:
        return False
    if probe.get("pagination"):
        return False
    query = urlparse(url).query
    if not query:
        return False
    present = {key for key, _ in urllib.parse.parse_qsl(query, keep_blank_values=True)}
    return any(param in present for param in _PAGINATION_QUERY_PARAMS)


def _infer_missing_pagination_for_cached_probe(probe: object) -> dict | None:
    """补旧缓存：OpenAnolis blogByCategoryPage 的干净 URL 仍需要 page/pageSize。"""
    if not isinstance(probe, dict) or probe.get("pagination"):
        return None
    url = probe.get("url")
    if not isinstance(url, str) or not url:
        return None

    path = urlparse(url).path.lower()
    if "blogbycategorypage" not in path:
        return None

    return {
        "page_param": "page",
        "size_param": "pageSize",
        "size": 10,
        "start_page": 1,
        "max_pages": 5,
        "has_more_path": "data.hasMore",
    }


_RELATIVE_RANGE_TO_DELTA = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_raw_item(item: AgentItem, default_main_category: str | None = None) -> RawItem:
    """将 AgentItem 转换为 RawItem，富化数据编码在 extra 字段中。

    content_type 通过 key_points 的首元素 __type: 前缀传递，
    Pipeline 中的 agent 旁路会从 extra 读取所有元数据。
    """
    key_points_prefix = [f"__type:{item.content_type}"] + item.key_facts
    return RawItem(
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        raw_content=item.body,
        published_at=None,
        extra={
            "agent_item": True,
            "main_category": item.topic_group or default_main_category or "OS跟踪来源",
            "importance": item.importance,
            "info_type": "其他",
            "key_points": key_points_prefix,
            "sub_tags": item.sub_tags,
            "merge_suggestions": item.merge_suggestions,
        },
    )


class AgentCrawlFetcher:
    """实现 Fetcher Protocol 的 Agent Crawl 抓取器。

    通过 asyncio.run() 驱动完整的 Handoff Chain：
    PlanAgent → CrawlDAG → QualityWorkerPool → SummaryWorkerPool。

    每个 fetch() 调用会在 agent_crawl_runs 表中创建一条运行记录，
    跟踪各阶段的产出数量，便于监控和调试。
    """

    def __init__(
        self,
        db: Session,
        *,
        plan_agent=None,
        crawl_dag=None,
        quality_pool=None,
        summary_pool=None,
        rss_fetcher=None,
        time_window=None,
    ):
        self._db = db
        self._memory = SiteMemory()
        self._plan_agent = plan_agent or PlanAgent(memory=self._memory)
        self._crawl_dag = crawl_dag or CrawlDAG()
        self._quality_pool = quality_pool or QualityWorkerPool(memory=self._memory)
        self._summary_pool = summary_pool or SummaryWorkerPool()
        self._rss_fetcher = rss_fetcher or RssFetcher()
        self._time_window = AgentCrawlRunRequest.model_validate(time_window or {})
        self._prefetched_pages: list[RawPage] | None = None

    def _time_window_label(self) -> str:
        if self._time_window.time_mode == "relative":
            return f"最近 {self._time_window.relative_range}"
        start = self._time_window.start_at.date().isoformat() if self._time_window.start_at else "--"
        end = self._time_window.end_at.date().isoformat() if self._time_window.end_at else "--"
        return f"{start} 至 {end}"

    def _matches_time_window(self, item: RawItem, *, now: datetime | None = None) -> bool:
        if item.published_at is None:
            return False

        item_ts = _as_utc(item.published_at)
        if self._time_window.time_mode == "relative":
            current_time = now or datetime.now(timezone.utc)
            lower_bound = _as_utc(current_time) - _RELATIVE_RANGE_TO_DELTA[self._time_window.relative_range]
            return item_ts >= lower_bound

        start_ts = _as_utc(self._time_window.start_at)
        end_ts = _as_utc(self._time_window.end_at)
        return start_ts <= item_ts <= end_ts

    def _build_plan(self, source: Source, config: AgentSourceConfig) -> CrawlPlan:
        """Build the URL plan for API-probe, RSS-backed, or HTML-homepage sources.

        Branch order:
        1. probe (own or borrowed from candidate) → ``_build_plan_from_api``
        2. RSS seed → ``_build_plan_from_rss``
        3. Runtime API discovery (Playwright) → writeback probe → ``_build_plan_from_api``
        4. LLM PlanAgent (fallback, unchanged)
        """
        api_config = source.api_config or {}
        seed_url = str(api_config.get("seed_url") or source.url)

        # ── API probe seed: use ApiAdapterFetcher to get item URLs ──
        # Prefer candidate's probe (user-curated, may be updated after re-discovery).
        # Fall back to agent source's own cached probe (from prior runtime discovery).
        probe = None
        probe_owner = None
        candidate_id = api_config.get("candidate_source_id")
        if isinstance(candidate_id, int):
            candidate = self._db.get(Source, candidate_id)
            if candidate and candidate.api_config and isinstance(candidate.api_config.get("probe"), dict):
                probe = candidate.api_config["probe"]
                probe_owner = candidate
                # Use candidate's URL if the agent source URL points to the API
                if not source.url or source.url == candidate.url:
                    source = Source(
                        id=source.id, name=source.name, type="api",
                        url=candidate.url, api_config=candidate.api_config,
                        stream=source.stream, enabled=True,
                    )
        if not probe:
            probe = api_config.get("probe")
            probe_owner = source if isinstance(probe, dict) else None
        if isinstance(probe, dict) and _is_stale_paginated_probe(probe):
            append_run_log(
                "plan",
                "检测到分页式 probe 缺失 pagination 配置，触发重新探测",
                source=source.name,
                url=probe.get("url") or source.url,
                level="warning",
            )
            # 丢弃脏 probe，落到下文 _try_runtime_discovery 重探
            probe = None
        elif isinstance(probe, dict):
            inferred_pagination = _infer_missing_pagination_for_cached_probe(probe)
            if inferred_pagination:
                probe = {**probe, "pagination": inferred_pagination}
                if probe_owner is not None:
                    owner_config = dict(probe_owner.api_config or {})
                    owner_config["probe"] = probe
                    probe_owner.api_config = owner_config
                    self._db.commit()
                append_run_log(
                    "plan",
                    "检测到旧缓存 probe 缺失 pagination，已自动补齐",
                    source=source.name,
                    url=probe.get("url") or source.url,
                    level="warning",
                )
            return self._build_plan_from_api(source, config)
        elif (
            source.api_config
            and isinstance(source.api_config.get("probe"), dict)
            and not _is_stale_paginated_probe(source.api_config.get("probe"))
        ):
            return self._build_plan_from_api(source, config)

        # ── RSS seed ──
        if api_config.get("seed_type") == "rss" or _looks_like_feed_url(seed_url):
            return self._build_plan_from_rss(source, config, seed_url)

        # ── Runtime API discovery (before LLM fallback) ──
        discovered = self._try_runtime_discovery(source, config)
        if discovered is not None:
            return discovered

        # ── LLM PlanAgent (fallback) ──
        return self._plan_agent.plan(source, config, db=self._db)

    def _try_runtime_discovery(self, source: Source, config: AgentSourceConfig) -> CrawlPlan | None:
        """Attempt runtime API discovery via Playwright.

        On success: writes the probe back to the **candidate source** (if
        ``candidate_source_id`` is set) so that subsequent runs borrow the cached
        probe from the candidate instead of re-discovering. If there is no
        candidate (standalone agent source), writes to ``source.api_config``
        directly. Then delegates to ``_build_plan_from_api``.
        On failure: logs and returns ``None`` so the caller falls back to LLM.
        """
        from app.sources.api_discovery import discover_api_source

        append_run_log(
            "plan",
            "运行时探测 API…",
            source=source.name,
            url=source.url,
        )
        try:
            result = discover_api_source(source.url, sample_items=config.max_urls_per_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_crawl: runtime discovery failed for %s: %s", source.url, exc)
            append_run_log(
                "plan", "运行时探测失败，回退 LLM",
                source=source.name, level="warning", url=source.url, reason=str(exc),
            )
            return None

        if not result.success or not result.api_url:
            append_run_log(
                "plan", "运行时探测未发现 API，回退 LLM",
                source=source.name, level="warning", url=source.url,
            )
            return None

        probe = {
            "mode": "json_list",
            "method": result.method,
            "url": result.api_url,
            "items_path": result.items_path or "",
            "fields": result.fields,
        }
        if result.pagination:
            probe["pagination"] = result.pagination

        api_config = source.api_config or {}
        candidate_id = api_config.get("candidate_source_id")
        if candidate_id:
            # Write to candidate only — it's the single source of truth.
            # The agent source will borrow it on next run via _build_plan.
            candidate = self._db.get(Source, candidate_id)
            if candidate:
                cand_config = dict(candidate.api_config or {})
                cand_config["probe"] = probe
                candidate.api_config = cand_config
                self._db.commit()
                # Also update the in-memory source so _build_plan_from_api sees it
                source = Source(
                    id=source.id, name=source.name, type="api",
                    url=candidate.url, api_config=cand_config,
                    stream=source.stream, enabled=True,
                )
        else:
            # Standalone agent source (no candidate): cache on itself
            updated_config = dict(api_config)
            updated_config["probe"] = probe
            source.api_config = updated_config
            self._db.commit()

        append_run_log(
            "plan", "运行时探测命中，已缓存 probe",
            source=source.name, api_url=result.api_url,
        )
        return self._build_plan_from_api(source, config)

    def _build_plan_from_api(self, source: Source, config: AgentSourceConfig) -> CrawlPlan:
        """Use ApiAdapterFetcher with probe config to discover article URLs."""
        from app.fetchers.api_adapters import ApiAdapterFetcher

        append_run_log(
            "plan",
            "开始规划 URL（API 种子模式）",
            source=source.name,
            url=source.url,
        )
        try:
            fetcher = ApiAdapterFetcher()
            raw_items = fetcher.fetch(source)
        except Exception as e:
            logger.warning("agent_crawl: API seed fetch failed for %s: %s", source.url, e)
            append_run_log(
                "plan",
                "API 种子抓取失败",
                source=source.name,
                level="error",
                url=source.url,
                reason=str(e),
            )
            return CrawlPlan(source_id=config.source_id, urls=[])

        filtered_items = [item for item in raw_items if self._matches_time_window(item)]
        seen: set[str] = set()
        urls: list[PlanUrl] = []
        prefetched: list[RawPage] = []
        for item in filtered_items:
            if not item.url or item.url in seen:
                continue
            seen.add(item.url)
            guessed = item.title or (config.topic_groups[0] if config.topic_groups else "")
            urls.append(PlanUrl(url=item.url, guessed_topic=guessed))
            prefetched.append(RawPage(
                url=item.url,
                guessed_topic=guessed,
                title=item.title or "",
                content=item.raw_content or "",
            ))
            if len(urls) >= config.max_urls_per_run:
                break

        self._prefetched_pages = prefetched

        append_run_log(
            "plan",
            "API 种子解析完成",
            source=source.name,
            entries=len(raw_items),
            matched=len(filtered_items),
            plan_urls=len(urls),
            window=self._time_window_label(),
        )
        logger.info(
            "agent_crawl: API seed %s produced %d plan URLs from %d entries (%s)",
            source.url,
            len(urls),
            len(raw_items),
            self._time_window_label(),
        )
        return CrawlPlan(source_id=config.source_id, urls=urls)

    def _build_plan_from_rss(self, source: Source, config: AgentSourceConfig, seed_url: str) -> CrawlPlan:
        """Use RssFetcher to discover article URLs from a feed."""
        seed_source = Source(
            id=source.id,
            name=source.name,
            type="rss",
            url=seed_url,
            stream=source.stream,
            enabled=True,
        )
        raw_items = self._rss_fetcher.fetch(seed_source)
        filtered_items = [item for item in raw_items if self._matches_time_window(item)]
        seen: set[str] = set()
        urls: list[PlanUrl] = []
        for item in filtered_items:
            if not item.url or item.url in seen:
                continue
            seen.add(item.url)
            urls.append(
                PlanUrl(
                    url=item.url,
                    guessed_topic=item.title or (config.topic_groups[0] if config.topic_groups else ""),
                )
            )
            if len(urls) >= config.max_urls_per_run:
                break

        append_run_log(
            "plan",
            "RSS 种子解析完成",
            source=source.name,
            entries=len(raw_items),
            matched=len(filtered_items),
            plan_urls=len(urls),
            window=self._time_window_label(),
        )
        logger.info(
            "agent_crawl: RSS seed %s produced %d plan URLs from %d entries (%s)",
            seed_url,
            len(urls),
            len(raw_items),
            self._time_window_label(),
        )
        return CrawlPlan(source_id=config.source_id, urls=urls)

    def fetch(self, source: Source) -> list[RawItem]:
        """执行一次完整的 Agent Crawl 管线。

        流程：
        1. 从 agent_source_configs 表读取配置
        2. 创建 AgentCrawlRun 运行记录
        3. PlanAgent 规划 URL 列表
        4. CrawlDAG 并行抓取页面
        5. QualityWorkerPool 质量评估和过滤
        6. SummaryWorkerPool 摘要生成
        7. 将 AgentItem 转换为 RawItem 返回（Pipeline 负责存储）

        Args:
            source: 源记录（ORM 对象），需包含 id 和 url 属性。

        Returns:
            RawItem 列表，每个条目的 extra 字段包含 agent 富化元数据。
            管线失败时返回空列表。
        """
        clear_run_logs()
        append_run_log(
            "run",
            "Agent 抓取开始",
            source=source.name,
            source_id=source.id,
            url=source.url,
            target_count=self._time_window.target_count,
            time_mode=self._time_window.time_mode,
            window=self._time_window_label(),
        )

        # ── 1. 读取配置 ──
        config_model = self._db.get(AgentSourceConfigModel, source.id)
        if config_model is None:
            append_run_log(
                "run",
                "源缺少 Agent 配置，已跳过",
                source=source.name,
                source_id=source.id,
                level="warning",
            )
            logger.warning(
                "agent_crawl: 源 %d 无 AgentSourceConfig，跳过", source.id,
            )
            return []

        # 抓取限制的「数量」限制的是最终查取（入库候选）条数，而不是规划的 URL 数。
        # 规划广度仍由源配置 max_urls_per_run 决定，但若目标条数更大则相应放宽，
        # 以保证质量过滤后仍有机会凑足目标条数。最终在摘要前按质量分截断到目标条数。
        target_count = self._time_window.target_count
        plan_max_urls = config_model.max_urls_per_run
        if target_count is not None:
            plan_max_urls = max(plan_max_urls, target_count)

        config = AgentSourceConfig(
            source_id=source.id,
            focus_areas=config_model.focus_areas or [],
            topic_groups=config_model.topic_groups or [],
            crawl_depth=config_model.crawl_depth,
            max_urls_per_run=plan_max_urls,
            quality_threshold=config_model.quality_threshold,
            crawl_workers=config_model.crawl_workers,
            quality_workers=config_model.quality_workers,
            summary_workers=config_model.summary_workers,
        )

        # ── 2. 创建运行记录 ──
        run = AgentCrawlRun(
            source_id=source.id,
            status="running",
            current_stage="planning",
            stage_message="开始规划 URL",
            plan_urls_count=0,
            fetched_count=0,
            quality_passed=0,
            items_created=0,
            target_count=target_count,
        )
        self._db.add(run)
        self._db.flush()

        # ── 3–6. 执行异步管线 ──
        source_name = source.name

        async def _pipeline() -> list[AgentItem]:
            # ③ PlanAgent: URL 规划
            self._prefetched_pages = None
            plan = self._build_plan(source, config)
            run.plan_urls_count = len(plan.urls)
            run.current_stage = "planning"
            run.stage_message = f"已规划 {run.plan_urls_count} 个 URL"
            self._db.commit()

            # ④ CrawlDAG: 并行抓取（API 种子模式跳过，直接用预取内容）
            run.current_stage = "crawling"
            if self._prefetched_pages is not None:
                pages = self._prefetched_pages
                append_run_log(
                    "fetch",
                    "API 种子预取内容已就绪，跳过页面抓取",
                    source=source_name,
                    pages=len(pages),
                )
            else:
                run.stage_message = f"并行抓取 {run.plan_urls_count} 个 URL"
                pages = await self._crawl_dag.execute(plan, config, source_name=source_name)
            run.fetched_count = len(pages)
            self._db.commit()

            # ⑤ QualityWorkerPool: 质量评估
            run.current_stage = "quality"
            qualified = await self._quality_pool.assess_all(
                pages, config, db=self._db, source_name=source_name,
            )
            run.quality_passed = len(qualified)
            run.stage_message = f"质量通过 {run.quality_passed} / {run.fetched_count}"
            self._db.commit()

            # ⑤.5 按目标条数截断（查取条数限制）：保留质量分最高的若干条。
            if target_count is not None and len(qualified) > target_count:
                qualified = sorted(
                    qualified, key=lambda qp: qp.score, reverse=True
                )[:target_count]
                append_run_log(
                    "quality",
                    "按目标条数截断",
                    source=source_name,
                    target_count=target_count,
                    kept=len(qualified),
                    passed=run.quality_passed,
                )

            # ⑥ SummaryWorkerPool: 摘要生成
            run.current_stage = "summarizing"
            run.stage_message = f"正在生成 {len(qualified)} 条摘要"
            self._db.commit()
            items = await self._summary_pool.summarize_all(
                qualified,
                config,
                source_name=source_name,
                existing_tags=Repository(self._db).list_existing_sub_tags(),
            )
            return items

        try:
            agent_items = asyncio.run(_pipeline())
        except Exception as e:
            run.status = "failed"
            run.current_stage = "failed"
            run.stage_message = str(e)
            run.error_message = str(e)
            run.completed_at = datetime.now(timezone.utc)
            self._db.commit()
            append_run_log(
                "run",
                "Agent 抓取失败",
                source=source.name,
                source_id=source.id,
                level="error",
                error=str(e),
            )
            logger.exception(
                "agent_crawl: 管线失败 source=%d: %s", source.id, e,
            )
            return []

        # ── 7. 转换为 RawItem ──
        default_main_category = next(
            (topic for topic in config.topic_groups if topic),
            source.main_category,
        )
        raw_items = [_to_raw_item(item, default_main_category) for item in agent_items]
        run.items_created = len(raw_items)
        run.status = "completed"
        run.current_stage = "completed"
        run.stage_message = f"已生成 {run.items_created} 条候选"
        run.completed_at = datetime.now(timezone.utc)
        self._db.commit()

        append_run_log(
            "run",
            "Agent 抓取完成",
            source=source.name,
            source_id=source.id,
            plan_urls=run.plan_urls_count,
            fetched=run.fetched_count,
            quality_passed=run.quality_passed,
            items=run.items_created,
        )
        logger.info(
            "agent_crawl: source=%d 完成 plan=%d fetch=%d quality=%d items=%d",
            source.id, run.plan_urls_count, run.fetched_count,
            run.quality_passed, run.items_created,
        )
        return raw_items
