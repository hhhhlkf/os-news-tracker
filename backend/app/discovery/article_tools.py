from __future__ import annotations

import html
import re
import time
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from lxml import html as lxml_html

DEFAULT_ARTICLE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}

AGGREGATOR_SUMMARY_PATTERNS = (
    "article url:",
    "comments url:",
    "points:",
    "# comments:",
    "news.ycombinator.com/item",
)


def article_page_needs_content(
    item: dict[str, Any],
    *,
    min_existing_chars: int = 120,
) -> bool:
    """判断单条条目是否还需要补抓详情页正文。

    功能：若已有足够长的 content 或可信摘要（且不像聚合器摘要）则不需要；否则返回 True。
    谁会调用：should_enrich_article_pages_for_exploration、enrich_article_pages、website/recipe_audit.py 在筛选待补抓条目时调用。
    直接调用：
    - _compact_text(...)：压缩空白以便比较长度。
    - _strip_markup(...)：去除摘要里的 HTML 标记。
    - _looks_like_aggregator_summary(...)：排除聚合器式摘要。
    输入与结果：输入 item 字典与最小字符阈值；返回布尔。
    副作用：无。
    """
    content = _compact_text(item.get("content") or "")
    if len(content) >= min_existing_chars:
        return False
    summary = _compact_text(_strip_markup(str(item.get("summary") or "")))
    if len(summary) >= min_existing_chars and not _looks_like_aggregator_summary(summary):
        return False
    return True


def should_enrich_article_pages_for_exploration(exploration: dict[str, Any]) -> bool:
    """根据探查证据判断该来源是否应生成「补抓文章正文」动作。

    功能：仅对 rss/atom/html/json_api 来源生效；对 HN 类来源直接开启，否则抽样若干 sample_items，
    若过半样本正文不足且不像聚合器，则认为需要补抓。
    谁会调用：website/recipe_writer.dsl_writer 在生成 DSL 时决定是否追加 enrich_article_pages 动作。
    直接调用：
    - urlparse(...)：取列表页 host。
    - _sample_text(...)：从样本取字段文本。
    - article_page_needs_content(...)：判断单条是否需要补抓。
    输入与结果：输入 exploration 字典；返回布尔。
    副作用：无。
    """
    source_type = str(exploration.get("source_type") or "").lower()
    if source_type not in {"rss", "atom", "html", "json_api"}:
        return False
    list_url = str(exploration.get("list_url") or "")
    host = urlparse(list_url).netloc.lower()
    samples = exploration.get("sample_items") or []
    if "hnrss.org" in host or host == "news.ycombinator.com":
        return True
    if not samples:
        return False
    insufficient = 0
    checked = 0
    for sample in samples[:5]:
        if not isinstance(sample, dict):
            continue
        checked += 1
        candidate = {
            "content": _sample_text(sample, "content", "textContent", "body", "text"),
            "summary": _sample_text(sample, "summary", "description", "desc", "brief"),
        }
        if article_page_needs_content(candidate):
            insufficient += 1
    return checked > 0 and insufficient >= max(1, (checked + 1) // 2)


def enrich_article_pages(
    items: list[dict[str, Any]],
    *,
    fetch_content: bool = True,
    fill_missing_only: bool = True,
    max_items: int | None = None,
    timeout_seconds: float = 6.0,
    content_char_limit: int = 3000,
    min_existing_chars: int = 120,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """批量补抓文章详情页，回填标题/摘要/正文/发布时间。

    功能：先按 fill_missing_only 与 article_page_needs_content 筛出需要抓取的条目（受 max_items 限制），
    逐条抓取并抽取字段回填，期间通过回调上报进度，单条失败不影响其他条目。
    谁会调用：interpreter._enrich_article_pages 在执行 DSL 的 enrich_article_pages 动作时调用。
    直接调用：
    - article_page_needs_content(...)：筛选需要补抓的条目。
    - fetch_article_page_content(...)：抓取并抽取单个文章页。
    - _emit_progress(...)：上报补抓进度事件。
    输入与结果：输入 items 列表与抓取选项；返回含 enriched items 与统计（status/attempted/enriched 等）的 dict。
    副作用：对每条待补抓文章发起 HTTP 请求（网络副作用），并通过回调产出进度。
    """
    next_items = [dict(item) for item in items]
    fetch_indices: list[int] = []
    for idx, item in enumerate(next_items):
        if not item.get("url"):
            continue
        if fill_missing_only and not article_page_needs_content(item, min_existing_chars=min_existing_chars):
            continue
        if max_items is not None and len(fetch_indices) >= max_items:
            continue
        fetch_indices.append(idx)

    _emit_progress(
        progress_callback,
        "article_enrich_started",
        total_items=len(next_items),
        selected_items=len(fetch_indices),
        max_items=max_items,
        fetch_content=fetch_content,
        fill_missing_only=fill_missing_only,
    )

    attempted = 0
    enriched = 0
    statuses: set[str] = set()
    for idx in fetch_indices:
        attempted += 1
        item = next_items[idx]
        _emit_progress(
            progress_callback,
            "article_enrich_item_started",
            attempted_count=attempted,
            max_items=max_items,
            title=item.get("title"),
            url=item.get("url"),
        )
        try:
            article = fetch_article_page_content(
                str(item["url"]),
                timeout_seconds=timeout_seconds,
                content_char_limit=content_char_limit,
                progress_callback=progress_callback,
                progress_context={
                    "attempted_count": attempted,
                    "max_items": max_items,
                    "title": item.get("title"),
                },
            )
        except Exception as exc:  # noqa: BLE001 - 单篇失败不影响其他候选
            _emit_progress(
                progress_callback,
                "article_page_fetch_failed",
                attempted_count=attempted,
                max_items=max_items,
                title=item.get("title"),
                url=item.get("url"),
                error=str(exc)[:300],
            )
            article = {"status": "error", "content": ""}

        status = str(article.get("status") or "unknown")
        statuses.add(status)
        did_enrich = False
        if status in {"ok", "empty"}:
            if article.get("resolved_url"):
                item["url"] = article["resolved_url"]
            if article.get("title") and not item.get("title"):
                item["title"] = article["title"]
            if article.get("summary") and not item.get("summary"):
                item["summary"] = article["summary"]
            if article.get("published_at") and not item.get("published_at"):
                item["published_at"] = article["published_at"]
            if fetch_content and article.get("content") and not item.get("content"):
                item["content"] = article["content"]
            did_enrich = bool(article.get("content") or article.get("summary"))
        if did_enrich:
            enriched += 1
        _emit_progress(
            progress_callback,
            "article_enrich_item_finished",
            attempted_count=attempted,
            enriched_count=enriched,
            title=item.get("title"),
            url=item.get("url"),
            status=status,
        )

    status = "ok"
    if statuses and statuses <= {"unsupported", "empty", "error"} and enriched == 0:
        status = "empty"
    _emit_progress(
        progress_callback,
        "article_enrich_finished",
        status=status,
        attempted_count=attempted,
        enriched_count=enriched,
        total_items=len(next_items),
        selected_items=len(fetch_indices),
        fetch_content=fetch_content,
    )
    return {
        "status": status,
        "items": next_items,
        "attempted_count": attempted,
        "enriched_count": enriched,
        "selected_count": len(fetch_indices),
        "fetch_content": fetch_content,
    }


def fetch_article_page_content(
    url: str,
    *,
    timeout_seconds: float = 6.0,
    content_char_limit: int = 3000,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    progress_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """抓取单个文章页并抽取结构化字段。

    功能：用浏览器式请求头 GET 文章页，校验状态码与内容类型，解析出 title/summary/published_at/content，
    并在请求前后通过回调上报进度；非 HTML 或 ≥400 时返回 unsupported。
    谁会调用：enrich_article_pages 批量补抓时、tools.probe_article_content 探查时调用。
    直接调用：
    - httpx.Client.get(...)：发起文章页请求。
    - extract_article_fields(...)：从 HTML 抽取字段。
    - _emit_progress(...)：上报抓取进度。
    输入与结果：输入 url 与选项；返回含 status、resolved_url、content/title/summary/published_at 的 dict。
    副作用：发起一次 HTTP GET（网络请求）。
    """
    context = dict(progress_context or {})
    started_at = time.monotonic()
    _emit_progress(progress_callback, "article_page_fetch_request_started", **context, url=url)
    with httpx.Client(
        headers=DEFAULT_ARTICLE_HEADERS,
        timeout=httpx.Timeout(timeout_seconds, connect=min(3.0, timeout_seconds)),
        follow_redirects=True,
        max_redirects=3,
    ) as client:
        response = client.get(url)
    duration_ms = int((time.monotonic() - started_at) * 1000)
    body_text = response.text
    content_type = response.headers.get("content-type") or ""
    _emit_progress(
        progress_callback,
        "article_page_fetch_response_received",
        **context,
        url=url,
        final_url=str(response.url),
        status_code=response.status_code,
        duration_ms=duration_ms,
        body_chars=len(body_text),
        content_type=content_type[:80],
    )
    if response.status_code >= 400:
        return {"status": "unsupported", "content": "", "resolved_url": str(response.url)}
    if "html" not in content_type.lower() and body_text.lstrip()[:20].lower().startswith("<?xml"):
        return {"status": "unsupported", "content": "", "resolved_url": str(response.url)}

    parse_started_at = time.monotonic()
    extracted = extract_article_fields(body_text, content_char_limit=content_char_limit)
    parse_ms = int((time.monotonic() - parse_started_at) * 1000)
    status = "ok" if extracted.get("content") else "empty"
    _emit_progress(
        progress_callback,
        "article_page_fetch_finished",
        **context,
        url=url,
        final_url=str(response.url),
        status=status,
        status_code=response.status_code,
        duration_ms=int((time.monotonic() - started_at) * 1000),
        body_chars=len(body_text),
        content_chars=len(extracted.get("content") or ""),
        parse_ms=parse_ms,
    )
    return {
        "status": status,
        "resolved_url": str(response.url),
        **extracted,
    }


def extract_article_fields(html_text: str, *, content_char_limit: int = 3000) -> dict[str, str | None]:
    """从文章 HTML 解析出 title、summary、published_at 与正文主文本。

    功能：用 lxml 解析页面，优先读 og/meta 与 <time datetime>，再用 _extract_main_text 抽取正文，统一压缩空白。
    谁会调用：fetch_article_page_content 在拿到页面后调用。
    直接调用：
    - lxml_html.fromstring(...)：解析 HTML 为树。
    - _first_meta(...)：读 meta 标签字段。
    - _first_attr(...)：读 time 标签的 datetime 属性。
    - _extract_main_text(...)：抽取正文主文本。
    - _compact_text(...)：压缩字段空白。
    输入与结果：输入 HTML 文本；返回含 content/title/summary/published_at 的字典。
    副作用：无。
    """
    try:
        tree = lxml_html.fromstring(html_text)
    except Exception:
        return {"content": "", "title": None, "summary": None, "published_at": None}

    title = _first_meta(tree, "property", "og:title") or _first_meta(tree, "name", "twitter:title")
    if not title:
        title_nodes = tree.xpath("//title/text()")
        title = title_nodes[0].strip() if title_nodes else None
    summary = (
        _first_meta(tree, "property", "og:description")
        or _first_meta(tree, "name", "description")
        or _first_meta(tree, "name", "twitter:description")
    )
    published_at = (
        _first_meta(tree, "property", "article:published_time")
        or _first_meta(tree, "name", "pubdate")
        or _first_attr(tree, "//time[@datetime]", "datetime")
    )
    content = _extract_main_text(tree, content_char_limit=content_char_limit)
    return {
        "content": content,
        "title": _compact_text(title or "") or None,
        "summary": _compact_text(summary or "") or None,
        "published_at": _compact_text(published_at or "") or None,
    }


def _extract_main_text(tree: Any, *, content_char_limit: int) -> str:
    """从解析树抽取正文主文本（去掉脚本/样式/导航等噪声，取最长候选块）。

    功能：删除脚本/样式/导航/页脚等节点，优先在 article/main/post/content 等容器里找最长文本块，截断到字符上限。
    谁会调用：extract_article_fields 在抽取正文时调用。
    直接调用：
    - _compact_text(...)：压缩文本空白。
    - html.unescape(...)：还原 HTML 实体。
    - tree.xpath(...)：定位噪声节点与候选容器。
    输入与结果：输入 lxml 树；返回正文文本（已截断）。
    副作用：无。
    """
    for node in tree.xpath("//script|//style|//noscript|//nav|//header|//footer|//aside|//form"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    candidates = tree.xpath(
        "//article | //main | //*[@role='main'] | "
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' post ')] | "
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' entry ')] | "
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' content ')]"
    )
    if not candidates:
        body = tree.xpath("//body")
        candidates = body or [tree]
    best_text = ""
    for node in candidates[:12]:
        text = _compact_text(" ".join(node.itertext()))
        if len(text) > len(best_text):
            best_text = text
    return html.unescape(best_text)[:content_char_limit]


def _first_meta(tree: Any, attr_name: str, attr_value: str) -> str | None:
    """按 meta 标签的某属性值取 content。

    功能：用大小写不敏感的 XPath 查找指定 meta 标签的 content 值，用于读 og:title、description、发布时间等。
    谁会调用：extract_article_fields 在取标题/摘要/时间时调用。
    直接调用：
    - tree.xpath(...)：执行 meta 查询。
    输入与结果：输入解析树、属性名与属性值；返回 content 字符串或 None。
    副作用：无。
    """
    values = tree.xpath(
        f"//meta[translate(@{attr_name}, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
        f"'{attr_value.lower()}']/@content"
    )
    return str(values[0]).strip() if values else None


def _first_attr(tree: Any, xpath: str, attr_name: str) -> str | None:
    """按 XPath 取某个节点的属性值。

    功能：用于在 time 标签等位置取 datetime 等属性。
    谁会调用：extract_article_fields 在取发布时间时调用。
    直接调用：
    - tree.xpath(...)：执行属性查询。
    输入与结果：输入解析树、XPath 与属性名；返回属性字符串或 None。
    副作用：无。
    """
    values = tree.xpath(f"{xpath}/@{attr_name}")
    return str(values[0]).strip() if values else None


def _sample_text(sample: dict[str, Any], *keys: str) -> str:
    """从样本对象中按优先级取第一个非空文本字段。

    功能：依次尝试给定的键，命中非空值即返回；样本内嵌 raw 字典时也尝试同样的键。
    谁会调用：should_enrich_article_pages_for_exploration 在构造候选条目时调用。
    直接调用：无（仅字典取值）。
    输入与结果：输入样本字典与若干键名；返回首个非空文本或空串。
    副作用：无。
    """
    for key in keys:
        value = sample.get(key)
        if value:
            return str(value)
    raw = sample.get("raw")
    if isinstance(raw, dict):
        for key in keys:
            value = raw.get(key)
            if value:
                return str(value)
    return ""


def _looks_like_aggregator_summary(text: str) -> bool:
    """判断摘要是否像聚合器（如 Hacker News）格式而非正文摘要。

    功能：匹配 "article url:"、"points:"、"news.ycombinator.com" 等聚合器特有片段，避免把这类摘要误当正文。
    谁会调用：article_page_needs_content 在判断已有摘要是否足够时调用。
    直接调用：无（与常量模式匹配）。
    输入与结果：输入文本；返回布尔。
    副作用：无。
    """
    lowered = text.lower()
    return any(pattern in lowered for pattern in AGGREGATOR_SUMMARY_PATTERNS)


def _strip_markup(value: str) -> str:
    """去除 HTML 标签与字符实体，得到纯文本。

    功能：先把实体反转义，再用正则删掉所有标签，用于清理摘要文本做长度判断。
    谁会调用：article_page_needs_content 在清理摘要时调用。
    直接调用：
    - re.sub(...)：删除标签。
    - html.unescape(...)：反转义实体。
    输入与结果：输入含 HTML 的字符串；返回纯文本。
    副作用：无。
    """
    return re.sub(r"<[^>]+>", " ", html.unescape(value or ""))


def _compact_text(value: str) -> str:
    """把任意空白（含换行/多空格）压缩成单空格并去掉首尾空白。

    功能：统一文本空白，便于长度比较与展示。
    谁会调用：article_page_needs_content、extract_article_fields、_extract_main_text、_strip_markup 等多处调用。
    直接调用：无（仅字符串处理）。
    输入与结果：输入字符串；返回压缩后的文本。
    副作用：无。
    """
    return " ".join(str(value or "").split())


def _emit_progress(
    callback: Callable[[str, dict[str, Any]], None] | None,
    event: str,
    **payload: Any,
) -> None:
    """把进度事件透传给外部回调（无回调则跳过）。

    功能：在文章补抓流程各阶段统一上报事件名与负载，供上层写日志/推送。
    谁会调用：enrich_article_pages、fetch_article_page_content 在关键节点调用。
    直接调用：无（仅调用传入的 callback）。
    输入与结果：输入回调、事件名与任意负载；无返回值。
    副作用：通过回调产生进度输出（无自身副作用）。
    """
    if callback is None:
        return
    callback(event, payload)
