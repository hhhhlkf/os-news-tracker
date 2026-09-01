"""reconcile partially applied trusted relay usage schema

Revision ID: relay_usage_reconcile_20260829
Revises: relay_usage_20260829
Create Date: 2026-08-29

Some development databases recorded ``relay_usage_20260829`` after only part
of that revision's DDL was applied.  This revision repairs those databases
without rewriting Alembic history and is a no-op on a fresh schema.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "relay_usage_reconcile_20260829"
down_revision: Union[str, Sequence[str], None] = "relay_usage_20260829"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TRUSTED_RELAY_INDEX = "uq_llm_usage_events_trusted_relay"


def _column_names(inspector: Inspector, table_name: str) -> set[str]:
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    required_tables = {"site_discovery_runs", "llm_usage_events"}
    missing_tables = required_tables - tables
    if missing_tables:
        raise RuntimeError(
            "relay usage reconciliation requires predecessor tables: "
            + ", ".join(sorted(missing_tables))
        )

    discovery_columns = _column_names(inspector, "site_discovery_runs")
    missing_discovery_columns = [
        column
        for column in (
            sa.Column("cleanup_claim_owner", sa.String(length=64), nullable=True),
            sa.Column("cleanup_claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        )
        if column.name not in discovery_columns
    ]
    if missing_discovery_columns:
        with op.batch_alter_table("site_discovery_runs") as batch_op:
            for column in missing_discovery_columns:
                batch_op.add_column(column)

    usage_columns = _column_names(inspector, "llm_usage_events")
    missing_usage_columns = [
        column
        for column in (
            sa.Column("usage_provenance", sa.String(length=40), nullable=True),
            sa.Column("relay_session_id", sa.String(length=64), nullable=True),
            sa.Column("relay_sequence", sa.Integer(), nullable=True),
            sa.Column("relay_usage_status", sa.String(length=16), nullable=True),
        )
        if column.name not in usage_columns
    ]
    if missing_usage_columns:
        with op.batch_alter_table("llm_usage_events") as batch_op:
            for column in missing_usage_columns:
                batch_op.add_column(column)

    # Refresh inspection after conditional ALTERs.  The unique key is required
    # for cross-process usage idempotency and is safe to create only once.
    inspector = sa.inspect(bind)
    usage_columns = _column_names(inspector, "llm_usage_events")
    relay_key_columns = {
        "discovery_run_id",
        "usage_provenance",
        "relay_session_id",
        "relay_sequence",
    }
    if not relay_key_columns.issubset(usage_columns):
        raise RuntimeError("trusted relay idempotency columns remain incomplete")
    index_names = {
        str(index["name"])
        for index in inspector.get_indexes("llm_usage_events")
        if index.get("name")
    }
    if _TRUSTED_RELAY_INDEX not in index_names:
        op.create_index(
            _TRUSTED_RELAY_INDEX,
            "llm_usage_events",
            [
                "discovery_run_id",
                "usage_provenance",
                "relay_session_id",
                "relay_sequence",
            ],
            unique=True,
        )


def downgrade() -> None:
    # Deliberately preserve the canonical columns and index.  They are declared
    # as owned by relay_usage_20260829; this revision only reconciles physical
    # drift after that revision was already recorded.  A no-op here keeps both
    # fresh databases (where upgrade added nothing) and repaired legacy
    # databases compatible with the predecessor's own downgrade, which expects
    # every declared object to exist before removing it.
    pass
