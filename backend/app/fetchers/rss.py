from datetime import timezone

import feedparser
from dateutil import parser as dateparser

from app.fetchers.base import Fetcher
from app.models import Source
from app.schemas import RawItem


class RssFetcher(Fetcher):
    def fetch(self, source: Source) -> list[RawItem]:
        parsed = feedparser.parse(source.url)
        items: list[RawItem] = []
        for entry in parsed.entries:
            published = None
            if entry.get("published"):
                try:
                    published = dateparser.parse(entry["published"])
                    if published.tzinfo:
                        published = published.astimezone(timezone.utc)
                    else:
                        # Assume UTC when the source omits a timezone,
                        # keeping the datetime aware so comparisons are safe.
                        published = published.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    published = None
            items.append(
                RawItem(
                    source_id=source.id,
                    title=entry.get("title", "").strip(),
                    url=entry.get("link", ""),
                    raw_content=entry.get("summary", "") or entry.get("description", ""),
                    published_at=published,
                )
            )
        return items
