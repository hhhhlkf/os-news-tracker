"""remove unused news explanation card title

Revision ID: a4b5c6d7e8f9
Revises: a3b4c5d6e7f8
Create Date: 2026-07-29 20:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("news_explanation_cards", "card_title")


def downgrade() -> None:
    op.add_column(
        "news_explanation_cards",
        sa.Column("card_title", sa.String(length=100), nullable=True),
    )
