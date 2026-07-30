"""add Chinese title to news explanation cards

Revision ID: a3b4c5d6e7f8
Revises: a0b1c2d3e4f5
Create Date: 2026-07-29 20:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "a0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "news_explanation_cards",
        sa.Column("card_title", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("news_explanation_cards", "card_title")
