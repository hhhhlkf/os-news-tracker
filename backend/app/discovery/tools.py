"""DEPRECATED / MIGRATION-ONLY: old LangChain Explorer/Validator tools.

The active Single Agent Loop uses ``loop.explore_session`` inside gVisor.  This
module is retained only because the legacy DSL interpreter still references
some helpers during migration/rollback.  Do not expose these tools to new
Discovery code.

全部为只读探查动作（fetch/inspect/test），不写文件、不执行命令。Playwright 仅在
render_js=True 或 capture_network 时按需启动，模块加载零浏览器开销。
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from html.parser import HTMLParser

from langchain_core.tools import tool

from app.discovery.browser_actions import exercise_dynamic_page
from app.discovery.cancel import ensure_not_cancelled


@tool
def fetch_page(
    url: str,
    render_js: bool = False,
    transport: str = "httpx",
    impersonate: str | None = None,
    stealthy_headers: bool = True,
) -> dict:
    """抓取页面并精简摘要，供 Explorer 探查链接/feed/内嵌 JSON。

    功能：抓取指定 URL，返回状态码、标题、链接、内容类型等摘要；transport=scrapling 走浏览器指纹伪装请求，render_js=True 走 Playwright 渲染动态页，否则用 httpx 直取。
    谁会调用：Explorer/Validator worker 通过 LangChain 工具接口（LLM 自主调用）探查页面。
    直接调用：
    - _page_summary(...)：把响应整理成统一摘要结构。
    - exercise_dynamic_page(...)：render_js 时触发动态内容渲染。
    - ensure_not_cancelled(...)：抓取前/渲染前检查运行是否被取消。
    输入与结果：输入 url、是否渲染 JS、transport、浏览器指纹与伪装头开关；返回含 status/title/links/html 片段等的 dict。
    副作用：发起 HTTP 请求或启动 Playwright 浏览器（网络/浏览器副作用）。
    """
    import httpx
    ensure_not_cancelled()
    if transport == "scrapling":
        from scrapling.fetchers import Fetcher

        page = Fetcher.get(
            url,
            stealthy_headers=stealthy_headers,
            impersonate=impersonate or "chrome",
        )
        body = getattr(page, "body", b"")
        if isinstance(body, bytes):
            text = body.decode(getattr(page, "encoding", None) or "utf-8", errors="replace")
        else:
            text = str(getattr(page, "html_content", None) or body or "")
        return _page_summary(
            url=str(getattr(page, "url", None) or url),
            status=int(getattr(page, "status", 0) or 0),
            text=text,
            content_type=(getattr(page, "headers", {}) or {}).get("content-type", ""),
            transport="scrapling",
            impersonate=impersonate or "chrome",
            stealthy_headers=stealthy_headers,
        )
    if render_js:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            p = b.new_page()
            ensure_not_cancelled()
            p.goto(url, wait_until="networkidle")
            exercise_dynamic_page(p, scroll_rounds=2, wait_ms=500)
            html = p.content()
            title = p.title()
            links = p.eval_on_selector_all("a[href]", "els=>els.map(e=>e.href)")
            b.close()
            out = _page_summary(
                url=url,
                status=200,
                text=html,
                content_type="text/html",
                transport="playwright",
                impersonate=None,
                stealthy_headers=False,
            )
            out["title"] = title
            out["links"] = links[:100]
            return out
    r = httpx.get(url, timeout=15, follow_redirects=True)
    return _page_summary(
        url=str(r.url),
        status=r.status_code,
        text=r.text,
        content_type=(getattr(r, "headers", {}) or {}).get("content-type", ""),
        transport="httpx",
        impersonate=None,
        stealthy_headers=False,
    )


def _page_summary(
    *,
    url: str,
    status: int,
    text: str,
    content_type: str,
    transport: str,
    impersonate: str | None,
    stealthy_headers: bool,
) -> dict:
    """把抓回的页面整理成统一摘要（标题/链接/feed/内嵌 JSON 片段）。

    功能：用内置 HTMLParser 提取 <title> 与 <a href>，并探测是否有 RSS/Atom alternate 链接、内嵌 JSON 片段与 feed 摘要，统一塞进返回的 dict。
    谁会调用：fetch_page 在拿到响应后调用。
    直接调用：
    - _find_alternate_feed(...)：探测 RSS/Atom 链接。
    - _extract_embedded_json_html(...)：抽取内嵌 JSON 片段。
    - _summarize_feed(...)：解析 feed 内容摘要。
    - _TitleLinkParser（内部 HTMLParser）：提取标题与链接。
    输入与结果：输入 url/status/text/content_type/transport 等；返回统一摘要 dict。
    副作用：无（纯解析）。
    """
    # 轻量 HTML 解析：提取 <title> 和 <a href>，避免引入 BeautifulSoup
    class _TitleLinkParser(HTMLParser):
        def __init__(self):
            """初始化轻量 HTML 解析器状态：清空标题与链接列表、标记不在 title 内。

            功能：调用父类构造，并初始化 title/links 容器与 _in_title 标志。
            谁会调用：_page_summary（或外部）在构造解析器时调用。
            直接调用：
            - super().__init__(...)：HTMLParser 初始化。
            输入与结果：无参数；无返回值（设置实例属性）。
            副作用：无。
            """
            super().__init__()
            self.title = ""
            self._in_title = False
            self.links: list[str] = []

        def handle_starttag(self, tag, attrs):
            """遇到 <title> 进入标题捕获；遇到 <a> 把 href 拼成绝对 URL 加入链接列表。

            功能：HTMLParser 在碰到开始标签时回调；title 标签置捕获标志，a 标签取 href 拼绝对地址。
            谁会调用：HTMLParser.feed 解析 HTML 时自动回调。
            直接调用：
            - urljoin(...)：拼绝对 URL。
            输入与结果：输入标签名与属性列表；无返回值（写入实例属性）。
            副作用：修改解析器的 _in_title 与 links。
            """
            if tag == "title":
                self._in_title = True
            if tag == "a":
                href = dict(attrs).get("href")
                if href:
                    self.links.append(urljoin(url, href))

        def handle_data(self, data):
            """在 <title> 捕获状态下把文本片段累加到 title。

            功能：HTMLParser 在解析文本节点时回调；仅当处于 title 内才追加。
            谁会调用：HTMLParser.feed 解析文本时自动回调。
            直接调用：无（仅字符串拼接）。
            输入与结果：输入文本片段；无返回值（追加到 title）。
            副作用：修改解析器的 title。
            """
            if self._in_title:
                self.title += data

        def handle_endtag(self, tag):
            """遇到 </title> 退出标题捕获状态。

            功能：HTMLParser 在碰到结束标签时回调；title 结束则把 _in_title 置回 False。
            谁会调用：HTMLParser.feed 解析结束标签时自动回调。
            直接调用：无。
            输入与结果：输入标签名；无返回值（修改标志）。
            副作用：修改解析器的 _in_title。
            """
            if tag == "title":
                self._in_title = False

    parser = _TitleLinkParser()
    parser.feed(text)
    out = {
        "url": url,
        "status": status,
        "content_type": content_type,
        "transport": transport,
        "impersonate": impersonate,
        "stealthy_headers": stealthy_headers,
        "title": parser.title.strip(),
        "links": parser.links[:100],
        "html": text[:2000],
    }
    alternate_feed = _find_alternate_feed(text)
    if alternate_feed:
        kind, href = alternate_feed
        out["alternate_feed"] = {"kind": kind, "href": urljoin(url, href)}
    embedded_json_html = _extract_embedded_json_html(text)
    if embedded_json_html:
        out["embedded_json_html"] = embedded_json_html
    feed = _summarize_feed(text)
    if feed:
        out["feed"] = feed
    return out


def _find_alternate_feed(html: str) -> tuple[str, str] | None:
    """从页面 HTML 中找出 RSS/Atom 订阅链接（alternate）。

    功能：用正则扫描 <link> 标签并解析属性，命中 rel=alternate 且 type 为 rss/atom 时返回 (kind, href)，否则返回 None。
    谁会调用：_page_summary 在探测页面订阅源时调用。
    直接调用：re.finditer / re.findall(...)：扫描并解析 link 标签属性。
    输入与结果：输入 HTML 文本；返回 (rss/atom, href) 或 None。
    副作用：无。
    """
    for match in re.finditer(r"<link\b[^>]*>", html or "", re.IGNORECASE):
        attrs = {
            key.lower(): value
            for key, value in re.findall(
                r'([^\s=]+)\s*=\s*["\']([^"\']*)["\']',
                match.group(0),
                flags=re.IGNORECASE,
            )
        }
        rel = attrs.get("rel", "").lower()
        typ = attrs.get("type", "").lower()
        href = (attrs.get("href") or "").strip()
        if "alternate" not in rel or not href:
            continue
        if typ == "application/rss+xml":
            return "rss", href
        if typ == "application/atom+xml":
            return "atom", href
    return None


def _extract_embedded_json_html(text: str, *, limit: int = 300000) -> str:
    """截取页面里可能含文章列表的 <script> 内嵌 JSON 片段。

    功能：扫描所有 script 块，按关键词（_ROUTER_DATA/__NEXT_DATA__/article_list 等）筛出可疑内嵌 JSON，拼接成文本（带长度上限）。
    谁会调用：_page_summary 在抽取内嵌 JSON 时调用。
    直接调用：re.compile / re.finditer(...)：匹配可疑 script 片段。
    输入与结果：输入 HTML 文本与上限；返回拼接后的内嵌 JSON 文本（可能为空）。
    副作用：无。
    """
    snippets: list[str] = []
    interesting = re.compile(
        r"_ROUTER_DATA|_SSR_DATA|__INITIAL_STATE__|__NEXT_DATA__|__NUXT__|"
        r"__MODERN_SERVER_DATA__|__next_f|article_list|articles|posts|loaderData",
        re.IGNORECASE,
    )
    total = 0
    for match in re.finditer(r"<script[^>]*>.*?</script>", text, re.IGNORECASE | re.DOTALL):
        snippet = match.group(0)
        if not interesting.search(snippet):
            continue
        snippets.append(snippet)
        total += len(snippet)
        if total >= limit:
            break
    return "\n".join(snippets)[:limit]


def _summarize_feed(text: str) -> dict | None:
    """用 feedparser 解析 feed 文本，抽取标题与样本条目摘要。

    功能：解析 RSS/Atom 文本，若含条目则汇总 feed 标题、条目数与前 5 条样本的标题/链接/时间，否则返回 None。
    谁会调用：_page_summary 在识别 feed 页面时调用。
    直接调用：feedparser.parse(...)：解析 feed。
    输入与结果：输入 feed 文本；返回含 title/item_count/sample_items 的 dict 或 None。
    副作用：无。
    """
    import feedparser

    parsed = feedparser.parse(text)
    if parsed.bozo and not parsed.entries:
        return None
    if not parsed.entries:
        return None
    samples = []
    for entry in parsed.entries[:5]:
        samples.append({
            "title": str(entry.get("title") or "").strip(),
            "url": str(entry.get("link") or "").strip(),
            "published_at": str(entry.get("published") or entry.get("updated") or "").strip(),
        })
    return {
        "title": str(parsed.feed.get("title") or "").strip(),
        "item_count": len(parsed.entries),
        "sample_items": samples,
    }


@tool
def capture_network(url: str) -> list:
    """用 Playwright 抓取页面加载与滚动中触发的 JSON XHR/Fetch 响应。

    功能：启动浏览器打开页面并触发动态加载，监听所有响应，把 JSON 类型的 XHR/Fetch 响应（含请求体）收集返回，供 LLM 定位文章数据接口。
    谁会调用：Explorer/Validator worker 通过 LangChain 工具接口（LLM 自主调用）探查数据接口。
    直接调用：
    - exercise_dynamic_page(...)：滚动/加载更多触发接口。
    - ensure_not_cancelled(...)：操作前检查取消。
    输入与结果：输入 url；返回响应 dict 列表（api_url/method/status/parsed_json 等）。
    副作用：启动 Playwright 浏览器并联网（浏览器副作用）。
    """
    from playwright.sync_api import sync_playwright
    ensure_not_cancelled()
    caps: list = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_page()

        def on_resp(resp):
            """Playwright response 事件回调：捕获 JSON XHR/Fetch 响应（截断防 token 爆炸）。

            功能：取响应体，长度在阈值内则连同请求体解析成结构化捕获，追加到 caps 列表。
            谁会调用：page.on("response", on_resp) 在浏览器抓取网络时回调。
            直接调用：
            - resp.text()/resp.request：取响应体与请求信息。
            - json.loads(...)：解析 JSON 响应体。
            输入与结果：输入 resp 响应对象；无返回值（结果追加到 caps）。
            副作用：修改 caps 列表。
            """
            # 捕获 JSON XHR/Fetch 响应，body 截断防 token 爆炸
            try:
                body = resp.text()
                if body and len(body) < 500000:
                    post_data = getattr(resp.request, "post_data", None)
                    request_json_body = None
                    if post_data:
                        try:
                            request_json_body = json.loads(post_data)
                        except Exception:
                            request_json_body = None
                    caps.append({
                        "api_url": resp.url,
                        "method": resp.request.method,
                        "status": resp.status,
                        "request_json_body": request_json_body,
                        "parsed_json": json.loads(body),
                    })
            except Exception:
                pass

        p.on("response", on_resp)
        try:
            ensure_not_cancelled()
            p.goto(url, wait_until="networkidle", timeout=15000)
        except Exception:
            pass
        exercise_dynamic_page(p, scroll_rounds=8, wait_ms=700)
        b.close()
    return caps


@tool
def probe_embedded_json(url: str) -> dict:
    """探测页面内嵌 script/window JSON，找出像文章列表的候选 path 与字段样本。

    功能：抓取页面 HTML，枚举所有内嵌 JSON 载荷，筛选出类似文章列表的候选（含格式化定位符、评分、字段映射与样本），供 LLM 生成 embedded_json 提取式。
    谁会调用：Explorer/Validator worker 通过 LangChain 工具接口（LLM 自主调用）探查内嵌 JSON。
    直接调用：
    - _embedded_json_article_candidates(...)：从 HTML 提取候选列表。
    - ensure_not_cancelled(...)：抓取前检查取消。
    输入与结果：输入 url；返回含候选数量与候选详情的 dict。
    副作用：发起一次 HTTP GET（网络请求）。
    """
    import httpx

    ensure_not_cancelled()
    response = httpx.get(url, timeout=15, follow_redirects=True)
    html = response.text
    candidates = _embedded_json_article_candidates(html, site_url=str(response.url))
    return {
        "url": str(response.url),
        "status": response.status_code,
        "candidate_count": len(candidates),
        "candidates": candidates[:5],
    }


def _embedded_json_article_candidates(html: str, *, site_url: str) -> list[dict]:
    """扫描 HTML 内嵌 JSON 载荷，汇总所有像文章列表的候选。

    功能：遍历每个内嵌 JSON 载荷，按路径排名找出文章列表，推断字段映射并构造样本，按评分排序后返回候选列表。
    谁会调用：probe_embedded_json 在探测内嵌 JSON 时调用。
    直接调用：
    - _iter_embedded_json_payloads(...)：枚举内嵌 JSON 载荷。
    - _rank_json_item_lists(...)：给每个载荷内的列表打分排名。
    - _infer_embedded_item_fields(...)：推断字段映射。
    - _build_embedded_sample_items(...)：构造样本条目。
    输入与结果：输入 HTML 与站点 URL；返回候选 dict 列表（按评分降序）。
    副作用：无。
    """
    candidates: list[dict] = []
    for source_name, payload in _iter_embedded_json_payloads(html):
        for score, path, items in _rank_json_item_lists(payload):
            fields = _infer_embedded_item_fields(items[0], site_url=site_url)
            if not fields.get("title"):
                continue
            sample_items = _build_embedded_sample_items(items, site_url=site_url, fields=fields)
            if not sample_items:
                continue
            candidates.append({
                "source_name": source_name,
                "path": path,
                "format_locator": f"embedded_json:{source_name}:{path}",
                "score": score,
                "item_count": len(items),
                "fields": fields,
                "sample_items": sample_items[:3],
            })
    candidates.sort(key=lambda item: item.get("score", 0), reverse=True)
    return candidates


def _iter_embedded_json_payloads(html: str) -> list[tuple[str, object]]:
    """从 HTML 中枚举所有内嵌 JSON 载荷（script JSON / window 状态 / push 流）。

    功能：用正则找出 JSON 类型 script、指定 id 的窗口状态对象，以及 `ident.push([idx,"..."])` 形式的数据流，逐个解析为 (源名, 对象) 返回。
    谁会调用：_embedded_json_article_candidates（本文件）、interpreter._embedded_json_payload（解释器提取）。
    直接调用：
    - _iter_script_push_stream_payloads(...)：解析 push 流数据。
    - json.JSONDecoder().raw_decode(...)：解析赋值式 JSON。
    输入与结果：输入 HTML 文本；返回 (源名, JSON 对象) 列表。
    副作用：无。
    """
    payloads: list[tuple[str, object]] = []
    decoder = json.JSONDecoder()
    for match in re.finditer(
        r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        attrs = match.group("attrs") or ""
        body = (match.group("body") or "").strip()
        if not body:
            continue
        id_match = re.search(r'id=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        type_match = re.search(r'type=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        script_id = id_match.group(1) if id_match else None
        script_type = (type_match.group(1).lower() if type_match else "")
        if script_type in {"application/json", "application/ld+json"} or script_id in {
            "__NEXT_DATA__",
            "__MODERN_SERVER_DATA__",
        }:
            try:
                payloads.append((f"script#{script_id}" if script_id else "script#json", json.loads(body)))
            except Exception:
                pass
        for assignment in (
            "window._ROUTER_DATA",
            "window._SSR_DATA",
            "window.__INITIAL_STATE__",
            "window.__NUXT__",
            "__INITIAL_STATE__",
        ):
            match_assignment = re.search(rf"{re.escape(assignment)}\s*=\s*", body)
            if not match_assignment:
                continue
            text = body[match_assignment.end():].lstrip()
            try:
                payload, _ = decoder.raw_decode(text)
            except Exception:
                continue
            payloads.append((assignment, payload))
    payloads.extend(_iter_script_push_stream_payloads(html))
    return payloads


_SCRIPT_PUSH_STREAM_RE = re.compile(
    r"(?P<target>(?:self\.)?[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)"
    r"\.push\(\[\s*\d+\s*,\s*\"(?P<body>(?:\\.|[^\"\\])*)\"\s*\]\)",
)


def _iter_script_push_stream_payloads(html: str) -> list[tuple[str, dict]]:
    """从 `ident.push([idx, "…"])` 形式字符串流重建 `{列表键: 文章列表}`（如 RSC flight）。

    功能：正则匹配 push 流片段，解码其中 JSON，遍历值找出命名文章列表，按目标变量聚合为 (变量名, {键: 列表}) 返回。
    谁会调用：_iter_embedded_json_payloads 在枚举内嵌 JSON 时调用。
    直接调用：
    - _iter_json_values_in_text(...)：从片段文本提取 JSON 值。
    - _named_article_lists_in_tree(...)：在值里找命名文章列表。
    输入与结果：输入 HTML；返回 (变量名, 聚合字典) 列表。
    副作用：无。
    """
    by_target: dict[str, dict[str, list]] = {}
    best_score: dict[str, dict[str, int]] = {}
    for match in _SCRIPT_PUSH_STREAM_RE.finditer(html or ""):
        target = match.group("target")
        try:
            body = json.loads(f'"{match.group("body") or ""}"')
        except Exception:
            continue
        if not target or not isinstance(body, str) or not body:
            continue
        for value in _iter_json_values_in_text(body):
            for key, items, score in _named_article_lists_in_tree(value):
                bucket = by_target.setdefault(target, {})
                scores = best_score.setdefault(target, {})
                if score > scores.get(key, -1):
                    bucket[key] = items
                    scores[key] = score
    return [(target, payload) for target, payload in by_target.items() if payload]


def _iter_json_values_in_text(text: str) -> list[object]:
    """从文本中逐个解码出顶层 JSON 值（对象/数组）。

    功能：扫描文本里 `{`/`[` 起始位置，用 JSONDecoder.raw_decode 逐个解析，收集所有顶层 JSON 值。
    谁会调用：_iter_script_push_stream_payloads 在解析 push 流片段时调用。
    直接调用：json.JSONDecoder().raw_decode(...)：流式解析 JSON。
    输入与结果：输入文本；返回 JSON 值列表。
    副作用：无。
    """
    decoder = json.JSONDecoder()
    values: list[object] = []
    index = 0
    length = len(text or "")
    while index < length:
        while index < length and text[index] not in "{[":
            index += 1
        if index >= length:
            break
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        values.append(value)
        index = end
    return values


def _named_article_lists_in_tree(node: object) -> list[tuple[str, list, int]]:
    """递归遍历嵌套 dict/list，收集「字段名对应文章列表」及其评分。

    功能：深入 React/RSC 结构，发现值均为对象列表的字段时打分（_score_embedded_item_list），收集 (字段名, 列表, 分) 三元组。
    谁会调用：_iter_script_push_stream_payloads 在找文章列表时调用。
    直接调用：
    - _score_embedded_item_list(...)：给文章列表打分。
    输入与结果：输入任意节点；返回 (字段名, 列表, 分) 列表。
    副作用：无。
    """
    found: list[tuple[str, list, int]] = []

    def walk(current: object) -> None:
        """递归遍历 JSON 节点，把像文章列表的对象数组按分收集到 found，嵌套结构继续深入。

        功能：遇到全为 dict 的数组就用 _score_embedded_item_list 打分，>0 的连同键名收集；
        dict/list 则继续递归。
        谁会调用：_named_article_lists_in_tree 在收集候选时调用。
        直接调用：
        - _score_embedded_item_list(...)：给数组打分。
        输入与结果：输入当前节点；无返回值（结果写入外层 found）。
        副作用：无。
        """
        if isinstance(current, dict):
            for key, value in current.items():
                if (
                    isinstance(key, str)
                    and isinstance(value, list)
                    and value
                    and all(isinstance(item, dict) for item in value[:5])
                ):
                    score = _score_embedded_item_list(value)
                    if score > 0:
                        found.append((key, value, score))
                        continue
                walk(value)
            return
        if isinstance(current, list):
            for item in current[:40]:
                walk(item)

    walk(node)
    return found


def _rank_json_item_lists(payload: object) -> list[tuple[int, str, list[dict]]]:
    """给 JSON 载荷内所有文章列表按路径打分并降序排序。

    功能：递归遍历载荷，对每个对象列表打分，收集 (分, 路径, 列表) 后按分降序，便于优先选最像文章列表的节点。
    谁会调用：_embedded_json_article_candidates 在筛选候选时调用。
    直接调用：
    - _score_embedded_item_list(...)：给列表打分。
    输入与结果：输入 JSON 载荷；返回 (分, 路径, 列表) 列表（降序）。
    副作用：无。
    """
    ranked: list[tuple[int, str, list[dict]]] = []

    def walk(node: object, path: str) -> None:
        """递归遍历 JSON 载荷，给像文章列表的数组打分并收集 (分,路径,列表) 到 ranked。

        功能：遇到全为 dict 的数组就用 _score_embedded_item_list 打分，>0 的连同路径收集；
        dict 则沿每个 key 继续递归。
        谁会调用：_rank_json_item_lists 在收集候选时调用。
        直接调用：
        - _score_embedded_item_list(...)：给数组打分。
        输入与结果：输入节点与累计路径；无返回值（结果写入外层 ranked）。
        副作用：无。
        """
        if isinstance(node, list) and node and all(isinstance(item, dict) for item in node[:5]):
            score = _score_embedded_item_list(node)
            if score > 0:
                ranked.append((score, path or "obj", node))
            return
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)

    walk(payload, "")
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def _score_embedded_item_list(items: list[dict]) -> int:
    """给候选列表打分，判断它像不像「文章列表」。

    功能：依据是否含 title/name、url/link、date/published_at 等字段（含嵌套）加分，列表越长再加分，分数越高越可能是文章列表。
    谁会调用：_named_article_lists_in_tree、_rank_json_item_lists 在评估列表时调用。
    直接调用：
    - _nested_key_paths(...)：取嵌套字段路径用于匹配。
    输入与结果：输入对象列表；返回整型评分。
    副作用：无。
    """
    first = items[0] if items else {}
    keys = set(first.keys())
    nested_leaf_keys = {path.rsplit(".", 1)[-1] for path in _nested_key_paths(first)}
    score = 0
    if keys & {"title", "name", "subject"} or nested_leaf_keys & {"Title", "title", "Name", "name", "subject"}:
        score += 3
    if keys & {"url", "link", "href", "path"} or nested_leaf_keys & {
        "url", "link", "href", "path", "TitleKey", "slug"
    }:
        score += 3
    if (
        keys & {"date", "published_at", "pubDate", "publishTime", "created_at", "time"}
        or nested_leaf_keys & {"PublishDate", "published_at", "pubDate", "publishTime", "created_at", "time"}
    ):
        score += 2
    if len(items) >= 2:
        score += 1
    return score


def _infer_embedded_item_fields(sample: dict, *, site_url: str) -> dict:
    """从样本条目推断文章字段（id/title/url/published_at/summary/content）的 JSON 路径。

    功能：按中英文常见字段名在样本（含嵌套路径）里逐个匹配，优先选与站点语言区匹配的路径，输出字段到路径的映射。
    谁会调用：_embedded_json_article_candidates 在构造字段映射时调用。
    直接调用：
    - _nested_key_paths(...)：枚举嵌套字段路径用于匹配。
    - urlparse(...)：判断站点语言区。
    输入与结果：输入样本与站点 URL；返回字段名到 JSON 路径的 dict。
    副作用：无。
    """
    locale = "zh" if "/zh/" in urlparse(site_url).path else ("en" if "/en/" in urlparse(site_url).path else "")

    def pick(*names: str) -> str | None:
        """按候选字段名在样本里挑第一个命中的路径：顶层键优先，否则匹配嵌套路径并优先语言区。

        功能：先查样本顶层是否含某个候选名；没有则在嵌套路径里找末级名相等的路径，
        若站点有 zh/en 语言区则优先选含语言区 token 的路径。
        谁会调用：_infer_embedded_item_fields 在推断各字段路径时调用。
        直接调用：
        - _nested_key_paths(...)：枚举嵌套字段路径。
        输入与结果：输入候选字段名；返回命中的路径或 None。
        副作用：无。
        """
        exact = [name for name in names if name in sample]
        if exact:
            return exact[0]
        paths = _nested_key_paths(sample)
        for name in names:
            matches = [
                path for path in paths
                if path.rsplit(".", 1)[-1].lower() == name.lower()
            ]
            if not matches:
                continue
            if locale:
                locale_token = locale.lower()
                preferred = [
                    path for path in matches
                    if locale_token in path.lower()
                ]
                if preferred:
                    return preferred[0]
            return matches[0]
        return None

    return {
        "id": pick("TitleKey", "titleKey", "slug", "id", "no", "uuid", "ArticleID", "ID"),
        "title": pick("Title", "title", "name", "subject"),
        "url": pick("url", "link", "href", "path"),
        "published_at": pick("PublishDate", "published_at", "date", "pubDate", "publishTime", "created_at", "time"),
        "summary": pick("Abstract", "summary", "excerpt", "description", "desc", "brief"),
        "content": pick("content", "textContent", "body", "text"),
    }


def _nested_key_paths(item: dict) -> list[str]:
    """返回对象所有嵌套字段的以点分隔路径列表。

    功能：递归遍历 dict，把每一级 key 拼成 a.b.c 形式路径，供字段名匹配使用。
    谁会调用：_score_embedded_item_list、_infer_embedded_item_fields 在匹配字段时调用。
    直接调用：无（仅递归遍历 dict）。
    输入与结果：输入 dict；返回路径字符串列表。
    副作用：无。
    """
    paths: list[str] = []

    def walk(node: object, prefix: str) -> None:
        """递归遍历 dict，把所有嵌套字段拼成 a.b.c 形式路径追加到 paths。

        功能：非 dict 直接返回；dict 则沿每个 key 拼接前缀路径后追加，并对子 dict 继续递归深入。
        谁会调用：_nested_key_paths 在枚举嵌套字段路径时调用。
        直接调用：无（仅字典遍历）。
        输入与结果：输入当前节点与累计前缀；无返回值（结果写入外层 paths）。
        副作用：无。
        """
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.append(path)
            if isinstance(value, dict):
                walk(value, path)

    walk(item, "")
    return paths


def _embedded_json_path_value(node: object, path: str | None) -> object:
    """按点分隔路径从 JSON 节点取值（供抽取样本字段）。

    功能：把 path 按 "." 拆开逐层深入 dict，遇空段跳过、缺键返回 None。
    谁会调用：_build_embedded_sample_items 在取样本字段时调用。
    直接调用：无（仅字典遍历）。
    输入与结果：输入节点与路径；返回末级值或 None。
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


def _build_embedded_sample_items(items: list[dict], *, site_url: str, fields: dict) -> list[dict]:
    """按推断出的字段映射，从内嵌 JSON 列表构造前若干条样本条目。

    功能：用 fields 里的路径取每条的 id/url/title/summary/published_at，把相对 URL 拼成绝对 URL，输出可直接展示的样本。
    谁会调用：_embedded_json_article_candidates 在构造候选样本时调用。
    直接调用：
    - _embedded_json_path_value(...)：按路径取字段值。
    - urljoin(...)：把相对 URL 拼成绝对 URL。
    输入与结果：输入列表、站点 URL 与字段映射；返回样本 dict 列表。
    副作用：无。
    """
    sample_items: list[dict] = []
    for item in items[:5]:
        raw_url = _embedded_json_path_value(item, fields.get("url")) if fields.get("url") else None
        raw_id = _embedded_json_path_value(item, fields.get("id")) if fields.get("id") else None
        title = _embedded_json_path_value(item, fields.get("title")) if fields.get("title") else None
        summary = _embedded_json_path_value(item, fields.get("summary")) if fields.get("summary") else None
        published_at = (
            _embedded_json_path_value(item, fields.get("published_at"))
            if fields.get("published_at") else None
        )
        sample_items.append({
            "id": str(raw_id) if raw_id is not None else None,
            "raw_url": str(raw_url) if raw_url is not None else None,
            "url": urljoin(site_url, str(raw_url)) if raw_url else None,
            "title": str(title) if title is not None else None,
            "summary": str(summary) if summary is not None else None,
            "published_at": str(published_at) if published_at is not None else None,
        })
    return sample_items


@tool
def probe_html_entries(
    url: str,
    item_selector: str,
    link_selector: str | None = None,
    title_selector: str | None = None,
    date_selector: str | None = None,
) -> dict:
    """按给定 CSS selector 抽样探测页面文章条目，返回 title/url/date 样本。

    功能：用 Playwright 打开页面并渲染动态内容，对每个条目元素按 selector 取标题/链接/日期，返回样本与「是否像文章列表」判断。
    谁会调用：Explorer/Validator worker 通过 LangChain 工具接口（LLM 自主调用）验证 selector 提取。
    直接调用：
    - exercise_dynamic_page(...)：渲染动态列表。
    - ensure_not_cancelled(...)：操作前检查取消。
    - 嵌套 _pick(...)：按 selector 选取子元素。
    输入与结果：输入 url 与各类 selector；返回含样本与 looks_like_article_list 的 dict。
    副作用：启动 Playwright 浏览器并联网（浏览器副作用）。
    """
    from playwright.sync_api import sync_playwright

    ensure_not_cancelled()
    samples: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        exercise_dynamic_page(page, scroll_rounds=2, wait_ms=500)
        elements = page.query_selector_all(item_selector)

        def _pick(el, selector: str | None):
            """按 selector 从条目元素里选取子元素；空/self 表示取自身。

            功能：selector 为空或 "self" 时直接返回元素本身，否则用 query_selector 取第一个匹配子元素。
            谁会调用：probe_html_entries 在取链接/标题/日期子元素时调用。
            直接调用：el.query_selector(...)：Playwright 元素选择器。
            输入与结果：输入元素与 selector；返回子元素或自身。
            副作用：无。
            """
            if selector in (None, "", "self"):
                return el
            return el.query_selector(selector)

        for el in elements[:5]:
            ensure_not_cancelled()
            link_el = _pick(el, link_selector)
            title_el = _pick(el, title_selector)
            date_el = _pick(el, date_selector)
            href = None
            if link_el is not None:
                href = link_el.evaluate("(node) => node.href || node.getAttribute('href') || ''")
            title = ""
            if title_el is not None:
                title = (title_el.inner_text() or "").strip()
            published_at = ""
            if date_el is not None:
                published_at = (date_el.inner_text() or "").strip()
            samples.append({
                "title": title,
                "url": href or "",
                "published_at": published_at,
            })
        browser.close()

    valid = [sample for sample in samples if sample.get("title") and sample.get("url")]
    return {
        "count": len(samples),
        "valid_count": len(valid),
        "looks_like_article_list": len(valid) >= 2,
        "samples": samples,
    }


def _decode_json_lenient(text: str):
    """容错解析 JSON：标准失败则只取第一个对象（兼容拼接响应）。

    功能：先 json.loads；若响应里拼接了多个 JSON 文本，用 raw_decode 取第一个对象。
    谁会调用：inspect_item 在解析非标准响应时调用。
    直接调用：json.loads(...)/json.JSONDecoder().raw_decode(...)：解析 JSON。
    输入与结果：输入文本；返回解析出的对象。
    副作用：无。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text)
        return obj


@tool
def inspect_item(api_url: str, method: str = "GET", json_body: dict | None = None) -> dict:
    """请求某个 API 并查看其返回的 item 结构。

    功能：用指定方法（默认 GET）请求 API_URL，尝试把响应解析为 JSON，失败则容错取首个 JSON 对象，返回状态码与样本。
    谁会调用：Explorer/Validator worker 通过 LangChain 工具接口（LLM 自主调用）探查 API 结构。
    直接调用：
    - _decode_json_lenient(...)：容错解析非标准响应。
    - ensure_not_cancelled(...)：请求前检查取消。
    输入与结果：输入 api_url、方法与可选 json 体；返回含 status/sample 的 dict。
    副作用：发起一次 HTTP 请求（网络请求）。
    """
    import httpx
    ensure_not_cancelled()
    r = httpx.request(method, api_url, json=json_body, timeout=15)
    try:
        sample = r.json()
    except Exception:
        sample = _decode_json_lenient(r.text)
    return {"status": r.status_code, "sample": sample}


@tool
def test_url_template(template: str, id_field: str, sample_items: list[dict]) -> dict:
    """用样本里的真实 ID 填 URL 模板，逐个请求验证是否为文章页。

    功能：把 {id} 替换为样本字段值，逐个发起请求，以 HTTP 200 且有 <title> 判定为文章页，返回每个验证结果。
    谁会调用：Explorer/Validator worker 通过 LangChain 工具接口（LLM 自主调用）验证 URL 模板。
    直接调用：
    - ensure_not_cancelled(...)：每轮请求前检查取消。
    输入与结果：输入模板、id 字段名与样本；返回各 URL 验证结果列表。
    副作用：对每个样本发起一次 HTTP 请求（网络请求）。
    """
    import httpx
    results = []
    for it in sample_items[:5]:  # 最多验证 5 个样本
        ensure_not_cancelled()
        url = template.replace("{id}", str(it.get(id_field, "")))
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True)
            # 判定为文章页：HTTP 200 且有 <title>（不依赖正文长度，SPA 正文 JS 渲染）
            is_article = r.status_code == 200 and "<title>" in r.text
            results.append({"url": url, "status": r.status_code, "is_article_page": is_article})
        except Exception as e:
            results.append({"url": url, "status": 0, "is_article_page": False, "error": str(e)})
    return {"results": results}


@tool
def test_path_join(base_url: str, path_field: str, sample_items: list[dict]) -> dict:
    """用 base_url 拼接样本里的 path 字段值，逐个请求验证是否为文章页。

    功能：取样本（含 raw）里的 path 字段值拼到 base_url，逐个请求，以 200 + <title> 判定文章页，返回验证结果。
    谁会调用：Explorer/Validator worker 通过 LangChain 工具接口（LLM 自主调用）验证路径拼接。
    直接调用：
    - urljoin(...)：拼接 base 与相对路径。
    - ensure_not_cancelled(...)：每轮请求前检查取消。
    输入与结果：输入 base_url、path 字段名与样本；返回各 URL 验证结果列表。
    副作用：对每个样本发起一次 HTTP 请求（网络请求）。
    """
    import httpx

    results = []
    for it in sample_items[:5]:
        ensure_not_cancelled()
        raw = it.get("raw") if isinstance(it.get("raw"), dict) else {}
        sample_value = raw.get(path_field)
        if sample_value is None:
            sample_value = it.get(path_field)
        url = urljoin(base_url, str(sample_value or ""))
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True)
            is_article = r.status_code == 200 and "<title>" in r.text
            results.append({
                "sample_value": sample_value,
                "url": url,
                "status": r.status_code,
                "is_article_page": is_article,
            })
        except Exception as e:
            results.append({
                "sample_value": sample_value,
                "url": url,
                "status": 0,
                "is_article_page": False,
                "error": str(e),
            })
    return {"results": results}


_DEFAULT_URL_PATTERNS: list[str] = [
    "/blog/{id}",
    "/blog/detail/{id}",
    "/post/{id}",
    "/posts/{id}",
    "/article/{id}",
    "/articles/{id}",
    "/news/{id}",
    "/p/{id}",
    "/{id}",
]


@tool
def probe_url_patterns(base_url: str, id_value: str, patterns: list[str] | None = None) -> list:
    """LLM 没头绪时批量试常见 URL pattern，返回每个 pattern 的验证结果。

    功能：对一组候选 URL 模板（默认常见博客/文章路径）填入 id，逐个请求，以 200 + <title> 判定文章页，返回每个 pattern 的验证情况供 LLM 归纳规律。
    谁会调用：Explorer/Validator worker 通过 LangChain 工具接口（LLM 自主调用）试探 URL 规律。
    直接调用：
    - urljoin(...)：拼接 base 与相对路径。
    - ensure_not_cancelled(...)：每轮请求前检查取消。
    输入与结果：输入 base_url、id 值与可选 patterns；返回各 pattern 验证结果列表。
    副作用：对每个 pattern 发起一次 HTTP 请求（网络请求）。
    """
    import httpx
    candidates = patterns if patterns is not None else _DEFAULT_URL_PATTERNS
    results: list = []
    for pat in candidates:
        ensure_not_cancelled()
        path = pat.replace("{id}", str(id_value))
        if not path.startswith("/"):
            path = "/" + path
        generated = urljoin(base_url, path)
        try:
            r = httpx.get(generated, timeout=15, follow_redirects=True)
            is_article = r.status_code == 200 and "<title>" in r.text
            results.append({
                "pattern": pat,
                "generated_url": generated,
                "status": r.status_code,
                "is_article_page": is_article,
            })
        except Exception as e:
            results.append({
                "pattern": pat,
                "generated_url": generated,
                "status": 0,
                "is_article_page": False,
                "error": str(e),
            })
    return results


@tool
def probe_article_content(
    article_url: str,
    sample_item: dict,
    list_url: str | None = None,
    id_field: str | None = None,
    min_content_chars: int = 300,
) -> dict:
    """验证文章详情正文获取策略：先试 HTML 页，失败再探测详情 JSON API。

    功能：先用 fetch_article_page_content 抓 HTML 正文；不足阈值时，捕获页面 JSON 响应或猜测详情 API 逐个验证，返回验证结论与可选的内容获取方案。
    谁会调用：Explorer/Validator worker 通过 LangChain 工具接口（LLM 自主调用）确定正文策略。
    直接调用：
    - fetch_article_page_content(...)：抓 HTML 正文（来自 article_tools）。
    - _capture_article_json_responses(...)：捕获页面 JSON 接口。
    - _probe_detail_api_payload(...)：验证 JSON 载荷是否含正文。
    - _sample_id_value(...)/_detail_api_candidates(...)/_probe_detail_api_url(...)：构造并验证详情 API。
    - ensure_not_cancelled(...)：各步检查取消。
    输入与结果：输入文章 URL、样本、列表 URL、id 字段与最小字数；返回验证结论 dict。
    副作用：发起 HTTP 请求与可能的 Playwright 捕获（网络/浏览器副作用）。
    """
    import httpx
    from app.discovery.article_tools import fetch_article_page_content

    ensure_not_cancelled()
    html_result = fetch_article_page_content(article_url, timeout_seconds=8, content_char_limit=3000)
    html_content_chars = len(html_result.get("content") or "")
    if html_content_chars >= min_content_chars:
        return {
            "content_verified": True,
            "content_strategy": "html_page",
            "content_chars": html_content_chars,
            "html_status": html_result.get("status"),
            "article_url": html_result.get("resolved_url") or article_url,
        }

    for cap in _capture_article_json_responses(article_url):
        ensure_not_cancelled()
        payload = cap.get("parsed_json")
        api_result = _probe_detail_api_payload(
            payload,
            api_url=cap.get("api_url"),
            min_content_chars=min_content_chars,
        )
        if api_result.get("content_verified"):
            return {
                **api_result,
                "html_status": html_result.get("status"),
                "html_content_chars": html_content_chars,
                "article_url": article_url,
            }

    id_value = _sample_id_value(sample_item, id_field)
    detail_candidates = _detail_api_candidates(
        article_url=article_url,
        list_url=list_url,
        id_value=id_value,
        id_field=id_field,
    )
    for api_url in detail_candidates:
        ensure_not_cancelled()
        api_result = _probe_detail_api_url(
            api_url,
            min_content_chars=min_content_chars,
            guessed=True,
        )
        if api_result.get("content_verified"):
            return {
                **api_result,
                "html_status": html_result.get("status"),
                "html_content_chars": html_content_chars,
                "article_url": article_url,
            }

    return {
        "content_verified": False,
        "content_strategy": "none",
        "content_chars": 0,
        "html_status": html_result.get("status"),
        "html_content_chars": html_content_chars,
        "article_url": article_url,
        "detail_api_candidates": detail_candidates[:6],
    }


def _sample_id_value(sample_item: dict, id_field: str | None) -> str | None:
    """从样本条目里取出用于构造详情 API 的 id 值。

    功能：若指定 id_field 则优先从 raw/顶层取该字段；否则取样本的 id 字段，统一转字符串返回。
    谁会调用：probe_article_content 在构造详情 API 候选时调用。
    直接调用：无（仅字典取值）。
    输入与结果：输入样本与可选 id 字段名；返回 id 字符串或 None。
    副作用：无。
    """
    if id_field:
        raw = sample_item.get("raw") if isinstance(sample_item.get("raw"), dict) else {}
        value = raw.get(id_field) or sample_item.get(id_field)
        if value is not None:
            return str(value)
    value = sample_item.get("id")
    return str(value) if value is not None else None


def _detail_api_candidates(
    *,
    article_url: str,
    list_url: str | None,
    id_value: str | None,
    id_field: str | None,
) -> list[str]:
    """基于文章/列表 URL 与 id 猜测一组可能的详情 JSON API 地址。

    功能：从列表 URL 父路径拼接 detail.json，或从文章 URL 的 "detail" 段构造 api 前缀，带上 id 等查询参数，去重后返回候选 API 列表。
    谁会调用：probe_article_content 在 HTML 正文不足时调用。
    直接调用：
    - _url_with_query(...)：拼接带查询参数的 URL。
    - urlparse / urljoin(...)：解析与拼接 URL。
    输入与结果：输入文章 URL、列表 URL、id 值与 id 字段；返回候选 API URL 列表。
    副作用：无。
    """
    if not id_value:
        return []
    candidates: list[str] = []
    parsed_article = urlparse(article_url)
    origin = f"{parsed_article.scheme}://{parsed_article.netloc}"
    query_keys = [key for key in (id_field, "id", "no", "slug", "uuid") if key]
    if list_url:
        parsed_list = urlparse(list_url)
        list_path = parsed_list.path
        parent = list_path.rsplit("/", 1)[0]
        if parent:
            for filename in ("detail.json", "detail"):
                base = urljoin(f"{parsed_list.scheme}://{parsed_list.netloc}", f"{parent}/{filename}")
                for key in query_keys:
                    candidates.append(_url_with_query(base, {key: id_value}))
    article_parts = [part for part in parsed_article.path.split("/") if part]
    if "detail" in article_parts:
        idx = article_parts.index("detail")
        api_parts = ["api", *article_parts[:idx], "detail.json"]
        base = urljoin(origin, "/" + "/".join(api_parts))
        for key in query_keys:
            candidates.append(_url_with_query(base, {key: id_value}))
    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped[:12]


def _url_with_query(url: str, values: dict[str, str]) -> str:
    """把给定查询参数合并进 URL，返回重组后的 URL。

    功能：解析原 URL 查询串，更新/追加传入的键值对后重新拼装，用于给详情 API 附上 id 等参数。
    谁会调用：_detail_api_candidates 在构造候选 API 时调用。
    直接调用：urlparse / parse_qsl / urlencode / urlunparse(...)：解析与重组 URL。
    输入与结果：输入 URL 与参数字典；返回新 URL 字符串。
    副作用：无。
    """
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(values)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _probe_detail_api_url(api_url: str, *, min_content_chars: int, guessed: bool = False) -> dict:
    """请求一个详情 API URL，若返回 JSON 且含足量正文则判定验证通过。

    功能：GET 该 API，校验状态码与 content-type 为 JSON，再交给 _probe_detail_api_payload 判定是否含正文，返回验证结论。
    谁会调用：probe_article_content 在逐个验证猜测的详情 API 时调用。
    直接调用：
    - _probe_detail_api_payload(...)：判定 JSON 载荷是否含正文。
    - httpx.get(...)：发起请求。
    输入与结果：输入 API URL、最小字数与是否猜测；返回验证结论 dict。
    副作用：发起一次 HTTP GET（网络请求）。
    """
    import httpx

    try:
        response = httpx.get(api_url, timeout=8, follow_redirects=True)
        content_type = response.headers.get("content-type", "")
        if response.status_code >= 400 or "json" not in content_type.lower():
            return {"content_verified": False}
        payload = response.json()
    except Exception:
        return {"content_verified": False}
    return _probe_detail_api_payload(payload, api_url=api_url, min_content_chars=min_content_chars, guessed=guessed)


def _probe_detail_api_payload(
    payload: object,
    *,
    api_url: str | None,
    min_content_chars: int,
    guessed: bool = False,
) -> dict:
    """判断一个 JSON 载荷是否含足量正文，并产出字段路径映射。

    功能：在 payload 里找最长正文字段路径，比对去标签后的长度是否达标，再找 title/summary/published_at 等路径，输出内容获取方案。
    谁会调用：probe_article_content、_probe_detail_api_url 在验证 JSON 载荷时调用。
    直接调用：
    - _find_best_text_path(...)：找最佳正文字段路径。
    - _find_first_text_path(...)：找首个命名字段路径。
    - _strip_tags(...)：去 HTML 标签算纯文本长度。
    - _template_api_url(...)：把 API URL 转成模板。
    输入与结果：输入 JSON 载荷、API URL、最小字数与是否猜测；返回验证结论 dict。
    副作用：无。
    """
    content_path, content_text = _find_best_text_path(payload, ("content", "body", "html", "article"))
    if not content_path or len(_strip_tags(content_text)) < min_content_chars:
        return {"content_verified": False}
    field_paths = {
        "content": content_path,
        "title": _find_first_text_path(payload, ("title", "name", "subject")),
        "summary": _find_first_text_path(payload, ("summary", "description", "desc", "brief")),
        "published_at": _find_first_text_path(payload, ("publishTime", "published_at", "publishedAt", "date", "created_at", "gmtCreate")),
    }
    return {
        "content_verified": True,
        "content_strategy": "guessed_detail_api" if guessed else "detail_api",
        "content_chars": len(_strip_tags(content_text)),
        "detail_api": {
            "url_template": _template_api_url(api_url),
            "fields": {k: v for k, v in field_paths.items() if v},
            "method": "GET",
        },
    }


def _capture_article_json_responses(article_url: str) -> list[dict]:
    """打开文章页并捕获加载过程中返回的 JSON 接口响应。

    功能：用 Playwright 打开文章页并触发动态加载，监听响应，收集 JSON 类型的接口响应（含请求体）返回，供后续验证详情 API。
    谁会调用：probe_article_content 在 HTML 正文不足时调用。
    直接调用：
    - exercise_dynamic_page(...)：滚动/加载更多触发接口。
    输入与结果：输入文章 URL；返回响应 dict 列表（api_url/status/parsed_json 等）。
    副作用：启动 Playwright 浏览器并联网（浏览器副作用）。
    """
    from playwright.sync_api import sync_playwright

    caps: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(resp):
            """Playwright response 回调：只收 JSON 响应，截断后解析并追加到 caps。

            功能：按 content-type 过滤 JSON 响应，截断过大 body 后解析，记录 api_url/status/parsed_json。
            谁会调用：page.on("response", on_response) 在抓取文章 JSON 时回调。
            直接调用：
            - resp.headers.get(...)/resp.text()：取头部与响应体。
            - json.loads(...)：解析 JSON。
            输入与结果：输入 resp 响应对象；无返回值（结果追加到 caps）。
            副作用：修改 caps 列表。
            """
            try:
                content_type = resp.headers.get("content-type", "")
                if "json" not in content_type.lower():
                    return
                body = resp.text()
                if not body or len(body) > 500000:
                    return
                caps.append({"api_url": resp.url, "status": resp.status, "parsed_json": json.loads(body)})
            except Exception:
                return

        page.on("response", on_response)
        try:
            page.goto(article_url, wait_until="networkidle", timeout=15000)
        except Exception:
            pass
        exercise_dynamic_page(page, scroll_rounds=3, wait_ms=600)
        browser.close()
    return caps[:10]


def _find_best_text_path(payload: object, names: tuple[str, ...]) -> tuple[str | None, str]:
    """在 JSON 载荷里找出最匹配给定名称的、文本最长的字段路径。

    功能：递归遍历，命中名称（含嵌套）且为字符串的字段时，去标签取纯文本，保留最长的作为最佳正文/标题路径。
    谁会调用：_probe_detail_api_payload 在找正文字段时调用，_find_first_text_path 在找首个路径时调用。
    直接调用：
    - _strip_tags(...)：去标签算纯文本长度。
    输入与结果：输入载荷与候选名称元组；返回 (最佳路径, 最佳文本)。
    副作用：无。
    """
    best_path = None
    best_text = ""

    def walk(node: object, path: str) -> None:
        """递归遍历 JSON，在命中候选名称且为字符串的字段里保留纯文本最长者作为最佳正文/标题路径。

        功能：遇到键名含候选名、值为字符串的字段，去标签取纯文本，长度超过当前最佳则更新 best_path/best_text；
        并继续递归深入 dict/list。
        谁会调用：_find_best_text_path 在查找正文/标题路径时调用。
        直接调用：
        - _strip_tags(...)：去标签算纯文本长度。
        输入与结果：输入节点与累计路径；无返回值（结果写入 best_path/best_text）。
        副作用：无。
        """
        nonlocal best_path, best_text
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path}.{key}" if path else key
                if any(name.lower() in key.lower() for name in names) and isinstance(value, str):
                    text = _strip_tags(value)
                    if len(text) > len(best_text):
                        best_path = child_path
                        best_text = value
                walk(value, child_path)
        elif isinstance(node, list):
            for idx, value in enumerate(node[:5]):
                walk(value, f"{path}.{idx}" if path else str(idx))

    walk(payload, "")
    return best_path, best_text


def _find_first_text_path(payload: object, names: tuple[str, ...]) -> str | None:
    """返回首个命中给定名称的文本字段路径（无文本则为 None）。

    功能：复用 _find_best_text_path，仅在确有文本内容时返回路径，便于取 title/summary 等单值字段。
    谁会调用：_probe_detail_api_payload 在取 title/summary/published_at 路径时调用。
    直接调用：
    - _find_best_text_path(...)：实际查找路径。
    输入与结果：输入载荷与候选名称；返回路径字符串或 None。
    副作用：无。
    """
    path, text = _find_best_text_path(payload, names)
    return path if text else None


def _strip_tags(value: str) -> str:
    """去除 HTML 标签并压缩空白，得到纯文本。

    功能：把字符串里的标签删掉，再把多余空白压成单空格，用于正文长度判断。
    谁会调用：_probe_detail_api_payload、_find_best_text_path 在算纯文本长度时调用。
    直接调用：re.sub(...)：删除标签并压缩空白。
    输入与结果：输入含 HTML 的字符串；返回纯文本。
    副作用：无。
    """
    return " ".join(re.sub(r"<[^>]+>", " ", str(value or "")).split())


def _template_api_url(api_url: str | None) -> str:
    """把详情 API URL 的 id 类查询参数改写成 {item.xxx} 模板形式。

    功能：解析 URL 查询串，将 id/no/slug/uuid 等值替换为 {item.字段} 模板，便于回填到 DSL 的 url 模板。
    谁会调用：_probe_detail_api_payload 在生成详情 API 模板时调用。
    直接调用：urlparse / parse_qsl / urlunparse(...)：解析与重组 URL。
    输入与结果：输入 API URL；返回模板化 URL 字符串（空输入返回空串）。
    副作用：无。
    """
    if not api_url:
        return ""
    parsed = urlparse(api_url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    templated = [
        f"{key}={{item.{key}}}" if key in {"id", "no", "slug", "uuid"} else f"{key}={value}"
        for key, value in query
    ]
    return urlunparse(parsed._replace(query="&".join(templated)))


TOOLS = [
    fetch_page,
    capture_network,
    inspect_item,
    probe_html_entries,
    test_url_template,
    test_path_join,
    probe_url_patterns,
    probe_embedded_json,
    probe_article_content,
]
