"""add per-run discovery log viewer capability

Revision ID: h9i0j1k2l3m4
Revises: g6h7i8j9k0l1
Create Date: 2026-08-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h9i0j1k2l3m4"
down_revision: Union[str, Sequence[str], None] = "g6h7i8j9k0l1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "site_discovery_runs",
        sa.Column("viewer_token_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_site_discovery_runs_viewer_token_hash",
        "site_discovery_runs",
        ["viewer_token_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_site_discovery_runs_viewer_token_hash", table_name="site_discovery_runs")
    op.drop_column("site_discovery_runs", "viewer_token_hash")
