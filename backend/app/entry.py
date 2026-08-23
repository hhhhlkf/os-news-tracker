from contextlib import asynccontextmanager
import logging
import os
import threading
from datetime import datetime, timezone

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import select

from app.api.main import create_app
from app.db import SessionLocal
from app.models import DiscussionPipelineRun
from app.sources.registry import seed_sources_from_yaml

logging.basicConfig(level=logging.INFO)

_ALEMBIC_INI = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default) == "1"


def _start_background_task(target, *, name: str) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def _run_migrations() -> None:
    """Apply Alembic migrations to bring the DB schema to head.

    Replaces Base.metadata.create_all(): instead of recreating tables from the
    current ORM model (which skips existing tables and never upgrades schema),
    we replay the recorded migration history so restarts are idempotent and
    schema changes are versioned.
    """
    cfg = Config(os.path.abspath(_ALEMBIC_INI))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(_ALEMBIC_INI), "alembic"))
    command.upgrade(cfg, "head")


def _backfill_discovery_experience_embeddings() -> None:
    from app.discovery.loop.experience_store import backfill_legacy_approved_experiences
    from app.discovery.loop.rag import backfill_missing_experience_embeddings

    session = SessionLocal()
    try:
        converted = backfill_legacy_approved_experiences(session)
        session.commit()
        if converted:
            logging.getLogger(__name__).info(
                "split %s legacy Discovery experience(s) into Explore/Build knowledge",
                converted,
            )
        count = backfill_missing_experience_embeddings(session)
        if count:
            logging.getLogger(__name__).info("backfilled %s Discovery experience embedding(s)", count)
    except Exception:
        session.rollback()
        logging.getLogger(__name__).warning(
            "Discovery experience embedding backfill deferred",
            exc_info=True,
        )
    finally:
        session.close()


def _startup(app: FastAPI) -> None:
    _run_migrations()
    # Discovery 任务与沙箱均属于当前进程。重启后清理遗留容器并把未完成
    # 运行标记为可恢复的 interrupted；绝不尝试重新连接旧进程或旧容器。
    from app.discovery.recovery import recover_discovery_state

    interrupted_discovery_runs = recover_discovery_state()
    if interrupted_discovery_runs:
        logging.getLogger(__name__).warning(
            "marked %s Discovery run(s) interrupted after startup",
            interrupted_discovery_runs,
        )
    from app.discovery.fetch_runs import recover_dead_method_fetch_runs

    try:
        recovered_method_runs = recover_dead_method_fetch_runs()
    except Exception:
        recovered_method_runs = 0
        logging.getLogger(__name__).exception(
            "formal method owner recovery deferred because sandbox cleanup was not proven"
        )
    if recovered_method_runs:
        logging.getLogger(__name__).warning(
            "recovered %s formal method run(s) whose owner process exited",
            recovered_method_runs,
        )
    from app.discovery.loop.artifacts import recover_stale_packaging_artifacts

    packaging_session = SessionLocal()
    try:
        recovered_packaging = recover_stale_packaging_artifacts(packaging_session)
        if recovered_packaging:
            logging.getLogger(__name__).warning(
                "compensated %s interrupted connector packaging artifact(s)/record(s)",
                recovered_packaging,
            )
    finally:
        packaging_session.close()
    # Register the in-process Single Agent Loop resume dispatcher after recovery.
    # Queued resume records are then atomically claimed without loading legacy Graph code.
    from app.discovery.loop.engine import get_website_loop_engine

    get_website_loop_engine()
    # Recompute time-based DSL retirement eligibility after downtime.  This
    # never runs a shadow crawl or cutover automatically.
    from app.discovery.migration import (
        bootstrap_legacy_migrations,
        coordinate_next_migration_shadow,
        legacy_cleanup_readiness,
        reconcile_unrecorded_formal_runs,
        recover_stale_migration_comparisons,
        refresh_migration_eligibility,
    )
    from app.discovery.domain_transition import MigrationBusyError

    migration_session = SessionLocal()
    try:
        recover_stale_migration_comparisons(migration_session)
        try:
            registered_migrations = bootstrap_legacy_migrations(migration_session)
        except MigrationBusyError as exc:
            migration_session.rollback()
            registered_migrations = 0
            logging.getLogger(__name__).warning(
                "deferred Discovery migration bootstrap because a domain is busy: %s", exc
            )
        if registered_migrations:
            logging.getLogger(__name__).info(
                "registered %s existing per-site Discovery migration(s)",
                registered_migrations,
            )
        refresh_migration_eligibility(migration_session)
        try:
            reconcile_unrecorded_formal_runs(migration_session)
        except MigrationBusyError as exc:
            migration_session.rollback()
            logging.getLogger(__name__).warning(
                "deferred formal migration reconciliation because a domain is busy: %s", exc
            )
        cleanup_readiness = legacy_cleanup_readiness(migration_session)
        if not cleanup_readiness["ready"]:
            logging.getLogger(__name__).info(
                "legacy Discovery interpreter cleanup remains blocked: %s",
                cleanup_readiness["blockers"],
            )
    finally:
        migration_session.close()
    _start_background_task(
        coordinate_next_migration_shadow,
        name="discovery-migration-shadow-coordinator",
    )
    _start_background_task(
        _backfill_discovery_experience_embeddings,
        name="discovery-experience-embedding-backfill",
    )
    # 趋势总结由当前 Web 进程内的 daemon thread 执行。进程重启或热重载
    # 后，旧线程已不存在，因此必须把遗留状态收敛为 failed，不能让前端
    # 永久显示“运行中”。
    from app.trends.service import TrendService

    trend_session = SessionLocal()
    try:
        reclaimed_trend_runs = TrendService(trend_session).reclaim_orphaned_trend_runs()
        if reclaimed_trend_runs:
            logging.getLogger(__name__).warning(
                "reclaimed %s interrupted trend evaluation run(s) after startup",
                reclaimed_trend_runs,
            )
    finally:
        trend_session.close()
    # 讨论邮件的手动整理由本进程中的后台线程执行。重启后线程无法续跑，
    # 因此将遗留记录显式标记失败，前端可以提示用户重新发起，而非永久显示运行中。
    discussion_session = SessionLocal()
    try:
        interrupted_discussion_runs = list(discussion_session.scalars(
            select(DiscussionPipelineRun).where(DiscussionPipelineRun.status.in_(("running", "stopping"))),
        ))
        if interrupted_discussion_runs:
            finished_at = datetime.now(timezone.utc)
            for run in interrupted_discussion_runs:
                run.status = "failed"
                run.error_message = "服务进程已重启，任务未完成；请重新发起收取与整理。"
                run.finished_at = finished_at
            discussion_session.commit()
            logging.getLogger(__name__).warning(
                "marked %s interrupted discussion pipeline run(s) as failed after startup",
                len(interrupted_discussion_runs),
            )
    finally:
        discussion_session.close()
    if _env_flag("RUN_SEED", "0"):
        seed_path = os.path.join(os.path.dirname(__file__), "sources", "seed_sources.yaml")
        session = SessionLocal()
        try:
            seed_sources_from_yaml(session, seed_path)
        finally:
            session.close()
    if _env_flag("ENABLE_SCHEDULER", "1"):
        from app.scheduler import run_startup_backfill, start_scheduler

        app.state.scheduler = start_scheduler()
        if _env_flag("RUN_STARTUP_BACKFILL", "0"):
            app.state.startup_backfill_thread = _start_background_task(
                run_startup_backfill,
                name="startup-backfill",
            )
    # 邮件预定发送有独立开关，与 ENABLE_SCHEDULER（新闻源 cron）解耦，
    # 便于开发态只跑邮件定时。后续系统定时抓取同样应使用自己的独立开关。
    if _env_flag("ENABLE_MAIL_SCHEDULER", "0"):
        from app.scheduler import start_mail_scheduler

        app.state.mail_scheduler = start_mail_scheduler()
    # 系统定时抓取有独立开关，与 ENABLE_SCHEDULER / ENABLE_MAIL_SCHEDULER 解耦。
    if _env_flag("ENABLE_MORNING_CRAWL_SCHEDULER", "0"):
        from app.scheduler import start_morning_crawl_scheduler

        app.state.morning_crawl_scheduler = start_morning_crawl_scheduler()
    # 全局定时趋势总结同样使用独立开关，不影响新闻源 / 邮件 / 晨间抓取调度器。
    if _env_flag("ENABLE_TREND_SCHEDULER", "0"):
        from app.scheduler import start_trend_scheduler

        app.state.trend_scheduler = start_trend_scheduler()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _startup(app)
    try:
        yield
    finally:
        from app.wechat_auth import wechat_qr_login_manager

        wechat_qr_login_manager.shutdown_all()


app = create_app(lifespan=_lifespan)
