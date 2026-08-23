"""微信抓取工具集。

功能：提供微信公众号文章的检索、账号解析、历史文章抓取、单篇正文补抓与批量富化能力，覆盖搜狗检索与公众号后台 API 两种来源，并把抓取结果整理成统一的条目结构。
谁会调用：多源发现图（multi_graph / multi_interpreter）在路由到微信分支时调用这里的入口函数（wechat_search_articles、wechat_fetch_account_history、wechat_enrich_articles 等）。
直接调用：
- httpx / Playwright：发起检索与抓取的网络请求。
- app.wechat_auth：读取与失效微信公众号登录态。
- app.llm.client：在开启 LLM 主题预筛时调用模型判断是否值得补抓正文。
输入与结果：各函数接收检索词 / 账号标识 / 文章 URL 等，返回含 items、status 等字段的字典。
副作用：发起网络请求（搜狗检索、公众号 API、文章页下载）、调用 LLM（可选）、在登录失效时写数据库标记、写运行日志与进度回调。
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

SOGOU_WEIXIN_URL = "https://weixin.sogou.com/weixin"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
WECHAT_ARTICLE_CONTENT_CHAR_LIMIT = 2000
WECHAT_TOPIC_PRECHECK_TIMEOUT_SECONDS = 8.0
WECHAT_ARTICLE_HEAD_SCAN_CHARS = 200_000
WECHAT_ARTICLE_CONTENT_SCAN_CHARS = 256_000
_DATE_PATTERNS = (
    re.compile(r"(?P<year>20\d{2})[-/.年](?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})日?"),
    re.compile(r"(?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})日?\s*(?P<year>20\d{2})"),
)
_EMBEDDED_TS_PATTERNS = (
    re.compile(r"timeConvert\(['\"]?(\d{10})['\"]?\)"),
    re.compile(r"\b(?:publish_time|ct)\s*=\s*['\"]?(\d{10})['\"]?"),
    re.compile(r'"publish_time"\s*:\s*"?(\\d{10})"?'.replace("\\\\d", "\\d")),
)
DEFAULT_SEARCH_MAX_PAGES = 5


def _unescape_url(url: str) -> str:
    """解码 HTML 转义过的 URL，但不破坏查询参数。

    功能：只还原 HTML 属性里常见的 ``&amp;`` / ``&#38;`` / ``&#x26;`` 转义，避免 ``html.unescape`` 把 ``&timestamp=`` 错认成 ``&times`` 实体而破坏参数。
    谁会调用：``normalize_wechat_article_url``、``_extract_articles_from_html``、``_extract_redirect_url``、``wechat_article_key`` 在拿到原始 URL 时都会调用。
    直接调用：
    - 无（纯字符串替换，不调用其它函数）。
    输入与结果：输入可能含 HTML 实体的 URL 字符串；返回清理后的 URL 字符串（空输入原样返回）。
    副作用：无。
    """
    text = str(url or "").strip()
    if not text:
        return text
    return (
        text.replace("&amp;", "&")
        .replace("&#38;", "&")
        .replace("&#x26;", "&")
        .replace("&#X26;", "&")
    )


def normalize_wechat_article_url(url: str) -> str:
    """规范化微信文章 URL。

    功能：先把可能含 HTML 实体的文章 URL 解码，再把 ``mp.weixin.qq.com`` 的 http 链接统一升级为 https，方便后续去重与抓取。
    谁会调用：``wechat_fetch_article_content``、``wechat_enrich_articles``、``wechat_article_key`` 在拿到文章 URL 时都会先规范化。
    直接调用：
    - _unescape_url(...)：先还原 HTML 实体转义。
    输入与结果：输入原始文章 URL；返回规范化后的 URL（http→https 升级 + 实体解码）。
    副作用：无。
    """
    text = _unescape_url(url)
    parsed = urlparse(text)
    if parsed.scheme == "http" and parsed.netloc.lower() == "mp.weixin.qq.com":
        return parsed._replace(scheme="https").geturl()
    return text


def _article_head_slice(text: str) -> str:
    """截取微信文章 HTML 的 head 及前段内容。

    功能：优先按 ``</head>`` 切出文档头，切不到则取前一大段，供解析标题/描述/发布时间等元信息，避免整页解析开销过大。
    谁会调用：``wechat_fetch_article_content`` 在解析单篇正文前调用。
    直接调用：
    - 无（纯字符串切片，不调用其它函数）。
    输入与结果：输入整页 HTML 文本；返回 head 或前段文本。
    副作用：无。
    """
    lower = text[:WECHAT_ARTICLE_HEAD_SCAN_CHARS].lower()
    end = lower.find("</head>")
    if end >= 0:
        return text[: end + len("</head>")]
    return text[:WECHAT_ARTICLE_HEAD_SCAN_CHARS]


def _extract_js_content_window(text: str) -> str:
    """截取微信正文所在的 js_content 容器片段。

    功能：定位 ``id="js_content"`` 的微信正文容器，截取其起始 div 到一段上限长度的文本，作为后续去除标签、抽取正文的来源，避免整页解析开销。
    谁会调用：``wechat_fetch_article_content`` 在抽取正文时调用。
    直接调用：
    - 无（纯字符串查找与切片，不调用其它函数）。
    输入与结果：输入整页 HTML 文本；返回 js_content 容器片段，找不到时返回空串。
    副作用：无。
    """
    lower = text.lower()
    marker_pos = lower.find('id="js_content"')
    if marker_pos < 0:
        marker_pos = lower.find("id='js_content'")
    if marker_pos < 0:
        return ""
    start = text.rfind("<div", 0, marker_pos)
    if start < 0:
        start = marker_pos
    end_limit = min(len(text), start + WECHAT_ARTICLE_CONTENT_SCAN_CHARS)
    return text[start:end_limit]


def wechat_search_articles(
    query: str,
    limit: int | None = None,
    max_pages: int | None = DEFAULT_SEARCH_MAX_PAGES,
    resolve_final_urls_limit: int | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict:
    """通过搜狗微信检索入口搜索微信文章。

    功能：按关键词在搜狗微信里检索公众号文章，默认走 Playwright 渲染页面，降低被搜狗反爬验证码拦截的概率，返回统一结构的文章列表。
    谁会调用：多源发现图（multi_graph / multi_interpreter）在微信检索分支调用本入口。
    直接调用：
    - _emit_progress(...)：回报检索开始的进度事件。
    - _search_via_playwright(...)：实际用 Playwright 渲染并提取检索结果。
    输入与结果：输入检索词、数量上限、翻页上限、URL 解析上限与进度回调；返回含 status 与 items 的字典。
    副作用：发起 Playwright 浏览器请求（网络），写进度回调与日志。
    """
    _emit_progress(
        progress_callback,
        "wechat_search_started",
        query=query,
        limit=limit,
        max_pages=max_pages,
        resolve_final_urls_limit=resolve_final_urls_limit,
    )
    logger.info("wechat_search_articles using Playwright by default")
    return _search_via_playwright(
        query,
        limit,
        max_pages=max_pages,
        resolve_final_urls_limit=resolve_final_urls_limit,
        progress_callback=progress_callback,
    )


def _search_via_httpx(
    query: str,
    limit: int | None = None,
    *,
    max_pages: int | None = DEFAULT_SEARCH_MAX_PAGES,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict:
    """用 httpx 直接请求搜狗微信检索页并提取文章。

    功能：以普通 HTTP 方式翻页请求搜狗微信检索结果，识别验证码/限流后抽取文章链接；当前 ``wechat_search_articles`` 默认走 Playwright，本函数保留为 httpx 回退实现。
    谁会调用：当前 ``wechat_search_articles`` 默认调用的是 ``_search_via_playwright``，本函数作为备用检索实现保留（暂无直接调用方）。
    直接调用：
    - _emit_progress(...)：回报每页检索进度。
    - _is_sogou_captcha(...)：识别搜狗验证码页。
    - _extract_articles_from_html(...)：从检索页 HTML 抽文章链接。
    - _has_next_page(...)：判断是否还有下一页。
    - httpx.Client：发起检索页 HTTP 请求。
    输入与结果：输入检索词、数量上限、翻页上限与进度回调；返回含 status 与 items 的字典。
    副作用：发起 HTTP 请求（网络），写进度回调与日志。
    """
    items: list[dict] = []
    with httpx.Client(headers=DEFAULT_HEADERS, timeout=20, follow_redirects=True) as client:
        page_no = 1
        total_pages = max_pages or DEFAULT_SEARCH_MAX_PAGES
        while page_no <= total_pages:
            _emit_progress(
                progress_callback,
                "wechat_search_page_started",
                query=query,
                page_no=page_no,
                total_pages=total_pages,
                fetched_count=len(items),
                transport="httpx",
            )
            params = {"type": "2", "query": query, "ie": "utf8"}
            if page_no > 1:
                params["page"] = str(page_no)
            response = client.get(SOGOU_WEIXIN_URL, params=params)
            if response.status_code in {403, 429}:
                _emit_progress(
                    progress_callback,
                    "wechat_search_rate_limited",
                    query=query,
                    page_no=page_no,
                    raw_status=response.status_code,
                    fetched_count=len(items),
                    transport="httpx",
                )
                return {"status": "rate_limited", "items": items, "raw_status": response.status_code}
            response.raise_for_status()
            text = response.text
            if _is_sogou_captcha(text):
                _emit_progress(
                    progress_callback,
                    "wechat_search_captcha_required",
                    query=query,
                    page_no=page_no,
                    fetched_count=len(items),
                    transport="httpx",
                )
                return {
                    "status": "captcha_required",
                    "items": items,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
            remaining = None if limit is None else max(limit - len(items), 0)
            if remaining == 0:
                break
            page_items = _extract_articles_from_html(text, remaining)
            if not page_items:
                _emit_progress(
                    progress_callback,
                    "wechat_search_page_empty",
                    query=query,
                    page_no=page_no,
                    fetched_count=len(items),
                    transport="httpx",
                )
                break
            items.extend(page_items)
            _emit_progress(
                progress_callback,
                "wechat_search_page_finished",
                query=query,
                page_no=page_no,
                page_items=len(page_items),
                fetched_count=len(items),
                transport="httpx",
            )
            if limit is not None and len(items) >= limit:
                items = items[:limit]
                break
            if not _has_next_page(text):
                break
            page_no += 1
    status = "ok" if items else "empty"
    _emit_progress(
        progress_callback,
        "wechat_search_finished",
        query=query,
        fetched_count=len(items),
        status=status,
        transport="httpx",
    )
    return {
        "status": status,
        "items": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _search_via_playwright(
    query: str,
    limit: int | None = None,
    *,
    max_pages: int | None = DEFAULT_SEARCH_MAX_PAGES,
    resolve_final_urls_limit: int | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict:
    """用 Playwright 渲染搜狗微信检索页并抽取文章链接。

    功能：启动无头 Chromium 渲染搜狗微信检索结果，等待文章选择器出现，识别验证码/限流后逐页抽取文章，并可把搜狗的 /link 跳转解析成真实 mp.weixin.qq.com URL。
    谁会调用：``wechat_search_articles`` 默认调用本函数执行检索。
    直接调用：
    - sync_playwright（Playwright）：启动浏览器并渲染页面。
    - _is_sogou_captcha(...)：识别搜狗验证码页。
    - _extract_articles_from_dom(...)：从渲染后的 DOM 抽取文章链接。
    - _has_next_page(...)：判断是否还有下一页。
    - _emit_progress(...)：回报检索进度。
    输入与结果：输入检索词、数量上限、翻页上限、URL 解析上限与进度回调；返回含 status 与 items 的字典。
    副作用：启动浏览器并发起网络请求，写进度回调与日志。
    """
    from playwright.sync_api import sync_playwright

    items: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
            )
            page = context.new_page()

            page_no = 1
            total_pages = max_pages or DEFAULT_SEARCH_MAX_PAGES
            resolved_count = 0
            while page_no <= total_pages:
                _emit_progress(
                    progress_callback,
                    "wechat_search_page_started",
                    query=query,
                    page_no=page_no,
                    total_pages=total_pages,
                    fetched_count=len(items),
                    transport="playwright",
                )
                target_url = f"{SOGOU_WEIXIN_URL}?type=2&query={query}&ie=utf8"
                if page_no > 1:
                    target_url += f"&page={page_no}"
                logger.info("Playwright navigating to Sogou: %s", target_url)
                page.goto(target_url, wait_until="domcontentloaded", timeout=60000)

                try:
                    page.wait_for_selector(".txt-box h3 a", timeout=15000)
                    logger.info("Playwright: .txt-box detected")
                except Exception:
                    try:
                        page.wait_for_selector(".news-list a", timeout=5000)
                        logger.info("Playwright: .news-list detected")
                    except Exception:
                        logger.warning("Playwright: article selectors not found")
                page.wait_for_timeout(2000)

                content = page.content()
                if _is_sogou_captcha(content):
                    browser.close()
                    _emit_progress(
                        progress_callback,
                        "wechat_search_captcha_required",
                        query=query,
                        page_no=page_no,
                        fetched_count=len(items),
                        transport="playwright",
                    )
                    return {
                        "status": "captcha_required",
                        "items": items,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    }

                remaining = None if limit is None else max(limit - len(items), 0)
                if remaining == 0:
                    break
                page_resolve_limit = None
                if resolve_final_urls_limit is not None:
                    page_resolve_limit = max(resolve_final_urls_limit - resolved_count, 0)
                page_items = _extract_articles_from_dom(
                    page,
                    remaining,
                    resolve_final_urls_limit=page_resolve_limit,
                )
                resolved_count += sum(1 for item in page_items if "mp.weixin.qq.com/" in str(item.get("url") or ""))
                if not page_items:
                    _emit_progress(
                        progress_callback,
                        "wechat_search_page_empty",
                        query=query,
                        page_no=page_no,
                        fetched_count=len(items),
                        transport="playwright",
                    )
                    break
                items.extend(page_items)
                _emit_progress(
                    progress_callback,
                    "wechat_search_page_finished",
                    query=query,
                    page_no=page_no,
                    page_items=len(page_items),
                    fetched_count=len(items),
                    resolved_count=resolved_count,
                    transport="playwright",
                )
                if limit is not None and len(items) >= limit:
                    items = items[:limit]
                    break
                if not _has_next_page(content):
                    break
                page_no += 1
            browser.close()
    except Exception as exc:
        logger.warning("Playwright Sogou search failed: %s", exc)
        _emit_progress(
            progress_callback,
            "wechat_search_failed",
            query=query,
            fetched_count=len(items),
            transport="playwright",
            error=str(exc),
        )
        return {"status": "error", "items": [], "error": str(exc),
                "fetched_at": datetime.now(timezone.utc).isoformat()}

    status = "ok" if items else "empty"
    _emit_progress(
        progress_callback,
        "wechat_search_finished",
        query=query,
        fetched_count=len(items),
        status=status,
        transport="playwright",
    )
    return {
        "status": status,
        "items": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _emit_progress(
    callback: Callable[[str, dict[str, Any]], None] | None,
    event: str,
    **payload: Any,
) -> None:
    """统一上报微信抓取进度事件。

    功能：把检索/抓取过程中的各阶段事件（开始、翻页、限流、完成等）通过回调外抛，便于上层展示实时进度；回调为空时直接跳过。
    谁会调用：本模块内几乎所有检索/抓取函数都会调用（如 wechat_search_articles、_search_via_playwright、wechat_fetch_article_content、wechat_enrich_articles）。
    直接调用：
    - 无（仅调用传入的 callback 可调用对象，不调用其它项目函数）。
    输入与结果：输入进度回调、事件名与任意键值负载；无返回值。
    副作用：调用方传入的回调（若有），可能触发 UI/日志输出；自身不改全局状态。
    """
    if callback is None:
        return
    callback(event, payload)


def _extract_articles_from_dom(
    page,
    limit: int | None,
    resolve_final_urls_limit: int | None = None,
) -> list[dict]:
    """用 Playwright DOM 选择器抽取文章链接与标题。

    功能：从搜狗渲染结果里定位 ``.txt-box > h3 > a`` 或 ``.news-list`` 里的文章链接，必要时在浏览器会话内把搜狗 /link 跳转解析成真实 mp.weixin.qq.com URL，并顺带抽出发布时间与摘要。
    谁会调用：``_search_via_playwright`` 在每页结果里调用本函数。直接调用：
    - _resolve_sogou_link_in_context(...)：在浏览器内解析 /link 跳转得到真实 URL。
    - _extract_datetime_from_text(...)：从上下文抽发布时间。
    - _extract_summary_from_context(...)：从上下文抽摘要。
    - _strip_tags(...)：去除上下文 HTML 标签。
    输入与结果：输入 Playwright page、数量上限与 URL 解析上限；返回文章条目列表（含 title/url/published_at/summary）。
    副作用：调用 Playwright 元素查询/求值（浏览器内操作）；异常时记日志并返回已抽到的条目。
    """
    items: list[dict] = []
    try:
        title_links = page.query_selector_all(".txt-box h3 a")
        if not title_links:
            title_links = page.query_selector_all(".news-list a[href*='/link?url=']")

        resolved_count = 0
        selected_links = title_links if limit is None else title_links[:limit]
        for el in selected_links:
            href = (el.get_attribute("href") or "").strip()
            title = (el.inner_text() or "").strip()
            if not title:
                continue
            final_url = None
            if href.startswith("/link?"):
                href = f"https://weixin.sogou.com{href}"
                should_resolve = resolve_final_urls_limit is None or resolved_count < resolve_final_urls_limit
                if should_resolve:
                    final_url = _resolve_sogou_link_in_context(page, el)
                    if final_url:
                        resolved_count += 1
            if not href:
                continue
            context_text = ""
            try:
                context_text = el.evaluate(
                    """
                    (node) => {
                      const container = node.closest(".txt-box") || node.parentElement || node;
                      return container?.innerHTML || "";
                    }
                    """
                ) or ""
            except Exception:
                context_text = ""

            items.append({
                "title": title,
                "url": final_url or href,
                "published_at": _extract_datetime_from_text(context_text),
                "summary": _extract_summary_from_context(_strip_tags(context_text), title),
                "content": "",
            })
    except Exception as exc:
        logger.warning("DOM extraction failed: %s", exc)
    return items


def _extract_articles_from_html(text: str, limit: int | None) -> list[dict]:
    """用正则从检索页 HTML 中抽取文章链接（httpx 回退路径用）。

    功能：在 ``_search_via_httpx`` 的回退路径里，用正则匹配 ``<a>`` 标签，抽出搜狗文章链接并还原重定向/HTML 实体，顺带从上下文提取发布时间与摘要，整理成统一条目结构。
    谁会调用：``_search_via_httpx`` 在每页结果里调用本函数。
    直接调用：
    - _unescape_url(...)：还原链接里的 HTML 实体。
    - _extract_redirect_url(...)：从跳转参数里拿到真实 URL。
    - _strip_tags(...)：去除上下文 HTML 标签。
    - _extract_datetime_from_text(...)：抽发布时间。
    - _extract_summary_from_context(...)：抽摘要。
    输入与结果：输入检索页 HTML 文本与数量上限；返回文章条目列表，达到上限即停。
    副作用：无（纯文本解析）。
    """
    items: list[dict] = []
    for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S):
        href = _unescape_url(match.group(1))
        title = _strip_tags(match.group(2))
        if href.startswith("/"):
            href = urljoin(SOGOU_WEIXIN_URL, href)
        elif "mp.weixin.qq.com" not in href and "url=" in href:
            href = _extract_redirect_url(href) or href
        if not href or not title:
            continue
        context_slice = text[max(0, match.start() - 400): min(len(text), match.end() + 1200)]
        context_text = _strip_tags(context_slice)
        items.append(
            {
                "title": title,
                "url": href,
                "published_at": _extract_datetime_from_text(context_text),
                "summary": _extract_summary_from_context(context_text, title),
                "content": "",
            }
        )
        if limit is not None and len(items) >= limit:
            break
    return items


def _has_next_page(text: str) -> bool:
    """判断搜狗检索页是否还有下一页。

    功能：通过查找搜狗分页标记（``id="sogou_next"`` 或 ``page_next``）判断能否继续翻页，控制检索循环何时停止。
    谁会调用：``_search_via_httpx``、``_search_via_playwright`` 在每页结束后调用。
    直接调用：
    - 无（纯子串判断，不调用其它函数）。
    输入与结果：输入页面文本；返回布尔值，表示是否还有下一页。
    副作用：无。
    """
    return 'id="sogou_next"' in text or "page_next" in text


def _is_sogou_captcha(text: str) -> bool:
    """识别搜狗反爬验证码页。

    功能：通过中文「验证码/请依次点击」或英文 ``antispider/verify`` 等特征判断当前页面是不是搜狗的反爬验证页，从而决定停止检索并回报 captcha_required。
    谁会调用：``_search_via_httpx``、``_search_via_playwright`` 在每页抓取后调用。
    直接调用：
    - 无（纯子串/关键词判断，不调用其它函数）。
    输入与结果：输入页面文本；返回布尔值，表示是否命中验证码页。
    副作用：无。
    """
    return bool(
        ("验证码" in text and ("请依次点击" in text or "VerifyCode" in text))
        or ("antispider" in text.lower() and "verify" in text.lower())
    )


def _strip_tags(value: str) -> str:
    """去除 HTML 标签并还原实体、压缩空白。

    功能：把 HTML 文本里的标签删掉，再用 ``html.unescape`` 还原转义字符，并把多余空白合并成单空格，得到干净纯文本。
    谁会调用：``_strip_tags_limited``、``_extract_articles_from_html``、``_extract_articles_from_dom``、``_extract_title_text`` 在需要纯文本时调用。
    直接调用：
    - 无（仅 re.sub 与 html.unescape 标准库，不调用其它项目函数）。
    输入与结果：输入含 HTML 标签的文本；返回去除标签并压缩空白的纯文本。
    副作用：无。
    """
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(" ".join(value.split()))


def _strip_tags_limited(value: str, *, max_input_chars: int = WECHAT_ARTICLE_CONTENT_SCAN_CHARS) -> str:
    """在长度上限内去除 HTML 标签。

    功能：对可能很长的正文 HTML 先截断到上限长度再去除标签，避免超大正文片段拖慢纯文本转换。
    谁会调用：``wechat_fetch_article_content`` 在抽取 js_content 正文后调用。
    直接调用：
    - _strip_tags(...)：执行实际去标签与实体还原。
    输入与结果：输入 HTML 文本与可选上限；返回去标签后的纯文本。
    副作用：无。
    """
    return _strip_tags(value[:max_input_chars])


def _extract_redirect_url(value: str) -> str | None:
    """从搜狗跳转链接里取出真实 URL。

    功能：解析搜狗 ``/link?url=`` 这类跳转链接的 query 参数，还原经 HTML 实体与百分号编码的真实目标 URL。
    谁会调用：``_extract_articles_from_html`` 在链接不是直接 mp 域名、且含 ``url=`` 参数时调用。
    直接调用：
    - _unescape_url(...)：先还原 HTML 实体转义。
    输入与结果：输入跳转链接字符串；返回解码后的真实 URL，取不到时返回 None。
    副作用：无。
    """
    parsed = urlparse(_unescape_url(value))
    query = parse_qs(parsed.query)
    target = query.get("url", [None])[0]
    return unquote(target) if target else None


def _resolve_sogou_link_in_context(page, link_handle) -> str | None:
    """在浏览器会话内把搜狗 /link 跳转解析成真实微信 URL。

    功能：在 Playwright 会话里点击搜狗的 /link 跳转链接，等待新标签页加载完成后读取其真实 URL，仅当跳到 mp.weixin.qq.com 才返回，从而绕开搜狗跳转拿到可直接抓取的链接。
    谁会调用：``_extract_articles_from_dom`` 在需要把 /link 链接解析成真实 URL 时调用。
    直接调用：
    - Playwright page API（page.context.expect_page / link_handle.click / new_page.url）：点击跳转并读取真实地址。
    输入与结果：输入 Playwright page 与链接元素句柄；返回真实微信文章 URL，失败或不是微信域名时返回 None。
    副作用：触发浏览器点击与一次页面跳转加载（网络）；异常时记日志。
    """
    try:
        with page.context.expect_page(timeout=15000) as new_page_info:
            link_handle.click()
        article_page = new_page_info.value
        article_page.wait_for_load_state("domcontentloaded", timeout=60000)
        article_page.wait_for_timeout(2500)
        final_url = article_page.url
        article_page.close()
        if "mp.weixin.qq.com/" in final_url:
            return final_url
    except Exception as exc:
        logger.warning("Sogou link resolution in page context failed: %s", exc)
    return None


def _extract_datetime_from_text(value: str) -> str | None:
    """从文本里抽取微信文章的发布时间。

    功能：先在嵌入的 JS 时间戳（timeConvert/publish_time 等 10 位秒级时间戳）里找发布时间，找不到再用 ``年-月-日`` 这类中文/数字日期模式兜底，统一转成 UTC ISO 时间。
    谁会调用：``_extract_articles_from_dom``、``_extract_articles_from_html``、``_extract_article_published_at`` 在抽取发布时间时调用。
    直接调用：
    - 无（纯正则与时间戳换算，不调用其它项目函数）。
    输入与结果：输入含时间线索的文本；返回 ISO 格式（Z 结尾）的发布时间字符串，找不到时返回 None。
    副作用：无。
    """
    compact = " ".join(value[:WECHAT_ARTICLE_HEAD_SCAN_CHARS].split())
    for pattern in _EMBEDDED_TS_PATTERNS:
        match = pattern.search(compact)
        if not match:
            continue
        try:
            return datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            return None
    for pattern in _DATE_PATTERNS:
        match = pattern.search(compact)
        if not match:
            continue
        try:
            year = int(match.group("year"))
            month = int(match.group("month"))
            day = int(match.group("day"))
            return datetime(year, month, day, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            return None
    return None


def _extract_summary_from_context(value: str, title: str) -> str:
    """从上下文文本里抽文章摘要。

    功能：把链接周围的正文上下文清理成摘要：先去掉标题本身，再剔除日期串等噪声，截到一定长度，作为检索阶段可用的粗糙摘要。
    谁会调用：``_extract_articles_from_dom``、``_extract_articles_from_html`` 在整理条目摘要时调用。
    直接调用：
    - 无（纯字符串清洗与正则，不调用其它项目函数）。
    输入与结果：输入上下文文本与文章标题；返回去噪后的摘要字符串（最多 240 字符），空则返空串。
    副作用：无。
    """
    compact = " ".join(value.split())
    if not compact:
        return ""
    compact = compact.replace(title, "", 1).strip(" -|·")
    compact = re.sub(r"\b20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\b", "", compact).strip(" -|·")
    return compact[:240]


# ── WeChat MP API (authenticated) ──────────────────────────────────────────

MP_SEARCH_BIZ_URL = "https://mp.weixin.qq.com/cgi-bin/searchbiz"
MP_APPMSG_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg"


def resolve_wechat_auth_profile(auth_ref: str | None) -> dict:
    """解析当前可用的微信公众号登录凭证。

    功能：先按 auth_ref（默认取配置里的 profile 名）从数据库/运行时解析最新的公众号登录态，拿不到再回退到环境变量配置，统一返回含 cookie/token 的凭证字典。
    谁会调用：``wechat_resolve_account``、``wechat_fetch_account_history``、``wechat_fetch_article_content`` 在调用公众号 API 前都要先解析凭证。
    直接调用：
    - get_settings()（app.config）：读取公众号 profile 名等配置。
    - resolve_profile()（app.wechat_auth）：解析实际登录态。
    输入与结果：输入凭证引用名（可为 None）；返回含 status、cookie、token 等的凭证字典。
    副作用：读取配置与外部登录态存储，不写库。
    """
    from app.config import get_settings
    from app.wechat_auth import resolve_profile

    settings = get_settings()
    expected = settings.wechat_mp_profile_name or "wechat_mp_default"
    return resolve_profile(auth_ref or expected)


def _mp_auth_failure_reason(response: httpx.Response, data: dict[str, Any]) -> str | None:
    """判断公众号 API 响应是否表示登录态失效。

    功能：根据返回 JSON 的 base_resp.ret/err_msg，以及响应是否被重定向到登录页，识别 cookie/token 过期等登录失效原因，供上层标记为失效凭证。
    谁会调用：``probe_mp_session``、``wechat_resolve_account``、``wechat_fetch_account_history`` 在收到响应后调用。
    直接调用：
    - 无（仅 urlparse 与字段判断，不调用其它项目函数）。
    输入与结果：输入 httpx 响应对象与解析后的 JSON dict；返回失效原因字符串（如 "WeChat MP login expired"），非失效时返回 None。
    副作用：无。
    """
    base_resp = data.get("base_resp")
    if not isinstance(base_resp, dict):
        base_resp = {}
    ret = base_resp.get("ret")
    message = str(base_resp.get("err_msg") or base_resp.get("msg") or "")
    normalized_message = message.lower()
    obvious_markers = (
        "invalid session",
        "session expired",
        "login expired",
        "重新登录",
        "登录超时",
        "登录已失效",
    )
    # 200040 ("invalid csrf token") is an auth failure too: the stored token no
    # longer matches a live session, and only a fresh login can repair the pair.
    if str(ret) in {"200003", "200004", "200040"} or any(
        marker in normalized_message for marker in obvious_markers
    ):
        return f"WeChat MP login expired (ret={ret})"

    final_path = urlparse(str(response.url)).path.lower()
    content_type = response.headers.get("content-type", "").lower()
    if final_path in {"/", "/cgi-bin/loginpage", "/cgi-bin/bizlogin"} and "json" not in content_type:
        return "WeChat MP request was redirected to the login page"
    return None


def probe_mp_session(*, cookie: str, token: str) -> tuple[str, str | None]:
    """主动探测微信公众号 cookie/token 是否仍然登录有效。

    功能：用图文列表接口（返回 JSON 且含明确 base_resp.ret）探测凭证登录态，返回 valid（有效）/expired（失效）/unknown（无法判定，如超时、限流、微信侧错误）三者之一；unknown 绝不能当成失效，避免瞬时故障误吊销可用凭证。
    谁会调用：上层在需要确认/刷新公众号登录态时调用（如微信分支流程的鉴权检查）。
    直接调用：
    - _mp_auth_failure_reason(...)：把非 JSON 或异常响应归类为登录失效原因。
    - httpx.Client：发起探测请求（网络）。
    输入与结果：输入 cookie 与 token；返回 (状态字符串, 原因或 None) 元组。
    副作用：发起一次公众号 API 网络请求，写日志。
    """
    headers = {**DEFAULT_HEADERS, "Cookie": cookie}
    params = {
        "action": "list_ex",
        "begin": "0",
        "count": "1",
        "type": "9",
        "query": "",
        "token": token,
        "lang": "zh_CN",
        "f": "json",
        "ajax": "1",
    }
    try:
        with httpx.Client(headers=headers, timeout=15, follow_redirects=True) as client:
            response = client.get(MP_APPMSG_URL, params=params)
    except httpx.HTTPError as exc:
        return "unknown", f"无法连接微信公众平台（{type(exc).__name__}），本次未能确认登录态。"
    if response.status_code in {403, 429}:
        return "unknown", f"微信公众平台限流（HTTP {response.status_code}），本次未能确认登录态。"

    try:
        data = response.json()
    except ValueError:
        failure_reason = _mp_auth_failure_reason(response, {})
        if failure_reason:
            return "expired", failure_reason
        return "unknown", "微信公众平台返回了非 JSON 响应，本次未能确认登录态。"

    failure_reason = _mp_auth_failure_reason(response, data)
    if failure_reason:
        return "expired", failure_reason
    base_resp = data.get("base_resp")
    ret = base_resp.get("ret") if isinstance(base_resp, dict) else None
    if ret == 0:
        return "valid", None
    message = str(base_resp.get("err_msg") or "") if isinstance(base_resp, dict) else ""
    # Anything else (frequency control, server-side errors) says nothing about
    # the login state, so the stored verdict is deliberately left untouched.
    return "unknown", f"微信公众平台返回 ret={ret}（{message or '无错误信息'}），本次未能确认登录态。"


def _mp_business_failure(data: dict[str, Any]) -> tuple[str, str] | None:
    """把非鉴权类的公众号业务失败归类。

    功能：识别频率限制（ret=200013 / "freq control"）等并非登录失效的业务错误，避免把被限流返回的空结果误判成「账号真的没发文」，从而让限流静默变成「无文章」。
    谁会调用：``wechat_resolve_account``、``wechat_fetch_account_history`` 在拿到非零 base_resp 时调用。
    直接调用：
    - 无（纯字段判断，不调用其它项目函数）。
    输入与结果：输入公众号 API 返回的 JSON dict；返回 (状态, 原因) 元组，非业务失败时返回 None。
    副作用：无。
    """
    base_resp = data.get("base_resp")
    if not isinstance(base_resp, dict):
        return None
    ret = base_resp.get("ret")
    if ret in (0, None):
        return None
    message = str(base_resp.get("err_msg") or base_resp.get("msg") or "")
    if str(ret) == "200013" or "freq control" in message.lower():
        return "rate_limited", f"微信公众平台触发频率限制（ret={ret}），请稍后再试。"
    return "failed", f"微信公众平台返回 ret={ret}（{message or '无错误信息'}）。"


def _mark_auth_expired(
    auth_ref: str,
    reason: str,
    credential_token: str,
    credential_cookie: str,
) -> dict[str, Any]:
    """把登录失效的公众号凭证标记为过期。

    功能：在确认公众号 cookie/token 失效后，调用外部鉴权模块把对应 profile 标记为过期，避免后续继续用死凭证重试，并返回统一的失效结果字典。
    谁会调用：``wechat_resolve_account``、``wechat_fetch_account_history`` 在探测到登录失效时调用。
    直接调用：
    - mark_profile_expired()（app.wechat_auth）：把凭证置为过期（写外部登录态存储）。
    输入与结果：输入凭证引用名、失效原因、token、cookie；返回 {"status": "auth_invalid", "reason": ...}。
    副作用：标记凭证过期（写入外部登录态存储/数据库），写警告日志。
    """
    from app.wechat_auth import mark_profile_expired

    mark_profile_expired(auth_ref, reason, credential_token, credential_cookie)
    logger.warning("wechat_mp_auth_expired profile=%s reason=%s", auth_ref, reason)
    return {"status": "auth_invalid", "reason": reason}


def wechat_resolve_account(nickname_or_account_id: str, auth_ref: str = "wechat_mp_default") -> dict:
    """通过公众号后台 API 按昵称或账号 ID 检索公众号。

    功能：用已登录的公众号凭证调用 searchbiz 接口检索目标公众号，处理限流/登录失效/频率限制等情况，解析出 fakeid/biz 等后续抓历史需要的标识。
    谁会调用：多源发现图在需要解析某公众号时调用；``wechat_fetch_account_history`` 在缺 fakeid 时也会调用。
    直接调用：
    - resolve_wechat_auth_profile(...)：解析登录凭证。
    - _mp_auth_failure_reason(...)：识别登录失效。
    - _mark_auth_expired(...)：失效时标记凭证过期。
    - _mp_business_failure(...)：识别非鉴权业务失败。
    - httpx.Client：发起检索请求（网络）。
    输入与结果：输入昵称或账号 ID、凭证引用名；返回含 status、nickname、fakeid、__biz 等的字典。
    副作用：发起公众号 API 网络请求，登录失效时写凭证过期标记，写日志。
    """
    auth = resolve_wechat_auth_profile(auth_ref)
    if auth["status"] != "ok":
        return auth
    headers = {**DEFAULT_HEADERS, "Cookie": auth["cookie"]}
    params = {
        "action": "search_biz",
        "token": auth["token"],
        "lang": "zh_CN",
        "f": "json",
        "ajax": "1",
        "random": "0.1",
        "query": nickname_or_account_id,
        "begin": "0",
        "count": "5",
    }
    with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as client:
        response = client.get(MP_SEARCH_BIZ_URL, params=params)
    if response.status_code in {403, 429}:
        return {"status": "rate_limited", "items": []}
    try:
        data = response.json()
    except ValueError:
        reason = _mp_auth_failure_reason(response, {})
        if reason:
            return _mark_auth_expired(
                auth_ref,
                reason,
                str(auth["token"]),
                str(auth["cookie"]),
            )
        return {"status": "failed", "reason": "WeChat MP returned a non-JSON response"}
    failure_reason = _mp_auth_failure_reason(response, data)
    if failure_reason:
        return _mark_auth_expired(
            auth_ref,
            failure_reason,
            str(auth["token"]),
            str(auth["cookie"]),
        )
    business_failure = _mp_business_failure(data)
    if business_failure:
        status, reason = business_failure
        return {"status": status, "reason": reason, "accounts": []}
    accounts = data.get("list") or []
    if not accounts:
        return {"status": "needs_resolver", "accounts": []}
    first = accounts[0]
    return {
        "status": "ok",
        "nickname": first.get("nickname") or nickname_or_account_id,
        "account_id": nickname_or_account_id,
        "fakeid": first.get("fakeid"),
        "__biz": first.get("fakeid"),
        "accounts": accounts,
    }


def wechat_fetch_account_history(
    *,
    nickname: str | None,
    account_id: str | None,
    fakeid: str | None,
    biz: str | None,
    limit: int = 30,
    fetch_content: bool = False,
    auth_ref: str = "wechat_mp_default",
) -> dict:
    """通过公众号后台 API 抓取某公众号的近期文章。

    功能：用已登录凭证按 fakeid 分页调用图文列表接口，把返回的每条图文规范成统一条目；缺 fakeid 时先解析账号，必要时顺带补抓单篇正文，并处理限流/失效/频率限制等。
    谁会调用：多源发现图在微信抓取分支调用本入口拉取账号历史文章。
    直接调用：
    - resolve_wechat_auth_profile(...)：解析登录凭证。
    - wechat_resolve_account(...)：缺 fakeid 时先解析账号。
    - _mp_auth_failure_reason(...)：识别登录失效。
    - _mark_auth_expired(...)：失效时标记凭证过期。
    - _mp_business_failure(...)：识别非鉴权业务失败。
    - _normalize_mp_article(...)：把每条图文规范成统一结构。
    - wechat_fetch_article_content(...)：开启 fetch_content 时补抓单篇正文。
    - httpx.Client：发起列表/正文请求（网络）。
    输入与结果：输入昵称/账号 ID/fakeid/biz/数量上限/是否补正文/凭证引用名；返回含 status 与 items 的字典。
    副作用：发起公众号 API 网络请求，登录失效时写凭证过期标记，写日志。
    """
    auth = resolve_wechat_auth_profile(auth_ref)
    if auth["status"] != "ok":
        return {**auth, "items": []}
    resolved = None
    if not fakeid:
        resolved = wechat_resolve_account(nickname or account_id or "", auth_ref)
        if resolved.get("status") != "ok":
            return {**resolved, "items": []}
        fakeid = resolved.get("fakeid")
        biz = resolved.get("__biz")
        nickname = resolved.get("nickname") or nickname
    headers = {**DEFAULT_HEADERS, "Cookie": auth["cookie"]}
    items: list[dict] = []
    begin = 0
    page_size = min(10, max(1, limit))
    with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as client:
        while len(items) < limit:
            params = {
                "action": "list_ex",
                "begin": str(begin),
                "count": str(page_size),
                "fakeid": fakeid,
                "type": "9",
                "query": "",
                "token": auth["token"],
                "lang": "zh_CN",
                "f": "json",
                "ajax": "1",
            }
            response = client.get(MP_APPMSG_URL, params=params)
            if response.status_code in {403, 429}:
                return {"status": "rate_limited", "items": items}
            try:
                data = response.json()
            except ValueError:
                reason = _mp_auth_failure_reason(response, {})
                if reason:
                    return {
                        **_mark_auth_expired(
                            auth_ref,
                            reason,
                            str(auth["token"]),
                            str(auth["cookie"]),
                        ),
                        "items": items,
                    }
                return {"status": "failed", "reason": "WeChat MP returned a non-JSON response", "items": items}
            failure_reason = _mp_auth_failure_reason(response, data)
            if failure_reason:
                return {
                    **_mark_auth_expired(
                        auth_ref,
                        failure_reason,
                        str(auth["token"]),
                        str(auth["cookie"]),
                    ),
                    "items": items,
                }
            business_failure = _mp_business_failure(data)
            if business_failure:
                status, reason = business_failure
                return {"status": status, "reason": reason, "items": items}
            raw_items = data.get("app_msg_list") or []
            if not raw_items:
                break
            for raw in raw_items:
                items.append(_normalize_mp_article(raw))
                if len(items) >= limit:
                    break
            begin += len(raw_items)
    if fetch_content:
        for item in items:
            content = wechat_fetch_article_content(item["url"], auth_ref=auth_ref)
            if content.get("status") == "ok":
                item["content"] = content.get("content") or ""
    return {
        "status": "ok" if items else "empty",
        "items": items,
        "nickname": nickname,
        "account_id": account_id,
        "fakeid": fakeid,
        "__biz": biz,
        "limit": limit,
        "fetch_content": fetch_content,
    }


def _normalize_mp_article(raw: dict) -> dict:
    """把公众号 API 返回的单条图文规范成统一条目结构。

    功能：清洗标题空白与长度，把 update_time 秒级时间戳转成 UTC ISO 时间，提取 link/digest 等字段，整理成与其它来源一致的条目字典。
    谁会调用：``wechat_fetch_account_history`` 在遍历图文列表时逐条调用。
    直接调用：
    - 无（纯字段映射与时间戳换算，不调用其它项目函数）。
    输入与结果：输入公众号 API 的单条图文 dict；返回含 title/url/published_at/summary/content 的条目字典。
    副作用：无。
    """
    title = " ".join(str(raw.get("title") or "").split())
    if len(title) > 500:
        title = title[:500].rstrip()
    published_at = raw.get("update_time")
    if isinstance(published_at, (int, float)):
        published_at = datetime.fromtimestamp(published_at, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "title": title,
        "url": raw.get("link") or "",
        "published_at": published_at,
        "summary": raw.get("digest") or "",
        "content": "",
    }


def wechat_fetch_article_content(
    url: str,
    auth_ref: str | None = None,
    *,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    progress_context: dict[str, Any] | None = None,
) -> dict:
    """抓取单篇微信文章的正文与元信息。

    功能：下载文章页并解析出标题、描述摘要、发布时间、js_content 正文（去标签截断到上限），用较短超时与有限重定向把单篇耗时压在秒级；过程中通过回调回报请求/响应/完成等进度。被限流或 ≥400 时返回对应状态但不报错。
    谁会调用：``wechat_enrich_articles`` 在逐篇补抓时调用；也可被外部直接调用补抓单篇。
    直接调用：
    - normalize_wechat_article_url(...)：先规范化 URL。
    - resolve_wechat_auth_profile(...)：如需登录态则挂上 cookie。
    - _article_head_slice(...)：截取 head 与前段用于解析元信息。
    - _extract_article_published_at(...)：抽发布时间。
    - _extract_meta_value(...)：抽 og:title/description 等 meta。
    - _extract_title_text(...)：兜底抽 <title>。
    - _extract_js_content_window(...)：定位 js_content 正文容器。
    - _strip_tags_limited(...)：正文去标签并截断。
    - _emit_progress(...)：回报抓取进度。
    - httpx.Client：发起文章页请求（网络）。
    输入与结果：输入文章 URL、凭证引用名、进度回调与上下文；返回含 status、content、title、summary、published_at、resolved_url 的字典。
    副作用：发起文章页网络请求，写进度回调与日志。
    """
    url = normalize_wechat_article_url(url)
    context = dict(progress_context or {})
    started_at = time.monotonic()
    headers = dict(DEFAULT_HEADERS)
    auth = resolve_wechat_auth_profile(auth_ref) if auth_ref else {"status": "pending_auth"}
    if auth.get("status") == "ok":
        headers["Cookie"] = auth["cookie"]
    # 收紧超时并限制重定向跳数：src=11/src=3 跳转链每跳各自超时会把单篇累加到数十秒，
    # 用较短的 connect/read 超时 + 最多 2 跳重定向，把单篇上限压到 ~5s 级别。
    timeout = httpx.Timeout(5.0, connect=3.0)
    with httpx.Client(
        headers=headers, timeout=timeout, follow_redirects=True, max_redirects=2
    ) as client:
        _emit_progress(
            progress_callback,
            "wechat_article_fetch_request_started",
            **context,
            url=url,
        )
        response = client.get(url)
    response_duration_ms = int((time.monotonic() - started_at) * 1000)
    body_text = response.text
    _emit_progress(
        progress_callback,
        "wechat_article_fetch_response_received",
        **context,
        url=url,
        final_url=str(response.url),
        status_code=response.status_code,
        duration_ms=response_duration_ms,
        body_chars=len(body_text),
    )
    if response.status_code in {403, 429}:
        _emit_progress(
            progress_callback,
            "wechat_article_fetch_finished",
            **context,
            url=url,
            final_url=str(response.url),
            status="rate_limited",
            duration_ms=int((time.monotonic() - started_at) * 1000),
            body_chars=len(body_text),
        )
        return {"status": "rate_limited", "content": ""}
    if response.status_code >= 400:
        _emit_progress(
            progress_callback,
            "wechat_article_fetch_finished",
            **context,
            url=url,
            final_url=str(response.url),
            status="unsupported",
            status_code=response.status_code,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            body_chars=len(body_text),
        )
        return {"status": "unsupported", "content": ""}
    parse_started_at = time.monotonic()
    head_text = _article_head_slice(body_text)
    published_at = _extract_article_published_at(head_text)
    published_at_ms = int((time.monotonic() - parse_started_at) * 1000)
    meta_started_at = time.monotonic()
    title = (
        _extract_meta_value(head_text, "property", "og:title")
        or _extract_meta_value(head_text, "name", "twitter:title")
        or _extract_title_text(head_text)
        or str(context.get("title") or "")
    )
    summary = (
        _extract_meta_value(head_text, "property", "og:description")
        or _extract_meta_value(head_text, "name", "description")
        or _extract_meta_value(head_text, "name", "twitter:description")
        or ""
    )
    meta_ms = int((time.monotonic() - meta_started_at) * 1000)
    js_started_at = time.monotonic()
    content_html = _extract_js_content_window(body_text)
    js_content_ms = int((time.monotonic() - js_started_at) * 1000)
    if not content_html:
        _emit_progress(
            progress_callback,
            "wechat_article_fetch_finished",
            **context,
            url=url,
            final_url=str(response.url),
            status="empty",
            duration_ms=int((time.monotonic() - started_at) * 1000),
            body_chars=len(body_text),
            content_chars=0,
            published_at_ms=published_at_ms,
            meta_ms=meta_ms,
            js_content_ms=js_content_ms,
            strip_ms=0,
        )
        return {
            "status": "empty",
            "content": "",
            "title": title,
            "summary": summary,
            "published_at": published_at,
            "resolved_url": str(response.url),
        }
    strip_started_at = time.monotonic()
    content = _strip_tags_limited(content_html)[:WECHAT_ARTICLE_CONTENT_CHAR_LIMIT]
    strip_ms = int((time.monotonic() - strip_started_at) * 1000)
    _emit_progress(
        progress_callback,
        "wechat_article_fetch_finished",
        **context,
        url=url,
        final_url=str(response.url),
        status="ok",
        duration_ms=int((time.monotonic() - started_at) * 1000),
        body_chars=len(body_text),
        content_chars=len(content),
        published_at_ms=published_at_ms,
        meta_ms=meta_ms,
        js_content_ms=js_content_ms,
        strip_ms=strip_ms,
    )
    return {
        "status": "ok",
        "content": content,
        "title": title,
        "summary": summary,
        "published_at": published_at,
        "resolved_url": str(response.url),
    }


WECHAT_ENRICH_MAX_WORKERS = 1

WECHAT_PREFETCH_PROMPT = """你是 OS 技术情报采集系统的微信文章补抓前置判断器。

只根据标题、摘要和 URL 判断是否值得继续补抓正文。不要假设正文内容。

应该补抓正文的情况：
- 明显是操作系统、Linux 内核、发行版、编译器、工具链、RISC-V、GCC、LLVM、性能、CXL、AI Agent、调优、云原生基础设施、安全维护、版本发布、兼容性变化等技术内容。

不应该补抓正文的情况：
- 活动通知、会议预告、报名链接、Meetup、峰会、大会议程、运营报告、社区月报/周报、宣传材料、招聘、纯营销内容。

无法判断时返回 should_fetch=false，让后续 Enricher 使用标题/摘要保守拒收。

输出严格 JSON，不要多余文字：
- should_fetch: 布尔值
- reason: 从 technical_article、activity_notice、conference、registration、operation_report、marketing、insufficient_info、other 中选一个

示例：
{{"should_fetch": false, "reason": "registration"}}

标题：{title}
摘要：{summary}
URL：{url}
"""


def wechat_article_key(url: str) -> str | None:
    """计算微信文章去重用的唯一键。

    功能：从规范化后的微信文章 URL 里解析 ``__biz``、``mid``、``idx``、``sn`` 四个参数，拼成 ``biz:mid:idx:sn`` 形式的稳定键，用于同一篇文章的跨批次去重。
    谁会调用：``wechat_enrich_articles`` 在抓取前/去重与跳过判断时调用。
    直接调用：
    - normalize_wechat_article_url(...)：先规范化 URL。
    输入与结果：输入文章 URL；返回去重键字符串，缺 biz/mid/idx 时返回 None。
    副作用：无。
    """
    parsed = urlparse(normalize_wechat_article_url(url))
    query = parse_qs(parsed.query)
    biz = (query.get("__biz") or [None])[0]
    mid = (query.get("mid") or [None])[0]
    idx = (query.get("idx") or [None])[0]
    sn = (query.get("sn") or [""])[0] or ""
    if not (biz and mid and idx):
        return None
    return f"{biz}:{mid}:{idx}:{sn}"


def _extract_json_object(text: str) -> dict:
    """从 LLM 输出里提取第一个 JSON 对象。

    功能：优先从 ```json 代码块里取 JSON，否则退而从整段文本里匹配第一个 ``{...}``，供解析 LLM 返回的结构化结论；都找不到则报错。
    谁会调用：``_should_fetch_wechat_article_content`` 在拿到 LLM 原始输出后调用。
    直接调用：
    - 无（纯正则与 json.loads，不调用其它项目函数）。
    输入与结果：输入 LLM 文本；返回解析出的 JSON dict，找不到抛 ValueError。
    副作用：无。
    """
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        return json.loads(fenced.group(1))
    brace = re.search(r"\{.*\}", text, re.S)
    if brace:
        return json.loads(brace.group(0))
    raise ValueError(f"No JSON object found in LLM output: {text[:200]}")


def _should_fetch_wechat_article_content(item: dict, llm: Any) -> tuple[bool, str]:
    """仅用标题/摘要判断某篇微信文章是否值得补抓正文。

    功能：在不下载正文的前提下，用 LLM 根据标题/摘要/URL 判定是否为技术类文章（应补抓）还是活动/会议/营销等（不补抓），避免无谓地抓取整篇正文。
    谁会调用：``wechat_enrich_articles`` 在开启 LLM 主题预筛时，对每篇待抓条目调用。
    直接调用：
    - render_prompt()（app.discovery.prompts）：渲染预筛 prompt。
    - llm.complete(...)：调用 LLM 判主题（网络）。
    - _extract_json_object(...)：解析 LLM 返回的 JSON 结论。
    输入与结果：输入条目 dict 与 LLM 客户端；返回 (是否应补抓, 原因) 元组。
    副作用：发起 LLM 调用（网络）。
    """
    from app.discovery.prompts import render_prompt

    prompt = render_prompt(
        "wechat_prefetch",
        WECHAT_PREFETCH_PROMPT,
        {
            "{title}": item.get("title") or "",
            "{summary}": item.get("summary") or "",
            "{url}": item.get("url") or "",
        },
    )
    raw = llm.complete(
        prompt,
        temperature=0.0,
        response_format={"type": "json_object"},
        timeout=WECHAT_TOPIC_PRECHECK_TIMEOUT_SECONDS,
    )
    data = _extract_json_object(raw)
    return bool(data.get("should_fetch")), str(data.get("reason") or "other")


def wechat_enrich_articles(
    items: list[dict],
    *,
    fetch_content: bool = True,
    fill_missing_only: bool = True,
    max_items: int | None = None,
    skip_url_keys: list[str] | None = None,
    precheck_topic_with_llm: bool = False,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    max_workers: int = WECHAT_ENRICH_MAX_WORKERS,
) -> dict:
    """批量补抓微信文章正文。

    功能：对一批微信文章条目先按 URL/文章键去重，选出需要补抓（缺时间/摘要/正文）的条目，再用有界线程池并发抓取并回填标题/摘要/正文/发布时间；支持 LLM 主题预筛跳过非技术内容，并通过回调回报整体与每篇进度。
    谁会调用：多源发现图在微信抓取分支调用本入口做文章富化。直接调用：
    - normalize_wechat_article_url(...)：规范化待抓 URL。
    - wechat_article_key(...)：计算去重键。
    - _should_fetch_wechat_article_content(...)：LLM 开启时预筛是否值得补抓。
    - _emit_progress(...)：回报整体与单篇进度。
    - wechat_fetch_article_content(...)：实际抓取单篇内容（由线程池内的 _work 调用）。
    - _work（本函数内嵌套）：线程池任务单元，执行单篇抓取与回填。
    输入与结果：输入条目列表、是否补正文、是否只补缺字段、数量上限、跳过键、是否 LLM 预筛、进度回调与并发数；返回含 status、items、enriched_count 等统计的字典。
    副作用：发起文章页网络请求（线程池并发），写进度回调与日志；不改库。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    # 1) 抓取前去重：同一批内相同微信文章 key/url 只保留首个（无 url 的原样保留）
    deduped: list[dict] = []
    seen_urls: set[str] = set()
    seen_keys: set[str] = set()
    for item in items:
        next_item = dict(item)
        url = normalize_wechat_article_url(str(next_item.get("url") or ""))
        if url:
            next_item["url"] = url
        if url:
            key = wechat_article_key(url)
            if (key and key in seen_keys) or url in seen_urls:
                continue
            if key:
                seen_keys.add(key)
            seen_urls.add(url)
        deduped.append(next_item)

    # 2) 选出需要抓取的条目（受 max_items 限额），按原始顺序取前 N 条
    fetch_indices: list[int] = []
    skip_keys = {str(key) for key in skip_url_keys or [] if key}
    skipped_existing = 0
    skipped_topic = 0
    topic_check_failed = 0
    topic_llm = None
    if precheck_topic_with_llm:
        from app.llm.client import LlmClient

        topic_llm = LlmClient()
    for idx, next_item in enumerate(deduped):
        need_fetch = not fill_missing_only
        if fill_missing_only:
            need_fetch = (
                not next_item.get("published_at")
                or not next_item.get("summary")
                or (fetch_content and not next_item.get("content"))
            )
        if not (need_fetch and next_item.get("url")):
            continue
        key = wechat_article_key(str(next_item.get("url") or ""))
        if key and key in skip_keys:
            skipped_existing += 1
            continue
        if precheck_topic_with_llm:
            try:
                should_fetch, topic_reason = _should_fetch_wechat_article_content(next_item, topic_llm)
            except Exception:  # noqa: BLE001 - 预筛失败时保守不补正文，交给 Enricher 用摘要拒收
                topic_check_failed += 1
                skipped_topic += 1
                continue
            if not should_fetch:
                skipped_topic += 1
                _emit_progress(
                    progress_callback,
                    "wechat_enrich_topic_skipped",
                    title=next_item.get("title"),
                    url=next_item.get("url"),
                    reason=topic_reason,
                )
                continue
        if max_items is not None and len(fetch_indices) >= max_items:
            continue
        fetch_indices.append(idx)

    _emit_progress(
        progress_callback,
        "wechat_enrich_started",
        total_items=len(deduped),
        selected_items=len(fetch_indices),
        skipped_existing=skipped_existing,
        skipped_topic=skipped_topic,
        topic_check_failed=topic_check_failed,
        fetch_content=fetch_content,
        fill_missing_only=fill_missing_only,
        max_items=max_items,
        precheck_topic_with_llm=precheck_topic_with_llm,
    )

    statuses: set[str] = set()
    lock = threading.Lock()
    counters = {"attempted": 0, "enriched": 0}

    def _work(idx: int) -> None:
        """线程池任务：抓取并回填单篇微信文章。

        功能：在线程池里处理第 idx 篇待抓条目——调 ``wechat_fetch_article_content`` 抓正文，把返回的时间/摘要/正文/标题回填到该条目（仅在原字段缺失时覆盖），并回报开始/完成进度。
        谁会调用：``wechat_enrich_articles`` 用 ThreadPoolExecutor 把各待抓下标交给本函数并发执行。
        直接调用：
        - wechat_fetch_article_content(...)：抓取单篇正文与元信息。
        - _emit_progress(...)：回报单篇开始/完成/失败进度。
        输入与结果：输入待抓条目在原去重列表中的下标；无返回值，结果直接写回该条目字典。
        副作用：发起单篇文章页网络请求（线程池内），写进度回调与日志；失败时不中断其它任务。
        """
        next_item = deduped[idx]  # 每个 idx 仅由一个任务处理，字段写入无需加锁
        with lock:
            counters["attempted"] += 1
            my_attempt = counters["attempted"]
        _emit_progress(
            progress_callback,
            "wechat_enrich_item_started",
            attempted_count=my_attempt,
            max_items=max_items,
            title=next_item.get("title"),
            url=next_item.get("url"),
        )
        try:
            article = wechat_fetch_article_content(
                str(next_item["url"]),
                progress_callback=progress_callback,
                progress_context={
                    "attempted_count": my_attempt,
                    "max_items": max_items,
                    "title": next_item.get("title"),
                },
            )
        except Exception as exc:  # noqa: BLE001 - 单篇抓取失败不影响其他并发任务
            _emit_progress(
                progress_callback,
                "wechat_article_fetch_failed",
                attempted_count=my_attempt,
                max_items=max_items,
                title=next_item.get("title"),
                url=next_item.get("url"),
                error=str(exc)[:300],
            )
            article = {"status": "error", "content": ""}
        st = article.get("status", "unknown")
        did_enrich = False
        if st in {"ok", "empty"}:
            if article.get("resolved_url"):
                next_item["url"] = article["resolved_url"]
            if article.get("published_at") and not next_item.get("published_at"):
                next_item["published_at"] = article["published_at"]
            if article.get("summary") and not next_item.get("summary"):
                next_item["summary"] = article["summary"]
            if fetch_content and article.get("content") and not next_item.get("content"):
                next_item["content"] = article["content"]
            if article.get("title") and not next_item.get("title"):
                next_item["title"] = article["title"]
            did_enrich = True
        with lock:
            statuses.add(st)
            if did_enrich:
                counters["enriched"] += 1
            cur_enriched = counters["enriched"]
        _emit_progress(
            progress_callback,
            "wechat_enrich_item_finished",
            attempted_count=my_attempt,
            enriched_count=cur_enriched,
            title=next_item.get("title"),
            url=next_item.get("url"),
            status=st,
        )

    if fetch_indices:
        workers = max(1, min(max_workers, len(fetch_indices)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(_work, fetch_indices))

    attempted_count = counters["attempted"]
    enriched_count = counters["enriched"]
    status = "ok"
    if statuses and statuses <= {"rate_limited"}:
        status = "rate_limited"
    elif statuses and statuses <= {"unsupported", "empty", "error"} and enriched_count == 0:
        status = "empty"
    _emit_progress(
        progress_callback,
        "wechat_enrich_finished",
        status=status,
        attempted_count=attempted_count,
        enriched_count=enriched_count,
        total_items=len(deduped),
        selected_items=len(fetch_indices),
        skipped_existing=skipped_existing,
        skipped_topic=skipped_topic,
        topic_check_failed=topic_check_failed,
        fetch_content=fetch_content,
    )
    return {
        "status": status,
        "items": deduped,
        "enriched_count": enriched_count,
        "attempted_count": attempted_count,
        "selected_count": len(fetch_indices),
        "skipped_existing": skipped_existing,
        "skipped_topic": skipped_topic,
        "topic_check_failed": topic_check_failed,
        "fetch_content": fetch_content,
    }


def _extract_meta_value(text: str, attr_name: str, attr_value: str) -> str:
    """从 HTML 里按属性抽取某个 meta 标签的 content 值。

    功能：用正则兼容 ``<meta name="..." content="...">`` 与属性顺序相反两种写法，抽指定 meta（如 og:title、description）的 content，并还原 HTML 实体。
    谁会调用：``wechat_fetch_article_content`` 在抽取标题/描述等 meta 时调用。直接调用：
    - 无（纯正则与 html.unescape，不调用其它项目函数）。
    输入与结果：输入 HTML 文本、目标 meta 的属性名与属性值；返回 content 字符串，找不到时返回空串。
    副作用：无。
    """
    pattern = re.compile(
        rf'<meta[^>]+{attr_name}=["\']{re.escape(attr_value)}["\'][^>]+content=["\'](.*?)["\']',
        re.I | re.S,
    )
    match = pattern.search(text)
    if match:
        return html.unescape(match.group(1)).strip()
    reverse_pattern = re.compile(
        rf'<meta[^>]+content=["\'](.*?)["\'][^>]+{attr_name}=["\']{re.escape(attr_value)}["\']',
        re.I | re.S,
    )
    reverse_match = reverse_pattern.search(text)
    if reverse_match:
        return html.unescape(reverse_match.group(1)).strip()
    return ""


def _extract_title_text(text: str) -> str:
    """从 HTML 的 <title> 标签抽取标题文本。

    功能：用正则匹配 ``<title>...</title>``，去标签后得到文章标题，作为 meta 标题缺失时的兜底来源。
    谁会调用：``wechat_fetch_article_content`` 在 og:title/twitter:title 都拿不到时调用。直接调用：
    - _strip_tags(...)：去除标题里的标签。
    输入与结果：输入 HTML 文本；返回标题纯文本，找不到时返回空串。
    副作用：无。
    """
    match = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
    if not match:
        return ""
    return _strip_tags(match.group(1))


def _extract_article_published_at(text: str) -> str | None:
    """从文章页文本里抽取发布时间。

    功能：对微信文章页的 head/前段文本复用通用时间抽取逻辑，得到文章发布时间。
    谁会调用：``wechat_fetch_article_content`` 在解析单篇发布时间时调用。
    直接调用：
    - _extract_datetime_from_text(...)：执行实际的时间戳/日期抽取。
    输入与结果：输入文章页文本；返回 ISO 发布时间字符串，找不到时返回 None。
    副作用：无。
    """
    return _extract_datetime_from_text(text)
