from datetime import datetime, timezone

from app.fetchers.base import Fetcher
from app.models import Source
from app.processing.dedup import content_hash
from app.schemas import RawItem


class PageMonitorFetcher(Fetcher):
    def __init__(self, extractor):
        self._extractor = extractor

    def fetch(self, source: Source) -> list[RawItem]:
        # ── List-page mode (方案2): extract individual article links ──
        if source.link_selector:
            return self._fetch_list_mode(source)

        # ── Legacy mode: whole-page change detection ──────────────────
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

    def _fetch_list_mode(self, source: Source) -> list[RawItem]:
        """Extract article links from a list page and emit one RawItem per link.

        Each RawItem points to an individual article URL.  Content extraction
        is deferred to the pipeline, which will fetch and extract each article
        page independently.
        """
        items_data = self._extractor.extract_list_items(
            source.url,
            source.link_selector,
            source.title_selector,
            source.date_selector,
        )

        items: list[RawItem] = []
        for data in items_data:
            published_at: datetime | None = None
            if data["date_str"]:
                try:
                    from dateutil.parser import parse as parse_date
                    dt = parse_date(data["date_str"])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    published_at = dt
                except Exception:
                    pass

            items.append(
                RawItem(
                    source_id=source.id,
                    title=data["title"],
                    url=data["url"],
                    raw_content=None,  # pipeline will extract from article page
                    published_at=published_at,
                )
            )

        return items
