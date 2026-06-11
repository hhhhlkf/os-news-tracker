from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.main import create_app
from app.enums import Importance, InfoType, SourceType
from app.models import Base, Source
from app.repository import Repository
from app.schemas import EnrichedFields, EntityRef, ManualNewsRunStatus, NormalizedItem


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    seed = TestSession()
    seed.add(Source(id=1, name="Phoronix", type=SourceType.RSS, url="u"))
    seed.commit()
    repo = Repository(seed)

    items_data = [
        (
            NormalizedItem(
                source_id=1,
                title="Item A",
                url="https://x/a",
                canonical_url="https://x/a",
                clean_content="body a",
                published_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc),
            ),
            EnrichedFields(
                title_tldr="Item A TLDR",
                summary="sa",
                key_points=["pa"],
                info_type=InfoType.RELEASE,
                importance=Importance.HIGH,
                why_it_matters="wa",
                main_category="OS性能发展",
                sub_tags=["kernel"],
                entities=[EntityRef(type="os", name="Linux")],
                confidence=0.9,
            ),
        ),
        (
            NormalizedItem(
                source_id=1,
                title="Item B",
                url="https://x/b",
                canonical_url="https://x/b",
                clean_content="body b",
                published_at=datetime(2026, 6, 9, 5, 30, 0, tzinfo=timezone.utc),
            ),
            EnrichedFields(
                title_tldr="Item B TLDR",
                summary="sb",
                key_points=["pb"],
                info_type=InfoType.UPDATE,
                importance=Importance.MEDIUM,
                why_it_matters="wb",
                main_category="OS跟踪来源",
                sub_tags=["security"],
                entities=[EntityRef(type="vendor", name="Ubuntu")],
                confidence=0.85,
            ),
        ),
        (
            NormalizedItem(
                source_id=1,
                title="Item C",
                url="https://x/c",
                canonical_url="https://x/c",
                clean_content="body c",
                published_at=datetime(2026, 6, 8, 3, 0, 0, tzinfo=timezone.utc),
            ),
            EnrichedFields(
                title_tldr="Item C TLDR",
                summary="sc",
                key_points=["pc"],
                info_type=InfoType.ADAPTATION,
                importance=Importance.LOW,
                why_it_matters="wc",
                main_category="软件包适配",
                sub_tags=["container"],
                entities=[EntityRef(type="vendor", name="OpenCloudOS")],
                confidence=0.8,
            ),
        ),
        (
            NormalizedItem(
                source_id=1,
                title="Item D (no published_at)",
                url="https://x/d",
                canonical_url="https://x/d",
                clean_content="body d",
                published_at=None,
            ),
            EnrichedFields(
                title_tldr="Item D TLDR",
                summary="sd",
                key_points=["pd"],
                info_type=InfoType.ANALYSIS,
                importance=Importance.MEDIUM,
                why_it_matters="wd",
                main_category="司内AI工具",
                sub_tags=["workflow"],
                entities=[EntityRef(type="topic", name="AI")],
                confidence=0.75,
            ),
        ),
    ]
    for ni, ef in items_data:
        repo.save_enriched(ni, ef)

    seed.close()

    app = create_app()

    def _override():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def test_list_items(client):
    resp = client.get("/items")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4
    assert data["items"][0]["title_tldr"] == "Item A TLDR"


def test_filter_by_main_category(client):
    assert client.get("/items?main_category=OS性能发展").json()["total"] == 1
    assert client.get("/items?main_category=司内AI工具").json()["total"] == 1


def test_facets_endpoint(client):
    facets = client.get("/facets").json()
    assert "OS性能发展" in [f["value"] for f in facets["main_category"]]


def test_item_detail(client):
    item_id = client.get("/items").json()["items"][0]["id"]
    detail = client.get(f"/items/{item_id}").json()
    assert detail["summary"] == "sa"
    assert detail["entities"][0]["name"] == "Linux"


# ── Sorting tests ────────────────────────────────────────────────────

def test_list_items_default_sort_is_published_at_desc(client):
    """Without sort params, items are ordered by published_at DESC NULLS LAST."""
    resp = client.get("/items")
    assert resp.status_code == 200
    items = resp.json()["items"]
    # non-null published_at items come first, most recent first
    ts = [it["published_at"] for it in items]
    non_null = [t for t in ts if t is not None]
    assert non_null == ["2026-06-10T09:00:00", "2026-06-09T05:30:00", "2026-06-08T03:00:00"]
    # null is last
    assert ts[-1] is None


def test_list_items_sort_by_fetched_at_desc(client):
    resp = client.get("/items?sort_by=fetched_at&sort_dir=desc")
    assert resp.status_code == 200
    items = resp.json()["items"]
    # All items have fetched_at; verify non-null
    assert all(it["fetched_at"] is not None for it in items)
    # Verify descending: each item's fetched_at >= the next
    for i in range(len(items) - 1):
        assert items[i]["fetched_at"] >= items[i + 1]["fetched_at"]


def test_list_items_sort_by_published_at_asc(client):
    resp = client.get("/items?sort_by=published_at&sort_dir=asc")
    assert resp.status_code == 200
    items = resp.json()["items"]
    ts = [it["published_at"] for it in items]
    non_null = [t for t in ts if t is not None]
    # ascending: earliest first
    assert non_null == ["2026-06-08T03:00:00", "2026-06-09T05:30:00", "2026-06-10T09:00:00"]
    # null is last even in ascending
    assert ts[-1] is None


def test_list_items_invalid_sort_by_returns_422(client):
    resp = client.get("/items?sort_by=invalid")
    assert resp.status_code == 422


def test_item_summary_includes_fetched_at(client):
    resp = client.get("/items")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert "fetched_at" in item
        assert item["fetched_at"] is not None or item["fetched_at"] is None


# ── Time filtering tests ──────────────────────────────────────────────

def test_list_items_filter_published_after(client):
    """published_after=2026-06-09 returns items on or after that date."""
    resp = client.get("/items?published_after=2026-06-09")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert resp.json()["total"] == 2  # Item A (Jun 10) + Item B (Jun 9)
    titles = [it["title"] for it in items]
    assert "Item A" in titles
    assert "Item B" in titles


def test_list_items_filter_published_before(client):
    """published_before=2026-06-09 includes items on that day (inclusive upper bound)."""
    resp = client.get("/items?published_before=2026-06-09")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert resp.json()["total"] == 2  # Item B (Jun 9) + Item C (Jun 8)
    titles = [it["title"] for it in items]
    assert "Item B" in titles
    assert "Item C" in titles


def test_list_items_filter_published_range(client):
    """published_after + published_before narrows to exact range."""
    resp = client.get("/items?published_after=2026-06-09&published_before=2026-06-09")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert resp.json()["total"] == 1  # Only Item B (Jun 9)
    assert items[0]["title"] == "Item B"


def test_list_items_filter_published_before_includes_same_day(client):
    """published_before=2026-06-10 includes items from Jun 10 (inclusive upper bound)."""
    resp = client.get("/items?published_before=2026-06-10")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert resp.json()["total"] == 3  # Items A (Jun 10), B (Jun 9), C (Jun 8)
    titles = [it["title"] for it in items]
    assert "Item A" in titles


def test_list_items_filter_invalid_date_returns_422(client):
    resp = client.get("/items?published_after=not-a-date")
    assert resp.status_code == 422


def test_get_news_run_status(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.get_manual_news_run_status",
        lambda: ManualNewsRunStatus(state="idle"),
    )

    resp = client.get("/news-run")

    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"


def test_start_news_run_returns_conflict_when_already_active(client, monkeypatch):
    monkeypatch.setattr("app.api.routes.start_manual_news_run", lambda request: False)

    resp = client.post(
        "/news-run/start",
        json={"time_mode": "relative", "relative_range": "7d", "target_count": 50},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "manual news run already active"


def test_stop_news_run_returns_latest_status(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.stop_manual_news_run",
        lambda: ManualNewsRunStatus(state="stopping"),
    )

    resp = client.post("/news-run/stop")

    assert resp.status_code == 200
    assert resp.json()["state"] == "stopping"


def test_start_news_run_accepts_target_count(client, monkeypatch):
    called_with = {}

    def _fake_start(request):
        called_with["request"] = request
        return True

    monkeypatch.setattr("app.api.routes.start_manual_news_run", _fake_start)

    resp = client.post(
        "/news-run/start",
        json={"time_mode": "relative", "relative_range": "7d", "target_count": 100},
    )

    assert resp.status_code == 200
    assert called_with["request"].target_count == 100
