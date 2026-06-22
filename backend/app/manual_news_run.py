from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from queue import Queue
from threading import Event, Lock
import json
import logging
import re
import threading
from typing import Any, Protocol

from app.enums import MissingDatePolicy
from app.run_logs import append_run_log, clear_run_logs
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

# Bound LLM scoring prompts.  High-volume feeds can produce hundreds of rows;
# local prefiltering keeps queueing responsive while preserving LLM judgment.
CANDIDATE_LLM_PREFILTER_LIMIT = 30
CANDIDATE_SCORE_SNIPPET_LIMIT = 350

_TECH_SIGNAL_KEYWORDS = (
    "abi",
    "agent",
    "ai",
    "architecture",
    "benchmark",
    "bpf",
    "compiler",
    "container",
    "cve",
    "ebpf",
    "glibc",
    "kernel",
    "kubernetes",
    "linux",
    "llm",
    "openssl",
    "package",
    "performance",
    "rpm",
    "scheduler",
    "security",
    "supply chain",
    "toolchain",
    "vulnerability",
)

_LOW_VALUE_CANDIDATE_MARKERS = (
    "anubis",
    "browser verification",
    "community event",
    "conference",
    "enable javascript",
    "hashcash",
    "job",
    "making sure you're not a bot",
    "meetup",
    "podcast",
    "proof-of-work",
    "webinar",
    "确保您不是机器人",
)

logger = logging.getLogger(__name__)


def _build_not_stored_log_fields(result: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"reason": result.reason}
    if getattr(result, "detail", None):
        fields["reason_detail"] = result.detail
    return fields


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
        append_run_log(
            "llm_scoring",
            "候选质量 LLM 打分开始",
            count=len(items),
        )
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
        kept = sum(1 for score, should_keep in scores.values() if should_keep)
        append_run_log(
            "llm_scoring",
            "候选质量 LLM 打分完成",
            count=len(items),
            kept=kept,
            rejected=max(0, len(items) - kept),
        )
        return scores

    def _candidate_payload(self, item: RawItem) -> dict[str, str | None]:
        return {
            "title": item.title,
            "url": item.url,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "snippet": (item.raw_content or "")[:CANDIDATE_SCORE_SNIPPET_LIMIT],
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


def _local_candidate_prefilter(items: list[RawItem], *, limit: int) -> list[RawItem]:
    """Rank candidates cheaply before LLM scoring.

    This keeps large-source prompts bounded.  It is intentionally conservative:
    the LLM still makes the final keep/drop decision for the selected subset.
    """
    return sorted(items, key=_local_candidate_rank, reverse=True)[:limit]


def _local_candidate_rank(item: RawItem) -> tuple[int, datetime]:
    text = f"{item.title}\n{item.raw_content or ''}".lower()
    score = 0

    for marker in _LOW_VALUE_CANDIDATE_MARKERS:
        if marker in text:
            score -= 30

    for keyword in _TECH_SIGNAL_KEYWORDS:
        if keyword in text:
            score += 6

    title = (item.title or "").lower()
    if any(keyword in title for keyword in _TECH_SIGNAL_KEYWORDS):
        score += 8
    if item.raw_content:
        score += min(len(item.raw_content), 1000) // 100
    if item.published_at is None:
        score -= 5

    published_at = item.published_at or datetime.min.replace(tzinfo=timezone.utc)
    return score, published_at


@dataclass
class CandidateRunCounts:
    discovered: int = 0
    queued: int = 0
    processed: int = 0
    saved: int = 0


class CandidateRunLedger:
    """In-memory per-run candidate state ledger.

    Status counters are derived from this ledger so UI numbers stay aligned
    even as collection and processing are interleaved.
    """

    _ACTIVE_STATUSES = {"queued", "processing", "saved", "rejected", "duplicate", "failed"}
    _TERMINAL_STATUSES = {"saved", "rejected", "duplicate", "failed"}

    def __init__(self) -> None:
        self._discovered = 0
        self._by_url: dict[str, str] = {}
        self._lock = Lock()

    def mark_discovered(self, count: int) -> None:
        with self._lock:
            self._discovered += count

    def add_queued(self, url: str) -> bool:
        with self._lock:
            if url in self._by_url:
                return False
            self._by_url[url] = "queued"
            return True

    def mark_processing(self, url: str) -> None:
        with self._lock:
            if url in self._by_url:
                self._by_url[url] = "processing"

    def mark_saved(self, url: str) -> None:
        self._mark_terminal(url, "saved")

    def mark_rejected(self, url: str) -> None:
        self._mark_terminal(url, "rejected")

    def mark_duplicate(self, url: str) -> None:
        self._mark_terminal(url, "duplicate")

    def mark_failed(self, url: str) -> None:
        self._mark_terminal(url, "failed")

    def _mark_terminal(self, url: str, status: str) -> None:
        with self._lock:
            if url in self._by_url:
                self._by_url[url] = status

    def counts(self) -> CandidateRunCounts:
        with self._lock:
            statuses = list(self._by_url.values())
            terminal = sum(1 for status in statuses if status in self._TERMINAL_STATUSES)
            saved = sum(1 for status in statuses if status == "saved")
            return CandidateRunCounts(
                discovered=self._discovered,
                queued=sum(1 for status in statuses if status in self._ACTIVE_STATUSES),
                processed=terminal,
                saved=saved,
            )


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
        llm_items = _local_candidate_prefilter(
            items,
            limit=CANDIDATE_LLM_PREFILTER_LIMIT,
        )
        append_run_log(
            "candidate_prefilter",
            "本地预筛候选",
            count=len(items),
            sent_to_llm=len(llm_items),
            cap=MAX_CANDIDATES_PER_SOURCE_PER_ROUND,
        )
        if scorer is None:
            scorer = CandidateQualityScorer()
        try:
            scores = scorer.score(llm_items)
        except Exception:
            logger.exception("candidate quality scoring failed; falling back to recency")
            scores = {}

        rankable_items = [
            item for item in llm_items
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


def _fetch_source_for_manual_run(source_id: int) -> dict:
    """Fetch one source in an isolated worker context."""
    from app.db import SessionLocal
    from app.extract.scrapling_extractor import ScraplingExtractor
    from app.models import Source
    from app.scheduler import build_fetcher
    from app.search.base import get_search_provider

    session = SessionLocal()
    try:
        source = session.get(Source, source_id)
        if source is None or not source.enabled:
            return {
                "source_id": source_id,
                "source_name": f"source:{source_id}",
                "candidates": [],
                "error": None,
            }
        source_name = source.name
        try:
            append_run_log(
                "fetch",
                "开始抓取 source",
                source=source_name,
                source_id=source_id,
                type=str(source.type),
                url=source.url,
            )
            extractor = ScraplingExtractor(use_stealth=source.stealth)
            fetcher = build_fetcher(source, extractor, get_search_provider())
            candidates = fetcher.fetch(source)
            source.health_status = "ok"
            session.commit()
            append_run_log(
                "fetch",
                "source 抓取完成",
                source=source_name,
                source_id=source_id,
                count=len(candidates),
            )
            return {
                "source_id": source_id,
                "source_name": source_name,
                "candidates": candidates,
                "error": None,
            }
        except Exception as exc:
            logger.exception("fetch failed for source %s", source_name)
            append_run_log(
                "fetch",
                "source 抓取失败",
                source=source_name,
                source_id=source_id,
                level="error",
                error=str(exc),
            )
            source.fail_count += 1
            source.health_status = "error"
            session.commit()
            return {
                "source_id": source_id,
                "source_name": source_name,
                "candidates": [],
                "error": str(exc),
            }
    finally:
        session.close()


def _run_manual_news_run(request: ManualNewsRunRequest) -> None:
    """Target-driven news collection loop with a staged worker pipeline."""
    from app.config import get_settings
    from app.db import SessionLocal
    from app.enums import SourceType
    from app.extract.scrapling_extractor import ScraplingExtractor
    from app.models import Source
    from app.pipeline import Pipeline
    from app.processing.enricher import Enricher
    from app.scheduler import list_enabled_news_sources

    clear_run_logs()
    append_run_log(
        "run",
        "手动新闻处理开始",
        target_count=request.target_count,
        time_mode=request.time_mode,
        relative_range=request.relative_range,
    )
    logger.info("manual news run: started, target saved=%d", request.target_count)

    ledger = CandidateRunLedger()
    seen_urls: set[str] = set()
    seen_lock = Lock()
    stats_lock = Lock()
    acc_stats = TimeFilterStats()
    stop_event = Event()
    raw_result_queue: Queue[dict[str, Any] | object] = Queue()
    candidate_queue: Queue[RawItem | object] = Queue()
    raw_sentinel = object()
    candidate_sentinel = object()

    settings = get_settings()
    max_fetch_workers = max(1, settings.manual_fetch_max_workers)
    scoring_workers = max(1, settings.llm_max_concurrency)

    def _sync_status_from_ledger() -> CandidateRunCounts:
        counts = ledger.counts()
        _controller.mark_discovered(counts.discovered)
        _controller.mark_queued(counts.queued)
        _controller.mark_processed(counts.processed)
        _controller.mark_saved(counts.saved)
        return counts

    def _target_reached() -> bool:
        return ledger.counts().saved >= request.target_count

    def _update_time_filter_stats(round_stats: TimeFilterStats) -> None:
        with stats_lock:
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

    def _candidate_worker() -> None:
        scorer = CandidateQualityScorer()
        while True:
            result = raw_result_queue.get()
            try:
                if result is raw_sentinel:
                    return
                if stop_event.is_set() or _controller.should_stop():
                    continue
                assert isinstance(result, dict)
                if result["error"]:
                    logger.warning(
                        "manual news run source failed: %s (%s)",
                        result["source_name"],
                        result["error"],
                    )
                    continue

                candidates = result["candidates"]
                ledger.mark_discovered(len(candidates))
                _sync_status_from_ledger()

                time_filtered, round_stats = _controller.filter_candidates_with_stats(request, candidates)
                append_run_log(
                    "time_filter",
                    "时间过滤完成",
                    source=result["source_name"],
                    count=len(candidates),
                    matched=round_stats.matched,
                    before_start=round_stats.before_start,
                    after_end=round_stats.after_end,
                    missing_published_at=round_stats.missing_published_at,
                )
                _update_time_filter_stats(round_stats)

                limited_candidates = _controller.limit_candidates_per_source(
                    time_filtered,
                    scorer=scorer,
                )
                new_items: list[RawItem] = []
                with seen_lock:
                    for item in limited_candidates:
                        if item.url in seen_urls:
                            continue
                        seen_urls.add(item.url)
                        if ledger.add_queued(item.url):
                            new_items.append(item)

                counts = _sync_status_from_ledger()
                append_run_log(
                    "queue",
                    "候选入队完成",
                    source=result["source_name"],
                    count=len(new_items),
                    queued_total=counts.queued,
                    saved_total=counts.saved,
                )
                logger.info(
                    "manual news run source queued: %s discovered=%d queued=%d saved=%d",
                    result["source_name"],
                    counts.discovered,
                    counts.queued,
                    counts.saved,
                )
                for item in new_items:
                    candidate_queue.put(item)
            finally:
                raw_result_queue.task_done()

    def _processing_worker() -> None:
        process_session = SessionLocal()
        try:
            pipeline = Pipeline(
                session=process_session,
                extractor=ScraplingExtractor(),
                enricher=Enricher(),
            )
            while True:
                raw = candidate_queue.get()
                try:
                    if raw is candidate_sentinel:
                        return
                    if stop_event.is_set() or _controller.should_stop():
                        continue
                    if _target_reached():
                        stop_event.set()
                        continue
                    assert isinstance(raw, RawItem)
                    _controller.mark_processing()
                    source = process_session.get(Source, raw.source_id)
                    if source is None or not source.enabled:
                        ledger.mark_failed(raw.url)
                        _sync_status_from_ledger()
                        continue

                    ledger.mark_processing(raw.url)
                    _sync_status_from_ledger()
                    try:
                        result = pipeline.process_item_result(source, raw)
                    except Exception as exc:
                        logger.exception("manual news run item processing failed for %s", raw.url)
                        ledger.mark_failed(raw.url)
                        append_run_log(
                            "process",
                            "候选处理失败",
                            source=source.name,
                            level="error",
                            title=raw.title,
                            url=raw.url,
                            reason="failed",
                            reason_detail=str(exc),
                        )
                    else:
                        if result.stored:
                            ledger.mark_saved(raw.url)
                            append_run_log(
                                "process",
                                "候选已新增入库",
                                source=source.name,
                                title=raw.title,
                                url=raw.url,
                            )
                        else:
                            if result.reason == "duplicate":
                                ledger.mark_duplicate(raw.url)
                            else:
                                ledger.mark_rejected(raw.url)
                            append_run_log(
                                "process",
                                "候选未入库",
                                source=source.name,
                                title=raw.title,
                                url=raw.url,
                                **_build_not_stored_log_fields(result),
                            )
                    source.health_status = "ok"
                    process_session.commit()
                    counts = _sync_status_from_ledger()
                    if counts.saved >= request.target_count:
                        stop_event.set()
                finally:
                    candidate_queue.task_done()
        finally:
            process_session.close()

    discover_session = SessionLocal()
    try:
        all_sources = list_enabled_news_sources(discover_session)
        all_source_ids = [source.id for source in all_sources]
        search_source_ids = [
            source.id for source in all_sources if source.type == SourceType.SEARCH
        ]
    finally:
        discover_session.close()

    candidate_workers = [
        threading.Thread(target=_candidate_worker, name=f"manual-candidate-{idx}", daemon=True)
        for idx in range(scoring_workers)
    ]
    for worker in candidate_workers:
        worker.start()
    processor = threading.Thread(target=_processing_worker, name="manual-processing", daemon=True)
    processor.start()

    collection_round = 0
    try:
        while not stop_event.is_set() and ledger.counts().saved < request.target_count:
            collection_round += 1
            _controller.mark_collecting()
            source_ids_for_round = all_source_ids if collection_round == 1 else search_source_ids

            if collection_round > _MAX_EXPANSION_ROUNDS + 1:
                logger.info("manual news run: exceeded max collection rounds")
                break
            if not source_ids_for_round:
                break

            queued_before = ledger.counts().queued
            try:
                worker_count = min(max_fetch_workers, len(source_ids_for_round)) or 1
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = [
                        executor.submit(_fetch_source_for_manual_run, source_id)
                        for source_id in source_ids_for_round
                    ]
                    for future in as_completed(futures):
                        if stop_event.is_set() or _controller.should_stop():
                            stop_event.set()
                            for pending in futures:
                                pending.cancel()
                            break
                        raw_result_queue.put(future.result())
            except Exception as exc:
                logger.exception("manual news run collection failed")
                _controller.complete(state="failed", error=str(exc))
                stop_event.set()
                return

            raw_result_queue.join()
            candidate_queue.join()

            counts = _sync_status_from_ledger()
            no_new_work = counts.queued == queued_before
            if counts.saved >= request.target_count:
                break
            if no_new_work:
                break
            if collection_round > 1 and not search_source_ids:
                break

        if _controller.should_stop():
            stop_event.set()
    finally:
        for _ in candidate_workers:
            raw_result_queue.put(raw_sentinel)
        raw_result_queue.join()
        candidate_queue.put(candidate_sentinel)
        candidate_queue.join()
        for worker in candidate_workers:
            worker.join(timeout=5)
        processor.join(timeout=5)

    final_counts = _sync_status_from_ledger()
    fulfilled = final_counts.saved >= request.target_count
    _controller.set_fulfilled(fulfilled)

    logger.info(
        "manual news run: time-filter stats — "
        "discovered=%d, matched=%d, missing_pub=%d, before_start=%d, after_end=%d",
        final_counts.discovered,
        acc_stats.matched,
        acc_stats.missing_published_at,
        acc_stats.before_start,
        acc_stats.after_end,
    )

    if fulfilled:
        _controller.set_gap_reason(None)
    else:
        shortage = request.target_count - final_counts.saved
        filter_detail_parts = [
            f"发现 {final_counts.discovered} 条",
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
                f"入库不足：共新增入库 {final_counts.saved} 条（目标 {request.target_count}），"
                f"缺少 {shortage} 条，且无可扩展的搜索型数据源。"
                f"时间过滤统计：{filter_detail}"
            )
        else:
            _controller.set_gap_reason(
                f"入库不足：经过 {collection_round} 轮采集处理，共新增入库 {final_counts.saved} 条"
                f"（目标 {request.target_count}），缺少 {shortage} 条，已无新增候选。"
                f"时间过滤统计：{filter_detail}"
            )

    if _controller.should_stop():
        append_run_log("run", "手动新闻处理已停止", level="warning")
        _controller.complete(state="stopped")
        return
    append_run_log(
        "run",
        "手动新闻处理完成",
        saved=final_counts.saved,
        processed=final_counts.processed,
        queued=final_counts.queued,
        fulfilled=fulfilled,
    )
    _controller.complete(state="completed")
