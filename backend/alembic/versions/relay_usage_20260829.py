"""add durable identity for trusted OpenHands relay usage

Revision ID: relay_usage_20260829
Revises: queue_delete_requested_20260827
Create Date: 2026-08-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "relay_usage_20260829"
down_revision: Union[str, Sequence[str], None] = "queue_delete_requested_20260827"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("site_discovery_runs") as batch_op:
        batch_op.add_column(sa.Column("cleanup_claim_owner", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("cleanup_claim_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
    with op.batch_alter_table("llm_usage_events") as batch_op:
        batch_op.add_column(sa.Column("usage_provenance", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("relay_session_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("relay_sequence", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("relay_usage_status", sa.String(length=16), nullable=True))
        batch_op.create_index(
            "uq_llm_usage_events_trusted_relay",
            ["discovery_run_id", "usage_provenance", "relay_session_id", "relay_sequence"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_usage_events") as batch_op:
        batch_op.drop_index("uq_llm_usage_events_trusted_relay")
        batch_op.drop_column("relay_sequence")
        batch_op.drop_column("relay_usage_status")
        batch_op.drop_column("relay_session_id")
        batch_op.drop_column("usage_provenance")
    with op.batch_alter_table("site_discovery_runs") as batch_op:
        batch_op.drop_column("cleanup_claim_expires_at")
        batch_op.drop_column("cleanup_claim_owner")
