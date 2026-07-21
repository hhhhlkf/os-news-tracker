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
    content = _compact_text(item.get("content") or "")
    if len(content) >= min_existing_chars:
        return False
    summary = _compact_text(_strip_markup(str(item.get("summary") or "")))
    if len(summary) >= min_existing_chars and not _looks_like_aggregator_summary(summary):
        return False
    return True


def should_enrich_article_pages_for_exploration(exploration: dict[str, Any]) -> bool:
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
    values = tree.xpath(
        f"//meta[translate(@{attr_name}, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
        f"'{attr_value.lower()}']/@content"
    )
    return str(values[0]).strip() if values else None


def _first_attr(tree: Any, xpath: str, attr_name: str) -> str | None:
    values = tree.xpath(f"{xpath}/@{attr_name}")
    return str(values[0]).strip() if values else None


def _sample_text(sample: dict[str, Any], *keys: str) -> str:
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
    lowered = text.lower()
    return any(pattern in lowered for pattern in AGGREGATOR_SUMMARY_PATTERNS)


def _strip_markup(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", html.unescape(value or ""))


def _compact_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _emit_progress(
    callback: Callable[[str, dict[str, Any]], None] | None,
    event: str,
    **payload: Any,
) -> None:
    if callback is None:
        return
    callback(event, payload)
