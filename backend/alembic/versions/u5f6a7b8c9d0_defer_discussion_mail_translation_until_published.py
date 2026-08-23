"""defer discussion mail translation until publication

Revision ID: u5f6a7b8c9d0
Revises: t4e5f6a7b8c9
Create Date: 2026-08-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "u5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "t4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "discussion_messages",
        "translation_status",
        existing_type=sa.String(length=20),
        server_default="deferred",
    )


def downgrade() -> None:
    op.alter_column(
        "discussion_messages",
        "translation_status",
        existing_type=sa.String(length=20),
        server_default="pending",
    )
