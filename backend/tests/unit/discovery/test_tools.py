"""LangChain @tool 工具集单元测试 — mock httpx/Playwright 验证工具产出。"""

from app.discovery.tools import fetch_page, capture_network, inspect_item, test_url_template


def test_fetch_page_returns_summary(monkeypatch):
    class FakeResp:
        status_code = 200
        text = "<html><title>T</title><a href='/x'>L</a></html>"
        url = "https://x.com"
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResp())
    out = fetch_page.invoke({"url": "https://x.com", "render_js": False})
    assert out["status"] == 200
    assert out["title"] == "T"
    assert "https://x.com/x" in out["links"]


def test_test_url_template_validates(monkeypatch):
    monkeypatch.setattr(
        "httpx.get",
        lambda url, **k: type(
            "R",
            (),
            {
                "status_code": 200,
                "text": "<html><title>Article - Site</title></html>",
                "raise_for_status": lambda s: None,
            },
        )(),
    )
    out = test_url_template.invoke({
        "template": "https://x.com/{id}",
        "id_field": "no",
        "sample_items": [{"no": "1"}],
    })
    assert out["results"][0]["status"] == 200
    assert out["results"][0]["is_article_page"] is True
    assert out["results"][0]["url"] == "https://x.com/1"


def test_inspect_item_returns_sample(monkeypatch):
    class FakeResp:
        status_code = 200
        def json(self):
            return {"id": "1", "title": "A"}
    monkeypatch.setattr("httpx.request", lambda *a, **k: FakeResp())
    out = inspect_item.invoke({"api_url": "https://x.com/api/1"})
    assert out["status"] == 200
    assert out["sample"] == {"id": "1", "title": "A"}


def test_capture_network_parses_json_responses(monkeypatch):
    """mock sync_playwright：验证 on_response 回调解析 JSON 并收集到 caps。"""
    class FakeReq:
        method = "GET"
    class FakeResp:
        def __init__(self, url, body):
            self.url = url
            self._body = body
            self.status = 200
            self.request = FakeReq()
        def text(self):
            return self._body
    class FakePage:
        def __init__(self):
            self._handler = None
        def on(self, event, handler):
            if event == "response":
                self._handler = handler
        def goto(self, *a, **k):
            if self._handler:
                self._handler(FakeResp("https://x.com/api", '{"a": 1}'))
        def wait_for_timeout(self, *a, **k):
            pass
    class FakeBrowser:
        def new_page(self):
            return FakePage()
        def close(self):
            pass
    class FakeBrowserType:
        def launch(self, **k):
            return FakeBrowser()
    class FakePlaywright:
        chromium = FakeBrowserType()
    class FakeCM:
        def __enter__(self):
            return FakePlaywright()
        def __exit__(self, *a):
            return False
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakeCM())
    out = capture_network.invoke({"url": "https://x.com"})
    assert isinstance(out, list)
    assert out and out[0]["api_url"] == "https://x.com/api"
    assert out[0]["method"] == "GET"
    assert out[0]["parsed_json"] == {"a": 1}
