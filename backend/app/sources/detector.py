from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable

import feedparser

from app.enums import SourceType


class SourceDetectionError(ValueError):
    pass


@dataclass(frozen=True)
class DetectResult:
    detected_type: str
    name_suggestion: str
    api_config: dict | None
    notes: list[str]


@dataclass(frozen=True)
class FetchResult:
    text: str
    content_type: str = ""


SourceProbeFetcher = Callable[[str], FetchResult | str]

_COMMON_LIST_KEYS = ("items", "results", "data", "entries", "articles", "posts")
_TITLE_KEYS = ("title", "headline", "name", "subject")
_URL_KEYS = ("url", "link", "html_url", "href", "permalink")
_PUBLISHED_KEYS = ("published", "published_at", "created_at", "date", "updated", "pubDate")
_CONTENT_KEYS = ("summary", "description", "content", "body", "abstract")


def detect_source(url: str, fetcher: SourceProbeFetcher | None = None) -> DetectResult:
    normalized_url = _validate_url(url)
    fetch = fetcher or _default_fetcher
    fetched = fetch(normalized_url)
    result = fetched if isinstance(fetched, FetchResult) else FetchResult(text=str(fetched))
    text = result.text.strip()
    if not text:
        raise SourceDetectionError("empty response")

    feed_result = _detect_feed(text, normalized_url)
    if feed_result:
        return feed_result

    api_result = _detect_json_api(text, normalized_url)
    if api_result:
        return api_result

    html_result = _detect_html(text, normalized_url)
    if html_result:
        return html_result

    raise SourceDetectionError("unable to detect source shape")


def _default_fetcher(url: str) -> FetchResult:
    # v1 只做超时保护，不做 SSRF 拦截；后续可按部署网络边界补 allowlist/private IP 防护。
    req = urllib.request.Request(url, headers={"User-Agent": "os-news-tracker/0.1"})
    with urllib.request.urlopen(req, timeout=10) as response:
        content_type = response.headers.get("content-type", "")
        text = response.read().decode("utf-8", errors="replace")
    return FetchResult(text=text, content_type=content_type)


def _validate_url(url: str) -> str:
    value = url.strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SourceDetectionError("invalid url")
    return value


def _detect_feed(text: str, url: str) -> DetectResult | None:
    parsed = feedparser.parse(text)
    if parsed.bozo and not parsed.entries:
        return None
    if not parsed.entries:
        return None
    title = _compact(str(parsed.feed.get("title") or "")) or _domain_name(url)
    return DetectResult(
        detected_type=SourceType.RSS.value,
        name_suggestion=title,
        api_config=None,
        notes=["识别为 RSS/Atom 订阅源，将按条目发布时间增量抓取。"],
    )


def _detect_json_api(text: str, url: str) -> DetectResult | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None

    path, items = _find_json_items(payload)
    if not items:
        return None
    sample = next((item for item in items if isinstance(item, dict)), None)
    if sample is None:
        return None

    title_key = _first_existing_key(sample, _TITLE_KEYS)
    url_key = _first_existing_key(sample, _URL_KEYS)
    if not title_key or not url_key:
        return None

    fields: dict[str, object] = {
        "title": title_key,
        "url": url_key,
    }
    published_key = _first_existing_key(sample, _PUBLISHED_KEYS)
    if published_key:
        fields["published_at"] = published_key
    content_key = _first_existing_key(sample, _CONTENT_KEYS)
    if content_key:
        fields["content"] = [content_key]

    return DetectResult(
        detected_type=SourceType.API.value,
        name_suggestion=_domain_name(url),
        api_config={
            "probe": {
                "mode": "json_list",
                "items_path": path,
                "fields": fields,
            }
        },
        notes=[
            f"识别为 JSON API，列表路径为 {path or '根节点'}。",
            f"标题字段 {title_key}，链接字段 {url_key}。",
        ],
    )


def _find_json_items(payload: object) -> tuple[str, list]:
    if isinstance(payload, list):
        return "", payload
    if not isinstance(payload, dict):
        return "", []
    for key in _COMMON_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return key, value
    return "", []


def _first_existing_key(item: dict, candidates: tuple[str, ...]) -> str | None:
    for key in candidates:
        if key in item and item[key] not in (None, ""):
            return key
    return None


def _detect_html(text: str, url: str) -> DetectResult | None:
    if "<html" not in text[:1000].lower() and "<!doctype html" not in text[:1000].lower():
        return None
    parser = _TitleParser()
    parser.feed(text)
    title = _compact(parser.title or "") or _domain_name(url)
    return DetectResult(
        detected_type=SourceType.PAGE_MONITOR.value,
        name_suggestion=title,
        api_config=None,
        notes=["识别为普通网页，将按整页内容变化进行 page monitor 抓取，暂不自动生成选择器。"],
    )


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._inside_title = False
        self._parts: list[str] = []

    @property
    def title(self) -> str:
        return " ".join(self._parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self._inside_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._inside_title = False

    def handle_data(self, data: str) -> None:
        if self._inside_title:
            self._parts.append(data)


def _domain_name(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc.removeprefix("www.") or url


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
