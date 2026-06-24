import json
import logging
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

from app.extract.base import ContentExtractor
from app.schemas import ExtractedDoc

logger = logging.getLogger(__name__)

# Meta-tag attribute identifiers for date extraction.
# Each tuple is (attr_type, attr_value) where attr_type is "property" or "name".
# Both attribute orders are tried automatically.
_META_DATE_ATTRS: list[tuple[str, str]] = [
    ("property", "article:published_time"),
    ("property", "article:modified_time"),
    ("name", r"dc\.date"),
    ("name", r"dcterms\.created"),
    ("name", r"dcterms\.modified"),
    ("name", "date"),
    ("name", "pubdate"),
    ("name", "last-modified"),
    ("http-equiv", "last-modified"),
]

# Pre-built regex list — populated at import time.
_META_DATE_PATTERNS: list[tuple[str, str]] = []
for _attr_type, _attr_value in _META_DATE_ATTRS:
    # Forward: property="X" ... content="Y"
    _META_DATE_PATTERNS.append((
        rf'<meta[^>]+{_attr_type}="{_attr_value}"[^>]+content="([^"]+)"',
        f"{_attr_type}={_attr_value}",
    ))
    # Reverse: content="Y" ... property="X"
    _META_DATE_PATTERNS.append((
        rf'<meta[^>]+content="([^"]+)"[^>]+{_attr_type}="{_attr_value}"',
        f"{_attr_type}={_attr_value} (rev)",
    ))

# Regex for Strategy 2.5: elements with class/itemprop containing date-related keywords,
# capturing their text content or datetime attribute.
_DATE_CLASS_PATTERNS: list[tuple[str, str]] = [
    # <time datetime="...">
    (r'<time[^>]+datetime="([^"]+)"[^>]*>', "time[datetime]"),
    # <* class="...date..." itemprop="datePublished">text</*>
    (
        r'<(?:span|div|p|time)[^>]*\b'
        r'(?:class|itemprop|property)\s*=\s*"[^"]*'
        r'(?:date|time|publish|published|created|updated|modified|post-date|entry-date)'
        r'[^"]*"[^>]*>'
        r'\s*([^<]{4,40}?)\s*</(?:span|div|p|time)>',
        "date-class",
    ),
    # <meta itemprop="datePublished" content="...">
    (
        r'<meta[^>]+itemprop="datePublished"[^>]+content="([^"]+)"',
        "itemprop=datePublished",
    ),
    (
        r'<meta[^>]+content="([^"]+)"[^>]+itemprop="datePublished"',
        "itemprop=datePublished (rev)",
    ),
    # <meta itemprop="dateModified" content="...">
    (
        r'<meta[^>]+itemprop="dateModified"[^>]+content="([^"]+)"',
        "itemprop=dateModified",
    ),
    (
        r'<meta[^>]+content="([^"]+)"[^>]+itemprop="dateModified"',
        "itemprop=dateModified (rev)",
    ),
]

# Regex for Strategy 5: URL path date extraction.
# Matches /YYYY/MM/DD/ or /YYYY-MM-DD or /YYYY/MM/ patterns.
_URL_DATE_PATTERNS: list[tuple[str, str]] = [
    (r"/(\d{4})/(\d{2})/(\d{2})/", "url /YYYY/MM/DD/"),
    (r"/(\d{4})/(\d{2})/", "url /YYYY/MM/"),
    (r"/(\d{4})-(\d{2})-(\d{2})", "url /YYYY-MM-DD"),
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
        if self._in_title and self.title is None:
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


def _jsonld_walk(obj, key: str = "datePublished") -> datetime | None:
    """Recursively walk a JSON-LD object/array looking for *key*.

    Handles ``@graph`` arrays, nested ``mainEntity`` / ``hasPart``, and
    plain arrays of objects.
    """
    if isinstance(obj, dict):
        # Direct hit
        dp = obj.get(key)
        if dp:
            result = _try_parse_date(str(dp))
            if result:
                return result
        # @graph array inside a dict
        graph = obj.get("@graph")
        if isinstance(graph, list):
            result = _jsonld_walk(graph, key)
            if result:
                return result
        # Nested entities
        for nest_key in ("mainEntity", "hasPart", "mainEntityOfPage"):
            nested = obj.get(nest_key)
            if nested:
                result = _jsonld_walk(nested, key)
                if result:
                    return result
    elif isinstance(obj, list):
        for item in obj:
            result = _jsonld_walk(item, key)
            if result:
                return result
    return None


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_head_title(html: str) -> str | None:
    try:
        from lxml import html as lxml_html

        tree = lxml_html.fromstring(html)
        title_el = tree.find(".//head/title")
        if title_el is not None:
            title = _normalize_text(title_el.text_content())
            if title:
                return title
    except Exception:
        pass
    return None


def _extract_article_content(html: str) -> str | None:
    """Extract likely article body before falling back to whole-page text.

    Many vendor sites include large navigation trees, SVG icon titles, and
    shadow-DOM templates before the article. Feeding that prefix to the quality
    model makes real articles look like empty navigation pages.
    """
    try:
        from lxml import html as lxml_html
    except Exception:
        return None

    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return None

    for bad in tree.xpath(
        "//script|//style|//noscript|//template|//svg|//nav|//header|//footer|//form|//aside"
    ):
        bad.drop_tree()

    # Custom elements such as Red Hat's rh-navigation-primary are not <nav>
    # tags, but they still expose navigation text before the article.
    for bad in tree.xpath(
        '//*[contains(translate(local-name(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "navigation")]'
    ):
        bad.drop_tree()

    selectors = [
        '[property="schema:text"]',
        '[itemprop="articleBody"]',
        ".field--name-body",
        ".article-body",
        ".blog-post__content",
        ".entry-content",
        ".post-content",
        "article",
        "main",
    ]
    best = ""
    for selector in selectors:
        try:
            elements = tree.cssselect(selector)
        except Exception:
            continue
        for element in elements:
            text = _normalize_text(element.text_content())
            if len(text) > len(best):
                best = text
        if len(best) >= 500:
            return best

    return best if len(best) >= 200 else None


def _extract_published_at(page, url: str = "") -> datetime | None:
    """Extract a publish date from a Scrapling Response using multiple strategies.

    Strategies are tried in priority order — the first successful extraction
    wins:

    1. ``<time datetime="...">`` elements via CSS selector
    2. ``<meta>`` tags (both attribute orders)
    2.5 Elements with date-related class/itemprop
    3. JSON-LD structured data (recursive @graph / mainEntity / hasPart)
    4. HTTP ``Last-Modified`` response header
    5. URL path patterns (e.g. ``/2026/06/12/article-title``)
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

    # ── Strategy 2: <meta> tags (both attribute orders) ───────────
    html = getattr(page, "html_content", None) or getattr(page, "body", "") or ""
    if html:
        for pattern, label in _META_DATE_PATTERNS:
            m = re.search(pattern, html, re.IGNORECASE)
            if m:
                result = _try_parse_date(m.group(1))
                if result:
                    logger.debug("date from <meta %s>: %s", label, m.group(1))
                    return result

    # ── Strategy 2.5: date-related class/itemprop elements ────────
    if html:
        for pattern, label in _DATE_CLASS_PATTERNS:
            m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if m:
                result = _try_parse_date(m.group(1).strip())
                if result:
                    logger.debug("date from %s: %s", label, m.group(1).strip())
                    return result

    # ── Strategy 3: JSON-LD structured data (recursive) ───────────
    if html:
        jsonld_matches = re.findall(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        for raw_json in jsonld_matches:
            try:
                data = json.loads(raw_json)
                # Try datePublished first, then dateModified.
                result = _jsonld_walk(data, "datePublished") or _jsonld_walk(data, "dateModified")
                if result:
                    logger.debug("date from JSON-LD")
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

    # ── Strategy 5: URL path date patterns ────────────────────────
    if url:
        url_path = urlparse(url).path
        for pattern, label in _URL_DATE_PATTERNS:
            m = re.search(pattern, url_path)
            if m:
                try:
                    parts = [int(g) for g in m.groups()]
                    if len(parts) == 3:
                        result = datetime(parts[0], parts[1], parts[2])
                        logger.debug("date from %s in URL: %s", label, result)
                        return result
                    elif len(parts) == 2:
                        # /YYYY/MM — assume 1st of the month.
                        result = datetime(parts[0], parts[1], 1)
                        logger.debug("date from %s in URL: %s (assumed 1st)", label, result)
                        return result
                except (ValueError, IndexError):
                    continue

    return None


class ScraplingExtractor(ContentExtractor):
    def __init__(self, fetcher=None, use_stealth: bool = False):
        if fetcher:
            self._fetcher = fetcher
        elif use_stealth:
            from scrapling.fetchers import StealthyFetcher

            self._fetcher = StealthyFetcher()
        else:
            self._fetcher = _default_fetcher()

    def extract(self, url: str) -> ExtractedDoc:
        page = self._fetch_page(url)
        html = getattr(page, "html_content", None) or getattr(page, "body", "") or ""
        parser = _TextExtractor()
        parser.feed(html)
        title = _extract_head_title(html) or parser.title
        clean = _extract_article_content(html) or _normalize_text(" ".join(parser.chunks))
        published_at = _extract_published_at(page, url=url)
        if published_at is None:
            logger.warning("no date extracted for %s — all strategies exhausted", url)
        return ExtractedDoc(
            url=url,
            title=title,
            clean_content=clean,
            published_at=published_at,
        )

    def extract_list_items(
        self,
        url: str,
        link_selector: str,
        title_selector: str | None = None,
        date_selector: str | None = None,
    ) -> list[dict]:
        """Extract article links from a list page using CSS selectors.

        Returns a list of dicts with keys ``url``, ``title``, ``date_str``.
        Used by ``PageMonitorFetcher`` in list-page mode (方案2).
        """
        from lxml import html as lxml_html
        from urllib.parse import urljoin

        page = self._fetch_page(url)
        html_content = getattr(page, "html_content", None) or getattr(page, "body", "") or ""

        try:
            tree = lxml_html.fromstring(html_content)
        except Exception:
            logger.warning("Failed to parse HTML for list page %s", url)
            return []

        results: list[dict] = []
        link_elements = tree.cssselect(link_selector)

        for link_el in link_elements:
            href = (link_el.get("href") or "").strip()
            if not href:
                continue

            full_url = urljoin(url, href)

            # ── Title ──────────────────────────────────────────
            title: str | None = None
            if title_selector:
                try:
                    title_els = link_el.cssselect(title_selector)
                    if title_els:
                        title = title_els[0].text_content().strip()
                except Exception:
                    pass
            if not title:
                title = link_el.text_content().strip()
            if not title:
                title = full_url

            # ── Date ───────────────────────────────────────────
            date_str: str | None = None
            if date_selector:
                try:
                    # Try inside the link element first
                    date_els = link_el.cssselect(date_selector)
                    # Then try the parent element
                    parent = link_el.getparent()
                    if not date_els and parent is not None:
                        date_els = parent.cssselect(date_selector)
                    # Fall back to the nearest ancestor container that also
                    # contains a matching date element, such as Gitee release
                    # cards where the timestamp is a sibling of the body block.
                    if not date_els:
                        ancestor = parent.getparent() if parent is not None else None
                        while ancestor is not None:
                            date_els = ancestor.cssselect(date_selector)
                            if date_els:
                                break
                            ancestor = ancestor.getparent()
                    if date_els:
                        dt_attr = date_els[0].get("datetime", "")
                        date_str = dt_attr if dt_attr else date_els[0].text_content().strip()
                except Exception:
                    pass

            results.append({"url": full_url, "title": title, "date_str": date_str})

        logger.debug("extract_list_items: found %d links for %s", len(results), url)
        return results

    def _fetch_page(self, url: str):
        """Call the underlying fetcher, supporting both fetch() and get() APIs."""
        if hasattr(self._fetcher, "fetch"):
            return self._fetcher.fetch(url)
        elif hasattr(self._fetcher, "get"):
            return self._fetcher.get(url)
        else:
            raise AttributeError("configured fetcher must expose fetch() or get()")
