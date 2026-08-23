"""backfill discussion run trigger type

Revision ID: o9b0c1d2e3f4
Revises: n8a9b0c1d2e3
Create Date: 2026-08-16
"""

from typing import Sequence, Union

from alembic import op


revision: str = "o9b0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "n8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The prior revision was applied in development before this field was
    # added to its source file. Keep this idempotent for both histories.
    op.execute(
        "ALTER TABLE discussion_pipeline_runs "
        "ADD COLUMN IF NOT EXISTS trigger_type VARCHAR(30) NOT NULL DEFAULT 'manual'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE discussion_pipeline_runs DROP COLUMN IF EXISTS trigger_type")
