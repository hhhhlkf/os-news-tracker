from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def crawl_method_domain_key(entry_url: str, recipe: Any) -> str:
    """Return the dedup key for a discovery crawl method.

    RSS/Atom feeds are keyed by the normalized full feed URL so multiple feeds
    on the same host can coexist. Other website sources keep host-level dedup.
    """
    feed_url = _first_feed_url(recipe)
    if feed_url:
        return _feed_domain_key(feed_url)

    parsed = urlparse(entry_url)
    return parsed.netloc or entry_url


def crawl_method_domain_key_for_input_url(entry_url: str) -> str:
    """Best-effort pre-discovery key for direct feed-looking URLs."""
    if _looks_like_feed_url(entry_url):
        return _feed_domain_key(entry_url)
    parsed = urlparse(entry_url)
    return parsed.netloc or entry_url


def normalized_feed_domain_key(feed_url: str) -> str:
    return _feed_domain_key(feed_url)


def is_feed_recipe(recipe: Any) -> bool:
    return _first_feed_url(recipe) is not None


def _first_feed_url(recipe: Any) -> str | None:
    for action in _iter_actions(_recipe_actions(recipe)):
        if _value(action, "op") != "fetch":
            continue
        if str(_value(action, "mode") or "").lower() not in {"feed", "rss", "atom"}:
            continue
        url = _url_with_action_query(_value(action, "url"), _value(action, "query"))
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _iter_actions(actions: Any):
    if not isinstance(actions, list):
        return
    for action in actions:
        yield action
        if _value(action, "op") == "loop":
            yield from _iter_actions(_value(action, "body") or [])
            yield from _iter_actions(_value(action, "on_each") or [])


def _recipe_actions(recipe: Any) -> Any:
    if isinstance(recipe, dict):
        return recipe.get("actions")
    return getattr(recipe, "actions", None)


def _value(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    if key == "as":
        return getattr(obj, "as_", None)
    return getattr(obj, key, None)


def _feed_domain_key(feed_url: str) -> str:
    normalized = _normalize_full_url(feed_url)
    key = f"feed:{normalized}"
    if len(key) <= 255:
        return key
    parsed = urlparse(normalized)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"feed:{parsed.netloc}:{digest}"


def _url_with_action_query(url: Any, query: Any) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    if not isinstance(query, dict) or not query:
        return url
    parsed = urlparse(url.strip())
    params = parse_qsl(parsed.query, keep_blank_values=True)
    params.extend((str(k), str(v)) for k, v in query.items() if v is not None)
    merged_query = urlencode(sorted(params), doseq=True)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, merged_query, parsed.fragment)
    )


def _looks_like_feed_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    path = parsed.path.lower().rstrip("/")
    if path.endswith((".rss", ".atom", ".xml")):
        return True
    return any(part in {"feed", "feeds", "rss", "atom"} for part in path.split("/"))


def _normalize_full_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(parse_qsl(parsed.query, keep_blank_values=True)),
        doseq=True,
    )
    return urlunparse((scheme, netloc, path, "", query, ""))
