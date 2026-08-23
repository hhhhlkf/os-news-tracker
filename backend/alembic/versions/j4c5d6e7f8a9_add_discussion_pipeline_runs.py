"""add persisted discussion pipeline runs

Revision ID: j4c5d6e7f8a9
Revises: i3b4c5d6e7f8
Create Date: 2026-08-16 14:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "j4c5d6e7f8a9"
down_revision: Union[str, Sequence[str], None] = "i3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discussion_pipeline_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("source_ids_json", sa.JSON(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_discussion_pipeline_runs_status", "discussion_pipeline_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_discussion_pipeline_runs_status", table_name="discussion_pipeline_runs")
    op.drop_table("discussion_pipeline_runs")
