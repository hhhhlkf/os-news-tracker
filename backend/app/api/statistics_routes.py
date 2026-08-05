"""System-admin token usage analytics."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_system_access
from app.discovery.naming import (
    INTERNAL_FORUM_NAME_PREFIX,
    WECHAT_HISTORY_NAME_PREFIX,
    WECHAT_SEARCH_NAME_PREFIX,
    WEBSITE_NAME_PREFIX,
)
from app.enums import Importance
from app.models import (
    CrawlMethod,
    CrawlMethodRun,
    Item,
    LlmUsageEvent,
    MorningCrawlRun,
    MorningCrawlRunMethod,
    SiteDiscoveryRun,
    Source,
)

router = APIRouter(prefix="/statistics", tags=["statistics"])


def _utc(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _range(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    range_end = _utc(end, now)
    range_start = _utc(start, range_end - timedelta(days=30))
    if range_start >= range_end:
        range_start = range_end - timedelta(days=30)
    return range_start, range_end


def _events(
    db: Session,
    *,
    start: datetime,
    end: datetime,
    context_type: str | None = None,
) -> list[LlmUsageEvent]:
    stmt = select(LlmUsageEvent).where(
        LlmUsageEvent.occurred_at >= start,
        LlmUsageEvent.occurred_at < end,
    )
    if context_type is not None:
        stmt = stmt.where(LlmUsageEvent.context_type == context_type)
    return list(db.scalars(stmt.order_by(LlmUsageEvent.occurred_at.asc())))


def _totals(events: list[LlmUsageEvent]) -> dict[str, int]:
    return {
        "prompt_tokens": sum(event.prompt_tokens for event in events),
        "completion_tokens": sum(event.completion_tokens for event in events),
        "total_tokens": sum(event.total_tokens for event in events),
    }


def _exact_since(db: Session) -> str | None:
    value = db.scalar(select(func.min(LlmUsageEvent.occurred_at)))
    return value.isoformat() if value is not None else None


def _bucket_start(value: datetime, bucket: str) -> datetime:
    value = _utc(value, datetime.now(timezone.utc))
    if bucket == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


@router.get("/token-usage/summary")
def token_usage_summary(
    start: datetime | None = None,
    end: datetime | None = None,
    bucket: Literal["hour", "day"] = Query("day"),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> dict[str, Any]:
    range_start, range_end = _range(start, end)
    events = _events(db, start=range_start, end=range_end)
    summary = _totals(events)
    summary["query_tokens"] = sum(event.total_tokens for event in events if event.context_type == "query")
    summary["discovery_tokens"] = sum(
        event.total_tokens for event in events if event.context_type == "discovery"
    )
    summary["call_count"] = len(events)

    grouped: dict[datetime, dict[str, int]] = defaultdict(
        lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
    for event in events:
        point = grouped[_bucket_start(event.occurred_at, bucket)]
        point["prompt_tokens"] += event.prompt_tokens
        point["completion_tokens"] += event.completion_tokens
        point["total_tokens"] += event.total_tokens
    trend = [{"bucket": key.isoformat(), **grouped[key]} for key in sorted(grouped)]
    return {
        "start": range_start.isoformat(),
        "end": range_end.isoformat(),
        "bucket": bucket,
        "exact_since": _exact_since(db),
        "summary": summary,
        "trend": trend,
    }


_SOURCE_NAME_PREFIXES = (
    WEBSITE_NAME_PREFIX,
    WECHAT_SEARCH_NAME_PREFIX,
    WECHAT_HISTORY_NAME_PREFIX,
    INTERNAL_FORUM_NAME_PREFIX,
)


def _display_method_label(source_name: str | None, domain: str | None, method_id: int) -> str:
    """Use the human name after prefixes like 网站：/公众号：; fall back to domain."""
    raw = (source_name or domain or "").strip()
    if not raw:
        return f"方式 {method_id}"
    for prefix in _SOURCE_NAME_PREFIXES:
        if raw.startswith(prefix):
            short = raw[len(prefix):].strip()
            return short or raw
    return raw


def _method_labels(db: Session, method_ids: set[int]) -> dict[int, str]:
    if not method_ids:
        return {}
    methods = db.execute(
        select(CrawlMethod.id, CrawlMethod.domain, Source.name)
        .outerjoin(Source, Source.id == CrawlMethod.source_id)
        .where(CrawlMethod.id.in_(method_ids))
    ).all()
    return {
        method_id: _display_method_label(source_name, domain, method_id)
        for method_id, domain, source_name in methods
    }


# Group aliases for query-run filters (UI collapses related trigger types).
_TRIGGER_TYPE_GROUPS: dict[str, set[str]] = {
    "scheduled": {"scheduled", "patrol_resend"},
    "manual": {"manual", "manual_method"},
}


def _matches_trigger_filter(event_trigger: str | None, filter_value: str) -> bool:
    allowed = _TRIGGER_TYPE_GROUPS.get(filter_value)
    if allowed is not None:
        return event_trigger in allowed
    return event_trigger == filter_value


def _payload_batch_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("batch_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _aggregate_run_status(statuses: list[str]) -> str:
    if not statuses:
        return "unknown"
    if any(status == "running" for status in statuses):
        return "running"
    if all(status in {"cancelled"} for status in statuses):
        return "cancelled"
    if all(status in {"success", "ok", "empty", "partial", "completed"} for status in statuses):
        if any(status == "partial" for status in statuses):
            return "partial"
        if any(status == "empty" for status in statuses) and not any(
            status in {"success", "ok", "completed"} for status in statuses
        ):
            return "empty"
        return "success"
    if any(status == "failed" for status in statuses):
        return "failed"
    return statuses[0]


@router.get("/token-usage/query-runs")
def token_usage_query_runs(
    start: datetime | None = None,
    end: datetime | None = None,
    trigger_type: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> dict[str, Any]:
    range_start, range_end = _range(start, end)
    events = _events(db, start=range_start, end=range_end, context_type="query")
    if trigger_type:
        events = [event for event in events if _matches_trigger_filter(event.trigger_type, trigger_type)]

    morning_ids = {event.morning_crawl_run_id for event in events if event.morning_crawl_run_id is not None}
    method_run_ids = {event.crawl_method_run_id for event in events if event.crawl_method_run_id is not None}
    mornings = {
        run.id: run
        for run in db.scalars(select(MorningCrawlRun).where(MorningCrawlRun.id.in_(morning_ids)))
    } if morning_ids else {}
    method_runs = {
        run.id: run
        for run in db.scalars(select(CrawlMethodRun).where(CrawlMethodRun.id.in_(method_run_ids)))
    } if method_run_ids else {}

    # morning → one bar; method runs sharing request_payload.batch_id → one stacked bar.
    grouped: dict[tuple[str, int | str], list[LlmUsageEvent]] = defaultdict(list)
    for event in events:
        if event.morning_crawl_run_id is not None:
            grouped[("morning", event.morning_crawl_run_id)].append(event)
            continue
        if event.crawl_method_run_id is None:
            continue
        run = method_runs.get(event.crawl_method_run_id)
        batch_id = _payload_batch_id(run.request_payload if run is not None else None)
        if batch_id is not None:
            grouped[("batch", batch_id)].append(event)
        else:
            grouped[("method", event.crawl_method_run_id)].append(event)

    all_method_ids = {event.method_id for event in events if event.method_id is not None}
    labels = _method_labels(db, all_method_ids)

    rows: list[dict[str, Any]] = []
    for (kind, group_id), run_events in grouped.items():
        per_method: dict[int, list[LlmUsageEvent]] = defaultdict(list)
        for event in run_events:
            if event.method_id is not None:
                per_method[event.method_id].append(event)
        segments = [
            {
                "method_id": method_id,
                "label": labels.get(method_id, f"方式 {method_id}"),
                **_totals(method_events),
            }
            for method_id, method_events in sorted(per_method.items())
        ]
        if kind == "morning":
            run = mornings.get(int(group_id))
            if run is None:
                continue
            started_at = run.started_at
            finished_at = run.finished_at
            status = run.status
            run_trigger = run.trigger_type
            run_id = run.id
        elif kind == "batch":
            batch_run_ids = {
                event.crawl_method_run_id
                for event in run_events
                if event.crawl_method_run_id is not None
            }
            batch_runs = [method_runs[run_id] for run_id in batch_run_ids if run_id in method_runs]
            if not batch_runs:
                continue
            started_times = [run.started_at for run in batch_runs if run.started_at is not None]
            finished_times = [run.completed_at for run in batch_runs if run.completed_at is not None]
            started_at = min(started_times) if started_times else None
            finished_at = max(finished_times) if finished_times else None
            status = _aggregate_run_status([run.status for run in batch_runs])
            run_trigger = next(
                (event.trigger_type for event in run_events if event.trigger_type),
                "manual",
            )
            run_id = min(batch_run_ids)
        else:
            run = method_runs.get(int(group_id))
            if run is None:
                continue
            started_at = run.started_at
            finished_at = run.completed_at
            status = run.status
            run_trigger = next(
                (event.trigger_type for event in run_events if event.trigger_type),
                "manual_method",
            )
            run_id = run.id
        rows.append(
            {
                "run_key": f"{kind}:{group_id}",
                "run_id": run_id,
                "kind": kind,
                "trigger_type": run_trigger,
                "status": status,
                "started_at": started_at.isoformat() if started_at else None,
                "finished_at": finished_at.isoformat() if finished_at else None,
                **_totals(run_events),
                "methods": segments,
            }
        )
    rows.sort(key=lambda row: row.get("started_at") or "", reverse=True)
    return {
        "exact_since": _exact_since(db),
        "total": len(rows),
        "runs": rows[offset:offset + limit],
    }


def _avg_tokens_per_item(total_tokens: int, item_count: int) -> tuple[float, float]:
    """Average tokens per discovered item.

    Zero items with positive spend uses divisor 0.2 (never divide by zero).
    """
    divisor = 0.2 if item_count <= 0 else float(item_count)
    return total_tokens / divisor, divisor


@router.get("/token-usage/query-item-avg")
def token_usage_query_item_avg(
    start: datetime | None = None,
    end: datetime | None = None,
    trigger_type: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> dict[str, Any]:
    """Same time grain as query-runs: one stacked bar per query, segment = per-source avg.

    - Denominator is ``discovered_count`` for that source within the query.
    - If items == 0 but tokens > 0, divide by 0.2.
    - Sources with zero token spend are omitted from the stack.
    """
    range_start, range_end = _range(start, end)
    events = _events(db, start=range_start, end=range_end, context_type="query")
    if trigger_type:
        events = [event for event in events if _matches_trigger_filter(event.trigger_type, trigger_type)]

    morning_ids = {event.morning_crawl_run_id for event in events if event.morning_crawl_run_id is not None}
    method_run_ids = {event.crawl_method_run_id for event in events if event.crawl_method_run_id is not None}
    morning_method_ids = {
        event.morning_crawl_run_method_id
        for event in events
        if event.morning_crawl_run_method_id is not None
    }
    mornings = {
        run.id: run
        for run in db.scalars(select(MorningCrawlRun).where(MorningCrawlRun.id.in_(morning_ids)))
    } if morning_ids else {}
    method_runs = {
        run.id: run
        for run in db.scalars(select(CrawlMethodRun).where(CrawlMethodRun.id.in_(method_run_ids)))
    } if method_run_ids else {}
    morning_methods = {
        row.id: row
        for row in db.scalars(
            select(MorningCrawlRunMethod).where(MorningCrawlRunMethod.id.in_(morning_method_ids))
        )
    } if morning_method_ids else {}
    # Also load morning method rows by run for discovered_count lookup when events
    # only carry morning_crawl_run_id + method_id.
    morning_methods_by_run: dict[int, list[MorningCrawlRunMethod]] = defaultdict(list)
    if morning_ids:
        for row in db.scalars(
            select(MorningCrawlRunMethod).where(MorningCrawlRunMethod.run_id.in_(morning_ids))
        ):
            morning_methods_by_run[row.run_id].append(row)

    # Same grouping as query-runs: morning / batch / single method.
    grouped: dict[tuple[str, int | str], list[LlmUsageEvent]] = defaultdict(list)
    for event in events:
        if event.morning_crawl_run_id is not None:
            grouped[("morning", event.morning_crawl_run_id)].append(event)
            continue
        if event.crawl_method_run_id is None:
            continue
        run = method_runs.get(event.crawl_method_run_id)
        batch_id = _payload_batch_id(run.request_payload if run is not None else None)
        if batch_id is not None:
            grouped[("batch", batch_id)].append(event)
        else:
            grouped[("method", event.crawl_method_run_id)].append(event)

    all_method_ids = {event.method_id for event in events if event.method_id is not None}
    labels = _method_labels(db, all_method_ids)

    def _item_count_for_method(kind: str, group_id: int | str, method_id: int, run_events: list[LlmUsageEvent]) -> int:
        if kind == "morning":
            for row in morning_methods_by_run.get(int(group_id), []):
                if row.method_id == method_id:
                    return int(row.discovered_count or 0)
            # Fallback: match via morning_crawl_run_method_id on events.
            for event in run_events:
                if event.method_id != method_id or event.morning_crawl_run_method_id is None:
                    continue
                row = morning_methods.get(event.morning_crawl_run_method_id)
                if row is not None:
                    return int(row.discovered_count or 0)
            return 0
        # batch / method → CrawlMethodRun.discovered_count for that method's run(s).
        counts = [
            int(method_runs[event.crawl_method_run_id].discovered_count or 0)
            for event in run_events
            if event.method_id == method_id
            and event.crawl_method_run_id is not None
            and event.crawl_method_run_id in method_runs
        ]
        return max(counts) if counts else 0

    rows: list[dict[str, Any]] = []
    for (kind, group_id), run_events in grouped.items():
        per_method: dict[int, list[LlmUsageEvent]] = defaultdict(list)
        for event in run_events:
            if event.method_id is not None:
                per_method[event.method_id].append(event)

        segments: list[dict[str, Any]] = []
        for method_id, method_events in sorted(per_method.items()):
            totals = _totals(method_events)
            total_tokens = int(totals["total_tokens"] or 0)
            if total_tokens <= 0:
                continue
            item_count = _item_count_for_method(kind, group_id, method_id, run_events)
            avg_tokens, divisor = _avg_tokens_per_item(total_tokens, item_count)
            segments.append(
                {
                    "method_id": method_id,
                    "label": labels.get(method_id, f"方式 {method_id}"),
                    "item_count": item_count,
                    "divisor": divisor,
                    "avg_tokens_per_item": round(avg_tokens, 2),
                    **totals,
                }
            )
        if not segments:
            continue

        if kind == "morning":
            run = mornings.get(int(group_id))
            if run is None:
                continue
            started_at = run.started_at
            finished_at = run.finished_at
            status = run.status
            run_trigger = run.trigger_type
            run_id = run.id
        elif kind == "batch":
            batch_run_ids = {
                event.crawl_method_run_id
                for event in run_events
                if event.crawl_method_run_id is not None
            }
            batch_runs = [method_runs[run_id] for run_id in batch_run_ids if run_id in method_runs]
            if not batch_runs:
                continue
            started_times = [run.started_at for run in batch_runs if run.started_at is not None]
            finished_times = [run.completed_at for run in batch_runs if run.completed_at is not None]
            started_at = min(started_times) if started_times else None
            finished_at = max(finished_times) if finished_times else None
            status = _aggregate_run_status([run.status for run in batch_runs])
            run_trigger = next(
                (event.trigger_type for event in run_events if event.trigger_type),
                "manual",
            )
            run_id = min(batch_run_ids)
        else:
            run = method_runs.get(int(group_id))
            if run is None:
                continue
            started_at = run.started_at
            finished_at = run.completed_at
            status = run.status
            run_trigger = next(
                (event.trigger_type for event in run_events if event.trigger_type),
                "manual_method",
            )
            run_id = run.id

        avg_total = round(sum(seg["avg_tokens_per_item"] for seg in segments), 2)
        rows.append(
            {
                "run_key": f"{kind}:{group_id}",
                "run_id": run_id,
                "kind": kind,
                "trigger_type": run_trigger,
                "status": status,
                "started_at": started_at.isoformat() if started_at else None,
                "finished_at": finished_at.isoformat() if finished_at else None,
                "avg_tokens_per_item": avg_total,
                **_totals(run_events),
                "methods": segments,
            }
        )

    rows.sort(key=lambda row: row.get("started_at") or "", reverse=True)
    return {
        "exact_since": _exact_since(db),
        "total": len(rows),
        "runs": rows[offset:offset + limit],
    }


@router.get("/token-usage/discovery-runs")
def token_usage_discovery_runs(
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> dict[str, Any]:
    """List completed discovery runs that have exact ``LlmUsageEvent`` totals.

    Historical placeholder ``SiteDiscoveryRun.llm_token_usage`` stubs (e.g. +1000
    per dsl_writer step) are ignored — only event-backed usage is shown.
    """
    range_start, range_end = _range(start, end)
    runs = list(
        db.scalars(
            select(SiteDiscoveryRun)
            .where(
                SiteDiscoveryRun.started_at >= range_start,
                SiteDiscoveryRun.started_at < range_end,
                SiteDiscoveryRun.status == "completed",
            )
            .order_by(SiteDiscoveryRun.started_at.desc())
        ).all()
    )
    run_ids = [run.id for run in runs]
    grouped: dict[int, list[LlmUsageEvent]] = defaultdict(list)
    if run_ids:
        for event in db.scalars(
            select(LlmUsageEvent).where(
                LlmUsageEvent.context_type == "discovery",
                LlmUsageEvent.discovery_run_id.in_(run_ids),
            )
        ):
            if event.discovery_run_id is not None:
                grouped[event.discovery_run_id].append(event)

    rows = []
    for run in runs:
        run_events = grouped.get(run.id) or []
        if not run_events:
            continue
        totals = _totals(run_events)
        if int(totals["total_tokens"] or 0) <= 0:
            continue
        rows.append(
            {
                "run_id": run.id,
                "site_url": run.site_url,
                "status": run.status,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "ended_at": run.ended_at.isoformat() if run.ended_at else None,
                "exact": True,
                **totals,
            }
        )
    return {
        "exact_since": _exact_since(db),
        "total": len(rows),
        "runs": rows[offset:offset + limit],
    }


def _importance_bucket(value: str | None) -> Literal["high", "medium", "low"]:
    if value == Importance.HIGH or value == "高":
        return "high"
    if value == Importance.MEDIUM or value == "中":
        return "medium"
    return "low"


@router.get("/item-volume/daily")
def item_volume_daily(
    start: datetime | None = None,
    end: datetime | None = None,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> dict[str, Any]:
    """Daily message counts stacked by importance (高/中/低), based on fetched_at."""
    range_start, range_end = _range(start, end)
    day_expr = func.date_trunc("day", Item.fetched_at)
    rows = db.execute(
        select(day_expr, Item.importance, func.count())
        .where(Item.fetched_at >= range_start, Item.fetched_at < range_end)
        .group_by(day_expr, Item.importance)
        .order_by(day_expr.asc())
    ).all()

    grouped: dict[datetime, dict[str, int]] = {}
    cursor = _bucket_start(range_start, "day")
    end_day = _bucket_start(range_end - timedelta(microseconds=1), "day")
    while cursor <= end_day:
        grouped[cursor] = {"high": 0, "medium": 0, "low": 0, "total": 0}
        cursor += timedelta(days=1)

    for bucket, importance, count in rows:
        if bucket is None:
            continue
        day = _bucket_start(_utc(bucket, range_start), "day")
        point = grouped.setdefault(day, {"high": 0, "medium": 0, "low": 0, "total": 0})
        key = _importance_bucket(importance)
        point[key] += int(count)
        point["total"] += int(count)

    trend = [{"bucket": day.isoformat(), **grouped[day]} for day in sorted(grouped)]
    summary = {
        "high": sum(point["high"] for point in grouped.values()),
        "medium": sum(point["medium"] for point in grouped.values()),
        "low": sum(point["low"] for point in grouped.values()),
        "total": sum(point["total"] for point in grouped.values()),
    }
    return {
        "start": range_start.isoformat(),
        "end": range_end.isoformat(),
        "summary": summary,
        "trend": trend,
    }
