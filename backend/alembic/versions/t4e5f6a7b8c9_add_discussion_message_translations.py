"""add persisted discussion mail translations

Revision ID: t4e5f6a7b8c9
Revises: s3d4e5f6a7b8
Create Date: 2026-08-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "t4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "s3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("discussion_messages", sa.Column("translated_body_text", sa.Text(), nullable=True))
    op.add_column("discussion_messages", sa.Column("translation_summary", sa.Text(), nullable=True))
    op.add_column("discussion_messages", sa.Column("translation_phrase", sa.String(length=250), nullable=True))
    op.add_column("discussion_messages", sa.Column("translation_status", sa.String(length=20), nullable=False, server_default="pending"))
    op.add_column("discussion_messages", sa.Column("translation_error", sa.Text(), nullable=True))
    op.add_column("discussion_messages", sa.Column("translated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_discussion_messages_translation_status", "discussion_messages", ["translation_status"])


def downgrade() -> None:
    op.drop_index("ix_discussion_messages_translation_status", table_name="discussion_messages")
    op.drop_column("discussion_messages", "translated_at")
    op.drop_column("discussion_messages", "translation_error")
    op.drop_column("discussion_messages", "translation_status")
    op.drop_column("discussion_messages", "translation_phrase")
    op.drop_column("discussion_messages", "translation_summary")
    op.drop_column("discussion_messages", "translated_body_text")
