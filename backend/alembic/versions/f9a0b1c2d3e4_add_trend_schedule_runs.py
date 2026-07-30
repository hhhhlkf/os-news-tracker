"""add trend schedule runs

Revision ID: f9a0b1c2d3e4
Revises: d8e9f0a1b2c3
Create Date: 2026-07-29 21:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9a0b1c2d3e4"
down_revision: Union[str, Sequence[str], None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trend_schedule_runs",
        sa.Column("schedule_run_id", sa.String(length=36), nullable=False),
        sa.Column("schedule_rule", sa.String(length=100), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=False), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=True),
        sa.Column("trend_run_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("stage", sa.String(length=40), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("window_start_date", sa.Date(), nullable=True),
        sa.Column("window_end_date", sa.Date(), nullable=True),
        sa.Column("trend_count", sa.Integer(), nullable=True),
        sa.Column("storyline_candidate_goal", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'skipped', 'reused')",
            name="ck_trend_schedule_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["trend_identity_templates.template_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["trend_run_id"], ["trend_runs.run_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("schedule_run_id"),
        sa.UniqueConstraint("schedule_rule", "scheduled_for", name="uq_trend_schedule_runs_slot"),
    )
    op.create_index("ix_trend_schedule_runs_scheduled_for", "trend_schedule_runs", ["scheduled_for"])
    op.create_index("ix_trend_schedule_runs_status", "trend_schedule_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_trend_schedule_runs_status", table_name="trend_schedule_runs")
    op.drop_index("ix_trend_schedule_runs_scheduled_for", table_name="trend_schedule_runs")
    op.drop_table("trend_schedule_runs")
