"""新端点：/discovery/run（生成命）+ /discovery/methods/{id}/fetch（运行命）。

纯增量：不替换旧 /sources/discover，不接入常规新闻抓取主流程。
"""

import asyncio
from datetime import datetime, timezone
from enum import Enum
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import AliasChoices, BaseModel, Field, HttpUrl, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_system_access
from app.auth import verify_access_token

from app.discovery.cancel import request_cancel
from app.discovery.batch_queue import (
    enqueue_batch_item,
    get_discovery_batch_queue,
    queue_snapshot,
    serialize_queue_item,
)
from app.discovery.checkpoints import CheckpointStore
from app.discovery.events import append_discovery_event
from app.discovery.execution import run_method  # compatibility patch seam for existing callers
from app.discovery.loop.artifacts import find_existing_method
from app.discovery.loop.engine import cancel_website_loop_run, website_loop_timing_payload
from app.discovery.wechat_plugin import cancel_wechat_discovery_run, wechat_discovery_timing_payload
from app.discovery.multi_graph import start_multi_discovery_run, start_website_discovery_run
from app.discovery.runtime import DiscoveryCapacityExceeded
from app.discovery.recovery import create_resumed_run, dispatch_resumed_run_if_registered
from app.discovery.sandbox.capacity import get_sandbox_capacity_queue
from app.discovery.naming import (
    default_website_display_name,
    format_website_display_name,
    normalize_site_name,
)
from app.discovery.review import (
    REVIEW_APPROVED,
    REVIEW_PENDING,
    approve_methods,
    delete_method as delete_crawl_method,
    delete_methods,
    get_or_create_reminder_config,
    method_source_name,
    send_review_reminder_if_due,
)
from app.discovery.recipe_prepare import (
    _apply_fetch_limits,
    _attach_wechat_skip_keys,
    _ensure_wechat_history_article_enrich,
    _prepare_fetch_recipe,
)
from app.discovery.quality_audit import calculate_overall_score
from app.discovery.plugin.review import plugin_review_summary
from app.discovery.public_events import PUBLIC_EVENT_TYPES, public_discovery_event
from app.llm.client import LlmClient
from app.enums import TagKind
from app.models import (
    CrawlMethod,
    CrawlMethodDomain,
    CrawlMethodRun,
    DiscoveryQueueItem,
    DiscoveryRunEvent,
    DiscoveryPromptSet,
    DiscoveryMethodMigration,
    Item,
    ItemTag,
    MainCategory,
    SiteDiscoveryRun,
    Source,
    Tag,
)
from app.discovery.prompts import (
    DEFAULT_NAMING,
    STAGES,
    get_stage_defaults,
    resolve_prompt,
    validate_prompts,
)
from app.discovery.agent_budget import DiscoveryAgentBudget, normalize_agent_budget
from app.schemas import ManualNewsRunRequest

router = APIRouter(prefix="/discovery", tags=["discovery"])
logger = logging.getLogger(__name__)
SUGGEST_NAME_LLM_TIMEOUT_SECONDS = 5.0
# 站点命名 prompt（token 模版）；供 prompts.get_stage_defaults 引用，也是本模块默认。
_NAMING_PROMPT = DEFAULT_NAMING


class DiscoverRequest(BaseModel):
    url: HttpUrl
    force: bool = True  # 默认重新探查；新版本审核通过后再切换正式映射
    name: str | None = None  # 站点别名（选填，不填自动用域名）
    agent_budget: DiscoveryAgentBudget = Field(
        default_factory=DiscoveryAgentBudget,
        validation_alias=AliasChoices("agent_budget", "agentBudget"),
    )


class RouteType(str, Enum):
    WEBSITE = "website"
    WECHAT_SEARCH = "wechat_search"
    WECHAT_HISTORY = "wechat_history"
    INTERNAL_FORUM = "internal_forum"


class RouteSource(str, Enum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"


class MultiDiscoverRequest(BaseModel):
    input: str
    display_input: str | None = None
    force: bool = True
    name: str | None = None
    hints: dict[str, Any] | None = None
    selected_route_type: RouteType | None = None
    resolved_route_type: RouteType | None = None
    route_source: RouteSource = RouteSource.INFERRED
    agent_budget: DiscoveryAgentBudget = Field(
        default_factory=DiscoveryAgentBudget,
        validation_alias=AliasChoices("agent_budget", "agentBudget"),
    )


def _route_type_to_hints(route_type: RouteType | None) -> dict[str, Any]:
    if route_type == RouteType.WEBSITE:
        return {"source_kind": "website"}
    if route_type == RouteType.WECHAT_SEARCH:
        return {"source_kind": "wechat_search"}
    if route_type == RouteType.WECHAT_HISTORY:
        return {"source_kind": "wechat_history"}
    if route_type == RouteType.INTERNAL_FORUM:
        return {"source_kind": "internal_forum"}
    return {}


def require_discovery_authenticated_access(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Accept either a normal signed-in user or the system-admin login token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_access_token(authorization.removeprefix("Bearer ").strip())
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload


@router.post("/run")
def discover_run(body: DiscoverRequest, db: Session = Depends(get_db)):
    """从多源门面启动普通网站探查。

    功能：保持旧 HTTP 请求和重复检查行为，但不再进入旧 ``graph`` 模块。
    由谁调用：前端网站探查页。
    会调用谁：``check_existing_method`` 与 ``multi_graph.start_website_discovery_run``。
    """
    site_url = str(body.url)
    if not body.force:
        existing = find_existing_method(site_url, db)
        if existing:
            return {"status": "duplicate", "existing_method": existing}
    # 别名：前端选填，不填自动用"网站：..."命名
    name = body.name or default_website_display_name(site_url)
    try:
        run_id = start_website_discovery_run(
            site_url,
            force=body.force,
            name=name,
            agent_budget=body.agent_budget.snapshot(),
        )
    except DiscoveryCapacityExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {
        "status": "started",
        "run_id": run_id,
        "name": name,
        "viewer_token": _issue_discovery_viewer_token(db, run_id),
    }


@router.post("/multi-run")
def discover_multi_run(
    body: MultiDiscoverRequest,
    db: Session = Depends(get_db),
):
    effective_route = body.resolved_route_type or body.selected_route_type
    hints = _route_type_to_hints(effective_route)

    # Merge explicit hints with any user-provided hints
    merged_hints: dict[str, Any] = {**hints}
    if body.hints:
        merged_hints.update(body.hints)

    try:
        result = start_multi_discovery_run(
            body.input,
            force=body.force,
            name=body.name,
            hints=merged_hints,
            selected_route_type=effective_route.value if effective_route else None,
            route_source=body.route_source.value,
            agent_budget=body.agent_budget.snapshot(),
        )
    except DiscoveryCapacityExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    # Attach resolved route metadata to response
    result["resolved_route_type"] = effective_route.value if effective_route else None
    result["route_source"] = body.route_source.value
    run_id = result.get("run_id")
    if result.get("status") == "started" and isinstance(run_id, int):
        result["viewer_token"] = _issue_discovery_viewer_token(db, run_id)
    return result


def _batch_queue_payload(body: MultiDiscoverRequest) -> dict[str, Any]:
    """Translate the public request model into the queue module's small interface."""
    return {
        "input": body.input,
        "display_input": body.display_input,
        "force": body.force,
        "name": body.name,
        "selected_route_type": body.selected_route_type.value if body.selected_route_type else None,
        "resolved_route_type": body.resolved_route_type.value if body.resolved_route_type else None,
        "route_source": body.route_source.value,
        "agent_budget": body.agent_budget.snapshot(),
    }


@router.get("/queue")
def get_discovery_queue(db: Session = Depends(get_db)):
    """Return the shared pending and retry lanes for the batch Discovery UI."""
    return queue_snapshot(db)


@router.post("/queue")
def enqueue_discovery_queue(
    body: MultiDiscoverRequest,
    db: Session = Depends(get_db),
):
    """Add one user-entered target; the queue dispatcher starts it when a slot opens."""
    status, result = enqueue_batch_item(db, _batch_queue_payload(body))
    if status == "queue_duplicate":
        raise HTTPException(409, "该入口链接已在探查队列中（包括失败队列），请先处理原任务")
    if status == "duplicate":
        return {"status": "duplicate", "existing_method": result}
    return {"status": "queued", "item": serialize_queue_item(result)}


@router.post("/queue/{item_id}/requeue")
def requeue_discovery_item(
    item_id: int,
    db: Session = Depends(get_db),
):
    """Return one failed target to the tail of the unprocessed lane."""
    item = db.get(DiscoveryQueueItem, item_id)
    if item is None:
        raise HTTPException(404, "batch Discovery queue item not found")
    if item.status != "failed":
        raise HTTPException(409, "only failed Discovery items can be requeued")
    item.status = "queued"
    item.run_id = None
    item.error_message = None
    item.started_at = None
    item.completed_at = None
    db.commit()
    get_discovery_batch_queue().wake()
    return queue_snapshot(db)


def _cancel_queued_run(run_id: int) -> None:
    """Signal every supported Discovery route without introducing a new execution path."""
    request_cancel(run_id)
    cancel_website_loop_run(run_id)
    cancel_wechat_discovery_run(run_id)


@router.delete("/queue/{item_id}")
def delete_discovery_queue_item(
    item_id: int,
    db: Session = Depends(get_db),
    _system_access: dict = Depends(require_system_access),
):
    """Remove an unstarted/failed entry; deleting an active item also requests cancellation."""
    item = db.get(DiscoveryQueueItem, item_id)
    if item is None:
        raise HTTPException(404, "batch Discovery queue item not found")
    run_id = item.run_id
    if item.status in {"starting", "running", "cancelling"}:
        item.status = "cancelling"
        item.delete_requested = True
    else:
        db.delete(item)
    db.commit()
    if run_id is not None:
        _cancel_queued_run(run_id)
    return queue_snapshot(db)


@router.post("/queue/stop-all")
def stop_discovery_queue(
    db: Session = Depends(get_db),
    _system_access: dict = Depends(require_system_access),
):
    """Stop all active batch work and move it to the retry lane, leaving queued work intact."""
    active = list(db.scalars(
        select(DiscoveryQueueItem).where(
            DiscoveryQueueItem.status.in_(("starting", "running", "cancelling"))
        )
    ))
    run_ids = [item.run_id for item in active if item.run_id is not None]
    for item in active:
        item.status = "cancelling"
        item.error_message = None
    if active:
        db.commit()
    for run_id in run_ids:
        _cancel_queued_run(run_id)
    get_discovery_batch_queue().wake()
    return queue_snapshot(db)


@router.get("/runs")
def list_discovery_runs(limit: int = 20, db: Session = Depends(get_db)):
    """列出生成命历史（最近 limit 条），按 started_at 倒序。"""
    runs = db.scalars(
        select(SiteDiscoveryRun).order_by(SiteDiscoveryRun.started_at.desc()).limit(limit)
    ).all()
    return [_serialize_discovery_run(r, include_trace=False, db=db) for r in runs]


def _discovery_queue_position(run_id: int) -> int | None:
    queue = get_sandbox_capacity_queue()
    for job_id in (f"discovery-{run_id}", f"repair-{run_id}", str(run_id)):
        position = queue.queue_position(job_id)
        if position is not None:
            return position
    waiting = tuple(queue.snapshot().get("waiting") or ())
    prefix = f"discovery-{run_id}-"
    for index, job_id in enumerate(waiting, start=1):
        if str(job_id).startswith(prefix):
            return index
    return None


def _serialize_discovery_run(
    run: SiteDiscoveryRun,
    *,
    include_trace: bool,
    db: Session | None = None,
) -> dict[str, Any]:
    """Keep every historical response field and append optional Loop metadata."""
    current_step = run.node_trace[-1].get("step") if run.node_trace else None
    now = datetime.now(timezone.utc)
    active_started_at = None
    capacity_is_active = False
    latest_capacity_event = None
    already_elapsed_seconds = 0.0
    maximum_seconds = 1800.0
    if db is not None:
        latest_capacity_event = db.scalars(
            select(DiscoveryRunEvent).where(
                DiscoveryRunEvent.run_id == run.id,
                DiscoveryRunEvent.event_type.in_((
                    "sandbox_capacity_queued",
                    "sandbox_capacity_acquired",
                )),
            ).order_by(DiscoveryRunEvent.sequence.desc()).limit(1)
        ).first()
        if latest_capacity_event is not None:
            payload = latest_capacity_event.payload or {}
            capacity_is_active = latest_capacity_event.event_type == "sandbox_capacity_acquired"
            active_started_at = latest_capacity_event.created_at if capacity_is_active else None
            snapshot_key = "already_elapsed_seconds" if capacity_is_active else "elapsed_snapshot"
            already_elapsed_seconds = float(payload.get(snapshot_key) or 0.0)
            maximum_seconds = min(
                1800.0,
                max(
                    0.1,
                    float(payload.get("maximum_seconds") or 1800.0),
                ),
            )
    if active_started_at is None and latest_capacity_event is None and run.checkpoint_path:
        try:
            checkpoint = CheckpointStore().load(run.checkpoint_path)
            already_elapsed_seconds = min(1800.0, max(0.0, checkpoint.elapsed_seconds))
        except (OSError, ValueError):
            already_elapsed_seconds = 0.0
    ended_at = run.ended_at
    elapsed_seconds = max(0, int(already_elapsed_seconds))
    if capacity_is_active and active_started_at is not None:
        if active_started_at.tzinfo is None:
            active_started_at = active_started_at.replace(tzinfo=timezone.utc)
        effective_end = ended_at or now
        if effective_end.tzinfo is None:
            effective_end = effective_end.replace(tzinfo=timezone.utc)
        elapsed_seconds = max(
            0,
            int(already_elapsed_seconds + (effective_end - active_started_at).total_seconds()),
        )
    review_status = None
    if db is not None and run.resulting_method_id is not None:
        review_status = db.scalar(
            select(CrawlMethod.review_status).where(CrawlMethod.id == run.resulting_method_id)
        )
    result: dict[str, Any] = {
        "id": run.id,
        "site_url": run.site_url,
        "status": run.status,
        "resulting_method_id": run.resulting_method_id,
        "llm_token_usage": run.llm_token_usage,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "error_message": run.error_message,
        "trigger_type": run.trigger_type,
        "phase": run.phase,
        "round": run.round,
        "queue_position": _discovery_queue_position(run.id),
        "runtime_version": run.runtime_version,
        "agent_budget": normalize_agent_budget(run.agent_budget),
        "repair_method_id": run.repair_method_id,
        "source_kind": run.source_kind,
        "review_status": review_status,
        "elapsed_seconds": elapsed_seconds,
        "remaining_seconds": max(0, int(maximum_seconds - elapsed_seconds)),
    }
    if include_trace:
        result.update(
            {
                "node_trace": run.node_trace,
                "retry_count": run.retry_count,
                "current_step": current_step,
            }
        )
    return result


def _serialize_discovery_run_for_ui(run: SiteDiscoveryRun, *, db: Session) -> dict[str, Any]:
    """Return phase-driven UI state without leaking raw trace/evidence payloads."""
    result = _serialize_discovery_run(run, include_trace=False, db=db)
    result.update(
        {
            "node_trace": [],
            "retry_count": run.retry_count,
            "current_step": run.phase,
            "error_message": (
                "已手动取消"
                if run.status == "cancelled"
                else "探查失败，请重新放入队列"
                if run.status in {"failed", "interrupted"}
                else None
            ),
        }
    )
    return result


_DISCOVERY_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
_SSE_PAGE_SIZE = 200
_SSE_POLL_SECONDS = 0.25
_SSE_HEARTBEAT_SECONDS = 15.0
_SSE_CONNECTION_SLOTS = threading.BoundedSemaphore(value=32)


class _SseSlotLease:
    """Release one acquired stream slot at most once on every response path."""

    def __init__(self) -> None:
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        _SSE_CONNECTION_SLOTS.release()


class _LeaseStreamingResponse(StreamingResponse):
    """Release the stream lease even when ASGI send/receive fails before iteration."""

    def __init__(self, *args: Any, lease: _SseSlotLease, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lease = lease

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._lease.release()


def _parse_event_cursor(last_event_id: str | None, cursor: int | None) -> int:
    """Resolve reconnect cursors without allowing a client to replay before either cursor."""
    header_cursor = 0
    if last_event_id:
        try:
            header_cursor = int(last_event_id.strip())
        except ValueError as exc:
            raise HTTPException(422, "Last-Event-ID must be a non-negative integer") from exc
        if header_cursor < 0:
            raise HTTPException(422, "Last-Event-ID must be a non-negative integer")
    return max(header_cursor, cursor or 0)


def _hash_discovery_viewer_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_discovery_viewer_token(db: Session, run_id: int) -> str:
    viewer_token = secrets.token_urlsafe(32)
    run = db.get(SiteDiscoveryRun, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    run.viewer_token_hash = _hash_discovery_viewer_token(viewer_token)
    db.commit()
    return viewer_token


def _has_system_access(authorization: str | None) -> bool:
    if not authorization or not authorization.startswith("Bearer "):
        return False
    payload = verify_access_token(authorization.removeprefix("Bearer ").strip())
    return payload is not None and payload.get("role") in {"system_admin", "admin"}


def _is_batch_discovery_run(db: Session, run_id: int) -> bool:
    return db.scalar(
        select(DiscoveryQueueItem.id)
        .where(DiscoveryQueueItem.run_id == run_id)
        .limit(1)
    ) is not None


def _authorize_discovery_run_view(
    db: Session,
    run: SiteDiscoveryRun,
    *,
    viewer_token: str | None,
    authorization: str | None,
    allow_shared_batch: bool = False,
) -> None:
    """Authorize a run view; shared queue projections are public and redacted."""
    if _has_system_access(authorization):
        return
    if allow_shared_batch and _is_batch_discovery_run(db, run.id):
        return
    if viewer_token and run.viewer_token_hash and hmac.compare_digest(
        run.viewer_token_hash,
        _hash_discovery_viewer_token(viewer_token),
    ):
        return
    raise HTTPException(403, "Discovery run access denied")


def _authorize_discovery_event_stream(
    run_id: int,
    viewer_token: str | None,
    authorization: str | None,
) -> None:
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        run = db.get(SiteDiscoveryRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        _authorize_discovery_run_view(
            db,
            run,
            viewer_token=viewer_token,
            authorization=authorization,
            allow_shared_batch=True,
        )
    finally:
        db.close()


def _discovery_event_page(run_id: int, after_sequence: int) -> tuple[list[dict[str, Any]], str]:
    """Load one bounded, already-redacted replay page in an isolated DB session."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        run = db.get(SiteDiscoveryRun, run_id)
        if run is None:
            return [], "missing"
        rows = list(db.scalars(
            select(DiscoveryRunEvent)
            .where(
                DiscoveryRunEvent.run_id == run_id,
                DiscoveryRunEvent.sequence > max(0, after_sequence),
                DiscoveryRunEvent.event_type.in_(PUBLIC_EVENT_TYPES),
            )
            .order_by(DiscoveryRunEvent.sequence.asc())
            .limit(_SSE_PAGE_SIZE)
        ))
        events = [
            projected
            for event in rows
            if (projected := public_discovery_event(event)) is not None
        ]
        return events, run.status
    finally:
        db.close()


@router.get("/runs/{run_id}/events")
async def stream_discovery_run_events(
    run_id: int,
    request: Request,
    cursor: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    viewer_token: str | None = Header(default=None, alias="X-Discovery-Viewer-Token"),
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    """Replay persisted redacted events for the initiating browser or a system admin."""
    await asyncio.to_thread(
        _authorize_discovery_event_stream,
        run_id,
        viewer_token,
        authorization,
    )
    initial_cursor = _parse_event_cursor(last_event_id, cursor)
    if not _SSE_CONNECTION_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "too many active Discovery event streams")
    lease = _SseSlotLease()
    try:
        initial_events, initial_status = await asyncio.to_thread(
            _discovery_event_page, run_id, initial_cursor
        )
        if initial_status == "missing":
            raise HTTPException(404, "run not found")
    except BaseException:
        lease.release()
        raise

    async def event_stream() -> AsyncIterator[str]:
        current = initial_cursor
        pending = initial_events
        status = initial_status
        heartbeat_at = time.monotonic() + _SSE_HEARTBEAT_SECONDS
        try:
            while True:
                if await request.is_disconnected():
                    return
                if not pending:
                    pending, status = await asyncio.to_thread(
                        _discovery_event_page, run_id, current
                    )
                if pending:
                    for event in pending:
                        sequence = int(event["sequence"])
                        if sequence <= current:
                            continue
                        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                        yield f"id: {sequence}\nevent: discovery\ndata: {data}\n\n"
                        current = sequence
                    pending = []
                    heartbeat_at = time.monotonic() + _SSE_HEARTBEAT_SECONDS
                    continue
                if status in _DISCOVERY_TERMINAL_STATUSES:
                    end_data = json.dumps(
                        {"cursor": current, "status": status},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"event: end\ndata: {end_data}\n\n"
                    return
                if time.monotonic() >= heartbeat_at:
                    yield ": heartbeat\n\n"
                    heartbeat_at = time.monotonic() + _SSE_HEARTBEAT_SECONDS
                await asyncio.sleep(_SSE_POLL_SECONDS)
        except asyncio.CancelledError:
            return
        finally:
            lease.release()

    try:
        return _LeaseStreamingResponse(
            event_stream(),
            lease=lease,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except BaseException:
        lease.release()
        raise


@router.get("/runs/{run_id}")
def get_discovery_run(
    run_id: int,
    db: Session = Depends(get_db),
    viewer_token: str | None = Header(default=None, alias="X-Discovery-Viewer-Token"),
    authorization: str | None = Header(default=None),
):
    """单 run 详情/轮询：含 node_trace 逐步轨迹 + current_step（前端高亮"进行到哪一步了"）。"""
    r = db.get(SiteDiscoveryRun, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    # Preserve the established single-run detail contract.  Shared batch
    # work is different: its raw trace may only be read by system access, and
    # ordinary observers use the projected /batch-ui endpoint above.
    if _is_batch_discovery_run(db, run_id):
        _authorize_discovery_run_view(
            db,
            r,
            viewer_token=viewer_token,
            authorization=authorization,
        )
    return _serialize_discovery_run(r, include_trace=True, db=db)


@router.get("/runs/{run_id}/batch-ui")
def get_batch_discovery_run_ui(
    run_id: int,
    db: Session = Depends(get_db),
    viewer_token: str | None = Header(default=None, alias="X-Discovery-Viewer-Token"),
    authorization: str | None = Header(default=None),
):
    """Safe phase-driven state for the shared batch queue observer panel."""
    r = db.get(SiteDiscoveryRun, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    _authorize_discovery_run_view(
        db,
        r,
        viewer_token=viewer_token,
        authorization=authorization,
        allow_shared_batch=True,
    )
    return _serialize_discovery_run_for_ui(r, db=db)


@router.post("/runs/{run_id}/cancel")
def cancel_discovery_run(
    run_id: int,
    db: Session = Depends(get_db),
    viewer_token: str | None = Header(default=None, alias="X-Discovery-Viewer-Token"),
    authorization: str | None = Header(default=None),
):
    """手动取消生成命：停止后续节点/LLM/API 调用，并把状态标为 cancelled。"""
    r = db.get(SiteDiscoveryRun, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    if _is_batch_discovery_run(db, run_id):
        if not _has_system_access(authorization):
            raise HTTPException(403, "system access required to cancel batch Discovery work")
    if r.status not in {"queued", "running", "repairing"}:
        return _serialize_discovery_run(r, include_trace=True, db=db)
    timing_payload = (
        wechat_discovery_timing_payload(run_id)
        if r.source_kind == "wechat"
        else website_loop_timing_payload(run_id)
        if r.source_kind == "website"
        else {}
    )
    cancelled = db.execute(
        update(SiteDiscoveryRun)
        .where(
            SiteDiscoveryRun.id == run_id,
            SiteDiscoveryRun.status.in_(("queued", "running", "repairing")),
        )
        .values(
            status="cancelled",
            error_message="已手动取消",
            ended_at=datetime.now(timezone.utc),
        )
    )
    if cancelled.rowcount == 1:
        append_discovery_event(
            run_id,
            event_type="run_cancelled",
            summary="用户已取消智能探查。",
            phase=r.phase,
            round_number=r.round,
            level="warning",
            payload=timing_payload,
            session=db,
        )
    db.commit()
    db.expire_all()
    r = db.get(SiteDiscoveryRun, run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    if cancelled.rowcount != 1:
        return _serialize_discovery_run(r, include_trace=True, db=db)
    request_cancel(run_id)
    cancel_website_loop_run(run_id)
    cancel_wechat_discovery_run(run_id)
    return _serialize_discovery_run(r, include_trace=True, db=db)


@router.post("/runs/{run_id}/resume")
def resume_discovery_run(
    run_id: int,
    db: Session = Depends(get_db),
    _system_access: dict = Depends(require_system_access),
):
    """Create a new queued run from a validated checkpoint; never calls legacy Graph."""
    try:
        resumed = create_resumed_run(run_id, session=db)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    dispatch_resumed_run_if_registered(resumed.id)
    db.expire_all()
    refreshed = db.get(SiteDiscoveryRun, resumed.id)
    return _serialize_discovery_run(refreshed or resumed, include_trace=True, db=db)


class MethodPatch(BaseModel):
    status: str | None = None  # active | disabled | failed

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str | None) -> str | None:
        if value is not None and value not in {"active", "inactive", "disabled", "failed"}:
            raise ValueError("unsupported method status")
        return value


class MethodReviewBatchRequest(BaseModel):
    method_ids: list[int]
    low_frequency_exception_reason: str | None = None


class ReviewReminderUpdateRequest(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None
    recipients: list[str] | None = None

    @field_validator("recipients", mode="before")
    @classmethod
    def _validate_recipients(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        from app.mail.validation import normalize_recipients

        return normalize_recipients(value if isinstance(value, list) else [])


class MigrationRegisterRequest(BaseModel):
    legacy_method_id: int
    plugin_method_id: int


class MigrationExplanationRequest(BaseModel):
    explanation: str = Field(min_length=10, max_length=4000)


class MigrationRollbackRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


def _method_quality_fields(method: CrawlMethod) -> dict[str, Any]:
    overall_score = _method_overall_score(method)
    return {
        "overall_score": overall_score,
        "quality_score": method.quality_score,
        "quality_grade": _method_quality_grade(overall_score),
        "quality_reason": method.quality_reason,
        "quality_sample_count": method.quality_sample_count,
        "density_score": method.density_score,
        "density_daily_avg": method.density_daily_avg,
        "density_weekly_avg": method.density_weekly_avg,
        "quality_audit_status": method.quality_audit_status,
        "quality_audited_at": method.quality_audited_at.isoformat() if method.quality_audited_at else None,
    }


def _method_overall_score(method: CrawlMethod) -> int | None:
    if method.quality_score is None:
        return method.overall_score
    density_score = method.density_score if method.density_score is not None else method.quality_score
    return calculate_overall_score(method.quality_score, density_score)


def _method_quality_grade(overall_score: int | None) -> str | None:
    if overall_score is None:
        return None
    if overall_score >= 85:
        return "A"
    if overall_score >= 70:
        return "B"
    if overall_score >= 50:
        return "C"
    return "D"


def _method_response(method: CrawlMethod, db: Session) -> dict[str, Any]:
    is_plugin = (method.dsl_recipe or {}).get("recipe_type") == "python_plugin"
    return {
        "id": method.id,
        "source_id": method.source_id,
        "domain": method.domain,
        "entry_url": method.entry_url,
        "status": method.status,
        "review_status": method.review_status,
        "reviewed_at": method.reviewed_at.isoformat() if method.reviewed_at else None,
        "reviewed_by": None if is_plugin else method.reviewed_by,
        "review_note": None if is_plugin else method.review_note,
        "source_name": method_source_name(db, method),
        "signature": method.signature,
        "last_run_at": method.last_run_at.isoformat() if method.last_run_at else None,
        "last_run_status": method.last_run_status,
        "created_at": method.created_at.isoformat() if method.created_at else None,
        "plugin_review": plugin_review_summary(method),
        **_method_quality_fields(method),
    }


def _reject_packaging_method_ids(db: Session, method_ids: list[int]) -> None:
    if method_ids and db.scalar(
        select(CrawlMethod.id).where(
            CrawlMethod.id.in_(method_ids),
            CrawlMethod.status == "packaging",
        )
    ) is not None:
        raise HTTPException(409, "method packaging is not available for review")


def _reject_duplicate_review_domains(db: Session, method_ids: list[int]) -> None:
    domains = list(db.scalars(select(CrawlMethod.domain).where(CrawlMethod.id.in_(method_ids))))
    if len(domains) != len(set(domains)):
        raise HTTPException(409, "one approval batch cannot contain multiple versions of the same entry URL")


def _reminder_config_response(config) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "interval_minutes": config.interval_minutes,
        "recipients": list(config.recipients_json or []),
        "last_sent_at": config.last_sent_at.isoformat() if config.last_sent_at else None,
        "last_result_status": config.last_result_status,
        "last_error": config.last_error,
    }


@router.get("/methods")
def list_methods(db: Session = Depends(get_db)):
    """列出所有已发现的爬取方式（摘要，不含完整 DSL Recipe）。"""
    ms = db.scalars(
        select(CrawlMethod)
        .where(
            CrawlMethod.review_status == REVIEW_APPROVED,
            CrawlMethod.status != "packaging",
        )
        .order_by(CrawlMethod.id.desc())
    ).all()
    return [_method_response(m, db) for m in ms]


@router.get("/methods/review-pending")
def list_pending_review_methods(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    """列出待审核爬取方式。"""
    ms = db.scalars(
        select(CrawlMethod)
        .where(
            CrawlMethod.review_status == REVIEW_PENDING,
            CrawlMethod.status != "packaging",
        )
        .order_by(CrawlMethod.created_at.desc(), CrawlMethod.id.desc())
    ).all()
    return [_method_response(m, db) for m in ms]


@router.post("/methods/review/approve")
def approve_pending_methods(
    body: MethodReviewBatchRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    reviewer = str(_access.get("sub") or _access.get("email") or "system_admin")
    try:
        count = approve_methods(
            db,
            body.method_ids,
            reviewer=reviewer,
            low_frequency_exception_reason=body.low_frequency_exception_reason,
        )
    except (LookupError, ValueError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        from app.discovery.domain_transition import MigrationBusyError

        if isinstance(exc, MigrationBusyError):
            db.rollback()
            raise HTTPException(503, {"message": str(exc), "retryable": True}) from exc
        raise
    return {"approved_count": count}


@router.post("/methods/review/delete")
def delete_pending_methods(
    body: MethodReviewBatchRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    try:
        count = delete_methods(db, body.method_ids)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        from app.discovery.domain_transition import MigrationBusyError

        if isinstance(exc, MigrationBusyError):
            db.rollback()
            raise HTTPException(503, {"message": str(exc), "retryable": True}) from exc
        raise
    return {"deleted_count": count}


@router.get("/methods/review/reminder")
def get_review_reminder_config(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    return _reminder_config_response(get_or_create_reminder_config(db))


@router.put("/methods/review/reminder")
def update_review_reminder_config(
    body: ReviewReminderUpdateRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    config = get_or_create_reminder_config(db)
    if body.enabled is not None:
        config.enabled = body.enabled
    if body.interval_minutes is not None:
        config.interval_minutes = max(5, min(body.interval_minutes, 10080))
    if body.recipients is not None:
        config.recipients_json = list(body.recipients)
    config.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(config)
    return _reminder_config_response(config)


@router.post("/methods/review/reminder/send-now")
def send_review_reminder_now(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    return send_review_reminder_if_due(db, force=True)


@router.get("/migrations")
def list_discovery_migrations(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    from app.discovery.migration import refresh_migration_eligibility, serialize_migration

    refresh_migration_eligibility(db)
    migrations = list(db.scalars(
        select(DiscoveryMethodMigration)
        .order_by(DiscoveryMethodMigration.id)
        .offset(offset)
        .limit(limit)
    ))
    return [serialize_migration(db, migration, include_evidence=False) for migration in migrations]


@router.get("/migrations/cleanup-readiness")
def get_legacy_cleanup_readiness(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    """Expose the fail-closed gate before removing migration-only runtimes."""
    from app.discovery.migration import legacy_cleanup_readiness

    return legacy_cleanup_readiness(db)


@router.post("/migrations/register")
def register_discovery_migration(
    body: MigrationRegisterRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    from app.discovery.migration import register_migration, serialize_migration

    try:
        migration = register_migration(
            db,
            legacy_method_id=body.legacy_method_id,
            plugin_method_id=body.plugin_method_id,
        )
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        from app.discovery.domain_transition import MigrationBusyError

        if isinstance(exc, MigrationBusyError):
            db.rollback()
            raise HTTPException(503, {"message": str(exc), "retryable": True}) from exc
        raise
    return serialize_migration(db, migration, include_evidence=False)


@router.get("/migrations/{migration_id}")
def get_discovery_migration(
    migration_id: int,
    include_evidence: bool = Query(default=False),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    from app.discovery.migration import serialize_migration

    migration = db.get(DiscoveryMethodMigration, migration_id)
    if migration is None:
        raise HTTPException(404, "migration not found")
    return serialize_migration(db, migration, include_evidence=include_evidence)


@router.post("/migrations/{migration_id}/shadow", status_code=202)
def run_discovery_migration_shadow(
    migration_id: int,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    from app.discovery.domain_transition import MigrationBusyError
    from app.discovery.migration import enqueue_shadow_comparison, serialize_comparison

    try:
        comparison = enqueue_shadow_comparison(db, migration_id)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except MigrationBusyError as exc:
        db.rollback()
        raise HTTPException(503, {"message": str(exc), "retryable": True}) from exc
    return serialize_comparison(comparison, include_evidence=False)


@router.get("/migrations/{migration_id}/comparisons/{comparison_id}")
def get_discovery_migration_comparison(
    migration_id: int,
    comparison_id: int,
    include_evidence: bool = Query(default=False),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    from app.discovery.migration import serialize_comparison
    from app.models import DiscoveryMigrationComparison

    comparison = db.scalar(select(DiscoveryMigrationComparison).where(
        DiscoveryMigrationComparison.id == comparison_id,
        DiscoveryMigrationComparison.migration_id == migration_id,
    ))
    if comparison is None:
        raise HTTPException(404, "comparison not found")
    return serialize_comparison(comparison, include_evidence=include_evidence)


@router.post("/migrations/{migration_id}/comparisons/{comparison_id}/cancel", status_code=202)
def cancel_discovery_migration_comparison(
    migration_id: int,
    comparison_id: int,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    from app.discovery.migration import request_shadow_comparison_cancel
    from app.models import DiscoveryMigrationComparison

    comparison = db.scalar(select(DiscoveryMigrationComparison).where(
        DiscoveryMigrationComparison.id == comparison_id,
        DiscoveryMigrationComparison.migration_id == migration_id,
    ))
    if comparison is None:
        raise HTTPException(404, "comparison not found")
    comparison = request_shadow_comparison_cancel(db, migration_id, comparison_id)
    from app.discovery.migration import serialize_comparison

    payload = serialize_comparison(comparison, include_evidence=False)
    return {
        "comparison_id": comparison.id,
        "cancel_requested": comparison.cancel_requested_at is not None,
        "status": comparison.status,
        "cancel_requested_at": payload["cancel_requested_at"],
        "cancel_acknowledged_at": payload["cancel_acknowledged_at"],
    }


@router.post("/migrations/{migration_id}/accept-comparison")
def accept_discovery_migration_comparison(
    migration_id: int,
    body: MigrationExplanationRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    from app.discovery.migration import accept_comparison, serialize_comparison

    reviewer = str(_access.get("sub") or _access.get("email") or "system_admin")
    try:
        comparison = accept_comparison(
            db,
            migration_id,
            explanation=body.explanation,
            reviewer=reviewer,
        )
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return serialize_comparison(comparison)


@router.post("/migrations/{migration_id}/cutover")
def cutover_discovery_migration(
    migration_id: int,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    from app.discovery.domain_transition import MigrationBusyError
    from app.discovery.migration import cutover_migration, serialize_migration

    try:
        migration = cutover_migration(db, migration_id)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except MigrationBusyError as exc:
        db.rollback()
        raise HTTPException(503, {"message": str(exc), "retryable": True}) from exc
    return serialize_migration(db, migration, include_evidence=False)


@router.post("/migrations/{migration_id}/rollback")
def rollback_discovery_migration(
    migration_id: int,
    body: MigrationRollbackRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    from app.discovery.domain_transition import MigrationBusyError
    from app.discovery.migration import rollback_migration, serialize_migration

    try:
        migration = rollback_migration(db, migration_id, reason=body.reason)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except MigrationBusyError as exc:
        db.rollback()
        raise HTTPException(503, {"message": str(exc), "retryable": True}) from exc
    return serialize_migration(db, migration, include_evidence=False)


@router.post("/migrations/{migration_id}/retire-legacy")
def retire_discovery_migration_legacy(
    migration_id: int,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    from app.discovery.domain_transition import MigrationBusyError
    from app.discovery.migration import retire_legacy_method, serialize_migration

    try:
        migration = retire_legacy_method(db, migration_id)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except MigrationBusyError as exc:
        db.rollback()
        raise HTTPException(503, {"message": str(exc), "retryable": True}) from exc
    return serialize_migration(db, migration, include_evidence=False)


@router.get("/methods/{method_id}")
def get_method(method_id: int, db: Session = Depends(get_db)):
    """单方法详情，含完整 DSL Recipe（前端可展示/编辑）。"""
    from app.models import Source
    m = db.get(CrawlMethod, method_id)
    if not m:
        raise HTTPException(404, "method not found")
    if m.status == "packaging":
        raise HTTPException(409, "method packaging is not available for review")
    source = db.get(Source, m.source_id)
    is_plugin = (m.dsl_recipe or {}).get("recipe_type") == "python_plugin"
    execution_steps = []
    if is_plugin:
        try:
            from app.discovery.plugin.flow import describe_plugin_execution
            from app.discovery.plugin.errors import ConnectorProtocolError

            execution_steps = describe_plugin_execution(m.dsl_recipe or {})
        except (ConnectorProtocolError, OSError, SyntaxError, ValueError):
            execution_steps = []
    return {"id": m.id, "source_id": m.source_id, "domain": m.domain, "entry_url": m.entry_url, "status": m.status,
            "review_status": m.review_status,
            "reviewed_at": m.reviewed_at.isoformat() if m.reviewed_at else None,
            "reviewed_by": None if is_plugin else m.reviewed_by,
            "review_note": None if is_plugin else m.review_note,
            "source_name": source.name if source else m.domain,
            "dsl_recipe": m.dsl_recipe, "signature": m.signature,
            "execution_steps": execution_steps,
            "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None,
            "last_run_status": m.last_run_status,
            "plugin_review": plugin_review_summary(m),
            **_method_quality_fields(m)}


@router.get("/methods/{method_id}/source")
def get_method_source(
    method_id: int,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    """Return validated Python Connector source to an authorized reviewer."""
    method = db.get(CrawlMethod, method_id)
    if method is None:
        raise HTTPException(404, "method not found")
    try:
        from app.discovery.plugin.flow import read_plugin_source
        from app.discovery.plugin.errors import ConnectorProtocolError

        source = read_plugin_source(method.dsl_recipe or {})
    except (ConnectorProtocolError, OSError, UnicodeError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"method_id": method.id, "filename": "crawler.py", "source": source}


@router.patch("/methods/{method_id}")
def patch_method(
    method_id: int,
    body: MethodPatch,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    """禁用/启用方法（改 status）。"""
    m = db.get(CrawlMethod, method_id)
    if not m:
        raise HTTPException(404, "method not found")
    if m.status == "packaging":
        raise HTTPException(409, "method packaging is not mutable")
    if body.status == "active":
        if m.review_status != REVIEW_APPROVED:
            raise HTTPException(409, "method is pending review")
        mapping = db.scalar(select(CrawlMethodDomain).where(CrawlMethodDomain.domain == m.domain))
        if mapping is None or mapping.method_id != m.id:
            raise HTTPException(409, "only the currently published domain version can be enabled")
        if (m.dsl_recipe or {}).get("recipe_type") == "python_plugin":
            from app.discovery.plugin.review import validate_plugin_activation

            try:
                validate_plugin_activation(m)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        source = db.get(Source, m.source_id)
        if source is not None:
            source.enabled = True
        m.status = "active"
    elif body.status:
        m.status = body.status
        source = db.get(Source, m.source_id)
        if source is not None:
            active_count = db.scalar(
                select(func.count(CrawlMethodDomain.id))
                .join(CrawlMethod, CrawlMethodDomain.method_id == CrawlMethod.id)
                .where(
                    CrawlMethod.source_id == m.source_id,
                    CrawlMethod.id != m.id,
                    CrawlMethod.status == "active",
                )
            )
            if not active_count:
                source.enabled = False
    db.commit()
    return {"id": m.id, "status": m.status}


@router.delete("/methods/{method_id}", status_code=204)
def delete_method(
    method_id: int,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    """删除方法 + 级联清 crawl_method_domains 映射。"""
    try:
        deleted = delete_crawl_method(db, method_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        from app.discovery.domain_transition import MigrationBusyError

        if isinstance(exc, MigrationBusyError):
            db.rollback()
            raise HTTPException(503, {"message": str(exc), "retryable": True}) from exc
        raise
    if not deleted:
        raise HTTPException(404, "method not found")


@router.post("/methods/{method_id}/fetch")
def discovery_fetch(
    method_id: int,
    request: ManualNewsRunRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    """运行命：按 DSL Recipe 抓取 + 接现有 pipeline 入 items（走 LLM Enricher 富化+打分）。

    默认在可杀子进程中执行；取消时 terminate/kill 子进程，避免卡在网络 I/O。
    测试环境（PYTEST_CURRENT_TEST / DISCOVERY_FETCH_SYNC=1）仍走进程内路径便于 mock。
    """
    from app.discovery.fetch_jobs import FetchCancelled, get_active_fetch_job_run_id, run_killable_fetch
    from app.discovery.fetch_runs import ActiveMethodFetchError, get_active_method_fetch_run
    from app.run_logs import append_run_log

    m = db.get(CrawlMethod, method_id)
    if not m:
        raise HTTPException(404, "method not found")
    if m.review_status != REVIEW_APPROVED:
        raise HTTPException(409, "method is pending review")
    from app.discovery.migration import assert_formal_method_current

    try:
        assert_formal_method_current(db, m)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    request_payload = request.model_dump(mode="json") if request is not None else None
    try:
        return run_killable_fetch(method_id, request_payload, db=db)
    except ActiveMethodFetchError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "method_id": method_id,
                "active_run_id": exc.active_run_id,
            },
        ) from exc
    except FetchCancelled as exc:
        active_run = get_active_method_fetch_run(method_id, db)
        append_run_log(
            "抓方式",
            "爬取方式抓取已强制取消",
            source=m.domain,
            method_id=m.id,
            run_id=exc.run_id or (active_run.id if active_run else get_active_fetch_job_run_id(method_id)),
            level="warning",
        )
        raise HTTPException(status_code=499, detail="fetch cancelled") from None
    except RuntimeError as exc:
        from app.discovery.domain_transition import MigrationBusyError

        if isinstance(exc, MigrationBusyError):
            raise HTTPException(503, {"message": str(exc), "retryable": True}) from exc
        if "already has an active fetch job" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise


@router.post("/methods/{method_id}/fetch/cancel")
def discovery_fetch_cancel(
    method_id: int,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    """强制杀掉当前 method 的抓取子进程（硬取消，不等协作式退出）。"""
    from app.discovery.fetch_jobs import cancel_fetch_job, get_active_fetch_job_run_id
    from app.discovery.fetch_runs import (
        get_active_method_fetch_run,
        recover_dead_method_fetch_runs,
        request_method_fetch_cancel,
    )
    from app.run_logs import append_run_log

    m = db.get(CrawlMethod, method_id)
    if not m:
        raise HTTPException(404, "method not found")
    active_run = get_active_method_fetch_run(method_id, db)
    active_run_id = active_run.id if active_run else get_active_fetch_job_run_id(method_id)
    if active_run_id is not None:
        request_method_fetch_cancel(active_run_id, db)
    killed = cancel_fetch_job(method_id)
    if active_run_id is not None and not killed:
        try:
            recover_dead_method_fetch_runs(db, run_id=active_run_id)
        except Exception:
            # The request remains durably pending if Docker cleanup cannot be
            # proven; never claim cancellation acknowledgement in that case.
            logging.getLogger(__name__).exception(
                "dead formal fetch owner recovery failed run_id=%s", active_run_id
            )
    db.expire_all()
    acknowledged = bool(
        active_run_id is not None
        and (refreshed := db.get(CrawlMethodRun, active_run_id)) is not None
        and refreshed.status == "cancelled"
        and refreshed.cancel_acknowledged_at is not None
    )
    append_run_log(
        "抓方式",
        "收到强制取消抓取请求",
        source=m.domain,
        method_id=m.id,
        run_id=active_run_id,
        killed=killed,
    )
    return {
        "cancelled": acknowledged,
        "cancel_pending": bool(active_run_id is not None and not acknowledged),
        "killed": killed,
        "method_id": method_id,
        "run_id": active_run_id,
    }


class SuggestNameRequest(BaseModel):
    input: str
    display_input: str | None = None
    selected_route_type: RouteType | None = None
    resolved_route_type: RouteType | None = None
    route_source: RouteSource = RouteSource.INFERRED
    url: HttpUrl | None = None  # legacy: website URL for backward compat


def _extract_title(html: str) -> str | None:
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title or None


def _suggest_name_with_llm(site_url: str, domain: str, title: str | None) -> str | None:
    prompt = (
        resolve_prompt("naming", _NAMING_PROMPT)
        .replace("{site_url}", site_url)
        .replace("{domain}", domain)
        .replace("{title}", title or "(无标题)")
    )
    result = LlmClient().complete(
        prompt,
        temperature=0.1,
        timeout=SUGGEST_NAME_LLM_TIMEOUT_SECONDS,
    )
    return normalize_site_name(result)


@router.post("/suggest-name")
def suggest_name(body: SuggestNameRequest):
    from app.run_logs import append_run_log

    resolved_route_type = (body.resolved_route_type or body.selected_route_type)
    display_input = body.display_input or body.input

    # Route-aware naming for non-website types
    if resolved_route_type == RouteType.WECHAT_SEARCH:
        from app.discovery.naming import format_wechat_search_display_name
        value = body.input.strip()
        name = format_wechat_search_display_name(value)
        append_run_log(
            "命名",
            "自动生成名称",
            input=display_input,
            effective_input=body.input,
            resolved_route_type=resolved_route_type.value,
            name=name,
        )
        return {"name": name, "resolved_route_type": resolved_route_type.value}

    if resolved_route_type == RouteType.WECHAT_HISTORY:
        from app.discovery.naming import format_wechat_history_display_name
        value = body.input.strip()
        name = format_wechat_history_display_name(value)
        append_run_log(
            "命名",
            "自动生成名称",
            input=display_input,
            effective_input=body.input,
            resolved_route_type=resolved_route_type.value,
            name=name,
        )
        return {"name": name, "resolved_route_type": resolved_route_type.value}

    if resolved_route_type == RouteType.INTERNAL_FORUM:
        from app.discovery.naming import format_internal_forum_display_name
        value = body.input.strip()
        name = format_internal_forum_display_name(value)
        append_run_log(
            "命名",
            "自动生成名称",
            input=display_input,
            effective_input=body.input,
            resolved_route_type=resolved_route_type.value,
            name=name,
        )
        return {"name": name, "resolved_route_type": resolved_route_type.value}

    # Website: legacy path using LLM + title fetch
    from urllib.parse import urlparse
    import httpx

    site_url = body.input.strip()

    domain = urlparse(site_url).netloc.removeprefix("www.")
    title = None
    try:
        logger.info("suggest-name title fetch start url=%s", site_url)
        resp = httpx.get(site_url, timeout=8.0, follow_redirects=True,
                         headers={"User-Agent": "os-news-tracker/discovery"})
        if resp.status_code < 400:
            title = _extract_title(resp.text)
        logger.info(
            "suggest-name title fetch done url=%s status=%s title=%s",
            site_url,
            resp.status_code,
            (title or "")[:80],
        )
    except Exception:
        logger.exception("suggest-name title fetch failed url=%s", site_url)

    try:
        logger.info(
            "suggest-name llm start url=%s domain=%s has_title=%s timeout=%s",
            site_url,
            domain,
            bool(title),
            SUGGEST_NAME_LLM_TIMEOUT_SECONDS,
        )
        llm_name = _suggest_name_with_llm(site_url, domain, title)
        if llm_name:
            logger.info("suggest-name llm success url=%s name=%s", site_url, llm_name)
            name = format_website_display_name(llm_name, fallback_url=site_url)
            append_run_log(
                "命名",
                "自动生成名称",
                input=display_input,
                effective_input=body.input,
                resolved_route_type=resolved_route_type.value if resolved_route_type else "website",
                name=name,
            )
            return {"name": name, "resolved_route_type": resolved_route_type.value if resolved_route_type else "website"}
    except Exception:
        logger.exception("suggest-name llm failed url=%s", site_url)

    fallback = normalize_site_name(title) or normalize_site_name(domain) or domain
    name = format_website_display_name(fallback, fallback_url=site_url)
    logger.info("suggest-name fallback url=%s name=%s", site_url, fallback)
    append_run_log(
        "命名",
        "自动生成名称",
        input=display_input,
        effective_input=body.input,
        resolved_route_type=resolved_route_type.value if resolved_route_type else "website",
        name=name,
    )
    return {"name": name, "resolved_route_type": resolved_route_type.value if resolved_route_type else "website"}


# ---------------------------------------------------------------------------
# Prompt Studio：站点发现 fetch 阶段的 LLM prompt 多套自定义
# ---------------------------------------------------------------------------


class PromptStageInfo(BaseModel):
    key: str
    label: str
    description: str
    required_tokens: list[str]
    default_template: str


class PromptSetResponse(BaseModel):
    id: int
    name: str
    is_active: bool
    prompts: dict[str, str]
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PromptSetCreateRequest(BaseModel):
    name: str
    prompts: dict[str, str] = {}


class PromptSetUpdateRequest(BaseModel):
    name: str | None = None
    prompts: dict[str, str] | None = None


def _prompt_set_to_response(row: DiscoveryPromptSet) -> PromptSetResponse:
    return PromptSetResponse(
        id=row.id,
        name=row.name,
        is_active=row.is_active,
        prompts=dict(row.prompts or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/prompt-stages", response_model=list[PromptStageInfo])
def list_prompt_stages(_access: dict = Depends(require_system_access)):
    """各阶段元信息 + 内置默认模版（前端"新建"预填 / 恢复默认用）。"""
    defaults = get_stage_defaults()
    return [
        PromptStageInfo(
            key=stage.key,
            label=stage.label,
            description=stage.description,
            required_tokens=list(stage.required_tokens),
            default_template=defaults.get(stage.key, ""),
        )
        for stage in STAGES
    ]


@router.get("/prompt-sets", response_model=list[PromptSetResponse])
def list_prompt_sets(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    rows = db.scalars(select(DiscoveryPromptSet).order_by(DiscoveryPromptSet.id.desc())).all()
    return [_prompt_set_to_response(r) for r in rows]


@router.post("/prompt-sets", response_model=PromptSetResponse, status_code=201)
def create_prompt_set(
    body: PromptSetCreateRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "名称不能为空")
    errors = validate_prompts(body.prompts)
    if errors:
        raise HTTPException(422, "；".join(errors))
    row = DiscoveryPromptSet(name=name, prompts=dict(body.prompts or {}), is_active=False)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _prompt_set_to_response(row)


@router.put("/prompt-sets/{set_id}", response_model=PromptSetResponse)
def update_prompt_set(
    set_id: int,
    body: PromptSetUpdateRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    row = db.get(DiscoveryPromptSet, set_id)
    if row is None:
        raise HTTPException(404, "prompt set not found")
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "名称不能为空")
        row.name = name
    if body.prompts is not None:
        errors = validate_prompts(body.prompts)
        if errors:
            raise HTTPException(422, "；".join(errors))
        row.prompts = dict(body.prompts)
    db.commit()
    db.refresh(row)
    return _prompt_set_to_response(row)


@router.delete("/prompt-sets/{set_id}", status_code=204)
def delete_prompt_set(
    set_id: int,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    row = db.get(DiscoveryPromptSet, set_id)
    if row is None:
        raise HTTPException(404, "prompt set not found")
    db.delete(row)
    db.commit()


@router.post("/prompt-sets/{set_id}/activate", response_model=PromptSetResponse)
def activate_prompt_set(
    set_id: int,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    """启用某一套（其余自动停用）。"""
    row = db.get(DiscoveryPromptSet, set_id)
    if row is None:
        raise HTTPException(404, "prompt set not found")
    db.execute(update(DiscoveryPromptSet).values(is_active=False))
    row.is_active = True
    db.commit()
    db.refresh(row)
    return _prompt_set_to_response(row)


@router.post("/prompt-sets/{set_id}/deactivate", response_model=PromptSetResponse)
def deactivate_prompt_set(
    set_id: int,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    """停用某一套（回退到内置默认 prompt）。"""
    row = db.get(DiscoveryPromptSet, set_id)
    if row is None:
        raise HTTPException(404, "prompt set not found")
    row.is_active = False
    db.commit()
    db.refresh(row)
    return _prompt_set_to_response(row)


# ---------------------------------------------------------------------------
# 主分类管理：添加 / 改名（同步条目与标签）/ 删除（仅限空分类）
# ---------------------------------------------------------------------------


class MainCategoryResponse(BaseModel):
    id: int
    name: str
    sort_order: int
    item_count: int


class MainCategoryCreateRequest(BaseModel):
    name: str


class MainCategoryUpdateRequest(BaseModel):
    name: str


def _main_category_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(Item.main_category, func.count()).group_by(Item.main_category)
    ).all()
    return {name: count for name, count in rows if name is not None}


def _list_main_categories(db: Session) -> list[MainCategoryResponse]:
    counts = _main_category_counts(db)
    rows = db.scalars(
        select(MainCategory).order_by(MainCategory.sort_order, MainCategory.id)
    ).all()
    return [
        MainCategoryResponse(
            id=r.id, name=r.name, sort_order=r.sort_order, item_count=counts.get(r.name, 0)
        )
        for r in rows
    ]


@router.get("/main-categories", response_model=list[MainCategoryResponse])
def list_main_categories(db: Session = Depends(get_db)):
    return _list_main_categories(db)


@router.post("/main-categories", response_model=MainCategoryResponse, status_code=201)
def create_main_category(body: MainCategoryCreateRequest, db: Session = Depends(get_db)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "名称不能为空")
    exists = db.scalar(select(MainCategory).where(MainCategory.name == name))
    if exists is not None:
        raise HTTPException(422, "该主分类已存在")
    max_order = db.scalar(select(func.max(MainCategory.sort_order))) or 0
    row = MainCategory(name=name, sort_order=max_order + 1)
    db.add(row)
    db.commit()
    db.refresh(row)
    return MainCategoryResponse(id=row.id, name=row.name, sort_order=row.sort_order, item_count=0)


def _rename_main_category_tag(db: Session, old: str, new: str) -> None:
    """把 kind=MAIN_CATEGORY 的标签 old→new；若 new 已存在则把关联并入 new 后删除 old。"""
    old_tag = db.scalar(
        select(Tag).where(Tag.kind == TagKind.MAIN_CATEGORY, Tag.name == old)
    )
    if old_tag is None:
        return
    new_tag = db.scalar(
        select(Tag).where(Tag.kind == TagKind.MAIN_CATEGORY, Tag.name == new)
    )
    if new_tag is None or new_tag.id == old_tag.id:
        old_tag.name = new
        return
    # 合并：把 old_tag 的条目关联迁到 new_tag（去重），再删 old_tag
    existing_item_ids = set(
        db.scalars(select(ItemTag.item_id).where(ItemTag.tag_id == new_tag.id)).all()
    )
    for link in db.scalars(select(ItemTag).where(ItemTag.tag_id == old_tag.id)).all():
        if link.item_id in existing_item_ids:
            db.delete(link)
        else:
            link.tag_id = new_tag.id
            existing_item_ids.add(link.item_id)
    db.flush()
    db.delete(old_tag)


@router.put("/main-categories/{category_id}", response_model=MainCategoryResponse)
def update_main_category(
    category_id: int, body: MainCategoryUpdateRequest, db: Session = Depends(get_db)
):
    row = db.get(MainCategory, category_id)
    if row is None:
        raise HTTPException(404, "main category not found")
    new_name = (body.name or "").strip()
    if not new_name:
        raise HTTPException(422, "名称不能为空")
    old_name = row.name
    if new_name == old_name:
        counts = _main_category_counts(db)
        return MainCategoryResponse(
            id=row.id, name=row.name, sort_order=row.sort_order, item_count=counts.get(row.name, 0)
        )
    clash = db.scalar(
        select(MainCategory).where(MainCategory.name == new_name, MainCategory.id != category_id)
    )
    if clash is not None:
        raise HTTPException(422, "已存在同名主分类")
    # 同步：条目字段 + 主分类标签
    db.execute(
        update(Item).where(Item.main_category == old_name).values(main_category=new_name)
    )
    _rename_main_category_tag(db, old_name, new_name)
    row.name = new_name
    db.commit()
    db.refresh(row)
    counts = _main_category_counts(db)
    return MainCategoryResponse(
        id=row.id, name=row.name, sort_order=row.sort_order, item_count=counts.get(row.name, 0)
    )


@router.delete("/main-categories/{category_id}", status_code=204)
def delete_main_category(category_id: int, db: Session = Depends(get_db)):
    row = db.get(MainCategory, category_id)
    if row is None:
        raise HTTPException(404, "main category not found")
    count = db.scalar(
        select(func.count()).select_from(Item).where(Item.main_category == row.name)
    ) or 0
    if count > 0:
        raise HTTPException(422, f"该主分类下还有 {count} 条条目，无法删除")
    db.delete(row)
    db.commit()
