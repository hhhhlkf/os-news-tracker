from app.enums import SourceType
from app.fetchers.page_monitor import PageMonitorFetcher
from app.fetchers.rss import RssFetcher
from app.fetchers.search import SearchFetcher
from app.models import Source
from app.scheduler import build_fetcher


class _Ext:
    def extract(self, url):
        from app.schemas import ExtractedDoc

        return ExtractedDoc(url=url, clean_content="x")


class _Search:
    def search(self, q):
        return []


def test_build_fetcher_by_type():
    ext, search = _Ext(), _Search()
    assert isinstance(build_fetcher(Source(type=SourceType.RSS), ext, search), RssFetcher)
    assert isinstance(build_fetcher(Source(type=SourceType.PAGE_MONITOR), ext, search), PageMonitorFetcher)
    assert isinstance(build_fetcher(Source(type=SourceType.SEARCH), ext, search), SearchFetcher)
