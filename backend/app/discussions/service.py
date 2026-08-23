"""Persistence and deterministic stages for technical discussion mail."""

from __future__ import annotations

import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from email.message import Message
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.discussions.imap import ReadOnlyImapMailbox
from app.discussions.mime import PARSER_VERSION, parse_message
from app.discussions.rules import matching_source_ids
from app.models import (
    DiscussionGroup,
    DiscussionGroupThread,
    DiscussionMailboxConnection,
    DiscussionMessage,
    DiscussionMessageSource,
    DiscussionSourceRule,
    DiscussionThread,
    Source,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DiscussionService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def receive(self, connection_id: int, *, should_stop: Callable[[], bool] | None = None) -> dict[str, int | str]:
        connection = self.db.get(DiscussionMailboxConnection, connection_id)
        if connection is None:
            raise LookupError("discussion mailbox connection not found")
        if not connection.enabled:
            return {"status": "disabled", "headers": 0, "matched": 0, "unknown": 0, "inserted": 0, "duplicates": 0}
        username = os.environ.get(connection.username_env_key)
        password = os.environ.get(connection.password_env_key)
        if not username or not password:
            raise RuntimeError("discussion mailbox credentials are not configured")
        rules = list(self.db.scalars(
            select(DiscussionSourceRule)
            .join(Source, Source.id == DiscussionSourceRule.source_id)
            .where(DiscussionSourceRule.enabled.is_(True), Source.enabled.is_(True))
        ))
        batch_id = str(uuid.uuid4())
        stats: dict[str, int | str] = {"status": "ok", "headers": 0, "matched": 0, "unknown": 0, "inserted": 0, "duplicates": 0}
        try:
            with ReadOnlyImapMailbox(
                host=connection.host,
                port=connection.port,
                username=username,
                password=password,
                folder=connection.folder,
            ) as mailbox:
                current_uidvalidity = mailbox.uidvalidity
                # A UIDVALIDITY change is deliberately a controlled re-scan.
                start_uid = connection.last_uid if current_uidvalidity == connection.uidvalidity else 0
                for uid in mailbox.new_uids(start_uid):
                    if should_stop and should_stop():
                        stats["status"] = "cancelled"
                        return stats
                    headers = mailbox.fetch_headers(uid)
                    stats["headers"] = int(stats["headers"]) + 1
                    source_ids = matching_source_ids(headers, rules)
                    if not source_ids:
                        stats["unknown"] = int(stats["unknown"]) + 1
                    else:
                        stats["matched"] = int(stats["matched"]) + 1
                        outcome = self._store_message(
                            mailbox.fetch_full(uid), source_ids=source_ids, uid=uid,
                            uidvalidity=current_uidvalidity, batch_id=batch_id,
                        )
                        stats[outcome] = int(stats[outcome]) + 1
                    # Advancing only after this UID has been handled gives a retry
                    # point after a transient fetch or database failure.
                    connection.last_uid = uid
                    connection.uidvalidity = current_uidvalidity
                    self.db.commit()
                connection.last_success_at = utcnow()
                connection.health_status = "ok"
                connection.last_error = None
                self.db.commit()
        except Exception as exc:
            self.db.rollback()
            connection = self.db.get(DiscussionMailboxConnection, connection_id)
            if connection is not None:
                connection.health_status = "error"
                connection.last_error = str(exc)[:2000]
                self.db.commit()
            raise
        return stats

    def _store_message(
        self,
        raw: Message,
        *,
        source_ids: set[int],
        uid: int,
        uidvalidity: str | None,
        batch_id: str,
    ) -> str:
        parsed = parse_message(raw)
        existing = self.db.scalar(select(DiscussionMessage).where(DiscussionMessage.message_id == parsed.message_id))
        if existing is not None:
            if existing.content_hash != parsed.content_hash:
                existing.parse_status = "content_conflict"
            for source_id in source_ids:
                self._add_source(existing.id, source_id)
            return "duplicates"
        message = DiscussionMessage(
            message_id=parsed.message_id,
            in_reply_to=parsed.in_reply_to,
            references_json=parsed.references,
            subject=parsed.subject,
            author_name=parsed.author_name,
            author_email=parsed.author_email,
            sent_at=parsed.sent_at,
            body_text=parsed.body_text,
            authored_text=parsed.authored_text,
            inline_text=parsed.inline_text,
            attachments_json=parsed.attachments,
            headers_json=parsed.headers,
            content_hash=parsed.content_hash,
            parser_version=PARSER_VERSION,
            parse_status=parsed.parse_status,
            uidvalidity=uidvalidity,
            imap_uid=uid,
            received_batch_id=batch_id,
        )
        self.db.add(message)
        self.db.flush()
        for source_id in source_ids:
            self._add_source(message.id, source_id)
        return "inserted"

    def _add_source(self, message_id: int, source_id: int) -> None:
        if self.db.get(DiscussionMessageSource, {"message_id": message_id, "source_id": source_id}) is None:
            self.db.add(DiscussionMessageSource(message_id=message_id, source_id=source_id))

    def rebuild_threads(self) -> dict[str, int]:
        """Rebuild only Message-ID-derived edges and repair temporary roots.

        The operation is deterministic and intentionally does not look at
        subjects or embeddings.  It is cheap enough for the daily mail batch
        and makes late ancestors repair previously split threads automatically.
        """
        # Mail relationships come exclusively from RFC822 headers.  GitHub
        # relationships are established by its connector and must never be
        # recalculated from mail-only fields such as ``in_reply_to``.
        messages = list(self.db.scalars(
            select(DiscussionMessage)
            .where(DiscussionMessage.provider == "mail")
            .order_by(DiscussionMessage.id)
        ))
        by_message_id = {message.message_id: message for message in messages}
        for message in messages:
            parent = by_message_id.get(message.in_reply_to or "")
            if parent is None:
                for ancestor_id in reversed(message.references_json or []):
                    if ancestor := by_message_id.get(ancestor_id):
                        parent = ancestor
                        break
            message.parent_message_id = parent.id if parent else None
            message.context_incomplete = bool((message.in_reply_to or message.references_json) and parent is None)

        by_id = {message.id: message for message in messages}
        children: dict[int, list[int]] = defaultdict(list)
        for message in messages:
            if message.parent_message_id:
                children[message.parent_message_id].append(message.id)
        components: list[list[DiscussionMessage]] = []
        seen: set[int] = set()
        for message in messages:
            if message.id in seen:
                continue
            stack = [message.id]
            component_ids: list[int] = []
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                component_ids.append(current)
                parent_id = by_id[current].parent_message_id
                if parent_id:
                    stack.append(parent_id)
                stack.extend(children.get(current, []))
            components.append([by_id[item_id] for item_id in component_ids])

        existing = {
            thread.id: thread
            for thread in self.db.scalars(
                select(DiscussionThread)
                .join(DiscussionMessage, DiscussionMessage.thread_id == DiscussionThread.id)
                .where(DiscussionMessage.provider == "mail")
                .distinct()
            ).all()
        }
        used_threads: set[int] = set()
        repaired = 0
        for component in components:
            prior_thread_ids = {message.thread_id for message in component if message.thread_id in existing}
            thread = existing[min(prior_thread_ids)] if prior_thread_ids else DiscussionThread()
            if thread.id is None:
                self.db.add(thread)
                self.db.flush()
                existing[thread.id] = thread
            used_threads.add(thread.id)
            roots = [m for m in component if m.parent_message_id not in {x.id for x in component}]
            root = min(roots or component, key=lambda m: (m.sent_at or datetime.max.replace(tzinfo=timezone.utc), m.id))
            was_temporary = thread.is_temporary_root
            thread.root_message_id = root.id
            thread.is_temporary_root = root.context_incomplete
            thread.context_incomplete = any(m.context_incomplete for m in component)
            sent = [m.sent_at for m in component if m.sent_at]
            thread.first_activity_at = min(sent) if sent else None
            thread.last_activity_at = max(sent) if sent else None
            thread.message_count = len(component)
            thread.participant_count = len({m.author_email for m in component if m.author_email})
            for message in component:
                message.thread_id = thread.id
            if was_temporary and not thread.is_temporary_root:
                repaired += 1
        for thread_id, thread in existing.items():
            if thread_id not in used_threads and not self.db.scalar(select(DiscussionGroupThread.group_id).where(DiscussionGroupThread.thread_id == thread_id)):
                self.db.delete(thread)
        self.db.commit()
        return {"threads": len(components), "messages": len(messages), "repaired": repaired}

    def refresh_platform_threads(self) -> dict[str, int]:
        """Refresh connector-owned trees without inferring any new edges.

        GitHub Issue comments are deliberately all direct children of the
        Issue root; GitHub Discussion replies already carry their GraphQL
        parent edge.  This stage only recalculates aggregate counters.
        """
        threads = list(self.db.scalars(
            select(DiscussionThread)
            .join(DiscussionMessage, DiscussionMessage.thread_id == DiscussionThread.id)
            .where(DiscussionMessage.provider == "github")
            .distinct()
        ))
        for thread in threads:
            messages = list(self.db.scalars(
                select(DiscussionMessage)
                .where(DiscussionMessage.thread_id == thread.id)
                .order_by(DiscussionMessage.sent_at, DiscussionMessage.id)
            ))
            if not messages:
                continue
            root = next((message for message in messages if message.parent_message_id is None), messages[0])
            times = [message.sent_at for message in messages if message.sent_at]
            thread.root_message_id = root.id
            thread.is_temporary_root = root.context_incomplete
            thread.context_incomplete = any(message.context_incomplete for message in messages)
            thread.first_activity_at = min(times) if times else None
            last_activity = [message.upstream_updated_at or message.sent_at for message in messages if message.upstream_updated_at or message.sent_at]
            thread.last_activity_at = max(last_activity) if last_activity else None
            thread.message_count = len(messages)
            thread.participant_count = len({message.platform_user_id or message.author_name for message in messages if message.platform_user_id or message.author_name})
        self.db.commit()
        return {"threads": len(threads)}

    def sync_candidate_groups(self, source_ids: set[int] | None = None) -> dict[str, int]:
        """Give each deterministic thread a candidate group without LLM work."""
        groups_by_thread = {
            link.thread_id: link.group_id
            for link in self.db.scalars(select(DiscussionGroupThread)).all()
        }
        created = 0
        updated = 0
        threads = list(self.db.scalars(select(DiscussionThread)).all())
        for thread in threads:
            message_ids = list(self.db.scalars(select(DiscussionMessage.id).where(DiscussionMessage.thread_id == thread.id)))
            if source_ids is not None and not self.db.scalar(
                select(DiscussionMessageSource.message_id).where(
                    DiscussionMessageSource.message_id.in_(message_ids),
                    DiscussionMessageSource.source_id.in_(source_ids),
                )
            ):
                continue
            group_id = groups_by_thread.get(thread.id)
            group = self.db.get(DiscussionGroup, group_id) if group_id else None
            if group is None:
                group = DiscussionGroup()
                self.db.add(group)
                self.db.flush()
                self.db.add(DiscussionGroupThread(group_id=group.id, thread_id=thread.id))
                created += 1
                prior_message_count = 0
            else:
                prior_message_count = group.message_count
            group.first_activity_at = thread.first_activity_at
            group.last_activity_at = thread.last_activity_at
            group.message_count = thread.message_count
            group.participant_count = thread.participant_count
            has_new_activity = (
                group.last_processed_at is not None
                and thread.last_activity_at is not None
                and _as_utc(thread.last_activity_at) > _as_utc(group.last_processed_at)
            )
            if thread.message_count > prior_message_count or has_new_activity:
                group.pending_message_count = max(1 if has_new_activity else 0, thread.message_count - prior_message_count)
                group.processing_status = "pending" if self._is_candidate(thread) else "waiting"
            updated += 1
        self.db.commit()
        return {"created": created, "updated": updated}

    def requeue_failed_groups(self, source_ids: set[int] | None = None) -> int:
        """Make a later manual or scheduled run retry groups that hit a transient error."""
        requeued = 0
        for group in self.db.scalars(
            select(DiscussionGroup).where(DiscussionGroup.processing_status == "failed"),
        ):
            if source_ids is not None and not (set(discussion_source_ids(self.db, group.id)) & source_ids):
                continue
            group.processing_status = "pending"
            requeued += 1
        if requeued:
            self.db.commit()
        return requeued

    @staticmethod
    def _is_candidate(thread: DiscussionThread) -> bool:
        return thread.message_count >= 2 or thread.participant_count >= 2


def discussion_source_ids(db: Session, group_id: int) -> list[int]:
    rows = db.execute(
        select(func.distinct(DiscussionMessageSource.source_id))
        .join(DiscussionMessage, DiscussionMessage.id == DiscussionMessageSource.message_id)
        .join(DiscussionGroupThread, DiscussionGroupThread.thread_id == DiscussionMessage.thread_id)
        .where(DiscussionGroupThread.group_id == group_id)
    ).all()
    return [row[0] for row in rows]


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
