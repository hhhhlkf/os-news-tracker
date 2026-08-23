"""add per-site Discovery DSL to plugin migration state

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-08-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("crawl_method_runs") as batch_op:
        batch_op.add_column(sa.Column("migration_recorded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "discovery_method_migrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("source_kind", sa.String(length=30), nullable=False),
        sa.Column("legacy_method_id", sa.Integer(), sa.ForeignKey("crawl_methods.id", ondelete="SET NULL"), nullable=True),
        sa.Column("plugin_method_id", sa.Integer(), sa.ForeignKey("crawl_methods.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False, server_default="shadow_pending"),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(length=200), nullable=True),
        sa.Column("approval_note", sa.Text(), nullable=True),
        sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("formal_success_count_since_cutover", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_formal_status", sa.String(length=20), nullable=True),
        sa.Column("last_formal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_reason", sa.Text(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("legacy_snapshot", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_discovery_method_migrations_state", "discovery_method_migrations", ["state"])
    op.create_index("ix_discovery_method_migrations_domain", "discovery_method_migrations", ["domain"])
    active = sa.text(
        "state IN ('shadow_pending','shadow_running','shadow_passed','blocked','ready','cutover','eligible')"
    )
    active_legacy = sa.text(
        "legacy_method_id IS NOT NULL AND state IN ('shadow_pending','shadow_running','shadow_passed','blocked','ready','cutover','eligible')"
    )
    op.create_index("uq_discovery_method_migrations_active_domain", "discovery_method_migrations", ["domain"], unique=True, postgresql_where=active, sqlite_where=active)
    op.create_index("uq_discovery_method_migrations_active_legacy", "discovery_method_migrations", ["legacy_method_id"], unique=True, postgresql_where=active_legacy, sqlite_where=active_legacy)
    op.create_table(
        "discovery_migration_comparisons",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("migration_id", sa.Integer(), sa.ForeignKey("discovery_method_migrations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("evaluator_passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("comparison_passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("legacy_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("plugin_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.String(length=200), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.String(length=100), nullable=True),
        sa.Column("owner_pid", sa.Integer(), nullable=True),
        sa.Column("owner_start_token", sa.String(length=100), nullable=True),
        sa.Column("owner_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_discovery_migration_comparisons_migration",
        "discovery_migration_comparisons",
        ["migration_id", "id"],
    )
    active_comparison = sa.text("status IN ('queued','running')")
    op.create_index(
        "uq_discovery_migration_comparisons_active",
        "discovery_migration_comparisons",
        ["migration_id"],
        unique=True,
        postgresql_where=active_comparison,
        sqlite_where=active_comparison,
    )


def downgrade() -> None:
    op.drop_index("uq_discovery_migration_comparisons_active", table_name="discovery_migration_comparisons")
    op.drop_index("ix_discovery_migration_comparisons_migration", table_name="discovery_migration_comparisons")
    op.drop_table("discovery_migration_comparisons")
    op.drop_index("ix_discovery_method_migrations_domain", table_name="discovery_method_migrations")
    op.drop_index("ix_discovery_method_migrations_state", table_name="discovery_method_migrations")
    op.drop_index("uq_discovery_method_migrations_active_legacy", table_name="discovery_method_migrations")
    op.drop_index("uq_discovery_method_migrations_active_domain", table_name="discovery_method_migrations")
    op.drop_table("discovery_method_migrations")
    with op.batch_alter_table("crawl_method_runs") as batch_op:
        batch_op.drop_column("migration_recorded_at")
