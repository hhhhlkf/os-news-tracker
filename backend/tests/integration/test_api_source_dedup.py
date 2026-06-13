"""Tests that the item_detail API returns deduplicated source_links."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.main import create_app
from app.enums import Importance, InfoType, SourceType
from app.models import Base, ItemSource, Source
from app.repository import Repository
from app.schemas import EnrichedFields, NormalizedItem


@pytest.fixture
def client_with_dup_sources():
    """Creates a test client where the DB has duplicate ItemSource rows (historical dirty data).

    After creating the item normally, we inject duplicates via raw SQL
    to simulate a pre-migration database.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Create all tables except item_sources (so we can make one without the constraint)
    tables_no_is = [t for t in Base.metadata.sorted_tables if t.name != "item_sources"]
    Base.metadata.create_all(engine, tables=tables_no_is)

    # Create item_sources without the unique constraint
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE item_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER REFERENCES items(id),
                source_id INTEGER REFERENCES sources(id),
                url VARCHAR(1000)
            )
        """))
        conn.commit()

    TestSession = sessionmaker(bind=engine)
    seed = TestSession()
    seed.add(Source(id=1, name="Phoronix", type=SourceType.RSS, url="u"))
    seed.add(Source(id=2, name="LWN", type=SourceType.RSS, url="u2"))
    seed.commit()

    repo = Repository(seed)
    item = repo.save_enriched(
        NormalizedItem(
            source_id=1,
            title="Linux 6.9",
            url="https://x/a",
            canonical_url="https://x/a",
            clean_content="body",
        ),
        EnrichedFields(
            title_zh="Linux 6.9 正式发布",
            summary="s",
            tech_highlights=["[内核] a"],
            info_type=InfoType.RELEASE,
            importance=Importance.HIGH,
            main_category="OS性能发展",
            sub_tags=["kernel"],
            keywords=["Linux"],
            confidence=0.9,
        ),
    )

    # Insert duplicate rows via raw SQL (no constraint to stop us)
    with engine.connect() as conn:
        conn.execute(text(
            "INSERT INTO item_sources (item_id, source_id, url) VALUES (:iid, :sid, :u)"
        ), [
            {"iid": item.id, "sid": 1, "u": "https://x/a"},
            {"iid": item.id, "sid": 1, "u": "https://x/a"},
            {"iid": item.id, "sid": 2, "u": "https://x/a"},
        ])
        conn.commit()

    seed.close()

    app = create_app()

    def _override():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)


def test_item_detail_deduplicates_source_links(client_with_dup_sources):
    """Even with duplicate ItemSource rows in DB, the API returns unique source_links."""
    items = client_with_dup_sources.get("/items").json()["items"]
    item_id = items[0]["id"]
    detail = client_with_dup_sources.get(f"/items/{item_id}").json()

    urls_with_source = [(s["source_id"], s["url"]) for s in detail["source_links"]]
    assert len(urls_with_source) == len(set(urls_with_source)), (
        f"source_links should be deduplicated but got: {urls_with_source}"
    )
    # We have source_id=1 + url=https://x/a and source_id=2 + url=https://x/a → 2 unique pairs
    assert len(detail["source_links"]) == 2


def test_item_detail_source_links_order_stable(client_with_dup_sources):
    """source_links order should be stable across requests."""
    items = client_with_dup_sources.get("/items").json()["items"]
    item_id = items[0]["id"]

    results = []
    for _ in range(3):
        detail = client_with_dup_sources.get(f"/items/{item_id}").json()
        results.append(detail["source_links"])

    assert results[0] == results[1] == results[2]
