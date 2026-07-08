import os
import time

import httpx
import pytest

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
RUN_FLAG = "RUN_LIVE_MULTI_DISCOVERY_ROUTES"


def _log(step: int | str, msg: str) -> None:
    print(f"\n  [{step}] {msg}", flush=True)


def _post_multi_run(payload: dict, *, timeout: float = 30.0) -> dict:
    with httpx.Client(base_url=API_BASE, timeout=timeout) as client:
        response = client.post("/discovery/multi-run", json=payload)
    assert response.status_code == 200, f"multi-run failed: {response.status_code} {response.text[:300]}"
    return response.json()


def _get_method(method_id: int, *, timeout: float = 30.0) -> dict:
    with httpx.Client(base_url=API_BASE, timeout=timeout) as client:
        response = client.get(f"/discovery/methods/{method_id}")
    assert response.status_code == 200, f"get method failed: {response.status_code} {response.text[:300]}"
    return response.json()


def _wait_website_run(run_id: int, *, timeout_seconds: int = 240) -> dict:
    deadline = time.time() + timeout_seconds
    run_payload = None
    with httpx.Client(base_url=API_BASE, timeout=30) as client:
        while time.time() < deadline:
            run_response = client.get(f"/discovery/runs/{run_id}")
            assert run_response.status_code == 200, f"get run failed: {run_response.status_code} {run_response.text[:300]}"
            run_payload = run_response.json()
            if run_payload["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(2)
    assert run_payload is not None, "website run payload must not be empty"
    return run_payload


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv(RUN_FLAG) != "1",
    reason=f"set {RUN_FLAG}=1 to run live multi discovery route coverage",
)
def test_multi_route_website_live():
    url = os.getenv("MULTI_DISCOVERY_WEBSITE_URL", "https://blogs.oracle.com/linux/feed")

    print(f"\n{'='*60}")
    print("  Multi Route 1: Website")
    print(f"  URL: {url}")
    print(f"{'='*60}")

    _log(1, f"POST /discovery/multi-run  (input={url!r})")
    payload = _post_multi_run(
        {"input": url, "force": True},
        timeout=30,
    )
    _log(1, f"  -> status={payload['status']}")
    _log(1, f"  -> route.kind={payload['route']['kind']}")
    _log(1, f"  -> run_id={payload.get('run_id')}")

    assert payload["status"] == "started"
    assert payload["route"]["kind"] == "website"
    run_id = payload["run_id"]

    _log(2, f"GET /discovery/runs/{run_id}  (poll website discovery run)")
    run_payload = _wait_website_run(run_id)
    _log(2, f"  -> final_status={run_payload['status']}")
    _log(2, f"  -> resulting_method_id={run_payload.get('resulting_method_id')}")

    assert run_payload["status"] == "completed", run_payload
    assert run_payload["resulting_method_id"], "website route should produce a discovery method"
    method_id = run_payload["resulting_method_id"]

    _log(3, f"GET /discovery/methods/{method_id}  (verify stored website recipe)")
    method_payload = _get_method(method_id)
    recipe = method_payload["dsl_recipe"]
    _log(3, f"  -> source_name={method_payload['source_name']}")
    _log(3, f"  -> recipe_type={recipe['recipe_type']}")
    _log(3, f"  -> actions={len(recipe['actions'])}")
    assert method_payload["source_name"].startswith("网站：")
    assert recipe["recipe_type"] == "dsl"
    assert len(recipe["actions"]) >= 2

    # ── Step 4: Fetch using the saved method ────────────────────────
    # _log(4, f"POST /discovery/methods/{method_id}/fetch  (run saved website recipe)")
    # with httpx.Client(base_url=API_BASE, timeout=180) as client:
    #     fetch_response = client.post(f"/discovery/methods/{method_id}/fetch", json=None)
    # assert fetch_response.status_code == 200, f"fetch failed: {fetch_response.status_code} {fetch_response.text[:300]}"
    # fetch_payload = fetch_response.json()
    # _log(4, f"  -> discovered_count={fetch_payload.get('discovered_count')}")
    # _log(4, f"  -> stored_count={fetch_payload.get('stored_count')}")
    # assert "items" in fetch_payload
    # assert "stats" in fetch_payload


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv(RUN_FLAG) != "1",
    reason=f"set {RUN_FLAG}=1 to run live multi discovery route coverage",
)
def test_multi_route_wechat_search_live():
    query = os.getenv("WECHAT_SEARCH_QUERY", "Linux")
    max_pages = int(os.getenv("WECHAT_SEARCH_MAX_PAGES", "5"))
    template_variant = os.getenv("WECHAT_SEARCH_TEMPLATE_VARIANT", "search_then_article_enrich")
    fetch_content = os.getenv("WECHAT_SEARCH_FETCH_CONTENT", "1") == "1"
    timeout = float(os.getenv("WECHAT_SEARCH_MULTI_RUN_TIMEOUT", "120"))

    print(f"\n{'='*60}")
    print("  Multi Route 2: WeChat Search")
    print(f"  Query: {query!r}  Max pages: {max_pages}")
    print(f"{'='*60}")

    _log(
        1,
        "POST /discovery/multi-run  "
        f"(input={query!r}, hints.source_kind=wechat_search, template_variant={template_variant!r}, max_pages={max_pages})",
    )
    payload = _post_multi_run(
        {
            "input": query,
            "force": True,
            "name": f"微信搜索: {query}",
            "hints": {
                "source_kind": "wechat_search",
                "max_pages": max_pages,
                "template_variant": template_variant,
                "fetch_content": fetch_content,
            },
        },
        timeout=timeout,
    )
    _log(1, f"  -> status={payload['status']}")
    _log(1, f"  -> route.kind={payload['route']['kind']}")
    _log(1, f"  -> route.input_type={payload['route']['input_type']}")
    _log(1, f"  -> method_id={payload.get('method_id')}")
    _log(1, f"  -> discovered_count={payload.get('discovered_count', 'N/A')}")

    assert payload["status"] == "completed"
    assert payload["route"]["kind"] == "wechat"
    assert payload["route"]["input_type"] == "wechat_search"
    assert payload["method_id"]
    method_id = payload["method_id"]

    _log(2, f"GET /discovery/methods/{method_id}  (verify stored wechat_search recipe)")
    method_payload = _get_method(method_id)
    recipe = method_payload["dsl_recipe"]
    _log(2, f"  -> recipe_type={recipe['recipe_type']}")
    _log(2, f"  -> actions={len(recipe['actions'])}")
    for i, action in enumerate(recipe["actions"]):
        _log(2, f"     action[{i}]: op={action.get('op')}")

    expected_action_count = 4 if template_variant == "search_then_article_enrich" else 3
    assert recipe["recipe_type"] == "multi_dsl"
    assert recipe["source_kind"] == "wechat"
    assert len(recipe["actions"]) == expected_action_count
    assert recipe["actions"][0]["op"] == "wechat_search_articles"
    assert recipe["actions"][1]["op"] == "extract"

    # ── Step 3: Fetch using the saved method ────────────────────────
    # _log(3, f"POST /discovery/methods/{method_id}/fetch  (run saved wechat_search recipe)")
    # with httpx.Client(base_url=API_BASE, timeout=300) as client:
    #     fetch_response = client.post(f"/discovery/methods/{method_id}/fetch", json=None)
    # assert fetch_response.status_code == 200, f"fetch failed: {fetch_response.status_code} {fetch_response.text[:300]}"
    # fetch_payload = fetch_response.json()
    # _log(3, f"  -> discovered_count={fetch_payload.get('discovered_count')}")
    # _log(3, f"  -> stored_count={fetch_payload.get('stored_count')}")
    # assert "items" in fetch_payload
    # assert "stats" in fetch_payload


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv(RUN_FLAG) != "1",
    reason=f"set {RUN_FLAG}=1 to run live multi discovery route coverage",
)
def test_multi_route_wechat_history_live():
    account = os.getenv("WECHAT_HISTORY_ACCOUNT", "腾讯技术工程")
    limit = int(os.getenv("WECHAT_HISTORY_LIMIT", "100"))
    fetch_content = os.getenv("WECHAT_HISTORY_FETCH_CONTENT", "0") == "1"

    print(f"\n{'='*60}")
    print("  Multi Route 3: WeChat History")
    print(f"  Account: {account!r}  Limit: {limit}")
    print(f"{'='*60}")

    _log(1, f"POST /discovery/multi-run  (input={account!r}, hints.source_kind=wechat_history)")
    payload = _post_multi_run(
        {
            "input": account,
            "force": True,
            "name": f"公众号: {account}",
            "hints": {
                "source_kind": "wechat_history",
                "limit": limit,
                "fetch_content": fetch_content,
            },
        },
        timeout=30,
    )
    _log(1, f"  -> status={payload['status']}")
    _log(1, f"  -> route.kind={payload['route']['kind']}")
    _log(1, f"  -> route.input_type={payload['route']['input_type']}")
    _log(1, f"  -> method_id={payload.get('method_id')}")
    _log(1, f"  -> method_status={payload.get('method_status')}")
    _log(1, f"  -> discovered_count={payload.get('discovered_count', 'N/A')}")

    if payload.get("method_status") == "pending_auth":
        method_id = payload["method_id"]
        method_payload = _get_method(method_id)
        recipe = method_payload["dsl_recipe"]
        assert recipe["recipe_type"] == "multi_dsl"
        assert recipe["auth_ref"] == "wechat_mp_default"
        recipe_str = str(recipe)
        assert "WECHAT_MP_COOKIE" not in recipe_str
        assert "WECHAT_MP_TOKEN" not in recipe_str
        _log(1, "  -> pending_auth branch verified")
        return

    assert payload["status"] == "completed"
    assert payload["route"]["kind"] == "wechat"
    assert payload["route"]["input_type"] == "wechat_history"
    assert payload["method_id"]
    method_id = payload["method_id"]

    _log(2, f"GET /discovery/methods/{method_id}  (verify stored wechat_history recipe)")
    method_payload = _get_method(method_id)
    recipe = method_payload["dsl_recipe"]
    _log(2, f"  -> recipe_type={recipe['recipe_type']}")
    _log(2, f"  -> auth_ref={recipe.get('auth_ref')}")
    _log(2, f"  -> requires_auth={recipe.get('requires_auth')}")
    _log(2, f"  -> actions={len(recipe['actions'])}")
    for i, action in enumerate(recipe["actions"]):
        _log(2, f"     action[{i}]: op={action.get('op')}")

    assert recipe["recipe_type"] == "multi_dsl"
    assert recipe["source_kind"] == "wechat"
    assert recipe["auth_ref"] == "wechat_mp_default"
    assert len(recipe["actions"]) == 3
    assert recipe["actions"][0]["op"] == "wechat_fetch_account_history"
    assert recipe["actions"][1]["op"] == "extract"
    assert recipe["actions"][2]["op"] == "dedup_by"

    # ── Step 3: Fetch using the saved method ────────────────────────
    # _log(3, f"POST /discovery/methods/{method_id}/fetch  (run saved wechat_history recipe)")
    # with httpx.Client(base_url=API_BASE, timeout=300) as client:
    #     fetch_response = client.post(f"/discovery/methods/{method_id}/fetch", json=None)
    # assert fetch_response.status_code == 200, f"fetch failed: {fetch_response.status_code} {fetch_response.text[:300]}"
    # fetch_payload = fetch_response.json()
    # _log(3, f"  -> discovered_count={fetch_payload.get('discovered_count')}")
    # _log(3, f"  -> stored_count={fetch_payload.get('stored_count')}")
    # assert "items" in fetch_payload
    # assert "stats" in fetch_payload


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv(RUN_FLAG) != "1",
    reason=f"set {RUN_FLAG}=1 to run live multi discovery route coverage",
)
def test_multi_route_internal_mcp_live():
    query = os.getenv("MULTI_DISCOVERY_INTERNAL_QUERY", "[km] Linux")

    print(f"\n{'='*60}")
    print("  Multi Route 4: Internal MCP")
    print(f"  Query: {query!r}")
    print(f"{'='*60}")

    _log(1, f"POST /discovery/multi-run  (input={query!r})")
    payload = _post_multi_run(
        {"input": query, "force": True, "name": "multi internal mcp live"},
        timeout=30,
    )
    _log(1, f"  -> status={payload['status']}")
    _log(1, f"  -> route.kind={payload['route']['kind']}")
    _log(1, f"  -> route.input_type={payload['route']['input_type']}")
    _log(1, f"  -> markers={payload['route'].get('markers')}")
    _log(1, f"  -> branch_artifact.status={payload.get('branch_artifact', {}).get('status')}")
    _log(1, f"  -> mcp_action_contract={payload.get('branch_artifact', {}).get('mcp_action_contract')}")

    assert payload["status"] == "accepted"
    assert payload["route"]["kind"] == "internal_mcp"
    assert payload["branch_artifact"]["source_kind"] == "internal_mcp"
    assert payload["branch_artifact"]["status"] == "needs_implementation"
    assert payload["branch_artifact"]["mcp_action_contract"]["op"] == "mcp_call"
