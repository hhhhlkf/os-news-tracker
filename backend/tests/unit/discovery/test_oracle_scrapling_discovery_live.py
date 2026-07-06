"""
Live end-to-end discovery smoke test for Oracle Linux RSS via Scrapling.

Usage from the repository root:

    RUN_LIVE_DISCOVERY_ORACLE=1 ENABLE_SCHEDULER=0 PYTHONPATH=backend python3 -m pytest \
      backend/tests/unit/discovery/test_oracle_scrapling_discovery_live.py -m "live and slow" -v -s

This test intentionally performs real network requests to Oracle. It is skipped
unless RUN_LIVE_DISCOVERY_ORACLE=1 is set.
"""

from __future__ import annotations

import json
import os

import pytest
from langchain_core.messages import AIMessage

from app.discovery.graph import (
    auditor,
    capture_network,
    dsl_writer,
    explorer,
    fetch_homepage,
    validator,
)


ORACLE_LINUX_FEED_URL = "https://blogs.oracle.com/linux/feed"


def _print_block(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    if isinstance(payload, str):
        print(payload)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


class _ScriptedExplorerLlm:
    """Tiny scripted tool-calling model for the Explorer worker.

    The point of this live test is to exercise the real discovery nodes and
    real fetch tools without depending on an external LLM gateway.
    """

    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="先用默认 httpx 探测 Oracle RSS。",
                tool_calls=[{
                    "id": "oracle_httpx_probe",
                    "name": "fetch_page",
                    "args": {
                        "url": ORACLE_LINUX_FEED_URL,
                        "render_js": False,
                    },
                }],
            )
        if self.calls == 2:
            return AIMessage(
                content="默认调取被拦后，改用 Scrapling chrome impersonation。",
                tool_calls=[{
                    "id": "oracle_scrapling_probe",
                    "name": "fetch_page",
                    "args": {
                        "url": ORACLE_LINUX_FEED_URL,
                        "render_js": False,
                        "transport": "scrapling",
                        "impersonate": "chrome",
                        "stealthy_headers": True,
                    },
                }],
            )
        return AIMessage(
            content=(
                "Oracle Linux RSS 可用。默认 httpx 返回 403；Scrapling transport=scrapling, "
                "impersonate=chrome 成功拿到 feed.entries，应将该 transport 写入 DSL 配方。"
            )
        )


class _ExplorerSynthesisClient:
    """Fake LlmClient used by explorer's synthesis phase."""

    def complete(self, prompt: str, **kwargs) -> str:
        return json.dumps({
            "source_type": "rss",
            "list_url": ORACLE_LINUX_FEED_URL,
            "fetch": {
                "method": "GET",
                "transport": "scrapling",
                "impersonate": "chrome",
                "stealthy_headers": True,
                "headers": {},
                "query": {},
                "json_body": None,
            },
            "format_locator": {"kind": "feed_entries", "value": "feed.entries"},
            "fields": {
                "id": None,
                "title": "title",
                "url": "link",
                "published_at": "published",
                "summary": "summary",
                "content": None,
            },
            "html_selectors": {
                "item_selector": None,
                "link_selector": None,
                "title_selector": None,
                "date_selector": None,
            },
            "sample_items": [],
            "url_candidates": [{
                "mode": "existing_url",
                "url_field": "link",
                "id_field": None,
                "template": None,
                "verification": "feed_entry_link",
            }],
            "pagination": {
                "type": "none",
                "page_param": None,
                "size_param": None,
                "offset_param": None,
                "limit_param": None,
                "cursor_param": None,
                "next_path": None,
                "has_more_path": None,
                "start": 1,
                "size": None,
                "notes": "RSS feed has no pagination",
            },
            "evidence": [{
                "tool": "fetch_page",
                "summary": "Oracle feed fetched via Scrapling chrome impersonation.",
            }],
            "notes": ["Scrapling transport must be preserved in the DSL recipe."],
            "success": True,
        }, ensure_ascii=False)


class _StaticStructuredLlm:
    """Structured-output fake for Validator and Auditor workers."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        return self.payload


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_DISCOVERY_ORACLE") != "1",
    reason="set RUN_LIVE_DISCOVERY_ORACLE=1 to run live Oracle discovery smoke test",
)
def test_live_oracle_rss_discovery_runs_every_agent_step(monkeypatch):
    """Run every discovery step for Oracle RSS and print the handoffs.

    The sequence is:
    fetch_homepage -> capture_network -> explorer -> validator -> dsl_writer -> auditor.
    Discovery tools are executed through LangGraph ToolNode inside graph.py.
    """

    monkeypatch.setattr("app.llm.client.LlmClient", _ExplorerSynthesisClient)

    state = {
        "site_url": ORACLE_LINUX_FEED_URL,
        "attempt": 0,
        "dsl_cycle_attempt": 0,
        "token_used": 0,
        "homepage": {},
        "network_captures": [],
        "exploration": {},
        "url_rule": None,
        "dsl_recipe": None,
        "audit_result": None,
        "verdict": None,
        "method_id": None,
        "error": None,
    }

    homepage_update = fetch_homepage(state)
    state.update(homepage_update)
    _print_block("01.fetch_homepage", {
        "status": state["homepage"].get("status"),
        "content_type": state["homepage"].get("content_type"),
        "transport": state["homepage"].get("transport"),
        "title": state["homepage"].get("title"),
        "has_feed": bool(state["homepage"].get("feed")),
    })

    try:
        network_update = capture_network(state)
    except Exception as exc:  # browser availability should not hide agent flow output
        network_update = {"network_captures": [{"error": str(exc)}]}
    state.update(network_update)
    _print_block("02.capture_network", {
        "captures": len(state.get("network_captures") or []),
        "sample": (state.get("network_captures") or [])[:3],
    })

    explorer_update = explorer(state, llm=_ScriptedExplorerLlm())
    state.update(explorer_update)
    _print_block("03.explorer.agent_trace", state.get("explorer_trace_logs") or [])
    _print_block("03.explorer.exploration", state["exploration"])
    trace_logs = state.get("explorer_trace_logs") or []
    tool_events = [event for event in trace_logs if event.get("kind") == "tool"]

    validator_llm = _StaticStructuredLlm({
        "mode": "existing_url",
        "template": None,
        "base_url": None,
        "path_field": None,
        "id_field": None,
        "url_field": "link",
        "sample_items": state["exploration"].get("sample_items") or [],
        "validation_samples": [],
        "confidence": "high",
        "reason": "RSS feed entries expose link as the article URL.",
    })
    validator_update = validator(state, llm=validator_llm)
    state.update(validator_update)
    _print_block("04.validator.url_rule", state["url_rule"])

    dsl_update = dsl_writer(state)
    state.update(dsl_update)
    _print_block("05.dsl_writer.recipe", state["dsl_recipe"])

    auditor_llm = _StaticStructuredLlm({
        "passed": True,
        "is_real_content": True,
        "has_pagination": True,
        "not_blocked": True,
        "value_assessment": "Scrapling feed recipe fetched real Oracle Linux RSS entries.",
        "issues": [],
        "suggested_fix": None,
    })
    auditor_update = auditor(state, llm=auditor_llm)
    state.update(auditor_update)
    _print_block("06.auditor.input", state.get("audit_input"))
    _print_block("06.auditor.result", state["audit_result"])

    recipe = state["dsl_recipe"]
    fetch_action = recipe["actions"][0]
    fetch_tool_events = [event for event in tool_events if event.get("name") == "fetch_page"]

    assert fetch_tool_events
    assert any("scrapling" in str(event.get("content", "")).lower() for event in fetch_tool_events)
    assert state["exploration"]["source_type"] == "rss"
    assert state["exploration"]["fetch"]["transport"] == "scrapling"
    assert state["url_rule"]["mode"] == "existing_url"
    assert fetch_action["op"] == "fetch"
    assert fetch_action["mode"] == "feed"
    assert fetch_action["transport"] == "scrapling"
    assert fetch_action["impersonate"] == "chrome"
    assert state["audit_result"]["passed"] is True
    assert state["audit_result"]["test"]["stats"]["discovered_count"] >= 3
