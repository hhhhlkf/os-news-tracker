from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import SiteDiscoveryRun


_start_lock = Lock()


@dataclass
class DiscoveryCapacityExceeded(Exception):
    active_count: int
    max_concurrent: int

    def __str__(self) -> str:
        return f"智能探查并发已满（{self.active_count}/{self.max_concurrent}），请稍后再试"


def count_active_discovery_runs(db) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(SiteDiscoveryRun)
            .where(SiteDiscoveryRun.status == "running")
        )
        or 0
    )


def create_discovery_run_or_raise(site_url: str) -> int:
    settings = get_settings()
    max_concurrent = max(1, int(settings.discovery_max_concurrent_runs or 3))
    with _start_lock:
        db = SessionLocal()
        try:
            active_count = count_active_discovery_runs(db)
            if active_count >= max_concurrent:
                raise DiscoveryCapacityExceeded(active_count=active_count, max_concurrent=max_concurrent)
            run = SiteDiscoveryRun(site_url=site_url, status="running")
            db.add(run)
            db.commit()
            return run.id
        finally:
            db.close()


def finish_discovery_run(
    run_id: int,
    *,
    status: str,
    resulting_method_id: int | None = None,
    node_trace: list | None = None,
    error_message: str | None = None,
    llm_token_usage: int | None = None,
) -> None:
    db = SessionLocal()
    try:
        run = db.get(SiteDiscoveryRun, run_id)
        if not run:
            return
        run.status = status
        run.resulting_method_id = resulting_method_id
        if node_trace is not None:
            run.node_trace = node_trace
        if llm_token_usage is not None:
            run.llm_token_usage = llm_token_usage
        run.error_message = error_message
        run.ended_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
