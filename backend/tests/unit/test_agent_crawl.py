"""AgentCrawlFetcher 单元测试 — 验证 Handoff Chain 编排、配置缺失处理、异常隔离。"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.schemas import AgentItem, CrawlPlan, PlanUrl, QualifiedPage, RawPage
from app.fetchers.agent_crawl import AgentCrawlFetcher, _is_stale_paginated_probe, _to_raw_item
from app.models import AgentSourceConfig as AgentSourceConfigModel
from app.schemas import RawItem
from app.sources.api_discovery import ApiDiscoveryResult


def _make_config_model(**kwargs):
    """构造一个数据库 AgentSourceConfig 记录。"""
    defaults = dict(
        source_id=1, focus_areas=["kernel", "eBPF"], topic_groups=["项目动态"],
        crawl_depth=1, max_urls_per_run=5, quality_threshold=4,
        crawl_workers=3, quality_workers=2, summary_workers=2,
    )
    defaults.update(kwargs)
    return AgentSourceConfigModel(**defaults)


def _make_agent_item(url="https://blog.example.com/post/1", **kwargs):
    """构造一个 AgentItem。"""
    defaults = dict(
        source_id=1, url=url, title="Linux 6.12 发布",
        topic_group="项目动态", content_type="release_note",
        importance="高", body="内核 6.12 引入 sched_ext。",
        key_facts=["[内核] sched_ext 合入主线"],
    )
    defaults.update(kwargs)
    return AgentItem(**defaults)


class TestToRawItem:
    """测试 _to_raw_item 将 AgentItem 转换为 RawItem。"""

    def test_converts_basic_fields(self):
        """基本字段应正确映射。"""
        item = _make_agent_item()
        raw = _to_raw_item(item)
        assert raw.source_id == 1
        assert raw.url == "https://blog.example.com/post/1"
        assert raw.title == "Linux 6.12 发布"
        assert raw.raw_content == "内核 6.12 引入 sched_ext。"
        assert raw.published_at is None

    def test_published_at_passes_through(self):
        """AgentItem 携带 published_at 时，应透传到 RawItem（不再硬编码 None）。"""
        when = datetime(2024, 11, 19, tzinfo=timezone.utc)
        item = _make_agent_item(published_at=when)
        raw = _to_raw_item(item)
        assert raw.published_at == when

    def test_extra_contains_agent_metadata(self):
        """extra 字段应包含 agent 元数据和编码后的 key_points。"""
        item = _make_agent_item(sub_tags=["Linux Kernel", "调度器"])
        raw = _to_raw_item(item)
        assert raw.extra is not None
        assert raw.extra["agent_item"] is True
        assert raw.extra["main_category"] == "项目动态"
        assert raw.extra["importance"] == "高"
        assert "__type:release_note" in raw.extra["key_points"]
        assert raw.extra["sub_tags"] == ["Linux Kernel", "调度器"]

    def test_topic_group_none_falls_back_to_default_main_category(self):
        """topic_group 为空时，main_category 应继承来源默认主分类。"""
        item = _make_agent_item(topic_group=None)
        raw = _to_raw_item(item, default_main_category="友商产品信息")
        assert raw.extra["main_category"] == "友商产品信息"

    def test_key_points_include_content_type_prefix(self):
        """content_type 通过 __type: 前缀编码到 key_points 首元素。"""
        item = _make_agent_item(
            content_type="benchmark",
            key_facts=["[性能] SPEC CPU 2017 提升 5%"],
        )
        raw = _to_raw_item(item)
        assert raw.extra["key_points"][0] == "__type:benchmark"
        assert raw.extra["key_points"][1] == "[性能] SPEC CPU 2017 提升 5%"


class TestMatchesTimeWindowForAgentItem:
    """LLM 规划路径在入库前按时间窗过滤 agent_items（AgentItem 带 published_at）。"""

    def _fetcher(self, time_window: dict) -> AgentCrawlFetcher:
        return AgentCrawlFetcher(db=MagicMock(), time_window=time_window)

    def test_relative_window_keeps_recent_drops_old(self):
        """relative 30d：最近 30 天的保留，2024/2025 的旧文章丢弃。"""
        from datetime import timedelta

        fetcher = self._fetcher({"time_mode": "relative", "relative_range": "30d"})
        now = datetime.now(timezone.utc)
        recent = _make_agent_item(
            url="https://x/recent",
            published_at=now - timedelta(days=5),
        )
        old = _make_agent_item(
            url="https://x/old",
            published_at=datetime(2024, 7, 1, tzinfo=timezone.utc),
        )
        assert fetcher._matches_time_window(recent, now=now) is True
        assert fetcher._matches_time_window(old, now=now) is False

    def test_null_published_at_dropped(self):
        """published_at 为空的 AgentItem 被时间窗丢弃。"""
        fetcher = self._fetcher({"time_mode": "relative", "relative_range": "30d"})
        no_date = _make_agent_item(url="https://x/nodate", published_at=None)
        assert fetcher._matches_time_window(no_date) is False

    def test_absolute_window_respects_bounds(self):
        """absolute 模式：窗口内的保留，窗口外的丢弃。"""
        fetcher = self._fetcher({
            "time_mode": "absolute",
            "start_at": "2026-01-01T00:00:00Z",
            "end_at": "2026-06-26T00:00:00Z",
            "relative_range": None,
        })
        inside = _make_agent_item(
            url="https://x/in",
            published_at=datetime(2026, 5, 12, tzinfo=timezone.utc),
        )
        outside = _make_agent_item(
            url="https://x/out",
            published_at=datetime(2024, 7, 1, tzinfo=timezone.utc),
        )
        assert fetcher._matches_time_window(inside) is True
        assert fetcher._matches_time_window(outside) is False


class TestAgentCrawlFetcherFetch:
    """测试 AgentCrawlFetcher.fetch() 编排逻辑。"""

    def test_missing_config_returns_empty(self):
        """当 AgentSourceConfig 不存在时，应返回空列表。"""
        db = MagicMock()
        db.get.return_value = None  # AgentSourceConfig 查询返回 None
        fetcher = AgentCrawlFetcher(db=db)
        source = MagicMock()
        source.id = 1
        result = fetcher.fetch(source)
        assert result == []

    def test_successful_pipeline_returns_raw_items(self):
        """完整 pipeline 成功时应返回 RawItem 列表。"""
        db = MagicMock()
        db.get.return_value = _make_config_model()
        stage_snapshots: list[tuple[str | None, str | None, int, int, int, int, str | None]] = []

        def record_stage():
            run = db.add.call_args_list[0].args[0]
            stage_snapshots.append(
                (
                    getattr(run, "current_stage", None),
                    getattr(run, "stage_message", None),
                    run.plan_urls_count,
                    run.fetched_count,
                    run.quality_passed,
                    run.items_created,
                    run.status,
                )
            )

        db.commit.side_effect = record_stage

        # Mock 四个阶段
        mock_plan = CrawlPlan(source_id=1, urls=[
            PlanUrl(url="https://blog.example.com/post/1", guessed_topic="kernel"),
        ])
        mock_page = RawPage(
            url="https://blog.example.com/post/1", guessed_topic="kernel",
            title="Linux 6.12", content="内核内容...",
        )
        mock_qualified = QualifiedPage(page=mock_page, verdict="keep", score=8)
        # 带 recent published_at，避免被时间窗过滤（默认 relative 7d）
        mock_agent_item = _make_agent_item(
            published_at=datetime.now(timezone.utc),
        )

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=MagicMock(plan=MagicMock(return_value=mock_plan)),
            crawl_dag=MagicMock(execute=AsyncMock(return_value=[mock_page])),
            quality_pool=MagicMock(assess_all=AsyncMock(return_value=[mock_qualified])),
            summary_pool=MagicMock(summarize_all=AsyncMock(return_value=[mock_agent_item])),
        )
        source = MagicMock()
        source.id = 1
        source.api_config = None  # no probe/RSS → runtime discovery → LLM fallback
        with patch("app.sources.api_discovery.discover_api_source", side_effect=RuntimeError("no playwright in test")):
            result = fetcher.fetch(source)
        assert len(result) == 1
        assert isinstance(result[0], RawItem)
        assert result[0].title == "Linux 6.12 发布"
        assert stage_snapshots == [
            ("planning", "已规划 1 个 URL", 1, 0, 0, 0, "running"),
            ("crawling", "并行抓取 1 个 URL", 1, 1, 0, 0, "running"),
            ("quality", "正在质量评估 1 页", 1, 1, 0, 0, "running"),
            ("quality", "质量通过 1 / 1", 1, 1, 1, 0, "running"),
            ("summarizing", "正在生成 1 条摘要", 1, 1, 1, 0, "running"),
            ("completed", "已生成 1 条候选", 1, 1, 1, 1, "completed"),
        ]

    def test_rss_seed_source_uses_feed_entries_as_plan_urls(self):
        """RSS-backed agent sources should seed CrawlDAG from feed entry links."""
        db = MagicMock()
        db.get.return_value = _make_config_model(max_urls_per_run=2)
        plan_agent = MagicMock()
        crawl_dag = MagicMock(execute=AsyncMock(return_value=[]))
        rss_fetcher = MagicMock(fetch=MagicMock(return_value=[
            RawItem(
                source_id=1,
                title="Kernel fix",
                url="https://example.com/post-1",
                published_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
            ),
            RawItem(
                source_id=1,
                title="Release notes",
                url="https://example.com/post-2",
                published_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
            ),
            RawItem(
                source_id=1,
                title="Overflow",
                url="https://example.com/post-3",
                published_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
            ),
        ]))

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=plan_agent,
            crawl_dag=crawl_dag,
            quality_pool=MagicMock(assess_all=AsyncMock(return_value=[])),
            summary_pool=MagicMock(summarize_all=AsyncMock(return_value=[])),
            rss_fetcher=rss_fetcher,
            time_window={
                "time_mode": "absolute",
                "start_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "end_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
            },
        )
        source = MagicMock()
        source.id = 1
        source.url = "https://example.com/feed.xml"
        source.api_config = {
            "seed_type": "rss",
            "seed_url": "https://example.com/feed.xml",
        }

        result = fetcher.fetch(source)

        assert result == []
        plan_agent.plan.assert_not_called()
        plan = crawl_dag.execute.call_args.args[0]
        assert [url.url for url in plan.urls] == [
            "https://example.com/post-1",
            "https://example.com/post-2",
        ]

    def test_legacy_agent_source_with_feed_url_uses_rss_seed(self):
        """Existing agent sources created from RSS URLs should work without api_config."""
        db = MagicMock()
        db.get.return_value = _make_config_model(max_urls_per_run=1)
        plan_agent = MagicMock()
        crawl_dag = MagicMock(execute=AsyncMock(return_value=[]))
        rss_fetcher = MagicMock(fetch=MagicMock(return_value=[
            RawItem(
                source_id=1,
                title="Blog article",
                url="https://example.com/blog/article",
                published_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
            ),
        ]))

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=plan_agent,
            crawl_dag=crawl_dag,
            quality_pool=MagicMock(assess_all=AsyncMock(return_value=[])),
            summary_pool=MagicMock(summarize_all=AsyncMock(return_value=[])),
            rss_fetcher=rss_fetcher,
            time_window={
                "time_mode": "absolute",
                "start_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "end_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
            },
        )
        source = MagicMock()
        source.id = 1
        source.url = "https://example.com/blog/index.xml"
        source.api_config = None

        fetcher.fetch(source)

        plan_agent.plan.assert_not_called()
        plan = crawl_dag.execute.call_args.args[0]
        assert [url.url for url in plan.urls] == ["https://example.com/blog/article"]

    def test_rss_seed_filters_entries_by_absolute_time_window(self):
        """RSS-backed agent sources should only plan URLs inside the run time window."""
        db = MagicMock()
        db.get.return_value = _make_config_model(max_urls_per_run=5)
        crawl_dag = MagicMock(execute=AsyncMock(return_value=[]))
        rss_fetcher = MagicMock(fetch=MagicMock(return_value=[
            RawItem(
                source_id=1,
                title="Old",
                url="https://example.com/old",
                published_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            ),
            RawItem(
                source_id=1,
                title="Inside",
                url="https://example.com/inside",
                published_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
            ),
            RawItem(
                source_id=1,
                title="Future",
                url="https://example.com/future",
                published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            ),
        ]))

        fetcher = AgentCrawlFetcher(
            db=db,
            crawl_dag=crawl_dag,
            quality_pool=MagicMock(assess_all=AsyncMock(return_value=[])),
            summary_pool=MagicMock(summarize_all=AsyncMock(return_value=[])),
            rss_fetcher=rss_fetcher,
            time_window={
                "time_mode": "absolute",
                "start_at": datetime(2026, 6, 10, tzinfo=timezone.utc),
                "end_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
            },
        )
        source = MagicMock()
        source.id = 1
        source.url = "https://example.com/feed.xml"
        source.api_config = None

        fetcher.fetch(source)

        plan = crawl_dag.execute.call_args.args[0]
        assert [url.url for url in plan.urls] == ["https://example.com/inside"]

    def test_rss_seed_with_content_skips_detail_page_fetch(self):
        """RSS-backed agent sources should use feed content when details are bot-blocked."""
        db = MagicMock()
        db.get.return_value = _make_config_model(max_urls_per_run=1)
        crawl_dag = MagicMock(execute=AsyncMock(return_value=[]))
        quality_pool = MagicMock(assess_all=AsyncMock(return_value=[]))
        rss_fetcher = MagicMock(fetch=MagicMock(return_value=[
            RawItem(
                source_id=1,
                title="Kernel news",
                url="https://www.phoronix.com/news/Linux-Example",
                raw_content="RSS summary with enough Linux technical detail.",
                published_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
            ),
        ]))

        fetcher = AgentCrawlFetcher(
            db=db,
            crawl_dag=crawl_dag,
            quality_pool=quality_pool,
            summary_pool=MagicMock(summarize_all=AsyncMock(return_value=[])),
            rss_fetcher=rss_fetcher,
            time_window={
                "time_mode": "absolute",
                "start_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "end_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
            },
        )
        source = MagicMock()
        source.id = 1
        source.name = "Phoronix"
        source.url = "https://www.phoronix.com/rss.php"
        source.api_config = None

        fetcher.fetch(source)

        crawl_dag.execute.assert_not_called()
        pages = quality_pool.assess_all.call_args.args[0]
        assert pages[0].url == "https://www.phoronix.com/news/Linux-Example"
        assert pages[0].content == "RSS summary with enough Linux technical detail."
        assert pages[0].published_at == datetime(2026, 6, 20, tzinfo=timezone.utc)

    def test_pipeline_failure_returns_empty(self):
        """Pipeline 中任何阶段抛出异常时应返回空列表并记录失败。"""
        db = MagicMock()
        db.get.return_value = _make_config_model()
        stage_snapshots: list[tuple[str | None, str | None, str, str | None]] = []

        def record_stage():
            run = db.add.call_args_list[0].args[0]
            stage_snapshots.append(
                (
                    getattr(run, "current_stage", None),
                    getattr(run, "stage_message", None),
                    run.status,
                    run.error_message,
                )
            )

        db.commit.side_effect = record_stage

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=MagicMock(plan=MagicMock(side_effect=RuntimeError("LLM 不可用"))),
        )
        source = MagicMock()
        source.id = 1
        source.api_config = None  # no probe/RSS → runtime discovery → LLM fallback
        with patch("app.sources.api_discovery.discover_api_source", side_effect=RuntimeError("no playwright in test")):
            result = fetcher.fetch(source)
        assert result == []
        # 验证 run 被标记为 failed
        calls = db.add.call_args_list
        assert len(calls) >= 1  # 至少创建了 AgentCrawlRun
        assert stage_snapshots[-1] == ("failed", "LLM 不可用", "failed", "LLM 不可用")

    def test_runtime_discovery_writes_back_probe_and_uses_api_plan(self):
        """When no probe/RSS seed exists, runtime discovery should find an API,
        write back the probe to source.api_config, and use _build_plan_from_api."""
        from app.sources.api_discovery import ApiDiscoveryResult

        discovery_result = ApiDiscoveryResult(
            root_url="https://example.com/blog",
            success=True,
            api_url="https://api.example.com/blog/list",
            method="GET",
            items_path="data.records",
            fields={"title": "title", "url": "url", "published_at": "published_at"},
            name_suggestion="Example Blog",
            real_content_count=1,
        )

        raw_item = RawItem(
            source_id=1,
            title="Discovered Article",
            url="https://example.com/blog/1",
            published_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
        )

        api_fetcher = MagicMock(fetch=MagicMock(return_value=[raw_item]))
        db = MagicMock()
        db.get.return_value = _make_config_model()

        with patch("app.fetchers.api_adapters.ApiAdapterFetcher", return_value=api_fetcher), \
             patch("app.sources.api_discovery.discover_api_source", return_value=discovery_result):
            fetcher = AgentCrawlFetcher(
                db=db,
                plan_agent=MagicMock(),  # should NOT be called
                crawl_dag=MagicMock(execute=AsyncMock(return_value=[])),
                quality_pool=MagicMock(assess_all=AsyncMock(return_value=[])),
                summary_pool=MagicMock(summarize_all=AsyncMock(return_value=[])),
                time_window={
                    "time_mode": "absolute",
                    "start_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                    "end_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
                },
            )
            source = MagicMock()
            source.id = 1
            source.url = "https://example.com/blog"
            source.api_config = {}

            result = fetcher.fetch(source)

        assert result == []
        fetcher._plan_agent.plan.assert_not_called()
        api_fetcher.fetch.assert_called_once()

    def test_api_seed_prefetched_pages_preserve_published_at(self):
        """API seed mode must carry RawItem.published_at into prefetched RawPage."""
        published_at = datetime(2026, 6, 29, tzinfo=timezone.utc)
        raw_item = RawItem(
            source_id=1,
            title="openEuler article",
            url="https://www.openeuler.org/zh/blog/20260629/a.html",
            raw_content="article body",
            published_at=published_at,
        )
        api_fetcher = MagicMock(fetch=MagicMock(return_value=[raw_item]))
        db = MagicMock()
        fetcher = AgentCrawlFetcher(
            db=db,
            time_window={
                "time_mode": "absolute",
                "start_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "end_at": datetime(2026, 7, 1, 23, 59, 59, tzinfo=timezone.utc),
            },
        )
        source = MagicMock()
        source.id = 1
        source.name = "openeuler.org"
        source.url = "https://www.openeuler.org/api-search/search/sort/blog"

        with patch("app.fetchers.api_adapters.ApiAdapterFetcher", return_value=api_fetcher):
            plan = fetcher._build_plan_from_api(source, _make_config_model())

        assert [url.url for url in plan.urls] == [raw_item.url]
        assert fetcher._prefetched_pages is not None
        assert fetcher._prefetched_pages[0].published_at == published_at

    def test_runtime_discovery_failure_falls_back_to_llm(self):
        """When runtime discovery finds nothing, fall back to PlanAgent."""
        from app.sources.api_discovery import ApiDiscoveryResult

        discovery_result = ApiDiscoveryResult(
            root_url="https://example.com/blog",
            success=False,
            notes=["未捕获到任何 JSON XHR/Fetch 响应"],
        )

        mock_plan = CrawlPlan(source_id=1, urls=[])
        plan_agent = MagicMock(plan=MagicMock(return_value=mock_plan))
        db = MagicMock()
        db.get.return_value = _make_config_model()

        with patch("app.sources.api_discovery.discover_api_source", return_value=discovery_result):
            fetcher = AgentCrawlFetcher(
                db=db,
                plan_agent=plan_agent,
                crawl_dag=MagicMock(execute=AsyncMock(return_value=[])),
                quality_pool=MagicMock(assess_all=AsyncMock(return_value=[])),
                summary_pool=MagicMock(summarize_all=AsyncMock(return_value=[])),
            )
            source = MagicMock()
            source.id = 1
            source.url = "https://example.com/blog"
            source.api_config = {}

            fetcher.fetch(source)

        plan_agent.plan.assert_called_once()


class TestTryRuntimeDiscoveryPagination:
    """测试 _try_runtime_discovery 把 pagination 写入缓存 probe。"""

    def _make_source_with_candidate(self, db, *, candidate_api_config):
        """构造一个 agent source，其 api_config 指向 candidate_source_id=2。"""
        from app.enums import Stream

        candidate = MagicMock()
        candidate.id = 2
        candidate.api_config = candidate_api_config
        candidate.url = "https://openanolis.cn/blog"

        agent_source = MagicMock()
        agent_source.id = 1
        agent_source.name = "OpenAnolis Blog"
        agent_source.url = "https://openanolis.cn/blog"
        agent_source.api_config = {"candidate_source_id": 2}
        agent_source.stream = Stream.NEWS

        def fake_get(model, pk):
            if model.__name__ == "AgentSourceConfig":
                return _make_config_model()
            if pk == 2:
                return candidate
            return None
        db.get.side_effect = fake_get
        db.commit = MagicMock()
        return agent_source, candidate

    def test_writes_pagination_into_cached_probe(self):
        from app.agent.schemas import AgentSourceConfig
        from unittest.mock import patch

        db = MagicMock()
        agent_source, candidate = self._make_source_with_candidate(
            db, candidate_api_config={},
        )
        config = AgentSourceConfig(
            source_id=1, focus_areas=["kernel"], topic_groups=["项目动态"],
            crawl_depth=1, max_urls_per_run=5, quality_threshold=4,
            crawl_workers=3, quality_workers=2, summary_workers=2,
        )

        discovery_result = ApiDiscoveryResult(
            root_url="https://openanolis.cn/blog",
            success=True,
            api_url="https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=",
            method="GET",
            items_path="data.items",
            fields={"title": "title", "url_template": "https://openanolis.cn/blog/{no}"},
            pagination={
                "page_param": "page", "size_param": "pageSize", "size": 10,
                "start_page": 1, "max_pages": 5, "has_more_path": "data.hasMore",
            },
            name_suggestion="openanolis.cn",
        )

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=MagicMock(),
            crawl_dag=MagicMock(),
            quality_pool=MagicMock(),
            summary_pool=MagicMock(),
        )
        # _build_plan_from_api 会调 ApiAdapterFetcher 真实抓取，桩掉它只断言 probe 写入
        fetcher._build_plan_from_api = MagicMock(return_value=CrawlPlan(source_id=1, urls=[]))

        with patch("app.sources.api_discovery.discover_api_source", return_value=discovery_result):
            fetcher._try_runtime_discovery(agent_source, config)

        cached_probe = candidate.api_config["probe"]
        assert cached_probe["pagination"] == discovery_result.pagination
        assert cached_probe["url"] == "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo="

    def test_omits_pagination_key_when_result_has_none(self):
        from app.agent.schemas import AgentSourceConfig
        from unittest.mock import patch

        db = MagicMock()
        agent_source, candidate = self._make_source_with_candidate(
            db, candidate_api_config={},
        )
        config = AgentSourceConfig(
            source_id=1, focus_areas=["kernel"], topic_groups=["项目动态"],
            crawl_depth=1, max_urls_per_run=5, quality_threshold=4,
            crawl_workers=3, quality_workers=2, summary_workers=2,
        )

        discovery_result = ApiDiscoveryResult(
            root_url="https://example.com/blog",
            success=True,
            api_url="https://api.example.com/list",
            method="GET",
            items_path="items",
            fields={"title": "title", "url": "url"},
            pagination=None,
        )

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=MagicMock(),
            crawl_dag=MagicMock(),
            quality_pool=MagicMock(),
            summary_pool=MagicMock(),
        )
        fetcher._build_plan_from_api = MagicMock(return_value=CrawlPlan(source_id=1, urls=[]))

        with patch("app.sources.api_discovery.discover_api_source", return_value=discovery_result):
            fetcher._try_runtime_discovery(agent_source, config)

        cached_probe = candidate.api_config["probe"]
        assert "pagination" not in cached_probe


class TestIsStalePaginatedProbe:
    """测试 _is_stale_paginated_probe 识别上一轮旧代码产出的脏 probe。"""

    def test_url_with_page_param_and_no_pagination_is_stale(self):
        probe = {
            "mode": "json_list",
            "url": "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=&page=1&pageSize=10",
            "items_path": "data.items",
            "fields": {"title": "title"},
        }
        assert _is_stale_paginated_probe(probe) is True

    def test_url_with_page_param_and_pagination_is_not_stale(self):
        probe = {
            "mode": "json_list",
            "url": "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=&page=1&pageSize=10",
            "items_path": "data.items",
            "fields": {"title": "title"},
            "pagination": {"page_param": "page", "has_more_path": "data.hasMore"},
        }
        assert _is_stale_paginated_probe(probe) is False

    def test_url_without_pagination_params_is_not_stale(self):
        probe = {
            "mode": "json_list",
            "url": "https://api.example.com/list?category=all",
            "items_path": "items",
            "fields": {"title": "title"},
        }
        assert _is_stale_paginated_probe(probe) is False

    def test_camel_case_page_param_detected_as_stale(self):
        probe = {
            "mode": "json_list",
            "url": "https://example.com/api/list?currentPage=1",
            "items_path": "data.records",
            "fields": {"title": "title"},
        }
        assert _is_stale_paginated_probe(probe) is True

    def test_non_dict_probe_is_not_stale(self):
        assert _is_stale_paginated_probe(None) is False
        assert _is_stale_paginated_probe("not a dict") is False

    def test_probe_without_url_is_not_stale(self):
        probe = {"mode": "json_list", "items_path": "items", "fields": {}}
        assert _is_stale_paginated_probe(probe) is False


class TestBuildPlanStaleProbeRediscovers:
    """测试 _build_plan 对脏 probe 触发重探、对干净 probe 直接复用。"""

    def _make_fetcher(self, db):
        return AgentCrawlFetcher(
            db=db,
            plan_agent=MagicMock(),
            crawl_dag=MagicMock(),
            quality_pool=MagicMock(),
            summary_pool=MagicMock(),
        )

    def _config(self):
        from app.agent.schemas import AgentSourceConfig
        return AgentSourceConfig(
            source_id=1, focus_areas=["kernel"], topic_groups=["项目动态"],
            crawl_depth=1, max_urls_per_run=5, quality_threshold=4,
            crawl_workers=3, quality_workers=2, summary_workers=2,
        )

    def test_stale_probe_triggers_rediscovery(self):
        from unittest.mock import patch
        from app.sources.api_discovery import ApiDiscoveryResult

        db = MagicMock()
        # candidate 持有脏 probe（URL 有 page=1 但无 pagination）
        candidate = MagicMock()
        candidate.id = 2
        candidate.url = "https://openanolis.cn/blog"
        candidate.api_config = {
            "probe": {
                "mode": "json_list",
                "method": "GET",
                "url": "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=&page=1&pageSize=10",
                "items_path": "data.items",
                "fields": {"title": "title", "url_template": "https://openanolis.cn/blog/{no}"},
            }
        }
        agent_source = MagicMock()
        agent_source.id = 1
        agent_source.name = "OpenAnolis Blog"
        agent_source.url = "https://openanolis.cn/blog"
        agent_source.api_config = {"candidate_source_id": 2}
        agent_source.stream = MagicMock()

        def fake_get(model, pk):
            if model.__name__ == "AgentSourceConfig":
                return _make_config_model()
            if pk == 2:
                return candidate
            return None
        db.get.side_effect = fake_get

        fetcher = self._make_fetcher(db)
        plan_from_api = CrawlPlan(source_id=1, urls=[PlanUrl(url="https://openanolis.cn/blog/1", guessed_topic="t")])
        fetcher._build_plan_from_api = MagicMock(return_value=plan_from_api)
        fetcher._try_runtime_discovery = MagicMock(return_value=plan_from_api)

        discovery_result = ApiDiscoveryResult(
            root_url="https://openanolis.cn/blog",
            success=True,
            api_url="https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=",
            items_path="data.items",
            fields={"title": "title"},
            pagination={"page_param": "page", "has_more_path": "data.hasMore"},
        )

        with patch("app.sources.api_discovery.discover_api_source", return_value=discovery_result) as mock_discover:
            plan = fetcher._build_plan(agent_source, self._config())

        # 脏 probe 触发了重探（_try_runtime_discovery 被调用，内部会调 discover_api_source）
        assert fetcher._try_runtime_discovery.called
        # _build_plan_from_api 没有被直接用脏 probe 调用
        assert not fetcher._build_plan_from_api.called
        assert "probe" not in candidate.api_config
        assert db.commit.called
        assert plan is plan_from_api

    def test_clean_probe_with_pagination_is_reused_directly(self):
        db = MagicMock()
        candidate = MagicMock()
        candidate.id = 2
        candidate.url = "https://openanolis.cn/blog"
        candidate.api_config = {
            "probe": {
                "mode": "json_list",
                "method": "GET",
                "url": "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=",
                "items_path": "data.items",
                "fields": {"title": "title", "url_template": "https://openanolis.cn/blog/{no}"},
                "pagination": {"page_param": "page", "has_more_path": "data.hasMore"},
            }
        }
        agent_source = MagicMock()
        agent_source.id = 1
        agent_source.name = "OpenAnolis Blog"
        agent_source.url = "https://openanolis.cn/blog"
        agent_source.api_config = {"candidate_source_id": 2}
        agent_source.stream = MagicMock()

        def fake_get(model, pk):
            if model.__name__ == "AgentSourceConfig":
                return _make_config_model()
            if pk == 2:
                return candidate
            return None
        db.get.side_effect = fake_get

        fetcher = self._make_fetcher(db)
        plan_from_api = CrawlPlan(source_id=1, urls=[PlanUrl(url="https://openanolis.cn/blog/1", guessed_topic="t")])
        fetcher._build_plan_from_api = MagicMock(return_value=plan_from_api)
        fetcher._try_runtime_discovery = MagicMock()

        plan = fetcher._build_plan(agent_source, self._config())

        # 干净 probe 直接复用，不重探
        assert fetcher._build_plan_from_api.called
        assert not fetcher._try_runtime_discovery.called
        assert plan is plan_from_api

    def test_non_paginated_probe_is_reused_directly(self):
        db = MagicMock()
        candidate = MagicMock()
        candidate.id = 2
        candidate.url = "https://api.example.com/list"
        candidate.api_config = {
            "probe": {
                "mode": "json_list",
                "method": "GET",
                "url": "https://api.example.com/list?category=all",
                "items_path": "items",
                "fields": {"title": "title", "url": "url"},
            }
        }
        agent_source = MagicMock()
        agent_source.id = 1
        agent_source.name = "Example API"
        agent_source.url = "https://api.example.com/list"
        agent_source.api_config = {"candidate_source_id": 2}
        agent_source.stream = MagicMock()

        def fake_get(model, pk):
            if model.__name__ == "AgentSourceConfig":
                return _make_config_model()
            if pk == 2:
                return candidate
            return None
        db.get.side_effect = fake_get

        fetcher = self._make_fetcher(db)
        plan_from_api = CrawlPlan(source_id=1, urls=[PlanUrl(url="https://example.com/a", guessed_topic="t")])
        fetcher._build_plan_from_api = MagicMock(return_value=plan_from_api)
        fetcher._try_runtime_discovery = MagicMock()

        plan = fetcher._build_plan(agent_source, self._config())

        # 无分页参数的 probe 也直接复用，不重探
        assert fetcher._build_plan_from_api.called
        assert not fetcher._try_runtime_discovery.called

    def test_clean_openanolis_blog_probe_without_pagination_is_repaired(self):
        db = MagicMock()
        candidate = MagicMock()
        candidate.id = 2
        candidate.url = "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo="
        candidate.api_config = {
            "probe": {
                "mode": "json_list",
                "method": "GET",
                "url": "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=",
                "items_path": "data.items",
                "fields": {
                    "title": "title",
                    "published_at": "publishTime",
                    "content": ["summary", "content"],
                    "url_template": "https://openanolis.cn/blog/detail/{item.no}",
                },
            }
        }
        agent_source = MagicMock()
        agent_source.id = 1
        agent_source.name = "OpenAnolis Blog"
        agent_source.url = "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo="
        agent_source.api_config = {"candidate_source_id": 2}
        agent_source.stream = MagicMock()

        def fake_get(model, pk):
            if model.__name__ == "AgentSourceConfig":
                return _make_config_model()
            if pk == 2:
                return candidate
            return None
        db.get.side_effect = fake_get

        fetcher = self._make_fetcher(db)
        plan_from_api = CrawlPlan(source_id=1, urls=[PlanUrl(url="https://openanolis.cn/blog/detail/1", guessed_topic="t")])
        fetcher._build_plan_from_api = MagicMock(return_value=plan_from_api)
        fetcher._try_runtime_discovery = MagicMock()

        plan = fetcher._build_plan(agent_source, self._config())

        repaired_probe = candidate.api_config["probe"]
        assert repaired_probe["pagination"] == {
            "page_param": "page",
            "size_param": "pageSize",
            "size": 10,
            "start_page": 1,
            "max_pages": 5,
            "has_more_path": "data.hasMore",
        }
        assert fetcher._build_plan_from_api.called
        assert not fetcher._try_runtime_discovery.called
        assert db.commit.called
        assert plan is plan_from_api


class TestPaginatedProbeEndToEnd:
    """端到端：探测带 pagination 的 result → 写入 candidate → 多页抓取超过单页条数。"""

    def test_rediscovery_yields_multi_page_items(self):
        from unittest.mock import patch
        from app.fetchers.api_adapters import ApiAdapterFetcher
        from app.sources.api_discovery import ApiDiscoveryResult

        # 三页响应：page1/2 各 2 条 hasMore=true，page3 1 条 hasMore=false
        def fake_requester(url):
            from urllib.parse import parse_qs, urlparse as _urlparse
            import json as _json
            page = int(parse_qs(_urlparse(url).query).get("page", ["1"])[0])
            if page == 1:
                items = [
                    {"title": "A", "no": "a", "date": "2026-06-20"},
                    {"title": "B", "no": "b", "date": "2026-06-20"},
                ]
                has_more = True
            elif page == 2:
                items = [
                    {"title": "C", "no": "c", "date": "2026-06-20"},
                    {"title": "D", "no": "d", "date": "2026-06-20"},
                ]
                has_more = True
            else:
                items = [{"title": "E", "no": "e", "date": "2026-06-20"}]
                has_more = False
            return _json.dumps({"data": {"items": items, "hasMore": has_more}})

        # 用真实的 ApiAdapterFetcher（注入 fake_requester），走 ConfigurableApiProbeAdapter 分页引擎
        real_fetcher = ApiAdapterFetcher(requester=fake_requester)

        db = MagicMock()
        candidate = MagicMock()
        candidate.id = 2
        candidate.url = "https://openanolis.cn/blog"
        candidate.api_config = {}  # 探测前无 probe
        agent_source = MagicMock()
        agent_source.id = 1
        agent_source.name = "OpenAnolis Blog"
        agent_source.url = "https://openanolis.cn/blog"
        agent_source.api_config = {"candidate_source_id": 2}
        agent_source.stream = MagicMock()

        def fake_get(model, pk):
            if model.__name__ == "AgentSourceConfig":
                return _make_config_model(max_urls_per_run=10)
            if pk == 2:
                return candidate
            return None
        db.get.side_effect = fake_get

        discovery_result = ApiDiscoveryResult(
            root_url="https://openanolis.cn/blog",
            success=True,
            api_url="https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=",
            method="GET",
            items_path="data.items",
            fields={
                "title": "title",
                "url_template": "https://openanolis.cn/blog/{no}",
                "published_at": "date",
            },
            pagination={
                "page_param": "page", "size_param": "pageSize", "size": 10,
                "start_page": 1, "max_pages": 5, "has_more_path": "data.hasMore",
            },
            name_suggestion="openanolis.cn",
        )

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=MagicMock(),
            crawl_dag=MagicMock(),
            quality_pool=MagicMock(),
            summary_pool=MagicMock(),
            # 固定绝对时间窗口，避免依赖「现在」的相对窗口；条目日期 2026-06-20 落在窗口内
            time_window={
                "time_mode": "absolute",
                "start_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "end_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
            },
        )
        # _build_plan_from_api 内部局部 `from app.fetchers.api_adapters import ApiAdapterFetcher`，
        # 每次 call 都重新读取该模块属性，故 patch 模块属性即可让 ApiAdapterFetcher() 返回
        # 注入了 fake_requester 的 real_fetcher，从而走真实 ConfigurableApiProbeAdapter 分页引擎。
        with patch("app.fetchers.api_adapters.ApiAdapterFetcher", return_value=real_fetcher), \
             patch("app.sources.api_discovery.discover_api_source", return_value=discovery_result):
            from app.agent.schemas import AgentSourceConfig
            config = AgentSourceConfig(
                source_id=1, focus_areas=["kernel"], topic_groups=["项目动态"],
                crawl_depth=1, max_urls_per_run=10, quality_threshold=4,
                crawl_workers=3, quality_workers=2, summary_workers=2,
            )
            plan = fetcher._build_plan(agent_source, config)

        # 三页共 5 条，超过单页实际 2 条 —— 证明分页引擎端到端生效
        assert len(plan.urls) == 5
        titles = [pu.guessed_topic for pu in plan.urls]
        assert "A" in titles and "E" in titles
        # candidate 的缓存 probe 现在带 pagination（_try_runtime_discovery 写入）
        cached_probe = candidate.api_config["probe"]
        assert cached_probe["pagination"]["page_param"] == "page"
