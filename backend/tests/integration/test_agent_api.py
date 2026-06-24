"""Agent API 集成测试 — 验证 /sources/agent CRUD、触发和管理端点。"""

from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from app.api.deps import get_db, get_current_user
from app.api.main import create_app
from app.models import AgentCrawlRun, AgentSiteMemory, Base, Source, User

TEST_DB = "sqlite+pysqlite:///:memory:"
DEFAULT_AGENT_TIME_WINDOW = {
    "time_mode": "relative",
    "relative_range": "7d",
    "start_at": None,
    "end_at": None,
}


def _make_user(user_id="test-user-id", role="user"):
    """构造一个 mock User 用于身份验证覆盖。"""
    user = User(id=user_id, email="test@test.com", password_hash="hash", role=role)
    return user


@pytest.fixture
def client():
    """创建使用内存 SQLite 的 TestClient，覆盖认证依赖。"""
    engine = create_engine(
        TEST_DB,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionTest = sessionmaker(bind=engine)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: SessionTest()
    # 默认覆盖为普通用户
    app.dependency_overrides[get_current_user] = lambda: _make_user()
    yield TestClient(app)
    Base.metadata.drop_all(engine)


@pytest.fixture
def public_client():
    """创建仅覆盖数据库依赖的 TestClient，用于验证公开 Agent 接口。"""
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


@pytest.fixture
def admin_client(client):
    """覆盖为管理员用户。"""
    from app.api.main import create_app
    # 直接修改全局 app 的依赖覆盖
    app = client.app
    original = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: _make_user(role="admin")
    yield client
    app.dependency_overrides[get_current_user] = original


@pytest.fixture
def auth_headers():
    """构造 Bearer token 头部。由于我们用 dependency_overrides 绕过了认证，
    只需要提供格式正确的 Authorization header 即可。"""
    return {"Authorization": "Bearer test-token"}


class TestCreateAgentSource:
    def test_create_minimal(self, client, auth_headers):
        """最小字段创建 agent source。"""
        r = client.post("/sources/agent", json={
            "name": "Phoronix Blog", "root_url": "https://www.phoronix.com/",
            "focus_areas": ["kernel", "gpu"], "topic_groups": ["技术迭代"],
        }, headers=auth_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "Phoronix Blog"
        assert data["url"] == "https://www.phoronix.com/"
        assert data["enabled"] is True
        assert data["config"]["focus_areas"] == ["kernel", "gpu"]

    def test_create_with_all_config_fields(self, client, auth_headers):
        """完整配置字段创建。"""
        r = client.post("/sources/agent", json={
            "name": "Full Config", "root_url": "https://example.com/",
            "focus_areas": ["a"], "topic_groups": ["b"],
            "crawl_depth": 2, "max_urls_per_run": 10,
            "quality_threshold": 5, "crawl_workers": 4,
            "quality_workers": 2, "summary_workers": 1,
        }, headers=auth_headers)
        assert r.status_code == 201
        cfg = r.json()["config"]
        assert cfg["crawl_depth"] == 2
        assert cfg["max_urls_per_run"] == 10
        assert cfg["quality_threshold"] == 5


class TestListAgentSources:
    def test_list_empty(self, client, auth_headers):
        """无 agent source 时返回空列表。"""
        r = client.get("/sources/agent", headers=auth_headers)
        assert r.status_code == 200
        assert r.json() == []

    def test_list_with_items(self, client, auth_headers):
        """创建后应出现在列表中。"""
        client.post("/sources/agent", json={
            "name": "Source 1", "root_url": "https://a.com/",
            "focus_areas": ["kernel"], "topic_groups": [],
        }, headers=auth_headers)
        client.post("/sources/agent", json={
            "name": "Source 2", "root_url": "https://b.com/",
            "focus_areas": ["eBPF"], "topic_groups": [],
        }, headers=auth_headers)
        r = client.get("/sources/agent", headers=auth_headers)
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_list_is_public_without_auth(self, public_client):
        """Agent source 列表应允许未登录访问。"""
        r = public_client.get("/sources/agent")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_alias_works_without_agent_path(self, public_client):
        """兼容别名路径应返回同样的 Agent source 列表。"""
        r = public_client.get("/crawl-sources")
        assert r.status_code == 200
        assert r.json() == []


class TestUpdateAgentSource:
    def test_update_existing(self, client, auth_headers):
        """更新已存在的 agent source。"""
        created = client.post("/sources/agent", json={
            "name": "Old Name", "root_url": "https://old.com/",
            "focus_areas": [], "topic_groups": [],
        }, headers=auth_headers)
        source_id = created.json()["id"]
        r = client.put(f"/sources/agent/{source_id}", json={
            "name": "New Name", "root_url": "https://new.com/",
            "focus_areas": ["kernel"], "topic_groups": ["项目动态"],
        }, headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["name"] == "New Name"
        assert r.json()["url"] == "https://new.com/"

    def test_update_nonexistent_returns_404(self, client, auth_headers):
        """更新不存在的源返回 404。"""
        r = client.put("/sources/agent/99999", json={
            "name": "X", "root_url": "https://x.com/",
            "focus_areas": [], "topic_groups": [],
        }, headers=auth_headers)
        assert r.status_code == 404


class TestDeleteAgentSource:
    def test_delete_existing(self, client, auth_headers):
        """删除已存在的 agent source。"""
        created = client.post("/sources/agent", json={
            "name": "To Delete", "root_url": "https://del.com/",
            "focus_areas": [], "topic_groups": [],
        }, headers=auth_headers)
        source_id = created.json()["id"]
        r = client.delete(f"/sources/agent/{source_id}", headers=auth_headers)
        assert r.status_code == 204

    def test_delete_nonexistent_returns_404(self, client, auth_headers):
        """删除不存在的源返回 404。"""
        r = client.delete("/sources/agent/99999", headers=auth_headers)
        assert r.status_code == 404

    def test_delete_keeps_crawled_items(self, client, auth_headers):
        """删除 Agent 流程保留此前抓取的条目，并改挂到原始候选来源。"""
        from app.models import Item, ItemSource

        session = client.app.dependency_overrides[get_db]()
        candidate = Source(
            name="Origin", type="rss", url="https://keep.com/feed.xml",
            stream="news", enabled=True,
        )
        session.add(candidate)
        session.flush()
        agent = Source(
            name="Agent Keep", type="agent_crawl", url="https://keep.com/feed.xml",
            stream="news", enabled=True,
            api_config={"candidate_source_id": candidate.id},
        )
        session.add(agent)
        session.flush()
        item = Item(
            source_id=agent.id,
            title="Crawled Item",
            url="https://keep.com/article",
            url_hash="keep-hash",
            content_hash="keep-content",
            raw_content="body",
        )
        session.add(item)
        session.flush()
        session.add(ItemSource(item_id=item.id, source_id=agent.id, url=item.url))
        session.commit()
        agent_id, candidate_id, item_id = agent.id, candidate.id, item.id

        r = client.delete(f"/sources/agent/{agent_id}", headers=auth_headers)
        assert r.status_code == 204

        session.expire_all()
        assert session.get(Source, agent_id) is None
        kept = session.get(Item, item_id)
        assert kept is not None
        assert kept.source_id == candidate_id
        assert session.query(ItemSource).filter_by(source_id=agent_id).count() == 0
        assert session.query(ItemSource).filter_by(item_id=item_id, source_id=candidate_id).count() == 1


class TestListRuns:
    def test_empty_runs(self, client, auth_headers):
        """新创建的源应无运行记录。"""
        created = client.post("/sources/agent", json={
            "name": "Run Test", "root_url": "https://run.com/",
            "focus_areas": [], "topic_groups": [],
        }, headers=auth_headers)
        source_id = created.json()["id"]
        r = client.get(f"/sources/agent/{source_id}/runs", headers=auth_headers)
        assert r.status_code == 200
        assert r.json() == []


class TestAgentMemory:
    def test_view_memory_is_public_without_auth(self, public_client):
        """SiteMemory 查看应允许未登录访问。"""
        created = public_client.post("/sources/agent", json={
            "name": "Memory Source", "root_url": "https://memory.com/",
            "focus_areas": [], "topic_groups": [],
        })
        source_id = created.json()["id"]

        session = public_client.app.dependency_overrides[get_db]()
        session.add(
            AgentSiteMemory(
                source_id=source_id,
                url_pattern="https://memory.com/advisories/*",
                verdict="accept",
                quality_score=5,
                quality_reason="high signal",
                seen_count=2,
            )
        )
        session.commit()

        r = public_client.get(f"/sources/agent/{source_id}/memory")

        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["url_pattern"] == "https://memory.com/advisories/*"


class TestTriggerAgentRun:
    def test_trigger_agent_run_accepts_agent_source(self, client, auth_headers):
        """agent source 应可被立即触发。"""
        created = client.post("/sources/agent", json={
            "name": "Trigger Test", "root_url": "https://trigger.com/",
            "focus_areas": ["kernel"], "topic_groups": [],
        }, headers=auth_headers)
        source_id = created.json()["id"]

        with patch("app.api.agent_routes._start_agent_source_run", create=True) as start_run:
            r = client.post(f"/sources/agent/{source_id}/run", headers=auth_headers)

        assert r.status_code == 202
        assert r.json() == {
            "message": "Agent crawl accepted",
            "source_id": source_id,
            "accepted": True,
        }
        start_run.assert_called_once_with(source_id, DEFAULT_AGENT_TIME_WINDOW)

    def test_trigger_agent_run_returns_404_for_non_agent_source(self, client, auth_headers):
        """非 agent_crawl source 不应走该触发端点。"""
        session = client.app.dependency_overrides[get_db]()
        source = Source(
            name="RSS Source",
            type="rss",
            url="https://example.com/rss.xml",
            stream="news",
            enabled=True,
        )
        session.add(source)
        session.commit()
        session.refresh(source)

        r = client.post(f"/sources/agent/{source.id}/run", headers=auth_headers)

        assert r.status_code == 404
        assert r.json()["detail"] == "Agent source not found"

    def test_trigger_agent_run_returns_409_when_same_source_is_running(self, client, auth_headers):
        """同一 source 已有运行态记录时应拒绝重复触发。"""
        created = client.post("/sources/agent", json={
            "name": "Busy Source", "root_url": "https://busy.com/",
            "focus_areas": [], "topic_groups": [],
        }, headers=auth_headers)
        source_id = created.json()["id"]

        session = client.app.dependency_overrides[get_db]()
        session.add(
            AgentCrawlRun(
                source_id=source_id,
                status="running",
            )
        )
        session.commit()

        r = client.post(f"/sources/agent/{source_id}/run", headers=auth_headers)

        assert r.status_code == 409
        body = r.json()
        assert body["detail"] == "Agent source is already running"
        assert body["current_stage"] == "planning"
        assert isinstance(body["run_id"], int)

    def test_trigger_agent_run_accepts_public_request(self, public_client):
        """单源 Agent 立即运行应允许未登录触发。"""
        created = public_client.post("/sources/agent", json={
            "name": "Public Trigger", "root_url": "https://public-trigger.com/",
            "focus_areas": ["kernel"], "topic_groups": [],
        })
        source_id = created.json()["id"]

        with patch("app.api.agent_routes._start_agent_source_run", create=True) as start_run:
            r = public_client.post(f"/sources/agent/{source_id}/run")

        assert r.status_code == 202
        assert r.json()["accepted"] is True
        start_run.assert_called_once_with(source_id, DEFAULT_AGENT_TIME_WINDOW)


class TestAgentRunCandidates:
    def test_list_agent_run_candidates_excludes_agent_sources(self, client, auth_headers):
        """候选列表应来自标准抓取来源，不包含 agent_crawl。"""
        session = client.app.dependency_overrides[get_db]()
        session.add_all([
            Source(
                name="Fedora Updates",
                type="rss",
                url="https://example.com/fedora.xml",
                stream="news",
                enabled=True,
                main_category="OS跟踪来源",
            ),
            Source(
                name="Existing Agent",
                type="agent_crawl",
                url="https://example.com/agent",
                stream="news",
                enabled=True,
            ),
        ])
        session.commit()

        r = client.get("/sources/agent/candidates", headers=auth_headers)

        assert r.status_code == 200
        assert len(r.json()["items"]) == 1
        assert r.json()["items"][0]["name"] == "Fedora Updates"
        assert r.json()["items"][0]["source_type"] == "rss"
        assert r.json()["total"] == 1
        assert r.json()["page"] == 1
        assert r.json()["page_size"] == 5

    def test_list_candidates_is_public_without_auth(self, public_client):
        """候选来源列表应允许未登录访问。"""
        session = public_client.app.dependency_overrides[get_db]()
        session.add(
            Source(
                name="OpenELA",
                type="rss",
                url="https://example.com/openela.xml",
                stream="news",
                enabled=True,
                main_category="OS跟踪来源",
            )
        )
        session.commit()

        r = public_client.get("/sources/agent/candidates")

        assert r.status_code == 200
        assert len(r.json()["items"]) == 1
        assert r.json()["items"][0]["name"] == "OpenELA"

    def test_candidates_alias_works_without_agent_path(self, public_client):
        """兼容别名路径应返回同样的候选来源列表。"""
        session = public_client.app.dependency_overrides[get_db]()
        session.add(
            Source(
                name="Alias Fedora",
                type="rss",
                url="https://example.com/alias-fedora.xml",
                stream="news",
                enabled=True,
                main_category="OS跟踪来源",
            )
        )
        session.commit()

        r = public_client.get("/crawl-sources/candidates")

        assert r.status_code == 200
        assert len(r.json()["items"]) == 1
        assert r.json()["items"][0]["name"] == "Alias Fedora"

    def test_list_candidates_backfills_seed_sources_when_database_is_empty(self, public_client, monkeypatch, tmp_path):
        """空库时应从 seed YAML 自愈出标准抓取候选来源。"""
        seed_file = tmp_path / "seed_sources.yaml"
        seed_file.write_text(
            """
- name: Seeded Fedora
  type: rss
  url: https://example.com/fedora.xml
  stream: news
  main_category: OS跟踪来源
  enabled: true
- name: Structured Only
  type: api
  url: https://example.com/structured.json
  stream: structured
  main_category: OS跟踪来源
  enabled: true
""".strip(),
            encoding="utf-8",
        )
        monkeypatch.setattr("app.api.agent_routes.SEED_SOURCES_YAML", Path(seed_file))

        r = public_client.get("/sources/agent/candidates")

        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "Seeded Fedora"
        assert body["items"][0]["source_type"] == "rss"

        session = public_client.app.dependency_overrides[get_db]()
        seeded = session.query(Source).filter(Source.name == "Seeded Fedora").one_or_none()
        assert seeded is not None
        assert seeded.stream == "news"

    def test_list_candidates_supports_backend_pagination(self, public_client):
        """候选来源列表应支持 page/page_size 分页。"""
        session = public_client.app.dependency_overrides[get_db]()
        session.add_all(
            [
                Source(
                    name=f"Source {index:02d}",
                    type="rss",
                    url=f"https://example.com/{index}.xml",
                    stream="news",
                    enabled=True,
                    main_category="OS跟踪来源",
                )
                for index in range(1, 13)
            ]
        )
        session.commit()

        r = public_client.get("/sources/agent/candidates?page=2&page_size=5")

        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 12
        assert body["page"] == 2
        assert body["page_size"] == 5
        assert body["total_pages"] == 3
        assert [item["name"] for item in body["items"]] == [
            "Source 06",
            "Source 07",
            "Source 08",
            "Source 09",
            "Source 10",
        ]

    def test_trigger_from_candidate_creates_agent_source_and_runs(self, client, auth_headers):
        """从标准抓取来源一键运行时，应创建 agent source 并立即触发。"""
        session = client.app.dependency_overrides[get_db]()
        source = Source(
            name="Ubuntu Security",
            type="rss",
            url="https://example.com/ubuntu.xml",
            stream="news",
            enabled=True,
            main_category="OS跟踪来源",
        )
        session.add(source)
        session.commit()
        session.refresh(source)

        with patch("app.api.agent_routes._start_agent_source_run") as start_run:
            r = client.post(
                f"/sources/agent/candidates/{source.id}/run",
                headers=auth_headers,
                json={
                    "time_mode": "absolute",
                    "start_at": "2026-06-10T00:00:00Z",
                    "end_at": "2026-06-30T23:59:59Z",
                },
            )

        assert r.status_code == 202
        body = r.json()
        assert body["accepted"] is True
        assert body["created"] is True
        assert body["candidate_source_id"] == source.id
        assert isinstance(body["agent_source_id"], int)
        start_run.assert_called_once_with(
            body["agent_source_id"],
            {
                "time_mode": "absolute",
                "relative_range": None,
                "start_at": "2026-06-10T00:00:00Z",
                "end_at": "2026-06-30T23:59:59Z",
            },
        )

        agent_source = session.get(Source, body["agent_source_id"])
        assert agent_source is not None
        assert agent_source.api_config == {
            "seed_type": "rss",
            "seed_url": "https://example.com/ubuntu.xml",
            "candidate_source_id": source.id,
        }

    def test_trigger_from_candidate_reuses_existing_agent_source(self, client, auth_headers):
        """同 URL 的 agent source 已存在时，应复用而不是重复创建。"""
        session = client.app.dependency_overrides[get_db]()
        candidate = Source(
            name="Rocky Linux",
            type="rss",
            url="https://example.com/rocky.xml",
            stream="news",
            enabled=True,
            main_category="OS跟踪来源",
        )
        session.add(candidate)
        session.flush()
        agent_source = Source(
            name="Rocky Linux",
            type="agent_crawl",
            url="https://example.com/rocky.xml",
            stream="news",
            enabled=True,
        )
        session.add(agent_source)
        session.flush()
        session.commit()

        with patch("app.api.agent_routes._start_agent_source_run") as start_run:
            r = client.post(f"/sources/agent/candidates/{candidate.id}/run", headers=auth_headers)

        assert r.status_code == 202
        body = r.json()
        assert body["created"] is False
        assert body["agent_source_id"] == agent_source.id
        start_run.assert_called_once_with(agent_source.id, DEFAULT_AGENT_TIME_WINDOW)


class TestViewMemory:
    def test_authenticated_requests_can_still_view_memory(self, client, auth_headers):
        """带认证头的请求仍可正常访问 SiteMemory。"""
        r = client.get("/sources/agent/1/memory", headers=auth_headers)
        assert r.status_code == 200
