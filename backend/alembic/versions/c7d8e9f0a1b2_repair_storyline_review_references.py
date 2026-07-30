"""repair storyline review references for amended stage-five migration

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-07-29 23:55:00.000000

The original stage-five revision was applied before its ``review_id`` columns
and related constraints were added.  Alembic records a revision by id, so an
amended copy of that migration cannot repair databases that already recorded
``b6c7d8e9f0a1``.  This migration is deliberately defensive: fresh databases
already have the complete schema and therefore perform no data changes.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _column_is_nullable(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return next(column["nullable"] for column in inspector.get_columns(table_name) if column["name"] == column_name)


def _has_review_foreign_key(inspector: sa.Inspector, table_name: str) -> bool:
    return any(
        foreign_key["constrained_columns"] == ["review_id"]
        and foreign_key["referred_table"] == "trend_storyline_reviews"
        for foreign_key in inspector.get_foreign_keys(table_name)
    )


def _has_index(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def _backfill_storyline_reviews(bind: sa.Connection) -> None:
    """Give pre-repair storylines an audit record without discarding their facts."""
    metadata = sa.MetaData()
    storylines = sa.Table("storylines", metadata, autoload_with=bind)
    reviews = sa.Table("trend_storyline_reviews", metadata, autoload_with=bind)
    members = sa.Table("storyline_members", metadata, autoload_with=bind)

    rows = bind.execute(
        sa.select(
            storylines.c.storyline_id,
            storylines.c.overall_start_date,
            storylines.c.overall_end_date,
            storylines.c.window_start_date,
            storylines.c.window_end_date,
            storylines.c.decision,
            storylines.c.agent_review,
        ).where(storylines.c.review_id.is_(None))
    ).mappings()
    for storyline in rows:
        storyline_id = storyline["storyline_id"]
        member_card_ids = list(
            bind.execute(
                sa.select(members.c.card_id)
                .where(members.c.storyline_id == storyline_id)
                .order_by(members.c.card_id)
            ).scalars()
        )
        decision = storyline["decision"]
        review_id = str(uuid.uuid4())
        review_key = "migration-" + hashlib.sha256(storyline_id.encode("utf-8")).hexdigest()[:48]
        bind.execute(
            sa.insert(reviews).values(
                review_id=review_id,
                review_key=review_key,
                candidate_cluster_id=None,
                embedding_version="legacy-stage-five",
                window_start_date=storyline["window_start_date"] or storyline["overall_start_date"],
                window_end_date=storyline["window_end_date"] or storyline["overall_end_date"],
                member_card_ids=member_card_ids,
                removed_card_ids=[],
                status="accepted" if decision == "accept" else "split",
                decision=decision,
                agent_review=storyline["agent_review"],
                error_message=None,
                attempt_count=1,
            )
        )
        bind.execute(
            sa.update(storylines)
            .where(storylines.c.storyline_id == storyline_id)
            .values(review_id=review_id)
        )


def _copy_sqlite_storyline_children(bind: sa.Connection) -> None:
    """Save child rows before batch recreation drops a referenced parent table."""
    bind.execute(sa.text("CREATE TEMPORARY TABLE _trend_repair_storyline_members AS SELECT * FROM storyline_members"))
    bind.execute(sa.text("CREATE TEMPORARY TABLE _trend_repair_storyline_snapshots AS SELECT * FROM storyline_snapshots"))


def _restore_sqlite_storyline_children(bind: sa.Connection) -> None:
    """Restore rows SQLite cascaded while the storylines table was recreated."""
    bind.execute(sa.text("INSERT INTO storyline_members SELECT * FROM _trend_repair_storyline_members"))
    bind.execute(sa.text("INSERT INTO storyline_snapshots SELECT * FROM _trend_repair_storyline_snapshots"))
    bind.execute(sa.text("DROP TABLE _trend_repair_storyline_members"))
    bind.execute(sa.text("DROP TABLE _trend_repair_storyline_snapshots"))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "storylines", "review_id"):
        op.add_column("storylines", sa.Column("review_id", sa.String(length=36), nullable=True))
        inspector = sa.inspect(bind)

    _backfill_storyline_reviews(bind)
    inspector = sa.inspect(bind)
    storyline_has_foreign_key = _has_review_foreign_key(inspector, "storylines")
    if bind.dialect.name == "sqlite":
        if _column_is_nullable(inspector, "storylines", "review_id") or not storyline_has_foreign_key:
            _copy_sqlite_storyline_children(bind)
            with op.batch_alter_table("storylines") as batch:
                batch.alter_column("review_id", existing_type=sa.String(length=36), nullable=False)
                if not storyline_has_foreign_key:
                    batch.create_foreign_key(
                        "fk_storylines_review_id_trend_storyline_reviews",
                        "trend_storyline_reviews",
                        ["review_id"],
                        ["review_id"],
                        ondelete="RESTRICT",
                    )
            _restore_sqlite_storyline_children(bind)
    else:
        op.alter_column("storylines", "review_id", existing_type=sa.String(length=36), nullable=False)
        if not storyline_has_foreign_key:
            op.create_foreign_key(
                "fk_storylines_review_id_trend_storyline_reviews",
                "storylines",
                "trend_storyline_reviews",
                ["review_id"],
                ["review_id"],
                ondelete="RESTRICT",
            )

    inspector = sa.inspect(bind)
    if not _has_index(inspector, "storylines", "ix_storylines_review_id"):
        op.create_index("ix_storylines_review_id", "storylines", ["review_id"])

    inspector = sa.inspect(bind)
    if not _has_column(inspector, "storyline_snapshots", "review_id"):
        op.add_column("storyline_snapshots", sa.Column("review_id", sa.String(length=36), nullable=True))

    bind.execute(
        sa.text(
            "UPDATE storyline_snapshots AS snapshot "
            "SET review_id = storyline.review_id "
            "FROM storylines AS storyline "
            "WHERE snapshot.storyline_id = storyline.storyline_id "
            "AND snapshot.review_id IS NULL"
        )
    )
    inspector = sa.inspect(bind)
    snapshots_have_foreign_key = _has_review_foreign_key(inspector, "storyline_snapshots")
    if bind.dialect.name == "sqlite":
        if _column_is_nullable(inspector, "storyline_snapshots", "review_id") or not snapshots_have_foreign_key:
            with op.batch_alter_table("storyline_snapshots") as batch:
                batch.alter_column("review_id", existing_type=sa.String(length=36), nullable=False)
                if not snapshots_have_foreign_key:
                    batch.create_foreign_key(
                        "fk_storyline_snapshots_review_id_trend_storyline_reviews",
                        "trend_storyline_reviews",
                        ["review_id"],
                        ["review_id"],
                        ondelete="RESTRICT",
                    )
    else:
        op.alter_column("storyline_snapshots", "review_id", existing_type=sa.String(length=36), nullable=False)
        if not snapshots_have_foreign_key:
            op.create_foreign_key(
                "fk_storyline_snapshots_review_id_trend_storyline_reviews",
                "storyline_snapshots",
                "trend_storyline_reviews",
                ["review_id"],
                ["review_id"],
                ondelete="RESTRICT",
            )

    inspector = sa.inspect(bind)
    if not _has_index(inspector, "storyline_snapshots", "ix_storyline_snapshots_review_id"):
        op.create_index("ix_storyline_snapshots_review_id", "storyline_snapshots", ["review_id"])


def downgrade() -> None:
    """Keep repaired columns when returning to the amended parent revision."""
