from datetime import datetime, timezone
from typing import Iterable

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.enums import Importance, InfoType, SourceType
from app.models import Base, Item, Source
from app.pipeline import Pipeline
from app.schemas import EnrichedFields, ExtractedDoc, RawItem


@pytest.fixture
def session() -> Iterable[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    db_session = session_factory()
    yield db_session
    db_session.close()


class _TrackingExtractor:
    def __init__(self, clean_content: str, published_at: datetime | None = None) -> None:
        self.clean_content = clean_content
        self.published_at = published_at
        self.extracted_urls: list[str] = []

    def extract(self, url: str) -> ExtractedDoc:
        self.extracted_urls.append(url)
        return ExtractedDoc(
            url=url,
            title="Extracted article",
            clean_content=self.clean_content,
            published_at=self.published_at,
        )


class _TrackingEnricher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def enrich(self, item) -> EnrichedFields:
        self.calls.append(item.canonical_url)
        return EnrichedFields(
            title_zh="新闻标题",
            summary="summary",
            tech_highlights=["highlight"],
            info_type=InfoType.RELEASE,
            importance=Importance.MEDIUM,
            main_category="OS跟踪来源",
            sub_tags=["release"],
            keywords=["kernel"],
            confidence=0.8,
        )


def _add_source(
    session: Session,
    *,
    source_type: SourceType,
    link_selector: str | None = None,
) -> Source:
    source = Source(
        id=1,
        name="Source",
        type=source_type,
        url="https://example.com/source",
        link_selector=link_selector,
    )
    session.add(source)
    session.commit()
    return source


def _raw_item(*, raw_content: str | None, published_at: datetime | None) -> RawItem:
    return RawItem(
        source_id=1,
        title="Raw title",
        url="https://example.com/item",
        raw_content=raw_content,
        published_at=published_at,
    )


def test_legacy_page_monitor_short_content_is_skipped_before_enrichment(
    session: Session,
) -> None:
    source = _add_source(session, source_type=SourceType.PAGE_MONITOR)
    enricher = _TrackingEnricher()
    pipeline = Pipeline(
        session=session,
        extractor=_TrackingExtractor(clean_content="unused"),
        enricher=enricher,
    )

    stored = pipeline.process_item(
        source,
        _raw_item(
            raw_content="short portal copy",
            published_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        ),
    )

    assert stored is False
    assert enricher.calls == []
    assert session.query(Item).count() == 0

    result = pipeline.process_item_result(
        source,
        _raw_item(
            raw_content="short portal copy",
            published_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        ),
    )
    assert result.stored is False
    assert result.reason == "legacy_page_short_content"


def test_legacy_page_monitor_missing_published_at_is_skipped_before_enrichment(
    session: Session,
) -> None:
    source = _add_source(session, source_type=SourceType.PAGE_MONITOR)
    enricher = _TrackingEnricher()
    pipeline = Pipeline(
        session=session,
        extractor=_TrackingExtractor(clean_content="unused"),
        enricher=enricher,
    )

    stored = pipeline.process_item(
        source,
        _raw_item(raw_content="substantial article body " * 40, published_at=None),
    )

    assert stored is False
    assert enricher.calls == []
    assert session.query(Item).count() == 0

    result = pipeline.process_item_result(
        source,
        _raw_item(raw_content="substantial article body " * 40, published_at=None),
    )
    assert result.stored is False
    assert result.reason == "legacy_page_missing_published_at"


def test_legacy_page_monitor_long_content_with_date_is_stored(
    session: Session,
) -> None:
    source = _add_source(session, source_type=SourceType.PAGE_MONITOR)
    enricher = _TrackingEnricher()
    pipeline = Pipeline(
        session=session,
        extractor=_TrackingExtractor(clean_content="unused"),
        enricher=enricher,
    )

    stored = pipeline.process_item(
        source,
        _raw_item(
            raw_content="substantial article body " * 40,
            published_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        ),
    )

    assert stored is True
    assert enricher.calls == ["https://example.com/item"]
    assert session.query(Item).count() == 1


def test_list_mode_page_monitor_is_not_blocked_by_legacy_quality_gate(
    session: Session,
) -> None:
    source = _add_source(
        session,
        source_type=SourceType.PAGE_MONITOR,
        link_selector="a.article",
    )
    enricher = _TrackingEnricher()
    pipeline = Pipeline(
        session=session,
        extractor=_TrackingExtractor(clean_content="tiny", published_at=None),
        enricher=enricher,
    )

    stored = pipeline.process_item(source, _raw_item(raw_content=None, published_at=None))

    assert stored is True
    assert enricher.calls == ["https://example.com/item"]
    assert session.query(Item).count() == 1


def test_rss_item_is_not_blocked_by_legacy_quality_gate(session: Session) -> None:
    source = _add_source(session, source_type=SourceType.RSS)
    enricher = _TrackingEnricher()
    pipeline = Pipeline(
        session=session,
        extractor=_TrackingExtractor(clean_content="tiny", published_at=None),
        enricher=enricher,
    )

    stored = pipeline.process_item(source, _raw_item(raw_content=None, published_at=None))

    assert stored is True
    assert enricher.calls == ["https://example.com/item"]
    assert session.query(Item).count() == 1
