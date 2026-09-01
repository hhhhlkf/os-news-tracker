"""add batch queue delete request marker

Revision ID: queue_delete_requested_20260827
Revises: a1b2c3d4e5f7
Create Date: 2026-08-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "queue_delete_requested_20260827"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("discovery_queue_items")}
    if "delete_requested" not in columns:
        op.add_column(
            "discovery_queue_items",
            sa.Column("delete_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    # The preceding migration's current schema already includes this column.
    # This remediation revision only brings databases created by an earlier
    # version of that migration into the same shape, so downgrade is a no-op.
    pass
