"""add discussion pipeline timeline events

Revision ID: k5d6e7f8a9b0
Revises: j4c5d6e7f8a9
Create Date: 2026-08-16 14:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "k5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "j4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discussion_pipeline_runs",
        sa.Column("events_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    )
    op.alter_column("discussion_pipeline_runs", "events_json", server_default=None)


def downgrade() -> None:
    op.drop_column("discussion_pipeline_runs", "events_json")
