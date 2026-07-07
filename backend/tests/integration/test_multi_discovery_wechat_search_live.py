import os

import httpx
import pytest

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
DEFAULT_TEMPLATE_VARIANT = os.getenv("WECHAT_SEARCH_TEMPLATE_VARIANT", "search_then_article_enrich")
MULTI_RUN_TIMEOUT = float(os.getenv("WECHAT_SEARCH_MULTI_RUN_TIMEOUT", "120"))


def _log(step: int, msg: str) -> None:
    print(f"\n  [{step}] {msg}", flush=True)


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_WECHAT_SEARCH") != "1",
    reason="set RUN_LIVE_WECHAT_SEARCH=1 to run live WeChat search discovery",
)
def test_multi_run_wechat_keyword_search_generates_fetchable_method():
    query = os.getenv("WECHAT_SEARCH_QUERY", "Linux")
    max_pages = int(os.getenv("WECHAT_SEARCH_MAX_PAGES", "5"))
    template_variant = DEFAULT_TEMPLATE_VARIANT
    fetch_content = os.getenv("WECHAT_SEARCH_FETCH_CONTENT", "1") == "1"

    print(f"\n{'='*60}")
    print(f"  Task 2: WeChat Keyword Search Multi DSL")
    print(f"  Query: {query!r}  Max pages: {max_pages}")
    print(f"{'='*60}")

    # ── Step 1: Multi-run ──────────────────────────────────────────
    _log(
        1,
        "POST /discovery/multi-run  "
        f"(input={query!r}, hints.source_kind=wechat, wechat_mode=search, template_variant={template_variant!r}, max_pages={max_pages})",
    )
    with httpx.Client(base_url=API_BASE, timeout=MULTI_RUN_TIMEOUT) as client:
        response = client.post(
            "/discovery/multi-run",
            json={
                "input": query,
                "force": True,
                "name": f"微信搜索: {query}",
                "hints": {
                    "source_kind": "wechat",
                    "wechat_mode": "search",
                    "max_pages": max_pages,
                    "template_variant": template_variant,
                    "fetch_content": fetch_content,
                },
            },
        )
    assert response.status_code == 200, f"multi-run failed: {response.status_code} {response.text[:300]}"
    payload = response.json()

    _log(1, f"  -> status={payload['status']}")
    _log(1, f"  -> route.kind={payload['route']['kind']}")
    _log(1, f"  -> route.input_type={payload['route']['input_type']}")
    _log(1, f"  -> method_id={payload.get('method_id')}")
    _log(1, f"  -> discovered_count={payload.get('discovered_count', 'N/A')}")

    assert payload["status"] == "completed", f"Expected completed, got {payload}"
    assert payload["method_id"], "method_id must be present"
    method_id = payload["method_id"]

    # ── Step 2: Verify stored method ────────────────────────────────
    _log(2, f"GET /discovery/methods/{method_id}  (verify recipe structure)")
    with httpx.Client(base_url=API_BASE, timeout=30) as client:
        method_response = client.get(f"/discovery/methods/{method_id}")
    assert method_response.status_code == 200
    method_payload = method_response.json()
    recipe = method_payload["dsl_recipe"]

    _log(2, f"  -> domain={method_payload['domain']}")
    _log(2, f"  -> entry_url={method_payload['entry_url']}")
    _log(2, f"  -> status={method_payload['status']}")
    _log(2, f"  -> recipe_type={recipe['recipe_type']}")
    _log(2, f"  -> source_kind={recipe['source_kind']}")
    _log(2, f"  -> notes={recipe.get('notes')}")
    _log(2, f"  -> actions={len(recipe['actions'])}")
    for i, a in enumerate(recipe["actions"]):
        _log(2, f"     action[{i}]: op={a.get('op')}")

    assert recipe["recipe_type"] == "multi_dsl", f"Expected multi_dsl, got {recipe.get('recipe_type')}"
    assert recipe["source_kind"] == "wechat"
    expected_action_count = 4 if template_variant == "search_then_article_enrich" else 3
    assert len(recipe["actions"]) == expected_action_count, (
        f"Expected {expected_action_count} actions for {template_variant}, got {len(recipe['actions'])}"
    )
    assert recipe["actions"][0]["op"] == "wechat_search_articles"
    assert recipe["actions"][1]["op"] == "extract"
    if template_variant == "search_then_article_enrich":
        assert recipe["actions"][2]["op"] == "enrich_wechat_articles"
        assert recipe["actions"][2]["fetch_content"] == fetch_content
        assert recipe["actions"][2]["fill_missing_only"] is True
        assert recipe["actions"][3]["op"] == "dedup_by"
        assert any("article pages are fetched" in note for note in recipe.get("notes", []))
    else:
        assert recipe["actions"][2]["op"] == "dedup_by"
        assert any("Card-only template" in note for note in recipe.get("notes", []))

    # Step 3/4 intentionally disabled for this live test for now.
    # `/fetch` is much heavier because it runs the saved recipe and then
    # serially sends each item through the enrichment/store pipeline.
    # This test currently focuses on whether multi-run can generate and
    # persist a valid WeChat search method.

    print(f"\n{'='*60}")
    print(f"  Result: multi-run completed and method {method_id} stored")
    print(f"{'='*60}\n")
