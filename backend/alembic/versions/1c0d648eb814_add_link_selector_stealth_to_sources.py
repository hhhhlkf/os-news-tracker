"""add_link_selector_stealth_to_sources

Revision ID: 1c0d648eb814
Revises: b8f1a2c3d4e5
Create Date: 2026-06-12 16:45:56.040770

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1c0d648eb814'
down_revision: Union[str, Sequence[str], None] = 'b8f1a2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("sources", sa.Column("link_selector", sa.String(500), nullable=True))
    op.add_column("sources", sa.Column("title_selector", sa.String(500), nullable=True))
    op.add_column("sources", sa.Column("date_selector", sa.String(500), nullable=True))
    op.add_column("sources", sa.Column("stealth", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("sources", "stealth")
    op.drop_column("sources", "date_selector")
    op.drop_column("sources", "title_selector")
    op.drop_column("sources", "link_selector")
