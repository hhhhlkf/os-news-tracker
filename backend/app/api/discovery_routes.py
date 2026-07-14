"""新端点：/discovery/run（生成命）+ /discovery/methods/{id}/fetch（运行命）。

纯增量：不替换旧 /sources/discover，不接入 agent_crawl 主流程。
"""

from datetime import datetime, timezone
from enum import Enum

import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, HttpUrl
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_system_access
from typing import Any

from app.discovery.cancel import request_cancel
from app.discovery.execution import run_method
from app.discovery.graph import check_existing_method, start_discovery_run
from app.discovery.multi_graph import start_multi_discovery_run
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
from app.llm.client import LlmClient
from app.enums import TagKind
from app.models import (
    CrawlMethod,
    DiscoveryPromptSet,
    Item,
    ItemTag,
    MainCategory,
    SiteDiscoveryRun,
    Tag,
)
from app.discovery.prompts import (
    DEFAULT_NAMING,
    STAGES,
    get_stage_defaults,
    resolve_prompt,
    validate_prompts,
)
from app.schemas import ManualNewsRunRequest

router = APIRouter(prefix="/discovery", tags=["discovery"])
logger = logging.getLogger(__name__)
SUGGEST_NAME_LLM_TIMEOUT_SECONDS = 5.0
# 站点命名 prompt（token 模版）；供 prompts.get_stage_defaults 引用，也是本模块默认。
_NAMING_PROMPT = DEFAULT_NAMING


class DiscoverRequest(BaseModel):
    url: HttpUrl
    force: bool = False  # true=跳过去重检查/覆盖同 domain 旧范式
    name: str | None = None  # 站点别名（选填，不填自动用域名）


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
    force: bool = False
    name: str | None = None
    hints: dict[str, Any] | None = None
    selected_route_type: RouteType | None = None
    resolved_route_type: RouteType | None = None
    route_source: RouteSource = RouteSource.INFERRED


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


@router.post("/run")
def discover_run(body: DiscoverRequest, db: Session = Depends(get_db)):
    """生成命：force=false 先查重，重复返回 duplicate；无重复/force=true 异步启动，返回 run_id 供轮询。"""
    site_url = str(body.url)
    if not body.force:
        existing = check_existing_method(site_url, db)
        if existing:
            return {"status": "duplicate", "existing_method": existing}
    # 别名：前端选填，不填自动用"网站：..."命名
    name = body.name or default_website_display_name(site_url)
    run_id = start_discovery_run(site_url, force=body.force, name=name)
    return {"status": "started", "run_id": run_id, "name": name}


@router.post("/multi-run")
def discover_multi_run(body: MultiDiscoverRequest):
    from app.run_logs import append_run_log

    effective_route = body.resolved_route_type or body.selected_route_type
    hints = _route_type_to_hints(effective_route)
    display_input = body.display_input or body.input

    append_run_log(
        "路由",
        "已锁定最终路由",
        input=display_input,
        effective_input=body.input,
        selected_route_type=body.selected_route_type.value if body.selected_route_type else None,
        resolved_route_type=effective_route.value if effective_route else None,
        route_source=body.route_source.value,
    )

    append_run_log(
        "探查",
        "已按已保存分支启动",
        input=display_input,
        effective_input=body.input,
        resolved_route_type=effective_route.value if effective_route else None,
        route_source=body.route_source.value,
        name=body.name,
    )

    # Merge explicit hints with any user-provided hints
    merged_hints: dict[str, Any] = {**hints}
    if body.hints:
        merged_hints.update(body.hints)

    result = start_multi_discovery_run(
        body.input,
        force=body.force,
        name=body.name,
        hints=merged_hints,
        selected_route_type=effective_route.value if effective_route else None,
        route_source=body.route_source.value,
    )

    # Attach resolved route metadata to response
    result["resolved_route_type"] = effective_route.value if effective_route else None
    result["route_source"] = body.route_source.value
    return result


@router.get("/runs")
def list_discovery_runs(limit: int = 20, db: Session = Depends(get_db)):
    """列出生成命历史（最近 limit 条），按 started_at 倒序。"""
    runs = db.scalars(
        select(SiteDiscoveryRun).order_by(SiteDiscoveryRun.started_at.desc()).limit(limit)
    ).all()
    return [{"id": r.id, "site_url": r.site_url, "status": r.status,
             "resulting_method_id": r.resulting_method_id, "llm_token_usage": r.llm_token_usage,
             "started_at": r.started_at.isoformat() if r.started_at else None,
             "ended_at": r.ended_at.isoformat() if r.ended_at else None,
             "error_message": r.error_message} for r in runs]


@router.get("/runs/{run_id}")
def get_discovery_run(run_id: int, db: Session = Depends(get_db)):
    """单 run 详情/轮询：含 node_trace 逐步轨迹 + current_step（前端高亮"进行到哪一步了"）。"""
    r = db.get(SiteDiscoveryRun, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    current_step = r.node_trace[-1]["step"] if r.node_trace else None
    return {"id": r.id, "site_url": r.site_url, "status": r.status,
            "resulting_method_id": r.resulting_method_id, "llm_token_usage": r.llm_token_usage,
            "node_trace": r.node_trace, "retry_count": r.retry_count,
            "current_step": current_step,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "error_message": r.error_message}


@router.post("/runs/{run_id}/cancel")
def cancel_discovery_run(run_id: int, db: Session = Depends(get_db)):
    """手动取消生成命：停止后续节点/LLM/API 调用，并把状态标为 cancelled。"""
    r = db.get(SiteDiscoveryRun, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    if r.status != "running":
        current_step = r.node_trace[-1]["step"] if r.node_trace else None
        return {"id": r.id, "site_url": r.site_url, "status": r.status,
                "resulting_method_id": r.resulting_method_id, "llm_token_usage": r.llm_token_usage,
                "node_trace": r.node_trace, "retry_count": r.retry_count,
                "current_step": current_step,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "error_message": r.error_message}
    request_cancel(run_id)
    r.status = "cancelled"
    r.error_message = "已手动取消"
    r.ended_at = datetime.now(timezone.utc)
    db.commit()
    current_step = r.node_trace[-1]["step"] if r.node_trace else None
    return {"id": r.id, "site_url": r.site_url, "status": r.status,
            "resulting_method_id": r.resulting_method_id, "llm_token_usage": r.llm_token_usage,
            "node_trace": r.node_trace, "retry_count": r.retry_count,
            "current_step": current_step,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "error_message": r.error_message}


class MethodPatch(BaseModel):
    status: str | None = None  # active | disabled | failed


class MethodReviewBatchRequest(BaseModel):
    method_ids: list[int]


class ReviewReminderUpdateRequest(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None
    recipients: list[str] | None = None


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
    if method.overall_score is not None:
        return method.overall_score
    if method.quality_score is None:
        return None
    density_score = method.density_score if method.density_score is not None else method.quality_score
    return int(round(method.quality_score * 0.5 + density_score * 0.5))


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
    return {
        "id": method.id,
        "domain": method.domain,
        "entry_url": method.entry_url,
        "status": method.status,
        "review_status": method.review_status,
        "reviewed_at": method.reviewed_at.isoformat() if method.reviewed_at else None,
        "reviewed_by": method.reviewed_by,
        "review_note": method.review_note,
        "source_name": method_source_name(db, method),
        "signature": method.signature,
        "last_run_at": method.last_run_at.isoformat() if method.last_run_at else None,
        "last_run_status": method.last_run_status,
        "created_at": method.created_at.isoformat() if method.created_at else None,
        **_method_quality_fields(method),
    }


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
        .where(CrawlMethod.review_status == REVIEW_APPROVED)
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
        .where(CrawlMethod.review_status == REVIEW_PENDING)
        .order_by(CrawlMethod.created_at.desc(), CrawlMethod.id.desc())
    ).all()
    return [_method_response(m, db) for m in ms]


@router.post("/methods/review/approve")
def approve_pending_methods(
    body: MethodReviewBatchRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    count = approve_methods(db, body.method_ids)
    return {"approved_count": count}


@router.post("/methods/review/delete")
def delete_pending_methods(
    body: MethodReviewBatchRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    count = delete_methods(db, body.method_ids)
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
        config.recipients_json = [recipient.strip() for recipient in body.recipients if recipient.strip()]
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


@router.get("/methods/{method_id}")
def get_method(method_id: int, db: Session = Depends(get_db)):
    """单方法详情，含完整 DSL Recipe（前端可展示/编辑）。"""
    from app.models import Source
    m = db.get(CrawlMethod, method_id)
    if not m:
        raise HTTPException(404, "method not found")
    source = db.get(Source, m.source_id)
    return {"id": m.id, "domain": m.domain, "entry_url": m.entry_url, "status": m.status,
            "review_status": m.review_status,
            "reviewed_at": m.reviewed_at.isoformat() if m.reviewed_at else None,
            "reviewed_by": m.reviewed_by,
            "review_note": m.review_note,
            "source_name": source.name if source else m.domain,
            "dsl_recipe": m.dsl_recipe, "signature": m.signature,
            "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None,
            "last_run_status": m.last_run_status,
            **_method_quality_fields(m)}


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
    if body.status:
        m.status = body.status
    db.commit()
    return {"id": m.id, "status": m.status}


@router.delete("/methods/{method_id}", status_code=204)
def delete_method(
    method_id: int,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
):
    """删除方法 + 级联清 crawl_method_domains 映射。"""
    if not delete_crawl_method(db, method_id):
        raise HTTPException(404, "method not found")


@router.post("/methods/{method_id}/fetch")
def discovery_fetch(
    method_id: int,
    request: ManualNewsRunRequest | None = Body(default=None),
    db: Session = Depends(get_db),
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
        if "already has an active fetch job" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise


@router.post("/methods/{method_id}/fetch/cancel")
def discovery_fetch_cancel(method_id: int, db: Session = Depends(get_db)):
    """强制杀掉当前 method 的抓取子进程（硬取消，不等协作式退出）。"""
    from app.discovery.fetch_jobs import cancel_fetch_job, get_active_fetch_job_run_id
    from app.discovery.fetch_runs import finish_method_fetch_run, get_active_method_fetch_run
    from app.run_logs import append_run_log

    m = db.get(CrawlMethod, method_id)
    if not m:
        raise HTTPException(404, "method not found")
    active_run = get_active_method_fetch_run(method_id, db)
    active_run_id = active_run.id if active_run else get_active_fetch_job_run_id(method_id)
    killed = cancel_fetch_job(method_id)
    if active_run is not None and not killed:
        finish_method_fetch_run(active_run.id, "cancelled", error_message="fetch cancelled", db=db)
    append_run_log(
        "抓方式",
        "收到强制取消抓取请求",
        source=m.domain,
        method_id=m.id,
        run_id=active_run_id,
        killed=killed,
    )
    return {"cancelled": True, "killed": killed, "method_id": method_id, "run_id": active_run_id}


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
