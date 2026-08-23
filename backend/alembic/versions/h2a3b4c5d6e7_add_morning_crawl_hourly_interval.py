"""add hourly interval to morning crawl config

Revision ID: h2a3b4c5d6e7
Revises: g1a2b3c4d5e6
Create Date: 2026-08-13 14:20:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "h2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "g1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("morning_crawl_config", sa.Column("interval_hours", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    op.drop_column("morning_crawl_config", "interval_hours")
