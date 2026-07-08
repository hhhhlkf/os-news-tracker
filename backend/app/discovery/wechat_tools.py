from __future__ import annotations

import html
import logging
import re
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


def wechat_search_articles(
    query: str,
    limit: int | None = None,
    max_pages: int | None = DEFAULT_SEARCH_MAX_PAGES,
    resolve_final_urls_limit: int | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict:
    """Search WeChat articles via Sogou WeChat search.

    Default to Playwright so multi discovery is less likely to be blocked
    by Sogou's anti-bot verification on plain HTTP requests.
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
    """Use Playwright to render Sogou WeChat search page and extract article links."""
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
    if callback is None:
        return
    callback(event, payload)


def _extract_articles_from_dom(page, limit: int | None, resolve_final_urls_limit: int | None = None) -> list[dict]:
    """Extract article links and titles using Playwright DOM selectors.

    Sogou renders results as `.txt-box > h3 > a[href^="/link?url="]`.
    We resolve the obfuscated /link redirect within the Playwright session
    to get the real mp.weixin.qq.com URL.
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
    """Extract article links from HTML using regex (fallback)."""
    items: list[dict] = []
    for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S):
        href = html.unescape(match.group(1))
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
    return 'id="sogou_next"' in text or "page_next" in text


def _is_sogou_captcha(text: str) -> bool:
    """Detect Sogou anti-bot verification page."""
    return bool(
        ("验证码" in text and ("请依次点击" in text or "VerifyCode" in text))
        or ("antispider" in text.lower() and "verify" in text.lower())
    )


def _strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(" ".join(value.split()))


def _extract_redirect_url(value: str) -> str | None:
    parsed = urlparse(html.unescape(value))
    query = parse_qs(parsed.query)
    target = query.get("url", [None])[0]
    return unquote(target) if target else None


def _resolve_sogou_link_in_context(page, link_handle) -> str | None:
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
    compact = " ".join(value.split())
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
    """Resolve a named WeChat MP auth profile from env/config."""
    from app.config import get_settings

    settings = get_settings()
    expected = settings.wechat_mp_profile_name or "wechat_mp_default"
    if auth_ref not in {None, expected}:
        return {"status": "auth_invalid", "reason": "unknown auth_ref"}
    if not settings.wechat_mp_cookie or not settings.wechat_mp_token:
        return {"status": "pending_auth", "reason": "WECHAT_MP_COOKIE or WECHAT_MP_TOKEN is not configured"}
    return {
        "status": "ok",
        "auth_ref": expected,
        "cookie": settings.wechat_mp_cookie,
        "token": settings.wechat_mp_token,
    }


def wechat_resolve_account(nickname_or_account_id: str, auth_ref: str = "wechat_mp_default") -> dict:
    """Search for a WeChat official account by nickname or account ID via MP API."""
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
    data = response.json()
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
    """Fetch recent articles from a WeChat official account via MP API."""
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
            data = response.json()
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


def wechat_fetch_article_content(url: str, auth_ref: str | None = None) -> dict:
    """Fetch full article content from a WeChat MP article URL."""
    headers = dict(DEFAULT_HEADERS)
    auth = resolve_wechat_auth_profile(auth_ref) if auth_ref else {"status": "pending_auth"}
    if auth.get("status") == "ok":
        headers["Cookie"] = auth["cookie"]
    # 收紧超时并限制重定向跳数：src=11/src=3 跳转链每跳各自超时会把单篇累加到数十秒，
    # 用较短的 connect/read 超时 + 最多 3 跳重定向，把单篇上限压到 ~10s 级别。
    timeout = httpx.Timeout(10.0, connect=5.0)
    with httpx.Client(
        headers=headers, timeout=timeout, follow_redirects=True, max_redirects=3
    ) as client:
        response = client.get(url)
    if response.status_code in {403, 429}:
        return {"status": "rate_limited", "content": ""}
    if response.status_code >= 400:
        return {"status": "unsupported", "content": ""}
    published_at = _extract_article_published_at(response.text)
    title = (
        _extract_meta_value(response.text, "property", "og:title")
        or _extract_meta_value(response.text, "name", "twitter:title")
        or _extract_title_text(response.text)
        or ""
    )
    summary = (
        _extract_meta_value(response.text, "property", "og:description")
        or _extract_meta_value(response.text, "name", "description")
        or _extract_meta_value(response.text, "name", "twitter:description")
        or ""
    )
    match = re.search(r'<div[^>]+id="js_content"[^>]*>(.*?)</div>', response.text, re.S)
    if not match:
        return {
            "status": "empty",
            "content": "",
            "title": title,
            "summary": summary,
            "published_at": published_at,
            "resolved_url": str(response.url),
        }
    return {
        "status": "ok",
        "content": _strip_tags(match.group(1)),
        "title": title,
        "summary": summary,
        "published_at": published_at,
        "resolved_url": str(response.url),
    }


WECHAT_ENRICH_MAX_WORKERS = 6


def wechat_enrich_articles(
    items: list[dict],
    *,
    fetch_content: bool = True,
    fill_missing_only: bool = True,
    max_items: int | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    max_workers: int = WECHAT_ENRICH_MAX_WORKERS,
) -> dict:
    """逐篇补抓微信文章正文。有界并发（默认 6）抓取，避免串行长等待；
    抓取前先按 url 去重，避免同一批重复抓取同一链接。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    # 1) 抓取前去重：同一批内相同 url 只保留首个（无 url 的原样保留）
    deduped: list[dict] = []
    seen_urls: set[str] = set()
    for item in items:
        url = str(item.get("url") or "")
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
        deduped.append(dict(item))

    _emit_progress(
        progress_callback,
        "wechat_enrich_started",
        total_items=len(deduped),
        fetch_content=fetch_content,
        fill_missing_only=fill_missing_only,
        max_items=max_items,
    )

    # 2) 选出需要抓取的条目（受 max_items 限额），按原始顺序取前 N 条
    fetch_indices: list[int] = []
    for idx, next_item in enumerate(deduped):
        need_fetch = not fill_missing_only
        if fill_missing_only:
            need_fetch = (
                not next_item.get("published_at")
                or not next_item.get("summary")
                or (fetch_content and not next_item.get("content"))
            )
        if need_fetch and next_item.get("url") and (max_items is None or len(fetch_indices) < max_items):
            fetch_indices.append(idx)

    statuses: set[str] = set()
    lock = threading.Lock()
    counters = {"attempted": 0, "enriched": 0}

    def _work(idx: int) -> None:
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
            article = wechat_fetch_article_content(str(next_item["url"]))
        except Exception:  # noqa: BLE001 - 单篇抓取失败不影响其他并发任务
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
        fetch_content=fetch_content,
    )
    return {
        "status": status,
        "items": deduped,
        "enriched_count": enriched_count,
        "attempted_count": attempted_count,
        "fetch_content": fetch_content,
    }


def _extract_meta_value(text: str, attr_name: str, attr_value: str) -> str:
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
    match = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
    if not match:
        return ""
    return _strip_tags(match.group(1))


def _extract_article_published_at(text: str) -> str | None:
    return _extract_datetime_from_text(text)
