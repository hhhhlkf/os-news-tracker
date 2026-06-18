"""add_tag_aliases

Revision ID: d4e5f6a7b8c9
Revises: 7f3e2a9b1c4d, cb7dd0a4d986
Create Date: 2026-06-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = ("7f3e2a9b1c4d", "cb7dd0a4d986")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tag_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("child_tag_id", sa.Integer(), nullable=False),
        sa.Column("parent_tag_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("child_tag_id != parent_tag_id", name="ck_tag_alias_not_self"),
        sa.ForeignKeyConstraint(["child_tag_id"], ["tags.id"]),
        sa.ForeignKeyConstraint(["parent_tag_id"], ["tags.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("child_tag_id", name="uq_tag_alias_child"),
    )
    op.create_index("ix_tag_aliases_child_tag_id", "tag_aliases", ["child_tag_id"])
    op.create_index("ix_tag_aliases_parent_tag_id", "tag_aliases", ["parent_tag_id"])
    op.create_index("ix_tag_aliases_status", "tag_aliases", ["status"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_tag_aliases_status", table_name="tag_aliases")
    op.drop_index("ix_tag_aliases_parent_tag_id", table_name="tag_aliases")
    op.drop_index("ix_tag_aliases_child_tag_id", table_name="tag_aliases")
    op.drop_table("tag_aliases")
