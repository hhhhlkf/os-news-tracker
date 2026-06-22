"""Agent API 集成测试 — 验证 /sources/agent CRUD、触发和管理端点。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from app.api.deps import get_db, get_current_user
from app.api.main import create_app
from app.models import AgentCrawlRun, Base, Source, User

TEST_DB = "sqlite+pysqlite:///:memory:"


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
        start_run.assert_called_once_with(source_id)

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


class TestViewMemory:
    def test_admin_can_view_memory(self, admin_client, auth_headers):
        """管理员可查看 SiteMemory。"""
        r = admin_client.get("/sources/agent/1/memory", headers=auth_headers)
        # 可能 200（空）或正常返回
        assert r.status_code in (200, 404)

    def test_normal_user_cannot_view_memory(self, client, auth_headers):
        """普通用户访问 memory 应返回 403。"""
        r = client.get("/sources/agent/1/memory", headers=auth_headers)
        assert r.status_code == 403
