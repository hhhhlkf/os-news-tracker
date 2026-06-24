"""XHR/Fetch 探测器：用 Playwright 加载网页，拦截 JSON API 响应，确定性打分。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, parse_qs

_BLOCKLIST_PATTERNS = re.compile(
    r"/analytics|/tracking|/telemetry|/beacon|/sentry|"
    r"/log/|/logs/|/metric|/statistic|/monitor|"
    r"/health|/ping|/heartbeat|/cart|/wishlist|"
    r"/searchsuggest|/autocomplete|/taglist|"
    r"/config|/setting|/i18n|/locale|"
    r"/csrf|/captcha|/verify",
    re.IGNORECASE,
)

_LIST_KEYS = ("items", "results", "data", "entries", "articles", "posts", "records", "list", "rows")
_TITLE_KEYS = ("title", "headline", "name", "subject", "subject_name")
_URL_KEYS = ("url", "link", "html_url", "href", "permalink", "path", "detail_url")
_DATE_KEYS = ("published", "published_at", "created_at", "date", "updated", "pubDate", "publish_time", "create_time")
_CONTENT_KEYS = ("summary", "description", "content", "body", "abstract", "text")


@dataclass
class XhrCandidate:
    method: str
    url: str
    status_code: int
    content_type: str
    request_headers: dict[str, str]
    query: dict[str, str]
    json_body: dict | None
    response_sample: Any
    inferred_items_path: str
    inferred_fields: dict[str, str | None]
    score: int
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "url": self.url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "request_headers": self.request_headers,
            "query": self.query,
            "json_body": self.json_body,
            "response_sample": _truncate(self.response_sample, 2000),
            "inferred_items_path": self.inferred_items_path,
            "inferred_fields": self.inferred_fields,
            "score": self.score,
            "notes": self.notes,
        }


def detect_xhr_apis(page_url: str, *, timeout_ms: int = 15000) -> list[dict]:
    """加载 *page_url*，拦截 XHR/Fetch JSON 响应，返回打分后的候选列表。"""
    from playwright.sync_api import sync_playwright

    candidates: list[XhrCandidate] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        def on_response(response):
            try:
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type.lower():
                    return
                body_text = response.text()
                try:
                    payload = json.loads(body_text)
                except (json.JSONDecodeError, ValueError):
                    return
                request = response.request
                candidate = _build_candidate(
                    method=request.method,
                    url=response.url,
                    status_code=response.status,
                    content_type=content_type,
                    request_headers=dict(request.headers),
                    post_data=request.post_data,
                    payload=payload,
                )
                if candidate is not None:
                    candidates.append(candidate)
            except Exception:
                pass

        page.on("response", on_response)
        try:
            page.goto(page_url, wait_until="networkidle", timeout=timeout_ms)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        browser.close()

    candidates.sort(key=lambda c: c.score, reverse=True)
    return [c.to_dict() for c in candidates[:20]]


def _build_candidate(
    *,
    method: str,
    url: str,
    status_code: int,
    content_type: str,
    request_headers: dict[str, str],
    post_data: str | None,
    payload: Any,
) -> XhrCandidate | None:
    parsed = urlparse(url)
    query = {k: v[-1] if v else "" for k, v in parse_qs(parsed.query).items()}

    json_body: dict | None = None
    if post_data:
        try:
            json_body = json.loads(post_data)
        except (json.JSONDecodeError, ValueError):
            json_body = None

    items_path, items = _find_items(payload)
    if not items or len(items) < 2:
        return None

    sample = items[0] if isinstance(items[0], dict) else {}
    fields = _infer_fields(sample)

    score, notes = _score_candidate(
        method=method, url=url, items=items, fields=fields,
    )
    if score <= 0:
        return None

    return XhrCandidate(
        method=method,
        url=url,
        status_code=status_code,
        content_type=content_type,
        request_headers=request_headers,
        query=query,
        json_body=json_body,
        response_sample=_truncate(payload, 3000),
        inferred_items_path=items_path,
        inferred_fields=fields,
        score=score,
        notes=notes,
    )


def _find_items(payload: Any) -> tuple[str, list]:
    """在 payload 中查找列表数据，支持顶层和一层嵌套。"""
    if isinstance(payload, list):
        return "", payload
    if not isinstance(payload, dict):
        return "", []
    # 顶层 key 匹配
    for key in _LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list) and value:
            return key, value
    # 顶层任意 key 的列表
    for key, value in payload.items():
        if isinstance(value, list) and len(value) >= 2:
            if all(isinstance(item, dict) for item in value):
                return key, value
    # 一层嵌套：obj.records, data.items 等
    for outer_key, outer_value in payload.items():
        if isinstance(outer_value, dict):
            for inner_key in _LIST_KEYS:
                inner_value = outer_value.get(inner_key)
                if isinstance(inner_value, list) and inner_value:
                    return f"{outer_key}.{inner_key}", inner_value
            for inner_key, inner_value in outer_value.items():
                if isinstance(inner_value, list) and len(inner_value) >= 2:
                    if all(isinstance(item, dict) for item in inner_value):
                        return f"{outer_key}.{inner_key}", inner_value
    return "", []


def _infer_fields(sample: dict) -> dict[str, str | None]:
    return {
        "title": _first_key(sample, _TITLE_KEYS),
        "url": _first_key(sample, _URL_KEYS),
        "published_at": _first_key(sample, _DATE_KEYS),
        "content": _first_key(sample, _CONTENT_KEYS),
    }


def _first_key(item: dict, candidates: tuple[str, ...]) -> str | None:
    for key in candidates:
        if key in item and item[key] not in (None, ""):
            return key
    return None


def _score_candidate(*, method: str, url: str, items: list, fields: dict[str, str | None]) -> tuple[int, list[str]]:
    score = 0
    notes: list[str] = []

    if _BLOCKLIST_PATTERNS.search(url):
        score -= 30
        notes.append("URL 命中屏蔽关键词（统计/埋点/配置类）")

    score += 30
    notes.append("响应包含列表数据")

    if len(items) >= 5:
        score += 10
        notes.append(f"列表条目数 {len(items)}，内容丰富")
    elif len(items) < 3:
        score -= 10
        notes.append(f"列表条目数仅 {len(items)}，可能非内容列表")

    if fields.get("title"):
        score += 20
        notes.append(f"识别到标题字段: {fields['title']}")
    else:
        score -= 10
        notes.append("未识别到标题字段")

    if fields.get("url"):
        score += 20
        notes.append(f"识别到链接字段: {fields['url']}")

    if fields.get("published_at"):
        score += 10
        notes.append(f"识别到日期字段: {fields['published_at']}")

    if fields.get("content"):
        score += 10
        notes.append(f"识别到内容字段: {fields['content']}")

    if method.upper() == "GET":
        score += 10
        notes.append("GET 请求，简单易复现")
    else:
        notes.append(f"{method} 请求，需关注请求体")

    auth_headers = {k.lower() for k in ("authorization", "cookie", "x-api-key", "x-auth-token")}
    return score, notes


def _truncate(obj: Any, max_chars: int) -> Any:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return obj
    return text[:max_chars] + "...(truncated)"
