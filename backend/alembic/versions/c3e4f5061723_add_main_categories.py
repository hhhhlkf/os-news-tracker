"""add main_categories table + seed defaults

Revision ID: c3e4f5061723
Revises: b2d3e4f50612
Create Date: 2026-07-09 12:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3e4f5061723"
down_revision: Union[str, Sequence[str], None] = "b2d3e4f50612"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULTS = [
    "OS跟踪来源",
    "友商产品信息",
    "软件包适配",
    "OS性能发展",
    "司内AI工具",
]


def upgrade() -> None:
    table = op.create_table(
        "main_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_main_category_name"),
    )
    op.bulk_insert(
        table,
        [{"name": name, "sort_order": idx} for idx, name in enumerate(_DEFAULTS)],
    )


def downgrade() -> None:
    op.drop_table("main_categories")
