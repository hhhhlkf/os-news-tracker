import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.enums import Importance, InfoType, SourceType
from app.models import Base, Source
from app.pipeline import Pipeline
from app.schemas import EnrichedFields, ExtractedDoc, RawItem


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
            title_zh="Linux 6.9 正式发布",
            summary="s",
            tech_highlights=["[内核] 调度器改进"],
            info_type=InfoType.RELEASE,
            importance=Importance.HIGH,
            main_category="OS性能发展",
            sub_tags=["kernel"],
            keywords=["Linux 6.9"],
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
            title_zh="Linux 6.9 正式发布",
            summary="s",
            tech_highlights=["[内核] 调度器改进"],
            info_type=InfoType.RELEASE,
            importance=Importance.HIGH,
            main_category="司内AI工具",
            sub_tags=["kernel"],
            keywords=["Linux 6.9"],
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

    stored = session.get(Source, 1).items[0]
    assert stored.main_category == "OS性能发展"


def test_list_mode_page_monitor_extracts_from_article_url(session):
    """When a page_monitor item has no raw_content (list mode), the pipeline
    should fetch and extract the article page content."""
    src = Source(
        id=2,
        name="ListMonitor",
        type=SourceType.PAGE_MONITOR,
        url="https://example.com/news-list",
    )
    session.add(src)
    session.commit()

    class _ListModeFetcher:
        def fetch(self, source):
            return [
                RawItem(
                    source_id=2,
                    title="Article from list",
                    url="https://example.com/article-1",
                    raw_content=None,  # list mode — no pre-fetched content
                    published_at=None,
                )
            ]

    # Track which URLs the extractor was asked to extract.
    class _TrackingExtractor:
        def __init__(self):
            self.extracted_urls: list[str] = []

        def extract(self, url):
            self.extracted_urls.append(url)
            return ExtractedDoc(
                url=url,
                title="Article from list",
                clean_content="extracted article body",
                published_at=None,
            )

    extractor = _TrackingExtractor()
    pipeline = Pipeline(session=session, extractor=extractor, enricher=_StubEnricher())
    n = pipeline.run_source(src, fetcher=_ListModeFetcher())
    assert n == 1
    # The extractor was called for the article URL, not the list page URL.
    assert "https://example.com/article-1" in extractor.extracted_urls

    stored = src.items[0]
    assert stored.url == "https://example.com/article-1"


def test_pipeline_applies_relevance_filter_when_enabled(session, monkeypatch):
    src = session.get(Source, 1)
    src.relevance_filter = True
    src.relevance_keywords = "kernel, release"
    session.commit()

    monkeypatch.setattr("app.pipeline.llm_relevance", lambda *args, **kwargs: False)

    pipeline = Pipeline(session=session, extractor=_StubExtractor(), enricher=_StubEnricher())

    assert pipeline.run_source(src, fetcher=_StubFetcher()) == 0
    assert session.get(Source, 1).items == []


def test_process_item_result_reports_duplicate_reason(session):
    src = session.get(Source, 1)
    pipeline = Pipeline(session=session, extractor=_StubExtractor(), enricher=_StubEnricher())
    raw = RawItem(
        source_id=1,
        title="Linux 6.9",
        url="https://x/a?utm_source=rss",
        raw_content="body",
    )

    assert pipeline.process_item(src, raw) is True

    result = pipeline.process_item_result(src, raw)

    assert result.stored is False
    assert result.reason == "duplicate"


def test_process_item_result_reports_relevance_reason(session, monkeypatch):
    src = session.get(Source, 1)
    src.relevance_filter = True
    src.relevance_keywords = "kernel, release"
    session.commit()

    monkeypatch.setattr("app.pipeline.llm_relevance", lambda *args, **kwargs: False)
    pipeline = Pipeline(session=session, extractor=_StubExtractor(), enricher=_StubEnricher())

    result = pipeline.process_item_result(
        src,
        RawItem(
            source_id=1,
            title="Linux 6.9",
            url="https://x/relevance",
            raw_content="body",
        ),
    )

    assert result.stored is False
    assert result.reason == "relevance"


class _RejectingEnricher:
    def enrich(self, item):
        return EnrichedFields(
            title_zh="无效页面",
            summary="页面缺少可提取的正文内容。",
            tech_highlights=[],
            info_type=InfoType.OTHER,
            importance=Importance.LOW,
            main_category="友商产品信息",
            sub_tags=[],
            keywords=[],
            confidence=0.1,
            should_store=False,
            reject_reason="source page is a docs or landing page without newsworthy content",
        )


class _CountingEnricher(_StubEnricher):
    def __init__(self):
        self.calls = 0

    def enrich(self, item):
        self.calls += 1
        return super().enrich(item)


def test_pipeline_skips_non_newsworthy_items_rejected_by_enricher(session):
    src = session.get(Source, 1)
    pipeline = Pipeline(session=session, extractor=_StubExtractor(), enricher=_RejectingEnricher())

    assert pipeline.run_source(src, fetcher=_StubFetcher()) == 0
    assert session.get(Source, 1).items == []


def test_process_item_result_reports_enrich_reject_reason(session):
    src = session.get(Source, 1)
    pipeline = Pipeline(session=session, extractor=_StubExtractor(), enricher=_RejectingEnricher())

    result = pipeline.process_item_result(
        src,
        RawItem(
            source_id=1,
            title="Linux 6.9",
            url="https://x/rejected",
            raw_content="body",
        ),
    )

    assert result.stored is False
    assert result.reason == "enrich_reject"
    assert result.detail == "source page is a docs or landing page without newsworthy content"


def test_pipeline_skips_bot_challenge_pages_before_enrichment(session):
    src = session.get(Source, 1)
    challenge = (
        "确保您不是机器人！ Making sure you're not a bot! "
        "Anubis uses a Proof-of-Work scheme based on Hashcash to protect "
        "the website from large-scale scraping. Please enable JavaScript."
    )

    class _ChallengeFetcher:
        def fetch(self, source):
            return [
                RawItem(
                    source_id=1,
                    title="Making sure you're not a bot!",
                    url="https://x/anubis",
                    raw_content=challenge,
                )
            ]

    class _ChallengeExtractor:
        def extract(self, url):
            return ExtractedDoc(
                url=url,
                title="Making sure you're not a bot!",
                clean_content=challenge,
            )

    enricher = _CountingEnricher()
    pipeline = Pipeline(session=session, extractor=_ChallengeExtractor(), enricher=enricher)

    assert pipeline.run_source(src, fetcher=_ChallengeFetcher()) == 0
    assert enricher.calls == 0
    assert session.get(Source, 1).items == []


def test_process_item_result_reports_bot_challenge_reason(session):
    src = session.get(Source, 1)
    challenge = (
        "确保您不是机器人！ Making sure you're not a bot! "
        "Anubis uses a Proof-of-Work scheme based on Hashcash to protect "
        "the website from large-scale scraping. Please enable JavaScript."
    )

    class _ChallengeExtractor:
        def extract(self, url):
            return ExtractedDoc(
                url=url,
                title="Making sure you're not a bot!",
                clean_content=challenge,
            )

    pipeline = Pipeline(session=session, extractor=_ChallengeExtractor(), enricher=_StubEnricher())

    result = pipeline.process_item_result(
        src,
        RawItem(
            source_id=1,
            title="Making sure you're not a bot!",
            url="https://x/anubis-2",
            raw_content=challenge,
        ),
    )

    assert result.stored is False
    assert result.reason == "bot_challenge"
