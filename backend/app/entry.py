import logging
import os
import threading
from app.api.main import create_app
from app.db import engine, SessionLocal
from app.models import Base
from app.sources.registry import seed_sources_from_yaml

logging.basicConfig(level=logging.INFO)
app = create_app()


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default) == "1"


def _start_background_task(target, *, name: str) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


@app.on_event("startup")
def _startup():
    Base.metadata.create_all(engine)
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
