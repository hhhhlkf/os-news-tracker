"""add relative trend window settings

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-07-29 13:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trend_settings",
        sa.Column("window_mode", sa.String(length=20), nullable=False, server_default="relative"),
    )
    op.add_column(
        "trend_settings",
        sa.Column("relative_window_unit", sa.String(length=10), nullable=False, server_default="week"),
    )
    op.add_column(
        "trend_settings",
        sa.Column("relative_window_value", sa.Integer(), nullable=False, server_default="4"),
    )
    op.execute(
        "UPDATE trend_settings SET window_mode = 'date_range' "
        "WHERE window_start_date IS NOT NULL AND window_end_date IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("trend_settings", "relative_window_value")
    op.drop_column("trend_settings", "relative_window_unit")
    op.drop_column("trend_settings", "window_mode")
