"""DSL 解释执行器，按 actions 顺序执行 DSL，零 LLM。

维护一个 context（vars/items/last_fetch/browser），每个 action 读/写 context，
最后吐 {"items": [...], "stats": {...}} 给 CrawlOutputIngester。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable
from urllib.parse import urlencode

from app.discovery.dsl import (
    Action,
    ClickAction,
    DedupByAction,
    DslRecipe,
    EnrichArticleApiAction,
    EnrichArticlePagesAction,
    ExtractAction,
    FetchAction,
    GotoAction,
    LoopAction,
    SetAction,
    WaitForAction,
    render_vars,
)

logger = logging.getLogger(__name__)


class DslExecutionPartialError(RuntimeError):
    """DSL 执行中途失败，但已抓到部分结果。"""

    def __init__(self, message: str, *, items: list[dict], stats: dict[str, Any]) -> None:
        super().__init__(message)
        self.items = items
        self.stats = stats


class _DslStopExecution(RuntimeError):
    """达到条目上限后中止执行的内部信号。"""


class DslInterpreter:
    """解释执行 DslRecipe，逐 action 分发到原语处理。

    fetch_fn / browser_fn 用于测试注入 mock；默认走真实 httpx / Playwright。
    """

    def __init__(
        self,
        fetch_fn: Callable[..., Any] | None = None,
        browser_fn: Callable[..., Any] | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._fetch_fn = fetch_fn
        self._browser_fn = browser_fn
        self._progress_callback = progress_callback
        self._page = None  # Playwright 页面句柄（Task 6 用，懒加载）

    def run(self, recipe: DslRecipe, *, max_items: int | None = None) -> dict[str, Any]:
        """执行整份 Recipe，返回约定 JSON 产出。"""
        ctx: dict[str, Any] = {
            "vars": {"entry_url": recipe.entry_url},
            "items": [],
            "last_fetch": None,
        }
        try:
            for action in recipe.actions:
                self._exec(action, ctx, max_items=max_items)
                self._check_max_items(ctx, max_items)
        except _DslStopExecution:
            pass
        except Exception as e:
            if ctx["items"]:
                raise DslExecutionPartialError(
                    str(e),
                    items=list(ctx["items"]),
                    stats={"discovered_count": len(ctx["items"])},
                ) from e
            raise
        finally:
            self._cleanup()
        return {"items": ctx["items"], "stats": {"discovered_count": len(ctx["items"])}}

    def _exec(self, action: Action, ctx: dict[str, Any], *, max_items: int | None = None) -> None:
        """按 action 类型分发到对应原语处理。"""
        if isinstance(action, FetchAction):
            self._fetch(action, ctx)
        elif isinstance(action, ExtractAction):
            self._extract(action, ctx)
        elif isinstance(action, SetAction):
            self._set(action, ctx)
        elif isinstance(action, DedupByAction):
            self._dedup(action, ctx)
        elif isinstance(action, EnrichArticlePagesAction):
            self._enrich_article_pages(action, ctx)
        elif isinstance(action, EnrichArticleApiAction):
            self._enrich_article_api(action, ctx)
        elif isinstance(action, LoopAction):
            self._loop(action, ctx, max_items=max_items)
        elif isinstance(action, (GotoAction, WaitForAction, ClickAction)):
            self._browser_action(action, ctx)

    def _browser_action(
        self, action: GotoAction | WaitForAction | ClickAction, ctx: dict[str, Any]
    ) -> None:
        """Playwright 浏览器动作（goto/wait_for/click），懒加载浏览器。"""
        if self._browser_fn:
            self._browser_fn(action, ctx, page=self._page)  # 测试 mock 路径
            return
        # 真实 Playwright（运行命用），首次调用时懒加载
        from playwright.sync_api import sync_playwright

        if self._page is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_page()
        if action.op == "goto":
            self._page.goto(render_vars(action.url, ctx), wait_until=action.wait_until)
        elif action.op == "wait_for":
            self._page.wait_for_selector(action.selector, timeout=action.timeout_ms)
        elif action.op == "click":
            self._page.click(action.selector)
            self._page.wait_for_timeout(action.after_wait_ms)  # 点击后等待响应

    def _fetch(self, action: FetchAction, ctx: dict[str, Any]) -> None:
        """HTTP 获取，按 mode 解析后存入 ctx[last_fetch]。"""
        if self._fetch_fn:
            self._fetch_fn(action, ctx)
            return  # 测试 mock 路径

        url = render_vars(action.url, ctx)  # 变量替换（如 {{page}}）
        headers = {k: render_vars(v, ctx) for k, v in action.headers.items()}
        params = {k: render_vars(v, ctx) for k, v in action.query.items()}
        body = self._render_json_like(action.json_body, ctx) if action.json_body is not None else None
        r = self._request(action, url, headers=headers, params=params, body=body, ctx=ctx)
        text = self._response_text(r)
        if action.mode == "json":
            try:
                ctx["last_fetch"] = r.json()
            except Exception:
                ctx["last_fetch"] = self._decode_json_lenient(text)
        elif action.mode == "feed":
            import feedparser

            ctx["last_fetch"] = {"feed": feedparser.parse(text)}
        else:
            ctx["last_fetch"] = {"html": text}
            self._load_html_page_for_extract(url, params=params)

    def _request(
        self,
        action: FetchAction,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
        body: Any,
        ctx: dict[str, Any],
    ) -> Any:
        if action.transport == "scrapling":
            if action.method.upper() != "GET":
                raise RuntimeError("scrapling transport currently supports GET fetch actions only")
            from scrapling.fetchers import Fetcher

            return Fetcher.get(
                url,
                headers=headers or None,
                params=params or None,
                stealthy_headers=action.stealthy_headers,
                impersonate=action.impersonate or "chrome",
            )

        import httpx

        try:
            return httpx.request(action.method, url, headers=headers, params=params, json=body, timeout=30)
        except httpx.ReadTimeout as e:
            page_value = params.get("page")
            if page_value is None and isinstance(body, dict):
                page_value = body.get("page")
            if page_value is None:
                page_value = ctx.get("vars", {}).get("page")
            query_text = "&".join(f"{k}={v}" for k, v in params.items()) or "none"
            body_text = json.dumps(body, ensure_ascii=False) if body is not None else "none"
            detail = f"fetch timeout at page={page_value or 'unknown'} url={url} query={query_text} json_body={body_text}"
            logger.warning(detail)
            raise RuntimeError(detail) from e

    def _response_text(self, response: Any) -> str:
        body = getattr(response, "body", None)
        if isinstance(body, bytes):
            encoding = getattr(response, "encoding", None) or "utf-8"
            return body.decode(encoding, errors="replace")
        text = getattr(response, "text", None)
        if callable(text):
            text = text()
        if text is not None:
            return str(text)
        html_content = getattr(response, "html_content", None)
        if html_content is not None:
            return str(html_content)
        return str(response)

    def _load_html_page_for_extract(self, url: str, *, params: dict[str, str]) -> None:
        """为后续 selector:extract 准备真实 DOM 页面。"""
        from playwright.sync_api import sync_playwright

        if self._page is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_page()
        target_url = url
        if params:
            query_text = urlencode(params)
            sep = "&" if "?" in url else "?"
            target_url = f"{url}{sep}{query_text}"
        self._page.goto(target_url, wait_until="networkidle")

    def _render_json_like(self, value: Any, ctx: dict[str, Any]) -> Any:
        """递归渲染 JSON 载荷中的字符串变量，保留 int/bool/null 等原始类型。"""
        if isinstance(value, str):
            return render_vars(value, ctx)
        if isinstance(value, list):
            return [self._render_json_like(v, ctx) for v in value]
        if isinstance(value, dict):
            return {k: self._render_json_like(v, ctx) for k, v in value.items()}
        return value

    def _decode_json_lenient(self, text: str) -> Any:
        """按标准 JSON 解析；若响应拼接了多个 JSON，则只取第一个对象。"""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(text)
            return obj

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
        """按 selector: 从 Playwright 页面提取 items。"""
        if self._browser_fn:
            return self._browser_fn(action, ctx, page=self._page) or []
        elements = self._page.query_selector_all(action.from_.removeprefix("selector:"))
        result: list[dict] = []
        for el in elements:
            item: dict[str, Any] = {}
            for field, spec in action.fields.items():
                item[field] = self._resolve_html_field(spec, el)
            result.append(item)
        return result

    def _resolve_html_field(self, spec: Any, el: Any) -> str:
        """解析 HTML 字段：attr:取属性，其余按 selector 取文本。"""
        if spec == "self":
            return (el.inner_text() or "").strip()
        if isinstance(spec, str) and spec.startswith("attr:"):
            attr = spec.removeprefix("attr:")
            child = el.query_selector(f"[{attr}]") or el
            if attr in {"href", "src"}:
                return child.evaluate("(node, attr) => node[attr] || node.getAttribute(attr) || ''", attr) or ""
            return child.get_attribute(attr) or ""
        # "selector:text" 或裸 selector
        sel = spec.split(":")[0] if ":" in spec else spec
        child = el.query_selector(sel)
        return child.inner_text() if child else ""

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

    def _enrich_article_pages(self, action: EnrichArticlePagesAction, ctx: dict[str, Any]) -> None:
        from app.discovery.article_tools import enrich_article_pages

        result = enrich_article_pages(
            ctx.get("items") or [],
            fetch_content=action.fetch_content,
            fill_missing_only=action.fill_missing_only,
            max_items=action.max_items,
            timeout_seconds=action.timeout_seconds,
            content_char_limit=action.content_char_limit,
            min_existing_chars=action.min_existing_chars,
            progress_callback=self._progress_callback,
        )
        ctx["last_fetch"] = result
        ctx["items"] = list(result.get("items") or [])

    def _enrich_article_api(self, action: EnrichArticleApiAction, ctx: dict[str, Any]) -> None:
        import httpx

        next_items = [dict(item) for item in (ctx.get("items") or [])]
        attempted = 0
        enriched = 0
        for item in next_items:
            existing_content = " ".join(str(item.get("content") or "").split())
            if action.fill_missing_only and len(existing_content) >= action.min_existing_chars:
                continue
            if action.max_items is not None and attempted >= action.max_items:
                break
            attempted += 1
            url = self._render_item_template(action.url_template, item, ctx)
            headers = {k: self._render_item_template(v, item, ctx) for k, v in action.headers.items()}
            params = {k: self._render_item_template(v, item, ctx) for k, v in action.query.items()}
            body = self._render_item_json_like(action.json_body, item, ctx) if action.json_body is not None else None
            try:
                request_kwargs = {
                    "timeout": action.timeout_seconds,
                    "follow_redirects": True,
                }
                if headers:
                    request_kwargs["headers"] = headers
                if params:
                    request_kwargs["params"] = params
                if body is not None:
                    request_kwargs["json"] = body
                response = httpx.request(action.method, url, **request_kwargs)
                response.raise_for_status()
                payload = response.json()
            except Exception:
                continue
            changed = False
            for field, path in action.fields.items():
                value = self._json_path_value(payload, path)
                if value in (None, ""):
                    continue
                text = str(value)
                if field == "content":
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = " ".join(text.split())[: action.content_char_limit]
                if action.fill_missing_only and item.get(field):
                    continue
                item[field] = text
                changed = True
            if changed and item.get("content"):
                enriched += 1
        ctx["last_fetch"] = {
            "status": "ok" if enriched else "empty",
            "attempted_count": attempted,
            "enriched_count": enriched,
        }
        ctx["items"] = next_items

    def _render_item_template(self, text: str, item: dict[str, Any], ctx: dict[str, Any]) -> str:
        def repl_item(m: "re.Match[str]") -> str:
            return str(item.get(m.group(1), ""))

        rendered = re.sub(r"\{item\.(\w+)\}", repl_item, text)
        return render_vars(rendered, ctx)

    def _render_item_json_like(self, value: Any, item: dict[str, Any], ctx: dict[str, Any]) -> Any:
        if isinstance(value, str):
            return self._render_item_template(value, item, ctx)
        if isinstance(value, list):
            return [self._render_item_json_like(v, item, ctx) for v in value]
        if isinstance(value, dict):
            return {k: self._render_item_json_like(v, item, ctx) for k, v in value.items()}
        return value

    def _json_path_value(self, payload: Any, path: str) -> Any:
        node = payload
        for part in path.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            elif isinstance(node, list) and part.isdigit():
                node = node[int(part)]
            else:
                return None
        return node

    def _loop(self, action: LoopAction, ctx: dict[str, Any], *, max_items: int | None = None) -> None:
        """循环：until 条件为真或 max_iters 用尽则停（先到先停，防死循环）。"""
        from app.discovery.dsl import eval_condition
        for _ in range(action.max_iters):
            if eval_condition(action.until.model_dump(), ctx):
                break  # 终止条件满足，退出循环
            for sub in action.body:  # 执行循环体
                self._exec(sub, ctx, max_items=max_items)
                self._check_max_items(ctx, max_items)
            for sub in action.on_each:  # 每轮后置动作（如 page+1）
                self._exec(sub, ctx, max_items=max_items)
                self._check_max_items(ctx, max_items)

    def _check_max_items(self, ctx: dict[str, Any], max_items: int | None) -> None:
        if max_items is None:
            return
        items = ctx.get("items", [])
        if len(items) >= max_items:
            ctx["items"] = items[:max_items]
            raise _DslStopExecution()

    def _cleanup(self) -> None:
        """关闭 Playwright 浏览器，run 结束时调用（mock 路径下 _page 为 None，no-op）。"""
        if self._page is not None:
            try:
                self._browser.close()
                self._pw.stop()
            except Exception:
                pass
