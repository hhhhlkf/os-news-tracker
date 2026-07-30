"""Persisted state machine of the trend embedding model.

States: ``not_installed`` → ``downloading`` → ``ready`` → ``loading`` →
``processing`` → back to ``ready``; any step may end in ``failed`` with an
actionable message. The row is also the cross-process guard that keeps a single
embedding worker running at a time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.trends.embedding import EmbeddingProviderError, build_embedding_provider, current_embedding_descriptor
from app.trends.embedding_config import get_trend_embedding_settings
from app.trends.models import TrendEmbeddingModelState

TrendEmbeddingStatus = Literal["not_installed", "downloading", "ready", "loading", "processing", "failed"]

IN_FLIGHT_STATUSES: tuple[str, ...] = ("downloading", "loading", "processing")

_STATE_ID = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def probe_cache_installed() -> tuple[bool, str | None, str]:
    """Filesystem-only cache probe; never imports model runtimes."""
    settings = get_trend_embedding_settings()
    try:
        cache = build_embedding_provider(settings).probe_cache()
    except EmbeddingProviderError:
        return False, None, settings.cache_dir
    return cache.installed, cache.model_version, cache.cache_dir or settings.cache_dir


def sync_state(db: Session) -> TrendEmbeddingModelState:
    """Return the state row aligned with the configured provider.

    Cache availability is not probed here: only the worker container mounts the
    model cache, so it is reported through :func:`apply_cache_report`.
    """
    descriptor = current_embedding_descriptor()
    state = db.get(TrendEmbeddingModelState, _STATE_ID)
    if state is None:
        state = TrendEmbeddingModelState(
            id=_STATE_ID,
            provider=descriptor.provider,
            model_id=descriptor.model_id,
            model_revision=descriptor.model_revision,
            embedding_version=descriptor.embedding_version,
            dimension=descriptor.dimension,
            normalization=descriptor.normalization,
            status="not_installed",
            status_updated_at=_now(),
        )
        db.add(state)
    elif state.embedding_version != descriptor.embedding_version or state.model_revision != descriptor.model_revision:
        # A new vector space invalidates the previous readiness and version.
        state.provider = descriptor.provider
        state.model_id = descriptor.model_id
        state.model_revision = descriptor.model_revision
        state.embedding_version = descriptor.embedding_version
        state.dimension = descriptor.dimension
        state.normalization = descriptor.normalization
        state.model_version = None
        state.status = "not_installed"
        state.error_message = None
        state.remedy = None
        state.worker_pid = None
        state.worker_started_at = None
        state.ready_at = None
        state.status_updated_at = _now()

    _expire_stale_job(state)
    db.commit()
    db.refresh(state)
    return state


def _expire_stale_job(state: TrendEmbeddingModelState) -> None:
    if state.status not in IN_FLIGHT_STATUSES:
        return
    timeout = get_trend_embedding_settings().job_timeout_seconds
    started_at = _as_utc(state.worker_started_at)
    if started_at is not None and _now() - started_at <= timedelta(seconds=timeout):
        return
    state.status = "failed"
    state.error_message = f"Embedding Worker 在 {timeout} 秒内没有完成任务，已判定为超时。"
    state.remedy = "检查 Worker 容器日志与网络状况后，重新点击“准备模型”。"
    state.worker_pid = None
    state.status_updated_at = _now()


def apply_cache_report(
    db: Session,
    *,
    installed: bool,
    model_version: str | None,
) -> TrendEmbeddingModelState:
    """Reconcile readiness with what the worker reports about its cache."""
    state = sync_state(db)
    if state.status in IN_FLIGHT_STATUSES or state.status == "failed":
        return state

    if installed:
        if model_version and state.model_version != model_version:
            state.model_version = model_version
        if state.status == "not_installed":
            state.status = "ready"
            state.ready_at = _now()
            state.status_updated_at = _now()
    elif state.status == "ready":
        state.status = "not_installed"
        state.model_version = None
        state.status_updated_at = _now()

    db.commit()
    db.refresh(state)
    return state


def read_state() -> TrendEmbeddingModelState:
    db = SessionLocal()
    try:
        return sync_state(db)
    finally:
        db.close()


def report_cache_state(*, installed: bool, model_version: str | None) -> TrendEmbeddingModelState:
    """Cache reconciliation from a process that owns its own session."""
    db = SessionLocal()
    try:
        return apply_cache_report(db, installed=installed, model_version=model_version)
    finally:
        db.close()


def try_claim_worker(initial_status: TrendEmbeddingStatus, *, pid: int | None = None) -> bool:
    """Atomically move the singleton row into an in-flight status."""
    db = SessionLocal()
    try:
        sync_state(db)
        now = _now()
        result = db.execute(
            update(TrendEmbeddingModelState)
            .where(
                TrendEmbeddingModelState.id == _STATE_ID,
                TrendEmbeddingModelState.status.not_in(IN_FLIGHT_STATUSES),
            )
            .values(
                status=initial_status,
                error_message=None,
                remedy=None,
                worker_pid=pid,
                worker_started_at=now,
                status_updated_at=now,
            )
        )
        db.commit()
        return bool(result.rowcount)
    finally:
        db.close()


def set_worker_pid(pid: int | None) -> None:
    db = SessionLocal()
    try:
        db.execute(
            update(TrendEmbeddingModelState)
            .where(TrendEmbeddingModelState.id == _STATE_ID)
            .values(worker_pid=pid)
        )
        db.commit()
    finally:
        db.close()


def mark_status(status: TrendEmbeddingStatus, *, pid: int | None = None) -> None:
    db = SessionLocal()
    try:
        values: dict[str, object] = {"status": status, "status_updated_at": _now()}
        if pid is not None:
            values["worker_pid"] = pid
        db.execute(update(TrendEmbeddingModelState).where(TrendEmbeddingModelState.id == _STATE_ID).values(**values))
        db.commit()
    finally:
        db.close()


def mark_ready(*, model_version: str | None = None) -> None:
    db = SessionLocal()
    try:
        now = _now()
        values: dict[str, object] = {
            "status": "ready",
            "error_message": None,
            "remedy": None,
            "worker_pid": None,
            "ready_at": now,
            "status_updated_at": now,
        }
        if model_version:
            values["model_version"] = model_version
        db.execute(update(TrendEmbeddingModelState).where(TrendEmbeddingModelState.id == _STATE_ID).values(**values))
        db.commit()
    finally:
        db.close()


def mark_failed(message: str, *, remedy: str | None = None) -> None:
    db = SessionLocal()
    try:
        db.execute(
            update(TrendEmbeddingModelState)
            .where(TrendEmbeddingModelState.id == _STATE_ID)
            .values(
                status="failed",
                error_message=message,
                remedy=remedy,
                worker_pid=None,
                status_updated_at=_now(),
            )
        )
        db.commit()
    finally:
        db.close()


def reclaim_stale_state() -> None:
    """Fail in-flight rows left behind by a crashed or restarted worker."""
    db = SessionLocal()
    try:
        db.execute(
            update(TrendEmbeddingModelState)
            .where(
                TrendEmbeddingModelState.id == _STATE_ID,
                TrendEmbeddingModelState.status.in_(IN_FLIGHT_STATUSES),
            )
            .values(
                status="failed",
                error_message="Embedding Worker 在任务未完成时退出（服务重启或进程被终止）。",
                remedy="重新点击“准备模型”；已下载完成的缓存文件会被复用，不会重复下载。",
                worker_pid=None,
                status_updated_at=_now(),
            )
        )
        db.commit()
    finally:
        db.close()
