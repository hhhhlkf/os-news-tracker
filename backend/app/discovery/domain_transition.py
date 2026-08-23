"""Cross-worker lock protocol for formal-run creation and domain handover."""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
import time
from typing import Any, Iterator

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models import CrawlMethodDomain, CrawlMethodRun


_SQLITE_DOMAIN_LOCK = RLock()
ACTIVE_FORMAL_RUN_STATES = ("queued", "running", "cancelling")
DOMAIN_LOCK_TIMEOUT_SECONDS = 5.0


class MigrationBusyError(RuntimeError):
    """Retryable contention while serializing one discovery domain."""


def _begin_sqlite_immediate(db: Session, *, timeout_seconds: float, message: str) -> None:
    """Take SQLite's writer lock without inheriting the driver's long timeout."""
    connection = db.connection()
    original_timeout = int(connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one())
    bounded_timeout = max(1, int(timeout_seconds * 1000))
    try:
        connection.exec_driver_sql(f"PRAGMA busy_timeout = {bounded_timeout}")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    except DBAPIError as exc:
        raise MigrationBusyError(message) from exc
    finally:
        # busy_timeout is connection-local and pooled connections are reused.
        # Restore it even when BEGIN failed so unrelated DB work keeps the
        # application's configured timeout.
        connection.exec_driver_sql(f"PRAGMA busy_timeout = {original_timeout}")


def active_formal_run_id(db: Session, method_ids: tuple[int, ...]) -> int | None:
    if not method_ids:
        return None
    return db.scalar(
        select(CrawlMethodRun.id)
        .where(
            CrawlMethodRun.method_id.in_(method_ids),
            CrawlMethodRun.status.in_(ACTIVE_FORMAL_RUN_STATES),
        )
        .order_by(CrawlMethodRun.id)
        .limit(1)
    )


@contextmanager
def domain_transition_lock(
    db: Session,
    domain: str,
    *,
    cancel_event: Any | None = None,
    timeout_seconds: float | None = None,
) -> Iterator[CrawlMethodDomain | None]:
    """Serialize one domain across PostgreSQL workers and SQLite processes/threads.

    Callers must commit their transition before leaving the context.  Failures
    are rolled back before the process-local SQLite lock is released.
    """
    timeout_seconds = DOMAIN_LOCK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    dialect = db.bind.dialect.name if db.bind is not None else ""
    process_lock = _SQLITE_DOMAIN_LOCK if dialect == "sqlite" else None
    acquired = False
    if process_lock is not None:
        acquired = process_lock.acquire(timeout=max(0.001, timeout_seconds))
        if not acquired:
            db.rollback()
            raise MigrationBusyError(f"domain {domain} is busy; retry the operation")
    try:
        if dialect == "sqlite":
            # Callers only perform an identifying read before entering.  Drop
            # that read transaction, then take the database-wide write lock.
            if db.in_transaction():
                db.rollback()
            _begin_sqlite_immediate(
                db,
                timeout_seconds=timeout_seconds,
                message=f"domain {domain} SQLite write lock timed out",
            )
        elif dialect == "postgresql":
            try:
                lock_timeout_ms = max(1, min(2000, int(timeout_seconds * 1000)))
                db.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout_ms}ms'"))
                deadline = time.monotonic() + timeout_seconds
                while not bool(db.scalar(
                    text("SELECT pg_try_advisory_xact_lock(hashtext(:domain))"),
                    {"domain": domain},
                )):
                    if cancel_event is not None and cancel_event.is_set():
                        raise MigrationBusyError(f"domain {domain} lock acquisition was cancelled")
                    if time.monotonic() >= deadline:
                        raise MigrationBusyError(f"domain {domain} is busy; retry the operation")
                    time.sleep(0.05)
            except DBAPIError as exc:
                raise MigrationBusyError(f"domain {domain} lock acquisition timed out") from exc
        try:
            mapping = db.scalar(
                select(CrawlMethodDomain)
                .where(CrawlMethodDomain.domain == domain)
                .with_for_update()
            )
        except DBAPIError as exc:
            raise MigrationBusyError(f"domain {domain} mapping lock timed out") from exc
        yield mapping
    except BaseException:
        db.rollback()
        raise
    finally:
        if process_lock is not None and acquired:
            process_lock.release()


@contextmanager
def domain_transition_locks(
    db: Session,
    domains: list[str] | tuple[str, ...] | set[str],
    *,
    cancel_event: Any | None = None,
) -> Iterator[dict[str, CrawlMethodDomain]]:
    """Acquire multiple domains in stable order under one bounded transaction."""
    ordered = sorted(set(domains))
    if not ordered:
        yield {}
        return
    dialect = db.bind.dialect.name if db.bind is not None else ""
    process_lock = _SQLITE_DOMAIN_LOCK if dialect == "sqlite" else None
    acquired = False
    if process_lock is not None:
        acquired = process_lock.acquire(timeout=DOMAIN_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            db.rollback()
            raise MigrationBusyError("Discovery domains are busy; retry the operation")
    try:
        if dialect == "sqlite":
            if db.in_transaction():
                db.rollback()
            _begin_sqlite_immediate(
                db,
                timeout_seconds=DOMAIN_LOCK_TIMEOUT_SECONDS,
                message="Discovery domain SQLite write lock timed out",
            )
        elif dialect == "postgresql":
            try:
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
                deadline = time.monotonic() + DOMAIN_LOCK_TIMEOUT_SECONDS
                for domain in ordered:
                    while not bool(db.scalar(
                        text("SELECT pg_try_advisory_xact_lock(hashtext(:domain))"),
                        {"domain": domain},
                    )):
                        if cancel_event is not None and cancel_event.is_set():
                            raise MigrationBusyError("Discovery domain lock acquisition was cancelled")
                        if time.monotonic() >= deadline:
                            raise MigrationBusyError(f"domain {domain} is busy; retry the operation")
                        time.sleep(0.05)
            except DBAPIError as exc:
                raise MigrationBusyError("Discovery domain lock acquisition timed out") from exc
        try:
            mappings = list(db.scalars(
                select(CrawlMethodDomain)
                .where(CrawlMethodDomain.domain.in_(ordered))
                .order_by(CrawlMethodDomain.domain)
                .with_for_update()
            ))
        except DBAPIError as exc:
            raise MigrationBusyError("Discovery domain mapping lock timed out") from exc
        yield {mapping.domain: mapping for mapping in mappings}
    except BaseException:
        db.rollback()
        raise
    finally:
        if process_lock is not None and acquired:
            process_lock.release()
