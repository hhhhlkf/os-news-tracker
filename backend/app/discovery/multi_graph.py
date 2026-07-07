from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.discovery.graph import (
    DiscoveryState,
    auditor,
    dsl_writer,
    start_discovery_run,
)

logger = logging.getLogger(__name__)

BranchKind = Literal["website", "wechat", "internal_mcp", "unsupported"]


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
    hinted_kind = hints.get("source_kind")
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

    if hinted_kind == "wechat":
        return SourceRoute(
            kind="wechat",
            confidence=1.0,
            normalized_input=normalized,
            input_type=_classify_wechat_input(normalized),
            reason="source_kind hint requested wechat",
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
        kind="unsupported",
        confidence=0.7,
        normalized_input=normalized,
        input_type="keyword",
        reason="plain keyword input needs an explicit source_kind hint",
        suggested_branch="unsupported",
    )


def _classify_wechat_input(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        return "wechat_history_url"
    return "wechat_account"


def build_wechat_search_recipe(
    entry: str,
    *,
    limit: int | None = None,
    max_pages: int | None = 10,
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
    limit: int = 100,
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
            {"op": "dedup_by", "field": "url"},
        ],
        "notes": ["WeChat account history uses the default wechat_mp_default auth profile."],
    }


def _required_count(recipe: dict[str, Any]) -> int:
    limit = 20
    for action in recipe.get("actions") or []:
        if action.get("op") in {"wechat_fetch_account_history", "wechat_search_articles"}:
            if action.get("limit") is not None:
                limit = int(action["limit"])
            break
    return min(5, max(1, int(limit * 0.25)))


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

    effective_run_recipe = run_recipe or recipe
    output = MultiDslInterpreter().run(MultiDslRecipe(**effective_run_recipe))
    items = output.get("items") or []
    status = (output.get("stats") or {}).get("status")

    # Classify method status
    if status in {"pending_auth", "auth_invalid"}:
        method_status = status
    elif status in {"rate_limited", "captcha_required"}:
        method_status = "retry_later"
    elif len(items) >= _required_count(effective_run_recipe):
        method_status = "active"
    else:
        method_status = "failed"

    sanitized_recipe = _sanitize_recipe_for_storage(recipe)
    sig = _compute_multi_signature(sanitized_recipe)

    db = SessionLocal()
    try:
        from datetime import datetime as dt
        from app.enums import SourceType, Stream
        from app.models import CrawlMethod, CrawlMethodDomain, Source

        if urlparse(route.normalized_input).scheme:
            domain = urlparse(route.normalized_input).netloc
        elif route.kind == "wechat" and not recipe.get("requires_auth"):
            # Keyword search — shared domain across all search queries
            domain = "wechat_search"
        elif route.input_type == "wechat_account":
            # Account history — one domain per account
            domain = f"wechat_mp_{route.normalized_input}"
        else:
            domain = "wechat_search"
        if not domain:
            domain = route.kind

        if name:
            display_name = name
        elif route.kind == "wechat":
            if recipe.get("requires_auth"):
                display_name = f"公众号: {route.normalized_input}"
            else:
                display_name = f"微信搜索: {route.normalized_input}"
        else:
            display_name = route.normalized_input[:80]

        existing_domain = db.query(CrawlMethodDomain).filter_by(domain=domain).first()

        if existing_domain is not None and force:
            m = db.get(CrawlMethod, existing_domain.method_id)
            m.dsl_recipe = sanitized_recipe
            m.signature = sig
            m.status = method_status
            m.updated_at = dt.now(timezone.utc)
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
            }

        if existing_domain is not None:
            m = db.get(CrawlMethod, existing_domain.method_id)
            return {
                "status": "completed",
                "method_id": m.id,
                "route": route.model_dump(),
                "discovered_count": len(items),
                "method_status": m.status,
                "note": "existing method reused (force=false)",
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
        )
        db.add(m)
        db.flush()

        db.add(CrawlMethodDomain(domain=domain, method_id=m.id))
        db.commit()

        return {
            "status": "completed",
            "method_id": m.id,
            "method_status": method_status,
            "route": route.model_dump(),
            "discovered_count": len(items),
        }
    finally:
        db.close()


def start_multi_discovery_run(
    raw_input: str,
    *,
    force: bool = False,
    name: str | None = None,
    hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    route = source_router_for_input(raw_input, hints)

    if route.kind == "website":
        run_id = start_discovery_run(route.normalized_input, force=force, name=name)
        return {
            "status": "started",
            "run_id": run_id,
            "route": route.model_dump(),
            "delegated": "website_discovery",
        }

    if route.kind == "wechat" and (hints or {}).get("wechat_mode") == "search":
        template_variant = str((hints or {}).get("template_variant") or "search_then_article_enrich")
        fetch_content = bool((hints or {}).get("fetch_content", True))
        max_pages = int((hints or {}).get("max_pages") or 10)
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
        return _run_and_save_multi_recipe(
            route=route,
            recipe=recipe,
            run_recipe=validation_recipe,
            force=force,
            name=name,
        )

    if route.kind == "wechat":
        limit = int((hints or {}).get("limit") or 100)
        fetch_content = bool((hints or {}).get("fetch_content", False))
        artifact = {
            "source_kind": "wechat",
            "nickname": route.normalized_input,
            "auth_ref": "wechat_mp_default",
        }
        recipe = build_wechat_history_recipe(
            route.normalized_input, artifact, limit=limit, fetch_content=fetch_content
        )
        return _run_and_save_multi_recipe(route=route, recipe=recipe, force=force, name=name)

    return {
        "status": "accepted",
        "run_id": None,
        "route": route.model_dump(),
        "branch_artifact": _contract_artifact(route),
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
