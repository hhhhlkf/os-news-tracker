"""cap persisted OpenHands context token budgets at 200k

Revision ID: agent_budget_cap_20260831
Revises: agent_budget_20260831
"""

from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "agent_budget_cap_20260831"
down_revision: Union[str, Sequence[str], None] = "agent_budget_20260831"
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
        value = raw.get("tokenBudget")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 200_000:
            continue
        budget = dict(raw)
        budget["tokenBudget"] = 200_000
        bind.execute(
            sa.update(table).where(table.c.id == row["id"]).values(agent_budget=budget)
        )


def upgrade() -> None:
    _clamp_table("site_discovery_runs")
    _clamp_table("discovery_queue_items")


def downgrade() -> None:
    # The previous larger per-run value cannot be reconstructed after clamping.
    pass
