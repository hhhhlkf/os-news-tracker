import calendar
import logging
from datetime import datetime, timezone

import feedparser
from dateutil import parser as dateparser

from app.fetchers.base import Fetcher
from app.models import Source
from app.schemas import RawItem

logger = logging.getLogger(__name__)


def _parse_date_from_struct(parsed_struct) -> datetime | None:
    """Convert a feedparser ``*_parsed`` struct_time to a UTC datetime."""
    try:
        ts = calendar.timegm(parsed_struct)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, OverflowError):
        return None


def _make_aware_utc(dt: datetime) -> datetime:
    """Return a UTC-aware copy of *dt*."""
    if dt.tzinfo:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


class RssFetcher(Fetcher):
    def fetch(self, source: Source) -> list[RawItem]:
        parsed = feedparser.parse(source.url)
        items: list[RawItem] = []

        # Feed-level date as ultimate fallback (low confidence).
        feed_date = (
            parsed.feed.get("published")
            or parsed.feed.get("updated")
        )

        for entry in parsed.entries:
            published = self._resolve_entry_date(entry, feed_date)
            if published is None:
                logger.debug("no date for RSS entry: %s", entry.get("link", "?"))

            items.append(
                RawItem(
                    source_id=source.id,
                    title=entry.get("title", "").strip(),
                    url=entry.get("link", ""),
                    raw_content=entry.get("summary", "")
                    or entry.get("description", ""),
                    published_at=published,
                )
            )
        return items

    def _resolve_entry_date(
        self, entry, feed_date: str | None
    ) -> datetime | None:
        """Resolve a publish date for a feed entry, trying multiple sources.

        Priority:
        1. ``published`` string (RSS <pubDate>)
        2. ``updated`` string (Atom <updated>)
        3. ``created`` string
        4. ``published_parsed`` struct_time
        5. ``updated_parsed`` struct_time
        6. Feed-level published / updated string (low confidence)
        """
        # ── String fields ──────────────────────────────────────────
        for field in ("published", "updated", "created"):
            date_str = entry.get(field)
            if date_str:
                try:
                    dt = dateparser.parse(date_str)
                    return _make_aware_utc(dt)
                except (TypeError, ValueError):
                    pass

        # ── Parsed struct_time tuples ──────────────────────────────
        for field in ("published_parsed", "updated_parsed"):
            struct = entry.get(field)
            if struct:
                result = _parse_date_from_struct(struct)
                if result:
                    return result

        # ── Feed-level fallback ────────────────────────────────────
        if feed_date:
            try:
                dt = dateparser.parse(feed_date)
                return _make_aware_utc(dt)
            except (TypeError, ValueError):
                pass

        return None
