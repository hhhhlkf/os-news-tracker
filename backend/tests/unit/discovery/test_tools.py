"""LangChain @tool 工具集单元测试 — mock httpx/Playwright 验证工具产出。"""

from app.discovery.tools import fetch_page, capture_network, inspect_item, test_url_template, probe_url_patterns


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


def test_inspect_item_tolerates_concatenated_json_response(monkeypatch):
    class FakeResp:
        status_code = 200
        text = (
            '{"status":200,"msg":"ok","obj":{"records":[{"id":"1"}]}}'
            '{"status":201,"msg":"查询失败"}'
        )

        def json(self):
            raise ValueError("Extra data: line 1 column 57 (char 56)")

    monkeypatch.setattr("httpx.request", lambda *a, **k: FakeResp())
    out = inspect_item.invoke({"api_url": "https://x.com/api/1", "method": "POST"})
    assert out["status"] == 200
    assert out["sample"]["status"] == 200
    assert out["sample"]["obj"]["records"][0]["id"] == "1"


def test_capture_network_parses_json_responses(monkeypatch):
    """mock sync_playwright：验证 on_response 回调解析 JSON 并收集到 caps。"""
    seen = {}

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
            seen["goto_kwargs"] = k
            if self._handler:
                self._handler(FakeResp("https://x.com/api", '{"a": 1}'))
        def wait_for_timeout(self, ms):
            seen["wait_ms"] = ms
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
    assert seen["goto_kwargs"]["timeout"] == 15000
    assert seen["wait_ms"] == 1000


def test_probe_url_patterns_uses_default_candidates(monkeypatch):
    class FakeResp:
        status_code = 200
        text = "<html><title>Article</title></html>"
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResp())
    out = probe_url_patterns.invoke({"base_url": "https://x.com", "id_value": "42"})
    assert isinstance(out, list) and len(out) > 0
    # spec §4.4 形状：每条含 pattern/generated_url/status/is_article_page
    first = out[0]
    assert {"pattern", "generated_url", "status", "is_article_page"} <= set(first.keys())
    assert first["status"] == 200
    assert first["is_article_page"] is True
    assert "42" in first["generated_url"]


def test_probe_url_patterns_with_custom_patterns(monkeypatch):
    calls = []

    class FakeResp:
        status_code = 200
        text = "<html><title>T</title></html>"

    def fake_get(url, **k):
        calls.append(url)
        return FakeResp()

    monkeypatch.setattr("httpx.get", fake_get)
    out = probe_url_patterns.invoke({
        "base_url": "https://x.com",
        "id_value": "7",
        "patterns": ["/blog/{id}", "/post/{id}"],
    })
    assert len(out) == 2
    assert out[0]["pattern"] == "/blog/{id}"
    assert out[0]["generated_url"] == "https://x.com/blog/7"
    assert out[1]["pattern"] == "/post/{id}"
    assert out[1]["generated_url"] == "https://x.com/post/7"
    assert calls == ["https://x.com/blog/7", "https://x.com/post/7"]


def test_probe_url_patterns_marks_non_article_and_errors(monkeypatch):
    def fake_get(url, **k):
        if "/blog/" in url:
            return type("R", (), {"status_code": 404, "text": "<html><title>Not Found</title></html>"})()
        raise Exception("boom")

    monkeypatch.setattr("httpx.get", fake_get)
    out = probe_url_patterns.invoke({
        "base_url": "https://x.com",
        "id_value": "1",
        "patterns": ["/blog/{id}", "/post/{id}"],
    })
    assert out[0]["status"] == 404
    assert out[0]["is_article_page"] is False  # status != 200
    assert out[1]["status"] == 0
    assert out[1]["is_article_page"] is False
    assert "error" in out[1]  # 异常带 error 字段
