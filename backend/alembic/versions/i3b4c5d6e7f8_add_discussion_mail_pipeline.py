"""add discussion mail pipeline

Revision ID: i3b4c5d6e7f8
Revises: h2a3b4c5d6e7
Create Date: 2026-08-14 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "i3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "h2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("items", sa.Column("item_kind", sa.String(20), nullable=False, server_default="news"))
    op.add_column("items", sa.Column("last_activity_at", sa.DateTime(), nullable=True))
    op.add_column("items", sa.Column("content_revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("items", sa.Column("heat_score", sa.Float(), nullable=True))
    op.add_column("items", sa.Column("merged_into_item_id", sa.Integer(), nullable=True))
    op.create_index("ix_items_item_kind", "items", ["item_kind"])
    op.create_index("ix_items_last_activity_at", "items", ["last_activity_at"])
    op.create_index("ix_items_merged_into_item_id", "items", ["merged_into_item_id"])
    op.create_foreign_key("fk_items_merged_into_item", "items", "items", ["merged_into_item_id"], ["id"])
    op.alter_column("items", "source_id", existing_type=sa.Integer(), nullable=True)
    op.alter_column("items", "url", existing_type=sa.String(length=1000), nullable=True)
    op.alter_column("items", "url_hash", existing_type=sa.String(length=64), nullable=True)

    op.create_table("discussion_mailbox_connections",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("name", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False, server_default="imap"), sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="993"), sa.Column("folder", sa.String(255), nullable=False, server_default="INBOX"),
        sa.Column("username_env_key", sa.String(120), nullable=False), sa.Column("password_env_key", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("uidvalidity", sa.String(100)),
        sa.Column("last_uid", sa.Integer(), nullable=False, server_default="0"), sa.Column("last_success_at", sa.DateTime()),
        sa.Column("health_status", sa.String(20), nullable=False, server_default="unknown"), sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_discussion_mailbox_name"))
    op.create_table("discussion_source_rules",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("rule_type", sa.String(40), nullable=False), sa.Column("match_value", sa.String(1000), nullable=False),
        sa.Column("header_name", sa.String(255)), sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]), sa.UniqueConstraint("source_id", "rule_type", "match_value", name="uq_discussion_source_rule"))
    op.create_index("ix_discussion_source_rules_source_id", "discussion_source_rules", ["source_id"])
    op.create_table("discussion_threads",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("root_message_id", sa.Integer()),
        sa.Column("is_temporary_root", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("context_incomplete", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("first_activity_at", sa.DateTime()), sa.Column("last_activity_at", sa.DateTime()), sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("participant_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("embedding_version", sa.String(120)), sa.Column("embedding_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()))
    op.create_table("discussion_messages",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("message_id", sa.String(1000), nullable=False), sa.Column("in_reply_to", sa.String(1000)),
        sa.Column("references_json", sa.JSON(), nullable=False), sa.Column("subject", sa.Text(), nullable=False), sa.Column("author_name", sa.String(500)), sa.Column("author_email", sa.String(500)),
        sa.Column("sent_at", sa.DateTime()), sa.Column("body_text", sa.Text()), sa.Column("authored_text", sa.Text()), sa.Column("inline_text", sa.Text()), sa.Column("attachments_json", sa.JSON(), nullable=False), sa.Column("headers_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False), sa.Column("parser_version", sa.String(50), nullable=False, server_default="discussion-mime-v1"), sa.Column("parse_status", sa.String(30), nullable=False, server_default="parsed"),
        sa.Column("context_incomplete", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("body_purged_at", sa.DateTime()), sa.Column("uidvalidity", sa.String(100)), sa.Column("imap_uid", sa.Integer()), sa.Column("received_batch_id", sa.String(36)),
        sa.Column("parent_message_id", sa.Integer()), sa.Column("thread_id", sa.Integer()), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["parent_message_id"], ["discussion_messages.id"]), sa.ForeignKeyConstraint(["thread_id"], ["discussion_threads.id"]), sa.UniqueConstraint("message_id", name="uq_discussion_message_id"))
    for name, cols in (("ix_discussion_messages_in_reply_to", ["in_reply_to"]), ("ix_discussion_messages_sent_at", ["sent_at"]), ("ix_discussion_messages_author_email", ["author_email"]), ("ix_discussion_messages_content_hash", ["content_hash"]), ("ix_discussion_messages_received_batch_id", ["received_batch_id"]), ("ix_discussion_messages_parent_message_id", ["parent_message_id"]), ("ix_discussion_messages_thread_id", ["thread_id"])):
        op.create_index(name, "discussion_messages", cols)
    op.create_foreign_key("fk_discussion_threads_root_message", "discussion_threads", "discussion_messages", ["root_message_id"], ["id"])
    op.create_unique_constraint("uq_discussion_threads_root_message", "discussion_threads", ["root_message_id"])
    op.create_table("discussion_message_sources", sa.Column("message_id", sa.Integer(), nullable=False), sa.Column("source_id", sa.Integer(), nullable=False), sa.ForeignKeyConstraint(["message_id"], ["discussion_messages.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["source_id"], ["sources.id"]), sa.PrimaryKeyConstraint("message_id", "source_id"))
    op.create_table("discussion_groups",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("item_id", sa.Integer()), sa.Column("activity_status", sa.String(20), nullable=False, server_default="active"), sa.Column("resolution_status", sa.String(20), nullable=False, server_default="exploring"),
        sa.Column("first_activity_at", sa.DateTime()), sa.Column("last_activity_at", sa.DateTime()), sa.Column("last_processed_at", sa.DateTime()), sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("participant_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("pending_message_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("structured_result", sa.JSON()), sa.Column("content_revision", sa.Integer(), nullable=False, server_default="0"), sa.Column("processing_status", sa.String(30), nullable=False, server_default="pending"), sa.Column("rejection_reason", sa.Text()), sa.Column("heat_score", sa.Float()), sa.Column("heat_version", sa.String(50), nullable=False, server_default="discussion-heat-v1"), sa.Column("title_locked", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()), sa.ForeignKeyConstraint(["item_id"], ["items.id"]), sa.UniqueConstraint("item_id", name="uq_discussion_group_item"))
    op.create_index("ix_discussion_groups_item_id", "discussion_groups", ["item_id"])
    op.create_table("discussion_group_threads", sa.Column("group_id", sa.Integer(), nullable=False), sa.Column("thread_id", sa.Integer(), nullable=False), sa.ForeignKeyConstraint(["group_id"], ["discussion_groups.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["thread_id"], ["discussion_threads.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("group_id", "thread_id"))
    op.create_table("discussion_progress_snapshots", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("group_id", sa.Integer(), nullable=False), sa.Column("content_revision", sa.Integer(), nullable=False), sa.Column("new_message_ids", sa.JSON(), nullable=False), sa.Column("new_reply_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("progress_summary", sa.Text()), sa.Column("activity_status", sa.String(20), nullable=False), sa.Column("resolution_status", sa.String(20), nullable=False), sa.Column("evidence_message_ids", sa.JSON(), nullable=False), sa.Column("prompt_version", sa.String(120)), sa.Column("model_name", sa.String(255)), sa.Column("parser_version", sa.String(50)), sa.Column("heat_version", sa.String(50)), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()), sa.ForeignKeyConstraint(["group_id"], ["discussion_groups.id"], ondelete="CASCADE"))
    op.create_index("ix_discussion_progress_snapshots_group_id", "discussion_progress_snapshots", ["group_id"])
    op.create_table("discussion_title_history", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("item_id", sa.Integer(), nullable=False), sa.Column("previous_title", sa.String(1000), nullable=False), sa.Column("new_title", sa.String(1000), nullable=False), sa.Column("reason", sa.Text(), nullable=False), sa.Column("actor", sa.String(40), nullable=False, server_default="system"), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()), sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"))
    op.create_index("ix_discussion_title_history_item_id", "discussion_title_history", ["item_id"])
    op.create_table("discussion_processing_runs", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("group_id", sa.Integer()), sa.Column("run_kind", sa.String(40), nullable=False), sa.Column("status", sa.String(30), nullable=False, server_default="running"), sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"), sa.Column("detail", sa.Text()), sa.Column("metadata_json", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()), sa.Column("finished_at", sa.DateTime()), sa.ForeignKeyConstraint(["group_id"], ["discussion_groups.id"], ondelete="CASCADE"))
    op.create_index("ix_discussion_processing_runs_group_id", "discussion_processing_runs", ["group_id"])


def downgrade() -> None:
    for table in ("discussion_processing_runs", "discussion_title_history", "discussion_progress_snapshots", "discussion_group_threads", "discussion_groups", "discussion_message_sources", "discussion_messages", "discussion_threads", "discussion_source_rules", "discussion_mailbox_connections"):
        op.drop_table(table)
    op.drop_constraint("fk_items_merged_into_item", "items", type_="foreignkey")
    op.drop_index("ix_items_merged_into_item_id", table_name="items")
    op.drop_index("ix_items_last_activity_at", table_name="items")
    op.drop_index("ix_items_item_kind", table_name="items")
    op.drop_column("items", "merged_into_item_id")
    op.drop_column("items", "heat_score")
    op.drop_column("items", "content_revision")
    op.drop_column("items", "last_activity_at")
    op.drop_column("items", "item_kind")
