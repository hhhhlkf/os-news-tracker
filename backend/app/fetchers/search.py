from app.fetchers.base import Fetcher
from app.models import Source
from app.processing.relevance import llm_relevance
from app.schemas import RawItem


class SearchFetcher(Fetcher):
    def __init__(self, search, extractor, relevance_fn=llm_relevance):
        self._search = search
        self._extractor = extractor
        self._relevance_fn = relevance_fn

    def fetch(self, source: Source) -> list[RawItem]:
        keywords = source.keywords or ""
        results = self._search.search(keywords)
        items: list[RawItem] = []
        for result in results:
            doc = self._extractor.extract(result.url)
            if not self._relevance_fn(doc.title or result.title or "", doc.clean_content, keywords):
                continue
            items.append(
                RawItem(
                    source_id=source.id,
                    title=doc.title or result.title or result.url,
                    url=result.url,
                    raw_content=doc.clean_content,
                    published_at=doc.published_at,
                )
            )
        return items
