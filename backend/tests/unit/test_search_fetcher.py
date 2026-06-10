from app.enums import SourceType
from app.fetchers.search import SearchFetcher
from app.models import Source
from app.schemas import ExtractedDoc
from app.search.base import SearchResult


class _StubSearch:
    def search(self, query):
        return [
            SearchResult(url="https://a.com/1", title="A"),
            SearchResult(url="https://b.com/2", title="B"),
        ]


class _StubExtractor:
    def extract(self, url):
        return ExtractedDoc(url=url, title="T", clean_content=f"content of {url}")


def _relevance_only_a(title, content, keywords):
    return "a.com" in title or "a.com" in content


def test_search_fetcher_filters_by_relevance():
    src = Source(id=3, name="kw", type=SourceType.SEARCH, url="", keywords="linux kernel")
    fetcher = SearchFetcher(
        search=_StubSearch(),
        extractor=_StubExtractor(),
        relevance_fn=_relevance_only_a,
    )
    items = fetcher.fetch(src)
    assert len(items) == 1
    assert items[0].url == "https://a.com/1"
