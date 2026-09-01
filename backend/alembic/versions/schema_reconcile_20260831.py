"""reconcile the production public schema with the canonical development schema

Revision ID: schema_reconcile_20260831
Revises: full_url_method_keys_20260831
Create Date: 2026-08-31

Both databases reported the previous Alembic head even though a few objects in
the production schema still reflected older definitions.  This migration is
deliberately idempotent: it inspects each affected object before changing it so
an already-canonical database only advances its Alembic revision.

Naive GitHub timestamps were written as UTC by the application.  PostgreSQL's
explicit ``AT TIME ZONE 'UTC'`` conversion preserves those instants while
changing the columns to the timezone-aware types declared by the ORM.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "schema_reconcile_20260831"
down_revision: str | Sequence[str] | None = "full_url_method_keys_20260831"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UTC_TIMESTAMP_COLUMNS = {
    "discussion_github_repositories": (
        "backfill_start_at",
        "backfill_end_at",
        "issue_watermark_at",
        "discussion_watermark_at",
        "last_success_at",
        "rate_limit_reset_at",
    ),
    "discussion_github_sync_runs": (
        "started_at",
        "finished_at",
    ),
    "discussion_messages": (
        "upstream_updated_at",
        "last_seen_at",
    ),
}

_QUEUE_STATUS_CHECK = (
    "status IN ('queued', 'starting', 'running', 'cancelling', "
    "'failed', 'completed')"
)


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _quote(identifier: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(identifier)


def _ensure_queue_state_table() -> None:
    if "discovery_queue_state" in _inspector().get_table_names():
        return
    op.create_table(
        "discovery_queue_state",
        sa.Column("id", sa.Integer(), primary_key=True),
    )


def _ensure_queue_status_check() -> None:
    table = "discovery_queue_items"
    constraint = "ck_discovery_queue_items_status"
    checks = {
        item["name"]: str(item.get("sqltext") or "")
        for item in _inspector().get_check_constraints(table)
        if item.get("name")
    }
    current = checks.get(constraint, "").lower()
    if "starting" in current and "cancelling" in current:
        return
    if constraint in checks:
        op.drop_constraint(constraint, table, type_="check")
    op.create_check_constraint(constraint, table, _QUEUE_STATUS_CHECK)


def _ensure_utc_timestamps() -> None:
    for table, column_names in _UTC_TIMESTAMP_COLUMNS.items():
        columns = {column["name"]: column for column in _inspector().get_columns(table)}
        for column_name in column_names:
            column = columns[column_name]
            column_type = column["type"]
            if isinstance(column_type, sa.DateTime) and column_type.timezone:
                continue
            quoted_column = _quote(column_name)
            op.alter_column(
                table,
                column_name,
                existing_type=column_type,
                type_=sa.DateTime(timezone=True),
                existing_nullable=bool(column["nullable"]),
                postgresql_using=f"{quoted_column} AT TIME ZONE 'UTC'",
            )


def _ensure_repair_method_foreign_key_name() -> None:
    table = "site_discovery_runs"
    canonical_name = "fk_site_discovery_runs_repair_method_id_crawl_methods"
    foreign_keys = _inspector().get_foreign_keys(table)
    if any(item.get("name") == canonical_name for item in foreign_keys):
        return

    matching = next(
        (
            item
            for item in foreign_keys
            if item.get("constrained_columns") == ["repair_method_id"]
            and item.get("referred_table") == "crawl_methods"
            and item.get("referred_columns") == ["id"]
        ),
        None,
    )
    if matching is not None and matching.get("name"):
        op.execute(
            sa.text(
                f"ALTER TABLE {_quote(table)} "
                f"RENAME CONSTRAINT {_quote(str(matching['name']))} "
                f"TO {_quote(canonical_name)}"
            )
        )
        return

    op.create_foreign_key(
        canonical_name,
        table,
        "crawl_methods",
        ["repair_method_id"],
        ["id"],
        ondelete="SET NULL",
    )


def upgrade() -> None:
    _ensure_queue_state_table()
    _ensure_queue_status_check()
    _ensure_utc_timestamps()
    _ensure_repair_method_foreign_key_name()


def downgrade() -> None:
    # This revision reconciles databases that had different physical schemas at
    # the same prior Alembic revision.  There is no single truthful old shape to
    # restore, and undoing these repairs would reintroduce model incompatibility.
    pass
