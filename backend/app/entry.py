from contextlib import asynccontextmanager
import logging
import os
import threading

from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from app.api.main import create_app
from app.db import SessionLocal
from app.discovery.graph import reclaim_stale_runs
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
    # 回收上一进程遗留的 running run（进程崩/被 kill 后没人收，永久卡 running）
    reclaim_stale_runs()
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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _startup(app)
    yield


app = create_app(lifespan=_lifespan)
