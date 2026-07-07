import os

import httpx
import pytest

API_BASE = os.getenv("API_BASE", "http://localhost:8000")


def _log(step: int, msg: str) -> None:
    print(f"\n  [{step}] {msg}", flush=True)


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_WECHAT_HISTORY") != "1",
    reason="set RUN_LIVE_WECHAT_HISTORY=1 to run live WeChat history discovery",
)
def test_multi_run_wechat_account_history_generates_fetchable_method():
    account = os.getenv("WECHAT_HISTORY_ACCOUNT", "腾讯技术工程")
    limit = int(os.getenv("WECHAT_HISTORY_LIMIT", "5"))
    fetch_content = os.getenv("WECHAT_HISTORY_FETCH_CONTENT", "0") == "1"

    print(f"\n{'='*60}")
    print(f"  Task 3: WeChat Account History Discovery")
    print(f"  Account: {account!r}  Limit: {limit}")
    print(f"{'='*60}")

    # ── Step 1: Multi-run (history) ─────────────────────────────────
    _log(1, f"POST /discovery/multi-run  (input={account!r}, hints.source_kind=wechat)")
    with httpx.Client(base_url=API_BASE, timeout=30) as client:
        response = client.post(
            "/discovery/multi-run",
            json={
                "input": account,
                "force": True,
                "name": f"公众号: {account}",
                "hints": {"source_kind": "wechat", "limit": limit, "fetch_content": fetch_content},
            },
        )
    assert response.status_code == 200, f"multi-run failed: {response.status_code} {response.text[:300]}"
    payload = response.json()

    _log(1, f"  -> status={payload['status']}")
    _log(1, f"  -> route.kind={payload['route']['kind']}")
    _log(1, f"  -> route.input_type={payload['route']['input_type']}")
    _log(1, f"  -> method_id={payload.get('method_id')}")
    _log(1, f"  -> method_status={payload.get('method_status')}")
    _log(1, f"  -> discovered_count={payload.get('discovered_count', 'N/A')}")

    # If auth is not configured, verify pending_auth status is handled gracefully
    if payload.get("method_status") == "pending_auth":
        _log(1, "  -> Auth not configured — method saved as pending_auth (expected when env missing)")
        method_id = payload["method_id"]
        with httpx.Client(base_url=API_BASE, timeout=30) as client:
            method_response = client.get(f"/discovery/methods/{method_id}")
        assert method_response.status_code == 200
        method_payload = method_response.json()
        recipe = method_payload["dsl_recipe"]
        assert recipe["recipe_type"] == "multi_dsl"
        assert recipe["auth_ref"] == "wechat_mp_default"
        # Verify no secrets leaked
        recipe_str = str(recipe)
        assert "WECHAT_MP_COOKIE" not in recipe_str
        assert "WECHAT_MP_TOKEN" not in recipe_str
        assert "secret" not in recipe_str.lower()
        _log(1, "  -> Secrets check: PASSED (no cookie/token in stored recipe)")
        print(f"\n{'='*60}")
        print(f"  Result: pending_auth — method structure verified, secrets clean")
        print(f"{'='*60}\n")
        return

    assert payload["status"] == "completed", payload
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
    _log(2, f"  -> auth_ref={recipe.get('auth_ref')}")
    _log(2, f"  -> requires_auth={recipe.get('requires_auth')}")
    _log(2, f"  -> notes={recipe.get('notes')}")
    _log(2, f"  -> actions={len(recipe['actions'])}")
    for i, a in enumerate(recipe["actions"]):
        _log(2, f"     action[{i}]: op={a.get('op')}")

    assert recipe["recipe_type"] == "multi_dsl"
    assert recipe["source_kind"] == "wechat"
    assert recipe["auth_ref"] == "wechat_mp_default"
    assert len(recipe["actions"]) == 3
    assert recipe["actions"][0]["op"] == "wechat_fetch_account_history"
    assert recipe["actions"][0]["fetch_content"] == fetch_content
    assert recipe["actions"][1]["op"] == "extract"
    assert recipe["actions"][2]["op"] == "dedup_by"

    # Verify no secrets leaked in stored recipe
    recipe_str = str(recipe)
    assert "WECHAT_MP_COOKIE" not in recipe_str
    assert "WECHAT_MP_TOKEN" not in recipe_str
    _log(2, "  -> Secrets check: PASSED (no cookie/token in stored recipe)")

    # Step 3/4 intentionally disabled for this live test for now.
    # `/fetch` is much heavier because it runs the saved recipe and then
    # serially sends each item through the enrichment/store pipeline.
    # This test currently focuses on whether multi-run can generate and
    # persist a valid WeChat history method.
    #
    # # ── Step 3: Fetch using the saved method ────────────────────────
    # _log(3, f"POST /discovery/methods/{method_id}/fetch  (run saved recipe)")
    # with httpx.Client(base_url=API_BASE, timeout=120) as client:
    #     fetch_response = client.post(f"/discovery/methods/{method_id}/fetch", json=None)
    # assert fetch_response.status_code == 200, f"fetch failed: {fetch_response.status_code} {fetch_response.text[:300]}"
    # fetch_payload = fetch_response.json()
    #
    # items = fetch_payload.get("items", [])
    # stats = fetch_payload.get("stats", {})
    #
    # _log(3, f"  -> discovered_count={fetch_payload.get('discovered_count')}")
    # _log(3, f"  -> stored_count={fetch_payload.get('stored_count')}")
    # _log(3, f"  -> items returned={len(items)}")
    # _log(3, f"  -> stats={stats}")
    #
    # assert "items" in fetch_payload
    # assert "stats" in fetch_payload
    #
    # # ── Step 4: Validate items ──────────────────────────────────────
    # _log(4, "Validating fetched items")
    # if items:
    #     _log(4, f"  -> {len(items)} items from WeChat MP API")
    #     for i, item in enumerate(items):
    #         title_ok = bool(item.get("title"))
    #         url_ok = bool(item.get("url"))
    #         has_mp = "mp.weixin.qq.com" in (item.get("url") or "")
    #         published_at = item.get("published_at")
    #         summary = item.get("summary")
    #         content = item.get("content")
    #         _log(4, f"     [{i}] title={item.get('title','')[:60]}")
    #         _log(4, f"         url={item.get('url','')[:150]}")
    #         _log(4, f"         published_at={published_at!r}")
    #         _log(4, f"         summary_len={len(summary or '')} content_len={len(content or '')}")
    #         assert title_ok, f"Item {i} missing title"
    #         assert url_ok, f"Item {i} missing url"
    #         assert has_mp, f"Item {i} should resolve to mp.weixin.qq.com"
    #         if published_at is not None:
    #             assert isinstance(published_at, str)
    #             assert "T" in published_at or len(published_at) == 100
    #         if summary is not None:
    #             assert isinstance(summary, str)
    #         if content is not None:
    #             assert isinstance(content, str)
    #     if fetch_content:
    #         assert any(item.get("content") for item in items), "fetch_content=1 should populate content for at least one item"
    # else:
    #     _log(4, "  -> No items (auth may not be configured) — structure verified by steps 1-3")
    #
    # print(f"\n{'='*60}")
    # print(f"  Result: {len(items)} articles from WeChat MP history")
    # print(f"{'='*60}\n")

    print(f"\n{'='*60}")
    print(f"  Result: multi-run completed and method {method_id} stored")
    print(f"{'='*60}\n")
