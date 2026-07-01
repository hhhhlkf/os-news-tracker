"""Task 14: 端到端集成测试 — mock 站点 → 生成命（mock LLM）→ 存 method → 运行命 → 产出。

生成命用 mock run_discovery（绕开真 LLM/PostgresSaver）；运行命走真 DslInterpreter，
仅 mock HTTP fetch（注入 fake_fetch），验证 DSL Recipe 真实跑通产出 N 条。
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.main import create_app
from app.enums import SourceType
from app.models import Base, CrawlMethod, Source

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
    app.dependency_overrides[get_db] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


_FIXED_RECIPE = {
    "recipe_type": "dsl",
    "entry_url": "https://mockx.com",
    "actions": [
        {"op": "fetch", "mode": "json", "url": "https://mockx.com/api"},
        {"op": "extract", "from": "obj.records",
         "fields": {"title": "title", "url": "template:https://mockx.com/{item.no}"}},
        {"op": "dedup_by", "field": "url"},
    ],
}


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


def test_e2e_json_api_site(client, session, monkeypatch):
    """mock JSON API 站：生成命（mock）→ 存 method → 运行命（mock fetch）→ 产出 2 条。"""
    # 1. 生成命：mock start_discovery_run（不真起后台线程），仅验 /run 异步端点通
    with patch("app.api.discovery_routes.start_discovery_run", return_value=99):
        r_run = client.post("/discovery/run", json={"url": "https://mockx.com"})
    assert r_run.status_code == 200
    assert r_run.json() == {"status": "started", "run_id": 99}

    # 2. 直接造一条 method（含合法 DSL Recipe）跑运行命，绕过 LLM
    src = Source(name="mockx.com", type=SourceType.DISCOVERY.value, url="https://mockx.com")
    session.add(src); session.flush()
    m = CrawlMethod(domain="mockx.com", entry_url="https://mockx.com", source_id=src.id,
                    dsl_recipe=_FIXED_RECIPE, signature="e2e")
    session.add(m); session.commit()

    # 3. mock DslInterpreter 的 fetch：注入固定 JSON 产出（不真发 HTTP）
    def fake_fetch(action, ctx):
        ctx["last_fetch"] = {"obj": {"records": [{"no": "1", "title": "A"}, {"no": "2", "title": "B"}]}}

    monkeypatch.setattr(
        "app.discovery.interpreter.DslInterpreter.__init__",
        lambda self, **kw: setattr(self, "_fetch_fn", fake_fetch)
                          or setattr(self, "_browser_fn", None)
                          or setattr(self, "_page", None),
    )
    monkeypatch.setattr("app.processing.enricher.Enricher", _MockEnricher)

    # 4. 运行命：真 DslInterpreter 跑 _FIXED_RECIPE（fetch mock + 真 extract/dedup）
    r = client.post(f"/discovery/methods/{m.id}/fetch")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["stats"]["discovered_count"] == 2
    assert body["items"][0]["url"] == "https://mockx.com/1"
    # last_run_at 被更新
    session.refresh(m)
    assert m.last_run_at is not None
