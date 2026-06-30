"""Task 13: /discovery/run + /discovery/methods/{id}/fetch 端点集成测试。"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.main import create_app
from app.models import Base, CrawlMethod

TEST_DB = "sqlite+pysqlite:///:memory:"


@pytest.fixture
def engine():
    eng = create_engine(TEST_DB, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


@pytest.fixture
def client(session):
    app = create_app()
    # get_db 复用同一个 session，避免 StaticPool 单连接下多 session 抢占
    app.dependency_overrides[get_db] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_discover_run_async_returns_run_id(client):
    """无重复 → 异步启动（mock start_discovery_run）→ 返回 started + run_id。"""
    with patch("app.api.discovery_routes.start_discovery_run", return_value=42):
        r = client.post("/discovery/run", json={"url": "https://x.com"})
    assert r.status_code == 200
    assert r.json() == {"status": "started", "run_id": 42}


def test_discover_run_duplicate_returns_existing(client, session):
    """同 domain 已有 method → force=false 返回 duplicate + 已有范式摘要。"""
    from app.enums import SourceType
    from app.models import CrawlMethod, CrawlMethodDomain, Source
    src = Source(name="x.com", type=SourceType.DISCOVERY.value, url="https://x.com")
    session.add(src); session.flush()
    m = CrawlMethod(domain="x.com", entry_url="https://x.com", source_id=src.id,
                    dsl_recipe={"actions": []}, signature="abc")
    session.add(m); session.flush()
    session.add(CrawlMethodDomain(domain="x.com", method_id=m.id)); session.commit()
    r = client.post("/discovery/run", json={"url": "https://x.com"})  # force 默认 false
    assert r.json()["status"] == "duplicate"
    assert r.json()["existing_method"]["method_id"] == m.id


def test_list_discovery_runs(client, session):
    from app.models import SiteDiscoveryRun
    session.add(SiteDiscoveryRun(site_url="https://x.com", status="completed"))
    session.commit()
    r = client.get("/discovery/runs")
    assert r.status_code == 200
    assert len(r.json()) >= 1
    assert r.json()[0]["status"] == "completed"


def test_get_discovery_run(client, session):
    from app.models import SiteDiscoveryRun
    run = SiteDiscoveryRun(site_url="https://x.com", status="running")
    session.add(run); session.commit()
    r = client.get(f"/discovery/runs/{run.id}")
    assert r.json()["status"] == "running"


def test_discovery_fetch_endpoint(client, session):
    """按已存 method 跑运行命（mock run_method）→ 200。"""
    from app.enums import SourceType
    from app.models import Source
    src = Source(name="x.com", type=SourceType.DISCOVERY.value, url="https://x.com")
    session.add(src); session.flush()
    m = CrawlMethod(domain="x.com", entry_url="https://x.com", source_id=src.id,
                    dsl_recipe={"recipe_type": "dsl", "entry_url": "https://x.com", "actions": []},
                    signature="abc")
    session.add(m); session.commit()
    with patch("app.api.discovery_routes.run_method",
               return_value={"items": [], "stats": {}}):
        r = client.post(f"/discovery/methods/{m.id}/fetch")
    assert r.status_code == 200
    assert r.json() == {"items": [], "stats": {}}
    # last_run_at 被更新
    session.refresh(m)
    assert m.last_run_at is not None
