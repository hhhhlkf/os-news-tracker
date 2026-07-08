"""morning_crawl_run_methods.method_id nullable + ON DELETE SET NULL

Revision ID: a1c2e3f40501
Revises: 9b4c1d2e3f40
Create Date: 2026-07-08 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1c2e3f40501"
down_revision: Union[str, Sequence[str], None] = "9b4c1d2e3f40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK_NAME = "morning_crawl_run_methods_method_id_fkey"


def upgrade() -> None:
    op.alter_column(
        "morning_crawl_run_methods",
        "method_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.drop_constraint(_FK_NAME, "morning_crawl_run_methods", type_="foreignkey")
    op.create_foreign_key(
        _FK_NAME,
        "morning_crawl_run_methods",
        "crawl_methods",
        ["method_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(_FK_NAME, "morning_crawl_run_methods", type_="foreignkey")
    op.create_foreign_key(
        _FK_NAME,
        "morning_crawl_run_methods",
        "crawl_methods",
        ["method_id"],
        ["id"],
    )
    op.alter_column(
        "morning_crawl_run_methods",
        "method_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
