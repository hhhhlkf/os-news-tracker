"""系统晨抓服务：单例配置、今日状态聚合、执行全部 active discovery methods。

时间语义：配置与运行记录的所有墙钟时间统一使用北京时间（UTC+8），与邮件模块一致，
前端原样展示。晨抓只使用 discovery methods（CrawlMethod），不碰旧 sources 业务模型。
"""
from __future__ import annotations

import logging
import threading
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
    MorningCrawlRunSummary,
)

logger = logging.getLogger(__name__)

BEIJING_TZ = timezone(timedelta(hours=8))
ACTIVE_METHOD_STATUS = "active"
_RECENT_RUN_LIMIT = 10


def beijing_now() -> datetime:
    """当前北京时间（naive 墙钟值，便于与 run_time 字符串直接比较/入库）。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def _compute_next_run(config: MorningCrawlConfig, reference: datetime) -> datetime | None:
    """按 run_time / frequency 计算下次晨抓时间（北京时间）。"""
    try:
        hour_str, minute_str = (config.run_time or "07:00").split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        return None
    candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= reference:
        candidate = candidate + timedelta(days=1)
    if config.frequency == "weekly":
        anchor = config.created_at.weekday() if config.created_at else reference.weekday()
        while candidate.weekday() != anchor:
            candidate = candidate + timedelta(days=1)
    return candidate


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
            select(func.count()).select_from(CrawlMethod).where(CrawlMethod.status == ACTIVE_METHOD_STATUS)
        )
        or 0
    )


def _list_active_methods(db: Session) -> list[CrawlMethod]:
    return list(
        db.scalars(
            select(CrawlMethod)
            .where(CrawlMethod.status == ACTIVE_METHOD_STATUS)
            .order_by(CrawlMethod.id)
        )
    )


def is_running(db: Session) -> bool:
    return (
        db.scalar(
            select(func.count()).select_from(MorningCrawlRun).where(MorningCrawlRun.status == "running")
        )
        or 0
    ) > 0


def config_to_response(config: MorningCrawlConfig) -> MorningCrawlConfigResponse:
    return MorningCrawlConfigResponse(
        enabled=config.enabled,
        run_time=config.run_time,
        frequency=config.frequency if config.frequency in ("daily", "weekly") else "daily",
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


def get_morning_crawl_dashboard(db: Session) -> MorningCrawlDashboardResponse:
    config = get_or_create_config(db)
    today = beijing_now().date().isoformat()
    recent = list(
        db.scalars(
            select(MorningCrawlRun).order_by(MorningCrawlRun.id.desc()).limit(_RECENT_RUN_LIMIT)
        )
    )
    today_run = next((r for r in recent if r.run_date == today), None)
    running = any(r.status == "running" for r in recent)
    if running:
        today_status = "running"
    elif today_run is not None:
        today_status = today_run.status
    else:
        today_status = "not_run"
    return MorningCrawlDashboardResponse(
        config=config_to_response(config),
        active_method_count=_active_method_count(db),
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
        target_count=500,
    )


def _fetch_and_ingest_method(db: Session, method: CrawlMethod, request) -> dict:
    """运行单条 discovery method 的 DSL 并走正常 pipeline 入库。复用 discovery 内部入口，不反调 HTTP。"""
    from app.api.discovery_routes import _apply_fetch_limits, _prepare_fetch_recipe, run_method
    from app.discovery.ingester import CrawlOutputIngester
    from app.models import Source
    from app.pipeline import Pipeline
    from app.processing.enricher import Enricher

    recipe = _prepare_fetch_recipe(method.dsl_recipe, request)
    output = run_method(recipe)
    raw_items = list(output.get("items", []))
    output["items"] = _apply_fetch_limits(raw_items, request)

    raws = CrawlOutputIngester().to_raw_items(output, source_id=method.source_id)
    source = db.get(Source, method.source_id)
    pipeline = Pipeline(session=db, extractor=None, enricher=Enricher())
    stored = 0
    for raw in raws:
        if pipeline.process_item_result(source, raw).stored:
            stored += 1

    method.last_run_at = datetime.now(timezone.utc)
    method.last_run_status = "ok" if stored > 0 else "empty"
    return {
        "discovered_count": len(raws),
        "stored_count": stored,
        "status": "ok" if stored > 0 else "empty",
    }


def _run_methods(db: Session, run: MorningCrawlRun, *, trigger_type: str) -> MorningCrawlRun:
    """逐条执行 active methods：单条失败不阻断整次 run；只有整次无失败才写当天成功标记。"""
    from app.run_logs import append_run_log

    config = get_or_create_config(db)
    methods = _list_active_methods(db)
    run.total_methods = len(methods)
    db.commit()

    append_run_log("晨抓", "系统晨抓开始", trigger_type=trigger_type, total_methods=len(methods))

    success = failed = stored_total = 0
    for method in methods:
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
        append_run_log("晨抓", "开始执行爬取方式", source=domain, method_id=method_id)
        try:
            request = _build_request(config)
            result = _fetch_and_ingest_method(db, method, request)
            rm.status = result["status"]
            rm.discovered_count = result["discovered_count"]
            rm.stored_count = result["stored_count"]
            rm.finished_at = beijing_now()
            db.commit()
            success += 1
            stored_total += result["stored_count"]
            append_run_log(
                "晨抓",
                "爬取方式执行完成",
                source=domain,
                method_id=method_id,
                discovered_count=result["discovered_count"],
                stored_count=result["stored_count"],
                status=result["status"],
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断整体
            db.rollback()
            failed += 1
            rm = db.get(MorningCrawlRunMethod, rm_id)
            if rm is not None:
                rm.status = "failed"
                rm.error_message = str(exc)
                rm.finished_at = beijing_now()
                db.commit()
            append_run_log(
                "晨抓",
                f"爬取方式执行失败 · {exc}",
                source=domain,
                method_id=method_id,
                level="error",
                error_type=type(exc).__name__,
            )

    run = db.get(MorningCrawlRun, run.id)
    if run.total_methods == 0:
        run.status = "success"
    elif failed == 0:
        run.status = "success"
    elif success == 0:
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
        "晨抓",
        "系统晨抓结束",
        trigger_type=trigger_type,
        status=run.status,
        success_methods=success,
        failed_methods=failed,
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
    return run


def execute_morning_crawl(db: Session, *, trigger_type: str) -> MorningCrawlRun:
    """同步执行一次晨抓（调度线程使用）。"""
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
    running = db.scalar(
        select(MorningCrawlRun).where(MorningCrawlRun.status == "running").order_by(MorningCrawlRun.id.desc()).limit(1)
    )
    if running is not None:
        return running
    run = _create_run(db, trigger_type)
    thread = threading.Thread(
        target=_run_worker, args=(run.id, trigger_type), name=f"morning-crawl-{run.id}", daemon=True
    )
    thread.start()
    return run


# --- 调度判定（供 scheduler tick / patrol 使用；均按北京时间） ---

def schedule_due_now(config: MorningCrawlConfig, *, now: datetime, today: str) -> bool:
    if not config.enabled:
        return False
    if config.last_success_date == today:
        return False
    try:
        hour_str, minute_str = (config.run_time or "07:00").split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        return False
    if config.frequency == "weekly":
        anchor = config.created_at.weekday() if config.created_at else now.weekday()
        if now.weekday() != anchor:
            return False
    return (now.hour, now.minute) >= (hour, minute)
