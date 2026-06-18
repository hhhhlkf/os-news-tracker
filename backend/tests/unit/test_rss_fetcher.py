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


# ── Timeout / HTTP fetch path ─────────────────────────────────────────


def test_rss_fetcher_uses_timeout_http_path_for_http_urls(monkeypatch):
    """RSS fetcher calls _fetch_feed_content for HTTP URLs (not raw feedparser)."""
    from unittest.mock import patch

    feed_xml = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Test Entry</title>
      <link>https://example.com/1</link>
      <pubDate>Wed, 10 Jun 2026 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""

    from app.config import get_settings
    get_settings.cache_clear()

    from app.enums import SourceType
    from app.fetchers.rss import RssFetcher
    from app.models import Source

    fetch_calls = []

    def fake_fetch(url, timeout, user_agent):
        fetch_calls.append((url, timeout, user_agent))
        return feed_xml

    with patch("app.fetchers.rss._fetch_feed_content", fake_fetch):
        src = Source(
            id=1,
            name="TestFeed",
            type=SourceType.RSS,
            url="https://example.com/feed.xml",
        )
        fetcher = RssFetcher()
        items = fetcher.fetch(src)

    assert len(items) == 1
    assert items[0].title == "Test Entry"
    assert len(fetch_calls) == 1
    # First call: timeout should come from settings.
    assert fetch_calls[0][0] == "https://example.com/feed.xml"
    assert fetch_calls[0][1] == 20.0  # default fetch_timeout_seconds


def test_rss_fetcher_still_handles_file_urls():
    """file:// URLs bypass httpx and go straight to feedparser."""
    import os
    from app.enums import SourceType
    from app.fetchers.rss import RssFetcher
    from app.models import Source

    # Use the existing fixture.
    fixtures_dir = os.path.join(os.path.dirname(__file__), "..", "fixtures")
    path = os.path.join(fixtures_dir, "sample_feed.xml")
    src = Source(id=1, name="Local", type=SourceType.RSS, url=f"file://{path}")
    fetcher = RssFetcher()
    items = fetcher.fetch(src)

    assert len(items) == 2
    assert items[0].title == "Linux 6.9 Released"


def test_fetch_feed_content_uses_timeout(monkeypatch):
    """_fetch_feed_content passes the configured timeout to httpx.Client."""
    from unittest.mock import MagicMock, patch

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "<rss/>"
    mock_response.raise_for_status = MagicMock()
    mock_client_instance = MagicMock()
    mock_client_instance.get.return_value = mock_response
    mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
    mock_client_instance.__exit__ = MagicMock(return_value=None)
    mock_client.return_value = mock_client_instance

    with patch("httpx.Client", mock_client):
        from app.fetchers.rss import _fetch_feed_content

        result = _fetch_feed_content(
            "https://example.com/feed", timeout=15.5, user_agent="test/1.0",
        )

    assert result == "<rss/>"
    # Verify timeout was passed to Client constructor.
    mock_client.assert_called_once_with(timeout=15.5)
    # Verify User-Agent header was set.
    call_kwargs = mock_client_instance.get.call_args
    assert call_kwargs is not None
    assert call_kwargs[1]["headers"]["User-Agent"] == "test/1.0"


def test_fetch_feed_content_returns_none_for_file_urls():
    """_fetch_feed_content returns None for file:// URLs."""
    from app.fetchers.rss import _fetch_feed_content

    result = _fetch_feed_content(
        "file:///tmp/test.xml", timeout=10, user_agent="test",
    )
    assert result is None
