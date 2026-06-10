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
