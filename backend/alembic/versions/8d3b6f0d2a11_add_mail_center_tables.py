"""add_mail_center_tables

Revision ID: 8d3b6f0d2a11
Revises: f6a7b8c9d0e1
Create Date: 2026-07-08 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8d3b6f0d2a11"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mail_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("recipients_json", sa.JSON(), nullable=False),
        sa.Column("filter_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_send_at", sa.DateTime(), nullable=True),
        sa.Column("last_send_status", sa.String(length=50), nullable=True),
        sa.Column("last_send_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "mail_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("mail_templates.id"), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("recipients_json", sa.JSON(), nullable=False),
        sa.Column("filter_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("frequency", sa.String(length=50), nullable=False, server_default="daily"),
        sa.Column("send_time", sa.String(length=10), nullable=False, server_default="09:00"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_result_status", sa.String(length=50), nullable=True),
        sa.Column("last_result_count", sa.Integer(), nullable=True),
        sa.Column("last_sent_marker_date", sa.String(length=10), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("patrol_status", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_mail_schedules_template_id", "mail_schedules", ["template_id"])

    op.create_table(
        "mail_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("schedule_id", sa.Integer(), sa.ForeignKey("mail_schedules.id"), nullable=True),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("mail_templates.id"), nullable=True),
        sa.Column("trigger_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("recipients_json", sa.JSON(), nullable=False),
        sa.Column("filter_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_mail_deliveries_schedule_id", "mail_deliveries", ["schedule_id"])
    op.create_index("ix_mail_deliveries_template_id", "mail_deliveries", ["template_id"])


def downgrade() -> None:
    op.drop_index("ix_mail_deliveries_template_id", table_name="mail_deliveries")
    op.drop_index("ix_mail_deliveries_schedule_id", table_name="mail_deliveries")
    op.drop_table("mail_deliveries")
    op.drop_index("ix_mail_schedules_template_id", table_name="mail_schedules")
    op.drop_table("mail_schedules")
    op.drop_table("mail_templates")
