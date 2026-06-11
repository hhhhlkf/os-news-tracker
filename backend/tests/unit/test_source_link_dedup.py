"""Tests for ItemSource deduplication and idempotent writes."""

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from app.enums import Importance, InfoType, SourceType
from app.models import Base, Item, ItemSource, Source
from app.repository import Repository
from app.schemas import EnrichedFields, EntityRef, NormalizedItem


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(Source(id=1, name="Phoronix", type=SourceType.RSS, url="https://phoronix.com/rss"))
    s.add(Source(id=2, name="LWN", type=SourceType.RSS, url="https://lwn.net/rss"))
    s.commit()
    yield s
    s.close()


def _make_normalized(source_id: int = 1, canonical_url: str = "https://x.com/article-1") -> NormalizedItem:
    return NormalizedItem(
        source_id=source_id,
        title="Linux 6.9",
        url=canonical_url,
        canonical_url=canonical_url,
        clean_content="body text",
    )


def _make_enriched() -> EnrichedFields:
    return EnrichedFields(
        title_tldr="Linux 6.9",
        summary="summary",
        key_points=["point"],
        info_type=InfoType.RELEASE,
        importance=Importance.HIGH,
        why_it_matters="matters",
        main_category="OS性能发展",
        sub_tags=["kernel"],
        entities=[EntityRef(type="os", name="Linux")],
        confidence=0.9,
    )


def _count_item_sources(session, item_id: int) -> int:
    return session.scalar(
        select(func.count(ItemSource.id)).where(ItemSource.item_id == item_id)
    ) or 0


class TestMergeSourceLinkIdempotent:
    def test_duplicate_merge_same_source_and_url_no_duplicate(self, session):
        """Same (source_id, url) merged twice → only 1 ItemSource row."""
        repo = Repository(session)
        repo.save_enriched(_make_normalized(), _make_enriched())

        item_id = session.scalar(select(Item.id))
        assert _count_item_sources(session, item_id) == 1

        repo.merge_source_link("https://x.com/article-1", source_id=1, url="https://x.com/article-1")
        assert _count_item_sources(session, item_id) == 1

    def test_merge_different_source_same_url_adds_one(self, session):
        """Different source_id, same url → should add a new row."""
        repo = Repository(session)
        repo.save_enriched(_make_normalized(), _make_enriched())
        item_id = session.scalar(select(Item.id))

        added = repo.merge_source_link("https://x.com/article-1", source_id=2, url="https://x.com/article-1")
        assert added is True
        assert _count_item_sources(session, item_id) == 2

    def test_merge_same_source_different_url_adds_one(self, session):
        """Same source_id, different url → should add a new row."""
        repo = Repository(session)
        repo.save_enriched(_make_normalized(), _make_enriched())
        item_id = session.scalar(select(Item.id))

        added = repo.merge_source_link("https://x.com/article-1", source_id=1, url="https://x.com/article-1?ref=rss")
        assert added is True
        assert _count_item_sources(session, item_id) == 2

    def test_merge_returns_false_when_already_exists(self, session):
        """merge_source_link returns False when the link already exists."""
        repo = Repository(session)
        repo.save_enriched(_make_normalized(), _make_enriched())

        result = repo.merge_source_link("https://x.com/article-1", source_id=1, url="https://x.com/article-1")
        assert result is False

    def test_repeated_pipeline_reruns_no_source_growth(self, session):
        """Simulates multiple pipeline re-runs for the same item; ItemSource count stays stable."""
        repo = Repository(session)
        repo.save_enriched(_make_normalized(), _make_enriched())
        item_id = session.scalar(select(Item.id))

        for _ in range(5):
            repo.merge_source_link("https://x.com/article-1", source_id=1, url="https://x.com/article-1")

        assert _count_item_sources(session, item_id) == 1


class TestSaveEnrichedIdempotentSourceLink:
    def test_save_enriched_creates_exactly_one_source_link(self, session):
        repo = Repository(session)
        repo.save_enriched(_make_normalized(), _make_enriched())
        item_id = session.scalar(select(Item.id))
        assert _count_item_sources(session, item_id) == 1
