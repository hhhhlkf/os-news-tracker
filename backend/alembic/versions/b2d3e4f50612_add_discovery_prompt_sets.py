"""add discovery_prompt_sets table

Revision ID: b2d3e4f50612
Revises: a1c2e3f40501
Create Date: 2026-07-09 09:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2d3e4f50612"
down_revision: Union[str, Sequence[str], None] = "a1c2e3f40501"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "discovery_prompt_sets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("prompts", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_discovery_prompt_sets_is_active", "discovery_prompt_sets", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_discovery_prompt_sets_is_active", table_name="discovery_prompt_sets")
    op.drop_table("discovery_prompt_sets")
