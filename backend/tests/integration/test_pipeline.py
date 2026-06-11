import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.enums import Importance, InfoType, SourceType
from app.models import Base, Source
from app.pipeline import Pipeline
from app.schemas import EnrichedFields, EntityRef, ExtractedDoc, RawItem


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    s = session_factory()
    s.add(Source(id=1, name="Phoronix", type=SourceType.RSS, url="u"))
    s.commit()
    yield s
    s.close()


class _StubFetcher:
    def fetch(self, source):
        return [
            RawItem(
                source_id=1,
                title="Linux 6.9",
                url="https://x/a?utm_source=rss",
                raw_content="body",
            )
        ]


class _StubExtractor:
    def extract(self, url):
        return ExtractedDoc(url=url, title="Linux 6.9", clean_content="kernel body text")


class _StubEnricher:
    def enrich(self, item):
        return EnrichedFields(
            title_tldr="Linux 6.9",
            summary="s",
            key_points=["a"],
            info_type=InfoType.RELEASE,
            importance=Importance.HIGH,
            why_it_matters="w",
            main_category="OS性能发展",
            sub_tags=["kernel"],
            entities=[EntityRef(type="os", name="Linux")],
            confidence=0.9,
        )


def test_pipeline_end_to_end_dedups_on_rerun(session):
    src = session.get(Source, 1)
    pipeline = Pipeline(session=session, extractor=_StubExtractor(), enricher=_StubEnricher())
    n1 = pipeline.run_source(src, fetcher=_StubFetcher())
    n2 = pipeline.run_source(src, fetcher=_StubFetcher())
    assert n1 == 1
    assert n2 == 0


class _InternalOnlyCategoryEnricher:
    def enrich(self, item):
        return EnrichedFields(
            title_tldr="Linux 6.9",
            summary="s",
            key_points=["a"],
            info_type=InfoType.RELEASE,
            importance=Importance.HIGH,
            why_it_matters="w",
            main_category="司内AI工具",
            sub_tags=["kernel"],
            entities=[EntityRef(type="os", name="Linux")],
            confidence=0.9,
        )


def test_pipeline_blocks_internal_ai_category_for_non_internal_sources(session):
    src = session.get(Source, 1)
    src.main_category = "OS性能发展"
    session.commit()

    pipeline = Pipeline(
        session=session,
        extractor=_StubExtractor(),
        enricher=_InternalOnlyCategoryEnricher(),
    )

    assert pipeline.run_source(src, fetcher=_StubFetcher()) == 1

    stored = session.query(Source).get(1).items[0]
    assert stored.main_category == "OS性能发展"
