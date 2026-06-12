from datetime import datetime, timezone

from app.fetchers.base import Fetcher
from app.models import Source
from app.processing.dedup import content_hash
from app.schemas import RawItem


class PageMonitorFetcher(Fetcher):
    def __init__(self, extractor):
        self._extractor = extractor

    def fetch(self, source: Source) -> list[RawItem]:
        doc = self._extractor.extract(source.url)
        new_hash = content_hash(doc.clean_content)
        if source.last_content_hash == new_hash:
            return []
        source.last_content_hash = new_hash
        # Use the date extracted from the page; fall back to now (UTC) so
        # items pass time-filtering in manual news runs.
        published_at = doc.published_at or datetime.now(timezone.utc)
        return [
            RawItem(
                source_id=source.id,
                title=doc.title or source.name,
                url=source.url,
                raw_content=doc.clean_content,
                published_at=published_at,
            )
        ]
