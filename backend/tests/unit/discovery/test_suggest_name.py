import respx
from fastapi.testclient import TestClient
from app.entry import app

client = TestClient(app)

@respx.mock
def test_suggest_name_from_title():
    respx.get("https://openanolis.cn/").respond(
        200, text="<html><head><title>OpenAnolis 开源社区</title></head><body></body></html>"
    )
    r = client.post("/discovery/suggest-name", json={"url": "https://openanolis.cn/"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "OpenAnolis 开源社区"

@respx.mock
def test_suggest_name_falls_back_to_domain():
    respx.get("https://no-title.example.org/").respond(200, text="<html><head></head></html>")
    r = client.post("/discovery/suggest-name", json={"url": "https://no-title.example.org/"})
    assert r.status_code == 200
    assert r.json()["name"] == "no-title.example.org"

@respx.mock
def test_suggest_name_on_fetch_error_falls_back_to_domain():
    respx.get("https://down.example.net/").respond(503)
    r = client.post("/discovery/suggest-name", json={"url": "https://down.example.net/"})
    assert r.status_code == 200
    assert r.json()["name"] == "down.example.net"
