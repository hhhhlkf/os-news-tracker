"""add crawl method quality audit fields

Revision ID: e1f2a3b4c5d6
Revises: d7e8f9a0b1c2
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("crawl_methods", sa.Column("quality_score", sa.Integer(), nullable=True))
    op.add_column("crawl_methods", sa.Column("quality_grade", sa.String(length=2), nullable=True))
    op.add_column("crawl_methods", sa.Column("quality_reason", sa.Text(), nullable=True))
    op.add_column("crawl_methods", sa.Column("quality_sample_count", sa.Integer(), nullable=True))
    op.add_column("crawl_methods", sa.Column("density_score", sa.Integer(), nullable=True))
    op.add_column("crawl_methods", sa.Column("density_daily_avg", sa.Float(), nullable=True))
    op.add_column("crawl_methods", sa.Column("density_weekly_avg", sa.Float(), nullable=True))
    op.add_column("crawl_methods", sa.Column("quality_audit_status", sa.String(length=20), nullable=True))
    op.add_column("crawl_methods", sa.Column("quality_audited_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("crawl_methods", "quality_audited_at")
    op.drop_column("crawl_methods", "quality_audit_status")
    op.drop_column("crawl_methods", "density_weekly_avg")
    op.drop_column("crawl_methods", "density_daily_avg")
    op.drop_column("crawl_methods", "density_score")
    op.drop_column("crawl_methods", "quality_sample_count")
    op.drop_column("crawl_methods", "quality_reason")
    op.drop_column("crawl_methods", "quality_grade")
    op.drop_column("crawl_methods", "quality_score")
