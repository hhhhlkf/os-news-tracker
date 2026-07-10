from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import CrawlMethodRun


class ActiveMethodFetchError(RuntimeError):
    def __init__(self, method_id: int, active_run_id: int | None) -> None:
        self.method_id = method_id
        self.active_run_id = active_run_id
        suffix = f" (run_id={active_run_id})" if active_run_id is not None else ""
        super().__init__(f"method {method_id} already has an active fetch job{suffix}")


def _with_session(db: Session | None, fn: Callable[[Session], Any]) -> Any:
    if db is not None:
        return fn(db)
    session = SessionLocal()
    try:
        return fn(session)
    finally:
        session.close()


def get_active_method_fetch_run(method_id: int, db: Session | None = None) -> CrawlMethodRun | None:
    def _get(session: Session) -> CrawlMethodRun | None:
        return session.scalar(
            select(CrawlMethodRun)
            .where(CrawlMethodRun.method_id == method_id, CrawlMethodRun.status == "running")
            .order_by(CrawlMethodRun.started_at.desc(), CrawlMethodRun.id.desc())
        )

    return _with_session(db, _get)


def start_method_fetch_run(
    method_id: int,
    request_payload: dict[str, Any] | None,
    db: Session | None = None,
) -> int:
    def _start(session: Session) -> int:
        run = CrawlMethodRun(
            method_id=method_id,
            status="running",
            request_payload=request_payload,
        )
        session.add(run)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            active = get_active_method_fetch_run(method_id, session)
            raise ActiveMethodFetchError(
                method_id,
                active.id if active is not None else None,
            ) from exc
        session.refresh(run)
        return run.id

    return _with_session(db, _start)


def finish_method_fetch_run(
    run_id: int,
    status: str,
    *,
    discovered_count: int = 0,
    stored_count: int = 0,
    error_message: str | None = None,
    db: Session | None = None,
) -> None:
    def _finish(session: Session) -> None:
        run = session.get(CrawlMethodRun, run_id)
        if run is None or run.status != "running":
            return
        run.status = status
        run.discovered_count = discovered_count
        run.stored_count = stored_count
        run.error_message = error_message
        run.completed_at = datetime.now(timezone.utc)
        session.commit()

    _with_session(db, _finish)
