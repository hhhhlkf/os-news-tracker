"""add discussion schedule config

Revision ID: n8a9b0c1d2e3
Revises: m7f8a9b0c1d2
Create Date: 2026-08-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "n8a9b0c1d2e3"
down_revision: Union[str, Sequence[str], None] = "m7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "discussion_pipeline_runs",
        sa.Column("trigger_type", sa.String(length=30), nullable=False, server_default="manual"),
    )
    op.create_table(
        "discussion_schedule_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("run_time", sa.String(length=10), nullable=False, server_default="08:00"),
        sa.Column("patrol_interval_hours", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_status", sa.String(length=20), nullable=True),
        sa.Column("last_success_date", sa.String(length=10), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("discussion_schedule_config")
    op.drop_column("discussion_pipeline_runs", "trigger_type")
