"""add exact llm usage events

Revision ID: f1a2b3c4d5e6
Revises: e9f0a1b2c3d4
Create Date: 2026-07-23 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("context_type", sa.String(length=20), nullable=False),
        sa.Column("trigger_type", sa.String(length=30), nullable=True),
        sa.Column("stage", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("discovery_run_id", sa.Integer(), nullable=True),
        sa.Column("crawl_method_run_id", sa.Integer(), nullable=True),
        sa.Column("morning_crawl_run_id", sa.Integer(), nullable=True),
        sa.Column("morning_crawl_run_method_id", sa.Integer(), nullable=True),
        sa.Column("method_id", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["crawl_method_run_id"], ["crawl_method_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["discovery_run_id"], ["site_discovery_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["method_id"], ["crawl_methods.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["morning_crawl_run_id"], ["morning_crawl_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["morning_crawl_run_method_id"], ["morning_crawl_run_methods.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_llm_usage_events_occurred_context",
        "llm_usage_events",
        ["occurred_at", "context_type"],
        unique=False,
    )
    op.create_index(
        "ix_llm_usage_events_discovery_run", "llm_usage_events", ["discovery_run_id"], unique=False
    )
    op.create_index(
        "ix_llm_usage_events_crawl_method_run", "llm_usage_events", ["crawl_method_run_id"], unique=False
    )
    op.create_index(
        "ix_llm_usage_events_morning_run", "llm_usage_events", ["morning_crawl_run_id"], unique=False
    )
    op.create_index(
        "ix_llm_usage_events_morning_method",
        "llm_usage_events",
        ["morning_crawl_run_method_id"],
        unique=False,
    )
    op.create_index("ix_llm_usage_events_method", "llm_usage_events", ["method_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_llm_usage_events_method", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_morning_method", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_morning_run", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_crawl_method_run", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_discovery_run", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_occurred_context", table_name="llm_usage_events")
    op.drop_table("llm_usage_events")
