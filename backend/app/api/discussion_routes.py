"""Administrative and read APIs for the technical discussion mail stream."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_system_access
from app.discussions.organizer import DiscussionOrganizer, DiscussionOrganizerCancelled
from app.discussions.github import GitHubDiscussionService, GitHubSyncCancelled
from app.discussions.service import DiscussionService
from app.schemas import TimeWindowRequest
from app.models import (
    DiscussionGroup,
    DiscussionGroupThread,
    DiscussionMailboxConnection,
    DiscussionMessage,
    DiscussionPipelineRun,
    DiscussionPipelineEvent,
    DiscussionGithubRepository,
    DiscussionGithubSyncRun,
    DiscussionMessageSource,
    DiscussionProgressSnapshot,
    DiscussionSourceRule,
    Item,
    ItemSource,
    Source,
)
from app.db import SessionLocal
from app.enums import Stream


router = APIRouter(prefix="/discussions", tags=["discussions"])
logger = logging.getLogger(__name__)
_ACTIVE_PIPELINE_STATUSES = ("running", "stopping")
_pipeline_cancel_lock = threading.Lock()
_pipeline_cancel_events: dict[str, threading.Event] = {}


class DiscussionPipelineCancelled(RuntimeError):
    """Controlled, cooperative cancellation of a discussion pipeline run."""


def _register_pipeline_run(run_id: str) -> None:
    with _pipeline_cancel_lock:
        _pipeline_cancel_events[run_id] = threading.Event()


def _unregister_pipeline_run(run_id: str) -> None:
    with _pipeline_cancel_lock:
        _pipeline_cancel_events.pop(run_id, None)


def _is_pipeline_stop_requested(run_id: str) -> bool:
    with _pipeline_cancel_lock:
        event = _pipeline_cancel_events.get(run_id)
        return bool(event and event.is_set())


def _request_pipeline_stop(run_id: str) -> bool:
    with _pipeline_cancel_lock:
        event = _pipeline_cancel_events.get(run_id)
        if event is None:
            return False
        event.set()
        return True


def _raise_if_pipeline_stopped(run_id: str) -> None:
    if _is_pipeline_stop_requested(run_id):
        raise DiscussionPipelineCancelled()


class ConnectionPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=993, ge=1, le=65535)
    folder: str = Field(default="INBOX", min_length=1, max_length=255)
    username_env_key: str = Field(min_length=1, max_length=120)
    password_env_key: str = Field(min_length=1, max_length=120)
    enabled: bool = False


class RulePayload(BaseModel):
    source_id: int
    rule_type: str = Field(pattern="^(list_id|list_post|delivered_to|to|cc|header)$")
    match_value: str = Field(min_length=1, max_length=1000)
    header_name: str | None = Field(default=None, max_length=255)
    enabled: bool = True


class DiscussionRunPayload(BaseModel):
    channel: Literal["mail", "github"]
    source_ids: list[int] | None = None
    github_repository_ids: list[int] | None = None
    organize: bool = True


class DiscussionSourcePayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    rule_type: str = Field(pattern="^(list_id|list_post|delivered_to|to|cc|header)$")
    match_value: str = Field(min_length=1, max_length=1000)
    header_name: str | None = Field(default=None, max_length=255)


class DiscussionSourceBatchPayload(BaseModel):
    source_ids: list[int] = Field(min_length=1)


class DiscussionSourceStatusPayload(BaseModel):
    enabled: bool


class DiscussionSchedulePayload(BaseModel):
    enabled: bool | None = None
    run_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    patrol_interval_hours: int | None = Field(default=None, ge=1, le=24)


class GitHubRepositoryPayload(BaseModel):
    owner: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    repo: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=200)
    token_env_key: str = Field(default="GITHUB_TOKEN", min_length=1, max_length=120)
    include_issues: bool = True
    include_discussions: bool = True
    issue_label_allowlist: list[str] = Field(default_factory=list)
    issue_label_blocklist: list[str] = Field(default_factory=list)
    discussion_category_allowlist: list[str] = Field(default_factory=list)
    discussion_category_blocklist: list[str] = Field(default_factory=list)
    # The page-level capture limit is the only place a user configures the
    # initial historical window.  The resolved bounds stay on the repository
    # as a durable sync baseline.
    time_window: TimeWindowRequest

class GitHubRepositoryStatusPayload(BaseModel):
    enabled: bool


class GitHubRepositoryBatchPayload(BaseModel):
    repository_ids: list[int] = Field(min_length=1)


def _connection_response(connection: DiscussionMailboxConnection) -> dict:
    return {
        "id": connection.id, "name": connection.name, "provider": connection.provider,
        "host": connection.host, "port": connection.port, "folder": connection.folder,
        "username_env_key": connection.username_env_key, "password_env_key": connection.password_env_key,
        "enabled": connection.enabled, "last_uid": connection.last_uid, "uidvalidity": connection.uidvalidity,
        "last_success_at": connection.last_success_at, "health_status": connection.health_status,
        "last_error": connection.last_error,
    }


def _pipeline_run_response(run: DiscussionPipelineRun) -> dict:
    return {
        "id": run.id,
        "trigger_type": run.trigger_type,
        "status": run.status,
        "source_ids": run.source_ids_json,
        "github_repository_ids": run.github_repository_ids_json,
        "result": run.result_json,
        "events": run.events_json or [],
        "result": run.result_json,
        "error_message": run.error_message,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _pipeline_run_state_response(run: DiscussionPipelineRun) -> dict:
    return {
        "id": run.id,
        "trigger_type": run.trigger_type,
        "status": run.status,
        "source_ids": run.source_ids_json,
        "github_repository_ids": run.github_repository_ids_json,
        "error_message": run.error_message,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _append_pipeline_event(
    run_id: str,
    stage: str,
    message: str,
    level: str = "info",
    *,
    provider: str = "organizer",
    source: str | None = None,
) -> None:
    """Persist a small, user-facing timeline entry without holding pipeline work open."""
    event_db = SessionLocal()
    try:
        run = event_db.get(DiscussionPipelineRun, run_id)
        if run is None:
            return
        occurred_at = datetime.now(timezone.utc)
        sequence = (event_db.scalar(
            select(func.max(DiscussionPipelineEvent.sequence)).where(DiscussionPipelineEvent.pipeline_run_id == run_id),
        ) or 0) + 1
        event_payload = {
            "at": occurred_at.isoformat(),
            "stage": stage,
            "level": level,
            "message": message,
            "provider": provider,
            "source": source,
        }
        event_db.add(DiscussionPipelineEvent(
            pipeline_run_id=run_id,
            sequence=sequence,
            occurred_at=occurred_at,
            stage=stage,
            level=level,
            message=message,
            provider=provider,
            source=source,
        ))
        events = list(run.events_json or [])
        events.append(event_payload)
        run.events_json = events[-120:]
        event_db.commit()
    except Exception:
        event_db.rollback()
        logger.exception("could not persist discussion pipeline event for %s", run_id)
    finally:
        event_db.close()


def _mark_pipeline_cancelled(db: Session, run_id: str, received: list[dict], github: list[dict]) -> None:
    db.rollback()
    run = db.get(DiscussionPipelineRun, run_id)
    if run is None:
        return
    run.status = "cancelled"
    run.result_json = {"received": received, "github": github}
    run.finished_at = datetime.now(timezone.utc)
    db.commit()
    if run.trigger_type in {"scheduled", "patrol_resend"}:
        from app.discussions.schedule import mark_run_finished

        mark_run_finished(db, status="cancelled")
    _append_pipeline_event(run_id, "任务已停止", "已停止当前技术探查；此前已提交的采集数据会保留。", "warning")


def _commit_pipeline_result(
    db: Session,
    run: DiscussionPipelineRun,
    run_id: str,
    result: dict,
    status: str,
) -> str:
    with _pipeline_cancel_lock:
        event = _pipeline_cancel_events.get(run_id)
        if event is not None and event.is_set():
            raise DiscussionPipelineCancelled()
        run.result_json = result
        run.status = status
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
    return status


def _enabled_mail_discussion_source_ids(db: Session) -> set[int]:
    """Return enabled mail-rule sources for an all-source scheduled run."""
    return set(db.scalars(
        select(DiscussionSourceRule.source_id)
        .join(Source, Source.id == DiscussionSourceRule.source_id)
        .where(
            DiscussionSourceRule.enabled.is_(True),
            Source.enabled.is_(True),
        )
    ))


def _execute_pipeline_run(run_id: str) -> None:
    """Run independently from the request and stop cooperatively between units of work."""
    db = SessionLocal()
    received: list[dict] = []
    github: list[dict] = []
    try:
        run = db.get(DiscussionPipelineRun, run_id)
        if run is None:
            return
        run_mail = run.trigger_type != "manual" or run.source_ids_json is not None
        run_github = run.trigger_type != "manual" or run.github_repository_ids_json is not None
        selected_source_ids = set(run.source_ids_json or [])
        selected_repository_ids = set(run.github_repository_ids_json or [])
        service = DiscussionService(db)
        threads = {"messages": 0, "threads": 0, "repaired": 0}
        platform_threads = {"threads": 0}

        if run_mail:
            # Scheduled runs collect every enabled mailbox source.  Include
            # those source IDs in the later grouping filter as well; otherwise
            # only GitHub source IDs are present and newly rebuilt mail trees
            # are silently excluded from candidate groups.
            if run.trigger_type != "manual":
                selected_source_ids.update(_enabled_mail_discussion_source_ids(db))
            connections = list(db.scalars(select(DiscussionMailboxConnection).where(DiscussionMailboxConnection.enabled.is_(True))))
            _append_pipeline_event(run_id, "任务启动", f"开始处理 {len(connections)} 个启用邮箱连接", provider="mail")
            for connection in connections:
                _raise_if_pipeline_stopped(run_id)
                try:
                    _append_pipeline_event(run_id, "收取邮件", f"正在扫描「{connection.name}」", provider="mail", source=connection.name)
                    receipt = {"connection_id": connection.id, "connection_name": connection.name, **service.receive(connection.id, should_stop=lambda: _is_pipeline_stop_requested(run_id))}
                    received.append(receipt)
                    _raise_if_pipeline_stopped(run_id)
                    _append_pipeline_event(run_id, "收取邮件", "「{name}」完成：扫描 {headers} 封，命中规则 {matched} 封，新增 {inserted} 封，重复 {duplicates} 封".format(name=connection.name, headers=receipt.get("headers", 0), matched=receipt.get("matched", 0), inserted=receipt.get("inserted", 0), duplicates=receipt.get("duplicates", 0)), "success", provider="mail", source=connection.name)
                except DiscussionPipelineCancelled:
                    raise
                except Exception as exc:
                    logger.exception("discussion mailbox connection %s failed", connection.id)
                    received.append({"connection_id": connection.id, "status": "error", "error": str(exc)[:1000]})
                    _append_pipeline_event(run_id, "收取邮件", f"「{connection.name}」失败：{str(exc)[:500]}", "error", provider="mail", source=connection.name)

        if run_github:
            repositories = list(db.scalars(select(DiscussionGithubRepository).where(DiscussionGithubRepository.enabled.is_(True))))
            repositories = [repository for repository in repositories if repository.id in selected_repository_ids] if run.trigger_type == "manual" else repositories
            selected_source_ids.update(repository.source_id for repository in repositories)
            _append_pipeline_event(run_id, "GitHub 拉取", f"开始同步 {len(repositories)} 个启用 GitHub 仓库", provider="github")
            github_service = GitHubDiscussionService(db, should_stop=lambda: _is_pipeline_stop_requested(run_id))
            for repository in repositories:
                _raise_if_pipeline_stopped(run_id)
                try:
                    _append_pipeline_event(run_id, "GitHub 拉取", f"正在同步「{repository.display_name}」", provider="github", source=repository.display_name)
                    github.append(github_service.sync_repository(repository, pipeline_run_id=run_id, progress=lambda stage, message, level: _append_pipeline_event(run_id, stage, message, level, provider="github", source=repository.display_name)))
                except GitHubSyncCancelled as exc:
                    raise DiscussionPipelineCancelled() from exc
                except Exception as exc:
                    logger.exception("GitHub discussion repository %s failed", repository.id)
                    github.append({"repository_id": repository.id, "repository": repository.display_name, "status": "error", "error": str(exc)[:1000]})
                    _append_pipeline_event(run_id, "GitHub 拉取", f"「{repository.display_name}」失败：{str(exc)[:500]}", "error", provider="github", source=repository.display_name)

        _raise_if_pipeline_stopped(run_id)
        if run_mail:
            _append_pipeline_event(run_id, "建立讨论树", "正在按邮件引用关系合并讨论")
            threads = service.rebuild_threads()
        if run_github:
            platform_threads = service.refresh_platform_threads()
        _append_pipeline_event(run_id, "建立讨论树", f"完成：{threads['messages']} 封匹配邮件归入 {threads['threads']} 个邮件树；GitHub 刷新 {platform_threads['threads']} 个原生讨论树", "success")
        processing_source_ids = selected_source_ids or None
        _raise_if_pipeline_stopped(run_id)
        _append_pipeline_event(run_id, "候选分组", "正在更新技术讨论候选组")
        groups = service.sync_candidate_groups(processing_source_ids)
        _append_pipeline_event(run_id, "候选分组", f"完成：新建 {groups['created']} 组，更新 {groups['updated']} 组", "success")
        _raise_if_pipeline_stopped(run_id)
        requeued = service.requeue_failed_groups(processing_source_ids)
        if requeued:
            _append_pipeline_event(run_id, "模型整理", f"已重试 {requeued} 个此前失败的讨论组", "warning")
        organized = DiscussionOrganizer(db).organize_pending(processing_source_ids, progress=lambda stage, message, level: _append_pipeline_event(run_id, stage, message, level), should_stop=lambda: _is_pipeline_stop_requested(run_id))
        _raise_if_pipeline_stopped(run_id)
        published_items = [{"item_id": item.id, "title": item.title, "group_id": group.id} for group, item in db.execute(select(DiscussionGroup, Item).join(Item, Item.id == DiscussionGroup.item_id).where(DiscussionGroup.processing_status == "published", DiscussionGroup.last_processed_at >= run.started_at).order_by(DiscussionGroup.last_processed_at.desc())).all()]
        run_status = _commit_pipeline_result(
            db,
            run,
            run_id,
            {"received": received, "github": github, "threads": threads, "groups": groups, "requeued": requeued, "organized": organized, "published_items": published_items},
            "succeeded" if not any(item.get("status") == "error" for item in [*received, *github]) else "partial",
        )
        if run.trigger_type in {"scheduled", "patrol_resend"}:
            from app.discussions.schedule import mark_run_finished

            mark_run_finished(db, status=run_status)
        _append_pipeline_event(run_id, "任务完成", f"已完成：发布 {organized['promoted']} 条技术讨论，等待后续回复 {organized['waiting']} 组，失败 {organized['failed']} 组", "success" if run_status == "succeeded" else "warning")
    except (DiscussionPipelineCancelled, DiscussionOrganizerCancelled):
        _mark_pipeline_cancelled(db, run_id, received, github)
    except Exception as exc:
        if _is_pipeline_stop_requested(run_id):
            _mark_pipeline_cancelled(db, run_id, received, github)
        else:
            logger.exception("discussion pipeline run %s failed", run_id)
            db.rollback()
            failed_run = db.get(DiscussionPipelineRun, run_id)
            if failed_run is not None:
                failed_run.status = "failed"
                failed_run.error_message = str(exc)[:3000]
                failed_run.finished_at = datetime.now(timezone.utc)
                db.commit()
                if failed_run.trigger_type in {"scheduled", "patrol_resend"}:
                    from app.discussions.schedule import mark_run_finished

                    mark_run_finished(db, status="failed")
            _append_pipeline_event(run_id, "任务失败", f"任务中断：{str(exc)[:500]}", "error")
    finally:
        _unregister_pipeline_run(run_id)
        db.close()


@router.get("/connections")
def list_connections(db: Session = Depends(get_db)) -> list[dict]:
    return [_connection_response(value) for value in db.scalars(select(DiscussionMailboxConnection).order_by(DiscussionMailboxConnection.name)).all()]


@router.post("/connections", status_code=201)
def create_connection(
    payload: ConnectionPayload,
    response: Response,
    db: Session = Depends(get_db),
    _: dict = Depends(require_system_access),
) -> dict:
    """Create or update the named singleton mailbox connection.

    The setup screen is deliberately safe to submit repeatedly: a browser
    refresh must never turn a correctly configured connection into a 409 error.
    """
    connection = db.scalar(
        select(DiscussionMailboxConnection).where(
            DiscussionMailboxConnection.name == payload.name.strip(),
        ),
    )
    if connection is None:
        connection = DiscussionMailboxConnection(provider="imap", **payload.model_dump())
        db.add(connection)
    else:
        for key, value in payload.model_dump().items():
            setattr(connection, key, value)
        response.status_code = 200
    db.commit()
    db.refresh(connection)
    return _connection_response(connection)


@router.put("/connections/{connection_id}")
def update_connection(connection_id: int, payload: ConnectionPayload, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> dict:
    connection = db.get(DiscussionMailboxConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="connection not found")
    for key, value in payload.model_dump().items():
        setattr(connection, key, value)
    db.commit()
    db.refresh(connection)
    return _connection_response(connection)


@router.get("/rules")
def list_rules(db: Session = Depends(get_db)) -> list[dict]:
    rules = db.scalars(
        select(DiscussionSourceRule)
        .where(DiscussionSourceRule.review_status != "deleted")
        .order_by(DiscussionSourceRule.source_id, DiscussionSourceRule.id),
    ).all()
    return [{"id": rule.id, "source_id": rule.source_id, "rule_type": rule.rule_type, "match_value": rule.match_value, "header_name": rule.header_name, "enabled": rule.enabled, "review_status": rule.review_status} for rule in rules]


@router.get("/sources")
def list_discussion_sources(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        select(Source, DiscussionSourceRule.review_status)
        .join(DiscussionSourceRule, DiscussionSourceRule.source_id == Source.id)
        .where(Source.stream == Stream.DISCUSSION.value, DiscussionSourceRule.review_status != "deleted")
        .order_by(Source.name, Source.id, DiscussionSourceRule.id),
    ).all()
    sources: dict[int, dict] = {}
    for source, review_status in rows:
        sources.setdefault(source.id, {"id": source.id, "name": source.name, "enabled": source.enabled, "review_status": review_status})
    return list(sources.values())


@router.post("/sources", status_code=201)
def create_discussion_source(
    payload: DiscussionSourcePayload,
    db: Session = Depends(get_db),
) -> dict:
    source = Source(
        name=payload.name.strip(),
        type="discussion_mail",
        # Source.url belongs to the common source model but an IMAP mailing list
        # has no fetch URL.  Matching is entirely determined by its rule.
        url="",
        stream=Stream.DISCUSSION.value,
        main_category="OS跟踪来源",
        enabled=False,
    )
    db.add(source)
    db.flush()
    rule = DiscussionSourceRule(
        source_id=source.id,
        rule_type=payload.rule_type,
        match_value=payload.match_value.strip(),
        header_name=payload.header_name.strip() if payload.header_name else None,
        enabled=False,
        review_status="pending",
    )
    db.add(rule)
    db.commit()
    db.refresh(source)
    db.refresh(rule)
    return {
        "id": source.id,
        "name": source.name,
        "enabled": source.enabled,
        "review_status": rule.review_status,
        "rule": {"id": rule.id, "rule_type": rule.rule_type, "match_value": rule.match_value},
    }


@router.post("/sources/review/approve")
def approve_discussion_sources(payload: DiscussionSourceBatchPayload, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> dict:
    source_ids = list(db.scalars(
        select(Source.id)
        .join(DiscussionSourceRule, DiscussionSourceRule.source_id == Source.id)
        .where(
            Source.id.in_(payload.source_ids),
            Source.stream == Stream.DISCUSSION.value,
            DiscussionSourceRule.review_status == "pending",
        )
        .distinct(),
    ))
    sources = [source for source_id in source_ids if (source := db.get(Source, source_id)) is not None]
    if source_ids:
        for source in sources:
            source.enabled = True
        for rule in db.scalars(select(DiscussionSourceRule).where(DiscussionSourceRule.source_id.in_(source_ids))):
            rule.enabled = True
            rule.review_status = "approved"
        db.commit()
    return {"approved_count": len(source_ids)}


@router.post("/sources/review/delete")
def delete_pending_discussion_sources(payload: DiscussionSourceBatchPayload, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> dict:
    source_ids = list(db.scalars(
        select(Source.id)
        .join(DiscussionSourceRule, DiscussionSourceRule.source_id == Source.id)
        .where(
            Source.id.in_(payload.source_ids),
            Source.stream == Stream.DISCUSSION.value,
            DiscussionSourceRule.review_status == "pending",
        )
        .distinct(),
    ))
    sources = [source for source_id in source_ids if (source := db.get(Source, source_id)) is not None]
    for source in sources:
        for rule in db.scalars(select(DiscussionSourceRule).where(DiscussionSourceRule.source_id == source.id)):
            db.delete(rule)
        db.delete(source)
    if sources:
        db.commit()
    return {"deleted_count": len(sources)}


@router.delete("/sources/{source_id}", status_code=204)
def delete_discussion_source(source_id: int, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> None:
    source = db.get(Source, source_id)
    if source is None or source.stream != Stream.DISCUSSION.value:
        raise HTTPException(status_code=404, detail="discussion source not found")
    # Received mail keeps a foreign-key association with its source.  Removing
    # it from the library therefore means disabling it and hiding its rules,
    # rather than breaking historical discussion evidence.
    source.enabled = False
    rules = list(db.scalars(select(DiscussionSourceRule).where(DiscussionSourceRule.source_id == source.id)))
    for rule in rules:
        rule.enabled = False
        rule.review_status = "deleted"
    db.commit()


@router.patch("/sources/{source_id}")
def update_discussion_source_status(
    source_id: int,
    payload: DiscussionSourceStatusPayload,
    db: Session = Depends(get_db),
    _: dict = Depends(require_system_access),
) -> dict:
    source = db.get(Source, source_id)
    if source is None or source.stream != Stream.DISCUSSION.value:
        raise HTTPException(status_code=404, detail="discussion source not found")
    rules = list(db.scalars(select(DiscussionSourceRule).where(DiscussionSourceRule.source_id == source.id)))
    if not rules or any(rule.review_status != "approved" for rule in rules):
        raise HTTPException(status_code=409, detail="discussion source is not approved")
    source.enabled = payload.enabled
    for rule in rules:
        rule.enabled = payload.enabled
    db.commit()
    return {"id": source.id, "enabled": source.enabled}


@router.post("/rules", status_code=201)
def create_rule(payload: RulePayload, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> dict:
    if db.get(Source, payload.source_id) is None:
        raise HTTPException(status_code=422, detail="source not found")
    rule = DiscussionSourceRule(**payload.model_dump())
    db.add(rule)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="duplicate source rule") from exc
    db.refresh(rule)
    return {"id": rule.id, "source_id": rule.source_id, "rule_type": rule.rule_type, "match_value": rule.match_value, "header_name": rule.header_name, "enabled": rule.enabled}


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, payload: RulePayload, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> dict:
    rule = db.get(DiscussionSourceRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    if db.get(Source, payload.source_id) is None:
        raise HTTPException(status_code=422, detail="source not found")
    for key, value in payload.model_dump().items():
        setattr(rule, key, value)
    db.commit()
    return {"id": rule.id, "source_id": rule.source_id, "rule_type": rule.rule_type, "match_value": rule.match_value, "header_name": rule.header_name, "enabled": rule.enabled}


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> None:
    rule = db.get(DiscussionSourceRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    db.delete(rule)
    db.commit()


def _github_repository_response(repository: DiscussionGithubRepository) -> dict:
    return {
        "id": repository.id,
        "source_id": repository.source_id,
        "owner": repository.owner,
        "repo": repository.repo,
        "display_name": repository.display_name,
        "token_env_key": repository.token_env_key,
        # Reveal only whether the named variable is available, never its value.
        "token_configured": bool(os.environ.get(repository.token_env_key)),
        "enabled": repository.enabled,
        "review_status": repository.review_status,
        "include_issues": repository.include_issues,
        "include_discussions": repository.include_discussions,
        "issue_label_allowlist": repository.issue_label_allowlist or [],
        "issue_label_blocklist": repository.issue_label_blocklist or [],
        "discussion_category_allowlist": repository.discussion_category_allowlist or [],
        "discussion_category_blocklist": repository.discussion_category_blocklist or [],
        "backfill_start_at": repository.backfill_start_at,
        "backfill_end_at": repository.backfill_end_at,
        "issue_watermark": {"updated_at": repository.issue_watermark_at, "external_id": repository.issue_watermark_external_id},
        "discussion_watermark": {"updated_at": repository.discussion_watermark_at, "external_id": repository.discussion_watermark_external_id},
        "last_success_at": repository.last_success_at,
        "last_error": repository.last_error,
        "rate_limit_reset_at": repository.rate_limit_reset_at,
    }


def _resolve_capture_window(time_window: TimeWindowRequest) -> tuple[datetime, datetime]:
    """Resolve a page-level capture limit to stable, UTC backfill bounds."""
    now = datetime.now(timezone.utc)
    if time_window.time_mode == "absolute":
        assert time_window.start_at is not None and time_window.end_at is not None
        return time_window.start_at.astimezone(timezone.utc), time_window.end_at.astimezone(timezone.utc)
    delta = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}[time_window.relative_range or "7d"]
    return now - delta, now


@router.get("/github/token-status")
def github_token_status(
    token_env_key: str = Query(min_length=1, max_length=120, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
) -> dict:
    """Check a credential reference without exposing its secret."""
    return {"token_env_key": token_env_key, "configured": bool(os.environ.get(token_env_key))}


@router.get("/github/repositories")
def list_github_repositories(db: Session = Depends(get_db)) -> list[dict]:
    return [_github_repository_response(value) for value in db.scalars(
        select(DiscussionGithubRepository).order_by(DiscussionGithubRepository.owner, DiscussionGithubRepository.repo),
    )]


@router.post("/github/repositories", status_code=201)
def create_github_repository(
    payload: GitHubRepositoryPayload,
    db: Session = Depends(get_db),
) -> dict:
    if not payload.include_issues and not payload.include_discussions:
        raise HTTPException(status_code=422, detail="至少选择 Issues 或 Discussions 其中一种内容")
    source = Source(
        name=payload.display_name.strip(), type="api", url=f"https://github.com/{payload.owner}/{payload.repo}",
        stream=Stream.DISCUSSION.value, enabled=False,
    )
    backfill_start_at, backfill_end_at = _resolve_capture_window(payload.time_window)
    repository = DiscussionGithubRepository(
        source_id=0,
        **payload.model_dump(exclude={"time_window"}),
        backfill_start_at=backfill_start_at,
        backfill_end_at=backfill_end_at,
    )
    db.add(source)
    db.flush()
    repository.source_id = source.id
    db.add(repository)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="GitHub 仓库已存在") from exc
    db.refresh(repository)
    return _github_repository_response(repository)


@router.post("/github/repositories/{repository_id}/estimate")
def estimate_github_backfill(repository_id: int, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> dict:
    repository = db.get(DiscussionGithubRepository, repository_id)
    if repository is None:
        raise HTTPException(status_code=404, detail="GitHub 仓库不存在")
    try:
        return GitHubDiscussionService(db).estimate_backfill(repository)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"无法读取 GitHub 预估：{str(exc)[:500]}") from exc


@router.post("/github/repositories/review/approve")
def approve_github_repositories(payload: GitHubRepositoryBatchPayload, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> dict:
    repositories = list(db.scalars(
        select(DiscussionGithubRepository).where(
            DiscussionGithubRepository.id.in_(payload.repository_ids),
            DiscussionGithubRepository.review_status == "pending",
        ),
    ))
    for repository in repositories:
        repository.enabled = True
        repository.review_status = "approved"
        source = db.get(Source, repository.source_id)
        if source is not None:
            source.enabled = True
    if repositories:
        db.commit()
    return {"approved_count": len(repositories)}


@router.post("/github/repositories/{repository_id}/approve")
def approve_github_repository(repository_id: int, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> dict:
    repository = db.get(DiscussionGithubRepository, repository_id)
    if repository is None:
        raise HTTPException(status_code=404, detail="GitHub 仓库不存在")
    repository.enabled = True
    repository.review_status = "approved"
    source = db.get(Source, repository.source_id)
    if source is not None:
        source.enabled = True
    db.commit()
    return _github_repository_response(repository)


def _delete_github_repository_record(db: Session, repository: DiscussionGithubRepository) -> None:
    """Delete connector state while preserving collected discussion evidence."""
    source = db.get(Source, repository.source_id)
    db.execute(delete(DiscussionGithubSyncRun).where(DiscussionGithubSyncRun.repository_id == repository.id))
    db.execute(delete(DiscussionMessageSource).where(DiscussionMessageSource.source_id == repository.source_id))
    db.execute(update(DiscussionMessage).where(DiscussionMessage.repository_id == repository.id).values(repository_id=None))
    if source is not None:
        db.execute(delete(ItemSource).where(ItemSource.source_id == source.id))
        db.execute(update(Item).where(Item.source_id == source.id).values(source_id=None))
    db.delete(repository)
    db.flush()
    if source is not None:
        db.delete(source)


@router.delete("/github/repositories/{repository_id}", status_code=204)
def delete_github_repository(repository_id: int, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> None:
    repository = db.get(DiscussionGithubRepository, repository_id)
    if repository is None:
        raise HTTPException(status_code=404, detail="GitHub 仓库不存在")
    if db.scalar(select(DiscussionPipelineRun.id).where(DiscussionPipelineRun.status.in_(_ACTIVE_PIPELINE_STATUSES))) is not None:
        raise HTTPException(status_code=409, detail="技术探查正在运行，请完成后再删除 GitHub 仓库")
    _delete_github_repository_record(db, repository)
    db.commit()


@router.post("/github/repositories/review/delete")
def delete_pending_github_repositories(payload: GitHubRepositoryBatchPayload, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> dict:
    if db.scalar(select(DiscussionPipelineRun.id).where(DiscussionPipelineRun.status.in_(_ACTIVE_PIPELINE_STATUSES))) is not None:
        raise HTTPException(status_code=409, detail="技术探查正在运行，请完成后再删除 GitHub 仓库")
    repositories = list(db.scalars(
        select(DiscussionGithubRepository).where(
            DiscussionGithubRepository.id.in_(payload.repository_ids),
            DiscussionGithubRepository.review_status == "pending",
        ),
    ))
    for repository in repositories:
        _delete_github_repository_record(db, repository)
    if repositories:
        db.commit()
    return {"deleted_count": len(repositories)}


@router.patch("/github/repositories/{repository_id}")
def update_github_repository_status(repository_id: int, payload: GitHubRepositoryStatusPayload, db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> dict:
    repository = db.get(DiscussionGithubRepository, repository_id)
    if repository is None:
        raise HTTPException(status_code=404, detail="GitHub 仓库不存在")
    if payload.enabled and repository.review_status != "approved":
        raise HTTPException(status_code=409, detail="请先确认历史回填范围后启用 GitHub 仓库")
    repository.enabled = payload.enabled
    source = db.get(Source, repository.source_id)
    if source is not None:
        source.enabled = payload.enabled
    db.commit()
    return _github_repository_response(repository)


def start_discussion_pipeline_run(
    db: Session,
    *,
    source_ids: list[int] | None = None,
    github_repository_ids: list[int] | None = None,
    trigger_type: str = "manual",
    run_in_background: bool = True,
) -> DiscussionPipelineRun:
    """Start the shared technical-discussion pipeline.

    Manual callers return immediately; the system scheduler can run the same
    pipeline synchronously to preserve its configured collection order.
    """
    running = db.scalar(select(DiscussionPipelineRun).where(DiscussionPipelineRun.status.in_(_ACTIVE_PIPELINE_STATUSES)))
    if running is not None:
        return running
    run = DiscussionPipelineRun(
        trigger_type=trigger_type,
        source_ids_json=source_ids,
        github_repository_ids_json=github_repository_ids,
        events_json=[{
            "at": datetime.now(timezone.utc).isoformat(),
            "stage": "任务已排队",
            "level": "info",
            "message": "系统每日收取任务已创建，等待后台开始处理" if trigger_type == "scheduled" else "收取与整理任务已创建，等待后台开始处理",
        }],
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    _register_pipeline_run(run.id)
    if run_in_background:
        threading.Thread(
            target=_execute_pipeline_run,
            args=(run.id,),
            name=f"discussion-pipeline-{run.id}",
            daemon=True,
        ).start()
    else:
        _execute_pipeline_run(run.id)
    return run


@router.post("/runs", status_code=202)
def run_discussion_pipeline(
    payload: DiscussionRunPayload,
    db: Session = Depends(get_db),
    _: dict = Depends(require_system_access),
) -> dict:
    if payload.channel == "mail" and (not payload.source_ids or payload.github_repository_ids):
        raise HTTPException(status_code=422, detail="邮件探查必须选择邮件列表，且不能同时选择 GitHub 仓库")
    if payload.channel == "github" and (not payload.github_repository_ids or payload.source_ids):
        raise HTTPException(status_code=422, detail="GitHub 探查必须选择 GitHub 仓库，且不能同时选择邮件列表")
    active_run = db.scalar(select(DiscussionPipelineRun).where(DiscussionPipelineRun.status.in_(_ACTIVE_PIPELINE_STATUSES)))
    if active_run is not None:
        active_channel = "邮件" if active_run.source_ids_json is not None and active_run.github_repository_ids_json is None else "GitHub" if active_run.github_repository_ids_json is not None and active_run.source_ids_json is None else "技术探查"
        raise HTTPException(status_code=409, detail=f"{active_channel}探查正在运行，请先停止或等待任务完成")
    run = start_discussion_pipeline_run(
        db,
        source_ids=payload.source_ids,
        github_repository_ids=payload.github_repository_ids,
        trigger_type="manual",
    )
    return _pipeline_run_response(run)


@router.post("/runs/{run_id}/stop")
def stop_discussion_pipeline_run(
    run_id: str,
    db: Session = Depends(get_db),
    _: dict = Depends(require_system_access),
) -> dict:
    run = db.get(DiscussionPipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="技术探查任务不存在")
    if run.status not in _ACTIVE_PIPELINE_STATUSES:
        raise HTTPException(status_code=409, detail="技术探查任务已结束，无法停止")
    if not _request_pipeline_stop(run_id):
        raise HTTPException(status_code=409, detail="技术探查任务不在当前服务进程中，无法安全停止")
    if run.status == "running":
        run.status = "stopping"
        db.commit()
        db.refresh(run)
    return _pipeline_run_response(run)


@router.get("/runs/latest")
def latest_discussion_pipeline_run(db: Session = Depends(get_db)) -> dict | None:
    run = db.scalar(select(DiscussionPipelineRun).order_by(DiscussionPipelineRun.started_at.desc()))
    return _pipeline_run_response(run) if run is not None else None


@router.get("/runs/{run_id}/events")
def discussion_pipeline_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict:
    run = db.get(DiscussionPipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="技术探查任务不存在")
    stored = list(db.scalars(
        select(DiscussionPipelineEvent)
        .where(DiscussionPipelineEvent.pipeline_run_id == run_id, DiscussionPipelineEvent.sequence > after)
        .order_by(DiscussionPipelineEvent.sequence)
        .limit(limit)
    ))
    if not stored:
        # Old runs predate the append-only stream.  Keep them inspectable while
        # newly created runs exclusively use the independent event chain.
        legacy = list(run.events_json or [])
        stored_payloads = [
            {"sequence": index, **event}
            for index, event in enumerate(legacy, start=1)
            if index > after
        ][:limit]
    else:
        stored_payloads = [
            {
                "sequence": event.sequence,
                "at": event.occurred_at.isoformat(),
                "stage": event.stage,
                "level": event.level,
                "message": event.message,
                "provider": event.provider,
                "source": event.source,
            }
            for event in stored
        ]
    return {
        "run": _pipeline_run_state_response(run),
        "events": stored_payloads,
        "next_after": stored_payloads[-1]["sequence"] if stored_payloads else after,
    }


@router.get("/schedule")
def get_discussion_schedule(db: Session = Depends(get_db), _: dict = Depends(require_system_access)) -> dict:
    from app.discussions.schedule import get_or_create_schedule_config, schedule_response

    return schedule_response(get_or_create_schedule_config(db))


@router.put("/schedule")
def update_discussion_schedule(
    payload: DiscussionSchedulePayload,
    db: Session = Depends(get_db),
    _: dict = Depends(require_system_access),
) -> dict:
    from app.discussions.schedule import schedule_response, update_schedule_config

    config = update_schedule_config(
        db,
        enabled=payload.enabled,
        run_time=payload.run_time,
        patrol_interval_hours=payload.patrol_interval_hours,
    )
    return schedule_response(config)


def _discussion_group_response(db: Session, group: DiscussionGroup, item: Item | None) -> dict:
    first_subject = db.scalar(
        select(DiscussionMessage.subject)
        .join(DiscussionGroupThread, DiscussionGroupThread.thread_id == DiscussionMessage.thread_id)
        .where(DiscussionGroupThread.group_id == group.id)
        .order_by(DiscussionMessage.sent_at, DiscussionMessage.id)
        .limit(1)
    )
    structured = group.structured_result or {}
    title = (
        item.title if item is not None else structured.get("canonical_title")
    ) or first_subject or f"未命名讨论树 #{group.id}"
    return {
        "id": group.id,
        "item_id": group.item_id,
        "title": title,
        "processing_status": group.processing_status,
        "activity_status": group.activity_status,
        "resolution_status": group.resolution_status,
        "message_count": group.message_count,
        "participant_count": group.participant_count,
        "pending_message_count": group.pending_message_count,
        "first_activity_at": group.first_activity_at,
        "last_activity_at": group.last_activity_at,
        "last_processed_at": group.last_processed_at,
        "rejection_reason": group.rejection_reason,
        "summary": structured.get("topic_summary") or structured.get("latest_progress"),
    }


@router.get("/groups/summary")
def discussion_group_counts(
    provider: Literal["mail", "github"] | None = None,
    db: Session = Depends(get_db),
) -> dict:
    query = (
        select(DiscussionGroup.processing_status, func.count(DiscussionGroup.id))
        .where(DiscussionGroup.hidden.is_(False))
        .group_by(DiscussionGroup.processing_status)
    )
    if provider:
        query = query.where(DiscussionGroup.id.in_(
            select(DiscussionGroupThread.group_id)
            .join(DiscussionMessage, DiscussionMessage.thread_id == DiscussionGroupThread.thread_id)
            .where(DiscussionMessage.provider == provider)
        ))
    rows = db.execute(query).all()
    counts_by_status = {status: count for status, count in rows}
    return {
        "all": sum(counts_by_status.values()),
        "published": counts_by_status.get("published", 0),
        "waiting": counts_by_status.get("waiting", 0),
    }


@router.get("/groups")
def list_discussion_groups(
    status: Literal["all", "published", "waiting"] = "all",
    provider: Literal["mail", "github"] | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    """Return persisted discussion-tree state, optionally scoped to one provider.

    A group is the durable unit that can later become a published discussion;
    initially each deterministic mail thread has one group.  Keeping this API
    independent from an individual run lets the control panel show old and
    newly collected trees together.
    """
    query = (
        select(DiscussionGroup, Item)
        .outerjoin(Item, Item.id == DiscussionGroup.item_id)
        .where(DiscussionGroup.hidden.is_(False))
        .order_by(DiscussionGroup.last_activity_at.desc().nullslast(), DiscussionGroup.id.desc())
    )
    if provider:
        query = query.where(DiscussionGroup.id.in_(
            select(DiscussionGroupThread.group_id)
            .join(DiscussionMessage, DiscussionMessage.thread_id == DiscussionGroupThread.thread_id)
            .where(DiscussionMessage.provider == provider)
        ))
    if status != "all":
        query = query.where(DiscussionGroup.processing_status == status)
    rows = db.execute(query.offset(offset).limit(limit + 1)).all()
    page_rows = rows[:limit]
    return {
        "groups": [_discussion_group_response(db, group, item) for group, item in page_rows],
        "next_offset": offset + len(page_rows) if len(rows) > limit else None,
    }


@router.get("/items/{item_id}")
def discussion_detail(item_id: int, db: Session = Depends(get_db)) -> dict:
    group = db.scalar(select(DiscussionGroup).where(DiscussionGroup.item_id == item_id))
    if group is None:
        raise HTTPException(status_code=404, detail="discussion not found")
    messages = list(db.scalars(
        select(DiscussionMessage)
        .join(DiscussionGroupThread, DiscussionGroupThread.thread_id == DiscussionMessage.thread_id)
        .where(DiscussionGroupThread.group_id == group.id)
        .order_by(DiscussionMessage.sent_at, DiscussionMessage.id)
    ))
    snapshots = db.scalars(select(DiscussionProgressSnapshot).where(DiscussionProgressSnapshot.group_id == group.id).order_by(DiscussionProgressSnapshot.created_at.desc())).all()
    return {
        "group_id": group.id, "activity_status": group.activity_status, "resolution_status": group.resolution_status,
        "message_count": group.message_count, "participant_count": group.participant_count,
        "pending_message_count": group.pending_message_count, "last_activity_at": group.last_activity_at,
        "structured_result": group.structured_result, "heat_score": group.heat_score,
        "messages": [{
            "id": message.id,
            "message_id": message.message_id,
            "provider": message.provider,
            "external_id": message.external_id,
            "external_parent_id": message.external_parent_id,
            "kind": message.message_kind,
            "public_url": message.public_url,
            "parent_message_id": message.parent_message_id,
            "thread_id": message.thread_id,
            "subject": message.subject,
            "author_name": message.author_name,
            "author_email": message.author_email,
            "platform_user_id": message.platform_user_id,
            "sent_at": message.sent_at,
            "updated_at": message.upstream_updated_at,
            "body_text": message.body_text,
            "authored_text": message.authored_text,
            "translated_body_text": message.translated_body_text,
            "translation_summary": message.translation_summary,
            "translation_phrase": message.translation_phrase,
            "translation_status": message.translation_status,
            "metadata": message.platform_metadata_json or {},
            "context_incomplete": message.context_incomplete,
        } for message in messages],
        "snapshots": [{"id": snapshot.id, "content_revision": snapshot.content_revision, "new_message_ids": snapshot.new_message_ids, "new_reply_count": snapshot.new_reply_count, "progress_summary": snapshot.progress_summary, "created_at": snapshot.created_at} for snapshot in snapshots],
    }
