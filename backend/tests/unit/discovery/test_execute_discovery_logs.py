"""Regression: _execute_discovery per-step append_run_log must carry run_id.

The frontend log panel (Task 9) polls /news-run/logs and filters by run_id.
Per-step progress logs emitted inside the g.stream() loop must include run_id,
otherwise the panel drops them and only shows the 3 "任务"-stage lines.
"""
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
