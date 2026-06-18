from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
import json
import logging
import re
import threading
from typing import Protocol

from app.enums import MissingDatePolicy
from app.schemas import (
    ManualNewsRunRequest,
    ManualNewsRunStatus,
    RawItem,
    TimeFilterStats,
)


def _as_utc(dt: datetime) -> datetime:
    """Return a UTC-aware equivalent of *dt*.

    Naive datetimes are assumed to already represent UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_RELATIVE_RANGE_TO_DELTA = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# Maximum expansion rounds when candidates fall short of target_count.
_MAX_EXPANSION_ROUNDS = 3

# Per source/link, each collection round should contribute only the highest
# quality candidates so one noisy feed cannot dominate the manual run.
MAX_CANDIDATES_PER_SOURCE_PER_ROUND = 5

logger = logging.getLogger(__name__)


class _Completer(Protocol):
    def complete(self, prompt: str, **kw) -> str: ...


_CANDIDATE_SCORE_PROMPT = """你是操作系统维护团队的关键技术新闻候选排序器。请给下面同一个 source/link 抓到的候选打质量分，用于只保留最值得进入后续摘要流程的最多 5 条。

评分目标：优先选择关键技术新闻，而不是最新但低价值的信息。

高分标准：
- 新兴技术/工具/架构进入可观察阶段
- OS、内核、发行版、编译器、包管理、云原生基础设施、AI agent/LLM 工具链的重要发布、重大更新、性能基准、兼容性变化或技术路线变化
- 会影响多个社区、多个发行版、上游项目或广泛生态的严重漏洞/供应链问题

低分或 should_keep=false：
- 社区活动、会议、播客、招聘、营销、入门教程、普通公告
- 普通 CVE 罗列、只影响单一厂商/单一产品的小范围漏洞
- 文档首页、仓库首页、列表页、登录页、验证码页、反爬挑战页
- “Making sure you're not a bot / Anubis / Proof-of-Work / Hashcash / enable JavaScript / browser verification”等页面

输出严格 JSON，不要多余文字。格式：
{{
  "scores": [
    {{"url": "候选URL", "score": 0-100, "reason": "一句中文原因", "should_keep": true}}
  ]
}}

要求：
- 每个输入候选都必须返回一条 score，url 必须原样复制
- score 是整数，0=完全无价值或反爬页，100=非常关键的技术新闻
- should_keep=false 的候选即使分数较高也不能排入前 5
- 不要因为发布时间新就给高分；发布时间只能作为同分时的次要因素

候选列表：
{candidates}
"""


def _extract_json_object(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        return json.loads(brace.group(0))
    raise ValueError(f"No JSON object found in LLM output: {text[:200]}")


class CandidateQualityScorer:
    def __init__(self, llm: _Completer | None = None):
        if llm is None:
            from app.llm.client import LlmClient

            llm = LlmClient()
        self._llm = llm

    def score(self, items: list[RawItem]) -> dict[str, tuple[int, bool]]:
        if not items:
            return {}
        prompt = _CANDIDATE_SCORE_PROMPT.format(
            candidates=json.dumps(
                [self._candidate_payload(item) for item in items],
                ensure_ascii=False,
                indent=2,
            )
        )
        raw = self._llm.complete(prompt, temperature=0.0)
        data = _extract_json_object(raw)

        scores: dict[str, tuple[int, bool]] = {}
        for row in data.get("scores", []):
            url = row.get("url")
            if not isinstance(url, str):
                continue
            raw_score = row.get("score", 0)
            try:
                score = int(raw_score)
            except (TypeError, ValueError):
                score = 0
            score = max(0, min(100, score))
            should_keep = bool(row.get("should_keep", True))
            scores[url] = (score, should_keep)
        return scores

    def _candidate_payload(self, item: RawItem) -> dict[str, str | None]:
        return {
            "title": item.title,
            "url": item.url,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "snippet": (item.raw_content or "")[:800],
        }


def _resolve_missing_date_policy() -> MissingDatePolicy:
    """Read ``MISSING_DATE_POLICY`` from settings at call time (not import)
    so tests can change the value."""
    from app.config import get_settings

    raw = get_settings().missing_date_policy
    try:
        return MissingDatePolicy(raw)
    except ValueError:
        logger.warning("invalid MISSING_DATE_POLICY=%r, falling back to exclude", raw)
        return MissingDatePolicy.EXCLUDE

@dataclass
class _RuntimeState:
    state: str = "idle"
    request: ManualNewsRunRequest | None = None
    discovered_count: int = 0
    queued_count: int = 0
    processed_count: int = 0
    saved_count: int = 0
    fulfilled: bool = False
    gap_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    stop_requested: bool = False
    time_filter_stats: TimeFilterStats | None = None


class ManualNewsRunController:
    """Thread-safe controller for target-driven news collection tasks.

    A task progresses through two phases:

    1. **collecting** — iterate enabled news sources; if queued candidates
       fall short of *target_count*, perform up to ``_MAX_EXPANSION_ROUNDS``
       of expansion on search-type sources.
    2. **processing** — sort candidates by recency, enrich the top
       *target_count* items, and persist new entries.

    When the task finishes it reports whether the target was met
    (``fulfilled``) and, if not, why (``gap_reason``).
    """

    def __init__(self):
        self._lock = Lock()
        self._runtime = _RuntimeState()

    # -- lifecycle --------------------------------------------------------

    def start(self, request: ManualNewsRunRequest, now: datetime | None = None) -> bool:
        with self._lock:
            if self._runtime.state in {"collecting", "processing", "stopping"}:
                return False
            self._runtime = _RuntimeState(
                state="collecting",
                request=request,
                started_at=now or datetime.now(timezone.utc),
            )
            return True

    def stop(self) -> None:
        with self._lock:
            if self._runtime.state in {"collecting", "processing"}:
                self._runtime.state = "stopping"
                self._runtime.stop_requested = True

    def should_stop(self) -> bool:
        with self._lock:
            return self._runtime.stop_requested

    def mark_collecting(self) -> None:
        """Transition to collecting state (called by runner thread)."""
        with self._lock:
            if self._runtime.state in {"processing", "collecting"}:
                self._runtime.state = "collecting"

    def mark_processing(self) -> None:
        """Transition from collecting → processing (called by runner thread)."""
        with self._lock:
            if self._runtime.state == "collecting":
                self._runtime.state = "processing"

    def complete(
        self,
        state: str = "completed",
        error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        with self._lock:
            self._runtime.state = state
            self._runtime.last_error = error
            self._runtime.finished_at = now or datetime.now(timezone.utc)
            self._runtime.stop_requested = False

    # -- counters ---------------------------------------------------------

    def mark_discovered(self, count: int) -> None:
        with self._lock:
            self._runtime.discovered_count = count

    def mark_queued(self, count: int) -> None:
        with self._lock:
            self._runtime.queued_count = count

    def mark_processed(self, count: int) -> None:
        with self._lock:
            self._runtime.processed_count = count

    def mark_saved(self, count: int) -> None:
        with self._lock:
            self._runtime.saved_count = count

    def set_fulfilled(self, value: bool) -> None:
        with self._lock:
            self._runtime.fulfilled = value

    def set_gap_reason(self, reason: str | None) -> None:
        with self._lock:
            self._runtime.gap_reason = reason

    def set_time_filter_stats(
        self,
        *,
        missing_pub: int = 0,
        before: int = 0,
        after: int = 0,
        matched: int = 0,
        included_without_date: int = 0,
    ) -> None:
        with self._lock:
            self._runtime.time_filter_stats = TimeFilterStats(
                missing_published_at=missing_pub,
                before_start=before,
                after_end=after,
                matched=matched,
                included_without_date=included_without_date,
            )

    # -- candidate helpers ------------------------------------------------

    def filter_candidates(
        self,
        request: ManualNewsRunRequest,
        items: list[RawItem],
        now: datetime | None = None,
    ) -> list[RawItem]:
        """Time-filter and sort candidates.  Does **not** truncate to
        *target_count* — that is a collection goal, not a hard cap."""
        current_time = now or datetime.now(timezone.utc)
        filtered = [
            item for item in items
            if self._matches_time_window(request, item, current_time)
        ]
        filtered.sort(
            key=lambda item: item.published_at or datetime.now(timezone.utc),
            reverse=True,
        )
        return filtered

    def filter_candidates_with_stats(
        self,
        request: ManualNewsRunRequest,
        items: list[RawItem],
        now: datetime | None = None,
    ) -> tuple[list[RawItem], TimeFilterStats]:
        """Time-filter and sort candidates, returning both the filtered
        list and per-category statistics.

        The stats answer *why* each item was excluded (or included) so
        operators can diagnose empty or low-yield runs.
        """
        current_time = now or datetime.now(timezone.utc)
        stats = TimeFilterStats()
        filtered: list[RawItem] = []

        policy = _resolve_missing_date_policy()

        for item in items:
            if item.published_at is None:
                if (
                    request.time_mode == "relative"
                    and policy == MissingDatePolicy.INCLUDE_AS_NOW
                ):
                    stats.included_without_date += 1
                    stats.matched += 1
                    filtered.append(item)
                else:
                    stats.missing_published_at += 1
                continue

            item_ts = _as_utc(item.published_at)

            if request.time_mode == "relative":
                lower_bound = _as_utc(current_time) - _RELATIVE_RANGE_TO_DELTA[request.relative_range]
                if item_ts < lower_bound:
                    stats.before_start += 1
                else:
                    stats.matched += 1
                    filtered.append(item)
            else:
                start_ts = _as_utc(request.start_at)  # type: ignore[arg-type]
                end_ts = _as_utc(request.end_at)  # type: ignore[arg-type]
                if item_ts < start_ts:
                    stats.before_start += 1
                elif item_ts > end_ts:
                    stats.after_end += 1
                else:
                    stats.matched += 1
                    filtered.append(item)

        filtered.sort(
            key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return filtered, stats

    def merge_candidates(
        self,
        request: ManualNewsRunRequest,
        existing_items: list[RawItem],
        incoming_items: list[RawItem],
        now: datetime | None = None,
    ) -> list[RawItem]:
        """Merge incoming candidates into the existing list, deduplicating by
        URL and re-filtering to the time window."""
        seen_urls = {item.url for item in existing_items}
        deduped_incoming = [
            item for item in incoming_items
            if item.url not in seen_urls
        ]
        return self.filter_candidates(
            request,
            [*existing_items, *deduped_incoming],
            now=now,
        )

    def limit_candidates_per_source(
        self,
        items: list[RawItem],
        *,
        scorer: CandidateQualityScorer | None = None,
    ) -> list[RawItem]:
        """Keep at most the LLM highest-scored candidates from one source fetch."""
        if not items:
            return items
        if scorer is None:
            scorer = CandidateQualityScorer()
        try:
            scores = scorer.score(items)
        except Exception:
            logger.exception("candidate quality scoring failed; falling back to recency")
            scores = {}

        rankable_items = [
            item for item in items
            if scores.get(item.url, (0, True))[1]
        ]
        ordered = sorted(
            rankable_items,
            key=lambda item: (
                scores.get(item.url, (0, True))[0],
                item.published_at or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
        return ordered[:MAX_CANDIDATES_PER_SOURCE_PER_ROUND]

    # -- status -----------------------------------------------------------

    def status(self) -> ManualNewsRunStatus:
        with self._lock:
            request = self._runtime.request
            return ManualNewsRunStatus(
                state=self._runtime.state,
                time_mode=request.time_mode if request else None,
                relative_range=request.relative_range if request else None,
                start_at=request.start_at if request else None,
                end_at=request.end_at if request else None,
                target_count=request.target_count if request else None,
                discovered_count=self._runtime.discovered_count,
                queued_count=self._runtime.queued_count,
                processed_count=self._runtime.processed_count,
                saved_count=self._runtime.saved_count,
                fulfilled=self._runtime.fulfilled,
                gap_reason=self._runtime.gap_reason,
                started_at=self._runtime.started_at,
                finished_at=self._runtime.finished_at,
                last_error=self._runtime.last_error,
                time_filter_stats=self._runtime.time_filter_stats,
            )

    # -- time window ------------------------------------------------------

    def _matches_time_window(
        self,
        request: ManualNewsRunRequest,
        item: RawItem,
        now: datetime,
    ) -> bool:
        if item.published_at is None:
            return (
                request.time_mode == "relative"
                and _resolve_missing_date_policy() == MissingDatePolicy.INCLUDE_AS_NOW
            )
        item_ts = _as_utc(item.published_at)
        if request.time_mode == "relative":
            lower_bound = _as_utc(now) - _RELATIVE_RANGE_TO_DELTA[request.relative_range]
            return item_ts >= lower_bound
        return (
            _as_utc(request.start_at) <= item_ts <= _as_utc(request.end_at)
        )


# -- module-level singleton & runner --------------------------------------

_controller = ManualNewsRunController()
_thread_lock = Lock()
_runner_thread: threading.Thread | None = None


def get_manual_news_run_status() -> ManualNewsRunStatus:
    return _controller.status()


def start_manual_news_run(request: ManualNewsRunRequest) -> bool:
    global _runner_thread

    if not _controller.start(request):
        return False

    with _thread_lock:
        _runner_thread = threading.Thread(
            target=_run_manual_news_run,
            args=(request,),
            name="manual-news-run",
            daemon=True,
        )
        _runner_thread.start()
    return True


def stop_manual_news_run() -> ManualNewsRunStatus:
    _controller.stop()
    return _controller.status()


def _run_manual_news_run(request: ManualNewsRunRequest) -> None:
    """Target-driven news collection loop.

    Repeatedly collects candidates then processes them until
    ``saved_count >= target_count`` or no more candidates can be found.
    """
    from app.db import SessionLocal
    from app.enums import SourceType
    from app.extract.scrapling_extractor import ScraplingExtractor
    from app.models import Source
    from app.pipeline import Pipeline
    from app.processing.enricher import Enricher
    from app.scheduler import build_fetcher, list_enabled_news_sources
    from app.search.base import get_search_provider

    logger.info(
        "manual news run: started, target saved=%d", request.target_count,
    )

    # ── Per-run state ──────────────────────────────────────────────
    all_candidates: list[RawItem] = []       # full candidate pool
    seen_urls: set[str] = set()              # for URL dedup across rounds
    processed_urls: set[str] = set()         # already fed to pipeline
    discovered_total = 0
    processed_total = 0
    saved_total = 0
    collection_round = 0
    # Accumulated time-filter stats across all sources / rounds.
    acc_stats = TimeFilterStats()
    candidate_scorer = CandidateQualityScorer()

    # Discover sources once.
    discover_session = SessionLocal()
    try:
        all_sources = list_enabled_news_sources(discover_session)
        search_sources = [s for s in all_sources if s.type == SourceType.SEARCH]
    finally:
        discover_session.close()

    # ── Outer loop: collect → process → repeat if needed ──────────
    while saved_total < request.target_count:
        collection_round += 1

        # --- Collect ---
        _controller.mark_collecting()

        sources_for_round = all_sources if collection_round == 1 else search_sources
        new_in_round = 0

        if collection_round > _MAX_EXPANSION_ROUNDS + 1:
            logger.info("manual news run: exceeded max collection rounds")
            break

        collect_session = SessionLocal()
        try:
            search = get_search_provider()

            for source in sources_for_round:
                if _controller.should_stop():
                    _controller.complete(state="stopped")
                    return

                # Re-attach the source to the active session so that
                # modifications (e.g. last_content_hash, health_status)
                # are persisted on commit.
                source = collect_session.merge(source)

                # Create extractor per-source so stealth is honored.
                extractor = ScraplingExtractor(use_stealth=source.stealth)
                fetcher = build_fetcher(source, extractor, search)
                try:
                    candidates = fetcher.fetch(source)
                except Exception:
                    logger.exception("fetch failed for source %s (round %d)", source.name, collection_round)
                    source.fail_count += 1
                    source.health_status = "error"
                    collect_session.commit()
                    continue

                discovered_total += len(candidates)
                _controller.mark_discovered(discovered_total)

                time_filtered, round_stats = _controller.filter_candidates_with_stats(request, candidates)
                # Accumulate stats across sources.
                acc_stats.missing_published_at += round_stats.missing_published_at
                acc_stats.before_start += round_stats.before_start
                acc_stats.after_end += round_stats.after_end
                acc_stats.matched += round_stats.matched
                acc_stats.included_without_date += round_stats.included_without_date
                _controller.set_time_filter_stats(
                    missing_pub=acc_stats.missing_published_at,
                    before=acc_stats.before_start,
                    after=acc_stats.after_end,
                    matched=acc_stats.matched,
                    included_without_date=acc_stats.included_without_date,
                )

                limited_candidates = _controller.limit_candidates_per_source(
                    time_filtered,
                    scorer=candidate_scorer,
                )
                new_items = [c for c in limited_candidates if c.url not in seen_urls]
                for c in new_items:
                    seen_urls.add(c.url)
                all_candidates.extend(new_items)
                new_in_round += len(new_items)
                _controller.mark_queued(len(all_candidates))

                source.health_status = "ok"
                collect_session.commit()
        except Exception as exc:
            logger.exception("manual news run collection failed")
            _controller.complete(state="failed", error=str(exc))
            return
        finally:
            collect_session.close()

        # --- Determine unprocessed candidates (newest first) ---
        unprocessed = [
            c for c in all_candidates
            if c.url not in processed_urls
        ]
        unprocessed.sort(
            key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        if not unprocessed:
            logger.info("manual news run: no unprocessed candidates, round=%d", collection_round)
            break

        # --- Process ---
        _controller.mark_processing()

        process_session = SessionLocal()
        try:
            pipeline = Pipeline(
                session=process_session,
                extractor=ScraplingExtractor(),
                enricher=Enricher(),
            )

            for raw in unprocessed:
                if _controller.should_stop():
                    _controller.complete(state="stopped")
                    return

                # Stop processing early if target already met.
                if saved_total >= request.target_count:
                    break

                source = process_session.get(Source, raw.source_id)
                if source is None or not source.enabled:
                    processed_urls.add(raw.url)
                    continue

                saved = pipeline.process_item(source, raw)
                processed_total += 1
                processed_urls.add(raw.url)
                if saved:
                    saved_total += 1
                _controller.mark_processed(processed_total)
                _controller.mark_saved(saved_total)
                source.health_status = "ok"
                process_session.commit()

                if saved_total >= request.target_count:
                    break

        except Exception as exc:
            logger.exception("manual news run processing failed")
            _controller.complete(state="failed", error=str(exc))
            return
        finally:
            process_session.close()

        # --- Check exit conditions ---
        if saved_total >= request.target_count:
            break

        # No new candidates this round and nothing left to process.
        if new_in_round == 0 and not [
            c for c in all_candidates if c.url not in processed_urls
        ]:
            break

        # No search sources for expansion.
        if collection_round > 1 and not search_sources:
            break

    # ── Finalise ──────────────────────────────────────────────────
    fulfilled = saved_total >= request.target_count
    _controller.set_fulfilled(fulfilled)

    # Always log the time-filter stats so operators can diagnose low-yield runs.
    logger.info(
        "manual news run: time-filter stats — "
        "discovered=%d, matched=%d, missing_pub=%d, before_start=%d, after_end=%d",
        discovered_total,
        acc_stats.matched,
        acc_stats.missing_published_at,
        acc_stats.before_start,
        acc_stats.after_end,
    )

    if fulfilled:
        _controller.set_gap_reason(None)
    else:
        shortage = request.target_count - saved_total
        # Build a time-filter summary so users can see *why* candidates
        # were discarded.
        filter_detail_parts = [
            f"发现 {discovered_total} 条",
            f"时间命中 {acc_stats.matched} 条",
        ]
        if acc_stats.included_without_date:
            filter_detail_parts.append(f"无日期视为当前 {acc_stats.included_without_date} 条")
        if acc_stats.missing_published_at:
            filter_detail_parts.append(f"缺少发布时间 {acc_stats.missing_published_at} 条")
        if acc_stats.before_start:
            filter_detail_parts.append(f"早于开始时间 {acc_stats.before_start} 条")
        if acc_stats.after_end:
            filter_detail_parts.append(f"晚于结束时间 {acc_stats.after_end} 条")
        filter_detail = "；".join(filter_detail_parts)

        if collection_round == 1:
            _controller.set_gap_reason(
                f"入库不足：共新增入库 {saved_total} 条（目标 {request.target_count}），"
                f"缺少 {shortage} 条，且无可扩展的搜索型数据源。"
                f"时间过滤统计：{filter_detail}"
            )
        else:
            _controller.set_gap_reason(
                f"入库不足：经过 {collection_round} 轮采集处理，共新增入库 {saved_total} 条"
                f"（目标 {request.target_count}），缺少 {shortage} 条，已无新增候选。"
                f"时间过滤统计：{filter_detail}"
            )

    if _controller.should_stop():
        _controller.complete(state="stopped")
        return
    _controller.complete(state="completed")
