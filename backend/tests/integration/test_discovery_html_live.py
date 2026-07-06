"""
Live HTML discovery smoke test for a real static blog.

Usage:

    RUN_LIVE_DISCOVERY_HTML=1 ENABLE_SCHEDULER=0 ./backend/.venv/bin/python -m pytest \
      backend/tests/integration/test_discovery_html_live.py -m "live and slow" -v -s
"""

from __future__ import annotations

import json
import os

import pytest

from app.discovery.dsl import DslRecipe
from app.discovery.graph import (
    capture_network,
    dsl_writer,
    explorer,
    fetch_homepage,
    _derive_exploration_candidates_from_state,
    _pick_best_deterministic_candidate,
    validator,
    _run_recipe_for_audit,
)


LIVE_URL = "https://hhhhlkf.github.io/"


def _print_block(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    if isinstance(payload, str):
        print(payload)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_DISCOVERY_HTML") != "1",
    reason="set RUN_LIVE_DISCOVERY_HTML=1 to run live HTML discovery smoke test",
)
def test_live_html_blog_discovery_pipeline():
    state = {
        "site_url": LIVE_URL,
        "attempt": 0,
        "dsl_cycle_attempt": 0,
        "token_used": 0,
        "exploration": {},
        "network_captures": [],
        "homepage": {},
        "url_rule": None,
        "dsl_recipe": None,
        "audit_result": None,
    }

    homepage_update = fetch_homepage(state)
    state.update(homepage_update)
    _print_block("homepage", {
        "status": state["homepage"].get("status"),
        "title": state["homepage"].get("title"),
        "links_count": len(state["homepage"].get("links") or []),
        "links_sample": (state["homepage"].get("links") or [])[:10],
    })

    capture_update = capture_network(state)
    state.update(capture_update)
    _print_block("network", {
        "captures": len(state["network_captures"] or []),
        "sample": [
            {
                "api_url": item.get("api_url"),
                "method": item.get("method"),
                "status": item.get("status"),
            }
            for item in (state["network_captures"] or [])[:5]
        ],
    })

    try:
        explorer_update = explorer(state)
        state.update(explorer_update)
    except Exception as exc:
        fallback = _pick_best_deterministic_candidate(_derive_exploration_candidates_from_state(state))
        assert fallback is not None, f"explorer failed and no deterministic fallback available: {exc}"
        state.update({
            "exploration": fallback,
            "explorer_parse_error": str(exc),
            "explorer_synthesis_output": "",
            "explorer_agent_output": "",
        })
    _print_block("explorer.summary", {
        "source_type": state["exploration"].get("source_type"),
        "list_url": state["exploration"].get("list_url"),
        "success": state["exploration"].get("success"),
        "html_selectors": state["exploration"].get("html_selectors"),
        "pagination": state["exploration"].get("pagination"),
        "sample_items": (state["exploration"].get("sample_items") or [])[:3],
        "parse_error": state.get("explorer_parse_error"),
    })
    _print_block("explorer.synthesis_preview", state.get("explorer_synthesis_output") or "")

    validator_update = validator(state)
    state.update(validator_update)
    _print_block("validator.url_rule", state["url_rule"])

    dsl_update = dsl_writer(state)
    state.update(dsl_update)
    _print_block("dsl_writer.recipe", state["dsl_recipe"])

    recipe = DslRecipe(**state["dsl_recipe"])
    run_out = _run_recipe_for_audit(recipe)
    _print_block("recipe.run", {
        "stats": run_out.get("stats"),
        "error": run_out.get("error"),
        "items_sample": (run_out.get("items") or [])[:5],
    })

    assert state["exploration"]["source_type"] == "html"
    assert state["exploration"]["success"] is True
    assert state["url_rule"] is not None
    assert state["dsl_recipe"] is not None
    assert (run_out.get("stats") or {}).get("discovered_count", 0) >= 1
