"""add formal fetch cancellation broker fields

Revision ID: b2c3d4e5f6a7
Revises: a1f2e3d4c5b6
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1f2e3d4c5b6"
branch_labels = None
depends_on = None

def upgrade() -> None:
    with op.batch_alter_table("crawl_method_runs") as batch:
        batch.add_column(sa.Column("owner_id", sa.String(200), nullable=True))
        batch.add_column(sa.Column("owner_pid", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("owner_start_token", sa.String(100), nullable=True))
        batch.add_column(sa.Column("owner_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("sandbox_job_id", sa.String(200), nullable=True))
        batch.add_column(sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("cancel_acknowledged_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_crawl_method_runs_owner_id", ["owner_id"], unique=False)

def downgrade() -> None:
    with op.batch_alter_table("crawl_method_runs") as batch:
        batch.drop_index("ix_crawl_method_runs_owner_id")
        batch.drop_column("cancel_acknowledged_at")
        batch.drop_column("cancel_requested_at")
        batch.drop_column("sandbox_job_id")
        batch.drop_column("owner_heartbeat_at")
        batch.drop_column("owner_start_token")
        batch.drop_column("owner_pid")
        batch.drop_column("owner_id")
