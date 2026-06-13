import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.enums import Importance, InfoType, SourceType
from app.models import Base, Source
from app.repository import Repository
from app.schemas import EnrichedFields, NormalizedItem


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Source(id=1, name="Phoronix", type=SourceType.RSS, url="u"))
    s.commit()
    yield s
    s.close()


def _norm():
    return NormalizedItem(
        source_id=1,
        title="t",
        url="https://x/a",
        canonical_url="https://x/a",
        clean_content="body text",
    )


def _fields():
    return EnrichedFields(
        title_zh="中文标题",
        summary="s",
        tech_highlights=["[内核] 调度器改进"],
        info_type=InfoType.RELEASE,
        importance=Importance.HIGH,
        main_category="OS性能发展",
        sub_tags=["kernel"],
        keywords=["kernel", "sched_ext"],
        confidence=0.9,
    )


def test_save_new_item_merges_sub_tags_and_keywords(session):
    repo = Repository(session)
    item = repo.save_enriched(_norm(), _fields())
    assert item.id is not None
    tag_names = {tag.name for tag in item.tags}
    assert {"kernel", "sched_ext", "OS性能发展"} <= tag_names
    assert item.entities == []
    assert repo.exists_by_canonical("https://x/a") is True


def test_duplicate_canonical_url_merges_source_not_duplicate(session):
    repo = Repository(session)
    repo.save_enriched(_norm(), _fields())
    before = repo.count_items()
    merged = repo.merge_source_link("https://x/a", source_id=1, url="https://mirror/a")
    assert repo.count_items() == before
    assert merged is True
