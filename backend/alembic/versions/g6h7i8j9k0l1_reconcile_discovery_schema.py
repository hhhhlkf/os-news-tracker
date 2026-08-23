"""reconcile Discovery schema changed during the staged refactor

Revision ID: g6h7i8j9k0l1
Revises: f5a6b7c8d9e0
Create Date: 2026-08-22

Some development databases applied early versions of the staged Discovery
migrations before later phases extended those same tables.  The Alembic version
therefore reached ``f5a6b7c8d9e0`` while a subset of the final columns and
partial indexes was absent.  This migration is deliberately idempotent so both
those databases and clean installations converge on the same schema.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "g6h7i8j9k0l1"
down_revision: str | Sequence[str] | None = "f5a6b7c8d9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes(table)
        if item.get("name")
    }


def _unique_constraints(table: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_unique_constraints(table)
        if item.get("name")
    }


def _add_missing_columns(table: str, columns: tuple[sa.Column, ...]) -> None:
    existing = _columns(table)
    missing = tuple(column for column in columns if column.name not in existing)
    if not missing:
        return
    with op.batch_alter_table(table) as batch_op:
        for column in missing:
            batch_op.add_column(column)


def _drop_unique_if_present(table: str, name: str) -> None:
    if name not in _unique_constraints(table):
        return
    with op.batch_alter_table(table) as batch_op:
        batch_op.drop_constraint(name, type_="unique")


def _drop_index_if_present(table: str, name: str) -> None:
    if name in _indexes(table):
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    _add_missing_columns(
        "site_discovery_runs",
        (
            sa.Column(
                "event_sequence",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        ),
    )
    site_indexes = _indexes("site_discovery_runs")
    if "ix_site_discovery_runs_status" not in site_indexes:
        op.create_index(
            "ix_site_discovery_runs_status",
            "site_discovery_runs",
            ["status"],
        )
    if "ix_site_discovery_runs_trigger_status" not in site_indexes:
        op.create_index(
            "ix_site_discovery_runs_trigger_status",
            "site_discovery_runs",
            ["trigger_type", "status"],
        )
    if "uq_site_discovery_runs_active_resume_checkpoint" not in site_indexes:
        active_resume = sa.text(
            "trigger_type = 'resume' AND status IN ('queued', 'running', 'repairing')"
        )
        op.create_index(
            "uq_site_discovery_runs_active_resume_checkpoint",
            "site_discovery_runs",
            ["checkpoint_path"],
            unique=True,
            postgresql_where=active_resume,
            sqlite_where=active_resume,
        )

    _add_missing_columns(
        "crawl_method_runs",
        (
            sa.Column("failure_evidence", sa.JSON(), nullable=True),
            sa.Column("owner_pid", sa.Integer(), nullable=True),
            sa.Column("owner_start_token", sa.String(length=100), nullable=True),
            sa.Column("owner_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("migration_recorded_at", sa.DateTime(timezone=True), nullable=True),
        ),
    )

    _drop_unique_if_present(
        "discovery_method_migrations",
        "uq_discovery_migration_legacy_method",
    )
    _drop_unique_if_present(
        "discovery_method_migrations",
        "uq_discovery_migration_plugin_method",
    )
    migration_indexes = _indexes("discovery_method_migrations")
    active_migration = sa.text(
        "state IN ('shadow_pending','shadow_running','shadow_passed','blocked',"
        "'ready','cutover','eligible')"
    )
    active_legacy = sa.text(
        "legacy_method_id IS NOT NULL AND "
        "state IN ('shadow_pending','shadow_running','shadow_passed','blocked',"
        "'ready','cutover','eligible')"
    )
    if "uq_discovery_method_migrations_active_domain" not in migration_indexes:
        op.create_index(
            "uq_discovery_method_migrations_active_domain",
            "discovery_method_migrations",
            ["domain"],
            unique=True,
            postgresql_where=active_migration,
            sqlite_where=active_migration,
        )
    if "uq_discovery_method_migrations_active_legacy" not in migration_indexes:
        op.create_index(
            "uq_discovery_method_migrations_active_legacy",
            "discovery_method_migrations",
            ["legacy_method_id"],
            unique=True,
            postgresql_where=active_legacy,
            sqlite_where=active_legacy,
        )

    _add_missing_columns(
        "discovery_migration_comparisons",
        (
            sa.Column("owner_id", sa.String(length=100), nullable=True),
            sa.Column("owner_pid", sa.Integer(), nullable=True),
            sa.Column("owner_start_token", sa.String(length=100), nullable=True),
            sa.Column("owner_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cancel_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        ),
    )
    _drop_index_if_present(
        "discovery_migration_comparisons",
        "uq_discovery_migration_comparisons_running",
    )
    comparison_indexes = _indexes("discovery_migration_comparisons")
    if "uq_discovery_migration_comparisons_active" not in comparison_indexes:
        active_comparison = sa.text("status IN ('queued','running')")
        op.create_index(
            "uq_discovery_migration_comparisons_active",
            "discovery_migration_comparisons",
            ["migration_id"],
            unique=True,
            postgresql_where=active_comparison,
            sqlite_where=active_comparison,
        )

    # The unique constraint already supplies the lookup index.
    _drop_index_if_present(
        "discovery_run_events",
        "ix_discovery_run_events_run_sequence",
    )


def downgrade() -> None:
    # This is a reconciliation migration: every repaired object is already part
    # of the declared f5 schema on a clean installation.  Removing any of them
    # would make a downgraded database inconsistent with that revision.
    pass
