import respx
from fastapi.testclient import TestClient
from app.entry import app

client = TestClient(app)

@respx.mock
def test_suggest_name_uses_llm_short_name(monkeypatch):
    from app.api import discovery_routes

    respx.get("https://openanolis.cn/").respond(
        200, text="<html><head><title>OpenAnolis 开源社区</title></head><body></body></html>"
    )
    monkeypatch.setattr(
        discovery_routes.LlmClient,
        "complete",
        lambda self, prompt, temperature=0.2: "龙蜥社区",
    )
    r = client.post("/discovery/suggest-name", json={"url": "https://openanolis.cn/"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "龙蜥社区"


@respx.mock
def test_suggest_name_truncates_llm_output_to_twenty_chars(monkeypatch):
    from app.api import discovery_routes

    respx.get("https://openanolis.cn/").respond(
        200, text="<html><head><title>OpenAnolis 开源社区</title></head><body></body></html>"
    )
    monkeypatch.setattr(
        discovery_routes.LlmClient,
        "complete",
        lambda self, prompt, temperature=0.2: "  OpenAnolis社区站点技术资讯平台每日快报  ",
    )
    r = client.post("/discovery/suggest-name", json={"url": "https://openanolis.cn/"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "OpenAnolis社区站点技术资讯平台"


@respx.mock
def test_suggest_name_llm_failure_falls_back_to_title(monkeypatch):
    from app.api import discovery_routes

    respx.get("https://openanolis.cn/").respond(
        200, text="<html><head><title>OpenAnolis 开源社区</title></head><body></body></html>"
    )

    def fail_complete(self, prompt, temperature=0.2):
        raise RuntimeError("llm down")

    monkeypatch.setattr(discovery_routes.LlmClient, "complete", fail_complete)
    r = client.post("/discovery/suggest-name", json={"url": "https://openanolis.cn/"})
    assert r.status_code == 200
    assert r.json()["name"] == "OpenAnolis 开源社区"


@respx.mock
def test_suggest_name_falls_back_to_domain_when_no_title(monkeypatch):
    from app.api import discovery_routes

    respx.get("https://no-title.example.org/").respond(200, text="<html><head></head></html>")

    def fail_complete(self, prompt, temperature=0.2):
        raise RuntimeError("llm down")

    monkeypatch.setattr(discovery_routes.LlmClient, "complete", fail_complete)
    r = client.post("/discovery/suggest-name", json={"url": "https://no-title.example.org/"})
    assert r.status_code == 200
    assert r.json()["name"] == "no-title.example.org"


@respx.mock
def test_suggest_name_on_fetch_error_falls_back_to_domain(monkeypatch):
    from app.api import discovery_routes

    respx.get("https://down.example.net/").respond(503)

    def fail_complete(self, prompt, temperature=0.2):
        raise RuntimeError("llm down")

    monkeypatch.setattr(discovery_routes.LlmClient, "complete", fail_complete)
    r = client.post("/discovery/suggest-name", json={"url": "https://down.example.net/"})
    assert r.status_code == 200
    assert r.json()["name"] == "down.example.net"
