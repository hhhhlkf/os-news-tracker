"""add explicit weekday to weekly mail schedules

Revision ID: x8c9d0e1f2a3
Revises: w7b8c9d0e1f2
Create Date: 2026-08-19 16:20:00.000000
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "x8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "w7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _weekday(value: object) -> int | None:
    if isinstance(value, datetime):
        return value.weekday()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).weekday()
        except ValueError:
            return None
    return None


def upgrade() -> None:
    op.add_column("mail_schedules", sa.Column("weekly_day", sa.Integer(), nullable=True))

    # Legacy schedules used the creation/update day's weekday implicitly. Prefer
    # their already-computed next run so the migration preserves actual behavior.
    bind = op.get_bind()
    schedules = sa.Table("mail_schedules", sa.MetaData(), autoload_with=bind)
    rows = bind.execute(
        sa.select(
            schedules.c.id,
            schedules.c.frequency,
            schedules.c.next_run_at,
            schedules.c.last_sent_at,
            schedules.c.updated_at,
            schedules.c.created_at,
        )
    ).mappings()
    for row in rows:
        if row["frequency"] != "weekly":
            continue
        day = next(
            (
                candidate
                for candidate in (
                    _weekday(row["next_run_at"]),
                    _weekday(row["last_sent_at"]),
                    _weekday(row["updated_at"]),
                    _weekday(row["created_at"]),
                )
                if candidate is not None
            ),
            0,
        )
        bind.execute(schedules.update().where(schedules.c.id == row["id"]).values(weekly_day=day))


def downgrade() -> None:
    op.drop_column("mail_schedules", "weekly_day")
