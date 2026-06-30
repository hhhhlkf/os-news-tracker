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


def test_discover_run_endpoint(client):
    """force=false 无重复 → 调 run_discovery（mock）→ 返回 completed + verdict。"""
    with patch("app.api.discovery_routes.run_discovery",
               return_value={"verdict": "dsl", "method_id": 1}):
        r = client.post("/discovery/run", json={"url": "https://x.com"})
    assert r.status_code == 200
    assert r.json()["verdict"] == "dsl"
    assert r.json()["status"] == "completed"


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
