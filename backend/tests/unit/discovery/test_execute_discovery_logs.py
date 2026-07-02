"""Regression: _execute_discovery per-step append_run_log must carry run_id.

The frontend log panel (Task 9) polls /news-run/logs and filters by run_id.
Per-step progress logs emitted inside the g.stream() loop must include run_id,
otherwise the panel drops them and only shows the 3 "任务"-stage lines.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch


class _FakeCheckpointer:
    def setup(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeGraph:
    """Yields per-step chunks; get_state returns a completed 'dsl' verdict."""
    def __init__(self):
        self._chunks = [
            {"capture_network": {
                "network_captures": [
                    {"api_url": "https://x.test/api/a"},
                    {"api_url": "https://x.test/api/b"},
                    {"api_url": "https://x.test/api/c"},
                    {"api_url": "https://x.test/api/d"},
                ],
            }},
            {"supervisor": {}},
            {"explorer": {"exploration": {"source_type": "rss", "success": True}}},
            {"validator": {"url_rule": {"mode": "existing_url"}}},
        ]

    def stream(self, initial, *, config=None, stream_mode="updates"):
        for c in self._chunks:
            yield c

    def get_state(self, config):
        return SimpleNamespace(values={"verdict": "dsl", "method_id": 7, "token_used": 100})


class _FakeSession:
    def __init__(self):
        self.run = SimpleNamespace(
            node_trace=[], status="running", resulting_method_id=None,
            llm_token_usage=0, ended_at=None, error_message=None,
        )

    def get(self, model, run_id):
        return self.run

    def commit(self): pass

    def rollback(self): pass

    def close(self): pass


def test_per_step_logs_carry_run_id():
    from app.discovery import graph as graph_mod

    calls = []

    def capture_append(stage, message, *, source=None, level="info", **fields):
        calls.append({"stage": stage, "message": message, "source": source,
                      "level": level, **fields})
        return {}

    fake_session = _FakeSession()
    with patch("langgraph.checkpoint.postgres.PostgresSaver.from_conn_string",
               lambda *a, **k: _FakeCheckpointer()), \
         patch.object(graph_mod, "_to_psycopg_conn_string", return_value="postgresql://x"), \
         patch.object(graph_mod, "build_graph", return_value=_FakeGraph()), \
         patch("app.db.SessionLocal", return_value=fake_session), \
         patch("app.run_logs.append_run_log", side_effect=capture_append):
        graph_mod._execute_discovery(
            run_id=42, site_url="https://x.test", force=False, name=None,
        )

    per_step = [c for c in calls if "step" in c]
    assert per_step, "expected per-step append_run_log calls from the stream loop"
    for c in per_step:
        assert c.get("run_id") == 42, (
            f"per-step log for step={c['step']} missing run_id: {c}"
        )


def test_per_step_logs_use_flow_node_labels_and_richer_details():
    from app.discovery import graph as graph_mod

    calls = []

    def capture_append(stage, message, *, source=None, level="info", **fields):
        calls.append({"stage": stage, "message": message, "source": source,
                      "level": level, **fields})
        return {}

    fake_session = _FakeSession()
    with patch("langgraph.checkpoint.postgres.PostgresSaver.from_conn_string",
               lambda *a, **k: _FakeCheckpointer()), \
         patch.object(graph_mod, "_to_psycopg_conn_string", return_value="postgresql://x"), \
         patch.object(graph_mod, "build_graph", return_value=_FakeGraph()), \
         patch("app.db.SessionLocal", return_value=fake_session), \
         patch("app.run_logs.append_run_log", side_effect=capture_append):
        graph_mod._execute_discovery(
            run_id=43, site_url="https://x.test", force=False, name=None,
        )

    capture_log = next(c for c in calls if c.get("step") == "capture_network")
    assert capture_log["stage"] == "抓网络请求"
    assert "sample=" in capture_log["message"]
    assert "/api/a" in capture_log["message"]

    supervisor_log = next(c for c in calls if c.get("step") == "supervisor")
    assert supervisor_log["stage"] == "路由"
    assert "next=探查" in supervisor_log["message"]


def test_explorer_logs_include_generation_details():
    from app.discovery import graph as graph_mod

    calls = []

    def capture_append(stage, message, *, source=None, level="info", **fields):
        calls.append({"stage": stage, "message": message, "source": source,
                      "level": level, **fields})
        return {}

    class _GraphWithExplorerDetails(_FakeGraph):
        def __init__(self):
            self._chunks = [
                {"explorer": {
                    "exploration": {"source_type": "json_api", "success": True},
                    "explorer_agent_output": "原始输出：先解释再给 JSON",
                    "explorer_synthesis_output": '{"source_type":"json_api","success":true}',
                }},
            ]

        def get_state(self, config):
            return SimpleNamespace(values={"verdict": "failed", "method_id": None, "token_used": 0})

    fake_session = _FakeSession()
    with patch("langgraph.checkpoint.postgres.PostgresSaver.from_conn_string",
               lambda *a, **k: _FakeCheckpointer()), \
         patch.object(graph_mod, "_to_psycopg_conn_string", return_value="postgresql://x"), \
         patch.object(graph_mod, "build_graph", return_value=_GraphWithExplorerDetails()), \
         patch("app.db.SessionLocal", return_value=fake_session), \
         patch("app.run_logs.append_run_log", side_effect=capture_append):
        graph_mod._execute_discovery(
            run_id=99, site_url="https://x.test", force=False, name=None,
        )

    messages = [c["message"] for c in calls if c.get("run_id") == 99]
    assert any("原始输出" in m for m in messages)
    assert any("结构化整理" in m for m in messages)


def test_explorer_logs_include_full_structured_json_without_truncation():
    from app.discovery import graph as graph_mod

    calls = []
    long_payload = {
        "source_type": "json_api",
        "list_url": "https://www.openeuler.org/api-search/search/sort/blog",
        "fetch": {
            "method": "POST",
            "headers": {},
            "query": {},
            "json_body": {"category": "blog", "pageNum": 1, "pageSize": 10},
        },
        "path": "obj.records",
        "fields": {"url": "path", "title": "title", "published_at": "date"},
        "sample_items": [{"url": "/blog/x", "title": "示例标题"}],
        "success": True,
    }
    synth_json = json.dumps(long_payload, ensure_ascii=False)

    def capture_append(stage, message, *, source=None, level="info", **fields):
        calls.append({"stage": stage, "message": message, "source": source,
                      "level": level, **fields})
        return {}

    class _GraphWithLongExplorerSynth(_FakeGraph):
        def __init__(self):
            self._chunks = [
                {"explorer": {
                    "exploration": long_payload,
                    "explorer_agent_output": "原始输出",
                    "explorer_synthesis_output": synth_json,
                }},
            ]

        def get_state(self, config):
            return SimpleNamespace(values={"verdict": "failed", "method_id": None, "token_used": 0})

    fake_session = _FakeSession()
    with patch("langgraph.checkpoint.postgres.PostgresSaver.from_conn_string",
               lambda *a, **k: _FakeCheckpointer()), \
         patch.object(graph_mod, "_to_psycopg_conn_string", return_value="postgresql://x"), \
         patch.object(graph_mod, "build_graph", return_value=_GraphWithLongExplorerSynth()), \
         patch("app.db.SessionLocal", return_value=fake_session), \
         patch("app.run_logs.append_run_log", side_effect=capture_append):
        graph_mod._execute_discovery(
            run_id=100, site_url="https://x.test", force=False, name=None,
        )

    structured = next(
        c["message"] for c in calls
        if c.get("run_id") == 100 and "结构化整理" in c["message"]
    )
    assert "...[truncated]" not in structured
    assert "api-search/search/sort/blog" in structured
    assert "obj.records" in structured
    assert "示例标题" in structured


def test_explorer_logs_include_parse_failure_reason():
    from app.discovery import graph as graph_mod

    calls = []

    def capture_append(stage, message, *, source=None, level="info", **fields):
        calls.append({"stage": stage, "message": message, "source": source,
                      "level": level, **fields})
        return {}

    class _GraphWithExplorerParseFailure(_FakeGraph):
        def __init__(self):
            self._chunks = [
                {"explorer": {
                    "exploration": {"source_type": "unknown", "success": False},
                    "explorer_agent_output": "原始输出",
                    "explorer_synthesis_output": '{"broken": "json"}',
                    "explorer_parse_error": "Expecting ',' delimiter: line 1 column 42 (char 41)",
                }},
            ]

        def get_state(self, config):
            return SimpleNamespace(values={"verdict": "failed", "method_id": None, "token_used": 0})

    fake_session = _FakeSession()
    with patch("langgraph.checkpoint.postgres.PostgresSaver.from_conn_string",
               lambda *a, **k: _FakeCheckpointer()), \
         patch.object(graph_mod, "_to_psycopg_conn_string", return_value="postgresql://x"), \
         patch.object(graph_mod, "build_graph", return_value=_GraphWithExplorerParseFailure()), \
         patch("app.db.SessionLocal", return_value=fake_session), \
         patch("app.run_logs.append_run_log", side_effect=capture_append):
        graph_mod._execute_discovery(
            run_id=101, site_url="https://x.test", force=False, name=None,
        )

    messages = [c["message"] for c in calls if c.get("run_id") == 101]
    assert any("整理失败" in m for m in messages)
    assert any("Expecting ',' delimiter" in m for m in messages)


def test_dsl_writer_logs_include_full_recipe():
    from app.discovery import graph as graph_mod

    calls = []

    def capture_append(stage, message, *, source=None, level="info", **fields):
        calls.append({"stage": stage, "message": message, "source": source,
                      "level": level, **fields})
        return {}

    class _GraphWithDslRecipe(_FakeGraph):
        def __init__(self):
            self._chunks = [
                {"dsl_writer": {
                    "dsl_recipe": {
                        "recipe_type": "dsl",
                        "entry_url": "https://x.test",
                        "actions": [
                            {"op": "set", "var": "page", "value": 1},
                            {"op": "loop", "max_iters": 5, "body": []},
                            {"op": "dedup_by", "field": "url"},
                        ],
                        "notes": ["ok"],
                    }
                }},
            ]

        def get_state(self, config):
            return SimpleNamespace(values={"verdict": "failed", "method_id": None, "token_used": 0})

    fake_session = _FakeSession()
    with patch("langgraph.checkpoint.postgres.PostgresSaver.from_conn_string",
               lambda *a, **k: _FakeCheckpointer()), \
         patch.object(graph_mod, "_to_psycopg_conn_string", return_value="postgresql://x"), \
         patch.object(graph_mod, "build_graph", return_value=_GraphWithDslRecipe()), \
         patch("app.db.SessionLocal", return_value=fake_session), \
         patch("app.run_logs.append_run_log", side_effect=capture_append):
        graph_mod._execute_discovery(
            run_id=102, site_url="https://x.test", force=False, name=None,
        )

    dsl_log = next(c for c in calls if c.get("step") == "dsl_writer_recipe")
    assert dsl_log["stage"] == "写配方"
    assert "DSL 全量输出" in dsl_log["message"]
    assert '"recipe_type": "dsl"' in dsl_log["message"]
    assert '"op": "loop"' in dsl_log["message"]


def test_validator_logs_show_real_field_name_and_validated_samples():
    from app.discovery import graph as graph_mod

    calls = []

    def capture_append(stage, message, *, source=None, level="info", **fields):
        calls.append({"stage": stage, "message": message, "source": source,
                      "level": level, **fields})
        return {}

    class _GraphWithPathJoinValidator(_FakeGraph):
        def __init__(self):
            self._chunks = [
                {"validator": {
                    "url_rule": {
                        "mode": "path_join",
                        "base_url": "https://www.openeuler.org",
                        "path_field": "path",
                        "evidence": "validated 2/2",
                        "validation_samples": [
                            {
                                "sample_value": "/zh/blog/a",
                                "url": "https://www.openeuler.org/zh/blog/a",
                                "status": 200,
                                "is_article_page": True,
                            },
                            {
                                "sample_value": "/zh/blog/b",
                                "url": "https://www.openeuler.org/zh/blog/b",
                                "status": 200,
                                "is_article_page": True,
                            },
                        ],
                    }
                }},
            ]

        def get_state(self, config):
            return SimpleNamespace(values={"verdict": "failed", "method_id": None, "token_used": 0})

    fake_session = _FakeSession()
    with patch("langgraph.checkpoint.postgres.PostgresSaver.from_conn_string",
               lambda *a, **k: _FakeCheckpointer()), \
         patch.object(graph_mod, "_to_psycopg_conn_string", return_value="postgresql://x"), \
         patch.object(graph_mod, "build_graph", return_value=_GraphWithPathJoinValidator()), \
         patch("app.db.SessionLocal", return_value=fake_session), \
         patch("app.run_logs.append_run_log", side_effect=capture_append):
        graph_mod._execute_discovery(
            run_id=103, site_url="https://www.openeuler.org", force=False, name=None,
        )

    summary_log = next(c for c in calls if c.get("step") == "validator")
    assert "path_field=path" in summary_log["message"]
    assert "sample_values=/zh/blog/a, /zh/blog/b" in summary_log["message"]

    detail_log = next(c for c in calls if c.get("step") == "validator_rule")
    assert detail_log["stage"] == "验证URL"
    assert "URL 规律全量输出" in detail_log["message"]
    assert '"path_field": "path"' in detail_log["message"]
    assert '"sample_value": "/zh/blog/a"' in detail_log["message"]
