"""GitHub connector for configured technical-discussion repositories.

Transport details deliberately live here.  Callers receive only durable,
provider-neutral discussion messages and stable progress counts.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DiscussionGithubRepository,
    DiscussionGithubSyncRun,
    DiscussionMessage,
    DiscussionMessageSource,
    DiscussionThread,
)


GITHUB_API = "https://api.github.com"
OVERLAP = timedelta(minutes=10)


class GitHubSyncCancelled(RuntimeError):
    """Raised when the enclosing technical-discussion run is stopped."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize legacy naive database timestamps before GitHub comparisons."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class GitHubClient:
    def __init__(self, token: str | None) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(base_url=GITHUB_API, headers=headers, timeout=30.0, follow_redirects=True)

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *_: object) -> None:
        self._client.close()

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> tuple[Any, httpx.Headers]:
        response = self._client.get(path, params=params)
        response.raise_for_status()
        return response.json(), response.headers

    def graphql(self, query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], httpx.Headers]:
        response = self._client.post("/graphql", json={"query": query, "variables": variables})
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError("GitHub GraphQL: " + "; ".join(str(row.get("message", "unknown")) for row in payload["errors"]))
        return payload["data"], response.headers


class GitHubDiscussionService:
    def __init__(self, db: Session, *, should_stop: Callable[[], bool] | None = None) -> None:
        self.db = db
        self._should_stop = should_stop

    def _ensure_not_stopped(self) -> None:
        if self._should_stop and self._should_stop():
            raise GitHubSyncCancelled()

    def estimate_backfill(self, repository: DiscussionGithubRepository) -> dict[str, int]:
        """Return an intentionally cheap estimate; no messages are persisted."""
        token = os.environ.get(repository.token_env_key)
        with GitHubClient(token) as client:
            issues, _ = client.get(
                "/search/issues",
                params={"q": self._issue_search(repository, include_backfill_window=True), "per_page": 1},
            )
            discussion_count = self._estimate_discussions_in_window(client, repository) if repository.include_discussions else 0
        return {"issues": int(issues.get("total_count", 0)), "discussions": discussion_count}

    @staticmethod
    def _estimate_discussions_in_window(client: GitHubClient, repository: DiscussionGithubRepository) -> int:
        after: str | None = None
        total = 0
        start_at = _as_utc(repository.backfill_start_at)
        end_at = _as_utc(repository.backfill_end_at)
        while True:
            data, _ = client.graphql(_DISCUSSION_ESTIMATE_QUERY, {"owner": repository.owner, "repo": repository.repo, "after": after})
            connection = data["repository"]["discussions"]
            for node in connection["nodes"]:
                created = _parse_time(node["createdAt"])
                if created and start_at and created < start_at:
                    continue
                if created and end_at and created > end_at:
                    return total
                total += 1
            if not connection["pageInfo"]["hasNextPage"]:
                return total
            after = connection["pageInfo"]["endCursor"]

    def sync_repository(
        self,
        repository: DiscussionGithubRepository,
        *,
        pipeline_run_id: str | None,
        progress: Callable[[str, str, str], None] | None = None,
    ) -> dict[str, Any]:
        self._ensure_not_stopped()
        if not repository.enabled:
            return {"repository_id": repository.id, "repository": repository.display_name, "status": "disabled"}
        token = os.environ.get(repository.token_env_key)
        if not token:
            raise RuntimeError(f"GitHub token env key is not configured: {repository.token_env_key}")
        result: dict[str, Any] = {"repository_id": repository.id, "repository": repository.display_name, "issues": None, "discussions": None}
        try:
            with GitHubClient(token) as client:
                if repository.include_issues:
                    result["issues"] = self._sync_issues(client, repository, pipeline_run_id, progress)
                if repository.include_discussions:
                    result["discussions"] = self._sync_discussions(client, repository, pipeline_run_id, progress)
            repository.last_success_at = _utcnow()
            repository.last_error = None
            self.db.commit()
            result["status"] = "ok"
            return result
        except GitHubSyncCancelled:
            self.db.rollback()
            raise
        except Exception as exc:
            self.db.rollback()
            repository = self.db.get(DiscussionGithubRepository, repository.id)
            if repository is not None:
                repository.last_error = str(exc)[:3000]
                self.db.commit()
            raise

    def _sync_issues(self, client: GitHubClient, repository: DiscussionGithubRepository, pipeline_run_id: str | None, progress: Callable[[str, str, str], None] | None) -> dict[str, Any]:
        run = self._new_run(repository, pipeline_run_id, "issue")
        since = self._start_at(repository.issue_watermark_at, repository)
        watermark: tuple[datetime, str] | None = None
        for issue in self._iter_issues(client, repository, since, run):
            self._ensure_not_stopped()
            if issue.get("pull_request"):
                run.skipped += 1
                continue
            if not self._labels_match(issue.get("labels", []), repository):
                run.skipped += 1
                continue
            root = self._upsert_message(repository, issue, kind="issue", parent_external_id=None)
            run.roots_read += 1
            comments_before = run.comments_read
            for comment in self._iter_issue_comments(client, issue["comments_url"], run):
                self._ensure_not_stopped()
                self._upsert_message(repository, comment, kind="issue_comment", parent_external_id=root.external_id, root_title=root.subject)
                run.comments_read += 1
            if progress:
                progress("GitHub 拉取", f"{repository.display_name} Issue #{issue.get('number', '?')}：评论 {run.comments_read - comments_before} 条；主列表累计 {run.roots_read} 个", "info")
            updated_at = _parse_time(issue.get("updated_at"))
            external_id = str(issue.get("node_id") or issue["id"])
            if updated_at and (watermark is None or (updated_at, external_id) > watermark):
                watermark = (updated_at, external_id)
            if repository.issue_watermark_at is not None:
                self._checkpoint(repository, "issue", watermark, run)
        # Issue updated_at normally changes with comments, but the dedicated
        # endpoint also covers delayed visibility and comment-only edits.
        for comment in self._iter_repository_issue_comments(client, repository, since, run):
            self._ensure_not_stopped()
            parent = self._issue_root_for_comment(client, repository, comment)
            if parent is None:
                run.skipped += 1
                continue
            self._upsert_message(repository, comment, kind="issue_comment", parent_external_id=parent.external_id, root_title=parent.subject)
            run.comments_read += 1
            if progress and run.comments_read % 25 == 0:
                progress("GitHub 拉取", f"{repository.display_name} 仓库级评论补扫：累计 {run.comments_read} 条；正在补齐父 Issue 上下文", "info")
        self._finish_run(run, repository, "issue", watermark)
        if progress:
            progress("GitHub 拉取", f"{repository.display_name} Issues：读取 {run.roots_read} 个，评论 {run.comments_read} 条，新增 {run.inserted} 条", "success")
        return self._run_payload(run)

    def _iter_issue_comments(self, client: GitHubClient, url: str, run: DiscussionGithubSyncRun) -> Iterable[dict[str, Any]]:
        page = 1
        while True:
            self._ensure_not_stopped()
            comments, headers = client.get(url, params={"per_page": 100, "page": page})
            run.pages += 1
            run.request_id = headers.get("x-github-request-id") or run.request_id
            yield from comments
            if len(comments) < 100:
                return
            page += 1

    def _iter_repository_issue_comments(self, client: GitHubClient, repository: DiscussionGithubRepository, since: datetime | None, run: DiscussionGithubSyncRun) -> Iterable[dict[str, Any]]:
        page = 1
        while True:
            self._ensure_not_stopped()
            params: dict[str, Any] = {"per_page": 100, "page": page, "sort": "updated", "direction": "asc"}
            if since:
                params["since"] = since.isoformat().replace("+00:00", "Z")
            comments, headers = client.get(f"/repos/{repository.owner}/{repository.repo}/issues/comments", params=params)
            run.pages += 1
            run.request_id = headers.get("x-github-request-id") or run.request_id
            yield from comments
            if len(comments) < 100:
                return
            page += 1

    def _issue_root_for_comment(self, client: GitHubClient, repository: DiscussionGithubRepository, comment: dict[str, Any]) -> DiscussionMessage | None:
        self._ensure_not_stopped()
        issue_url = _text(comment.get("issue_url"))
        roots = self.db.scalars(
            select(DiscussionMessage).where(
                DiscussionMessage.provider == "github",
                DiscussionMessage.repository_id == repository.id,
                DiscussionMessage.message_kind == "issue",
            ),
        )
        for root in roots:
            if _text((root.platform_metadata_json or {}).get("api_url")) == issue_url:
                return root
        if not issue_url:
            return None
        issue, _ = client.get(issue_url.removeprefix(GITHUB_API))
        if issue.get("pull_request"):
            return None
        return self._upsert_message(repository, issue, kind="issue", parent_external_id=None)

    def _iter_issues(self, client: GitHubClient, repository: DiscussionGithubRepository, since: datetime | None, run: DiscussionGithubSyncRun) -> Iterable[dict[str, Any]]:
        # Search is used only for the bounded initial backfill.  Daily slices
        # avoid GitHub Search's 1,000-result cap.  Incremental work uses the
        # repository endpoint ordered by updated_at.
        if repository.issue_watermark_at is None and repository.backfill_start_at:
            start = repository.backfill_start_at.date()
            end = (repository.backfill_end_at or _utcnow()).date()
            while start <= end:
                finish = min(start + timedelta(days=6), end)
                page = 1
                while True:
                    self._ensure_not_stopped()
                    payload, headers = client.get("/search/issues", params={"q": f"{self._issue_search(repository)} created:{start.isoformat()}..{finish.isoformat()}", "sort": "created", "order": "asc", "per_page": 100, "page": page})
                    run.pages += 1
                    run.request_id = headers.get("x-github-request-id") or run.request_id
                    rows = payload.get("items", [])
                    yield from rows
                    if len(rows) < 100:
                        break
                    page += 1
                start = finish + timedelta(days=1)
            return
        page = 1
        while True:
            self._ensure_not_stopped()
            params: dict[str, Any] = {"state": "all", "sort": "updated", "direction": "asc", "per_page": 100, "page": page}
            if since:
                params["since"] = since.isoformat().replace("+00:00", "Z")
            rows, headers = client.get(f"/repos/{repository.owner}/{repository.repo}/issues", params=params)
            run.pages += 1
            run.request_id = headers.get("x-github-request-id") or run.request_id
            yield from rows
            if len(rows) < 100:
                break
            page += 1

    def _sync_discussions(self, client: GitHubClient, repository: DiscussionGithubRepository, pipeline_run_id: str | None, progress: Callable[[str, str, str], None] | None) -> dict[str, Any]:
        run = self._new_run(repository, pipeline_run_id, "discussion")
        after: str | None = None
        since = self._start_at(repository.discussion_watermark_at, repository)
        watermark: tuple[datetime, str] | None = None
        reached_backfill_end = False
        backfill_end_at = _as_utc(repository.backfill_end_at)
        while True:
            self._ensure_not_stopped()
            data, headers = client.graphql(_DISCUSSIONS_QUERY, {"owner": repository.owner, "repo": repository.repo, "after": after})
            run.pages += 1
            run.request_id = headers.get("x-github-request-id") or run.request_id
            connection = data["repository"]["discussions"]
            for node in connection["nodes"]:
                self._ensure_not_stopped()
                updated_at = _parse_time(node["updatedAt"])
                if since and updated_at and updated_at < since:
                    continue
                if repository.discussion_watermark_at is None and backfill_end_at and updated_at and updated_at > backfill_end_at:
                    reached_backfill_end = True
                    break
                category = _text((node.get("category") or {}).get("name"))
                if not self._category_matches(category, repository):
                    run.skipped += 1
                    continue
                root = self._upsert_message(repository, node, kind="discussion", parent_external_id=None)
                run.roots_read += 1
                for comment in self._iter_discussion_comments(client, root.external_id or "", run):
                    self._ensure_not_stopped()
                    comment_message = self._upsert_message(repository, comment, kind="discussion_comment", parent_external_id=root.external_id, root_title=root.subject)
                    run.comments_read += 1
                    for reply in self._iter_discussion_replies(client, comment_message.external_id or "", run):
                        self._ensure_not_stopped()
                        self._upsert_message(repository, reply, kind="discussion_reply", parent_external_id=comment_message.external_id, root_title=root.subject)
                        run.replies_read += 1
                if updated_at and (watermark is None or (updated_at, root.external_id or "") > watermark):
                    watermark = (updated_at, root.external_id or "")
                if repository.discussion_watermark_at is not None:
                    self._checkpoint(repository, "discussion", watermark, run)
            page_info = connection["pageInfo"]
            if reached_backfill_end or not page_info["hasNextPage"]:
                break
            after = page_info["endCursor"]
        self._finish_run(run, repository, "discussion", watermark)
        if progress:
            progress("GitHub 拉取", f"{repository.display_name} Discussions：读取 {run.roots_read} 个，评论 {run.comments_read} 条、回复 {run.replies_read} 条，新增 {run.inserted} 条", "success")
        return self._run_payload(run)

    def _iter_discussion_comments(self, client: GitHubClient, discussion_id: str, run: DiscussionGithubSyncRun) -> Iterable[dict[str, Any]]:
        after: str | None = None
        while True:
            self._ensure_not_stopped()
            data, headers = client.graphql(_DISCUSSION_COMMENTS_QUERY, {"id": discussion_id, "after": after})
            run.pages += 1
            run.request_id = headers.get("x-github-request-id") or run.request_id
            connection = data["node"]["comments"]
            yield from connection["nodes"]
            if not connection["pageInfo"]["hasNextPage"]:
                return
            after = connection["pageInfo"]["endCursor"]

    def _iter_discussion_replies(self, client: GitHubClient, comment_id: str, run: DiscussionGithubSyncRun) -> Iterable[dict[str, Any]]:
        after: str | None = None
        while True:
            self._ensure_not_stopped()
            data, headers = client.graphql(_DISCUSSION_REPLIES_QUERY, {"id": comment_id, "after": after})
            run.pages += 1
            run.request_id = headers.get("x-github-request-id") or run.request_id
            connection = data["node"]["replies"]
            yield from connection["nodes"]
            if not connection["pageInfo"]["hasNextPage"]:
                return
            after = connection["pageInfo"]["endCursor"]

    def _upsert_message(self, repository: DiscussionGithubRepository, payload: dict[str, Any], *, kind: str, parent_external_id: str | None, root_title: str | None = None) -> DiscussionMessage:
        external_id = str(payload.get("id") or payload.get("node_id") or payload["databaseId"])
        # GraphQL and REST use different key spellings but expose equivalent
        # public data.  The original external_id is preserved exactly.
        title = _text(payload.get("title")) or root_title or "GitHub 技术讨论回复"
        author = payload.get("author") or payload.get("user") or {}
        login = _text(author.get("login"))
        body = _text(payload.get("body")) or _text(payload.get("bodyText"))
        created_at = _parse_time(payload.get("createdAt") or payload.get("created_at"))
        updated_at = _parse_time(payload.get("updatedAt") or payload.get("updated_at")) or created_at
        message = self.db.scalar(select(DiscussionMessage).where(DiscussionMessage.provider == "github", DiscussionMessage.external_id == external_id))
        metadata = self._metadata(payload, kind)
        if message is None:
            message = DiscussionMessage(
                provider="github", external_id=external_id, external_parent_id=parent_external_id,
                message_id=f"github:{external_id}", message_kind=kind, repository_id=repository.id,
                public_url=_text(payload.get("html_url") or payload.get("url")) or None, subject=title,
                author_name=login or None, platform_user_id=str(author.get("id") or login) or None,
                sent_at=created_at, upstream_updated_at=updated_at, last_seen_at=_utcnow(), body_text=body or None,
                authored_text=body or None, inline_text=None, attachments_json=[], headers_json={},
                platform_metadata_json=metadata, content_hash=hashlib.sha256(body.encode()).hexdigest(),
                parser_version="github-v1", parse_status="parsed", context_incomplete=False,
            )
            self.db.add(message)
            self.db.flush()
            self._attach_source(message, repository.source_id)
            self._attach_to_thread(message, repository, parent_external_id)
            self._active_run().inserted += 1
        else:
            changed = message.content_hash != hashlib.sha256(body.encode()).hexdigest() or message.upstream_updated_at != updated_at or message.external_parent_id != parent_external_id
            message.external_parent_id = parent_external_id
            message.public_url = _text(payload.get("html_url") or payload.get("url")) or message.public_url
            message.subject = title
            message.author_name = login or message.author_name
            message.platform_user_id = str(author.get("id") or login) or message.platform_user_id
            message.sent_at = created_at or message.sent_at
            message.upstream_updated_at = updated_at
            message.last_seen_at = _utcnow()
            message.upstream_visible = True
            message.body_text = body or None
            message.authored_text = body or None
            message.content_hash = hashlib.sha256(body.encode()).hexdigest()
            message.platform_metadata_json = metadata
            self._attach_source(message, repository.source_id)
            self._attach_to_thread(message, repository, parent_external_id)
            if changed:
                self._active_run().updated += 1
        return message

    def _attach_to_thread(self, message: DiscussionMessage, repository: DiscussionGithubRepository, parent_external_id: str | None) -> None:
        parent = self.db.scalar(select(DiscussionMessage).where(DiscussionMessage.provider == "github", DiscussionMessage.external_id == parent_external_id)) if parent_external_id else None
        message.parent_message_id = parent.id if parent else None
        if parent and parent.thread_id:
            message.thread_id = parent.thread_id
            return
        if message.thread_id:
            return
        thread = DiscussionThread(root_message_id=message.id, is_temporary_root=False, context_incomplete=parent_external_id is not None and parent is None, first_activity_at=message.sent_at, last_activity_at=message.sent_at, message_count=1, participant_count=1)
        self.db.add(thread)
        self.db.flush()
        message.thread_id = thread.id

    def _attach_source(self, message: DiscussionMessage, source_id: int) -> None:
        if self.db.get(DiscussionMessageSource, {"message_id": message.id, "source_id": source_id}) is None:
            self.db.add(DiscussionMessageSource(message_id=message.id, source_id=source_id))

    def _new_run(self, repository: DiscussionGithubRepository, pipeline_run_id: str | None, kind: str) -> DiscussionGithubSyncRun:
        watermark = self._watermark(repository, kind)
        run = DiscussionGithubSyncRun(pipeline_run_id=pipeline_run_id, repository_id=repository.id, content_kind=kind, watermark_before_json=watermark)
        self.db.add(run)
        self.db.flush()
        self._run = run
        return run

    def _active_run(self) -> DiscussionGithubSyncRun:
        return self._run

    def _finish_run(self, run: DiscussionGithubSyncRun, repository: DiscussionGithubRepository, kind: str, watermark: tuple[datetime, str] | None) -> None:
        if watermark:
            at, external_id = watermark
            if kind == "issue":
                repository.issue_watermark_at, repository.issue_watermark_external_id = at, external_id
            else:
                repository.discussion_watermark_at, repository.discussion_watermark_external_id = at, external_id
        run.watermark_after_json = self._watermark(repository, kind)
        run.status = "succeeded"
        run.finished_at = _utcnow()
        self.db.flush()

    def _checkpoint(self, repository: DiscussionGithubRepository, kind: str, watermark: tuple[datetime, str] | None, run: DiscussionGithubSyncRun) -> None:
        """Persist a completed root/page before fetching the next one.

        Content is idempotent, but a durable business watermark also prevents
        an interrupted historical run from replaying already completed pages.
        """
        if watermark:
            at, external_id = watermark
            if kind == "issue":
                repository.issue_watermark_at, repository.issue_watermark_external_id = at, external_id
            else:
                repository.discussion_watermark_at, repository.discussion_watermark_external_id = at, external_id
        run.watermark_after_json = self._watermark(repository, kind)
        self.db.commit()

    @staticmethod
    def _run_payload(run: DiscussionGithubSyncRun) -> dict[str, Any]:
        return {"status": run.status, "pages": run.pages, "roots": run.roots_read, "comments": run.comments_read, "replies": run.replies_read, "inserted": run.inserted, "updated": run.updated, "skipped": run.skipped}

    @staticmethod
    def _watermark(repository: DiscussionGithubRepository, kind: str) -> dict[str, str | None]:
        at = repository.issue_watermark_at if kind == "issue" else repository.discussion_watermark_at
        external_id = repository.issue_watermark_external_id if kind == "issue" else repository.discussion_watermark_external_id
        return {"updated_at": at.isoformat() if at else None, "external_id": external_id}

    @staticmethod
    def _start_at(watermark: datetime | None, repository: DiscussionGithubRepository) -> datetime | None:
        if normalized_watermark := _as_utc(watermark):
            return normalized_watermark - OVERLAP
        return _as_utc(repository.backfill_start_at)

    @staticmethod
    def _issue_search(repository: DiscussionGithubRepository, *, include_backfill_window: bool = False) -> str:
        query = f"repo:{repository.owner}/{repository.repo} is:issue"
        if include_backfill_window and repository.backfill_start_at and repository.backfill_end_at:
            query += f" created:{repository.backfill_start_at.date().isoformat()}..{repository.backfill_end_at.date().isoformat()}"
        return query

    @staticmethod
    def _labels_match(labels: list[Any], repository: DiscussionGithubRepository) -> bool:
        names = {_text(label.get("name") if isinstance(label, dict) else label).casefold() for label in labels}
        allow = {str(value).casefold() for value in repository.issue_label_allowlist or []}
        block = {str(value).casefold() for value in repository.issue_label_blocklist or []}
        return (not allow or bool(names & allow)) and not bool(names & block)

    @staticmethod
    def _category_matches(category: str, repository: DiscussionGithubRepository) -> bool:
        value = category.casefold()
        allow = {str(item).casefold() for item in repository.discussion_category_allowlist or []}
        block = {str(item).casefold() for item in repository.discussion_category_blocklist or []}
        return (not allow or value in allow) and value not in block

    @staticmethod
    def _metadata(payload: dict[str, Any], kind: str) -> dict[str, Any]:
        if kind.startswith("issue"):
            return {"state": payload.get("state"), "state_reason": payload.get("state_reason"), "labels": [_text(row.get("name")) for row in payload.get("labels", []) if isinstance(row, dict)], "milestone": (payload.get("milestone") or {}).get("title"), "number": payload.get("number"), "api_url": payload.get("url")}
        return {"category": _text((payload.get("category") or {}).get("name")), "is_answered": payload.get("isAnswered"), "is_locked": payload.get("locked")}


_DISCUSSION_COUNT_QUERY = """
query($owner: String!, $repo: String!) { repository(owner: $owner, name: $repo) { discussions(first: 1) { totalCount } } }
"""

_DISCUSSION_ESTIMATE_QUERY = """
query($owner: String!, $repo: String!, $after: String) { repository(owner: $owner, name: $repo) { discussions(first: 100, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) { pageInfo { hasNextPage endCursor } nodes { createdAt } } } }
"""

_DISCUSSIONS_QUERY = """
query($owner: String!, $repo: String!, $after: String) {
  repository(owner: $owner, name: $repo) {
    discussions(first: 50, after: $after, orderBy: {field: UPDATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id title bodyText url createdAt updatedAt isAnswered locked category { name }
        author { login }
      }
    }
  }
}
"""

_DISCUSSION_COMMENTS_QUERY = """
query($id: ID!, $after: String) { node(id: $id) { ... on Discussion { comments(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { id bodyText url createdAt updatedAt author { login } } } } } }
"""

_DISCUSSION_REPLIES_QUERY = """
query($id: ID!, $after: String) { node(id: $id) { ... on DiscussionComment { replies(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { id bodyText url createdAt updatedAt author { login } } } } } }
"""
