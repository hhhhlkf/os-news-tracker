"""add discussion source review status

Revision ID: m7f8a9b0c1d2
Revises: l6e7f8a9b0c1
Create Date: 2026-08-16 15:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "m7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "l6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discussion_source_rules",
        sa.Column("review_status", sa.String(20), nullable=False, server_default="approved"),
    )
    op.create_index("ix_discussion_source_rules_review_status", "discussion_source_rules", ["review_status"])
    op.alter_column("discussion_source_rules", "review_status", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_discussion_source_rules_review_status", table_name="discussion_source_rules")
    op.drop_column("discussion_source_rules", "review_status")
