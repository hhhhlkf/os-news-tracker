import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.db import SessionLocal
from app.enums import SourceType, Stream
from app.extract.scrapling_extractor import ScraplingExtractor
from app.fetchers.page_monitor import PageMonitorFetcher
from app.fetchers.rss import RssFetcher
from app.fetchers.search import SearchFetcher
from app.models import Source
from app.pipeline import Pipeline
from app.processing.enricher import Enricher
from app.search.base import get_search_provider

logger = logging.getLogger(__name__)

SUPPORTED_NEWS_SOURCE_TYPES = (
    SourceType.RSS,
    SourceType.PAGE_MONITOR,
    SourceType.SEARCH,
)


def build_fetcher(source: Source, extractor, search):
    if source.type == SourceType.RSS:
        return RssFetcher()
    if source.type == SourceType.PAGE_MONITOR:
        return PageMonitorFetcher(extractor=extractor)
    if source.type == SourceType.SEARCH:
        return SearchFetcher(search=search, extractor=extractor)
    raise ValueError(f"unknown source type {source.type}")


def list_enabled_news_sources(session) -> list[Source]:
    return list(
        session.scalars(
            select(Source).where(
                Source.enabled.is_(True),
                Source.stream == Stream.NEWS,
                Source.type.in_(SUPPORTED_NEWS_SOURCE_TYPES),
            )
        )
    )


def run_source_job(source_id: int):
    session = SessionLocal()
    try:
        source = session.get(Source, source_id)
        if not source or not source.enabled:
            return
        extractor = ScraplingExtractor(use_stealth=source.stealth)
        search = get_search_provider()
        fetcher = build_fetcher(source, extractor, search)
        pipeline = Pipeline(session=session, extractor=extractor, enricher=Enricher())
        count = pipeline.run_source(source, fetcher=fetcher)
        logger.info("source %s produced %d new items", source.name, count)
    finally:
        session.close()


def run_startup_backfill() -> int:
    session = SessionLocal()
    try:
        source_ids = [source.id for source in list_enabled_news_sources(session)]
    finally:
        session.close()

    for source_id in source_ids:
        run_source_job(source_id)

    logger.info("startup backfill processed %d news sources", len(source_ids))
    return len(source_ids)


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    session = SessionLocal()
    try:
        for source in session.scalars(select(Source).where(Source.enabled.is_(True))):
            cron = source.fetch_cron or "0 8 * * *"
            scheduler.add_job(
                run_source_job,
                CronTrigger.from_crontab(cron),
                args=[source.id],
                id=f"source-{source.id}",
            )
    finally:
        session.close()
    scheduler.start()
    return scheduler
