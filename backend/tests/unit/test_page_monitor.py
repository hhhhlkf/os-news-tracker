from app.enums import SourceType
from app.fetchers.page_monitor import PageMonitorFetcher
from app.models import Source
from app.schemas import ExtractedDoc


class _StubExtractor:
    def __init__(self, content):
        self._content = content

    def extract(self, url):
        return ExtractedDoc(url=url, title="Page", clean_content=self._content)


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
