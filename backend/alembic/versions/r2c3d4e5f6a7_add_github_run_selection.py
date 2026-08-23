"""add GitHub repository selection to technical runs

Revision ID: r2c3d4e5f6a7
Revises: q1b2c3d4e5f6
Create Date: 2026-08-17
"""

from typing import Sequence, Union

from alembic import op


revision: str = "r2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "q1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # p0a1b2c3d4e5 was already applied in development before this field was
    # introduced.  Keep the repair safe for both fresh and existing databases.
    op.execute(
        "ALTER TABLE discussion_pipeline_runs "
        "ADD COLUMN IF NOT EXISTS github_repository_ids_json JSON"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE discussion_pipeline_runs "
        "DROP COLUMN IF EXISTS github_repository_ids_json"
    )
