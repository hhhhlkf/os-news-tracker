from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.discovery.audit import audit_discovery_recipe
from app.discovery.quality_audit import (
    apply_quality_audit_to_method,
    audit_source_quality,
)
from app.discovery.review import REVIEW_PENDING
from app.discovery.graph import (
    DiscoveryState,
    auditor,
    dsl_writer,
    start_discovery_run,
)
from app.discovery.method_keys import crawl_method_domain_key
from app.discovery.naming import (
    default_website_display_name,
    format_wechat_search_display_name,
    format_wechat_history_display_name,
    format_internal_forum_display_name,
)

logger = logging.getLogger(__name__)

BranchKind = Literal["website", "wechat", "internal_mcp", "unsupported"]
DEFAULT_WECHAT_SEARCH_MAX_PAGES = 2
DEFAULT_WECHAT_HISTORY_LIMIT = 30


class SourceRoute(BaseModel):
    kind: BranchKind
    confidence: float = Field(ge=0.0, le=1.0)
    normalized_input: str
    input_type: str
    markers: list[str] = Field(default_factory=list)
    reason: str
    suggested_branch: BranchKind


class MultiDiscoveryState(DiscoveryState, total=False):
    raw_input: str
    normalized_input: str
    source_route: dict[str, Any]
    branch_kind: str
    branch_artifact: dict[str, Any]
    branch_trace_logs: list[dict[str, Any]]
    multi_dsl_recipe: dict[str, Any] | None
    multi_audit_result: dict[str, Any] | None


def normalize_input(raw_input: str) -> str:
    return " ".join((raw_input or "").strip().split())


def source_router_for_input(raw_input: str, hints: dict[str, Any] | None = None) -> SourceRoute:
    normalized = normalize_input(raw_input)
    hints = hints or {}
    hinted_kind = str(hints.get("source_kind") or "").strip().lower()
    lower = normalized.lower()
    markers: list[str] = []

    if "[km]" in lower:
        markers.append("KM")
    if "[iwiki]" in lower:
        markers.append("iWiki")

    if markers:
        return SourceRoute(
            kind="internal_mcp",
            confidence=1.0,
            normalized_input=normalized,
            input_type="internal_query",
            markers=markers,
            reason="input contains internal source marker",
            suggested_branch="internal_mcp",
        )

    if hinted_kind in {"wechat", "wechat_search", "wechat_history"}:
        input_type = _classify_wechat_input(normalized)
        if hinted_kind == "wechat_search":
            input_type = "wechat_search"
        elif hinted_kind == "wechat_history":
            input_type = "wechat_history"
        return SourceRoute(
            kind="wechat",
            confidence=1.0,
            normalized_input=normalized,
            input_type=input_type,
            reason=f"source_kind hint requested {hinted_kind}",
            suggested_branch="wechat",
        )

    parsed = urlparse(normalized)
    if parsed.scheme in {"http", "https"}:
        host = parsed.netloc.lower()
        if host.endswith("mp.weixin.qq.com"):
            return SourceRoute(
                kind="wechat",
                confidence=1.0,
                normalized_input=normalized,
                input_type="wechat_history_url",
                reason="mp.weixin.qq.com URL",
                suggested_branch="wechat",
            )
        return SourceRoute(
            kind="website",
            confidence=1.0,
            normalized_input=normalized,
            input_type="url",
            reason="ordinary URL",
            suggested_branch="website",
        )

    return SourceRoute(
        kind="wechat",
        confidence=0.6,
        normalized_input=normalized,
        input_type="wechat_search",
        reason="plain text defaults to wechat_search when source_kind is omitted",
        suggested_branch="wechat",
    )


def _classify_wechat_input(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        return "wechat_history_url"
    return "wechat_search"


def build_wechat_search_recipe(
    entry: str,
    *,
    limit: int | None = None,
    max_pages: int | None = DEFAULT_WECHAT_SEARCH_MAX_PAGES,
    template_variant: str = "search_then_article_enrich",
    fetch_content: bool = True,
    resolve_final_urls_limit: int | None = None,
    enrich_max_items: int | None = None,
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = [
        {
            "op": "wechat_search_articles",
            "query": entry,
            "limit": limit,
            "max_pages": max_pages,
            "resolve_final_urls_limit": resolve_final_urls_limit,
            "as": "last_fetch",
        },
        {
            "op": "extract",
            "from": "last_fetch.items",
            "fields": {
                "title": "title",
                "url": "url",
                "published_at": "published_at",
                "summary": "summary",
                "content": "content",
            },
            "into": "items",
            "merge": False,
        },
    ]
    notes = ["WeChat keyword search uses public Sogou WeChat search results."]

    if template_variant == "search_then_article_enrich":
        actions.append(
            {
                "op": "enrich_wechat_articles",
                "fetch_content": fetch_content,
                "fill_missing_only": True,
                "max_items": enrich_max_items,
            }
        )
        notes.append("After card extraction, article pages are fetched to backfill missing published_at, summary, and content.")
    elif template_variant == "search_card_only":
        notes.append("Card-only template: rely on search-result cards without article-page enrichment.")
    else:
        raise ValueError(f"unsupported wechat search template_variant: {template_variant}")

    actions.append({"op": "dedup_by", "field": "url"})
    return {
        "recipe_type": "multi_dsl",
        "source_kind": "wechat",
        "entry": entry,
        "auth_ref": None,
        "requires_auth": False,
        "actions": actions,
        "notes": notes,
    }


def build_wechat_history_recipe(
    entry: str,
    artifact: dict[str, Any],
    *,
    limit: int = DEFAULT_WECHAT_HISTORY_LIMIT,
    fetch_content: bool = False,
) -> dict[str, Any]:
    return {
        "recipe_type": "multi_dsl",
        "source_kind": "wechat",
        "entry": entry,
        "auth_ref": "wechat_mp_default",
        "requires_auth": True,
        "actions": [
            {
                "op": "wechat_fetch_account_history",
                "nickname": artifact.get("nickname") or entry,
                "account_id": artifact.get("account_id"),
                "fakeid": artifact.get("fakeid"),
                "__biz": artifact.get("__biz"),
                "limit": limit,
                "fetch_content": fetch_content,
                "auth_ref": "wechat_mp_default",
                "as": "last_fetch",
            },
            {
                "op": "extract",
                "from": "last_fetch.items",
                "fields": {
                    "title": "title",
                    "url": "url",
                    "published_at": "published_at",
                    "summary": "summary",
                    "content": "content",
                },
                "into": "items",
                "merge": False,
            },
            {
                "op": "enrich_wechat_articles",
                "fetch_content": True,
                "fill_missing_only": True,
                "max_items": None,
            },
            {"op": "dedup_by", "field": "url"},
        ],
        "notes": [
            "WeChat account history uses the default wechat_mp_default auth profile.",
            "After history extraction, article pages are fetched to backfill missing published_at, summary, and content.",
        ],
    }


def _sanitize_recipe_for_storage(recipe: dict[str, Any]) -> dict[str, Any]:
    """Remove sensitive keys (cookie, token, authorization, headers) before storing."""
    sanitized = json.loads(json.dumps(recipe, ensure_ascii=False, default=str))

    def _clean(obj: object) -> None:
        if isinstance(obj, dict):
            for key in list(obj.keys()):
                if key.lower() in {"cookie", "token", "authorization", "headers"}:
                    obj[key] = "[redacted]"
                elif isinstance(obj[key], (dict, list)):
                    _clean(obj[key])
        elif isinstance(obj, list):
            for item in obj:
                _clean(item)

    _clean(sanitized)
    return sanitized


def _compute_multi_signature(recipe: dict[str, Any]) -> str:
    """Deterministic SHA256 from source_kind, entry, and action JSON."""
    source_kind = recipe.get("source_kind", "")
    entry = recipe.get("entry", "")
    actions_json = json.dumps(recipe.get("actions") or [], ensure_ascii=False, sort_keys=True)
    key = f"{source_kind}|{entry}|{actions_json}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _is_wechat_history_input(input_type: str) -> bool:
    return input_type in {"wechat_history", "wechat_history_url", "wechat_account"}


def _run_and_save_multi_recipe(
    *,
    route: SourceRoute,
    recipe: dict[str, Any],
    run_recipe: dict[str, Any] | None = None,
    force: bool = False,
    name: str | None = None,
) -> dict[str, Any]:
    """Run a multi_dsl recipe, save CrawlMethod, and return result."""
    from app.db import SessionLocal
    from app.discovery.multi_dsl import MultiDslRecipe
    from app.discovery.multi_interpreter import MultiDslInterpreter
    from app.run_logs import append_run_log

    effective_run_recipe = run_recipe or recipe
    source_label = name or route.normalized_input

    def _log_progress(event: str, payload: dict[str, Any]) -> None:
        if event == "wechat_search_started":
            append_run_log(
                "探查",
                "开始执行微信搜索",
                source=source_label,
                query=payload.get("query"),
                max_pages=payload.get("max_pages"),
                limit=payload.get("limit"),
            )
        elif event == "wechat_search_page_started":
            append_run_log(
                "探查",
                "微信搜索开始抓取分页",
                source=source_label,
                query=payload.get("query"),
                page_no=payload.get("page_no"),
                total_pages=payload.get("total_pages"),
                fetched_count=payload.get("fetched_count"),
                transport=payload.get("transport"),
            )
        elif event == "wechat_search_page_finished":
            append_run_log(
                "探查",
                "微信搜索分页抓取完成",
                source=source_label,
                query=payload.get("query"),
                page_no=payload.get("page_no"),
                page_items=payload.get("page_items"),
                fetched_count=payload.get("fetched_count"),
                resolved_count=payload.get("resolved_count"),
                transport=payload.get("transport"),
            )
        elif event == "wechat_search_page_empty":
            append_run_log(
                "探查",
                "微信搜索当前分页未提取到结果",
                source=source_label,
                query=payload.get("query"),
                page_no=payload.get("page_no"),
                fetched_count=payload.get("fetched_count"),
                transport=payload.get("transport"),
            )
        elif event == "wechat_search_rate_limited":
            append_run_log(
                "探查",
                "微信搜索触发限流",
                source=source_label,
                query=payload.get("query"),
                page_no=payload.get("page_no"),
                raw_status=payload.get("raw_status"),
                fetched_count=payload.get("fetched_count"),
                transport=payload.get("transport"),
                level="warning",
            )
        elif event == "wechat_search_captcha_required":
            append_run_log(
                "探查",
                "微信搜索触发验证码",
                source=source_label,
                query=payload.get("query"),
                page_no=payload.get("page_no"),
                fetched_count=payload.get("fetched_count"),
                transport=payload.get("transport"),
                level="warning",
            )
        elif event == "wechat_search_failed":
            append_run_log(
                "探查",
                "微信搜索执行失败",
                source=source_label,
                query=payload.get("query"),
                fetched_count=payload.get("fetched_count"),
                transport=payload.get("transport"),
                error=payload.get("error"),
                level="error",
            )
        elif event == "wechat_search_finished":
            append_run_log(
                "探查",
                "微信搜索执行完成",
                source=source_label,
                query=payload.get("query"),
                fetched_count=payload.get("fetched_count"),
                status=payload.get("status"),
                transport=payload.get("transport"),
            )

    append_run_log(
        "探查",
        "开始执行多源探查配方",
        source=source_label,
        input_type=route.input_type,
        branch_kind=route.kind,
        recipe_type=effective_run_recipe.get("recipe_type"),
    )
    output = MultiDslInterpreter().run(
        MultiDslRecipe(**effective_run_recipe),
        progress_callback=_log_progress,
    )
    items = output.get("items") or []
    status = (output.get("stats") or {}).get("status")
    append_run_log(
        "探查",
        "多源探查配方执行结束",
        source=source_label,
        discovered_count=len(items),
        status=status,
    )

    audit_result = audit_discovery_recipe(
        source_kind=route.kind,
        input_type=route.input_type,
        recipe=effective_run_recipe,
        items=items,
        status=status,
    )
    append_run_log(
        "审计",
        "多源配方审计完成",
        source=source_label,
        audit_kind=audit_result["audit_kind"],
        input_type=route.input_type,
        branch_kind=route.kind,
        status=status,
        method_status=audit_result["method_status"],
        passed=audit_result["passed"],
        discovered_count=len(items),
        required_count=audit_result["required_count"],
        reason=audit_result["reason"],
    )

    method_status = audit_result["method_status"]

    sanitized_recipe = _sanitize_recipe_for_storage(recipe)
    sig = _compute_multi_signature(sanitized_recipe)

    db = SessionLocal()
    try:
        from datetime import datetime as dt
        from app.enums import SourceType, Stream
        from app.models import CrawlMethod, CrawlMethodDomain, Source

        if route.kind == "wechat" and _is_wechat_history_input(route.input_type):
            if urlparse(route.normalized_input).scheme:
                # History URL inputs should not collapse into the shared mp.weixin.qq.com host.
                domain = f"wechat_mp_{hashlib.sha256(route.normalized_input.encode()).hexdigest()[:16]}"
            else:
                # Account history — one domain per account input.
                domain = f"wechat_mp_{route.normalized_input}"
        elif route.kind == "wechat" and route.input_type == "wechat_search":
            # Keyword search — one domain per search query/method.
            domain = f"wechat_search_{hashlib.sha256(route.normalized_input.encode()).hexdigest()[:16]}"
        elif urlparse(route.normalized_input).scheme:
            domain = crawl_method_domain_key(route.normalized_input, sanitized_recipe)
        else:
            domain = route.kind
        if not domain:
            domain = route.kind

        if name:
            display_name = name
        elif route.kind == "wechat":
            if recipe.get("requires_auth"):
                display_name = format_wechat_history_display_name(route.normalized_input)
            else:
                display_name = format_wechat_search_display_name(route.normalized_input)
        else:
            display_name = route.normalized_input[:80]

        existing_domain = db.query(CrawlMethodDomain).filter_by(domain=domain).first()

        # Quality audit runs on every successful multi discovery (including reuse),
        # so its LLM tokens are always attributed to this discovery run.
        quality_audit = None
        audit_tokens = 0
        if audit_result.get("passed"):
            from app.llm.usage import current_usage_total, usage_stage

            tokens_before = current_usage_total()
            with usage_stage("quality_audit"):
                quality_audit = audit_source_quality(
                    items=items,
                    source_kind=route.kind,
                    input_type=route.input_type,
                )
            audit_tokens = max(0, current_usage_total() - tokens_before)
            append_run_log(
                "质量审计",
                "信息源质量审计完成",
                source=source_label,
                quality_score=quality_audit.quality_score,
                quality_grade=quality_audit.quality_grade,
                density_score=quality_audit.density_score,
                density_weekly_avg=quality_audit.density_weekly_avg,
                quality_audit_status=quality_audit.quality_audit_status,
                method_status=method_status,
                reason=quality_audit.quality_reason,
                token_used=audit_tokens,
            )
        else:
            append_run_log(
                "质量审计",
                "方法审计未通过，跳过信息源质量审计",
                source=source_label,
                audit_kind=audit_result.get("audit_kind"),
                reason=audit_result.get("reason"),
                token_used=0,
            )

        if existing_domain is not None and not force:
            m = db.get(CrawlMethod, existing_domain.method_id)
            if quality_audit is not None:
                apply_quality_audit_to_method(m, quality_audit)
                db.commit()
            append_run_log(
                "存库",
                "复用已有多源爬取方式",
                source=source_label,
                domain=domain,
                method_id=m.id,
                method_status=m.status,
                discovered_count=len(items),
                token_used=audit_tokens,
            )
            return {
                "status": "completed",
                "method_id": m.id,
                "route": route.model_dump(),
                "discovered_count": len(items),
                "method_status": m.status,
                "multi_audit_result": audit_result,
                "quality_audit": quality_audit.as_update_values() if quality_audit else None,
                "note": "existing method reused (force=false)",
            }

        if existing_domain is not None and force:
            append_run_log(
                "存库",
                "覆盖更新现有多源爬取方式",
                source=source_label,
                domain=domain,
                method_status=method_status,
                discovered_count=len(items),
            )
            m = db.get(CrawlMethod, existing_domain.method_id)
            m.dsl_recipe = sanitized_recipe
            m.signature = sig
            m.status = method_status
            m.review_status = REVIEW_PENDING
            m.reviewed_at = None
            m.reviewed_by = None
            m.review_note = None
            m.updated_at = dt.now(timezone.utc)
            if quality_audit is not None:
                apply_quality_audit_to_method(m, quality_audit)
            src = db.get(Source, m.source_id)
            if src:
                src.name = display_name
            db.commit()
            return {
                "status": "completed",
                "method_id": m.id,
                "route": route.model_dump(),
                "discovered_count": len(items),
                "method_status": method_status,
                "multi_audit_result": audit_result,
                "quality_audit": quality_audit.as_update_values() if quality_audit else None,
            }
        src = Source(
            name=display_name,
            type=SourceType.DISCOVERY.value,
            url=route.normalized_input,
            main_category="OS跟踪来源",
            stream=Stream.NEWS.value,
            enabled=True,
        )
        db.add(src)
        db.flush()

        m = CrawlMethod(
            domain=domain,
            entry_url=route.normalized_input,
            source_id=src.id,
            dsl_recipe=sanitized_recipe,
            signature=sig,
            status=method_status,
            review_status=REVIEW_PENDING,
        )
        db.add(m)
        db.flush()
        if quality_audit is not None:
            apply_quality_audit_to_method(m, quality_audit)

        db.add(CrawlMethodDomain(domain=domain, method_id=m.id))
        db.commit()
        append_run_log(
            "存库",
            "已新建多源爬取方式",
            source=source_label,
            domain=domain,
            method_id=m.id,
            method_status=method_status,
            discovered_count=len(items),
        )

        return {
            "status": "completed",
            "method_id": m.id,
            "method_status": method_status,
            "route": route.model_dump(),
            "discovered_count": len(items),
            "multi_audit_result": audit_result,
            "quality_audit": quality_audit.as_update_values() if quality_audit else None,
        }
    finally:
        db.close()


def _run_multi_discovery_sync(
    raw_input: str,
    *,
    force: bool = False,
    name: str | None = None,
    hints: dict[str, Any] | None = None,
    selected_route_type: str | None = None,
    route_source: str = "inferred",
) -> dict[str, Any]:
    route = source_router_for_input(raw_input, hints)

    # Honor explicit route selection from the frontend
    if selected_route_type == "website":
        # Force website semantics: extract URL from input
        normalized = route.normalized_input
        route.kind = "website"
        route.input_type = "url"
        route.reason = "explicit website route from frontend"
        route.suggested_branch = "website"
        display_name = name or default_website_display_name(normalized)
        run_id = start_discovery_run(normalized, force=force, name=display_name)
        return {
            "status": "started",
            "run_id": run_id,
            "route": route.model_dump(),
            "delegated": "website_discovery",
            "resolved_route_type": "website",
            "route_source": route_source,
        }

    if selected_route_type == "wechat_search":
        route.kind = "wechat"
        route.input_type = "wechat_search"
        route.reason = "explicit wechat_search route from frontend"
        route.suggested_branch = "wechat"
        template_variant = str((hints or {}).get("template_variant") or "search_then_article_enrich")
        fetch_content = bool((hints or {}).get("fetch_content", True))
        max_pages = int((hints or {}).get("max_pages") or DEFAULT_WECHAT_SEARCH_MAX_PAGES)
        recipe = build_wechat_search_recipe(
            route.normalized_input,
            limit=None,
            max_pages=max_pages,
            template_variant=template_variant,
            fetch_content=fetch_content,
            resolve_final_urls_limit=None,
            enrich_max_items=None,
        )
        validation_recipe = build_wechat_search_recipe(
            route.normalized_input,
            limit=None,
            max_pages=max_pages,
            template_variant=template_variant,
            fetch_content=False,
            resolve_final_urls_limit=0,
            enrich_max_items=0 if template_variant == "search_then_article_enrich" else None,
        )
        result = _run_and_save_multi_recipe(
            route=route,
            recipe=recipe,
            run_recipe=validation_recipe,
            force=force,
            name=name,
        )
        result["resolved_route_type"] = "wechat_search"
        result["route_source"] = route_source
        return result

    if selected_route_type == "wechat_history":
        route.kind = "wechat"
        route.input_type = "wechat_history"
        route.reason = "explicit wechat_history route from frontend"
        route.suggested_branch = "wechat"
        limit = int((hints or {}).get("limit") or DEFAULT_WECHAT_HISTORY_LIMIT)
        fetch_content = bool((hints or {}).get("fetch_content", False))
        artifact = {
            "source_kind": "wechat",
            "nickname": route.normalized_input,
            "auth_ref": "wechat_mp_default",
        }
        recipe = build_wechat_history_recipe(
            route.normalized_input, artifact, limit=limit, fetch_content=fetch_content
        )
        result = _run_and_save_multi_recipe(route=route, recipe=recipe, force=force, name=name)
        result["resolved_route_type"] = "wechat_history"
        result["route_source"] = route_source
        return result

    if selected_route_type == "internal_forum":
        return {
            "status": "accepted",
            "run_id": None,
            "route": route.model_dump(),
            "branch_artifact": _contract_artifact(route),
            "resolved_route_type": "internal_forum",
            "route_source": route_source,
        }

    # Fallback: implicit routing from source_router_for_input
    if route.kind == "website":
        display_name = name or default_website_display_name(route.normalized_input)
        run_id = start_discovery_run(route.normalized_input, force=force, name=display_name)
        return {
            "status": "started",
            "run_id": run_id,
            "route": route.model_dump(),
            "delegated": "website_discovery",
            "resolved_route_type": "website",
            "route_source": route_source,
        }

    if route.kind == "wechat" and route.input_type == "wechat_search":
        template_variant = str((hints or {}).get("template_variant") or "search_then_article_enrich")
        fetch_content = bool((hints or {}).get("fetch_content", True))
        max_pages = int((hints or {}).get("max_pages") or DEFAULT_WECHAT_SEARCH_MAX_PAGES)
        recipe = build_wechat_search_recipe(
            route.normalized_input,
            limit=None,
            max_pages=max_pages,
            template_variant=template_variant,
            fetch_content=fetch_content,
            resolve_final_urls_limit=None,
            enrich_max_items=None,
        )
        validation_recipe = build_wechat_search_recipe(
            route.normalized_input,
            limit=None,
            max_pages=max_pages,
            template_variant=template_variant,
            fetch_content=False,
            resolve_final_urls_limit=0,
            enrich_max_items=0 if template_variant == "search_then_article_enrich" else None,
        )
        result = _run_and_save_multi_recipe(
            route=route,
            recipe=recipe,
            run_recipe=validation_recipe,
            force=force,
            name=name,
        )
        result["resolved_route_type"] = "wechat_search"
        result["route_source"] = route_source
        return result

    if route.kind == "wechat":
        limit = int((hints or {}).get("limit") or DEFAULT_WECHAT_HISTORY_LIMIT)
        fetch_content = bool((hints or {}).get("fetch_content", False))
        artifact = {
            "source_kind": "wechat",
            "nickname": route.normalized_input,
            "auth_ref": "wechat_mp_default",
        }
        recipe = build_wechat_history_recipe(
            route.normalized_input, artifact, limit=limit, fetch_content=fetch_content
        )
        result = _run_and_save_multi_recipe(route=route, recipe=recipe, force=force, name=name)
        result["resolved_route_type"] = "wechat_history"
        result["route_source"] = route_source
        return result

    result = {
        "status": "accepted",
        "run_id": None,
        "route": route.model_dump(),
        "branch_artifact": _contract_artifact(route),
    }
    result["resolved_route_type"] = selected_route_type
    result["route_source"] = route_source
    return result


def _append_multi_run_trace(run_id: int, node_trace: list[dict[str, Any]], step: str, summary: dict[str, Any]) -> None:
    from app.db import SessionLocal
    from app.models import SiteDiscoveryRun

    node_trace.append(
        {
            "step": step,
            "status": "done",
            "ts": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
        }
    )
    db = SessionLocal()
    try:
        run = db.get(SiteDiscoveryRun, run_id)
        if run and run.status == "running":
            run.node_trace = list(node_trace)
            db.commit()
    finally:
        db.close()


def _execute_multi_discovery_run(
    run_id: int,
    raw_input: str,
    *,
    force: bool,
    name: str | None,
    hints: dict[str, Any] | None,
    selected_route_type: str | None,
    route_source: str,
) -> None:
    from app.discovery.runtime import finish_discovery_run
    from app.llm.usage import UsageScope, activate_usage_scope, deactivate_usage_scope
    from app.run_logs import append_run_log, run_log_context

    source_label = name or raw_input
    node_trace: list[dict[str, Any]] = []
    usage = UsageScope(
        context_type="discovery",
        trigger_type="manual",
        stage="multi_discovery",
        discovery_run_id=run_id,
    )
    usage_token = activate_usage_scope(usage)
    with run_log_context(run_id):
        try:
            route = source_router_for_input(raw_input, hints)
            _append_multi_run_trace(
                run_id,
                node_trace,
                "fetch_homepage",
                {
                    "status": "ok",
                    "title": source_label,
                    "links": 0,
                    "source_type": selected_route_type or route.input_type,
                },
            )
            _append_multi_run_trace(
                run_id,
                node_trace,
                "capture_network",
                {
                    "json_apis": 0,
                    "sample_urls": route.normalized_input,
                    "route": selected_route_type or route.input_type,
                },
            )
            append_run_log(
                "探查",
                "多源智能探查后台任务开始",
                source=source_label,
                input=raw_input,
                selected_route_type=selected_route_type,
                route_source=route_source,
            )
            result = _run_multi_discovery_sync(
                raw_input,
                force=force,
                name=name,
                hints=hints,
                selected_route_type=selected_route_type,
                route_source=route_source,
            )
            _append_multi_run_trace(
                run_id,
                node_trace,
                "explorer",
                {
                    "source_type": result.get("resolved_route_type") or selected_route_type,
                    "success": True,
                    "list_url": raw_input,
                },
            )
            audit_result = result.get("multi_audit_result") or {}
            _append_multi_run_trace(
                run_id,
                node_trace,
                "auditor",
                {
                    "passed": audit_result.get("passed", result.get("status") == "completed"),
                    "decision": audit_result.get("method_status") or result.get("status"),
                    "issues": audit_result.get("issues"),
                },
            )
            if result.get("method_id") is not None:
                _append_multi_run_trace(
                    run_id,
                    node_trace,
                    "save_method",
                    {
                        "method_id": result.get("method_id"),
                        "method_status": result.get("method_status"),
                    },
                )
            status = "completed" if result.get("status") == "completed" else "failed"
            finish_discovery_run(
                run_id,
                status=status,
                resulting_method_id=result.get("method_id"),
                node_trace=node_trace,
                error_message=None if status == "completed" else str(result.get("status") or "multi discovery failed"),
                llm_token_usage=usage.total_tokens,
            )
            append_run_log(
                "探查",
                "多源智能探查后台任务结束",
                source=source_label,
                status=status,
                method_id=result.get("method_id"),
                token_used=usage.total_tokens,
            )
        except Exception as exc:
            finish_discovery_run(
                run_id,
                status="failed",
                node_trace=node_trace,
                error_message=str(exc),
                llm_token_usage=usage.total_tokens,
            )
            append_run_log(
                "探查",
                f"多源智能探查后台任务失败 · {exc}",
                source=source_label,
                level="error",
                token_used=usage.total_tokens,
            )
            logger.exception("multi discovery run %s failed", run_id)
        finally:
            deactivate_usage_scope(usage_token)


def start_multi_discovery_run(
    raw_input: str,
    *,
    force: bool = False,
    name: str | None = None,
    hints: dict[str, Any] | None = None,
    selected_route_type: str | None = None,
    route_source: str = "inferred",
) -> dict[str, Any]:
    from app.discovery.runtime import create_discovery_run_or_raise

    route = source_router_for_input(raw_input, hints)
    if selected_route_type == "website" or (selected_route_type is None and route.kind == "website"):
        return _run_multi_discovery_sync(
            raw_input,
            force=force,
            name=name,
            hints=hints,
            selected_route_type=selected_route_type,
            route_source=route_source,
        )
    if selected_route_type == "internal_forum":
        return _run_multi_discovery_sync(
            raw_input,
            force=force,
            name=name,
            hints=hints,
            selected_route_type=selected_route_type,
            route_source=route_source,
        )

    run_id = create_discovery_run_or_raise(raw_input)
    threading.Thread(
        target=_execute_multi_discovery_run,
        kwargs={
            "run_id": run_id,
            "raw_input": raw_input,
            "force": force,
            "name": name,
            "hints": hints,
            "selected_route_type": selected_route_type,
            "route_source": route_source,
        },
        daemon=True,
        name=f"multi-discovery-run-{run_id}",
    ).start()
    return {
        "status": "started",
        "run_id": run_id,
        "route": route.model_dump(),
        "resolved_route_type": selected_route_type or route.input_type,
        "route_source": route_source,
    }


def _contract_artifact(route: SourceRoute) -> dict[str, Any]:
    if route.kind == "internal_mcp":
        return {
            "source_kind": "internal_mcp",
            "status": "needs_implementation",
            "markers": route.markers,
            "query": route.normalized_input,
            "mcp_action_contract": {
                "op": "mcp_call",
                "server": "km | iwiki",
                "tool": "search_articles",
                "args": {"query": route.normalized_input, "author": None},
                "as": "last_fetch",
            },
        }
    return {
        "source_kind": route.kind,
        "status": "unsupported",
        "reason": route.reason,
    }
