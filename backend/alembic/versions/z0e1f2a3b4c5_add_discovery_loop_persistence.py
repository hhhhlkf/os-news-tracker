"""add Discovery Loop checkpoints, experiences, and persisted events

Revision ID: z0e1f2a3b4c5
Revises: y9d0e1f2a3b4
Create Date: 2026-08-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "z0e1f2a3b4c5"
down_revision: Union[str, Sequence[str], None] = "y9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("site_discovery_runs") as batch_op:
        batch_op.add_column(
            sa.Column("trigger_type", sa.String(length=20), nullable=False, server_default="manual")
        )
        batch_op.add_column(sa.Column("phase", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("round", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("checkpoint_path", sa.String(length=2000), nullable=True))
        batch_op.add_column(sa.Column(
            "repair_method_id",
            sa.Integer(),
            sa.ForeignKey(
                "crawl_methods.id",
                name="fk_site_discovery_runs_repair_method_id_crawl_methods",
                ondelete="SET NULL",
            ),
            nullable=True,
        ))
        batch_op.add_column(sa.Column("runtime_version", sa.String(length=200), nullable=True))
        batch_op.add_column(
            sa.Column("event_sequence", sa.Integer(), nullable=False, server_default="0")
        )
    op.create_index(
        "ix_site_discovery_runs_repair_method_id",
        "site_discovery_runs",
        ["repair_method_id"],
    )
    op.create_index("ix_site_discovery_runs_status", "site_discovery_runs", ["status"])
    op.create_index(
        "ix_site_discovery_runs_trigger_status",
        "site_discovery_runs",
        ["trigger_type", "status"],
    )
    active_resume_predicate = sa.text(
        "trigger_type = 'resume' AND status IN ('queued', 'running', 'repairing')"
    )
    op.create_index(
        "uq_site_discovery_runs_active_resume_checkpoint",
        "site_discovery_runs",
        ["checkpoint_path"],
        unique=True,
        postgresql_where=active_resume_predicate,
        sqlite_where=active_resume_predicate,
    )

    op.create_table(
        "discovery_experiences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("experience_kind", sa.String(length=40), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("technical_features", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("repair_summary", sa.Text(), nullable=True),
        sa.Column("embedding", sa.JSON(), nullable=True),
        sa.Column("embedding_version", sa.String(length=200), nullable=True),
        sa.Column(
            "source_method_id",
            sa.Integer(),
            sa.ForeignKey("crawl_methods.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_discovery_experiences_domain_kind",
        "discovery_experiences",
        ["domain", "experience_kind"],
    )
    op.create_index(
        "ix_discovery_experiences_source_method",
        "discovery_experiences",
        ["source_method_id"],
    )
    op.create_index(
        "ix_discovery_experiences_approved",
        "discovery_experiences",
        ["approved"],
    )

    op.create_table(
        "discovery_run_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("site_discovery_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("phase", sa.String(length=40), nullable=True),
        sa.Column("round", sa.Integer(), nullable=True),
        sa.Column("level", sa.String(length=20), nullable=False, server_default="info"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("run_id", "sequence", name="uq_discovery_run_event_sequence"),
    )
    op.create_index(
        "ix_discovery_run_events_created_at",
        "discovery_run_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_discovery_run_events_created_at", table_name="discovery_run_events")
    op.drop_table("discovery_run_events")

    op.drop_index("ix_discovery_experiences_approved", table_name="discovery_experiences")
    op.drop_index("ix_discovery_experiences_source_method", table_name="discovery_experiences")
    op.drop_index("ix_discovery_experiences_domain_kind", table_name="discovery_experiences")
    op.drop_table("discovery_experiences")

    op.drop_index(
        "uq_site_discovery_runs_active_resume_checkpoint", table_name="site_discovery_runs"
    )
    op.drop_index("ix_site_discovery_runs_trigger_status", table_name="site_discovery_runs")
    op.drop_index("ix_site_discovery_runs_status", table_name="site_discovery_runs")
    op.drop_index("ix_site_discovery_runs_repair_method_id", table_name="site_discovery_runs")
    with op.batch_alter_table("site_discovery_runs") as batch_op:
        batch_op.drop_column("event_sequence")
        batch_op.drop_column("runtime_version")
        batch_op.drop_column("repair_method_id")
        batch_op.drop_column("checkpoint_path")
        batch_op.drop_column("round")
        batch_op.drop_column("phase")
        batch_op.drop_column("trigger_type")
