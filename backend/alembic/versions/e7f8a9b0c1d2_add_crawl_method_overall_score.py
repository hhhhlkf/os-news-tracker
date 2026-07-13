"""add crawl method overall quality score

Revision ID: e7f8a9b0c1d2
Revises: e1f2a3b4c5d6
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("crawl_methods", sa.Column("overall_score", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("crawl_methods", "overall_score")
