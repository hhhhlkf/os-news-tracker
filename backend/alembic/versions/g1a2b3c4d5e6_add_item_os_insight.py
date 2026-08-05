"""add items.os_insight

Revision ID: g1a2b3c4d5e6
Revises: f9a0b1c2d3e4
Create Date: 2026-08-05 11:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "f9a0b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("os_insight", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("items", "os_insight")
