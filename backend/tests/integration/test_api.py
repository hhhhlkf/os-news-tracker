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
from app.schemas import EnrichedFields, EntityRef, NormalizedItem


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
    repo.save_enriched(
        NormalizedItem(
            source_id=1,
            title="Linux 6.9",
            url="https://x/a",
            canonical_url="https://x/a",
            clean_content="body",
        ),
        EnrichedFields(
            title_tldr="Linux 6.9 发布",
            summary="s",
            key_points=["a"],
            info_type=InfoType.RELEASE,
            importance=Importance.HIGH,
            why_it_matters="w",
            main_category="OS性能发展",
            sub_tags=["kernel"],
            entities=[EntityRef(type="os", name="Linux")],
            confidence=0.9,
        ),
    )
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
    assert data["total"] == 1
    assert data["items"][0]["title_tldr"] == "Linux 6.9 发布"


def test_filter_by_main_category(client):
    assert client.get("/items?main_category=OS性能发展").json()["total"] == 1
    assert client.get("/items?main_category=司内AI工具").json()["total"] == 0


def test_facets_endpoint(client):
    facets = client.get("/facets").json()
    assert "OS性能发展" in [f["value"] for f in facets["main_category"]]


def test_item_detail(client):
    item_id = client.get("/items").json()["items"][0]["id"]
    detail = client.get(f"/items/{item_id}").json()
    assert detail["summary"] == "s"
    assert detail["entities"][0]["name"] == "Linux"
