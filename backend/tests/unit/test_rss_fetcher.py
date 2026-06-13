import os

from app.enums import SourceType
from app.fetchers.base import Fetcher
from app.fetchers.rss import RssFetcher
from app.models import Source


def test_rss_fetcher_parses_items(fixtures_dir):
    path = os.path.join(fixtures_dir, "sample_feed.xml")
    src = Source(id=1, name="Phoronix", type=SourceType.RSS, url=f"file://{path}")
    fetcher = RssFetcher()
    items = fetcher.fetch(src)
    assert len(items) == 2
    assert items[0].title == "Linux 6.9 Released"
    assert items[0].url.startswith("https://phoronix.com/linux-6-9")
    assert items[0].published_at is not None
    assert isinstance(fetcher, Fetcher)


def test_rss_fetcher_falls_back_to_updated(fixtures_dir):
    """Atom feeds often use <updated> without <pubDate> — fall back to updated."""
    path = os.path.join(fixtures_dir, "sample_feed_atom.xml")
    src = Source(id=2, name="AtomFeed", type=SourceType.RSS, url=f"file://{path}")
    fetcher = RssFetcher()
    items = fetcher.fetch(src)
    assert len(items) == 2

    # First entry: only <updated>, no <published>
    assert items[0].title == "Atom Entry with updated only"
    assert items[0].published_at is not None
    assert items[0].published_at.year == 2026
    assert items[0].published_at.month == 6
    assert items[0].published_at.day == 10

    # Second entry: both <published> and <updated> — published should win
    assert items[1].title == "Atom Entry with both dates"
    assert items[1].published_at is not None
    assert items[1].published_at.day == 9  # published wins over updated


def test_rss_resolve_entry_date_uses_created():
    """When published/updated are missing, created should be used."""
    from app.fetchers.rss import RssFetcher

    fetcher = RssFetcher()
    entry = {"created": "2026-05-20T08:00:00Z"}
    result = fetcher._resolve_entry_date(entry, feed_date=None)
    assert result is not None
    assert result.month == 5
    assert result.day == 20


def test_rss_resolve_entry_date_uses_published_parsed():
    """When string fields are missing, published_parsed struct_time is used."""
    import time
    from app.fetchers.rss import RssFetcher

    fetcher = RssFetcher()
    entry = {"published_parsed": time.strptime("2026-04-10T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ")}
    result = fetcher._resolve_entry_date(entry, feed_date=None)
    assert result is not None
    assert result.month == 4
    assert result.day == 10


def test_rss_resolve_entry_date_uses_feed_fallback():
    """When entry has no date, feed-level date is used as last resort."""
    from app.fetchers.rss import RssFetcher

    fetcher = RssFetcher()
    entry = {}
    result = fetcher._resolve_entry_date(entry, feed_date="Wed, 01 Jan 2026 00:00:00 GMT")
    assert result is not None
    assert result.month == 1
    assert result.day == 1


def test_rss_resolve_entry_date_prefers_published_over_feed():
    """Entry-level published takes priority over feed-level date."""
    from app.fetchers.rss import RssFetcher

    fetcher = RssFetcher()
    entry = {"published": "2026-06-01"}
    result = fetcher._resolve_entry_date(entry, feed_date="2026-01-01")
    assert result is not None
    assert result.month == 6  # entry wins, not feed


def test_rss_resolve_entry_date_all_missing_returns_none():
    """When nothing is available, return None."""
    from app.fetchers.rss import RssFetcher

    fetcher = RssFetcher()
    result = fetcher._resolve_entry_date({}, feed_date=None)
    assert result is None


def test_parse_date_from_struct():
    """_parse_date_from_struct converts struct_time to datetime."""
    import time
    from app.fetchers.rss import _parse_date_from_struct

    struct = time.strptime("2026-07-04T15:30:00Z", "%Y-%m-%dT%H:%M:%SZ")
    result = _parse_date_from_struct(struct)
    assert result is not None
    assert result.year == 2026
    assert result.month == 7
    assert result.day == 4
    assert result.hour == 15
