"""Process-restart recovery and the phase-4 handoff seam for resumed Discovery runs."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from threading import Lock

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.discovery.checkpoints import (
    CheckpointStore,
    DiscoveryCheckpoint,
    cleanup_orphaned_checkpoints,
)
from app.discovery.events import append_discovery_event
from app.discovery.redaction import redact_discovery_data, redact_discovery_text
from app.discovery.sandbox.runtime import SandboxRuntime, set_sandbox_cleanup_degraded
from app.models import SiteDiscoveryRun


logger = logging.getLogger(__name__)
UNFINISHED_DISCOVERY_STATUSES = ("queued", "running", "repairing")
STALE_RUN_TIMEOUT_SECONDS = 1800
STALE_RUN_PATROL_INTERVAL_MINUTES = 5
ResumeDispatcher = Callable[[int, DiscoveryCheckpoint], None]
_dispatcher_lock = Lock()
_resume_dispatcher: ResumeDispatcher | None = None


def register_resume_dispatcher(dispatcher: ResumeDispatcher) -> None:
    """Register the Single Agent Loop scheduler without importing legacy Graph code."""
    global _resume_dispatcher
    with _dispatcher_lock:
        _resume_dispatcher = dispatcher
    _dispatch_queued_resumes(dispatcher)


def unregister_resume_dispatcher(dispatcher: ResumeDispatcher) -> None:
    """Remove a dispatcher only when it is the currently registered instance."""
    global _resume_dispatcher
    with _dispatcher_lock:
        if _resume_dispatcher is dispatcher:
            _resume_dispatcher = None


def _dispatch_queued_resumes(dispatcher: ResumeDispatcher) -> None:
    """Hand persisted in-process resume requests to the newly registered Loop scheduler."""
    db = SessionLocal()
    try:
        queued_ids = list(
            db.scalars(
                select(SiteDiscoveryRun.id).where(
                    SiteDiscoveryRun.trigger_type == "resume",
                    SiteDiscoveryRun.status == "queued",
                    SiteDiscoveryRun.checkpoint_path.is_not(None),
                )
            )
        )
    finally:
        db.close()
    for run_id in queued_ids:
        _claim_and_dispatch_resume(run_id, dispatcher)


def dispatch_resumed_run_if_registered(run_id: int) -> bool:
    """Claim and dispatch only after the caller has committed the queued run."""
    with _dispatcher_lock:
        dispatcher = _resume_dispatcher
    return dispatcher is not None and _claim_and_dispatch_resume(run_id, dispatcher)


def _claim_and_dispatch_resume(run_id: int, dispatcher: ResumeDispatcher) -> bool:
    """Atomically claim queued→running so concurrent scanners dispatch exactly once."""
    db = SessionLocal()
    claim_owned = False
    try:
        claimed = db.execute(
            update(SiteDiscoveryRun)
            .where(
                SiteDiscoveryRun.id == run_id,
                SiteDiscoveryRun.trigger_type == "resume",
                SiteDiscoveryRun.status == "queued",
            )
            .values(status="running", error_message=None, ended_at=None)
        )
        db.commit()
        if claimed.rowcount != 1:
            return False
        claim_owned = True
        run = db.get(SiteDiscoveryRun, run_id)
        if run is None or not run.checkpoint_path:
            raise ValueError("claimed resume run has no checkpoint")
        checkpoint = CheckpointStore().load(run.checkpoint_path)
        dispatcher(run_id, checkpoint)
        try:
            append_discovery_event(
                run_id,
                event_type="resume_dispatched",
                summary="恢复运行已由 Single Agent Loop 调度器领取。",
                phase=run.phase,
                round_number=run.round,
                session=db,
            )
            db.commit()
        except Exception:  # noqa: BLE001 - accepted work must never be made dispatchable again
            db.rollback()
            logger.error("failed to persist resume dispatch event run_id=%s", run_id)
        return True
    except Exception as exc:  # noqa: BLE001 - persist a resumable terminal state
        db.rollback()
        safe_error = redact_discovery_text(str(exc))[:4000]
        if claim_owned:
            db.execute(
                update(SiteDiscoveryRun)
                .where(SiteDiscoveryRun.id == run_id, SiteDiscoveryRun.status == "running")
                .values(
                    status="interrupted",
                    ended_at=datetime.now(timezone.utc),
                    error_message=f"恢复任务调度失败：{safe_error}",
                )
            )
            db.commit()
        logger.error(
            "failed to dispatch queued Discovery resume run_id=%s error=%s",
            run_id,
            safe_error,
        )
        return False
    finally:
        db.close()


def recover_discovery_state() -> int:
    """Clean labeled sandbox resources and mark process-owned runs interrupted."""
    try:
        from app.discovery.fetch_runs import get_live_method_fetch_sandbox_job_ids

        cleanup_failures = SandboxRuntime().cleanup_stale_resources(
            protected_job_ids=get_live_method_fetch_sandbox_job_ids()
        )
        if cleanup_failures:
            set_sandbox_cleanup_degraded(True)
            logger.error(
                "Discovery sandbox startup cleanup was incomplete: %s",
                redact_discovery_data(cleanup_failures),
            )
        else:
            set_sandbox_cleanup_degraded(False)
    except Exception as exc:  # noqa: BLE001 - unavailable Docker must not become a fallback runtime
        set_sandbox_cleanup_degraded(True)
        logger.error(
            "Discovery sandbox startup cleanup could not inspect Docker resources: %s",
            redact_discovery_text(str(exc)),
        )

    db = SessionLocal()
    try:
        try:
            cleanup_orphaned_checkpoints(db)
        except Exception as exc:  # noqa: BLE001 - DB recovery must still converge run status
            logger.error(
                "Discovery checkpoint orphan cleanup failed: %s",
                redact_discovery_text(str(exc)),
            )
        runs = list(
            db.scalars(
                select(SiteDiscoveryRun)
                .where(SiteDiscoveryRun.status.in_(UNFINISHED_DISCOVERY_STATUSES))
                .with_for_update()
            )
        )
        interrupted_at = datetime.now(timezone.utc)
        for run in runs:
            previous_status = run.status
            run.status = "interrupted"
            run.ended_at = interrupted_at
            run.error_message = "服务进程已重启，任务已中断；可从最近检查点恢复。"
            append_discovery_event(
                run.id,
                event_type="run_interrupted",
                summary="服务重启后运行被标记为 interrupted，旧容器不会被重新连接。",
                phase=run.phase,
                round_number=run.round,
                level="warning",
                payload={"previous_status": previous_status, "checkpoint_available": bool(run.checkpoint_path)},
                session=db,
            )
        db.commit()
        return len(runs)
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def reclaim_stale_runs(
    older_than_seconds: int | None = None,
    db: Session | None = None,
) -> int:
    """Fail stale in-process runs without importing the retired website Graph."""
    own_session = db is None
    if own_session:
        db = SessionLocal()
    assert db is not None
    try:
        query = select(SiteDiscoveryRun).where(
            SiteDiscoveryRun.status.in_(("running", "repairing"))
        )
        if older_than_seconds is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
            query = query.where(SiteDiscoveryRun.started_at < cutoff)
        stale = list(db.scalars(query.with_for_update()))
        ended_at = datetime.now(timezone.utc)
        message = (
            "进程重启时回收：run 未正常结束（遗留 running）"
            if older_than_seconds is None
            else f"定时巡检回收：run 运行超过 {older_than_seconds}s 未完成，判超时"
        )
        for run in stale:
            run.status = "failed"
            run.ended_at = ended_at
            if not run.error_message:
                run.error_message = message
        db.commit()
        return len(stale)
    except BaseException:
        db.rollback()
        raise
    finally:
        if own_session:
            db.close()


def create_resumed_run(original_run_id: int, *, session: Session) -> SiteDiscoveryRun:
    """Flush a queued resume and its event; the route owns commit and later dispatch."""
    original = session.scalar(
        select(SiteDiscoveryRun)
        .where(SiteDiscoveryRun.id == original_run_id)
        .with_for_update()
    )
    if original is None:
        raise LookupError("run not found")
    if original.status != "interrupted":
        raise ValueError("only interrupted Discovery runs can be resumed")
    if not original.checkpoint_path:
        raise ValueError("interrupted run has no checkpoint")
    checkpoint = CheckpointStore().load(
        original.checkpoint_path,
        expected_run_id=original.id,
    )
    existing = session.scalar(
        select(SiteDiscoveryRun).where(
            SiteDiscoveryRun.trigger_type == "resume",
            SiteDiscoveryRun.checkpoint_path == original.checkpoint_path,
            SiteDiscoveryRun.status.in_(UNFINISHED_DISCOVERY_STATUSES),
        )
    )
    if existing is not None:
        return existing
    resumed = SiteDiscoveryRun(
        site_url=original.site_url,
        source_kind=original.source_kind,
        status="queued",
        trigger_type="resume",
        phase=checkpoint.phase,
        round=checkpoint.round,
        checkpoint_path=original.checkpoint_path,
        repair_method_id=original.repair_method_id,
        runtime_version=checkpoint.runtime_version or original.runtime_version,
        node_trace=list(original.node_trace or []),
        retry_count=original.retry_count,
        error_message="已进入恢复队列，等待 Single Agent Loop 调度器。",
    )
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        driver_connection = connection.connection.driver_connection
        if not driver_connection.in_transaction:
            # pysqlite otherwise starts with SAVEPOINT as the outermost DB
            # transaction, whose RELEASE would commit behind the caller's back.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        with session.begin_nested():
            session.add(resumed)
            session.flush()
            append_discovery_event(
                resumed.id,
                event_type="resume_queued",
                summary="已从最近检查点创建恢复运行；恢复时将使用新沙箱容器。",
                phase=resumed.phase,
                round_number=resumed.round,
                payload={"original_run_id": original.id, "checkpoint_available": True},
                session=session,
            )
        return resumed
    except IntegrityError:
        session.expire_all()
        existing = session.scalar(
            select(SiteDiscoveryRun).where(
                SiteDiscoveryRun.trigger_type == "resume",
                SiteDiscoveryRun.checkpoint_path == original.checkpoint_path,
                SiteDiscoveryRun.status.in_(UNFINISHED_DISCOVERY_STATUSES),
            )
        )
        if existing is None:
            raise
        return existing
