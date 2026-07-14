"""add crawl method review workflow

Revision ID: e9f0a1b2c3d4
Revises: e8f9a0b1c2d3
Create Date: 2026-07-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = "e8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("crawl_methods", sa.Column("review_status", sa.String(length=20), nullable=False, server_default="approved"))
    op.add_column("crawl_methods", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("crawl_methods", sa.Column("reviewed_by", sa.String(length=200), nullable=True))
    op.add_column("crawl_methods", sa.Column("review_note", sa.Text(), nullable=True))
    op.create_index(op.f("ix_crawl_methods_review_status"), "crawl_methods", ["review_status"], unique=False)
    op.alter_column("crawl_methods", "review_status", server_default=None)

    op.create_table(
        "crawl_method_review_reminder_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("interval_minutes", sa.Integer(), nullable=False, server_default="1440"),
        sa.Column("recipients_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result_status", sa.String(length=50), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("crawl_method_review_reminder_configs")
    op.drop_index(op.f("ix_crawl_methods_review_status"), table_name="crawl_methods")
    op.drop_column("crawl_methods", "review_note")
    op.drop_column("crawl_methods", "reviewed_by")
    op.drop_column("crawl_methods", "reviewed_at")
    op.drop_column("crawl_methods", "review_status")
