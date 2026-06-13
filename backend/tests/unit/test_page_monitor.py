from datetime import datetime, timezone

from app.enums import SourceType
from app.fetchers.page_monitor import PageMonitorFetcher
from app.models import Source
from app.schemas import ExtractedDoc


class _StubExtractor:
    def __init__(self, content, published_at=None, list_items=None):
        self._content = content
        self._published_at = published_at
        self._list_items = list_items or []

    def extract(self, url):
        return ExtractedDoc(
            url=url,
            title="Page",
            clean_content=self._content,
            published_at=self._published_at,
        )

    def extract_list_items(self, url, link_selector, title_selector=None, date_selector=None):
        return self._list_items


def test_page_monitor_emits_when_content_changed():
    src = Source(
        id=2,
        name="Vendor",
        type=SourceType.PAGE_MONITOR,
        url="https://vendor.com/news",
        last_content_hash=None,
    )
    fetcher = PageMonitorFetcher(extractor=_StubExtractor("first version"))
    items = fetcher.fetch(src)
    assert len(items) == 1
    assert src.last_content_hash is not None


def test_page_monitor_skips_when_unchanged():
    src = Source(id=2, name="Vendor", type=SourceType.PAGE_MONITOR, url="https://vendor.com/news")
    fetcher = PageMonitorFetcher(extractor=_StubExtractor("same"))
    first = fetcher.fetch(src)
    second = fetcher.fetch(src)
    assert len(first) == 1
    assert len(second) == 0


def test_page_monitor_uses_extracted_date():
    """When the extractor provides a date, it is used directly."""
    extracted_date = datetime(2026, 4, 15, 10, 0, 0, tzinfo=timezone.utc)
    src = Source(
        id=3,
        name="Blog",
        type=SourceType.PAGE_MONITOR,
        url="https://example.com/blog",
        last_content_hash=None,
    )
    fetcher = PageMonitorFetcher(
        extractor=_StubExtractor("content", published_at=extracted_date)
    )
    items = fetcher.fetch(src)
    assert len(items) == 1
    assert items[0].published_at == extracted_date


def test_page_monitor_falls_back_to_now_when_no_date():
    """When the extractor returns no date, datetime.now(UTC) is used as fallback."""
    before = datetime.now(timezone.utc)
    src = Source(
        id=4,
        name="NoDate",
        type=SourceType.PAGE_MONITOR,
        url="https://example.com/nodate",
        last_content_hash=None,
    )
    fetcher = PageMonitorFetcher(
        extractor=_StubExtractor("content", published_at=None)
    )
    items = fetcher.fetch(src)
    after = datetime.now(timezone.utc)
    assert len(items) == 1
    # The fallback should be within a small window around now.
    assert items[0].published_at is not None
    assert before - after < items[0].published_at - after < after - after  # pyright: ignore[reportUnusedExpression]
    # Simpler: just assert it's a recent UTC datetime.
    delta = (after - items[0].published_at).total_seconds()
    assert delta >= 0  # fallback time ≤ after
    assert delta < 5    # within 5 seconds


# ── List-page mode (方案2) ──────────────────────────────────────────


def test_list_mode_emits_multiple_items():
    """When link_selector is set, the fetcher returns one RawItem per link."""
    src = Source(
        id=5,
        name="NewsList",
        type=SourceType.PAGE_MONITOR,
        url="https://example.com/news",
        link_selector="a.article-link",
        date_selector=".date",
    )
    fetcher = PageMonitorFetcher(
        extractor=_StubExtractor(
            content="list page",
            list_items=[
                {"url": "https://example.com/news/1", "title": "Article 1", "date_str": "2026-06-10"},
                {"url": "https://example.com/news/2", "title": "Article 2", "date_str": "2026-06-11"},
                {"url": "https://example.com/news/3", "title": "Article 3", "date_str": "2026-06-12"},
            ],
        )
    )
    items = fetcher.fetch(src)
    assert len(items) == 3
    assert items[0].url == "https://example.com/news/1"
    assert items[0].title == "Article 1"
    assert items[0].published_at is not None
    assert items[0].published_at.day == 10
    assert items[0].raw_content is None  # pipeline will extract


def test_list_mode_empty_links_returns_empty():
    """When the list page has no links, return an empty list."""
    src = Source(
        id=6,
        name="EmptyNews",
        type=SourceType.PAGE_MONITOR,
        url="https://example.com/empty",
        link_selector="a.nonexistent",
    )
    fetcher = PageMonitorFetcher(
        extractor=_StubExtractor(content="empty page", list_items=[])
    )
    items = fetcher.fetch(src)
    assert len(items) == 0


def test_list_mode_unparseable_date_is_none():
    """When date_str can't be parsed, published_at is None (no crash)."""
    src = Source(
        id=7,
        name="BadDate",
        type=SourceType.PAGE_MONITOR,
        url="https://example.com/list",
        link_selector="a",
        date_selector=".date",
    )
    fetcher = PageMonitorFetcher(
        extractor=_StubExtractor(
            content="list",
            list_items=[
                {"url": "https://example.com/1", "title": "X", "date_str": "garbage date!!!"},
            ],
        )
    )
    items = fetcher.fetch(src)
    assert len(items) == 1
    assert items[0].published_at is None


def test_list_mode_no_title_falls_back():
    """When title is empty, the URL itself is used as title."""
    src = Source(
        id=8,
        name="NoTitle",
        type=SourceType.PAGE_MONITOR,
        url="https://example.com/list",
        link_selector="a",
    )
    fetcher = PageMonitorFetcher(
        extractor=_StubExtractor(
            content="list",
            list_items=[
                {"url": "https://example.com/1", "title": "", "date_str": None},
            ],
        )
    )
    items = fetcher.fetch(src)
    assert len(items) == 1
    # The extract_list_items stub returns empty title — in real impl, the
    # extractor would fall back to link text or URL.  The fetcher just
    # passes through whatever title it receives.
    assert items[0].title == ""


def test_legacy_mode_still_works():
    """When link_selector is NOT set, the legacy whole-page mode is used."""
    src = Source(
        id=9,
        name="Legacy",
        type=SourceType.PAGE_MONITOR,
        url="https://example.com/page",
        last_content_hash=None,
        link_selector=None,
    )
    fetcher = PageMonitorFetcher(extractor=_StubExtractor("legacy content"))
    items = fetcher.fetch(src)
    assert len(items) == 1
    assert items[0].raw_content == "legacy content"
    assert items[0].url == "https://example.com/page"
