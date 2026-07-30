"""add trend runs, results, and result items

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-07-29 20:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, Sequence[str], None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trend_runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("window_start_date", sa.Date(), nullable=False),
        sa.Column("window_end_date", sa.Date(), nullable=False),
        sa.Column("trend_count", sa.Integer(), nullable=False),
        sa.Column("storyline_candidate_goal", sa.Integer(), nullable=False),
        sa.Column("embedding_version", sa.String(length=200), nullable=False),
        sa.Column("card_prompt_version", sa.String(length=100), nullable=False),
        sa.Column("trend_prompt_version", sa.String(length=100), nullable=False),
        sa.Column("model_version", sa.String(length=200), nullable=False),
        sa.Column("candidate_storyline_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_trend_runs_status",
        ),
        sa.CheckConstraint("trend_count >= 1", name="ck_trend_runs_trend_count"),
        sa.CheckConstraint(
            "storyline_candidate_goal >= 1",
            name="ck_trend_runs_storyline_candidate_goal",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["trend_identity_templates.template_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_trend_runs_template_id", "trend_runs", ["template_id"])
    op.create_index("ix_trend_runs_status", "trend_runs", ["status"])

    op.create_table(
        "trend_results",
        sa.Column("result_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("storyline_id", sa.String(length=36), nullable=False),
        sa.Column("overall_start_date", sa.Date(), nullable=False),
        sa.Column("overall_end_date", sa.Date(), nullable=False),
        sa.Column("window_start_date", sa.Date(), nullable=False),
        sa.Column("window_end_date", sa.Date(), nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("window_score", sa.Float(), nullable=False),
        sa.Column("template_relevance_score", sa.Float(), nullable=False),
        sa.Column("trend_rank_score", sa.Float(), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("topic", sa.String(length=300), nullable=True),
        sa.Column("trend_summary", sa.Text(), nullable=True),
        sa.Column("agent_review", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "category IN ('emerging_trend', 'hot_event', 'periodic_activity', "
            "'attention_declining', 'unverified_change')",
            name="ck_trend_results_category",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["trend_runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["storyline_id"], ["storylines.storyline_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("result_id"),
        sa.UniqueConstraint("run_id", "storyline_id", name="uq_trend_results_run_storyline"),
    )
    op.create_index("ix_trend_results_run_id", "trend_results", ["run_id"])
    op.create_index("ix_trend_results_storyline_id", "trend_results", ["storyline_id"])

    op.create_table(
        "trend_result_items",
        sa.Column("result_id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["result_id"], ["trend_results.result_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("result_id", "item_id"),
    )


def downgrade() -> None:
    op.drop_table("trend_result_items")
    op.drop_index("ix_trend_results_storyline_id", table_name="trend_results")
    op.drop_index("ix_trend_results_run_id", table_name="trend_results")
    op.drop_table("trend_results")
    op.drop_index("ix_trend_runs_status", table_name="trend_runs")
    op.drop_index("ix_trend_runs_template_id", table_name="trend_runs")
    op.drop_table("trend_runs")
