from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.enums import SourceType, Stream
from app.fetchers.page_monitor import PageMonitorFetcher
from app.fetchers.rss import RssFetcher
from app.fetchers.search import SearchFetcher
from app.models import Source
from app.scheduler import build_fetcher, list_enabled_news_sources, run_startup_backfill


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


def test_run_startup_backfill_only_runs_enabled_news_sources(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestSession = sessionmaker(bind=engine)
    from app.models import Base

    Base.metadata.create_all(engine)
    session = TestSession()
    session.add_all(
        [
            Source(id=1, name="rss news", type=SourceType.RSS, url="u1", stream=Stream.NEWS, enabled=True),
            Source(id=2, name="page news", type=SourceType.PAGE_MONITOR, url="u2", stream=Stream.NEWS, enabled=True),
            Source(id=3, name="search news", type=SourceType.SEARCH, url="u3", stream=Stream.NEWS, enabled=False),
            Source(id=4, name="structured api", type=SourceType.API, url="u4", stream=Stream.STRUCTURED, enabled=True),
        ]
    )
    session.commit()
    session.close()

    triggered: list[int] = []

    monkeypatch.setattr("app.scheduler.SessionLocal", TestSession)
    monkeypatch.setattr("app.scheduler.run_source_job", lambda source_id: triggered.append(source_id))

    count = run_startup_backfill()

    assert count == 2
    assert triggered == [1, 2]


def test_list_enabled_news_sources_filters_to_enabled_news_types():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestSession = sessionmaker(bind=engine)
    from app.models import Base

    Base.metadata.create_all(engine)
    session = TestSession()
    session.add_all(
        [
            Source(id=1, name="rss news", type=SourceType.RSS, url="u1", stream=Stream.NEWS, enabled=True),
            Source(id=2, name="page news", type=SourceType.PAGE_MONITOR, url="u2", stream=Stream.NEWS, enabled=True),
            Source(id=3, name="search news", type=SourceType.SEARCH, url="u3", stream=Stream.NEWS, enabled=False),
            Source(id=4, name="structured api", type=SourceType.API, url="u4", stream=Stream.STRUCTURED, enabled=True),
        ]
    )
    session.commit()

    sources = list_enabled_news_sources(session)

    assert [source.id for source in sources] == [1, 2]
