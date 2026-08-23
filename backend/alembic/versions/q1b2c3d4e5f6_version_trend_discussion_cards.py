"""version fact cards by discussion content revision

Revision ID: q1b2c3d4e5f6
Revises: p0a1b2c3d4e5
Create Date: 2026-08-16
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "q1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "p0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.add_column("news_explanation_cards", sa.Column("item_kind", sa.String(length=20), nullable=False, server_default="news"))
    op.add_column("news_explanation_cards", sa.Column("input_content_revision", sa.Integer(), nullable=False, server_default="1"))

def downgrade() -> None:
    op.drop_column("news_explanation_cards", "input_content_revision")
    op.drop_column("news_explanation_cards", "item_kind")
