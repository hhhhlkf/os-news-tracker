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


class ApiAdapterFetcher(Fetcher):
    def __init__(
        self,
        requester: TextRequester = _default_text_requester,
        adapters: dict[str, ApiRawItemAdapter] | None = None,
    ) -> None:
        self._requester = requester
        self._adapters = adapters if adapters is not None else _ADAPTERS

    def fetch(self, source: Source) -> list[RawItem]:
        if not source.adapter or source.adapter not in self._adapters:
            raise ValueError(f"unsupported api adapter {source.adapter!r}")
        return self._adapters[source.adapter].fetch(source, self._requester)


class UbuntuSecurityAdapter:
    def fetch(self, source: Source, requester: TextRequester) -> list[RawItem]:
        payload = json.loads(requester(source.url))
        items: list[RawItem] = []
        for notice in payload.get("notices", []):
            notice_id = str(notice.get("id") or "").strip()
            title = str(notice.get("title") or notice_id).strip()
            if not notice_id or not title:
                continue
            content = _join_content(
                notice.get("summary"),
                notice.get("description"),
                _packages_summary(notice.get("release_packages")),
            )
            items.append(
                RawItem(
                    source_id=source.id,
                    title=f"{notice_id}: {title}",
                    url=f"https://ubuntu.com/security/notices/{notice_id}",
                    raw_content=content,
                    published_at=_parse_datetime(notice.get("published")),
                )
            )
        return items


class UbuntuCveAdapter:
    def fetch(self, source: Source, requester: TextRequester) -> list[RawItem]:
        url = _url_with_default_query(source.url, {"limit": "20"})
        payload = json.loads(requester(url))
        items: list[RawItem] = []
        for cve in payload.get("cves", []):
            cve_id = str(cve.get("id") or "").strip()
            if not cve_id:
                continue
            priority = str(cve.get("priority") or "unknown").strip()
            status = str(cve.get("status") or "unknown").strip()
            description = _join_content(
                cve.get("ubuntu_description"),
                cve.get("description"),
                _packages_summary(cve.get("packages")),
            )
            items.append(
                RawItem(
                    source_id=source.id,
                    title=f"{cve_id}: {priority} {status}",
                    url=f"https://ubuntu.com/security/{cve_id}",
                    raw_content=description,
                    published_at=_parse_datetime(cve.get("published")),
                )
            )
        return items


class UbuntuOsvAdapter:
    def fetch(self, source: Source, requester: TextRequester) -> list[RawItem]:
        readme_url = (
            "https://raw.githubusercontent.com/canonical/"
            "ubuntu-security-notices/main/README.md"
        )
        readme = requester(readme_url)
        title = _markdown_title(readme) or "Ubuntu Vulnerability Data"
        return [
            RawItem(
                source_id=source.id,
                title=title,
                url=source.url,
                raw_content=readme,
                published_at=None,
            )
        ]


class OpenEulerRepoAdapter:
    def fetch(self, source: Source, requester: TextRequester) -> list[RawItem]:
        rows = _parse_table_rows(requester(source.url))
        items: list[RawItem] = []
        for row in rows:
            link = row.first_link()
            if link is None or not link.href or "?" in link.href:
                continue
            name = link.text.rstrip("/")
            if not name or name.lower() in {"file name", "here", "mirror list", "contact us"}:
                continue
            published = _parse_datetime(row.last_date_text())
            url = urllib.parse.urljoin(source.url, link.href)
            items.append(
                RawItem(
                    source_id=source.id,
                    title=f"openEuler repo {name}",
                    url=url,
                    raw_content=f"openEuler repository entry {name}; modified {row.last_date_text() or 'unknown'}",
                    published_at=published,
                )
            )
        return items


class CanonicalSecurityMetaAdapter:
    def fetch(self, source: Source, requester: TextRequester) -> list[RawItem]:
        index_url = urllib.parse.urljoin(source.url.rstrip("/") + "/", "oval/")
        rows = _parse_table_rows(requester(index_url))
        items: list[RawItem] = []
        for row in rows:
            link = row.first_link()
            if link is None or not link.href.endswith(".bz2"):
                continue
            release = row.cells[1].text if len(row.cells) > 1 else ""
            published = _parse_datetime(row.last_date_text())
            url = urllib.parse.urljoin(index_url, link.href)
            items.append(
                RawItem(
                    source_id=source.id,
                    title=f"Ubuntu OVAL {release}",
                    url=url,
                    raw_content=f"Ubuntu OVAL metadata file {link.text}; release {release}; modified {row.last_date_text() or 'unknown'}",
                    published_at=published,
                )
            )
        return items


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


def _packages_summary(value: object) -> str:
    if isinstance(value, dict):
        names = sorted(
            {
                str(pkg.get("name"))
                for packages in value.values()
                if isinstance(packages, list)
                for pkg in packages
                if isinstance(pkg, dict) and pkg.get("name")
            }
        )
        return "Packages: " + ", ".join(names[:20]) if names else ""
    if isinstance(value, list):
        names = sorted(
            {
                str(pkg.get("name"))
                for pkg in value
                if isinstance(pkg, dict) and pkg.get("name")
            }
        )
        return "Packages: " + ", ".join(names[:20]) if names else ""
    return ""


def _markdown_title(value: str) -> str | None:
    for line in value.splitlines():
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


_ADAPTERS: dict[str, ApiRawItemAdapter] = {
    "ubuntu_security": UbuntuSecurityAdapter(),
    "ubuntu_cve": UbuntuCveAdapter(),
    "ubuntu_osv": UbuntuOsvAdapter(),
    "openeuler_repo": OpenEulerRepoAdapter(),
    "canonical_security_meta": CanonicalSecurityMetaAdapter(),
}
