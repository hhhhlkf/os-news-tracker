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
    assert r.json()["current_step"] is None  # 空 node_trace → None


def test_get_discovery_run_with_trace(client, session):
    """node_trace 逐步轨迹 + current_step（前端动画用）。"""
    from app.models import SiteDiscoveryRun
    run = SiteDiscoveryRun(site_url="https://x.com", status="running", node_trace=[
        {"step": "fetch_homepage", "status": "done", "ts": "2026-07-01T10:00:00+00:00"},
        {"step": "capture_network", "status": "done", "ts": "2026-07-01T10:00:05+00:00"},
        {"step": "supervisor", "status": "done", "ts": "2026-07-01T10:00:06+00:00"},
        {"step": "explorer", "status": "done", "ts": "2026-07-01T10:00:30+00:00"},
    ])
    session.add(run); session.commit()
    r = client.get(f"/discovery/runs/{run.id}")
    body = r.json()
    assert len(body["node_trace"]) == 4
    assert body["node_trace"][0]["step"] == "fetch_homepage"
    assert body["node_trace"][-1]["step"] == "explorer"
    assert body["current_step"] == "explorer"  # 最后一步 = 当前步骤


def test_cancel_discovery_run_marks_cancelled(client, session):
    from app.models import SiteDiscoveryRun
    run = SiteDiscoveryRun(site_url="https://x.com", status="running")
    session.add(run); session.commit()
    r = client.post(f"/discovery/runs/{run.id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["error_message"] == "已手动取消"
    session.refresh(run)
    assert run.status == "cancelled"


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


def test_discovery_fetch_applies_time_window_and_target_count(client, session):
    """运行命 /fetch 接收抓取限制，并在入 pipeline 前先做时间过滤和总条目截断。"""
    from app.enums import SourceType
    from app.models import Source

    src = Source(name="x.com", type=SourceType.DISCOVERY.value, url="https://x.com")
    session.add(src)
    session.flush()
    m = CrawlMethod(
        domain="x.com",
        entry_url="https://x.com",
        source_id=src.id,
        dsl_recipe={"recipe_type": "dsl", "entry_url": "https://x.com", "actions": []},
        signature="abc",
    )
    session.add(m)
    session.commit()

    with patch(
        "app.api.discovery_routes.run_method",
        return_value={
            "items": [
                {"title": "recent-a", "url": "https://x.com/a", "published_at": "2026-07-02T10:00:00Z"},
                {"title": "recent-b", "url": "https://x.com/b", "published_at": "2026-07-01T10:00:00Z"},
                {"title": "old-c", "url": "https://x.com/c", "published_at": "2026-05-01T10:00:00Z"},
            ],
            "stats": {},
        },
    ):
        r = client.post(
            f"/discovery/methods/{m.id}/fetch",
            json={"time_mode": "relative", "relative_range": "7d", "target_count": 1},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["discovered_count"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["url"] == "https://x.com/a"


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
    m = _make_crawl_method(session, domain="x.com")
    from app.models import Source
    source = session.get(Source, m.source_id)
    source.name = "LangChain Blog"
    session.commit()
    r = client.get("/discovery/methods")
    assert r.status_code == 200
    assert any(m["domain"] == "x.com" and m["source_name"] == "LangChain Blog" for m in r.json())


def test_get_method_detail(client, session):
    m = _make_crawl_method(
        session, domain="x.com",
        dsl_recipe={"recipe_type": "dsl", "entry_url": "https://x.com", "actions": []},
    )
    from app.models import Source
    source = session.get(Source, m.source_id)
    source.name = "LangChain Blog"
    session.commit()
    r = client.get(f"/discovery/methods/{m.id}")
    assert r.status_code == 200
    assert r.json()["dsl_recipe"]["recipe_type"] == "dsl"
    assert r.json()["last_run_status"] is None
    assert r.json()["source_name"] == "LangChain Blog"


def test_patch_method_disable(client, session):
    m = _make_crawl_method(session, domain="x.com", status="active")
    session.commit()
    r = client.patch(f"/discovery/methods/{m.id}", json={"status": "disabled"})
    assert r.status_code == 200
    assert r.json()["status"] == "disabled"
    session.refresh(m)
    assert m.status == "disabled"


def test_delete_method_cascades_domain(client, session):
    from app.models import CrawlMethodDomain, SiteDiscoveryRun
    m = _make_crawl_method(session, domain="x.com")
    session.add(CrawlMethodDomain(domain="x.com", method_id=m.id))
    run = SiteDiscoveryRun(site_url="https://x.com", status="completed", resulting_method_id=m.id)
    session.add(run)
    session.commit()
    mid = m.id
    r = client.delete(f"/discovery/methods/{mid}")
    assert r.status_code == 204
    assert session.get(CrawlMethod, mid) is None
    assert session.query(CrawlMethodDomain).filter_by(method_id=mid).count() == 0
    session.refresh(run)
    assert run.resulting_method_id is None


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


def test_discovery_fetch_logs_are_visible_to_news_run_log_panel(client, session, monkeypatch):
    from app.enums import SourceType
    from app.models import Source
    from app.run_logs import clear_run_logs

    clear_run_logs()
    src = Source(name="x.com", type=SourceType.DISCOVERY.value, url="https://x.com")
    session.add(src)
    session.flush()
    m = CrawlMethod(
        domain="x.com",
        entry_url="https://x.com",
        source_id=src.id,
        dsl_recipe={"recipe_type": "dsl", "entry_url": "https://x.com", "actions": []},
        signature="abc",
    )
    session.add(m)
    session.commit()

    monkeypatch.setattr(
        "app.api.discovery_routes.run_method",
        lambda recipe: {
            "items": [
                {"title": "recent", "url": "https://x.com/a", "published_at": "2026-07-02T10:00:00Z"},
                {"title": "older", "url": "https://x.com/b", "published_at": "2026-06-02T10:00:00Z"},
            ],
            "stats": {"discovered_count": 2},
        },
    )

    r = client.post(
        f"/discovery/methods/{m.id}/fetch",
        json={
            "time_mode": "absolute",
            "start_at": "2026-07-01T00:00:00Z",
            "end_at": "2026-07-03T00:00:00Z",
            "target_count": 1,
        },
    )
    assert r.status_code == 200

    logs = client.get("/news-run/logs").json()["logs"]
    fetch_logs = [log for log in logs if log["stage"] == "抓方式" and log.get("method_id") == m.id]
    assert fetch_logs
    assert any("开始抓取爬取方式" in log["message"] for log in logs)
    assert any("DSL 执行完成" in log["message"] and log.get("raw_count") == 2 for log in fetch_logs)
    assert any(
        "抓取限制已应用" in log["message"]
        and log.get("input_count") == 2
        and log.get("kept_count") == 1
        and log.get("dropped_count") == 1
        and log.get("target_count") == 1
        for log in fetch_logs
    )
    assert any(
        "爬取方式抓取完成" in log["message"]
        and log.get("discovered_count") == 1
        and log.get("stored_count") == 0
        and log.get("last_run_status") == "empty"
        for log in fetch_logs
    )


def test_discovery_fetch_logs_pipeline_item_outcomes(client, session, monkeypatch):
    from app.enums import SourceType, Stream
    from app.models import Source
    from app.run_logs import clear_run_logs

    clear_run_logs()
    src = Source(
        name="x.com",
        type=SourceType.DISCOVERY.value,
        url="https://x.com",
        main_category="OS跟踪来源",
        stream=Stream.NEWS,
        enabled=True,
    )
    session.add(src)
    session.flush()
    m = CrawlMethod(
        domain="x.com",
        entry_url="https://x.com",
        source_id=src.id,
        dsl_recipe={"recipe_type": "dsl", "entry_url": "https://x.com", "actions": []},
        signature="abc",
    )
    session.add(m)
    session.commit()

    monkeypatch.setattr(
        "app.api.discovery_routes.run_method",
        lambda recipe: {
            "items": [
                {"title": "duplicate-a", "url": "https://x.com/a", "published_at": "2026-07-02T10:00:00Z"},
                {"title": "new-b", "url": "https://x.com/b", "published_at": "2026-07-02T11:00:00Z"},
            ],
            "stats": {"discovered_count": 2},
        },
    )

    from app.pipeline import ProcessItemResult

    results = iter([
        ProcessItemResult(stored=False, reason="duplicate"),
        ProcessItemResult(stored=False, reason="enrich_reject", detail="summary too weak"),
    ])

    def fake_process_item_result(self, source, raw):
        return next(results)

    monkeypatch.setattr("app.pipeline.Pipeline.process_item_result", fake_process_item_result)

    r = client.post(f"/discovery/methods/{m.id}/fetch")
    assert r.status_code == 200

    logs = client.get("/news-run/logs").json()["logs"]
    method_process_logs = [
        log for log in logs
        if log["stage"] == "process" and log.get("method_id") == m.id
    ]
    assert any(
        log["message"] == "候选未入库"
        and log.get("url") == "https://x.com/a"
        and log.get("reason") == "duplicate"
        for log in method_process_logs
    )
    assert any(
        log["message"] == "候选未入库"
        and log.get("url") == "https://x.com/b"
        and log.get("reason") == "enrich_reject"
        and log.get("reason_detail") == "summary too weak"
        for log in method_process_logs
    )


def test_discover_run_with_custom_name(client):
    """前端传 name 别名 → /run 透传 + 返回里带 name。"""
    with patch("app.api.discovery_routes.start_discovery_run", return_value=7):
        r = client.post("/discovery/run", json={"url": "https://openanolis.cn", "name": "OpenAnolis 博客"})
    assert r.status_code == 200
    assert r.json()["name"] == "OpenAnolis 博客"
    assert r.json()["run_id"] == 7
