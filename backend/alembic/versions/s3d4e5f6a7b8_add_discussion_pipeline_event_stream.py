"""add append-only technical discussion event stream

Revision ID: s3d4e5f6a7b8
Revises: r2c3d4e5f6a7
Create Date: 2026-08-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "s3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "r2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discussion_pipeline_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pipeline_run_id", sa.String(length=36), sa.ForeignKey("discussion_pipeline_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("stage", sa.String(length=80), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.UniqueConstraint("pipeline_run_id", "sequence", name="uq_discussion_pipeline_event_sequence"),
    )
    op.create_index("ix_discussion_pipeline_events_pipeline_run_id", "discussion_pipeline_events", ["pipeline_run_id"])


def downgrade() -> None:
    op.drop_index("ix_discussion_pipeline_events_pipeline_run_id", table_name="discussion_pipeline_events")
    op.drop_table("discussion_pipeline_events")
