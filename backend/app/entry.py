import logging
import os
from app.api.main import create_app
from app.db import engine, SessionLocal
from app.models import Base
from app.sources.registry import seed_sources_from_yaml

logging.basicConfig(level=logging.INFO)
app = create_app()


@app.on_event("startup")
def _startup():
    Base.metadata.create_all(engine)
    seed_path = os.path.join(os.path.dirname(__file__), "sources", "seed_sources.yaml")
    session = SessionLocal()
    try:
        seed_sources_from_yaml(session, seed_path)
    finally:
        session.close()
    if os.environ.get("ENABLE_SCHEDULER", "1") == "1":
        from app.scheduler import start_scheduler
        app.state.scheduler = start_scheduler()
