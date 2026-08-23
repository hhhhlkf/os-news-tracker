"""DSL 解释执行器，按 actions 顺序执行 DSL，零 LLM。

维护一个 context（vars/items/last_fetch/browser），每个 action 读/写 context，
最后吐 {"items": [...], "stats": {...}} 给 CrawlOutputIngester。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode

from app.discovery.browser_actions import exercise_dynamic_page
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
from app.discovery.partial_output import PartialExecutionError

logger = logging.getLogger(__name__)


class DslExecutionPartialError(PartialExecutionError):
    """DSL 执行中途失败，但已抓到部分结果。"""

    def __init__(self, message: str, *, items: list[dict], stats: dict[str, Any]) -> None:
        """构造「DSL 中途失败但已有部分结果」的异常。

        功能：携带已抓到的 items 与统计，便于上层保留部分成果而不是整体失败。
        谁会调用：DslInterpreter.run 在捕获异常且 ctx 已有 items 时抛出。
        直接调用：super().__init__(...) 设置异常消息。
        输入与结果：输入消息、items 列表与 stats 字典；无返回值（设置实例属性）。
        副作用：无。
        """
        super().__init__(message, items=items, stats=stats)


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
        """初始化 DSL 解释器，注入可选的 fetch/browser mock 与进度回调。

        功能：保存抓取函数、浏览器函数与进度回调；Playwright 页面句柄延迟到首次需要时懒加载。
        谁会调用：MultiDslInterpreter.run（网站分支）、测试或 run 入口构造解释器时调用。
        直接调用：无（仅保存属性）。
        输入与结果：输入 fetch_fn、browser_fn 与 progress_callback（均可空）；无返回值。
        副作用：无。
        """
        self._fetch_fn = fetch_fn
        self._browser_fn = browser_fn
        self._progress_callback = progress_callback
        self._page = None  # Playwright 页面句柄（Task 6 用，懒加载）

    def run(self, recipe: DslRecipe, *, max_items: int | None = None) -> dict[str, Any]:
        """执行整份 Recipe：逐 action 分发，返回 {items, stats} 产出。

        功能：初始化 context，按序执行每个 action 并在每步后检查条目上限；执行中异常且已有 items 则抛 DslExecutionPartialError，
        否则原样抛出；最终关闭浏览器并返回产出。
        谁会调用：MultiDslInterpreter.run（网站分支）、测试直接调用。
        直接调用：
        - self._exec(...)：执行单个 action。
        - self._check_max_items(...)：检查/截断条目上限。
        - self._cleanup(...)：关闭浏览器。
        - DslExecutionPartialError(...)：包装部分失败。
        输入与结果：输入 DslRecipe 与可选 max_items；返回 {"items": [...], "stats": {...}}。
        副作用：触发真实抓取/浏览器操作，可能通过 progress_callback 上报进度，并关闭浏览器。
        """
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
        """按 action 类型分发到对应的原语处理方法。

        功能：用 isinstance 判断 action 是 Fetch/Extract/Set/DedupBy/Enrich/EnrichApi/Loop/Browser 之一，转发到对应私有方法。
        谁会调用：run（主循环）、_loop（循环体/后置动作）调用。
        直接调用：
        - self._fetch/_extract/_set/_dedup/_enrich_article_pages/_enrich_article_api/_loop/_browser_action。
        输入与结果：输入 action、ctx 与 max_items；无返回值（直接写 ctx）。
        副作用：因分发的动作而异（抓取、浏览器、写入 ctx 等）。
        """
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
        """执行 Playwright 浏览器动作（goto / wait_for / click），懒加载浏览器。

        功能：若有 browser_fn 则走 mock 路径；否则首次懒加载 Playwright 后，按 op 执行页面导航、等待选择器或点击并等待响应。
        谁会调用：_exec 在分发到 GotoAction/WaitForAction/ClickAction 时调用。
        直接调用：
        - self._browser_fn(...)：测试 mock 路径。
        - sync_playwright(...)：懒加载真实浏览器。
        - render_vars(...)：渲染 URL 变量。
        - exercise_dynamic_page(...)：触发动态内容加载。
        - self._page API（goto/wait_for_selector/click/wait_for_timeout）：真实浏览器操作。
        输入与结果：输入 action 与 ctx；无返回值（操作页面）。
        副作用：真实浏览器导航/点击（网络），写入页面状态。
        """
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
            exercise_dynamic_page(self._page, scroll_rounds=2, wait_ms=500)
        elif action.op == "wait_for":
            self._page.wait_for_selector(action.selector, timeout=action.timeout_ms)
        elif action.op == "click":
            self._page.click(action.selector)
            self._page.wait_for_timeout(action.after_wait_ms)  # 点击后等待响应

    def _fetch(self, action: FetchAction, ctx: dict[str, Any]) -> None:
        """执行一次 HTTP 获取，并按 mode 解析后存入 ctx["last_fetch"]。

        功能：渲染 URL/请求头/查询/JSON 体变量，发起请求并取响应文本；按 json/feed/html 三种 mode 解析为 last_fetch 供后续 extract 使用。
        谁会调用：_exec 在分发到 FetchAction 时调用。
        直接调用：
        - self._fetch_fn(...)：测试 mock 路径。
        - render_vars(...)：渲染 URL/头/查询变量。
        - self._render_json_like(...)：渲染 JSON body 变量。
        - self._request(...)：真正发起请求。
        - self._response_text(...)：取响应文本。
        - self._decode_json_lenient(...)：容错解析 JSON（feed 模式用 feedparser）。
        输入与结果：输入 action 与 ctx；无返回值（写 ctx["last_fetch"]）。
        副作用：发起 HTTP 请求（网络）。
        """
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
            ctx["last_fetch"] = {"html": text, "_url": url, "_params": params}

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
        """真正发起一次 HTTP 请求（支持 scrapling / httpx 两种 transport）。

        功能：scrapling 仅支持 GET（否则报错），用 Fetcher.get 带伪装头请求；否则用 httpx.request 发送，读超时则包装成 RuntimeError 并带页码信息。
        谁会调用：_fetch 在发起请求时调用。
        直接调用：
        - Fetcher.get(...)：scrapling 抓取。
        - httpx.request(...)：httpx 抓取（超时抛 httpx.ReadTimeout）。
        输入与结果：输入 action、url、headers、params、body、ctx；返回响应对象。
        副作用：发起 HTTP 请求（网络）。
        """
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
        """从响应对象中尽量取出文本内容（兼容 bytes / 可调用 / 属性）。

        功能：依次尝试 body(bytes 解码)/text(可调用或字符串)/html_content，最后退回 str(response)，保证统一拿到文本。
        谁会调用：_fetch 在读取响应体时调用。
        直接调用：无（仅属性读取与解码）。
        输入与结果：输入响应对象；返回字符串。
        副作用：无。
        """
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
        """为后续 selector:extract 准备真实 DOM 页面（懒加载浏览器并打开 URL）。

        功能：首次懒加载 Playwright，把 URL 与查询参数拼接后导航到页面并触发动态内容加载，供 HTML 选择器提取。
        谁会调用：_extract_from_html 在需要做真实 HTML 提取且尚未打开页面时调用。
        直接调用：
        - sync_playwright(...)：懒加载浏览器。
        - urlencode(...)：拼接查询参数。
        - self._page.goto(...)/exercise_dynamic_page(...)：导航并渲染动态内容。
        输入与结果：输入 url 与查询参数；无返回值（加载到 self._page）。
        副作用：真实浏览器导航（网络）。
        """
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
        exercise_dynamic_page(self._page, scroll_rounds=2, wait_ms=500)

    def _render_json_like(self, value: Any, ctx: dict[str, Any]) -> Any:
        """递归渲染 JSON 载荷中的字符串变量，保留 int/bool/null 原始类型。

        功能：对字符串调用 render_vars 替换 {{var}}，对 list/dict 递归渲染，其它类型原样返回，避免破坏 JSON 结构。
        谁会调用：_fetch 在渲染 JSON body 时调用。
        直接调用：
        - render_vars(...)：渲染字符串变量。
        输入与结果：输入任意值与 ctx；返回渲染后的同结构值。
        副作用：无。
        """
        if isinstance(value, str):
            return render_vars(value, ctx)
        if isinstance(value, list):
            return [self._render_json_like(v, ctx) for v in value]
        if isinstance(value, dict):
            return {k: self._render_json_like(v, ctx) for k, v in value.items()}
        return value

    def _decode_json_lenient(self, text: str) -> Any:
        """容错解析 JSON：标准解析失败则只取第一个 JSON 对象。

        功能：先 json.loads；若响应里拼接了多个 JSON 文本（常见后端把多个对象拼在同一响应），用 raw_decode 取第一个对象。
        谁会调用：_fetch 在 json 模式解析失败时使用。
        直接调用：
        - json.loads(...)/json.JSONDecoder().raw_decode(...)：解析 JSON。
        输入与结果：输入文本；返回解析出的对象。
        副作用：无。
        """
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(text)
            return obj

    def _extract(self, action: ExtractAction, ctx: dict[str, Any]) -> None:
        """根据 ExtractAction 提取字段并写入 ctx（merge 决定追加还是覆盖）。

        功能：调用 _extract_items 得到条目列表；merge=True 时追加进 ctx["items"]（翻页累积），否则覆盖对应 into 键（items 则直接替换）。
        谁会调用：_exec 在分发到 ExtractAction 时调用。
        直接调用：
        - self._extract_items(...)：实际按 from 提取条目。
        输入与结果：输入 action 与 ctx；无返回值（写 ctx["items"] 或 ctx[into]）。
        副作用：无。
        """
        items = self._extract_items(action, ctx)
        if action.merge:
            ctx["items"].extend(items)
        else:
            ctx[action.into] = items
            if action.into == "items":
                ctx["items"] = items

    def _extract_items(self, action: ExtractAction, ctx: dict[str, Any]) -> list[dict]:
        """按 from 取记录列表：selector: 走 HTML 提取，embedded_json: 走内嵌 JSON，其余按 json path 逐层取。

        功能：根据 from 前缀选择数据源与提取方式，对每条记录用 _resolve_field 解析字段映射（published_at 做规范化），返回条目列表。
        谁会调用：_extract 在提取条目时调用。
        直接调用：
        - self._extract_from_html(...)：selector 路径。
        - self._extract_from_embedded_json(...)：embedded_json 路径。
        - self._json_path_value(...)：json path 取节点。
        - self._resolve_field(...)：解析字段值。
        - self._normalize_published_at(...)：规范化发布时间。
        输入与结果：输入 action 与 ctx；返回条目字典列表。
        副作用：无。
        """
        if action.from_.startswith("selector:"):
            return self._extract_from_html(action, ctx)  # Task 6 实装 Playwright
        if action.from_.startswith("embedded_json:"):
            return self._extract_from_embedded_json(action, ctx)
        # json path 逐层取
        node: Any = ctx.get("last_fetch")
        for p in action.from_.split("."):
            node = node.get(p) if isinstance(node, dict) else None
        records = node if isinstance(node, list) else []
        result: list[dict] = []
        for rec in records:
            item: dict[str, Any] = {}
            for field, spec in action.fields.items():
                value = self._resolve_field(spec, rec, ctx)
                item[field] = self._normalize_published_at(value) if field == "published_at" else value
            result.append(item)
        return result

    def _resolve_field(self, spec: Any, rec: dict, ctx: dict[str, Any]) -> str:
        """解析单条记录的字段取值：支持候选数组、template:、attr: 与裸字段名四种写法。

        功能：根据 spec 形态取值——列表则逐个候选取第一个非空；template: 先填 {item.xxx} 再填 {{var}} 上下文变量；attr: 暂留空（HTML 属性在别处处理）；裸字段名按 json path 取。
        谁会调用：_extract_items、_extract_from_embedded_json 在逐字段解析时调用。
        直接调用：
        - self._json_path_value(...)：按 json path 取节点值。
        - render_vars(...)：渲染模板里的 {{var}} 上下文变量。
        输入与结果：输入 spec、当前记录 rec 与 ctx；返回字符串值（取不到返回空串）。
        副作用：无。
        """
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
                """re.sub 回调：把 {item.xxx} 占位符替换为当前记录 rec 的对应字段值。

                功能：按捕获的字段名从 rec 取 json path 值，None 时返回空串。
                谁会调用：_resolve_field 在渲染 template: 模板时调用。
                直接调用：
                - self._json_path_value(...)：按路径取值。
                输入与结果：输入正则匹配对象；返回替换字符串。
                副作用：无。
                """
                value = self._json_path_value(rec, m.group(1))
                return str(value if value is not None else "")

            tmpl = re.sub(r"\{item\.([^}]+)\}", repl_item, tmpl)
            return render_vars(tmpl, ctx)
        if isinstance(spec, str) and spec.startswith("attr:"):
            return ""  # HTML 属性提取在 Task 6
        # 裸字段名，直接取
        if isinstance(spec, str):
            value = self._json_path_value(rec, spec)
            return str(value if value is not None else "")
        return ""

    def _extract_from_embedded_json(self, action: ExtractAction, ctx: dict[str, Any]) -> list[dict]:
        """从 HTML 内嵌的 script JSON / window 状态对象中抽取条目列表。

        功能：取 ctx["last_fetch"] 的 HTML，按 embedded_json: 前缀拆出源名与路径，定位内嵌 JSON 载荷后逐层取值，再按字段映射解析（published_at 规范化）成条目。
        谁会调用：_extract_items 在 from 以 embedded_json: 开头时调用。
        直接调用：
        - self._embedded_json_payload(...)：从 HTML 中定位指定源名的内嵌 JSON。
        - self._json_path_value(...)：按路径取节点。
        - self._resolve_field(...)：解析字段值。
        - self._normalize_published_at(...)：规范化发布时间。
        输入与结果：输入 action 与 ctx；返回条目字典列表（无 HTML 或取不到返回空列表）。
        副作用：无。
        """
        last_fetch = ctx.get("last_fetch")
        html = str(last_fetch.get("html") or "") if isinstance(last_fetch, dict) else ""
        if not html:
            return []
        source = action.from_.removeprefix("embedded_json:")
        source_name, _, path = source.partition(":")
        payload = self._embedded_json_payload(html, source_name)
        if payload is None:
            return []
        node = self._json_path_value(payload, path) if path else payload
        records = node if isinstance(node, list) else []
        result: list[dict] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            item: dict[str, Any] = {}
            for field, spec in action.fields.items():
                value = self._resolve_field(spec, rec, ctx)
                item[field] = self._normalize_published_at(value) if field == "published_at" else value
            result.append(item)
        return result

    def _normalize_published_at(self, value: str) -> str:
        """把发布时间规范成 ISO 字符串；纯数字按秒/毫秒时间戳转换，其余原样返回。

        功能：剥离空白后若全为数字，则按时间戳处理（>10^10 视为毫秒，先除 1000），转 UTC 的 ISO 格式；非数字直接返回原文。
        谁会调用：_extract_items、_extract_from_embedded_json 在填充 published_at 字段时调用。
        直接调用：无（仅类型/数值判断与 datetime 转换）。
        输入与结果：输入原始值（字符串）；返回规范化后的字符串。
        副作用：无。
        """
        text = str(value or "").strip()
        if not text.isdigit():
            return text
        try:
            number = int(text)
            if number > 10_000_000_000:
                number = number // 1000
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
        except Exception:
            return text

    def _embedded_json_payload(self, html: str, source_name: str) -> Any:
        """在 HTML 中遍历所有内嵌 JSON 载荷，返回与指定源名匹配的那一个。

        功能：调用 tools 里的遍历器逐个取出 {源名: 载荷}，命中 source_name 即返回该载荷，遍历完未命中返回 None。
        谁会调用：_extract_from_embedded_json 在定位内嵌 JSON 时调用。
        直接调用：
        - _iter_embedded_json_payloads(...)：来自 app.discovery.tools，遍历 HTML 内嵌 JSON。
        输入与结果：输入 HTML 文本与源名；返回匹配的 JSON 对象或 None。
        副作用：无。
        """
        from app.discovery.tools import _iter_embedded_json_payloads

        for name, payload in _iter_embedded_json_payloads(html):
            if name == source_name:
                return payload
        return None

    def _json_path_value(self, node: object, path: str | None) -> object:
        """按点分隔的路径逐层深入字典取值（用于 json path 字段映射）。

        功能：把 path 按 "." 拆开，逐层 get；遇到空段跳过，遇到缺失键直接返回 None，找不到则返回末级节点。
        谁会调用：_resolve_field、_extract_items、_extract_from_embedded_json 在取嵌套字段时调用。
        直接调用：无（仅字典遍历）。
        输入与结果：输入起点节点与路径（可空）；返回末级值或 None。
        副作用：无。
        """
        current = node
        for part in (path or "").split("."):
            if part == "":
                continue
            current = current.get(part) if isinstance(current, dict) else None
            if current is None:
                return None
        return current

    def _extract_from_html(self, action: ExtractAction, ctx: dict[str, Any]) -> list[dict]:
        """按 selector: 前缀从 Playwright 真实页面中用 CSS 选择器提取条目。

        功能：mock 直接走 browser_fn；否则首次懒加载页面（取 last_fetch 的 _url/_params 或 entry_url 打开），用 selector 选出元素，再逐字段用 _resolve_html_field 解析成条目。
        谁会调用：_extract_items 在 from 以 selector: 开头时调用。
        直接调用：
        - self._load_html_page_for_extract(...)：懒加载浏览器并打开页面。
        - self._resolve_html_field(...)：解析单个元素字段。
        输入与结果：输入 action 与 ctx；返回条目字典列表（无页面/无元素返回空列表）。
        副作用：真实浏览器导航（网络）。
        """
        if self._browser_fn:
            return self._browser_fn(action, ctx, page=self._page) or []
        if self._page is None:
            last_fetch = ctx.get("last_fetch") if isinstance(ctx.get("last_fetch"), dict) else {}
            self._load_html_page_for_extract(
                str(last_fetch.get("_url") or ctx.get("vars", {}).get("entry_url") or ""),
                params=last_fetch.get("_params") or {},
            )
        elements = self._page.query_selector_all(action.from_.removeprefix("selector:"))
        result: list[dict] = []
        for el in elements:
            item: dict[str, Any] = {}
            for field, spec in action.fields.items():
                item[field] = self._resolve_html_field(spec, el)
            result.append(item)
        return result

    def _resolve_html_field(self, spec: Any, el: Any) -> str:
        """解析单个 HTML 元素的字段取值：支持 self / attr: / selector 三种写法。

        功能：spec 为 "self" 取元素自身文本；attr: 取子元素（或自身）指定属性（href/src 用 evaluate 取）；其余按 CSS 选择器取子元素文本。
        谁会调用：_extract_from_html 在逐字段解析元素时调用。
        直接调用：Playwright 元素 API（query_selector/inner_text/get_attribute/evaluate）：取子元素与属性文本。
        输入与结果：输入字段 spec 与元素对象；返回字符串值。
        副作用：无。
        """
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
        """往 ctx["vars"] 写入一个变量：expr 走纯算术表达式，value 走字面量。

        功能：若 action.expr 存在，替换 {{var}} 后仅用内置白名单求值（禁 __builtins__，防注入），结果写入 vars；否则直接把 value 写入 vars。
        谁会调用：_exec 在分发到 SetAction 时调用；也由 _loop 的 on_each 后置动作触发。
        直接调用：eval(...)：仅限纯算术求值（限制 builtins 为空）。
        输入与结果：输入 action 与 ctx；无返回值（写 ctx["vars"]）。
        副作用：修改 context 变量（影响后续 fetch/extract 的 {{var}} 渲染）。
        """
        if action.expr:
            # 简单表达式：{{var}} + N，先替换变量再求值
            def repl(m: "re.Match[str]") -> str:
                """re.sub 回调：把 set 表达式里的 {{var}} 替换为 context 变量值。

                功能：按捕获的变量名从 ctx["vars"] 取值（缺省为 0），转成字符串供算术求值。
                谁会调用：_set 在求值 set 的 expr 表达式时调用。
                直接调用：无（仅查 ctx["vars"]）。
                输入与结果：输入正则匹配对象；返回替换字符串。
                副作用：无。
                """
                v = ctx["vars"].get(m.group(1), 0)
                return str(v)

            rendered = re.sub(r"\{\{(\w+)\}\}", repl, action.expr)
            ctx["vars"][action.var] = eval(rendered, {"__builtins__": {}}, {})  # 仅纯算术，禁内置
        else:
            ctx["vars"][action.var] = action.value

    def _dedup(self, action: DedupByAction, ctx: dict[str, Any]) -> None:
        """按指定字段对 ctx["items"] 去重，保留首次出现的条目。

        功能：用一个 seen 集合记录已出现过的字段值（默认 url），遍历 items 跳过重复项，保留顺序输出覆盖回 ctx["items"]。
        谁会调用：_exec 在分发到 DedupByAction 时调用。
        直接调用：无（仅集合与列表操作）。
        输入与结果：输入 action 与 ctx；无返回值（覆盖 ctx["items"]）。
        副作用：修改 ctx["items"] 内容（删除重复条目）。
        """
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
        """调用 article_tools 批量补全文章正文，并把结果写回 ctx。

        功能：把当前 items 交给 enrich_article_pages（可抓取正文/仅补缺失/限制条数与字符），将返回结果同时写入 ctx["last_fetch"] 与 ctx["items"]。
        谁会调用：_exec 在分发到 EnrichArticlePagesAction 时调用。
        直接调用：
        - enrich_article_pages(...)：来自 app.discovery.article_tools，补全文案。
        输入与结果：输入 action 与 ctx；无返回值（写 ctx["last_fetch"]、ctx["items"]）。
        副作用：发起正文抓取请求（网络），可能触发进度回调。
        """
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
        """按 API 模板逐条补全文章字段（如正文/标题），把结果写回 ctx。

        功能：遍历 items 复刻副本，按 url_template/headers/query/json_body 渲染后发请求，把返回 JSON 中指定 path 的字段写回条目（content 会去 HTML 标签并截断），最后统计尝试/补全数写回 ctx。
        谁会调用：_exec 在分发到 EnrichArticleApiAction 时调用。
        直接调用：
        - self._render_item_template(...)：渲染字符串模板。
        - self._render_item_json_like(...)：渲染 JSON 体模板。
        - self._json_path_value(...)：从响应 JSON 取字段。
        - httpx.request(...)：发起补全请求（第三方的网络调用）。
        输入与结果：输入 action 与 ctx；无返回值（写 ctx["last_fetch"]、ctx["items"]）。
        副作用：对每条文章发起 HTTP 请求（网络），修改 items 字段。
        """
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
        """把模板中的 {item.xxx} 与 {{var}} 替换成实际值，用于单条文章取值。

        功能：先用当前条目字段填 {item.xxx}，再用 ctx 变量渲染 {{var}}，得到最终字符串。
        谁会调用：_enrich_article_api 在渲染 url/headers/query 模板时调用。
        直接调用：
        - render_vars(...)：渲染 {{var}} 上下文变量。
        输入与结果：输入模板串、当前 item 与 ctx；返回渲染后字符串。
        副作用：无。
        """
        def repl_item(m: "re.Match[str]") -> str:
            """re.sub 回调：把 {item.xxx} 占位符替换为当前 item 的对应字段值。

            功能：按捕获的字段名从 item 取字段，缺省返回空串。
            谁会调用：_render_item_template 在渲染模板时调用。
            直接调用：无（仅查 item.get）。
            输入与结果：输入正则匹配对象；返回替换字符串。
            副作用：无。
            """
            return str(item.get(m.group(1), ""))

        rendered = re.sub(r"\{item\.(\w+)\}", repl_item, text)
        return render_vars(rendered, ctx)

    def _render_item_json_like(self, value: Any, item: dict[str, Any], ctx: dict[str, Any]) -> Any:
        """递归渲染 JSON 结构（dict/list/str）里的模板变量，保持原结构类型。

        功能：对字符串走 _render_item_template，对 list/dict 递归渲染，其余类型原样返回，避免破坏 JSON 结构。
        谁会调用：_enrich_article_api 在渲染 json_body 时调用。
        直接调用：
        - self._render_item_template(...)：渲染字符串模板。
        输入与结果：输入任意值、当前 item 与 ctx；返回同结构渲染值。
        副作用：无。
        """
        if isinstance(value, str):
            return self._render_item_template(value, item, ctx)
        if isinstance(value, list):
            return [self._render_item_json_like(v, item, ctx) for v in value]
        if isinstance(value, dict):
            return {k: self._render_item_json_like(v, item, ctx) for k, v in value.items()}
        return value

    def _json_path_value(self, payload: Any, path: str) -> Any:
        """按点分隔路径从响应 JSON 取字段值（支持列表下标访问）。

        功能：把 path 按 "." 拆开逐层深入；字典用 get，列表且下标为数字时按索引取，缺失则返回 None。
        谁会调用：_enrich_article_api 在解析响应 JSON 字段时调用（同名另一版本用于 json path 提取）。
        直接调用：无（仅字典/列表遍历）。
        输入与结果：输入 JSON 节点与路径；返回末级值或 None。
        副作用：无。
        """
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
        """循环执行循环体直到终止条件满足或达到最大轮数（防死循环）。

        功能：每轮先判断 until 条件（满足即 break），否则依次执行 body 循环体与 on_each 后置动作（如分页 +1），并在每步后检查条目上限。
        谁会调用：_exec 在分发到 LoopAction 时调用。
        直接调用：
        - eval_condition(...)：判断终止条件（来自 app.discovery.dsl）。
        - self._exec(...)：执行循环体/后置动作。
        - self._check_max_items(...)：检查条目上限。
        输入与结果：输入 action、ctx 与可选 max_items；无返回值（写 ctx）。
        副作用：间接触发循环体内各 action 的副作用（抓取/浏览器等）。
        """
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
        """检查并截断 ctx["items"] 是否达到条目上限，达到则抛内部中止信号。

        功能：max_items 为空则直接返回；否则 items 达到上限时截断并抛出 _DslStopExecution，由 run 捕获后停止执行。
        谁会调用：run 主循环、_loop 每步后调用。
        直接调用：_DslStopExecution(...)：达到上限时抛出（由 run 的 except 捕获）。
        输入与结果：输入 ctx 与 max_items；无返回值（可能抛异常）。
        副作用：可能抛出 _DslStopExecution 终止执行；截断 ctx["items"]。
        """
        if max_items is None:
            return
        items = ctx.get("items", [])
        if len(items) >= max_items:
            ctx["items"] = items[:max_items]
            raise _DslStopExecution()

    def _cleanup(self) -> None:
        """执行收尾：关闭 Playwright 浏览器与驱动，run 的 finally 中调用。

        功能：若真实浏览器已懒加载（_page 非空），则关闭浏览器并停止 Playwright；mock 路径下 _page 为 None，直接跳过。
        谁会调用：run 在 finally 中调用（无论成功或异常都收尾）。
        直接调用：self._browser.close()/self._pw.stop()：关闭浏览器（Playwright API）。
        输入与结果：无输入；无返回值。
        副作用：关闭真实浏览器进程（仅真实路径）。
        """
        if self._page is not None:
            try:
                self._browser.close()
                self._pw.stop()
            except Exception:
                pass
