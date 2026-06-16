import json
import logging
import re
import urllib.parse
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable

from dateutil import parser as dateparser

from app.fetchers.base import Fetcher
from app.models import Source
from app.schemas import RawItem

logger = logging.getLogger(__name__)

JsonRequester = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    def text(self) -> str:
        return _compact_text(unescape(" ".join(self._parts)))


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _clean_content(value: Any, content_format: str) -> str:
    if value is None:
        return ""
    text = str(value)
    if content_format == "html":
        parser = _HtmlTextExtractor()
        parser.feed(text)
        return parser.text()
    return _compact_text(text)


def _get_path(data: Any, path: str | None) -> Any:
    if path is None or path == "":
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
            continue
        return None
    return current


def _first_field_value(item: dict[str, Any], field: str | list[str] | None) -> Any:
    if field is None:
        return None
    paths = field if isinstance(field, list) else [field]
    for path in paths:
        value = _get_path(item, path)
        if value not in (None, ""):
            return value
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = dateparser.parse(str(value))
        except (TypeError, ValueError):
            return None
    if dt is None:
        return None
    if dt.tzinfo:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def _render_url(item: dict[str, Any], fields: dict[str, Any]) -> str:
    url_field = fields.get("url")
    url_value = _first_field_value(item, url_field)
    if url_value:
        return str(url_value)

    template = fields.get("url_template")
    if template:
        return str(template).format(**item)

    return ""


def _default_requester(
    url: str, params: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request_url = f"{url}?{query}" if query else url
    req = urllib.request.Request(request_url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class GenericJsonApiFetcher(Fetcher):
    def __init__(self, requester: JsonRequester = _default_requester) -> None:
        self._requester = requester

    def fetch(self, source: Source) -> list[RawItem]:
        config = source.api_config or {}
        items: list[RawItem] = []
        for endpoint in self._endpoints(source, config):
            items.extend(self._fetch_endpoint(source, config, endpoint))
        return items

    def _endpoints(
        self, source: Source, config: dict[str, Any]
    ) -> list[dict[str, Any]]:
        endpoints = config.get("endpoints")
        if isinstance(endpoints, list) and endpoints:
            return [endpoint for endpoint in endpoints if isinstance(endpoint, dict)]
        return [{"url": source.url}]

    def _fetch_endpoint(
        self,
        source: Source,
        config: dict[str, Any],
        endpoint: dict[str, Any],
    ) -> list[RawItem]:
        pagination = config.get("pagination") or {}
        page_param = pagination.get("page_param")
        page = int(pagination.get("start_page", 1))
        max_pages = int(pagination.get("max_pages", 1))
        params = deepcopy(config.get("params") or {})
        params.update(endpoint.get("params") or {})
        headers = {
            str(key): str(value)
            for key, value in (config.get("headers") or {}).items()
        }
        url = str(endpoint.get("url") or source.url)
        items: list[RawItem] = []

        for _ in range(max_pages):
            if page_param:
                params[page_param] = page
            payload = self._requester(url, deepcopy(params), headers)
            items.extend(self._items_from_payload(source, config, payload))

            if not self._has_next_page(payload, pagination):
                break
            next_url_path = pagination.get("next_url_path")
            next_url = _get_path(payload, next_url_path) if next_url_path else None
            if next_url:
                url = str(next_url)
                params = {}
            else:
                page += 1

        return items

    def _has_next_page(
        self, payload: dict[str, Any], pagination: dict[str, Any]
    ) -> bool:
        next_url_path = pagination.get("next_url_path")
        if next_url_path and _get_path(payload, next_url_path):
            return True
        has_more_path = pagination.get("has_more_path")
        if has_more_path:
            return bool(_get_path(payload, has_more_path))
        return False

    def _items_from_payload(
        self, source: Source, config: dict[str, Any], payload: dict[str, Any]
    ) -> list[RawItem]:
        raw_items = _get_path(payload, config.get("items_path"))
        if not isinstance(raw_items, list):
            logger.info(
                "Generic JSON API source %s did not yield a list at %s",
                source.name,
                config.get("items_path"),
            )
            return []

        fields = config.get("fields") or {}
        content_format = str(config.get("content_format") or "text")
        min_content_len = int(config.get("min_content_len", 0))
        items: list[RawItem] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            title = _first_field_value(raw_item, fields.get("title"))
            content_value = _first_field_value(raw_item, fields.get("content"))
            content = _clean_content(content_value, content_format)
            if min_content_len and len(content) < min_content_len:
                logger.info(
                    "Skipping short JSON API item from %s: %s",
                    source.name,
                    title or _render_url(raw_item, fields),
                )
                continue
            url = _render_url(raw_item, fields)
            if not title or not url:
                logger.info(
                    "Skipping JSON API item from %s with missing title/url",
                    source.name,
                )
                continue
            items.append(
                RawItem(
                    source_id=source.id,
                    title=str(title).strip(),
                    url=url,
                    raw_content=content,
                    published_at=_parse_datetime(
                        _first_field_value(raw_item, fields.get("published_at"))
                    ),
                )
            )
        return items
