"""add mail notice config and notice snapshots

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-07-23 19:50:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mail_notice_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("doc_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("website_url", sa.String(length=2048), nullable=False, server_default=""),
        sa.Column("include_on_send", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("include_on_template", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.add_column("mail_templates", sa.Column("notice_json", sa.JSON(), nullable=True))
    op.add_column("mail_schedules", sa.Column("notice_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("mail_schedules", "notice_json")
    op.drop_column("mail_templates", "notice_json")
    op.drop_table("mail_notice_config")
