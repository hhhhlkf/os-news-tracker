"""add versioned card embeddings and candidate clusters

Revision ID: a5b6c7d8e9f0
Revises: a4b5c6d7e8f9
Create Date: 2026-07-29 21:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news_card_embeddings",
        sa.Column("card_id", sa.String(length=36), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("model_version", sa.String(length=100), nullable=True),
        sa.Column("embedding_version", sa.String(length=200), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("normalized", sa.Boolean(), nullable=False),
        sa.Column("generated_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["card_id"], ["news_explanation_cards.card_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("card_id"),
    )
    op.create_index("ix_news_card_embeddings_embedding_version", "news_card_embeddings", ["embedding_version"])
    op.create_table(
        "trend_candidate_clusters",
        sa.Column("candidate_cluster_id", sa.String(length=36), nullable=False),
        sa.Column("embedding_version", sa.String(length=200), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("cohesion_score", sa.Float(), nullable=False),
        sa.Column("max_cluster_size", sa.Integer(), nullable=False),
        sa.Column("window_start_date", sa.Date(), nullable=False),
        sa.Column("window_end_date", sa.Date(), nullable=False),
        sa.Column("generated_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("candidate_cluster_id"),
    )
    op.create_index("ix_trend_candidate_clusters_embedding_version", "trend_candidate_clusters", ["embedding_version"])
    op.create_table(
        "trend_candidate_cluster_members",
        sa.Column("candidate_cluster_id", sa.String(length=36), nullable=False),
        sa.Column("card_id", sa.String(length=36), nullable=False),
        sa.Column("member_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_cluster_id"], ["trend_candidate_clusters.candidate_cluster_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["card_id"], ["news_explanation_cards.card_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("candidate_cluster_id", "card_id"),
    )


def downgrade() -> None:
    op.drop_table("trend_candidate_cluster_members")
    op.drop_index("ix_trend_candidate_clusters_embedding_version", table_name="trend_candidate_clusters")
    op.drop_table("trend_candidate_clusters")
    op.drop_index("ix_news_card_embeddings_embedding_version", table_name="news_card_embeddings")
    op.drop_table("news_card_embeddings")
