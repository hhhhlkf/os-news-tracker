"""add_morning_crawl_tables

Revision ID: 9b4c1d2e3f40
Revises: 8d3b6f0d2a11
Create Date: 2026-07-08 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9b4c1d2e3f40"
down_revision: Union[str, Sequence[str], None] = "8d3b6f0d2a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "morning_crawl_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("run_time", sa.String(length=10), nullable=False, server_default="07:00"),
        sa.Column("frequency", sa.String(length=20), nullable=False, server_default="daily"),
        sa.Column("lookback_window", sa.String(length=20), nullable=False, server_default="24h"),
        sa.Column("patrol_interval_hours", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_status", sa.String(length=20), nullable=True),
        sa.Column("last_success_date", sa.String(length=10), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "morning_crawl_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trigger_type", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("run_date", sa.String(length=10), nullable=True),
        sa.Column("total_methods", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_methods", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_methods", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stored_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )

    op.create_table(
        "morning_crawl_run_methods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("morning_crawl_runs.id"), nullable=False),
        sa.Column("method_id", sa.Integer(), sa.ForeignKey("crawl_methods.id"), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ok"),
        sa.Column("discovered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stored_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_morning_crawl_run_methods_run_id", "morning_crawl_run_methods", ["run_id"])
    op.create_index("ix_morning_crawl_run_methods_method_id", "morning_crawl_run_methods", ["method_id"])


def downgrade() -> None:
    op.drop_index("ix_morning_crawl_run_methods_method_id", table_name="morning_crawl_run_methods")
    op.drop_index("ix_morning_crawl_run_methods_run_id", table_name="morning_crawl_run_methods")
    op.drop_table("morning_crawl_run_methods")
    op.drop_table("morning_crawl_runs")
    op.drop_table("morning_crawl_config")
