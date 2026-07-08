from __future__ import annotations

import re
from urllib.parse import urlparse

MAX_SUGGEST_NAME_LENGTH = 20
MAX_WEBSITE_LABEL_LENGTH = 48
WEBSITE_NAME_PREFIX = "网站："
WECHAT_SEARCH_NAME_PREFIX = "微信搜索："
WECHAT_HISTORY_NAME_PREFIX = "公众号："
INTERNAL_FORUM_NAME_PREFIX = "司内论坛："


def _clean_fragment(value: str | None, *, max_length: int) -> str | None:
    if not value:
        return None
    cleaned = value.strip().strip("'\"“”‘’`")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[。！？!?,，；;：:]+$", "", cleaned).strip()
    cleaned = cleaned[:max_length].strip()
    return cleaned or None


def normalize_site_name(value: str | None) -> str | None:
    return _clean_fragment(value, max_length=MAX_SUGGEST_NAME_LENGTH)


def format_website_display_name(value: str | None, *, fallback_url: str | None = None) -> str:
    base = _clean_fragment(value, max_length=MAX_WEBSITE_LABEL_LENGTH)
    if base and base.startswith(WEBSITE_NAME_PREFIX):
        base = _clean_fragment(base.removeprefix(WEBSITE_NAME_PREFIX), max_length=MAX_WEBSITE_LABEL_LENGTH)
    if not base and fallback_url:
        base = _website_label_from_url(fallback_url)
    if not base:
        base = "未命名站点"
    return f"{WEBSITE_NAME_PREFIX}{base}"


def default_website_display_name(site_url: str) -> str:
    return format_website_display_name(_website_label_from_url(site_url), fallback_url=site_url)


def _website_label_from_url(site_url: str) -> str | None:
    parsed = urlparse(site_url.strip())
    host = parsed.netloc.removeprefix("www.").strip()
    path = re.sub(r"/+", "/", parsed.path or "").strip("/")
    segments = [segment for segment in path.split("/") if segment]
    if segments and segments[-1].lower() in {"feed", "rss", "rss.xml", "atom.xml", "index.html"}:
        segments = segments[:-1]
    label_parts = [host] if host else []
    if segments:
        label_parts.append("/".join(segments[:2]))
    label = "/".join(part for part in label_parts if part)
    return _clean_fragment(label or site_url, max_length=MAX_WEBSITE_LABEL_LENGTH)


def format_wechat_search_display_name(value: str) -> str:
    return f"{WECHAT_SEARCH_NAME_PREFIX}{normalize_site_name(value) or value.strip()}"


def format_wechat_history_display_name(value: str) -> str:
    return f"{WECHAT_HISTORY_NAME_PREFIX}{normalize_site_name(value) or value.strip()}"


def format_internal_forum_display_name(value: str) -> str:
    return f"{INTERNAL_FORUM_NAME_PREFIX}{normalize_site_name(value) or value.strip()}"
