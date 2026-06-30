"""DSL 解释执行器，按 actions 顺序执行 DSL，零 LLM。

维护一个 context（vars/items/last_fetch/browser），每个 action 读/写 context，
最后吐 {"items": [...], "stats": {...}} 给 CrawlOutputIngester。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from app.discovery.dsl import (
    Action,
    DedupByAction,
    DslRecipe,
    ExtractAction,
    FetchAction,
    LoopAction,
    SetAction,
    render_vars,
)


class DslInterpreter:
    """解释执行 DslRecipe，逐 action 分发到原语处理。

    fetch_fn / browser_fn 用于测试注入 mock；默认走真实 httpx / Playwright。
    """

    def __init__(
        self,
        fetch_fn: Callable[..., Any] | None = None,
        browser_fn: Callable[..., Any] | None = None,
    ) -> None:
        self._fetch_fn = fetch_fn
        self._browser_fn = browser_fn

    def run(self, recipe: DslRecipe) -> dict[str, Any]:
        """执行整份 Recipe，返回约定 JSON 产出。"""
        ctx: dict[str, Any] = {
            "vars": {"entry_url": recipe.entry_url},
            "items": [],
            "last_fetch": None,
        }
        for action in recipe.actions:
            self._exec(action, ctx)
        return {"items": ctx["items"], "stats": {"discovered_count": len(ctx["items"])}}

    def _exec(self, action: Action, ctx: dict[str, Any]) -> None:
        """按 action 类型分发到对应原语处理。"""
        if isinstance(action, FetchAction):
            self._fetch(action, ctx)
        elif isinstance(action, ExtractAction):
            self._extract(action, ctx)
        elif isinstance(action, SetAction):
            self._set(action, ctx)
        elif isinstance(action, DedupByAction):
            self._dedup(action, ctx)
        elif isinstance(action, LoopAction):
            self._loop(action, ctx)
        # goto/wait_for/click 在 Task 6

    def _fetch(self, action: FetchAction, ctx: dict[str, Any]) -> None:
        """HTTP 获取，按 mode 解析后存入 ctx[last_fetch]。"""
        if self._fetch_fn:
            self._fetch_fn(action, ctx)
            return  # 测试 mock 路径
        import httpx

        url = render_vars(action.url, ctx)  # 变量替换（如 {{page}}）
        headers = {k: render_vars(v, ctx) for k, v in action.headers.items()}
        body = (
            {k: render_vars(v, ctx) for k, v in (action.json_body or {}).items()}
            if action.json_body
            else None
        )
        r = httpx.request(action.method, url, headers=headers, json=body, timeout=30)
        if action.mode == "json":
            ctx["last_fetch"] = r.json()
        elif action.mode == "feed":
            import feedparser

            ctx["last_fetch"] = {"feed": feedparser.parse(r.text)}
        else:
            ctx["last_fetch"] = {"html": r.text}

    def _extract(self, action: ExtractAction, ctx: dict[str, Any]) -> None:
        """提取字段，merge=True 追加（翻页），False 覆盖。"""
        items = self._extract_items(action, ctx)
        if action.merge:
            ctx["items"].extend(items)
        else:
            ctx[action.into] = items
            if action.into == "items":
                ctx["items"] = items

    def _extract_items(self, action: ExtractAction, ctx: dict[str, Any]) -> list[dict]:
        """按 from 取记录列表：json path 逐层取，selector: 走 HTML 提取。"""
        if action.from_.startswith("selector:"):
            return self._extract_from_html(action, ctx)  # Task 6 实装 Playwright
        # json path 逐层取
        node: Any = ctx.get("last_fetch")
        for p in action.from_.split("."):
            node = node.get(p) if isinstance(node, dict) else None
        records = node if isinstance(node, list) else []
        result: list[dict] = []
        for rec in records:
            item: dict[str, Any] = {}
            for field, spec in action.fields.items():
                item[field] = self._resolve_field(spec, rec, ctx)
            result.append(item)
        return result

    def _resolve_field(self, spec: Any, rec: dict, ctx: dict[str, Any]) -> str:
        """解析字段值：支持候选数组/template:/attr:/裸字段名。"""
        if isinstance(spec, list):
            # 多个候选，第一个非空者用
            for s in spec:
                v = self._resolve_field(s, rec, ctx)
                if v:
                    return v
            return ""
        if isinstance(spec, str) and spec.startswith("template:"):
            # 模板替换：先 {item.xxx} 用当前条目字段填，再 {{var}} 用 context 变量填
            tmpl = spec.removeprefix("template:")

            def repl_item(m: "re.Match[str]") -> str:
                return str(rec.get(m.group(1), ""))

            tmpl = re.sub(r"\{item\.(\w+)\}", repl_item, tmpl)
            return render_vars(tmpl, ctx)
        if isinstance(spec, str) and spec.startswith("attr:"):
            return ""  # HTML 属性提取在 Task 6
        # 裸字段名，直接取
        return str(rec.get(spec, "")) if isinstance(spec, str) else ""

    def _extract_from_html(self, action: ExtractAction, ctx: dict[str, Any]) -> list[dict]:
        return []  # Task 6 实装

    def _set(self, action: SetAction, ctx: dict[str, Any]) -> None:
        """设置变量：expr 走简单算术表达式，value 走字面量。"""
        if action.expr:
            # 简单表达式：{{var}} + N，先替换变量再求值
            def repl(m: "re.Match[str]") -> str:
                v = ctx["vars"].get(m.group(1), 0)
                return str(v)

            rendered = re.sub(r"\{\{(\w+)\}\}", repl, action.expr)
            ctx["vars"][action.var] = eval(rendered, {"__builtins__": {}}, {})  # 仅纯算术，禁内置
        else:
            ctx["vars"][action.var] = action.value

    def _dedup(self, action: DedupByAction, ctx: dict[str, Any]) -> None:
        """按字段去重 items（通常对 url）。"""
        seen: set = set()
        out: list[dict] = []
        for it in ctx["items"]:
            k = it.get(action.field)
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
        ctx["items"] = out

    def _loop(self, action: LoopAction, ctx: dict[str, Any]) -> None:
        """循环：until 条件为真或 max_iters 用尽则停（先到先停，防死循环）。"""
        from app.discovery.dsl import eval_condition
        for _ in range(action.max_iters):
            if eval_condition(action.until.model_dump(), ctx):
                break  # 终止条件满足，退出循环
            for sub in action.body:  # 执行循环体
                self._exec(sub, ctx)
            for sub in action.on_each:  # 每轮后置动作（如 page+1）
                self._exec(sub, ctx)
