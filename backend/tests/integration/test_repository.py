import pytest
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.enums import Importance, InfoType, SourceType
from app.models import Base, Source, Tag, TagAlias
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


def test_save_new_item_uses_sub_tags_and_main_category_as_tags(session):
    repo = Repository(session)
    item = repo.save_enriched(_norm(), _fields())
    assert item.id is not None
    tag_names = {tag.name for tag in item.tags}
    assert tag_names == {"kernel", "OS性能发展"}
    assert item.entities == []
    assert repo.exists_by_canonical("https://x/a") is True


def test_save_new_item_limits_total_tags_to_five(session):
    repo = Repository(session)
    fields = _fields()
    fields.sub_tags = ["kernel", "scheduler", "openEuler", "RPM", "security"]
    fields.keywords = ["Linux 6.9", "sched_ext", "EAS", "CVE-2026-0001"]

    item = repo.save_enriched(_norm(), fields)

    assert len(item.tags) == 5
    assert {tag.name for tag in item.tags} == {
        "kernel",
        "scheduler",
        "openEuler",
        "RPM",
        "OS性能发展",
    }


def test_save_new_item_records_approved_tag_alias_suggestions(session):
    repo = Repository(session)
    old_tag = Tag(name="kernel", kind="sub_tag")
    session.add(old_tag)
    session.commit()
    fields = _fields()
    fields.sub_tags = ["Linux Kernel"]
    fields.merge_suggestions = [
        {
            "child_tag_id": old_tag.id,
            "parent_tag_name": "Linux Kernel",
            "reason": "Linux Kernel is the canonical parent",
            "confidence": 0.93,
        }
    ]

    item = repo.save_enriched(_norm(), fields)

    parent_tag = session.scalar(
        select(Tag).where(Tag.name == "Linux Kernel", Tag.kind == "sub_tag")
    )
    alias = session.scalar(select(TagAlias).where(TagAlias.child_tag_id == old_tag.id))
    assert parent_tag is not None
    assert alias is not None
    assert alias.parent_tag_id == parent_tag.id
    assert alias.status == "approved"
    assert alias.source == "llm"
    assert alias.confidence == 0.93
    assert {tag.name for tag in item.tags} == {"Linux Kernel", "OS性能发展"}


def test_duplicate_canonical_url_merges_source_not_duplicate(session):
    repo = Repository(session)
    repo.save_enriched(_norm(), _fields())
    before = repo.count_items()
    merged = repo.merge_source_link("https://x/a", source_id=1, url="https://mirror/a")
    assert repo.count_items() == before
    assert merged is True
