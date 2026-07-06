"""
Live end-to-end discovery test for Oracle Linux RSS via Scrapling.

Usage inside the backend container:

    cd /app && RUN_LIVE_DISCOVERY_ORACLE=1 ENABLE_SCHEDULER=0 PYTHONPATH=. python -m pytest \
      tests/unit/discovery/test_oracle_scrapling_discovery_live.py -m "live and slow" -v -s

This test intentionally performs real network requests and real LLM calls. It is
skipped unless RUN_LIVE_DISCOVERY_ORACLE=1 is set.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from app.discovery.graph import (
    _make_llm,
    auditor,
    capture_network,
    dsl_writer,
    explorer,
    fetch_homepage,
    validator,
)
from app.llm.client import LlmClient as RealLlmClient


ORACLE_LINUX_FEED_URL = os.getenv(
    "DISCOVERY_TEST_URL",
    "https://blogs.oracle.com/linux/",
)


def _print_block(title: str, payload: Any) -> None:
    print(f"\n=== {title} ===")
    if isinstance(payload, str):
        print(payload)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _message_to_dict(message: Any) -> dict:
    return {
        "type": message.__class__.__name__,
        "content": getattr(message, "content", None),
        "tool_calls": getattr(message, "tool_calls", None),
        "name": getattr(message, "name", None),
        "tool_call_id": getattr(message, "tool_call_id", None),
    }


def _messages_to_dump(messages: Any) -> list[dict] | str:
    if isinstance(messages, list):
        return [_message_to_dict(message) for message in messages]
    return str(messages)


class _TracingChatModel:
    """Tracing wrapper around the real LangChain chat model used by Explorer."""

    def __init__(self, inner: Any, *, label: str = "explorer.chat") -> None:
        self.inner = inner
        self.label = label
        self.invoke_count = 0

    def bind_tools(self, tools):
        bound = self.inner.bind_tools(tools) if hasattr(self.inner, "bind_tools") else self.inner
        return _TracingChatModel(bound, label=f"{self.label}.bound_tools")

    def with_structured_output(self, schema):
        structured = (
            self.inner.with_structured_output(schema)
            if hasattr(self.inner, "with_structured_output")
            else self.inner
        )
        return _TracingChatModel(structured, label=f"{self.label}.structured")

    def invoke(self, messages):
        self.invoke_count += 1
        _print_block(
            f"LLM INPUT {self.label} #{self.invoke_count}",
            _messages_to_dump(messages),
        )
        output = self.inner.invoke(messages)
        _print_block(
            f"LLM OUTPUT {self.label} #{self.invoke_count}",
            _message_to_dict(output) if hasattr(output, "content") else output,
        )
        return output


class _TracingLlmClient:
    """Tracing wrapper around app.llm.client.LlmClient.complete."""

    call_count = 0

    def __init__(self, *args, **kwargs) -> None:
        self.inner = RealLlmClient(*args, **kwargs)

    def complete(self, prompt: str, **kwargs) -> str:
        type(self).call_count += 1
        idx = type(self).call_count
        _print_block(f"LLM INPUT LlmClient.complete #{idx}", {
            "kwargs": kwargs,
            "prompt": prompt,
        })
        output = self.inner.complete(prompt, **kwargs)
        _print_block(f"LLM OUTPUT LlmClient.complete #{idx}", output)
        return output


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_DISCOVERY_ORACLE") != "1",
    reason="set RUN_LIVE_DISCOVERY_ORACLE=1 to run live Oracle discovery test",
)
def test_live_oracle_rss_discovery_runs_real_llm_every_agent_step(monkeypatch):
    """Run every discovery step with real LLM decisions and print all LLM I/O.

    Sequence:
    fetch_homepage -> capture_network -> explorer -> validator -> dsl_writer -> auditor.
    """

    monkeypatch.setattr("app.llm.client.LlmClient", _TracingLlmClient)

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
    except Exception as exc:
        network_update = {"network_captures": [{"error": str(exc)}]}
    state.update(network_update)
    _print_block("02.capture_network", {
        "captures": len(state.get("network_captures") or []),
        "sample": (state.get("network_captures") or [])[:3],
    })

    explorer_llm = _TracingChatModel(_make_llm(), label="explorer")
    explorer_update = explorer(state, llm=explorer_llm)
    state.update(explorer_update)
    _print_block("03.explorer.agent_trace", state.get("explorer_trace_logs") or [])
    _print_block("03.explorer.agent_output", state.get("explorer_agent_output") or "")
    _print_block("03.explorer.synthesis_output", state.get("explorer_synthesis_output") or "")
    _print_block("03.explorer.exploration", state["exploration"])

    validator_update = validator(state)
    state.update(validator_update)
    _print_block("04.validator.llm_output", state.get("validator_llm_output") or "")
    _print_block("04.validator.url_rule", state["url_rule"])

    dsl_update = dsl_writer(state)
    state.update(dsl_update)
    _print_block("05.dsl_writer.llm_output", state.get("dsl_writer_llm_output") or "(deterministic shortcut; no LLM call)")
    _print_block("05.dsl_writer.recipe", state["dsl_recipe"])

    auditor_update = auditor(state)
    state.update(auditor_update)
    _print_block("06.auditor.input", state.get("audit_input"))
    _print_block("06.auditor.llm_output", state.get("auditor_llm_output") or "")
    _print_block("06.auditor.result", state["audit_result"])

    recipe = state["dsl_recipe"]
    fetch_action = recipe["actions"][0]
    assert state["exploration"]["success"] is True
    assert state["exploration"]["source_type"] in {"rss", "atom"}
    assert state["exploration"]["fetch"]["transport"] == "scrapling"
    assert state["url_rule"]["mode"] == "existing_url"
    assert fetch_action["op"] == "fetch"
    assert fetch_action["mode"] == "feed"
    assert fetch_action["transport"] == "scrapling"
    assert fetch_action["impersonate"] == "chrome"
    assert state["audit_result"]["passed"] is True
    assert state["audit_result"]["test"]["stats"]["discovered_count"] >= 3
