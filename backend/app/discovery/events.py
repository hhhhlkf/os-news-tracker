"""Append-only persistence repository for auditable Discovery events."""

from __future__ import annotations

from contextlib import contextmanager
import json
import time
from typing import Any, Iterator

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.discovery.redaction import (
    redact_discovery_data,
    should_drop_discovery_field,
    validate_discovery_data_bounds,
)
from app.models import DiscoveryRunEvent, SiteDiscoveryRun


MAX_EVENT_SUMMARY_CHARS = 16 * 1024
MAX_EVENT_PAYLOAD_BYTES = 256 * 1024
_NON_PERSISTED_EVENT_TYPES = {
    "chain_of_thought",
    "heartbeat",
    "model_token",
    "model_token_delta",
    "private_reasoning",
    "raw_model_response",
    "reasoning_content",
    "stdout_chunk",
    "stdout_fragment",
    "token",
    "token_delta",
}
@contextmanager
def _session_scope(session: Session | None) -> Iterator[tuple[Session, bool]]:
    own_session = session is None
    db = session or SessionLocal()
    try:
        yield db, own_session
        if own_session:
            db.commit()
    except BaseException:
        if own_session:
            db.rollback()
        raise
    finally:
        if own_session:
            db.close()


def append_discovery_event(
    run_id: int,
    *,
    event_type: str,
    summary: str,
    phase: str | None = None,
    round_number: int | None = None,
    level: str = "info",
    payload: dict[str, Any] | None = None,
    session: Session | None = None,
) -> DiscoveryRunEvent | None:
    """Atomically increment the run counter and persist one fully redacted event."""
    normalized_type = event_type.strip().lower()
    if normalized_type in _NON_PERSISTED_EVENT_TYPES or should_drop_discovery_field(event_type):
        return None
    validate_discovery_data_bounds(
        summary,
        max_string_chars=MAX_EVENT_SUMMARY_CHARS,
        max_approx_bytes=MAX_EVENT_SUMMARY_CHARS * 4,
    )
    cleaned_summary = str(redact_discovery_data(summary))
    if len(cleaned_summary) > MAX_EVENT_SUMMARY_CHARS:
        raise ValueError("Discovery event summary exceeds its persistence limit")
    cleaned_payload = None
    if payload is not None:
        validate_discovery_data_bounds(
            payload,
            max_string_chars=MAX_EVENT_PAYLOAD_BYTES,
            max_approx_bytes=MAX_EVENT_PAYLOAD_BYTES,
        )
        cleaned_payload = redact_discovery_data(payload)
        serialized_payload = json.dumps(
            cleaned_payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(serialized_payload) > MAX_EVENT_PAYLOAD_BYTES:
            raise ValueError("Discovery event payload exceeds its persistence limit")
    if session is not None:
        return _append_event_in_transaction(
            session,
            run_id=run_id,
            event_type=event_type,
            phase=phase,
            round_number=round_number,
            level=level,
            summary=cleaned_summary,
            payload=cleaned_payload,
        )
    for attempt in range(20):
        db = SessionLocal()
        try:
            event = _append_event_in_transaction(
                db,
                run_id=run_id,
                event_type=event_type,
                phase=phase,
                round_number=round_number,
                level=level,
                summary=cleaned_summary,
                payload=cleaned_payload,
            )
            db.commit()
            return event
        except OperationalError:
            db.rollback()
            if attempt == 19:
                raise
            time.sleep(min(0.01 * (attempt + 1), 0.1))
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
    raise RuntimeError("unable to persist Discovery event")


def _append_event_in_transaction(
    db: Session,
    *,
    run_id: int,
    event_type: str,
    phase: str | None,
    round_number: int | None,
    level: str,
    summary: str,
    payload: dict[str, Any] | None,
) -> DiscoveryRunEvent:
    """Increment the parent counter and insert the event in the caller's transaction."""
    sequence = db.scalar(
        update(SiteDiscoveryRun)
        .where(SiteDiscoveryRun.id == run_id)
        .values(event_sequence=SiteDiscoveryRun.event_sequence + 1)
        .returning(SiteDiscoveryRun.event_sequence)
    )
    if sequence is None:
        raise LookupError(f"Discovery run {run_id} does not exist")
    event = DiscoveryRunEvent(
        run_id=run_id,
        sequence=int(sequence),
        event_type=event_type[:80],
        phase=phase[:40] if phase else None,
        round=round_number,
        level=level[:20],
        summary=summary,
        payload=payload,
    )
    db.add(event)
    db.flush()
    return event


def list_discovery_events(
    run_id: int,
    *,
    after_sequence: int = 0,
    limit: int = 500,
    session: Session | None = None,
) -> list[DiscoveryRunEvent]:
    """Read an ordered replay window; phase 8 can stream this repository as SSE."""
    safe_limit = min(2000, max(1, limit))
    with _session_scope(session) as (db, _own_session):
        return list(
            db.scalars(
                select(DiscoveryRunEvent)
                .where(
                    DiscoveryRunEvent.run_id == run_id,
                    DiscoveryRunEvent.sequence > max(0, after_sequence),
                )
                .order_by(DiscoveryRunEvent.sequence.asc())
                .limit(safe_limit)
            )
        )
