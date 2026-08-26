"""Anonymous public WeChat article connector backed by Sogou Weixin search."""

from __future__ import annotations

import html as html_module
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus, urljoin

import httpx
from lxml import etree, html


SEARCH_ENDPOINT = "https://weixin.sogou.com/weixin"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
SECRET_KEYS = {"authorization", "cookie", "cookies", "token", "access_token", "auth_ref", "headers"}


def _event(name: str, **payload: Any) -> None:
    print(json.dumps({"event": name, **payload}, ensure_ascii=False), file=sys.stderr, flush=True)


def _public_config(raw: dict[str, Any]) -> dict[str, Any]:
    for key in raw:
        lowered = str(key).lower()
        if lowered in SECRET_KEYS or any(part in lowered for part in ("cookie", "token", "secret", "password")):
            raise ValueError(f"private configuration is forbidden: {key}")
    account_name = str(raw.get("account_name") or "").strip()
    keywords_raw = raw.get("keywords") or []
    if isinstance(keywords_raw, str):
        keywords_raw = [keywords_raw]
    keywords = [str(value).strip() for value in keywords_raw if str(value).strip()][:8]
    if not account_name and not keywords:
        raise ValueError("account_name or keywords is required")
    return {
        "account_name": account_name[:120],
        "keywords": keywords,
        "max_pages": min(10, max(1, int(raw.get("max_pages") or 6))),
        "limit": min(120, max(1, int(raw.get("limit") or 120))),
        "page": min(100, max(1, int(raw.get("page") or 1))),
    }


def _queries(config: dict[str, Any]) -> list[str]:
    account = config["account_name"]
    keywords = config["keywords"]
    if not keywords:
        return [account]
    if not account:
        return keywords
    return [f"{account} {keyword}" for keyword in keywords]


def _clean_text(value: str) -> str:
    return " ".join(html_module.unescape(value or "").split())


def _first_text(node: Any, expressions: tuple[str, ...]) -> str:
    for expression in expressions:
        values = node.xpath(expression)
        if values:
            value = values[0]
            if hasattr(value, "text_content"):
                value = value.text_content()
            cleaned = _clean_text(str(value))
            if cleaned:
                return cleaned
    return ""


def _extract_search_results(page_html: str, account_name: str) -> list[dict[str, str]]:
    try:
        document = html.fromstring(page_html)
    except (etree.ParserError, etree.XMLSyntaxError):
        return []
    nodes = document.xpath("//ul[contains(@class,'news-list')]/li | //div[contains(@class,'news-box')]//li")
    results: list[dict[str, str]] = []
    for node in nodes:
        publisher = _first_text(node, (
            ".//*[contains(concat(' ', normalize-space(@class), ' '), ' all-time-y2 ')]/text()",
            ".//*[contains(@class,'account')]/text()",
            ".//*[contains(@class,'s-p')]//text()",
        ))
        if account_name and account_name.casefold() not in publisher.casefold():
            continue
        links = node.xpath(
            ".//h3/a/@href | "
            ".//a[contains(@data-z, 'art') or contains(@uigs, 'article')]/@href"
        )
        if not links:
            continue
        title = _first_text(node, (".//h3/a", ".//h4/a", ".//a[contains(@uigs,'article')]") )
        if not title:
            continue
        timestamp = _first_text(node, (
            ".//*[contains(@class,'s-p')]//script/text()",
            ".//*[contains(@class,'s-p')]/@t",
        ))
        timestamp_match = re.search(r"(?<!\d)(\d{10})(?!\d)", timestamp)
        if not timestamp_match:
            continue
        published_at = datetime.fromtimestamp(
            int(timestamp_match.group(1)), tz=timezone.utc
        ).isoformat()
        summary = _first_text(node, (".//*[contains(@class,'txt-info')]", ".//p"))
        results.append({
            "title": title,
            "url": urljoin(SEARCH_ENDPOINT, str(links[0])),
            "published_at": published_at,
            "summary": summary or title,
            "publisher": publisher,
        })
    return results


def _blocked_status(response: httpx.Response) -> str | None:
    body = response.text.lower()
    if (
        "请输入验证码" in response.text
        or "完成验证" in response.text
        or "wappoc_appmsgcaptcha" in body
        or "captcha_required" in body
    ):
        return "captcha_required"
    if (
        response.status_code in {403, 429}
        or "访问过于频繁" in response.text
        or "您的访问出错了" in response.text
        or "异常访问请求" in response.text
    ):
        return "rate_limited"
    return None


async def crawl(request: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Return public Sogou Weixin search summaries without opening article pages."""
    del context
    config = _public_config(dict(request.get("config") or {}))
    target = min(config["limit"], int(request.get("target_count") or config["limit"]))
    candidates: list[dict[str, str]] = []
    seen_candidates: set[str] = set()
    status = "ok"
    timeout = httpx.Timeout(15.0, connect=8.0)

    async def strip_cookie(outgoing: httpx.Request) -> None:
        outgoing.headers.pop("cookie", None)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
        event_hooks={"request": [strip_cookie]},
    ) as client:
        for query in _queries(config):
            for page in range(config["page"], config["page"] + config["max_pages"]):
                client.cookies.clear()
                response = await client.get(f"{SEARCH_ENDPOINT}?type=2&query={quote_plus(query)}&page={page}")
                blocked = _blocked_status(response)
                if blocked:
                    status = blocked
                    break
                page_results = _extract_search_results(response.text, config["account_name"])
                _event("wechat_search_page", page=page, query=query, discovered=len(page_results))
                for item in page_results:
                    if item["url"] not in seen_candidates:
                        seen_candidates.add(item["url"])
                        candidates.append(item)
                if len(candidates) >= target or not page_results:
                    break
            if len(candidates) >= target or status != "ok":
                break
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for candidate in candidates[:target]:
        if candidate["url"] in seen_urls:
            continue
        seen_urls.add(candidate["url"])
        items.append({
            "title": candidate["title"],
            "url": candidate["url"],
            "published_at": candidate["published_at"],
            "summary": candidate["summary"][:2000],
            "content": None,
            "author": candidate.get("publisher") or None,
        })
    return {
        "items": items,
        "stats": {
            "discovered_count": len(items),
            "search_candidate_count": len(candidates),
            "status": status,
            "anonymous": True,
            "summary_only": True,
        },
    }
