import os
import time

import httpx
import pytest

API_BASE = os.getenv("API_BASE", "http://localhost:8000")


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_MULTI_DISCOVERY_WEBSITE") != "1",
    reason="set RUN_LIVE_MULTI_DISCOVERY_WEBSITE=1 to run live multi website discovery",
)
def test_multi_run_delegates_website_to_existing_discovery():
    url = os.getenv("MULTI_DISCOVERY_WEBSITE_URL", "https://blogs.oracle.com/linux/feed")
    with httpx.Client(base_url=API_BASE, timeout=30) as client:
        response = client.post(
            "/discovery/multi-run",
            json={"input": url, "force": True, "name": "multi website live"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "started"
    assert payload["route"]["kind"] == "website"
    run_id = payload["run_id"]

    deadline = time.time() + 240
    run_payload = None
    with httpx.Client(base_url=API_BASE, timeout=30) as client:
        while time.time() < deadline:
            run_response = client.get(f"/discovery/runs/{run_id}")
            assert run_response.status_code == 200
            run_payload = run_response.json()
            if run_payload["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(2)

    assert run_payload is not None
    assert run_payload["status"] == "completed", run_payload
    assert run_payload["resulting_method_id"]
