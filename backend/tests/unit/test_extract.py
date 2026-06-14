import re
import sys
import types
from datetime import datetime, timezone

from app.extract.base import ContentExtractor
from app.extract.scrapling_extractor import (
    ScraplingExtractor,
    _default_fetcher,
    _extract_published_at,
    _jsonld_walk,
    _try_parse_date,
)


class _FakeFetcher:
    def __init__(self, html, headers=None):
        self._html = html
        self._headers = headers or {}

    def fetch(self, url, **kw):
        return _FakePage(self._html, self._headers)


class _FakePage:
    def __init__(self, html, headers=None):
        self.html_content = html
        self.body = html
        self.headers = headers or {}
        self.attrib = {}

    def css(self, selector):
        """Minimal css() stub — returns matching fake elements."""
        # Support 'time[datetime]' selector for date extraction tests.
        if selector == "time[datetime]":
            matches = re.findall(
                r'<time[^>]*datetime="([^"]*)"[^>]*>', self.html_content
            )
            return [_FakeTimeEl(dt) for dt in matches]
        return []


class _FakeTimeEl:
    def __init__(self, datetime_val):
        self.attrib = {"datetime": datetime_val}


# ── Text extraction (existing) ────────────────────────────────────


def test_scrapling_extractor_extracts_text(monkeypatch):
    html = "<html><head><title>Hi</title></head><body><article><p>Hello world body</p></article></body></html>"
    ext = ScraplingExtractor(fetcher=_FakeFetcher(html))
    doc = ext.extract("https://x.com/a")
    assert doc.url == "https://x.com/a"
    assert "Hello world body" in doc.clean_content
    assert doc.title == "Hi"


def test_extractor_is_protocol_instance():
    assert isinstance(
        ScraplingExtractor(fetcher=_FakeFetcher("<html></html>")), ContentExtractor
    )


def test_default_fetcher_factory_instantiates_fetcher(monkeypatch):
    class _FetcherImpl:
        def fetch(self, url, **kw):
            class _Page:
                html_content = "<html><body>ok</body></html>"

            return _Page()

    module = types.ModuleType("scrapling.fetchers")
    module.Fetcher = _FetcherImpl
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", module)

    fetcher = _default_fetcher()
    assert isinstance(fetcher, _FetcherImpl)


def test_scrapling_extractor_supports_get_based_fetcher():
    class _GetFetcher:
        def get(self, url, **kw):
            class _Page:
                html_content = (
                    "<html><head><title>Hi</title></head><body>From get</body></html>"
                )

            return _Page()

    doc = ScraplingExtractor(fetcher=_GetFetcher()).extract("https://x.com/a")
    assert doc.title == "Hi"
    assert "From get" in doc.clean_content


# ── Date extraction ───────────────────────────────────────────────


class TestTryParseDate:
    def test_iso_8601(self):
        result = _try_parse_date("2026-04-26T11:33:46Z")
        assert result is not None
        assert result.year == 2026
        assert result.month == 4
        assert result.day == 26

    def test_rfc_2822(self):
        result = _try_parse_date("Thu, 07 May 2026 18:21:19 GMT")
        assert result is not None
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 7

    def test_garbage_returns_none(self):
        assert _try_parse_date("not a date") is None

    def test_empty_returns_none(self):
        assert _try_parse_date("") is None


class TestExtractPublishedAt:
    def test_from_time_datetime_element(self):
        html = '<html><body><time datetime="2026-04-26T11:33:46Z">Apr 26</time></body></html>'
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2026
        assert result.month == 4
        assert result.day == 26

    def test_from_article_published_time_meta(self):
        html = '<html><head><meta property="article:published_time" content="2026-01-15T08:00:00Z"></head><body></body></html>'
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 15

    def test_from_dc_date_meta(self):
        html = '<html><head><meta name="DC.date" content="2025-12-01"></head><body></body></html>'
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2025
        assert result.month == 12
        assert result.day == 1

    def test_from_date_meta(self):
        html = '<html><head><meta name="date" content="2025-06-15"></head><body></body></html>'
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2025
        assert result.month == 6
        assert result.day == 15

    def test_from_jsonld_date_published(self):
        html = """<html><head>
<script type="application/ld+json">
{"datePublished": "2026-02-20T14:30:00Z", "headline": "Test"}
</script></head><body></body></html>"""
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2026
        assert result.month == 2
        assert result.day == 20

    def test_from_jsonld_date_modified(self):
        html = """<html><head>
<script type="application/ld+json">
{"dateModified": "2025-10-05"}
</script></head><body></body></html>"""
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2025
        assert result.month == 10
        assert result.day == 5

    def test_from_jsonld_graph_array(self):
        html = """<html><head>
<script type="application/ld+json">
[{"@type": "WebPage", "datePublished": "2026-03-01T10:00:00Z"}, {"@type": "Article", "datePublished": "2026-03-02T12:00:00Z"}]
</script></head><body></body></html>"""
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        # Should pick the first one with datePublished
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 1

    def test_from_last_modified_header(self):
        page = _FakePage("<html></html>", headers={"last-modified": "Thu, 07 May 2026 18:21:19 GMT"})
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 7

    def test_from_date_header_fallback(self):
        page = _FakePage("<html></html>", headers={"date": "Fri, 12 Jun 2026 07:14:56 GMT"})
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2026
        assert result.month == 6
        assert result.day == 12

    def test_priority_time_over_last_modified(self):
        """<time datetime> should win over Last-Modified header."""
        html = '<html><body><time datetime="2026-01-01T00:00:00Z">Jan 1</time></body></html>'
        page = _FakePage(html, headers={"last-modified": "Thu, 07 May 2026 18:21:19 GMT"})
        result = _extract_published_at(page)
        assert result is not None
        assert result.month == 1  # time element wins

    def test_priority_meta_over_header(self):
        """<meta> date should win over Last-Modified header."""
        html = '<html><head><meta name="date" content="2025-03-15"></head><body></body></html>'
        page = _FakePage(html, headers={"last-modified": "Thu, 07 May 2026 18:21:19 GMT"})
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2025  # meta wins

    def test_no_date_signals_returns_none(self):
        page = _FakePage("<html><body>no dates here</body></html>")
        result = _extract_published_at(page)
        assert result is None

    def test_extracted_doc_includes_published_at(self):
        html = '<html><head><title>T</title></head><body><p>content</p><time datetime="2026-05-01T00:00:00Z"></time></body></html>'
        doc = ScraplingExtractor(fetcher=_FakeFetcher(html)).extract("https://x.com/a")
        assert doc.url == "https://x.com/a"
        assert doc.published_at is not None
        assert doc.published_at.year == 2026
        assert doc.published_at.month == 5
        assert doc.published_at.day == 1

    def test_extracted_doc_published_at_none_when_no_signals(self):
        html = "<html><head><title>T</title></head><body><p>content</p></body></html>"
        doc = ScraplingExtractor(fetcher=_FakeFetcher("<html></html>")).extract(
            "https://x.com/a"
        )
        assert doc.published_at is None

    # ── Meta tag reverse attribute order ────────────────────────────

    def test_meta_content_before_property(self):
        """<meta content="..." property="..."> — reverse attribute order."""
        html = '<html><head><meta content="2026-03-15T08:00:00Z" property="article:published_time"></head><body></body></html>'
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 15

    def test_meta_content_before_name(self):
        """<meta content="..." name="date"> — reverse attribute order for name attrs."""
        html = '<html><head><meta content="2025-09-01" name="date"></head><body></body></html>'
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2025
        assert result.month == 9
        assert result.day == 1

    # ── Strategy 2.5: date-related class/itemprop patterns ─────────

    def test_from_itemprop_date_published_meta(self):
        """<meta itemprop="datePublished" content="...">"""
        html = '<html><head><meta itemprop="datePublished" content="2026-05-10T12:00:00Z"></head><body></body></html>'
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 10

    def test_from_date_class_element(self):
        """<span class="post-date">Jan 15, 2026</span>"""
        html = '<html><body><article><span class="post-date">Jan 15, 2026</span></article></body></html>'
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 15

    def test_from_publish_class_div(self):
        """<div class="entry-published">2026-04-01</div>"""
        html = '<html><body><div class="entry-published">2026-04-01</div></body></html>'
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.month == 4
        assert result.day == 1

    def test_from_time_element_with_date_class(self):
        """<time class="dt-published" datetime="...">"""
        html = '<html><body><time class="dt-published" datetime="2026-02-28T00:00:00Z">Feb 28</time></body></html>'
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.month == 2
        assert result.day == 28

    # ── Strategy 3: JSON-LD @graph in dict form ────────────────────

    def test_from_jsonld_graph_dict(self):
        """JSON-LD with @graph as a key in a dict."""
        html = """<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [{"@type": "Article", "datePublished": "2026-06-01T10:00:00Z"}]}
</script></head><body></body></html>"""
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2026
        assert result.month == 6
        assert result.day == 1

    def test_from_jsonld_nested_main_entity(self):
        """JSON-LD with nested mainEntity containing the date."""
        html = """<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "mainEntity": {"@type": "Article", "datePublished": "2026-03-15"}}
</script></head><body></body></html>"""
        page = _FakePage(html)
        result = _extract_published_at(page)
        assert result is not None
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 15

    # ── Strategy 5: URL path date extraction ───────────────────────

    def test_from_url_ymd_path(self):
        """URL containing /YYYY/MM/DD/ pattern."""
        html = "<html><body>no other dates</body></html>"
        page = _FakePage(html)
        result = _extract_published_at(page, url="https://example.com/news/2026/06/12/article-title")
        assert result is not None
        assert result.year == 2026
        assert result.month == 6
        assert result.day == 12

    def test_from_url_ym_path(self):
        """URL containing /YYYY/MM/ pattern (assume 1st of month)."""
        html = "<html><body>no other dates</body></html>"
        page = _FakePage(html)
        result = _extract_published_at(page, url="https://example.com/archive/2026/03/")
        assert result is not None
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 1

    def test_from_url_ymd_dash(self):
        """URL containing YYYY-MM-DD pattern."""
        html = "<html><body>no other dates</body></html>"
        page = _FakePage(html)
        result = _extract_published_at(page, url="https://example.com/2025-11-20-release-notes")
        assert result is not None
        assert result.year == 2025
        assert result.month == 11
        assert result.day == 20

    def test_url_date_lower_priority_than_meta(self):
        """<meta> date wins over URL path date (strategy 2 > strategy 5)."""
        html = '<html><head><meta name="date" content="2025-01-15"></head><body></body></html>'
        page = _FakePage(html)
        result = _extract_published_at(page, url="https://example.com/2026/06/12/article")
        assert result is not None
        assert result.year == 2025  # meta wins over URL

    def test_url_date_invalid_path_returns_none(self):
        """URL without date patterns should not produce false positives."""
        html = "<html><body>no dates</body></html>"
        page = _FakePage(html)
        result = _extract_published_at(page, url="https://example.com/about/us")
        assert result is None

    # ── _jsonld_walk unit tests ────────────────────────────────────

    def test_jsonld_walk_direct_hit(self):
        result = _jsonld_walk({"datePublished": "2026-01-01"}, key="datePublished")
        assert result is not None
        assert result.year == 2026
        assert result.month == 1

    def test_jsonld_walk_graph_in_dict(self):
        data = {"@graph": [{"datePublished": "2026-06-01"}]}
        result = _jsonld_walk(data, key="datePublished")
        assert result is not None
        assert result.month == 6

    def test_jsonld_walk_main_entity(self):
        data = {"mainEntity": {"datePublished": "2026-02-14"}}
        result = _jsonld_walk(data, key="datePublished")
        assert result is not None
        assert result.day == 14

    def test_jsonld_walk_deeply_nested(self):
        data = {"mainEntity": {"hasPart": {"@graph": [{"dateModified": "2025-12-25"}]}}}
        result = _jsonld_walk(data, key="dateModified")
        assert result is not None
        assert result.month == 12
        assert result.day == 25

    def test_jsonld_walk_empty_returns_none(self):
        result = _jsonld_walk({}, key="datePublished")
        assert result is None


# ── List-page item extraction (方案2) ───────────────────────────────


class TestExtractListItems:
    def test_extracts_links_from_list_page(self):
        """extract_list_items extracts article links using CSS selectors."""
        html = """<html><body><ul class="news-list">
<li class="news-item"><a class="title" href="/news/1">Article One</a><span class="date">2026-06-10</span></li>
<li class="news-item"><a class="title" href="/news/2">Article Two</a><span class="date">2026-06-11</span></li>
<li class="news-item"><a class="title" href="http://other.com/news/3">External</a><span class="date">2026-06-12</span></li>
</ul></body></html>"""
        extractor = ScraplingExtractor(fetcher=_FakeFetcher(html))
        results = extractor.extract_list_items(
            "https://example.com/news/",
            link_selector="a.title",
            date_selector=".date",
        )
        assert len(results) == 3
        assert results[0]["url"] == "https://example.com/news/1"
        assert results[0]["title"] == "Article One"
        assert results[0]["date_str"] == "2026-06-10"
        # External URL should be preserved as-is (absolute).
        assert results[2]["url"] == "http://other.com/news/3"

    def test_extracts_links_with_title_selector(self):
        """title_selector can extract title from a child element."""
        html = """<html><body><div class="list">
<div class="item"><a href="/post/1"><h3 class="headline">The Title</h3><p class="desc">Description</p></a></div>
</div></body></html>"""
        extractor = ScraplingExtractor(fetcher=_FakeFetcher(html))
        results = extractor.extract_list_items(
            "https://example.com/",
            link_selector=".item a",
            title_selector="h3.headline",
        )
        assert len(results) == 1
        assert results[0]["title"] == "The Title"

    def test_empty_list_page_returns_empty(self):
        """A page with no matching links returns an empty list."""
        html = "<html><body><p>No links here</p></body></html>"
        extractor = ScraplingExtractor(fetcher=_FakeFetcher(html))
        results = extractor.extract_list_items(
            "https://example.com/",
            link_selector="a.nonexistent",
        )
        assert results == []

    def test_skip_links_without_href(self):
        """Links without href are skipped."""
        html = '<html><body><a>no href</a><a href="/ok">OK</a></body></html>'
        extractor = ScraplingExtractor(fetcher=_FakeFetcher(html))
        results = extractor.extract_list_items(
            "https://example.com/",
            link_selector="a",
        )
        assert len(results) == 1
        assert results[0]["url"] == "https://example.com/ok"

    def test_date_from_datetime_attribute(self):
        """date_selector prefers datetime attribute over text."""
        html = """<html><body><div class="list">
<div class="item"><a href="/post/1">Title</a><time class="date" datetime="2026-01-15T10:00:00Z">Jan 15</time></div>
</div></body></html>"""
        extractor = ScraplingExtractor(fetcher=_FakeFetcher(html))
        results = extractor.extract_list_items(
            "https://example.com/",
            link_selector="a",
            date_selector=".date",
        )
        assert len(results) == 1
        assert results[0]["date_str"] == "2026-01-15T10:00:00Z"

    def test_date_from_nearest_ancestor_container(self):
        """date_selector can be found on a sibling within the nearest ancestor container."""
        html = """<html><body><div class="releases">
<div class="release-card">
  <div class="d-flex-between">
    <div class="release-time" data-commit-date="2024-04-17 06:13:28 +0000">2024-04-17 14:21</div>
  </div>
  <div class="release-body">
    <div class="release-header">
      <a class="title" href="/repo/releases/tag/v1">Release One</a>
    </div>
  </div>
</div>
</div></body></html>"""
        extractor = ScraplingExtractor(fetcher=_FakeFetcher(html))
        results = extractor.extract_list_items(
            "https://example.com/repo/releases",
            link_selector="a.title",
            date_selector=".release-time",
        )
        assert len(results) == 1
        assert results[0]["date_str"] == "2024-04-17 14:21"


# ── Stealth mode (方案5) ───────────────────────────────────────────


def test_scrapling_extractor_stealth_init():
    """use_stealth=True creates a StealthyFetcher."""
    ext = ScraplingExtractor(use_stealth=True)
    from scrapling.fetchers import StealthyFetcher
    assert isinstance(ext._fetcher, StealthyFetcher)


def test_scrapling_extractor_default_init():
    """Default init uses the regular Fetcher."""
    ext = ScraplingExtractor()
    from scrapling.fetchers import Fetcher as ScraplingFetcher
    assert isinstance(ext._fetcher, ScraplingFetcher)


def test_scrapling_extractor_custom_fetcher():
    """Custom fetcher overrides stealth setting."""
    custom = _FakeFetcher("<html></html>")
    ext = ScraplingExtractor(fetcher=custom, use_stealth=True)
    # The custom fetcher should be used, not StealthyFetcher.
    assert ext._fetcher is custom
