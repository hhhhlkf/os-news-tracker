import re
import sys
import types
from datetime import datetime, timezone

from app.extract.base import ContentExtractor
from app.extract.scrapling_extractor import (
    ScraplingExtractor,
    _default_fetcher,
    _extract_published_at,
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
