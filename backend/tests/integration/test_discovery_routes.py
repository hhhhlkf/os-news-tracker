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
    assert r.json() == {"status": "started", "run_id": 42, "name": "x.com"}


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
    assert r.json()["items"] == []
    assert r.json()["stored_count"] == 0
    assert r.json()["discovered_count"] == 0
    # last_run_at 被更新
    session.refresh(m)
    assert m.last_run_at is not None


# --- Task 16: methods CRUD ---

def _make_crawl_method(session, domain="x.com", **overrides):
    """建 Source(discovery) + CrawlMethod（带 source_id），返回 method（已 flush，caller 负责 commit）。"""
    from app.enums import SourceType
    from app.models import CrawlMethod, Source
    src = Source(name=domain, type=SourceType.DISCOVERY.value, url=f"https://{domain}")
    session.add(src); session.flush()
    defaults = dict(entry_url=f"https://{domain}", dsl_recipe={"actions": []}, signature="a", status="active")
    defaults.update(overrides)
    m = CrawlMethod(domain=domain, source_id=src.id, **defaults)
    session.add(m); session.flush()
    return m


def test_list_methods(client, session):
    _make_crawl_method(session, domain="x.com")
    session.commit()
    r = client.get("/discovery/methods")
    assert r.status_code == 200
    assert any(m["domain"] == "x.com" for m in r.json())


def test_get_method_detail(client, session):
    m = _make_crawl_method(
        session, domain="x.com",
        dsl_recipe={"recipe_type": "dsl", "entry_url": "https://x.com", "actions": []},
    )
    session.commit()
    r = client.get(f"/discovery/methods/{m.id}")
    assert r.status_code == 200
    assert r.json()["dsl_recipe"]["recipe_type"] == "dsl"


def test_patch_method_disable(client, session):
    m = _make_crawl_method(session, domain="x.com", status="active")
    session.commit()
    r = client.patch(f"/discovery/methods/{m.id}", json={"status": "disabled"})
    assert r.status_code == 200
    assert r.json()["status"] == "disabled"
    session.refresh(m)
    assert m.status == "disabled"


def test_delete_method_cascades_domain(client, session):
    from app.models import CrawlMethodDomain
    m = _make_crawl_method(session, domain="x.com")
    session.add(CrawlMethodDomain(domain="x.com", method_id=m.id))
    session.commit()
    mid = m.id
    r = client.delete(f"/discovery/methods/{mid}")
    assert r.status_code == 204
    assert session.get(CrawlMethod, mid) is None
    assert session.query(CrawlMethodDomain).filter_by(method_id=mid).count() == 0


class _MockEnricher:
    """假 Enricher：enrich() 返回固定 EnrichedFields，避免测试真调 LLM。"""
    def enrich(self, item, *, existing_tags=None):
        from app.schemas import EnrichedFields
        return EnrichedFields(
            title_zh=item.title, summary="LLM富化摘要", tech_highlights=[],
            info_type="发布", importance="高", main_category="OS性能发展",
            sub_tags=["kernel"], keywords=["test"], merge_suggestions=[],
            confidence=0.9, should_store=True, reject_reason=None,
        )


def test_discovery_fetch_ingests_to_items(client, session, monkeypatch):
    """运行命 /fetch 把 DSL 产出经 pipeline + LLM Enricher 富化后入 items 表。"""
    from app.enums import SourceType, Stream
    from app.models import CrawlMethod, Item, Source
    src = Source(name="x.com", type=SourceType.DISCOVERY.value, url="https://x.com",
                 main_category="OS跟踪来源", stream=Stream.NEWS, enabled=True)
    session.add(src); session.flush()
    m = CrawlMethod(domain="x.com", entry_url="https://x.com", source_id=src.id,
                    dsl_recipe={
                        "recipe_type": "dsl", "entry_url": "https://x.com",
                        "actions": [
                            {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
                            {"op": "extract", "from": "obj.records",
                             "fields": {"title": "title", "url": "template:https://x.com/{item.no}"}},
                        ],
                    }, signature="a")
    session.add(m); session.commit()
    # mock DslInterpreter 的 fetch + Enricher 的 LLM 富化
    def fake_fetch(action, ctx):
        ctx["last_fetch"] = {"obj": {"records": [{"no": "1", "title": "A"}]}}

    monkeypatch.setattr(
        "app.discovery.interpreter.DslInterpreter.__init__",
        lambda self, **kw: setattr(self, "_fetch_fn", fake_fetch)
                          or setattr(self, "_browser_fn", None)
                          or setattr(self, "_page", None),
    )
    monkeypatch.setattr("app.processing.enricher.Enricher", _MockEnricher)

    r = client.post(f"/discovery/methods/{m.id}/fetch")
    assert r.status_code == 200
    # 验证入库 items（source_id 指向 discovery source 记录）
    items = session.query(Item).filter_by(source_id=src.id).all()
    assert len(items) >= 1
    assert items[0].url == "https://x.com/1"
    assert items[0].title == "A"
    # 验证走了 LLM 富化（不是 agent 旁路的静态默认值）
    assert items[0].importance == "高"              # mock enricher 给的，非旧默认"中"
    assert items[0].main_category == "OS性能发展"    # mock enricher 给的，非旧默认"OS跟踪来源"
    assert items[0].summary == "LLM富化摘要"


def test_discover_run_with_custom_name(client):
    """前端传 name 别名 → /run 透传 + 返回里带 name。"""
    with patch("app.api.discovery_routes.start_discovery_run", return_value=7):
        r = client.post("/discovery/run", json={"url": "https://openanolis.cn", "name": "OpenAnolis 博客"})
    assert r.status_code == 200
    assert r.json()["name"] == "OpenAnolis 博客"
    assert r.json()["run_id"] == 7
