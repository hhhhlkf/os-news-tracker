import json
import logging
import re
from datetime import datetime
from html.parser import HTMLParser

from app.extract.base import ContentExtractor
from app.schemas import ExtractedDoc

logger = logging.getLogger(__name__)

# Meta-tag patterns for date extraction, tried in order.
# Each tuple is (regex_pattern, label_for_logging).
_META_DATE_PATTERNS: list[tuple[str, str]] = [
    (
        r'<meta[^>]+property="article:published_time"[^>]+content="([^"]+)"',
        "article:published_time",
    ),
    (
        r'<meta[^>]+property="article:modified_time"[^>]+content="([^"]+)"',
        "article:modified_time",
    ),
    (
        r'<meta[^>]+name="dc\.date"[^>]+content="([^"]+)"',
        "DC.date",
    ),
    (
        r'<meta[^>]+name="dcterms\.created"[^>]+content="([^"]+)"',
        "dcterms.created",
    ),
    (
        r'<meta[^>]+name="dcterms\.modified"[^>]+content="([^"]+)"',
        "dcterms.modified",
    ),
    (
        r'<meta[^>]+name="date"[^>]+content="([^"]+)"',
        "date",
    ),
    (
        r'<meta[^>]+name="pubdate"[^>]+content="([^"]+)"',
        "pubdate",
    ),
    (
        r'<meta[^>]+name="last-modified"[^>]+content="([^"]+)"',
        "last-modified",
    ),
    (
        r'<meta[^>]+http-equiv="last-modified"[^>]+content="([^"]+)"',
        "http-equiv last-modified",
    ),
]


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = None
        self._in_title = False
        self._in_skip = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in ("script", "style"):
            self._in_skip = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in ("script", "style"):
            self._in_skip = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title = text
        elif not self._in_skip:
            self.chunks.append(text)


def _default_fetcher():
    from scrapling.fetchers import Fetcher

    return Fetcher()


def _try_parse_date(date_str: str) -> datetime | None:
    """Try to parse *date_str* into a datetime, returning ``None`` on failure."""
    try:
        from dateutil.parser import parse as parse_date

        return parse_date(date_str)
    except Exception:
        return None


def _extract_published_at(page) -> datetime | None:
    """Extract a publish date from a Scrapling Response using multiple strategies.

    Strategies are tried in priority order — the first successful extraction
    wins:

    1. ``<time datetime="...">`` elements via CSS selector
    2. ``<meta>`` tags (``article:published_time``, ``DC.date``, ``date``, etc.)
    3. JSON-LD structured data (``datePublished`` / ``dateModified``)
    4. HTTP ``Last-Modified`` response header
    """
    # ── Strategy 1: <time datetime> elements ──────────────────────
    try:
        time_els = page.css("time[datetime]")
        if time_els:
            dt_str = (time_els[0].attrib or {}).get("datetime", "")
            if dt_str:
                result = _try_parse_date(dt_str)
                if result:
                    logger.debug("date from <time datetime>: %s", dt_str)
                    return result
    except Exception:
        pass

    # ── Strategy 2: <meta> tags ───────────────────────────────────
    html = getattr(page, "html_content", None) or getattr(page, "body", "") or ""
    if html:
        for pattern, label in _META_DATE_PATTERNS:
            m = re.search(pattern, html, re.IGNORECASE)
            if m:
                result = _try_parse_date(m.group(1))
                if result:
                    logger.debug("date from <meta %s>: %s", label, m.group(1))
                    return result

    # ── Strategy 3: JSON-LD structured data ───────────────────────
    if html:
        jsonld_matches = re.findall(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        for raw_json in jsonld_matches:
            try:
                data = json.loads(raw_json)
                # Handle both a single object and @graph arrays.
                items: list[dict] = []
                if isinstance(data, dict):
                    items = [data]
                elif isinstance(data, list):
                    items = data  # type: ignore[assignment]
                for item in items:
                    dp = item.get("datePublished") or item.get("dateModified")
                    if dp:
                        result = _try_parse_date(dp)
                        if result:
                            logger.debug("date from JSON-LD: %s", dp)
                            return result
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue

    # ── Strategy 4: HTTP Last-Modified header ─────────────────────
    headers = getattr(page, "headers", None)
    if isinstance(headers, dict):
        lm = headers.get("last-modified")
        if lm:
            result = _try_parse_date(lm)
            if result:
                logger.debug("date from Last-Modified header: %s", lm)
                return result

        # Some CDNs use a lowercase 'date' header for the response time,
        # which can be a rough proxy for static pages.
        date_hdr = headers.get("date")
        if date_hdr:
            result = _try_parse_date(date_hdr)
            if result:
                logger.debug("date from Date header (fallback): %s", date_hdr)
                return result

    return None


class ScraplingExtractor(ContentExtractor):
    def __init__(self, fetcher=None):
        self._fetcher = fetcher or _default_fetcher()

    def extract(self, url: str) -> ExtractedDoc:
        page = self._fetch_page(url)
        html = getattr(page, "html_content", None) or getattr(page, "body", "") or ""
        parser = _TextExtractor()
        parser.feed(html)
        clean = re.sub(r"\s+", " ", " ".join(parser.chunks)).strip()
        published_at = _extract_published_at(page)
        return ExtractedDoc(
            url=url,
            title=parser.title,
            clean_content=clean,
            published_at=published_at,
        )

    def _fetch_page(self, url: str):
        """Call the underlying fetcher, supporting both fetch() and get() APIs."""
        if hasattr(self._fetcher, "fetch"):
            return self._fetcher.fetch(url)
        elif hasattr(self._fetcher, "get"):
            return self._fetcher.get(url)
        else:
            raise AttributeError("configured fetcher must expose fetch() or get()")
