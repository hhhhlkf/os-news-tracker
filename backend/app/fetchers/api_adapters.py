from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable, Protocol

from dateutil import parser as dateparser

from app.fetchers.base import Fetcher
from app.models import Source
from app.schemas import RawItem

TextRequester = Callable[[str], str]


class ApiRawItemAdapter(Protocol):
    def fetch(self, source: Source, requester: TextRequester) -> list[RawItem]: ...


def supported_api_adapters() -> set[str]:
    return set(_ADAPTERS)


def _default_text_requester(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "os-news-tracker/0.1"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _parse_json_lenient(text: str) -> Any:
    """Parse JSON, tolerating trailing non-JSON data (e.g. openEuler API extra bytes)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text)
        return obj


def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    json_body: dict | None = None,
) -> str:
    """支持 GET/POST、自定义 headers、query 和 JSON body 的 HTTP 请求。"""
    final_url = url
    if query:
        final_url = _url_with_default_query(url, query)

    req_headers = {"User-Agent": "os-news-tracker/0.1"}
    if headers:
        req_headers.update(headers)

    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        final_url,
        method=method.upper(),
        headers=req_headers,
        data=data,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


class ApiAdapterFetcher(Fetcher):
    def __init__(
        self,
        requester: TextRequester = _default_text_requester,
        adapters: dict[str, ApiRawItemAdapter] | None = None,
    ) -> None:
        self._requester = requester
        self._adapters = adapters if adapters is not None else _ADAPTERS

    def fetch(self, source: Source) -> list[RawItem]:
        probe = _probe_config(source)
        if probe:
            return ConfigurableApiProbeAdapter().fetch(source, self._requester)
        if not source.adapter or source.adapter not in self._adapters:
            raise ValueError(f"unsupported api adapter {source.adapter!r}")
        return self._adapters[source.adapter].fetch(source, self._requester)


class ConfigurableApiProbeAdapter:
    def fetch(self, source: Source, requester: TextRequester) -> list[RawItem]:
        probe = _probe_config(source)
        if not probe:
            raise ValueError(f"missing api_config.probe for {source.name}")
        mode = str(probe.get("mode") or "")
        if mode == "json_list":
            return self._fetch_json_list(source, requester, probe)
        if mode == "html_table":
            return self._fetch_html_table(source, requester, probe)
        if mode == "text":
            return self._fetch_text(source, requester, probe)
        raise ValueError(f"unsupported probe mode {mode!r}")

    def _fetch_json_list(
        self, source: Source, requester: TextRequester, probe: dict
    ) -> list[RawItem]:
        method = str(probe.get("method") or "GET").upper()
        probe_headers = probe.get("headers") or {}
        probe_query = probe.get("query") or {}
        json_body = probe.get("json_body")
        pagination = probe.get("pagination") if isinstance(probe.get("pagination"), dict) else None

        if pagination and pagination.get("page_param"):
            return self._fetch_json_list_paginated(
                source, requester, probe, pagination,
            )

        if method == "POST" or isinstance(json_body, dict):
            text = _http_request(
                str(probe.get("url") or source.url),
                method=method,
                headers=probe_headers if isinstance(probe_headers, dict) else None,
                query=probe_query if isinstance(probe_query, dict) else None,
                json_body=json_body if isinstance(json_body, dict) else None,
            )
        else:
            url = _probe_url(source, probe)
            text = requester(url)
        payload = _parse_json_lenient(text)
        raw_items = _get_path(payload, probe.get("items_path"))
        if not isinstance(raw_items, list):
            return []
        return self._items_from_raw_list(source, raw_items, probe.get("fields") or {})

    def _fetch_json_list_paginated(
        self,
        source: Source,
        requester: TextRequester,
        probe: dict,
        pagination: dict,
    ) -> list[RawItem]:
        """Loop over pages until ``max_pages`` or no more items / has_more=false.

        Supports two ``next page`` signals:
        - ``has_more_path``: a boolean field in the payload (e.g. ``data.hasMore``).
        - ``total_path`` + ``size``: stop once ``page * size >= total``.

        Page/size params are injected into the query string (GET) or the JSON
        body (POST) on each iteration.
        """
        method = str(probe.get("method") or "GET").upper()
        base_url = str(probe.get("url") or source.url)
        probe_headers = probe.get("headers") or {}
        probe_query = dict(probe.get("query") or {})
        json_body_template = probe.get("json_body")
        is_post = method == "POST" or isinstance(json_body_template, dict)

        page_param = str(pagination.get("page_param"))
        size_param = pagination.get("size_param")
        size_value = pagination.get("size")
        try:
            page = int(pagination.get("start_page", 1))
        except (TypeError, ValueError):
            page = 1
        try:
            max_pages = int(pagination.get("max_pages", 1))
        except (TypeError, ValueError):
            max_pages = 1
        items_path = probe.get("items_path")
        fields = probe.get("fields") or {}
        has_more_path = pagination.get("has_more_path")
        total_path = pagination.get("total_path")

        collected: list[RawItem] = []
        seen_urls: set[str] = set()
        empty_streak = 0
        for _ in range(max_pages):
            page_query = dict(probe_query)
            page_query[page_param] = str(page)
            if size_param and size_value is not None:
                page_query[size_param] = str(size_value)

            if is_post:
                body = dict(json_body_template) if isinstance(json_body_template, dict) else {}
                body[page_param] = page
                if size_param and size_value is not None:
                    body[size_param] = size_value
                text = _http_request(
                    base_url,
                    method=method,
                    headers=probe_headers if isinstance(probe_headers, dict) else None,
                    query=page_query or None,
                    json_body=body,
                )
            else:
                url = _url_with_default_query(base_url, page_query)
                text = requester(url)

            payload = _parse_json_lenient(text)
            raw_items = _get_path(payload, items_path)
            if not isinstance(raw_items, list):
                break

            page_items: list[RawItem] = []
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    continue
                item = self._build_raw_item(source, raw_item, fields)
                if item is None or item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                page_items.append(item)
            collected.extend(page_items)

            # 终止判定：has_more / total / 连续空页
            if has_more_path:
                if not bool(_get_path(payload, has_more_path)):
                    break
            elif total_path and size_value:
                total = _get_path(payload, total_path)
                try:
                    if int(total) <= page * int(size_value):
                        break
                except (TypeError, ValueError):
                    pass

            if not page_items:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0
            page += 1

        return collected

    @staticmethod
    def _build_raw_item(source: Source, raw_item: dict, fields: dict) -> RawItem | None:
        """Convert one raw JSON record into a RawItem, or None if missing title/url."""
        ctx = {**raw_item, "item": _DictWrapper(raw_item)}
        title = _render_config_template(fields.get("title_template"), ctx)
        if not title:
            title = _field_value(raw_item, fields.get("title"))
        url_value = _json_item_url(raw_item, fields)
        if not title or not url_value:
            return None
        content = _join_content(
            *[
                _field_value(raw_item, field)
                for field in _field_list(fields.get("content"))
            ]
        )
        return RawItem(
            source_id=source.id,
            title=str(title),
            url=str(url_value),
            raw_content=content,
            published_at=_parse_datetime(
                _field_value(raw_item, fields.get("published_at"))
            ),
        )

    def _items_from_raw_list(
        self, source: Source, raw_items: list, fields: dict
    ) -> list[RawItem]:
        items: list[RawItem] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            item = self._build_raw_item(source, raw_item, fields)
            if item is not None:
                items.append(item)
        return items

    def _fetch_html_table(
        self, source: Source, requester: TextRequester, probe: dict
    ) -> list[RawItem]:
        url = _probe_url(source, probe)
        rows = _parse_table_rows(requester(url))
        fields = probe.get("fields") or {}
        items: list[RawItem] = []
        for row in rows:
            ctx = _row_context(row, fields)
            if _is_header_or_sort_row(row):
                continue
            title = _render_config_template(fields.get("title_template"), ctx)
            content = _render_config_template(fields.get("content_template"), ctx)
            url_value = _html_row_url(url, row, fields)
            if not _html_row_link_allowed(row, fields):
                continue
            published_at = _parse_datetime(_cell_text(row, fields.get("published_at_cell")))
            if not title or not url_value:
                continue
            items.append(
                RawItem(
                    source_id=source.id,
                    title=title,
                    url=url_value,
                    raw_content=content,
                    published_at=published_at,
                )
            )
        return items

    def _fetch_text(
        self, source: Source, requester: TextRequester, probe: dict
    ) -> list[RawItem]:
        url = _probe_url(source, probe)
        value = requester(url)
        fields = probe.get("fields") or {}
        title = _render_config_template(fields.get("title_template"), {"source": source})
        if not title:
            title = _markdown_title(value) or source.name
        item_url = fields.get("url") or source.url
        return [
            RawItem(
                source_id=source.id,
                title=title,
                url=str(item_url),
                raw_content=value,
                published_at=None,
            )
        ]


@dataclass
class _Link:
    href: str
    text: str


@dataclass
class _Cell:
    text: str
    links: list[_Link]


@dataclass
class _Row:
    cells: list[_Cell]

    def first_link(self) -> _Link | None:
        for cell in self.cells:
            if cell.links:
                return cell.links[0]
        return None

    def last_date_text(self) -> str | None:
        for cell in reversed(self.cells):
            if _parse_datetime(cell.text) is not None:
                return cell.text
        return None


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[_Row] = []
        self._current_cells: list[_Cell] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_links: list[_Link] | None = None
        self._active_href: str | None = None
        self._active_link_parts: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attrs_dict = dict(attrs)
        if tag == "tr":
            self._current_cells = []
        elif tag in {"td", "th"} and self._current_cells is not None:
            self._cell_parts = []
            self._cell_links = []
        elif tag == "a" and self._cell_parts is not None:
            self._active_href = attrs_dict.get("href") or ""
            self._active_link_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active_link_parts is not None:
            text = _compact(" ".join(self._active_link_parts))
            self._cell_links.append(_Link(href=self._active_href or "", text=text))
            self._active_href = None
            self._active_link_parts = None
        elif tag in {"td", "th"} and self._cell_parts is not None:
            text = _compact(" ".join(self._cell_parts))
            self._current_cells.append(_Cell(text=text, links=self._cell_links or []))
            self._cell_parts = None
            self._cell_links = None
        elif tag == "tr" and self._current_cells is not None:
            if self._current_cells:
                self.rows.append(_Row(cells=self._current_cells))
            self._current_cells = None

    def handle_data(self, data: str) -> None:
        text = html.unescape(data).strip()
        if not text:
            return
        if self._cell_parts is not None:
            self._cell_parts.append(text)
        if self._active_link_parts is not None:
            self._active_link_parts.append(text)


def _parse_table_rows(value: str) -> list[_Row]:
    parser = _TableParser()
    parser.feed(value)
    return parser.rows


def _probe_config(source: Source) -> dict | None:
    config = source.api_config or {}
    probe = config.get("probe")
    return probe if isinstance(probe, dict) else None


def _probe_url(source: Source, probe: dict) -> str:
    url = str(probe.get("url") or source.url)
    query = probe.get("query") or {}
    if isinstance(query, dict) and query:
        return _url_with_default_query(url, {str(k): str(v) for k, v in query.items()})
    return url


def _get_path(data: object, path: object) -> object:
    if path in (None, ""):
        return data
    current = data
    for part in str(path).split("."):
        if isinstance(current, dict):
            current = current.get(part)
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
            continue
        return None
    return current


def _field_list(value: object) -> list[object]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _field_value(item: dict, field: object) -> object:
    for path in _field_list(field):
        value = _get_path(item, path)
        if value not in (None, ""):
            return value
    return None


def _json_item_url(item: dict, fields: dict) -> str:
    value = _field_value(item, fields.get("url"))
    if value:
        return str(value)
    template = fields.get("url_template")
    if template:
        return _render_config_template(str(template), {**item, "item": _DictWrapper(item)})
    return ""


def _html_row_url(base_url: str, row: _Row, fields: dict) -> str:
    link_index = fields.get("url_from_link")
    if link_index is None:
        link = row.first_link()
    else:
        cell = _cell(row, link_index)
        link = cell.links[0] if cell and cell.links else None
    if link is None:
        return ""
    return urllib.parse.urljoin(base_url, link.href)


def _html_row_link_allowed(row: _Row, fields: dict) -> bool:
    link_index = fields.get("url_from_link")
    if link_index is None:
        link = row.first_link()
    else:
        cell = _cell(row, link_index)
        link = cell.links[0] if cell and cell.links else None
    if link is None:
        return False
    for needle in _field_list(fields.get("exclude_href_contains")):
        if str(needle) in link.href:
            return False
    suffix = fields.get("include_href_suffix")
    if suffix and not link.href.endswith(str(suffix)):
        return False
    return True


def _cell(row: _Row, index: object) -> _Cell | None:
    try:
        i = int(index)
    except (TypeError, ValueError):
        return None
    return row.cells[i] if 0 <= i < len(row.cells) else None


def _cell_text(row: _Row, index: object) -> str | None:
    cell = _cell(row, index)
    return cell.text if cell else None


def _row_context(row: _Row, fields: dict | None = None) -> dict:
    fields = fields or {}
    strip_suffix = str(fields.get("strip_cell_suffix") or "")
    cells = [cell.text for cell in row.cells]
    if strip_suffix:
        cells = [cell.removesuffix(strip_suffix) for cell in cells]
    return {
        "cell": cells,
        "link": [
            {"href": link.href, "text": link.text}
            for cell in row.cells
            for link in cell.links
        ],
    }


def _is_header_or_sort_row(row: _Row) -> bool:
    values = {cell.text.lower() for cell in row.cells}
    return bool(values & {"file name", "modified", "size"}) and not row.first_link()


def _render_config_template(template: object, context: dict) -> str:
    if not template:
        return ""
    try:
        return str(template).format_map(_TemplateContext(context))
    except (KeyError, IndexError, TypeError, ValueError):
        return ""


class _TemplateContext(dict):
    def __missing__(self, key: str) -> object:
        if key == "item":
            return _DictWrapper(self.get("item", {}))
        if key == "cell":
            return self.get("cell", [])
        if key == "link":
            return self.get("link", [])
        if key == "source":
            return self.get("source")
        raise KeyError(key)


class _DictWrapper:
    """Wrap a dict so that ``{item.field}`` template syntax accesses dict keys."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getattr__(self, name: str) -> object:
        value = self._data.get(name)
        if isinstance(value, dict):
            return _DictWrapper(value)
        return value if value is not None else ""


def _parse_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        dt = dateparser.parse(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _join_content(*parts: object) -> str:
    return "\n\n".join(_compact(str(part)) for part in parts if _compact(str(part or "")))


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _markdown_title(value: str) -> str | None:
    for line in value.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line.removeprefix("# ").strip()
    return None


def _url_with_default_query(url: str, defaults: dict[str, str]) -> str:
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    changed = False
    for key, value in defaults.items():
        if key not in query:
            query[key] = value
            changed = True
    if not changed:
        return url
    return urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(query))
    )


_ADAPTERS: dict[str, ApiRawItemAdapter] = {}
