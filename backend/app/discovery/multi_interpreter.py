from __future__ import annotations

from typing import Any, Callable

from app.discovery.multi_dsl import MultiDslRecipe
from app.discovery.wechat_tools import (
    wechat_enrich_articles,
    wechat_fetch_account_history,
    wechat_search_articles,
)


class MultiDslInterpreter:
    def run(
        self,
        recipe: MultiDslRecipe,
        *,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict:
        ctx: dict = {"items": [], "last_fetch": None}
        for action in recipe.actions:
            op = action.get("op")
            if op == "wechat_search_articles":
                ctx["last_fetch"] = wechat_search_articles(
                    query=action["query"],
                    limit=int(action["limit"]) if action.get("limit") is not None else None,
                    max_pages=int(action["max_pages"]) if action.get("max_pages") is not None else None,
                    resolve_final_urls_limit=(
                        int(action["resolve_final_urls_limit"])
                        if action.get("resolve_final_urls_limit") is not None
                        else None
                    ),
                    progress_callback=progress_callback,
                )
            elif op == "wechat_fetch_account_history":
                ctx["last_fetch"] = wechat_fetch_account_history(
                    nickname=action.get("nickname"),
                    account_id=action.get("account_id"),
                    fakeid=action.get("fakeid"),
                    biz=action.get("__biz"),
                    limit=int(action.get("limit") or 30),
                    fetch_content=bool(action.get("fetch_content")),
                    auth_ref=action.get("auth_ref") or "wechat_mp_default",
                )
            elif op == "extract":
                source = ctx.get("last_fetch") or {}
                ctx["items"] = list(source.get("items") or [])
            elif op == "enrich_wechat_articles":
                source_items = ctx.get("items") or []
                ctx["last_fetch"] = wechat_enrich_articles(
                    source_items,
                    fetch_content=bool(action.get("fetch_content", True)),
                    fill_missing_only=bool(action.get("fill_missing_only", True)),
                    max_items=int(action["max_items"]) if action.get("max_items") is not None else None,
                    skip_url_keys=action.get("skip_url_keys") if isinstance(action.get("skip_url_keys"), list) else None,
                    precheck_topic_with_llm=bool(action.get("precheck_topic_with_llm", False)),
                    progress_callback=progress_callback,
                )
                ctx["items"] = list((ctx.get("last_fetch") or {}).get("items") or [])
            elif op == "dedup_by":
                ctx["items"] = _dedup(ctx["items"], action.get("field") or "url")
            else:
                return {"items": [], "stats": {"status": "unsupported_action", "op": op}}
        return {
            "items": ctx["items"],
            "stats": {
                "discovered_count": len(ctx["items"]),
                "status": (ctx.get("last_fetch") or {}).get("status", "ok"),
                "source_kind": recipe.source_kind,
            },
        }


def _dedup(items: list[dict], field: str) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        key = str(item.get(field) or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
