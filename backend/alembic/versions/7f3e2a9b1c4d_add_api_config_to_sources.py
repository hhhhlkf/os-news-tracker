"""add_api_config_to_sources

Revision ID: 7f3e2a9b1c4d
Revises: 1c0d648eb814
Create Date: 2026-06-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7f3e2a9b1c4d"
down_revision: Union[str, Sequence[str], None] = "1c0d648eb814"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("sources", sa.Column("api_config", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("sources", "api_config")
