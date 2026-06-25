from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.main import create_app
from app.models import AgentCrawlRun, Base, Item, ItemSource, Source
from app.sources.detector import DetectResult, SourceDetectionError

TEST_DB = "sqlite+pysqlite:///:memory:"

_DISCOVERY_RESULT = {
    "root_url": "https://openanolis.cn/blog",
    "success": True,
    "api_url": "https://api.openanolis.cn/blog/list",
    "method": "GET",
    "items_path": "data.records",
    "fields": {"title": "title", "url": "url", "published_at": "published_at"},
    "name_suggestion": "OpenAnolis 博客",
    "sample_items": [
        {
            "title": "Test Article",
            "url": "https://openanolis.cn/blog/1",
            "published_at": "2026-06-20T10:00:00Z",
            "content_preview": "Lorem ipsum",
        }
    ],
    "real_content_count": 1,
    "candidates": [],
    "notes": [],
}


@pytest.fixture
def client():
    engine = create_engine(
        TEST_DB,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionTest = sessionmaker(bind=engine)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: SessionTest()
    yield TestClient(app)
    Base.metadata.drop_all(engine)


def _rss_detect_result() -> DetectResult:
    return DetectResult(
        detected_type="rss",
        name_suggestion="Detected Feed",
        api_config=None,
        notes=["识别为 RSS/Atom 订阅源。"],
    )


def test_detect_source_returns_detect_result(client):
    with patch("app.api.source_routes.detect_source", return_value=_rss_detect_result()) as detect:
        response = client.post("/sources/detect", json={"url": "https://example.com/feed.xml"})

    assert response.status_code == 200
    assert response.json()["detected_type"] == "rss"
    assert response.json()["name_suggestion"] == "Detected Feed"
    detect.assert_called_once_with("https://example.com/feed.xml")


def test_detect_source_unable_returns_422(client):
    with patch("app.api.source_routes.detect_source", side_effect=SourceDetectionError("nope")):
        response = client.post("/sources/detect", json={"url": "https://example.com/nope"})

    assert response.status_code == 422
    assert response.json()["detail"] == "无法识别链接形态，请改用 RSS/API/网页首页链接"


def test_create_source_redetects_and_persists_news_source(client):
    detect_result = DetectResult(
        detected_type="api",
        name_suggestion="API News",
        api_config={"probe": {"mode": "json_list", "items_path": "items", "fields": {"title": "title", "url": "url"}}},
        notes=["识别为 JSON API。"],
    )

    with patch("app.api.source_routes.detect_source", return_value=detect_result):
        response = client.post(
            "/sources",
            json={
                "url": "https://api.example.com/news",
                "name": "",
                "main_category": "软件包适配",
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["name"] == "API News"
    assert payload["type"] == "api"
    assert payload["main_category"] == "软件包适配"

    session = client.app.dependency_overrides[get_db]()
    source = session.get(Source, payload["id"])
    assert source.stream == "news"
    assert source.api_config == detect_result.api_config


def test_create_source_rejects_invalid_category(client):
    response = client.post(
        "/sources",
        json={
            "url": "https://example.com/feed.xml",
            "name": "Feed",
            "main_category": "不存在的分类",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "未知内容类型"


def test_list_sources_returns_news_sources_only(client):
    session = client.app.dependency_overrides[get_db]()
    session.add_all(
        [
            Source(name="News", type="rss", url="https://example.com/feed.xml", stream="news", enabled=True),
            Source(name="Structured", type="api", url="https://example.com/api", stream="structured", enabled=True),
        ]
    )
    session.commit()

    response = client.get("/sources")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["News"]


def test_delete_source_cleans_related_items_and_runs(client):
    session = client.app.dependency_overrides[get_db]()
    source = Source(name="Delete Me", type="rss", url="https://example.com/feed.xml", stream="news", enabled=True)
    session.add(source)
    session.flush()
    item = Item(
        source_id=source.id,
        title="Item",
        url="https://example.com/item",
        url_hash="hash-url",
        content_hash="hash-content",
        raw_content="body",
    )
    session.add(item)
    session.flush()
    session.add(ItemSource(item_id=item.id, source_id=source.id, url=item.url))
    session.add(
        AgentCrawlRun(
            source_id=source.id,
            status="running",
            current_stage="planning",
            started_at=datetime.now(timezone.utc),
        )
    )
    session.commit()
    source_id = source.id
    item_id = item.id

    response = client.delete(f"/sources/{source_id}")

    session.expire_all()
    assert response.status_code == 204
    assert session.get(Source, source_id) is None
    assert session.get(Item, item_id) is None
    assert session.query(ItemSource).filter_by(source_id=source_id).count() == 0
    assert session.query(AgentCrawlRun).filter_by(source_id=source_id).count() == 0


def test_delete_source_missing_returns_404(client):
    response = client.delete("/sources/9999")

    assert response.status_code == 404


def test_discover_calls_api_discovery_and_returns_payload(client):
    from app.sources.api_discovery import ApiDiscoveryResult

    result = ApiDiscoveryResult(
        root_url="https://openanolis.cn/blog",
        success=True,
        api_url="https://api.openanolis.cn/blog/list",
        method="GET",
        items_path="data.records",
        fields={"title": "title", "url": "url", "published_at": "published_at"},
        name_suggestion="OpenAnolis 博客",
        sample_items=[],
        real_content_count=1,
    )
    with patch("app.sources.api_discovery.discover_api_source", return_value=result):
        response = client.post(
            "/sources/discover",
            json={"url": "https://openanolis.cn/blog"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["success"] is True
    assert payload["api_url"] == "https://api.openanolis.cn/blog/list"
    assert payload["items_path"] == "data.records"
    assert payload["fields"]["title"] == "title"


def test_discover_create_source_persists_api_source(client):
    from app.sources.api_discovery import ApiDiscoveryResult

    result = ApiDiscoveryResult(
        root_url="https://openanolis.cn/blog",
        success=True,
        api_url="https://api.openanolis.cn/blog/list",
        method="GET",
        items_path="data.records",
        fields={"title": "title", "url": "url", "published_at": "published_at"},
        name_suggestion="OpenAnolis 博客",
        real_content_count=1,
    )
    with patch("app.sources.api_discovery.discover_api_source", return_value=result):
        response = client.post(
            "/sources/discover",
            json={
                "url": "https://openanolis.cn/blog",
                "create_source": True,
                "name": "OpenAnolis 博客",
                "main_category": "软件包适配",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["created_source"]["type"] == "api"
    assert payload["created_source"]["name"] == "OpenAnolis 博客"

    listed = client.get("/sources").json()
    assert payload["created_source"]["id"] in [s["id"] for s in listed]


def test_create_from_probe_persists_api_source_in_list(client):
    response = client.post(
        "/sources/create-from-probe",
        json={
            "api_url": "https://api.openanolis.cn/blog/list?page=1&pageSize=10",
            "method": "GET",
            "items_path": "data.records",
            "fields": {"title": "title", "url": "url", "published_at": "published_at"},
            "name": "OpenAnolis 博客",
            "main_category": "软件包适配",
        },
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["type"] == "api"
    assert payload["name"] == "OpenAnolis 博客"

    listed = client.get("/sources").json()
    assert payload["id"] in [s["id"] for s in listed]
    assert "OpenAnolis 博客" in [s["name"] for s in listed]
