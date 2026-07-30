"""add news explanation cards

Revision ID: a0b1c2d3e4f5
Revises: f8a9b0c1d2e3
Create Date: 2026-07-29 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, Sequence[str], None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news_explanation_cards",
        sa.Column("card_id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("news_actor", sa.String(length=100), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=True),
        sa.Column("result", sa.String(length=100), nullable=True),
        sa.Column("potential_impact", sa.String(length=100), nullable=True),
        sa.Column("cause", sa.String(length=100), nullable=True),
        sa.Column("at", sa.Date(), nullable=False),
        sa.Column("card_prompt_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('ready', 'failed', 'skipped')",
            name="ck_news_explanation_cards_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_news_explanation_cards_attempt_count",
        ),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("card_id"),
    )
    op.create_index(
        op.f("ix_news_explanation_cards_at"),
        "news_explanation_cards",
        ["at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_news_explanation_cards_item_id"),
        "news_explanation_cards",
        ["item_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_news_explanation_cards_item_id"), table_name="news_explanation_cards")
    op.drop_index(op.f("ix_news_explanation_cards_at"), table_name="news_explanation_cards")
    op.drop_table("news_explanation_cards")
