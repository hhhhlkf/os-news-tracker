"""add runtime WeChat authentication profiles

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-07-23 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wechat_auth_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_name", sa.String(length=100), nullable=False),
        sa.Column("cookie", sa.Text(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="valid", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_name"),
    )
    op.create_index(
        "ix_wechat_auth_profiles_profile_name",
        "wechat_auth_profiles",
        ["profile_name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_wechat_auth_profiles_profile_name", table_name="wechat_auth_profiles")
    op.drop_table("wechat_auth_profiles")
