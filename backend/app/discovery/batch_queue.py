"""Persistent, shared dispatcher for batch Discovery work.

The queue is deliberately the only caller-facing seam for batch work.  It
owns ordering, the two active slots, terminal-state reconciliation, and
requeue/cancellation transitions; each item still delegates execution to the
existing Single Agent Loop facade.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.discovery.agent_budget import normalize_agent_budget
from app.discovery.loop.artifacts import find_existing_method
from app.discovery.method_keys import normalized_website_domain_key
from app.discovery.multi_graph import start_multi_discovery_run
from app.discovery.runtime import DiscoveryCapacityExceeded
from app.models import DiscoveryQueueItem, DiscoveryQueueState, SiteDiscoveryRun


logger = logging.getLogger(__name__)
BATCH_DISCOVERY_MAX_PARALLEL = 2
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
_ACTIVE_QUEUE_STATUSES = ("starting", "running", "cancelling")
_DUPLICATE_QUEUE_STATUSES = ("queued", *_ACTIVE_QUEUE_STATUSES, "failed")
_PUBLIC_QUEUE_ERRORS = {
    "服务重启，探查未完成",
    "探查已取消",
    "探查失败，请重新放入队列",
    "探查启动失败，请重新放入队列",
    "已停止探查",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _public_queue_error(message: str | None) -> str | None:
    if message is None:
        return None
    if message in _PUBLIC_QUEUE_ERRORS:
        return message
    return "探查失败，请重新放入队列"


def serialize_queue_item(item: DiscoveryQueueItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "input": item.input,
        "display_input": item.display_input,
        "selected_route_type": item.selected_route_type,
        "resolved_route_type": item.resolved_route_type,
        "route_source": item.route_source,
        "agent_budget": normalize_agent_budget(item.agent_budget),
        "status": item.status,
        "run_id": item.run_id,
        # Queue errors are deliberately stable public summaries.  Raw runner
        # failures remain in server-side run/event audit records only.
        "error_message": _public_queue_error(item.error_message),
        "created_at": item.created_at.isoformat() if item.created_at else _utcnow().isoformat(),
        "started_at": item.started_at.isoformat() if item.started_at else None,
    }


def queue_snapshot(session: Session) -> dict[str, Any]:
    pending = list(session.scalars(
        select(DiscoveryQueueItem)
        .where(DiscoveryQueueItem.status.in_(("queued", *_ACTIVE_QUEUE_STATUSES)))
        .order_by(DiscoveryQueueItem.created_at.asc(), DiscoveryQueueItem.id.asc())
    ))
    failed = list(session.scalars(
        select(DiscoveryQueueItem)
        .where(DiscoveryQueueItem.status == "failed")
        .order_by(DiscoveryQueueItem.completed_at.desc(), DiscoveryQueueItem.id.desc())
    ))
    completed = list(session.scalars(
        select(DiscoveryQueueItem)
        .where(DiscoveryQueueItem.status == "completed")
        .order_by(DiscoveryQueueItem.completed_at.desc(), DiscoveryQueueItem.id.desc())
        .limit(20)
    ))
    return {
        "pending": [serialize_queue_item(item) for item in pending],
        "failed": [serialize_queue_item(item) for item in failed],
        # This is intentionally not a third visible lane: retaining a small
        # completion window lets an observer finish the review hand-off after
        # a running item leaves the two operational queues.
        "completed": [serialize_queue_item(item) for item in completed],
        "running_count": sum(item.status in _ACTIVE_QUEUE_STATUSES for item in pending),
        "max_parallel": BATCH_DISCOVERY_MAX_PARALLEL,
    }


class DiscoveryBatchQueue:
    """Own the durable queue lifecycle in one lightweight background thread."""

    def __init__(self) -> None:
        self._wake = threading.Event()
        self._started = False
        self._start_lock = threading.Lock()
        self._dispatch_lock = threading.Lock()

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._started = True
        threading.Thread(
            target=self._work,
            daemon=True,
            name="discovery-batch-queue",
        ).start()
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    def recover(self) -> int:
        """Move orphaned active items to the retry lane after a process restart."""
        db = SessionLocal()
        try:
            recovered = 0
            running = list(db.scalars(
                select(DiscoveryQueueItem).where(DiscoveryQueueItem.status.in_(_ACTIVE_QUEUE_STATUSES))
            ))
            for item in running:
                run = db.get(SiteDiscoveryRun, item.run_id) if item.run_id is not None else None
                if run is None or run.status in _TERMINAL_RUN_STATUSES:
                    if item.delete_requested:
                        db.delete(item)
                    else:
                        item.status = "failed"
                        item.error_message = "服务重启，探查未完成"
                        item.completed_at = _utcnow()
                    recovered += 1
            if recovered:
                db.commit()
            return recovered
        finally:
            db.close()

    def _work(self) -> None:
        while True:
            try:
                self.dispatch_once()
            except Exception:
                logger.exception("batch Discovery queue dispatch failed")
            self._wake.wait(timeout=0.75)
            self._wake.clear()

    def dispatch_once(self) -> None:
        with self._dispatch_lock:
            self._reconcile_finished()
            while True:
                item = self._claim_next_if_slot()
                if item is None:
                    return
                if not self._start_claimed(item):
                    return

    def _reconcile_finished(self) -> None:
        db = SessionLocal()
        try:
            active = list(db.scalars(
                select(DiscoveryQueueItem).where(
                    DiscoveryQueueItem.status.in_(_ACTIVE_QUEUE_STATUSES),
                )
            ))
            changed = False
            for item in active:
                run = db.get(SiteDiscoveryRun, item.run_id)
                if run is None:
                    if item.status != "cancelling":
                        continue
                    if item.delete_requested:
                        db.delete(item)
                    else:
                        item.status = "failed"
                        item.error_message = "探查已取消"
                        item.completed_at = _utcnow()
                    changed = True
                    continue
                if run.status not in _TERMINAL_RUN_STATUSES:
                    continue
                if item.delete_requested:
                    db.delete(item)
                elif run.status == "completed":
                    item.completed_at = _utcnow()
                    item.status = "completed"
                    item.error_message = None
                else:
                    item.completed_at = _utcnow()
                    item.status = "failed"
                    item.error_message = "探查已取消" if run.status == "cancelled" else "探查失败，请重新放入队列"
                changed = True
            if changed:
                db.commit()
        finally:
            db.close()

    def _claim_next_if_slot(self) -> dict[str, Any] | None:
        """Atomically reserve one of the two shared slots and claim its next item."""
        db = SessionLocal()
        try:
            # This one fixed row is a narrow cross-process seam: PostgreSQL
            # locks it before calculating capacity and changing queue state.
            # SQLite test/dev remains serialized by this process's dispatcher
            # lock, while production never relies on a process-local counter.
            state = db.scalars(
                select(DiscoveryQueueState)
                .where(DiscoveryQueueState.id == 1)
                .with_for_update()
            ).first()
            if state is None:
                state = DiscoveryQueueState(id=1)
                db.add(state)
                db.flush()
            running_count = int(db.scalar(
                select(func.count())
                .select_from(DiscoveryQueueItem)
                .where(DiscoveryQueueItem.status.in_(_ACTIVE_QUEUE_STATUSES))
            ) or 0)
            if running_count >= BATCH_DISCOVERY_MAX_PARALLEL:
                db.rollback()
                return None
            item = db.scalars(
                select(DiscoveryQueueItem)
                .where(DiscoveryQueueItem.status == "queued")
                .order_by(DiscoveryQueueItem.created_at.asc(), DiscoveryQueueItem.id.asc())
                .limit(1)
                .with_for_update()
            ).first()
            if item is None:
                db.rollback()
                return None
            item.status = "starting"
            item.started_at = _utcnow()
            item.error_message = None
            db.commit()
            return {
                "id": item.id,
                "input": item.input,
                "force": item.force,
                "name": item.name,
                "selected_route_type": item.selected_route_type,
                "resolved_route_type": item.resolved_route_type,
                "route_source": item.route_source,
                "agent_budget": normalize_agent_budget(item.agent_budget),
            }
        finally:
            db.close()

    def _start_claimed(self, item: dict[str, Any]) -> bool:
        try:
            if not self._start_is_requested(item["id"]):
                self._finish_unstarted_cancellation(item["id"])
                return True
            route_type = item["resolved_route_type"] or item["selected_route_type"]
            result = start_multi_discovery_run(
                item["input"],
                force=bool(item["force"]),
                name=item["name"],
                hints=_route_hints(route_type),
                selected_route_type=route_type,
                route_source=item["route_source"],
                agent_budget=item["agent_budget"],
            )
            run_id = result.get("run_id")
            if result.get("status") != "started" or not isinstance(run_id, int):
                self._fail_claim(item["id"])
                return True
            db = SessionLocal()
            cancellation_requested = False
            try:
                row = db.get(DiscoveryQueueItem, item["id"])
                if row is not None and row.status in {"starting", "cancelling"}:
                    row.run_id = run_id
                    cancellation_requested = row.status == "cancelling"
                    if not cancellation_requested:
                        row.status = "running"
                    db.commit()
                elif row is None or row.status == "cancelling":
                    # A concurrent administrative cancellation/removal won
                    # the state transition.  The run was already created, so
                    # immediately signal it rather than leaving an orphan.
                    cancellation_requested = True
            finally:
                db.close()
            if cancellation_requested:
                self._cancel_started_run(run_id)
            return True
        except DiscoveryCapacityExceeded:
            # A separate direct run may temporarily own the system-wide
            # Discovery capacity.  This remains pending work, not a failed
            # target; retry after the next dispatcher wake-up.
            self._return_claim_to_pending(item["id"])
            return False
        except Exception as exc:
            logger.exception("failed to start batch Discovery item %s", item["id"])
            self._fail_claim(item["id"])
            return True

    def _return_claim_to_pending(self, item_id: int) -> None:
        db = SessionLocal()
        try:
            item = db.get(DiscoveryQueueItem, item_id)
            if item is not None and item.status == "starting":
                item.status = "queued"
                item.started_at = None
                item.error_message = None
                db.commit()
        finally:
            db.close()

    def _fail_claim(self, item_id: int) -> None:
        db = SessionLocal()
        try:
            item = db.get(DiscoveryQueueItem, item_id)
            if item is not None and item.status in {"starting", "cancelling"}:
                if item.delete_requested:
                    db.delete(item)
                else:
                    item.status = "failed"
                    item.error_message = "探查启动失败，请重新放入队列"
                    item.completed_at = _utcnow()
                db.commit()
        finally:
            db.close()

    def _start_is_requested(self, item_id: int) -> bool:
        db = SessionLocal()
        try:
            item = db.get(DiscoveryQueueItem, item_id)
            return item is not None and item.status == "starting"
        finally:
            db.close()

    def _finish_unstarted_cancellation(self, item_id: int) -> None:
        db = SessionLocal()
        try:
            item = db.get(DiscoveryQueueItem, item_id)
            if item is None or item.status != "cancelling" or item.run_id is not None:
                return
            if item.delete_requested:
                db.delete(item)
            else:
                item.status = "failed"
                item.error_message = "探查已取消"
                item.completed_at = _utcnow()
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _cancel_started_run(run_id: int) -> None:
        from app.discovery.cancel import request_cancel
        from app.discovery.loop.engine import cancel_website_loop_run
        from app.discovery.wechat_plugin import cancel_wechat_discovery_run

        request_cancel(run_id)
        cancel_website_loop_run(run_id)
        cancel_wechat_discovery_run(run_id)


def _route_hints(route_type: str | None) -> dict[str, Any]:
    return {
        "website": {"source_kind": "website"},
        "wechat_search": {"source_kind": "wechat_search"},
        "wechat_history": {"source_kind": "wechat_history"},
        "internal_forum": {"source_kind": "internal_forum"},
    }.get(route_type or "", {})


_batch_queue = DiscoveryBatchQueue()


def get_discovery_batch_queue() -> DiscoveryBatchQueue:
    return _batch_queue


def _normalized_entry_url(value: str | None) -> str | None:
    """Normalize only harmless URL syntax differences for queue deduplication."""
    parsed = urlparse((value or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return normalized_website_domain_key(value or "")


def enqueue_batch_item(session: Session, payload: dict[str, Any]) -> tuple[str, DiscoveryQueueItem | dict[str, Any]]:
    """Persist a queue item, preserving the existing force/duplicate contract."""
    entry_url = _normalized_entry_url(payload.get("input")) if payload.get("resolved_route_type") == "website" else None
    if entry_url:
        queued_item = session.scalars(
            select(DiscoveryQueueItem)
            .where(
                DiscoveryQueueItem.status.in_(_DUPLICATE_QUEUE_STATUSES),
                or_(
                    DiscoveryQueueItem.resolved_route_type == "website",
                    DiscoveryQueueItem.selected_route_type == "website",
                ),
            )
            .order_by(DiscoveryQueueItem.created_at.asc(), DiscoveryQueueItem.id.asc())
        ).all()
        duplicate = next((item for item in queued_item if _normalized_entry_url(item.input) == entry_url), None)
        if duplicate is not None:
            return "queue_duplicate", {"item_id": duplicate.id, "status": duplicate.status}

    if not payload["force"]:
        existing = find_existing_method(payload["input"], session)
        if existing:
            return "duplicate", existing
    item = DiscoveryQueueItem(
        name=payload.get("name"),
        input=payload["input"],
        display_input=payload.get("display_input"),
        selected_route_type=payload.get("selected_route_type"),
        resolved_route_type=payload.get("resolved_route_type"),
        route_source=payload.get("route_source") or "inferred",
        force=bool(payload["force"]),
        agent_budget=normalize_agent_budget(payload.get("agent_budget")),
        status="queued",
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    get_discovery_batch_queue().wake()
    return "queued", item
