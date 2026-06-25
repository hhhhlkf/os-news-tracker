"""智能探测 — 页面背后的 JSON API 发现引擎。

用 Playwright 渲染目标页，监听 ``page.on("response")`` 捕获所有 JSON XHR/Fetch
响应；在每个响应里递归查找列表数组，用 ``detector.py`` 的字段候选键映射
title/url/published_at/content，评分选最优候选生成与
``ApiAdapterFetcher`` 完全兼容的 ``probe`` 配置；再用
``ApiAdapterFetcher`` 复跑 probe 自检，确认能解析出带 title+url 的条目。

对外主入口：``discover_api_source(url, ...) -> ApiDiscoveryResult``。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.sources.detector import (
    _CONTENT_KEYS,
    _PUBLISHED_KEYS,
    _TITLE_KEYS,
    _URL_KEYS,
    _first_existing_key,
)

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 500_000
_MIN_ITEMS = 2
_DEFAULT_MAX_CANDIDATES = 20
_DEFAULT_SAMPLE_ITEMS = 3
_MAX_SEARCH_DEPTH = 5

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_ID_LIKE_KEYS = (
    "id", "slug", "post_id", "article_id", "nid", "uuid", "code",
    "no", "blog_id", "doc_id", "entry_id", "postNo", "blogNo",
)

# Extend detector's published_at candidates with camelCase / other common names.
_PUBLISHED_KEYS_EXT = _PUBLISHED_KEYS + (
    "publishTime", "gmtCreate", "publish_time", "createTime", "create_time",
    "postDate", "post_date", "pubdate", "lastModified", "last_modified",
    "updateTime", "update_time", "timestamp", "time", "createdAt",
)


@dataclass
class ApiCandidate:
    """A captured JSON response that might be an article list API."""

    api_url: str
    method: str
    post_data: str | None
    status: int
    items_path: str
    items: list[dict]
    fields: dict
    score: float

    def to_dict(self) -> dict:
        return {
            "api_url": self.api_url,
            "method": self.method,
            "status": self.status,
            "items_path": self.items_path,
            "items_count": len(self.items),
            "fields": self.fields,
            "score": round(self.score, 3),
        }


@dataclass
class ApiDiscoveryResult:
    root_url: str
    success: bool = False
    api_url: str | None = None
    method: str = "GET"
    items_path: str | None = None
    fields: dict = field(default_factory=dict)
    name_suggestion: str = ""
    sample_items: list[dict] = field(default_factory=list)
    real_content_count: int = 0
    candidates: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "root_url": self.root_url,
            "success": self.success,
            "api_url": self.api_url,
            "method": self.method,
            "items_path": self.items_path,
            "fields": self.fields,
            "name_suggestion": self.name_suggestion,
            "sample_items": self.sample_items,
            "real_content_count": self.real_content_count,
            "candidates": self.candidates,
            "notes": self.notes,
        }


# ──────────────────────────────────────────────────────────────────
# Playwright 渲染 + JSON 捕获
# ──────────────────────────────────────────────────────────────────
def _capture_and_render(url: str, *, wait_ms: int = 3000) -> tuple[list[dict], list[str]]:
    """Render the page with Playwright, capture JSON responses and anchor links.

    Returns ``(captured_responses, anchor_hrefs)``.
    """
    from playwright.sync_api import sync_playwright

    captured: list[dict] = []

    def on_response(response) -> None:
        try:
            try:
                body = response.text()
            except Exception:  # noqa: BLE001
                return
            if not body or len(body) > _MAX_BODY_BYTES:
                return
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return
            request = response.request
            captured.append(
                {
                    "api_url": response.url,
                    "method": request.method,
                    "post_data": request.post_data,
                    "status": response.status,
                    "payload": payload,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("api_discovery: response capture error: %s", exc)

    anchor_links: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=_USER_AGENT)
        page = context.new_page()
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(wait_ms)
        try:
            anchor_links = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.href).filter(h => h && h.startsWith('http'))",
            )
        except Exception:  # noqa: BLE001
            anchor_links = []
        browser.close()

    return captured, anchor_links


# ──────────────────────────────────────────────────────────────────
# JSON 列表数组查找（递归）
# ──────────────────────────────────────────────────────────────────
def _collect_arrays(
    obj: Any,
    path: str,
    results: list[tuple[str, list[dict]]],
    *,
    depth: int,
    max_depth: int,
) -> None:
    """Recursively collect (path, list-of-dicts) arrays in ``obj``."""
    if depth > max_depth:
        return
    if isinstance(obj, list):
        dict_items = [i for i in obj if isinstance(i, dict)]
        if len(dict_items) >= _MIN_ITEMS:
            results.append((path, dict_items))
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            _collect_arrays(value, child_path, results, depth=depth + 1, max_depth=max_depth)


def _find_list_arrays(payload: Any) -> list[tuple[str, list[dict]]]:
    """Find all list-of-dicts arrays in ``payload``, sorted by length desc."""
    results: list[tuple[str, list[dict]]] = []
    _collect_arrays(payload, "", results, depth=0, max_depth=_MAX_SEARCH_DEPTH)
    results.sort(key=lambda x: len(x[1]), reverse=True)
    return results


# ──────────────────────────────────────────────────────────────────
# 候选评估与评分
# ──────────────────────────────────────────────────────────────────
def _evaluate_candidate(captured: dict, page_url: str) -> list[ApiCandidate]:
    """Evaluate a captured JSON response; may yield multiple candidates (one per array)."""
    payload = captured["payload"]
    arrays = _find_list_arrays(payload)
    candidates: list[ApiCandidate] = []
    for path, items in arrays:
        sample = items[0]
        title_key = _first_existing_key(sample, _TITLE_KEYS)
        if not title_key:
            continue
        url_key = _first_existing_key(sample, _URL_KEYS)
        published_key = _first_existing_key(sample, _PUBLISHED_KEYS_EXT)
        # Collect ALL content-like keys (not just the first), so the probe
        # joins summary + full content, giving the quality pool enough text.
        content_keys = [
            k for k in _CONTENT_KEYS
            if k in sample and sample[k] not in (None, "")
        ]

        fields: dict[str, Any] = {"title": title_key}
        if url_key:
            fields["url"] = url_key
        if published_key:
            fields["published_at"] = published_key
        if content_keys:
            fields["content"] = content_keys

        score = _score(
            items=items,
            fields=fields,
            api_url=captured["api_url"],
            page_url=page_url,
        )
        candidates.append(
            ApiCandidate(
                api_url=captured["api_url"],
                method=captured["method"],
                post_data=captured.get("post_data"),
                status=captured["status"],
                items_path=path,
                items=items,
                fields=fields,
                score=score,
            )
        )
    return candidates


def _score(*, items: list[dict], fields: dict, api_url: str, page_url: str) -> float:
    score = 0.0
    score += min(len(items), 50) * 0.5
    if fields.get("title"):
        score += 5
        sample_title = str(items[0].get(fields["title"], ""))
        if len(sample_title) > 5:
            score += 2
    if fields.get("url"):
        score += 5
    else:
        score += 1
    if fields.get("published_at"):
        score += 3
    if fields.get("content"):
        score += 4  # strong signal: items have actual article content
    api_path = urlparse(api_url).path.lower()
    if any(kw in api_path for kw in ("blog", "news", "article", "post", "list", "content")):
        score += 3
    # Penalize metadata-like APIs (categories, tags, menus)
    if any(kw in api_path for kw in ("category", "categorie", "tag", "menu", "nav", "config")):
        score -= 5
    # Reward paginated article-list patterns
    if any(kw in api_path for kw in ("page", "search", "feed")):
        score += 2
    if _same_registrable_domain(api_url, page_url):
        score += 2
    return score


def _same_registrable_domain(url_a: str, url_b: str) -> bool:
    """Check if two URLs share the same registrable domain (last 2 parts of netloc)."""
    a = urlparse(url_a).netloc.split(":")[0].removeprefix("www.")
    b = urlparse(url_b).netloc.split(":")[0].removeprefix("www.")
    if a == b:
        return True
    # Compare on last 2 domain parts (e.g. api.example.com ~ example.com)
    a_parts = a.rsplit(".", 2)[-2:]
    b_parts = b.rsplit(".", 2)[-2:]
    return a_parts == b_parts


# ──────────────────────────────────────────────────────────────────
# URL 模板推断
# ──────────────────────────────────────────────────────────────────
def _infer_url_template_from_anchors(
    items: list[dict], anchor_links: list[str]
) -> str | None:
    """Try to find a detail URL pattern by matching item ids/slugs against page anchors.

    Handles both path-based (``/blog/detail/123``) and hash-based (``/blog#123``)
    SPA routes by checking the full URL, not just the path.
    """
    if not items:
        return None
    sample = items[0]
    for key in _ID_LIKE_KEYS:
        value = sample.get(key)
        if value is None:
            continue
        value_str = str(value)
        for link in anchor_links:
            if value_str in link:
                template = link.replace(value_str, "{item." + key + "}")
                # Accept if the placeholder landed in path OR fragment
                parsed = urlparse(template)
                if "{" in parsed.path or "{" in parsed.fragment:
                    return template
    return None


def _guess_url_template(items: list[dict], page_url: str) -> str | None:
    """Fallback: construct a url_template from page_url path + id/slug field."""
    if not items:
        return None
    sample = items[0]
    parsed = urlparse(page_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    for key in _ID_LIKE_KEYS:
        if sample.get(key) is not None:
            return f"{base}{path}/detail/{{item.{key}}}"
    return None


# ──────────────────────────────────────────────────────────────────
# Probe 构建 + 自检
# ──────────────────────────────────────────────────────────────────
def _build_probe(
    candidate: ApiCandidate,
    anchor_links: list[str],
    page_url: str,
    notes: list[str],
) -> dict:
    fields: dict[str, Any] = dict(candidate.fields)

    if not fields.get("url"):
        template = _infer_url_template_from_anchors(candidate.items, anchor_links)
        if template:
            fields["url_template"] = template
        else:
            template = _guess_url_template(candidate.items, page_url)
            if template:
                fields["url_template"] = template
                notes.append(f"url_template 为推测值（{template}），请在前端确认或补全")
            else:
                notes.append("条目缺少 url 字段且无法推断 url_template")

    probe: dict[str, Any] = {
        "mode": "json_list",
        "method": candidate.method,
        "url": candidate.api_url,
        "items_path": candidate.items_path or "",
        "fields": fields,
    }
    if candidate.method == "POST" and candidate.post_data:
        try:
            probe["json_body"] = json.loads(candidate.post_data)
        except (json.JSONDecodeError, ValueError):
            pass
    return probe


def _self_check_probe(probe: dict) -> list:
    """Re-run the probe with ApiAdapterFetcher to validate it produces items with title+url.

    Limits pagination to 1 page for quick validation.
    """
    from app.fetchers.api_adapters import ApiAdapterFetcher
    from app.models import Source

    check_probe = dict(probe)
    if isinstance(check_probe.get("pagination"), dict):
        check_probe["pagination"] = {**check_probe["pagination"], "max_pages": 1}

    temp_source = Source(
        id=0,
        name="_probe_check",
        type="api",
        url=str(check_probe.get("url") or ""),
        api_config={"probe": check_probe},
        stream="news",
        enabled=True,
    )
    fetcher = ApiAdapterFetcher()
    return fetcher.fetch(temp_source)


# ──────────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────────
def discover_api_source(
    root_url: str,
    *,
    max_candidates: int = _DEFAULT_MAX_CANDIDATES,
    sample_items: int = _DEFAULT_SAMPLE_ITEMS,
) -> ApiDiscoveryResult:
    """Discover the JSON API behind a page and generate a compatible probe config.

    Steps:
    1. Render the page with Playwright, capture JSON XHR/fetch responses.
    2. In each response, find list arrays and map title/url/date/content fields.
    3. Score candidates and pick the best.
    4. Generate a probe (compatible with ``ApiAdapterFetcher``).
    5. Self-check: re-run the probe to verify it produces items with title+url.
    """
    result = ApiDiscoveryResult(root_url=root_url)

    try:
        captured, anchor_links = _capture_and_render(root_url)
    except Exception as exc:  # noqa: BLE001
        result.notes.append(f"页面渲染失败: {exc}")
        return result

    if not captured:
        result.notes.append("未捕获到任何 JSON XHR/Fetch 响应")
        return result

    all_candidates: list[ApiCandidate] = []
    for cap in captured:
        all_candidates.extend(_evaluate_candidate(cap, root_url))

    # Deduplicate by (api_url, items_path)
    seen: set[tuple[str, str]] = set()
    unique: list[ApiCandidate] = []
    for c in all_candidates:
        key = (c.api_url, c.items_path)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    unique = unique[:max_candidates]
    result.candidates = [c.to_dict() for c in unique]

    if not unique:
        result.notes.append("捕获到 JSON 响应，但未找到含文章列表的 API")
        return result

    unique.sort(key=lambda c: c.score, reverse=True)

    # Try candidates in score order; use the first that passes self-check.
    for best in unique:
        candidate_notes: list[str] = []
        probe = _build_probe(best, anchor_links, root_url, candidate_notes)

        try:
            raw_items = _self_check_probe(probe)
        except Exception as exc:  # noqa: BLE001
            result.notes.append(f"候选 {best.api_url} 自检失败: {exc}")
            continue

        valid = [i for i in raw_items if i.title and i.url]
        if not valid:
            result.notes.append(
                f"候选 {best.api_url} 自检未产生有效条目"
                + (f"（{'；'.join(candidate_notes)}）" if candidate_notes else "")
            )
            continue

        # Success — use this candidate
        result.success = True
        result.api_url = best.api_url
        result.method = best.method
        result.items_path = best.items_path or ""
        result.fields = probe["fields"]
        result.name_suggestion = _domain_name(root_url)
        result.real_content_count = len(valid)
        result.sample_items = [
            {
                "title": i.title,
                "url": i.url,
                "published_at": i.published_at.isoformat() if i.published_at else None,
                "content_preview": (i.raw_content or "")[:300],
            }
            for i in valid[:sample_items]
        ]
        result.notes.extend(candidate_notes)

        if not result.fields.get("published_at"):
            result.notes.append(
                "⚠️ 未映射到日期字段，一键运行的时间窗过滤会丢弃无日期条目"
            )

        return result

    result.notes.append("所有候选均未通过自检")
    return result


def _domain_name(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.removeprefix("www.") or url
