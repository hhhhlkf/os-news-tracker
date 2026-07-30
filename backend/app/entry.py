from contextlib import asynccontextmanager
import logging
import os
import threading

from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from app.api.main import create_app
from app.db import SessionLocal
from app.discovery.graph import STALE_RUN_TIMEOUT_SECONDS, reclaim_stale_runs
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


def _startup(app: FastAPI) -> None:
    _run_migrations()
    # 仅回收超时的 running run，避免开发态热重启时误伤刚启动的任务
    reclaim_stale_runs(STALE_RUN_TIMEOUT_SECONDS)
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
