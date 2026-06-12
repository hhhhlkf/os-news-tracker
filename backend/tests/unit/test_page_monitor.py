from datetime import datetime, timezone

from app.enums import SourceType
from app.fetchers.page_monitor import PageMonitorFetcher
from app.models import Source
from app.schemas import ExtractedDoc


class _StubExtractor:
    def __init__(self, content, published_at=None):
        self._content = content
        self._published_at = published_at

    def extract(self, url):
        return ExtractedDoc(
            url=url,
            title="Page",
            clean_content=self._content,
            published_at=self._published_at,
        )


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
