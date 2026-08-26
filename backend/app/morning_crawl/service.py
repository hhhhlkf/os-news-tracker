"""系统定时抓取服务：单例配置、今日状态聚合、执行全部 active discovery methods。

时间语义：配置与运行记录的所有墙钟时间统一使用北京时间（UTC+8），与邮件模块一致，
前端原样展示。定时抓取只使用 discovery methods（CrawlMethod），不碰旧 sources 业务模型。
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    CrawlMethod,
    MorningCrawlConfig,
    MorningCrawlRun,
    MorningCrawlRunMethod,
)
from app.schemas import (
    MorningCrawlConfigResponse,
    MorningCrawlConfigUpdateRequest,
    MorningCrawlDashboardResponse,
    MorningCrawlRunDetailResponse,
    MorningCrawlRunMethodDetail,
    MorningCrawlRunSummary,
)


class MorningCrawlRunNotFoundError(Exception):
    pass


class MorningCrawlRetryNotAvailableError(Exception):
    pass

logger = logging.getLogger(__name__)

BEIJING_TZ = timezone(timedelta(hours=8))
ACTIVE_METHOD_STATUS = "active"
APPROVED_REVIEW_STATUS = "approved"
_RECENT_RUN_LIMIT = 10
# 超过该秒数仍处于 running/stopping 且当前进程无活动 worker（无取消事件登记）的 run，
# 判定为进程中断导致的僵尸 run，dashboard/触发前会被回收，避免「进程无法关闭」。
_STALE_RUN_GRACE_SECONDS = 120
_RUNNING_STATUSES = ("running", "stopping")

# 进程内运行取消登记：run_id -> Event。worker 在方法循环中检查，停止请求置位。
_cancel_lock = threading.Lock()
_cancel_events: dict[int, threading.Event] = {}


def _register_run(run_id: int) -> threading.Event:
    event = threading.Event()
    with _cancel_lock:
        _cancel_events[run_id] = event
    return event


def _unregister_run(run_id: int) -> None:
    with _cancel_lock:
        _cancel_events.pop(run_id, None)


def _has_live_worker(run_id: int) -> bool:
    with _cancel_lock:
        return run_id in _cancel_events


def _is_cancel_requested(run_id: int) -> bool:
    with _cancel_lock:
        event = _cancel_events.get(run_id)
    if event and event.is_set():
        return True
    session = SessionLocal()
    try:
        run = session.get(MorningCrawlRun, run_id)
        return bool(run is not None and run.status in {"stopping", "cancelled"})
    finally:
        session.close()


def _cancel_event_for_run(run_id: int) -> threading.Event | None:
    """Return the live cooperative-cancellation handle for one scheduled run."""
    with _cancel_lock:
        return _cancel_events.get(run_id)


def beijing_now() -> datetime:
    """当前北京时间（naive 墙钟值，便于与 run_time 字符串直接比较/入库）。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def _compute_next_run(config: MorningCrawlConfig, reference: datetime) -> datetime | None:
    """按 run_time / frequency 计算下次定时抓取时间（北京时间）。"""
    try:
        hour_str, minute_str = (config.run_time or "07:00").split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        return None
    candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if config.frequency == "hourly":
        interval = max(1, int(config.interval_hours or 1))
        if candidate <= reference:
            elapsed_hours = int((reference - candidate).total_seconds() // 3600)
            candidate += timedelta(hours=((elapsed_hours // interval) + 1) * interval)
        return candidate
    if candidate <= reference:
        candidate = candidate + timedelta(days=1)
    if config.frequency == "weekly":
        anchor = config.created_at.weekday() if config.created_at else reference.weekday()
        while candidate.weekday() != anchor:
            candidate = candidate + timedelta(days=1)
    elif config.frequency == "weekdays":
        while candidate.weekday() >= 5:
            candidate = candidate + timedelta(days=1)
    return candidate


def _scheduled_time_for_day(config: MorningCrawlConfig, reference: datetime) -> datetime | None:
    """Return the configured run time on the reference Beijing calendar day."""
    try:
        hour_str, minute_str = (config.run_time or "07:00").split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        return None
    return reference.replace(hour=hour, minute=minute, second=0, microsecond=0)


def get_or_create_config(db: Session) -> MorningCrawlConfig:
    config = db.scalar(select(MorningCrawlConfig).order_by(MorningCrawlConfig.id).limit(1))
    if config is None:
        now = beijing_now()
        config = MorningCrawlConfig(created_at=now, updated_at=now)
        config.next_run_at = _compute_next_run(config, now)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def update_morning_crawl_config(
    db: Session, payload: MorningCrawlConfigUpdateRequest
) -> MorningCrawlConfig:
    config = get_or_create_config(db)
    if payload.enabled is not None:
        config.enabled = payload.enabled
    if payload.run_time is not None:
        config.run_time = payload.run_time
    if payload.frequency is not None:
        config.frequency = payload.frequency
    if payload.interval_hours is not None:
        config.interval_hours = payload.interval_hours
    if payload.lookback_window is not None:
        config.lookback_window = payload.lookback_window
    if payload.patrol_interval_hours is not None:
        config.patrol_interval_hours = payload.patrol_interval_hours
    config.updated_at = beijing_now()
    config.next_run_at = _compute_next_run(config, beijing_now())
    db.commit()
    db.refresh(config)
    return config


def _active_method_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(CrawlMethod)
            .where(
                CrawlMethod.status == ACTIVE_METHOD_STATUS,
                CrawlMethod.review_status == APPROVED_REVIEW_STATUS,
            )
        )
        or 0
    )


def _list_active_methods(db: Session) -> list[CrawlMethod]:
    # Periodic coordination seam for the migration state machine.  It only
    # marks already-qualified rollback windows eligible; shadow/cutover and
    # retirement always remain explicit admin actions.
    from app.discovery.migration import refresh_migration_eligibility

    refresh_migration_eligibility(db)
    from app.discovery.migration import assert_formal_method_current

    methods = list(
        db.scalars(
            select(CrawlMethod)
            .where(
                CrawlMethod.status == ACTIVE_METHOD_STATUS,
                CrawlMethod.review_status == APPROVED_REVIEW_STATUS,
            )
            .order_by(CrawlMethod.id)
        )
    )
    current: list[CrawlMethod] = []
    for method in methods:
        try:
            assert_formal_method_current(db, method)
        except RuntimeError:
            continue
        current.append(method)
    return current


def _schedule_window_start(config: MorningCrawlConfig, now: datetime) -> datetime:
    """Return the start of the current configured crawl period in Beijing time.

    Patrol recovers methods not reached in a period and failures whose retry
    interval elapsed.  The boundary must be derived from the configured cadence
    rather than the latest retry's finish time, otherwise each failed retry
    creates a new moving retry window.
    """
    try:
        hour_str, minute_str = (config.run_time or "07:00").split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        hour, minute = 7, 0

    if config.frequency == "hourly":
        anchor = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        interval = max(1, int(config.interval_hours or 1))
        elapsed_hours = int((now - anchor).total_seconds() // 3600)
        return anchor + timedelta(hours=(elapsed_hours // interval) * interval)

    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while candidate > now or not _is_schedule_day(config, candidate):
        candidate -= timedelta(days=1)
    return candidate


def list_retryable_methods_in_window(
    db: Session,
    *,
    config: MorningCrawlConfig,
    now: datetime | None = None,
) -> list[CrawlMethod]:
    """Return unattempted methods and failures whose retry interval elapsed.

    A patrol must recover both sources skipped by an interrupted run and sources
    whose latest formal attempt failed.  The latest failure is subject to the
    configured patrol interval, so repeated failures remain retryable without
    turning each failed run into an immediate retry loop.
    """
    current_time = now or beijing_now()
    window_start = _schedule_window_start(config, current_time)
    retry_after = current_time - timedelta(hours=max(1, int(config.patrol_interval_hours or 3)))
    latest_attempt_by_method: dict[int, MorningCrawlRunMethod] = {}
    completed_attempts = db.scalars(
        select(MorningCrawlRunMethod)
        .where(
            MorningCrawlRunMethod.finished_at.is_not(None),
            MorningCrawlRunMethod.finished_at >= window_start,
            MorningCrawlRunMethod.method_id.is_not(None),
        )
        .order_by(MorningCrawlRunMethod.method_id, MorningCrawlRunMethod.finished_at.desc())
    )
    for attempt in completed_attempts:
        if attempt.method_id is not None:
            latest_attempt_by_method.setdefault(attempt.method_id, attempt)

    retryable: list[CrawlMethod] = []
    for method in _list_active_methods(db):
        latest_attempt = latest_attempt_by_method.get(method.id)
        if latest_attempt is None:
            retryable.append(method)
            continue
        if (
            latest_attempt.status in {"failed", "cancelled"}
            and latest_attempt.finished_at is not None
            and latest_attempt.finished_at <= retry_after
        ):
            retryable.append(method)
    return retryable


def _list_patrol_retry_methods(db: Session, run: MorningCrawlRun) -> list[CrawlMethod]:
    """Return active methods due for retry in the current configured period."""
    return list_retryable_methods_in_window(
        db,
        config=get_or_create_config(db),
        now=run.started_at or beijing_now(),
    )


def _reclaim_stale_runs(db: Session) -> None:
    """回收进程退出后残留的 running/stopping run（本进程无活动 worker 且超过宽限期）。"""
    from app.discovery.fetch_runs import has_live_scheduled_fetch_owner

    running = list(
        db.scalars(select(MorningCrawlRun).where(MorningCrawlRun.status.in_(_RUNNING_STATUSES)))
    )
    if not running:
        return
    now = beijing_now()
    changed = False
    for run in running:
        if _has_live_worker(run.id) or has_live_scheduled_fetch_owner(run.id, db):
            continue
        started = run.started_at or now
        if (now - started).total_seconds() < _STALE_RUN_GRACE_SECONDS:
            continue
        original_status = run.status
        if original_status == "stopping":
            _finalize_orphan_methods(
                db,
                run.id,
                now,
                "停止请求生效，方式执行已取消（stale）",
                status="cancelled",
            )
        else:
            _finalize_orphan_methods(db, run.id, now, "进程已退出，方式执行被中断（stale）")
        rows = list(
            db.scalars(
                select(MorningCrawlRunMethod).where(MorningCrawlRunMethod.run_id == run.id)
            )
        )
        _roll_up_run_method_stats(db, run)
        if original_status == "stopping":
            # A persisted stop request wins even between methods, including
            # when all rows written before the stop happened to be successful.
            run.status = "cancelled"
            run.error_message = "停止请求生效，进程退出后已完成取消（stale）"
        elif not rows:
            run.status = "failed"
            run.error_message = "进程已退出且没有方式执行记录，运行失败（stale）"
        else:
            if run.status in _RUNNING_STATUSES:
                run.status = "failed"
            run.error_message = "进程已退出，运行被判定为中断（stale）"
        run.finished_at = now
        assert run.status not in _RUNNING_STATUSES
        changed = True
    if changed:
        db.commit()


def _finalize_orphan_methods(
    db: Session,
    run_id: int,
    now: datetime,
    message: str,
    *,
    status: str = "failed",
) -> None:
    """把某个 run 下仍处于 running 的方式明细收尾为 failed，避免明细永远卡在执行中。"""
    orphans = db.scalars(
        select(MorningCrawlRunMethod).where(
            MorningCrawlRunMethod.run_id == run_id,
            MorningCrawlRunMethod.status == "running",
        )
    )
    for rm in orphans:
        rm.status = status
        rm.finished_at = now
        if not rm.error_message:
            rm.error_message = message


def _roll_up_run_method_stats(db: Session, run: MorningCrawlRun) -> None:
    """按方式明细回填 run 级 success/failed/stored，避免中断后聚合字段仍是 0。"""
    rows = list(
        db.scalars(select(MorningCrawlRunMethod).where(MorningCrawlRunMethod.run_id == run.id))
    )
    success = failed = stored = cancelled = 0
    for row in rows:
        stored += int(row.stored_count or 0)
        if row.status == "failed":
            failed += 1
        elif row.status == "cancelled":
            cancelled += 1
        elif row.status in ("ok", "empty", "partial"):
            success += 1
    run.success_methods = success
    run.failed_methods = failed
    run.stored_count = stored
    if cancelled > 0:
        run.status = "cancelled"
    elif failed > 0 and success == 0:
        run.status = "failed"
    elif failed > 0 or any(row.status == "partial" for row in rows):
        run.status = "partial"
    elif success > 0:
        run.status = "success"


def is_running(db: Session) -> bool:
    _reclaim_stale_runs(db)
    return (
        db.scalar(
            select(func.count()).select_from(MorningCrawlRun).where(MorningCrawlRun.status.in_(_RUNNING_STATUSES))
        )
        or 0
    ) > 0


def config_to_response(config: MorningCrawlConfig) -> MorningCrawlConfigResponse:
    return MorningCrawlConfigResponse(
        enabled=config.enabled,
        run_time=config.run_time,
        frequency=config.frequency if config.frequency in ("hourly", "daily", "weekdays", "weekly") else "daily",
        interval_hours=max(1, int(config.interval_hours or 1)),
        lookback_window=config.lookback_window if config.lookback_window in ("24h", "7d", "30d", "all") else "24h",
        patrol_interval_hours=config.patrol_interval_hours,
        last_run_at=config.last_run_at,
        last_run_status=config.last_run_status,
        last_success_date=config.last_success_date,
        next_run_at=config.next_run_at,
    )


def run_to_summary(run: MorningCrawlRun) -> MorningCrawlRunSummary:
    return MorningCrawlRunSummary(
        id=run.id,
        trigger_type=run.trigger_type,
        status=run.status,
        run_date=run.run_date,
        total_methods=run.total_methods,
        success_methods=run.success_methods,
        failed_methods=run.failed_methods,
        stored_count=run.stored_count,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error_message=run.error_message,
    )


def method_to_detail(rm: MorningCrawlRunMethod) -> MorningCrawlRunMethodDetail:
    return MorningCrawlRunMethodDetail(
        id=rm.id,
        method_id=rm.method_id,
        domain=rm.domain,
        status=rm.status,
        discovered_count=rm.discovered_count,
        stored_count=rm.stored_count,
        error_message=rm.error_message,
        started_at=rm.started_at,
        finished_at=rm.finished_at,
    )


def list_runs(db: Session, *, limit: int = 20) -> list[MorningCrawlRunSummary]:
    _reclaim_stale_runs(db)
    runs = db.scalars(
        select(MorningCrawlRun).order_by(MorningCrawlRun.id.desc()).limit(limit)
    )
    return [run_to_summary(r) for r in runs]


def resolve_default_run_id(db: Session) -> int | None:
    """默认查看的 run：优先今日 run，其次最近一次 run。"""
    today = beijing_now().date().isoformat()
    today_run = db.scalar(
        select(MorningCrawlRun).where(MorningCrawlRun.run_date == today).order_by(MorningCrawlRun.id.desc()).limit(1)
    )
    if today_run is not None:
        return today_run.id
    latest = db.scalar(select(MorningCrawlRun).order_by(MorningCrawlRun.id.desc()).limit(1))
    return latest.id if latest else None


def get_run_detail(db: Session, run_id: int) -> MorningCrawlRunDetailResponse:
    run = db.get(MorningCrawlRun, run_id)
    if run is None:
        raise MorningCrawlRunNotFoundError(f"morning crawl run {run_id} not found")
    methods = db.scalars(
        select(MorningCrawlRunMethod)
        .where(MorningCrawlRunMethod.run_id == run_id)
        .order_by(MorningCrawlRunMethod.id.asc())
    )
    return MorningCrawlRunDetailResponse(
        run=run_to_summary(run),
        methods=[method_to_detail(rm) for rm in methods],
    )


def get_morning_crawl_dashboard(db: Session) -> MorningCrawlDashboardResponse:
    _reclaim_stale_runs(db)
    config = get_or_create_config(db)
    today = beijing_now().date().isoformat()
    recent = list(
        db.scalars(
            select(MorningCrawlRun).order_by(MorningCrawlRun.id.desc()).limit(_RECENT_RUN_LIMIT)
        )
    )
    today_run = next((r for r in recent if r.run_date == today), None)
    running_run = next((r for r in recent if r.status in _RUNNING_STATUSES), None)
    running = running_run is not None
    if running_run is not None:
        today_status = running_run.status
    elif today_run is not None:
        today_status = today_run.status
    else:
        today_status = "not_run"
    retryable_method_count = (
        len(list_retryable_methods_in_window(db, config=config))
        if today_run is not None and today_run.status in ("partial", "failed", "cancelled") and running_run is None
        else 0
    )
    return MorningCrawlDashboardResponse(
        config=config_to_response(config),
        active_method_count=_active_method_count(db),
        retryable_method_count=retryable_method_count,
        today_status=today_status,
        today_run=run_to_summary(today_run) if today_run else None,
        recent_runs=[run_to_summary(r) for r in recent],
        is_running=running,
    )


def _build_request(config: MorningCrawlConfig):
    """按 lookback_window 构造 ManualNewsRunRequest；all 表示不加时间窗口过滤。"""
    from app.schemas import ManualNewsRunRequest

    if config.lookback_window == "all":
        return None
    return ManualNewsRunRequest(
        time_mode="relative",
        relative_range=config.lookback_window,
        target_count=200,
    )


def _persist_run_method_progress(
    db: Session,
    rm: MorningCrawlRunMethod | None,
    *,
    discovered_count: int | None = None,
    stored_count: int | None = None,
) -> None:
    """把统计用的 discovered/stored 尽早落库，避免进程中断后分母仍是 0。"""
    if rm is None:
        return
    row = db.get(MorningCrawlRunMethod, rm.id)
    if row is None:
        return
    if discovered_count is not None:
        row.discovered_count = discovered_count
        rm.discovered_count = discovered_count
    if stored_count is not None:
        row.stored_count = stored_count
        rm.stored_count = stored_count
    db.commit()


def _fetch_and_ingest_method(
    db: Session,
    method: CrawlMethod,
    request,
    *,
    run_method: MorningCrawlRunMethod | None = None,
) -> dict:
    """Execute one method through the same formal runner used by manual fetches."""
    from app.discovery.fetch_runs import (
        process_start_token,
        start_method_fetch_run,
        watch_method_fetch_owner,
    )
    from app.discovery.runner import execute_discovery_fetch
    from app.discovery.redaction import redact_discovery_text
    from app.discovery.sandbox import SandboxJobPriority
    from app.discovery.sandbox.runtime import get_formal_sandbox_runtime
    request_payload = request.model_dump(mode="json") if request is not None else None
    owner_pid = os.getpid()
    owner_start_token = process_start_token(owner_pid)
    owner_id = f"scheduled-{owner_pid}-{owner_start_token}-{uuid.uuid4().hex}"
    sandbox_job_id = (
        f"formal-scheduled-{run_method.run_id if run_method else 'direct'}-{method.id}-{owner_id}"
    )
    formal_run_id = start_method_fetch_run(
        method.id,
        request_payload,
        db,
        owner_id=owner_id,
        owner_pid=owner_pid,
        owner_start_token=owner_start_token,
        sandbox_job_id=sandbox_job_id,
    )
    runtime = get_formal_sandbox_runtime()
    cancel_event = (
        _cancel_event_for_run(run_method.run_id)
        if run_method is not None
        else threading.Event()
    )
    cancel_event = cancel_event or threading.Event()
    watcher_stop = threading.Event()

    watcher = threading.Thread(
        target=watch_method_fetch_owner,
        kwargs={
            "run_id": formal_run_id,
            "owner_id": owner_id,
            "sandbox_job_id": sandbox_job_id,
            "runtime": runtime,
            "cancel_event": cancel_event,
            "stop_event": watcher_stop,
        },
        name=f"scheduled-formal-owner-{formal_run_id}",
        daemon=True,
    )
    watcher.start()

    def _persist(discovered_count: int, stored_count: int) -> None:
        _persist_run_method_progress(
            db,
            run_method,
            discovered_count=discovered_count,
            stored_count=stored_count,
        )

    try:
        result = execute_discovery_fetch(
            method.id,
            formal_run_id,
            request_payload,
            db=db,
            sandbox_runtime=runtime,
            sandbox_job_id=sandbox_job_id,
            cancel_event=cancel_event,
            owner_id=owner_id,
            sandbox_priority=SandboxJobPriority.SCHEDULED,
            log_stage="定时抓取",
            manage_usage_scope=False,
            result_progress_callback=_persist,
        )
    finally:
        watcher_stop.set()
        watcher.join(timeout=1.0)
    status = method.last_run_status or "empty"
    return {
        **result,
        "status": status,
        "error_message": (
            redact_discovery_text(str(result.get("stats", {}).get("error")))[:4000]
            if status == "partial" and result.get("stats", {}).get("error")
            else None
        ),
    }


def _run_methods(db: Session, run: MorningCrawlRun, *, trigger_type: str) -> MorningCrawlRun:
    """逐条执行 active methods：单条失败不阻断整次 run；只有整次无失败才写当天成功标记。

    可通过停止请求（取消事件）在方法之间中断，最终 run.status = cancelled。
    """
    try:
        return _run_methods_body(db, run, trigger_type=trigger_type)
    finally:
        _unregister_run(run.id)


def _run_methods_body(db: Session, run: MorningCrawlRun, *, trigger_type: str) -> MorningCrawlRun:
    from app.discovery.redaction import redact_discovery_text
    from app.llm.usage import UsageScope, usage_scope
    from app.run_logs import append_run_log

    config = get_or_create_config(db)
    methods = (
        _list_patrol_retry_methods(db, run)
        if trigger_type == "patrol_resend"
        else _list_active_methods(db)
    )
    run.total_methods = len(methods)
    db.commit()

    append_run_log(
        "定时抓取",
        "系统巡检补跑开始（仅失败或未执行方式）" if trigger_type == "patrol_resend" else "系统定时抓取开始",
        trigger_type=trigger_type,
        total_methods=len(methods),
    )

    success = failed = partial = stored_total = 0
    cancelled = False
    for method in methods:
        if _is_cancel_requested(run.id):
            cancelled = True
            append_run_log("定时抓取", "收到停止请求，终止后续爬取", trigger_type=trigger_type, level="warn")
            break
        method_id = method.id
        domain = method.domain
        rm = MorningCrawlRunMethod(
            run_id=run.id,
            method_id=method_id,
            domain=domain,
            status="running",
            started_at=beijing_now(),
        )
        db.add(rm)
        db.commit()
        rm_id = rm.id
        append_run_log("定时抓取", "开始执行爬取方式", source=domain, method_id=method_id)
        try:
            request = _build_request(config)
            with usage_scope(
                UsageScope(
                    context_type="query",
                    trigger_type=trigger_type,
                    stage="query_enrichment",
                    morning_crawl_run_id=run.id,
                    morning_crawl_run_method_id=rm_id,
                    method_id=method_id,
                )
            ):
                result = _fetch_and_ingest_method(db, method, request, run_method=rm)
            rm.status = result["status"]
            rm.discovered_count = result["discovered_count"]
            rm.stored_count = result["stored_count"]
            rm.error_message = result.get("error_message")
            rm.finished_at = beijing_now()
            db.commit()
            if result["status"] == "partial":
                partial += 1
            else:
                success += 1
            stored_total += result["stored_count"]
            append_run_log(
                "定时抓取",
                "爬取方式执行完成",
                source=domain,
                method_id=method_id,
                discovered_count=result["discovered_count"],
                stored_count=result["stored_count"],
                status=result["status"],
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断整体
            db.rollback()
            safe_error = redact_discovery_text(str(exc))[:4000]
            method_cancelled = bool(getattr(exc, "_formal_cancelled", False)) or _is_cancel_requested(
                run.id
            )
            if method_cancelled:
                cancelled = True
            else:
                failed += 1
            rm = db.get(MorningCrawlRunMethod, rm_id)
            if rm is not None:
                # Progress is persisted after every stored item. The exception
                # branch is mutually exclusive with the normal result branch,
                # so fold this durable partial count into the run exactly once.
                stored_total += max(0, int(rm.stored_count or 0))
                rm.status = "cancelled" if method_cancelled else "failed"
                rm.error_message = safe_error
                rm.finished_at = beijing_now()
                db.commit()
            append_run_log(
                "定时抓取",
                (
                    f"爬取方式执行已取消 · {safe_error}"
                    if method_cancelled
                    else f"爬取方式执行失败 · {safe_error}"
                ),
                source=domain,
                method_id=method_id,
                level="warning" if method_cancelled else "error",
                error_type=type(exc).__name__,
            )

    db.expire_all()
    run = db.get(MorningCrawlRun, run.id)
    cancelled = cancelled or run.status in {"stopping", "cancelled"}
    if cancelled:
        run.status = "cancelled"
        run.error_message = "已手动停止"
    elif run.total_methods == 0:
        run.status = "success"
    elif failed == 0 and partial == 0:
        run.status = "success"
    elif failed > 0 and success == 0 and partial == 0:
        run.status = "failed"
    else:
        run.status = "partial"
    run.success_methods = success
    run.failed_methods = failed
    run.stored_count = stored_total
    run.finished_at = beijing_now()

    config = get_or_create_config(db)
    config.last_run_at = beijing_now()
    config.last_run_status = run.status
    if run.status == "success":
        config.last_success_date = run.run_date
    config.next_run_at = _compute_next_run(config, beijing_now())
    db.commit()
    db.refresh(run)

    append_run_log(
        "定时抓取",
        "系统定时抓取已停止" if cancelled else "系统定时抓取结束",
        trigger_type=trigger_type,
        status=run.status,
        success_methods=success,
        failed_methods=failed,
        partial_methods=partial,
        stored_count=stored_total,
    )
    return run


def _create_run(db: Session, trigger_type: str) -> MorningCrawlRun:
    now = beijing_now()
    run = MorningCrawlRun(
        trigger_type=trigger_type,
        status="running",
        run_date=now.date().isoformat(),
        started_at=now,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    _register_run(run.id)
    return run


def execute_morning_crawl(db: Session, *, trigger_type: str) -> MorningCrawlRun:
    """同步执行一次定时抓取（调度线程使用）。"""
    run = _create_run(db, trigger_type)
    return _run_methods(db, run, trigger_type=trigger_type)


def _run_worker(run_id: int, trigger_type: str) -> None:
    session = SessionLocal()
    try:
        run = session.get(MorningCrawlRun, run_id)
        if run is None:
            return
        _run_methods(session, run, trigger_type=trigger_type)
    except Exception:  # noqa: BLE001
        logger.exception("morning crawl worker failed (run_id=%s)", run_id)
    finally:
        session.close()


def trigger_morning_crawl_async(db: Session, *, trigger_type: str) -> MorningCrawlRun:
    """HTTP 入口：若已有 running 的 run 则复用；否则同步建 run 后后台线程执行方法循环。"""
    _reclaim_stale_runs(db)
    running = db.scalar(
        select(MorningCrawlRun)
        .where(MorningCrawlRun.status.in_(_RUNNING_STATUSES))
        .order_by(MorningCrawlRun.id.desc())
        .limit(1)
    )
    if running is not None:
        return running
    run = _create_run(db, trigger_type)
    thread = threading.Thread(
        target=_run_worker, args=(run.id, trigger_type), name=f"morning-crawl-{run.id}", daemon=True
    )
    thread.start()
    return run


def retry_today_failed_methods_async(db: Session) -> MorningCrawlRun:
    """Start one interval-bounded pass for unattempted and failed methods."""
    _reclaim_stale_runs(db)
    if is_running(db):
        raise MorningCrawlRetryNotAvailableError("当前已有定时抓取在执行，请先等待其结束")
    today = beijing_now().date().isoformat()
    previous_run = db.scalar(
        select(MorningCrawlRun)
        .where(MorningCrawlRun.run_date == today)
        .order_by(MorningCrawlRun.id.desc())
        .limit(1)
    )
    if previous_run is None:
        raise MorningCrawlRetryNotAvailableError("今日尚无定时抓取记录，无法重试")
    if previous_run.status not in ("partial", "failed", "cancelled"):
        raise MorningCrawlRetryNotAvailableError("今日抓取已成功完成，没有需要重试的方式")
    config = get_or_create_config(db)
    if not list_retryable_methods_in_window(db, config=config):
        raise MorningCrawlRetryNotAvailableError("当前巡检间隔内没有待查取或到期重试的活跃信息源")
    return trigger_morning_crawl_async(db, trigger_type="patrol_resend")


def stop_morning_crawl(db: Session) -> dict:
    """停止所有正在进行的定时抓取。

    - 有活动 worker 的 run：置位取消事件，worker 会在下一条方式前中断并落库为 cancelled；
      同时把 run.status 标为 stopping 以便前端即时反馈。
    - 无活动 worker 的僵尸 run（进程已退出）：直接强制标记 cancelled，避免卡死无法关闭。
    """
    from app.discovery.fetch_runs import (
        recover_dead_method_fetch_runs,
        request_method_fetch_cancel,
    )
    from app.models import CrawlMethodRun
    from app.run_logs import append_run_log

    running = list(
        db.scalars(select(MorningCrawlRun).where(MorningCrawlRun.status.in_(_RUNNING_STATUSES)))
    )
    stopping: list[int] = []
    cancelled: list[int] = []
    now = beijing_now()
    with _cancel_lock:
        for run in running:
            event = _cancel_events.get(run.id)
            if event is not None:
                event.set()
            run.status = "stopping"
            stopping.append(run.id)
    db.commit()

    active_by_morning_run: dict[int, list[int]] = {}
    for run in running:
        active = list(
            db.scalars(
                select(CrawlMethodRun).where(
                    CrawlMethodRun.status == "running",
                    CrawlMethodRun.sandbox_job_id.like(f"formal-scheduled-{run.id}-%"),
                )
            )
        )
        active_by_morning_run[run.id] = [item.id for item in active]
        for item in active:
            request_method_fetch_cancel(item.id, db)
            recover_dead_method_fetch_runs(db, run_id=item.id)

    deadline = time.monotonic() + 2.0
    pending_ids = {item for values in active_by_morning_run.values() for item in values}
    while pending_ids and time.monotonic() < deadline:
        db.expire_all()
        acknowledged = set(
            db.scalars(
                select(CrawlMethodRun.id).where(
                    CrawlMethodRun.id.in_(pending_ids),
                    CrawlMethodRun.status == "cancelled",
                    CrawlMethodRun.cancel_acknowledged_at.is_not(None),
                )
            )
        )
        pending_ids -= {int(value) for value in acknowledged}
        if pending_ids:
            time.sleep(0.05)

    for run in running:
        owned_ids = set(active_by_morning_run.get(run.id, ()))
        if owned_ids and owned_ids.isdisjoint(pending_ids):
            row = db.get(MorningCrawlRun, run.id)
            if row is not None and row.status == "stopping":
                row.status = "cancelled"
                row.finished_at = now
                row.error_message = "手动停止（正式抓取 owner 已确认取消）"
                _finalize_orphan_methods(
                    db,
                    run.id,
                    now,
                    "手动停止（owner 已确认取消）",
                    status="cancelled",
                )
                cancelled.append(run.id)
                if run.id in stopping:
                    stopping.remove(run.id)
    db.commit()
    if stopping or cancelled:
        append_run_log(
            "定时抓取",
            "收到停止全部爬取请求",
            stopping=stopping,
            acknowledged_cancelled=cancelled,
            cancel_pending=sorted(pending_ids),
            level="warn",
        )
    return {"stopping": stopping, "cancelled": cancelled}


# --- 调度判定（供 scheduler tick / patrol 使用；均按北京时间） ---

def _is_schedule_day(config: MorningCrawlConfig, now: datetime) -> bool:
    if config.frequency == "weekly":
        anchor = config.created_at.weekday() if config.created_at else now.weekday()
        return now.weekday() == anchor
    return config.frequency != "weekdays" or now.weekday() < 5


def _hourly_slot(config: MorningCrawlConfig, now: datetime) -> datetime | None:
    try:
        hour_str, minute_str = (config.run_time or "07:00").split(":", 1)
        anchor_hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        return None
    if now.minute != minute:
        return None
    interval = max(1, int(config.interval_hours or 1))
    if (now.hour - anchor_hour) % interval != 0:
        return None
    return now.replace(second=0, microsecond=0)


def schedule_due_now(config: MorningCrawlConfig, *, now: datetime, today: str) -> bool:
    """Whether the normal scheduled run is due at this exact scheduler tick."""
    if not config.enabled:
        return False
    if config.frequency == "hourly":
        slot = _hourly_slot(config, now)
        if slot is None:
            return False
        return config.last_run_at is None or config.last_run_at < slot
    if config.last_success_date == today:
        return False
    scheduled_at = _scheduled_time_for_day(config, now)
    if scheduled_at is None:
        return False
    if not _is_schedule_day(config, now):
        return False
    if now < scheduled_at:
        return False

    # Normal ticks run each scheduled slot once. Failed slots are recovered by the
    # separate patrol task so a one-minute tick never retries the whole run.
    if config.last_run_at and config.last_run_at.date().isoformat() == today and config.last_run_at >= scheduled_at:
        return False
    return True


def patrol_due_now(config: MorningCrawlConfig, *, now: datetime) -> bool:
    """Whether a failed/partial run may be recovered by the patrol job."""
    if not config.enabled or config.last_run_status not in ("failed", "partial", "cancelled"):
        return False
    if config.last_run_at is None or not _is_schedule_day(config, now):
        return False
    if config.frequency != "hourly":
        scheduled_at = _scheduled_time_for_day(config, now)
        if scheduled_at is None or now < scheduled_at:
            return False
    interval = max(1, int(config.patrol_interval_hours or 3))
    return now >= config.last_run_at + timedelta(hours=interval)
