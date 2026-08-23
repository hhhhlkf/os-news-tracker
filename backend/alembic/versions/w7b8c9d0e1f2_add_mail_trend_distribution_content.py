"""add trend distribution content fields to mail templates and schedules

Revision ID: w7b8c9d0e1f2
Revises: v6a7b8c9d0e1
Create Date: 2026-08-19 14:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "w7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "v6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table_name in ("mail_templates", "mail_schedules"):
        op.add_column(
            table_name,
            sa.Column("content_type", sa.String(length=40), nullable=False, server_default="news"),
        )
        op.add_column(
            table_name,
            sa.Column("trend_identity_template_id", sa.String(length=36), nullable=True),
        )


def downgrade() -> None:
    for table_name in ("mail_schedules", "mail_templates"):
        op.drop_column(table_name, "trend_identity_template_id")
        op.drop_column(table_name, "content_type")
