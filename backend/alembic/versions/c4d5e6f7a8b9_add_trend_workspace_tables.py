"""add trend workspace tables

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-07-29 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trend_identity_templates",
        sa.Column("template_id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("identity_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Date(), nullable=False, server_default=sa.text("CURRENT_DATE")),
    )
    op.create_table(
        "trend_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("window_start_date", sa.Date(), nullable=True),
        sa.Column("window_end_date", sa.Date(), nullable=True),
        sa.Column("trend_count", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("storyline_candidate_goal", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("trigger_mode", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("schedule_rule", sa.String(length=100), nullable=True),
        sa.Column("scheduled_template_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.Date(), nullable=False, server_default=sa.text("CURRENT_DATE")),
        sa.Column("updated_at", sa.Date(), nullable=False, server_default=sa.text("CURRENT_DATE")),
        sa.CheckConstraint("id = 1", name="ck_trend_settings_singleton"),
        sa.ForeignKeyConstraint(
            ["scheduled_template_id"],
            ["trend_identity_templates.template_id"],
            ondelete="SET NULL",
        ),
    )


def downgrade() -> None:
    op.drop_table("trend_settings")
    op.drop_table("trend_identity_templates")
