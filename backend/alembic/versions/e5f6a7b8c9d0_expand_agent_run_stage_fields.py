"""expand_agent_run_stage_fields

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "agent_crawl_runs",
        sa.Column(
            "current_stage",
            sa.String(length=20),
            nullable=False,
            server_default="planning",
        ),
    )
    op.add_column(
        "agent_crawl_runs",
        sa.Column("stage_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "agent_crawl_runs",
        sa.Column("triggered_by", sa.String(length=100), nullable=True),
    )
    op.alter_column("agent_crawl_runs", "current_stage", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("agent_crawl_runs", "triggered_by")
    op.drop_column("agent_crawl_runs", "stage_message")
    op.drop_column("agent_crawl_runs", "current_stage")
