"""add unique constraint to item_sources and clean up duplicates

Revision ID: b8f1a2c3d4e5
Revises: ca363b702936
Create Date: 2026-06-11 18:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8f1a2c3d4e5"
down_revision: Union[str, None] = "ca363b702936"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Delete historical duplicate ItemSource rows, keeping the one with
    # the smallest id for each (item_id, source_id, url) group.
    #
    # This works on both PostgreSQL and SQLite.
    op.execute(
        sa.text(
            """
            DELETE FROM item_sources
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM item_sources
                GROUP BY item_id, source_id, url
            )
            """
        )
    )

    # Step 2: Add unique constraint
    op.create_unique_constraint(
        "uq_item_source_url", "item_sources", ["item_id", "source_id", "url"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_item_source_url", "item_sources", type_="unique")
