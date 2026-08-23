"""Bound automatic plugin repairs to one run per failed method version.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "crawl_method_runs",
        sa.Column("failure_evidence", sa.JSON(), nullable=True),
    )
    op.create_index(
        "uq_site_discovery_runs_repair_method",
        "site_discovery_runs",
        ["repair_method_id"],
        unique=True,
        postgresql_where=sa.text(
            "trigger_type = 'repair' AND repair_method_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "trigger_type = 'repair' AND repair_method_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_site_discovery_runs_repair_method",
        table_name="site_discovery_runs",
    )
    op.drop_column("crawl_method_runs", "failure_evidence")
