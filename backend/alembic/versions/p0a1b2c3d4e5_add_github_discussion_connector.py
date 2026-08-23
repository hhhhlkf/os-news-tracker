"""add GitHub technical-discussion connector

Revision ID: p0a1b2c3d4e5
Revises: o9b0c1d2e3f4
Create Date: 2026-08-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "p0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "o9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "discussion_github_repositories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False, unique=True),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column("repo", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("token_env_key", sa.String(length=120), nullable=False, server_default="GITHUB_TOKEN"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("review_status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("include_issues", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("include_discussions", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("issue_label_allowlist", sa.JSON(), nullable=False),
        sa.Column("issue_label_blocklist", sa.JSON(), nullable=False),
        sa.Column("discussion_category_allowlist", sa.JSON(), nullable=False),
        sa.Column("discussion_category_blocklist", sa.JSON(), nullable=False),
        sa.Column("backfill_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backfill_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issue_watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issue_watermark_external_id", sa.String(length=1000), nullable=True),
        sa.Column("discussion_watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discussion_watermark_external_id", sa.String(length=1000), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connector_version", sa.String(length=50), nullable=False, server_default="github-v1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("owner", "repo", name="uq_discussion_github_repository"),
    )
    op.create_index("ix_discussion_github_repositories_review_status", "discussion_github_repositories", ["review_status"])
    op.create_table(
        "discussion_github_sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pipeline_run_id", sa.String(length=36), sa.ForeignKey("discussion_pipeline_runs.id"), nullable=True),
        sa.Column("repository_id", sa.Integer(), sa.ForeignKey("discussion_github_repositories.id"), nullable=False),
        sa.Column("content_kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("roots_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comments_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("replies_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("watermark_before_json", sa.JSON(), nullable=True),
        sa.Column("watermark_after_json", sa.JSON(), nullable=True),
        sa.Column("failure_range", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_discussion_github_sync_runs_pipeline", "discussion_github_sync_runs", ["pipeline_run_id"])
    with op.batch_alter_table("discussion_messages") as batch:
        batch.add_column(sa.Column("provider", sa.String(length=20), nullable=False, server_default="mail"))
        batch.add_column(sa.Column("external_id", sa.String(length=1000), nullable=True))
        batch.add_column(sa.Column("external_parent_id", sa.String(length=1000), nullable=True))
        batch.add_column(sa.Column("public_url", sa.String(length=1000), nullable=True))
        batch.add_column(sa.Column("message_kind", sa.String(length=30), nullable=False, server_default="mail"))
        batch.add_column(sa.Column("repository_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("platform_user_id", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("platform_metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        batch.add_column(sa.Column("upstream_updated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("upstream_visible", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.create_foreign_key("fk_discussion_messages_repository", "discussion_github_repositories", ["repository_id"], ["id"])
        batch.create_unique_constraint("uq_discussion_provider_external_id", ["provider", "external_id"])
        batch.create_index("ix_discussion_messages_repository_updated", ["repository_id", "upstream_updated_at", "external_id"])
    op.execute("UPDATE discussion_messages SET external_id = message_id WHERE provider = 'mail' AND external_id IS NULL")
    op.add_column("discussion_pipeline_runs", sa.Column("github_repository_ids_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("discussion_pipeline_runs", "github_repository_ids_json")
    with op.batch_alter_table("discussion_messages") as batch:
        batch.drop_index("ix_discussion_messages_repository_updated")
        batch.drop_constraint("uq_discussion_provider_external_id", type_="unique")
        batch.drop_constraint("fk_discussion_messages_repository", type_="foreignkey")
        for column in ("upstream_visible", "last_seen_at", "upstream_updated_at", "platform_metadata_json", "platform_user_id", "repository_id", "message_kind", "public_url", "external_parent_id", "external_id", "provider"):
            batch.drop_column(column)
    op.drop_index("ix_discussion_github_sync_runs_pipeline", table_name="discussion_github_sync_runs")
    op.drop_table("discussion_github_sync_runs")
    op.drop_index("ix_discussion_github_repositories_review_status", table_name="discussion_github_repositories")
    op.drop_table("discussion_github_repositories")
