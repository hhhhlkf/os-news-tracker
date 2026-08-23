"""Persistent daily scheduling policy for technical-discussion mail collection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DiscussionScheduleConfig


BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def _scheduled_time(config: DiscussionScheduleConfig, reference: datetime) -> datetime | None:
    try:
        hour, minute = (int(value) for value in config.run_time.split(":", 1))
    except (AttributeError, ValueError):
        return None
    return reference.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _next_run(config: DiscussionScheduleConfig, reference: datetime) -> datetime | None:
    scheduled = _scheduled_time(config, reference)
    if scheduled is None:
        return None
    return scheduled if scheduled > reference else scheduled + timedelta(days=1)


def get_or_create_schedule_config(db: Session) -> DiscussionScheduleConfig:
    config = db.scalar(select(DiscussionScheduleConfig).order_by(DiscussionScheduleConfig.id).limit(1))
    if config is None:
        now = beijing_now()
        config = DiscussionScheduleConfig(created_at=now, updated_at=now)
        config.next_run_at = _next_run(config, now)
        db.add(config)
        db.commit()
        db.refresh(config)
    elif config.next_run_at is None:
        config.next_run_at = _next_run(config, beijing_now())
        config.updated_at = beijing_now()
        db.commit()
        db.refresh(config)
    return config


def schedule_response(config: DiscussionScheduleConfig) -> dict:
    return {
        "enabled": config.enabled,
        "run_time": config.run_time,
        "patrol_interval_hours": config.patrol_interval_hours,
        "last_run_at": config.last_run_at,
        "last_run_status": config.last_run_status,
        "last_success_date": config.last_success_date,
        "next_run_at": config.next_run_at,
    }


def update_schedule_config(
    db: Session,
    *,
    enabled: bool | None = None,
    run_time: str | None = None,
    patrol_interval_hours: int | None = None,
) -> DiscussionScheduleConfig:
    config = get_or_create_schedule_config(db)
    if enabled is not None:
        config.enabled = enabled
    if run_time is not None:
        config.run_time = run_time
    if patrol_interval_hours is not None:
        config.patrol_interval_hours = patrol_interval_hours
    now = beijing_now()
    config.updated_at = now
    config.next_run_at = _next_run(config, now)
    db.commit()
    db.refresh(config)
    return config


def schedule_due_now(config: DiscussionScheduleConfig, *, now: datetime) -> bool:
    if not config.enabled:
        return False
    scheduled = _scheduled_time(config, now)
    if scheduled is None or now < scheduled:
        return False
    # One primary collection per Beijing calendar day, even if it failed. A
    # later patrol owns retries and prevents a minute tick from hammering IMAP.
    return config.last_run_at is None or config.last_run_at.date() != now.date()


def patrol_due_now(config: DiscussionScheduleConfig, *, now: datetime) -> bool:
    if not config.enabled or config.last_run_status not in {"failed", "partial"} or config.last_run_at is None:
        return False
    return now >= config.last_run_at + timedelta(hours=max(1, config.patrol_interval_hours))


def mark_run_started(db: Session, config: DiscussionScheduleConfig, *, now: datetime) -> None:
    config.last_run_at = now
    config.last_run_status = "running"
    config.updated_at = now
    db.commit()


def mark_run_finished(db: Session, *, status: str) -> None:
    config = get_or_create_schedule_config(db)
    now = beijing_now()
    config.last_run_at = now
    config.last_run_status = status
    if status == "succeeded":
        config.last_success_date = now.date().isoformat()
    config.next_run_at = _next_run(config, now)
    config.updated_at = now
    db.commit()
