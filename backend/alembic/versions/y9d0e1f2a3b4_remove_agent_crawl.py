"""remove obsolete Agent Crawl schema

Revision ID: y9d0e1f2a3b4
Revises: x8c9d0e1f2a3
Create Date: 2026-08-20 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "y9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "x8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    agent_sources = "SELECT id FROM sources WHERE type = 'agent_crawl'"

    # Preserve previously collected news while eliminating the retired source
    # records. Items support a nullable source_id and retain their enrichment.
    op.execute(sa.text(f"UPDATE items SET source_id = NULL WHERE source_id IN ({agent_sources})"))
    op.execute(sa.text(f"UPDATE items SET status = 'enriched' WHERE status = 'agent_enriched'"))
    op.execute(sa.text(f"DELETE FROM item_sources WHERE source_id IN ({agent_sources})"))
    op.execute(sa.text(f"DELETE FROM agent_source_configs WHERE source_id IN ({agent_sources})"))
    op.execute(sa.text(f"DELETE FROM agent_site_memory WHERE source_id IN ({agent_sources})"))
    op.execute(sa.text(f"DELETE FROM agent_crawl_runs WHERE source_id IN ({agent_sources})"))
    op.execute(sa.text("DELETE FROM sources WHERE type = 'agent_crawl'"))

    op.drop_table("agent_source_configs")
    op.drop_table("agent_site_memory")
    op.drop_table("agent_crawl_runs")


def downgrade() -> None:
    op.create_table(
        "agent_crawl_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plan_urls_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fetched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality_passed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("target_count", sa.Integer(), nullable=True),
        sa.Column("current_stage", sa.String(length=20), nullable=False, server_default="planning"),
        sa.Column("stage_message", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_table(
        "agent_site_memory",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url_pattern", sa.String(length=2000), nullable=False),
        sa.Column("quality_score", sa.Integer(), nullable=True),
        sa.Column("quality_reason", sa.Text(), nullable=True),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("relevant_topic", sa.String(length=200), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("seen_count", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("source_id", "url_pattern", name="uq_agent_memory_source_url"),
    )
    op.create_table(
        "agent_source_configs",
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("focus_areas", sa.JSON(), nullable=False),
        sa.Column("topic_groups", sa.JSON(), nullable=False),
        sa.Column("crawl_depth", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_urls_per_run", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("quality_threshold", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("crawl_workers", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("quality_workers", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("summary_workers", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
