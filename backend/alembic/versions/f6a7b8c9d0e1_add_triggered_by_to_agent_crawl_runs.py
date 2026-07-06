"""add_triggered_by_to_agent_crawl_runs

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-22 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `triggered_by` is already added in `e5f6a7b8c9d0`. Keep this revision as a
    # no-op so fresh databases can migrate through the historical chain.
    return None


def downgrade() -> None:
    return None
