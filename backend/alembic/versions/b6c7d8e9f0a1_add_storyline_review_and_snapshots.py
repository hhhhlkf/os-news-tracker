"""add storyline review, members, and snapshots

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-07-29 23:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, Sequence[str], None] = "a5b6c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trend_storyline_reviews",
        sa.Column("review_id", sa.String(length=36), nullable=False),
        sa.Column("review_key", sa.String(length=64), nullable=False),
        sa.Column("candidate_cluster_id", sa.String(length=36), nullable=True),
        sa.Column("embedding_version", sa.String(length=200), nullable=False),
        sa.Column("window_start_date", sa.Date(), nullable=False),
        sa.Column("window_end_date", sa.Date(), nullable=False),
        sa.Column("member_card_ids", sa.JSON(), nullable=False),
        sa.Column("removed_card_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=True),
        sa.Column("agent_review", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'running', 'accepted', 'split', 'rejected', 'failed')", name="ck_trend_storyline_reviews_status"),
        sa.CheckConstraint("decision IS NULL OR decision IN ('accept', 'split', 'reject')", name="ck_trend_storyline_reviews_decision"),
        sa.PrimaryKeyConstraint("review_id"),
        sa.UniqueConstraint("review_key"),
    )
    op.create_index("ix_trend_storyline_reviews_review_key", "trend_storyline_reviews", ["review_key"])
    op.create_index("ix_trend_storyline_reviews_embedding_version", "trend_storyline_reviews", ["embedding_version"])
    op.create_table(
        "storylines",
        sa.Column("storyline_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("overall_start_date", sa.Date(), nullable=False),
        sa.Column("overall_end_date", sa.Date(), nullable=False),
        sa.Column("window_start_date", sa.Date(), nullable=True),
        sa.Column("window_end_date", sa.Date(), nullable=True),
        sa.Column("cluster_threshold", sa.Float(), nullable=False),
        sa.Column("cohesion_score", sa.Float(), nullable=False),
        sa.Column("overall_influence_score", sa.Float(), nullable=False),
        sa.Column("window_influence_score", sa.Float(), nullable=True),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("agent_review", sa.Text(), nullable=False),
        sa.Column("review_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("last_member_at", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("decision IN ('accept', 'split')", name="ck_storylines_decision"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_storylines_status"),
        sa.ForeignKeyConstraint(["review_id"], ["trend_storyline_reviews.review_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("storyline_id"),
    )
    op.create_index("ix_storylines_review_id", "storylines", ["review_id"])
    op.create_index("ix_storylines_status", "storylines", ["status"])
    op.create_index("ix_storylines_last_member_at", "storylines", ["last_member_at"])
    op.create_table(
        "storyline_members",
        sa.Column("storyline_id", sa.String(length=36), nullable=False),
        sa.Column("card_id", sa.String(length=36), nullable=False),
        sa.Column("at", sa.Date(), nullable=False),
        sa.Column("membership", sa.String(length=20), nullable=False),
        sa.CheckConstraint("membership IN ('core', 'supporting', 'duplicate')", name="ck_storyline_members_membership"),
        sa.ForeignKeyConstraint(["storyline_id"], ["storylines.storyline_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["card_id"], ["news_explanation_cards.card_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("storyline_id", "card_id"),
    )
    op.create_index("ix_storyline_members_at", "storyline_members", ["at"])
    op.create_table(
        "storyline_snapshots",
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("storyline_id", sa.String(length=36), nullable=False),
        sa.Column("review_id", sa.String(length=36), nullable=False),
        sa.Column("at", sa.Date(), nullable=False, server_default=sa.func.current_date()),
        sa.Column("time_start_date", sa.Date(), nullable=False),
        sa.Column("time_end_date", sa.Date(), nullable=False),
        sa.Column("card_ids", sa.JSON(), nullable=False),
        sa.Column("memberships", sa.JSON(), nullable=False),
        sa.Column("influence_score", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["storyline_id"], ["storylines.storyline_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["review_id"], ["trend_storyline_reviews.review_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("snapshot_id"),
    )
    op.create_index("ix_storyline_snapshots_storyline_id", "storyline_snapshots", ["storyline_id"])
    op.create_index("ix_storyline_snapshots_review_id", "storyline_snapshots", ["review_id"])


def downgrade() -> None:
    op.drop_index("ix_storyline_snapshots_review_id", table_name="storyline_snapshots")
    op.drop_index("ix_storyline_snapshots_storyline_id", table_name="storyline_snapshots")
    op.drop_table("storyline_snapshots")
    op.drop_index("ix_storyline_members_at", table_name="storyline_members")
    op.drop_table("storyline_members")
    op.drop_index("ix_storylines_last_member_at", table_name="storylines")
    op.drop_index("ix_storylines_status", table_name="storylines")
    op.drop_index("ix_storylines_review_id", table_name="storylines")
    op.drop_table("storylines")
    op.drop_index("ix_trend_storyline_reviews_embedding_version", table_name="trend_storyline_reviews")
    op.drop_index("ix_trend_storyline_reviews_review_key", table_name="trend_storyline_reviews")
    op.drop_table("trend_storyline_reviews")
