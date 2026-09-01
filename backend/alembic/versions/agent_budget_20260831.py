"""add per-run OpenHands resource budget snapshots

Revision ID: agent_budget_20260831
Revises: relay_usage_reconcile_20260829
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "agent_budget_20260831"
down_revision: Union[str, Sequence[str], None] = "relay_usage_reconcile_20260829"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("site_discovery_runs") as batch:
        batch.add_column(
            sa.Column("agent_budget", sa.JSON(), nullable=False, server_default="{}")
        )
    with op.batch_alter_table("discovery_queue_items") as batch:
        batch.add_column(
            sa.Column("agent_budget", sa.JSON(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("discovery_queue_items") as batch:
        batch.drop_column("agent_budget")
    with op.batch_alter_table("site_discovery_runs") as batch:
        batch.drop_column("agent_budget")
