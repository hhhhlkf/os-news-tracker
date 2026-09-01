"""bound persisted OpenHands context memory to 20-80 events

Revision ID: agent_memory_cap_20260831
Revises: agent_budget_cap_20260831
"""

from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "agent_memory_cap_20260831"
down_revision: Union[str, Sequence[str], None] = "agent_budget_cap_20260831"
branch_labels = None
depends_on = None


def _clamp_table(table_name: str) -> None:
    table = sa.table(
        table_name,
        sa.column("id", sa.Integer()),
        sa.column("agent_budget", sa.JSON()),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.select(table.c.id, table.c.agent_budget)).mappings()
    for row in rows:
        raw: Any = row["agent_budget"]
        if not isinstance(raw, dict):
            continue
        value = raw.get("contextMemory")
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        bounded = min(80, max(20, value))
        if bounded == value:
            continue
        budget = dict(raw)
        budget["contextMemory"] = bounded
        bind.execute(
            sa.update(table).where(table.c.id == row["id"]).values(agent_budget=budget)
        )


def upgrade() -> None:
    _clamp_table("site_discovery_runs")
    _clamp_table("discovery_queue_items")


def downgrade() -> None:
    # Previous out-of-range per-run values cannot be reconstructed after clamping.
    pass
