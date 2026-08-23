from __future__ import annotations

from typing import Any, Callable

from app.discovery.dsl import DslRecipe
from app.discovery.interpreter import DslInterpreter
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
        """执行一个 multi_dsl 配方，按来源类型分派到对应抓取逻辑。

        功能：website 类型转成 DslRecipe 交给 DslInterpreter 执行；微信类型按顺序执行搜索/历史/抽取/补抓/去重等动作，
        把每步结果放进上下文迭代，最终产出 items 与 stats。
        谁会调用：execution.run_method、multi_graph 在执行已存配方时调用。
        直接调用：
        - DslInterpreter(...).run(...)：执行网站类型配方。
        - DslRecipe(...)：构造网站类型配方。
        - wechat_search_articles(...)/wechat_fetch_account_history(...)/wechat_enrich_articles(...)：微信抓取与补抓。
        - _dedup(...)：按字段去重。
        输入与结果：输入 MultiDslRecipe 与可选 progress_callback；返回含 items 与 stats 的 dict。
        副作用：触发微信抓取/补抓（网络）或网站 DSL 执行（网络/浏览器）。
        """
        if recipe.source_kind == "website":
            return DslInterpreter(progress_callback=progress_callback).run(
                DslRecipe(
                    recipe_type="dsl",
                    entry_url=recipe.entry,
                    actions=recipe.actions,
                    notes=recipe.notes,
                )
            )

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
    """按指定字段对条目去重，保留首次出现。

    功能：用集合记录已见 key（字段值），跳过空值或重复项，输出去重后的列表。
    谁会调用：MultiDslInterpreter.run 在执行 dedup_by 动作时调用。
    直接调用：无（仅集合判重）。
    输入与结果：输入条目列表与去重字段名；返回去重后的列表。
    副作用：无。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        key = str(item.get(field) or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
