"""add persistent batch Discovery queue

Revision ID: a1b2c3d4e5f7
Revises: h9i0j1k2l3m4
Create Date: 2026-08-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, Sequence[str], None] = "h9i0j1k2l3m4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "discovery_queue_state",
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    op.bulk_insert(
        sa.table("discovery_queue_state", sa.Column("id", sa.Integer())),
        [{"id": 1}],
    )
    op.create_table(
        "discovery_queue_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=300), nullable=True),
        sa.Column("input", sa.String(length=2000), nullable=False),
        sa.Column("display_input", sa.String(length=2000), nullable=True),
        sa.Column("selected_route_type", sa.String(length=40), nullable=True),
        sa.Column("resolved_route_type", sa.String(length=40), nullable=True),
        sa.Column("route_source", sa.String(length=20), nullable=False, server_default="inferred"),
        sa.Column("force", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column(
            "run_id", sa.Integer(),
            sa.ForeignKey("site_discovery_runs.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("delete_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'starting', 'running', 'cancelling', 'failed', 'completed')",
            name="ck_discovery_queue_items_status",
        ),
    )
    op.create_index(
        "ix_discovery_queue_items_status_created",
        "discovery_queue_items",
        ["status", "created_at", "id"],
    )
    op.create_index("ix_discovery_queue_items_run_id", "discovery_queue_items", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_discovery_queue_items_run_id", table_name="discovery_queue_items")
    op.drop_index("ix_discovery_queue_items_status_created", table_name="discovery_queue_items")
    op.drop_table("discovery_queue_items")
    op.drop_table("discovery_queue_state")
