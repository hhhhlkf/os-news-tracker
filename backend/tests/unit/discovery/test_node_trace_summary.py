from app.discovery.graph import _step_summary

def test_summary_explorer():
    upd = {"exploration": {"source_type": "rss", "list_url": "/feed.xml", "success": True}}
    s = _step_summary("explorer", upd)
    assert s["source_type"] == "rss"
    assert s["list_url"] == "/feed.xml"

def test_summary_validator():
    upd = {"url_rule": {"mode": "existing_url", "url_field": "link", "evidence": "existing_url"}}
    s = _step_summary("validator", upd)
    assert s["mode"] == "existing_url"
    assert s["evidence"] == "existing_url"

def test_summary_dsl_writer():
    upd = {"dsl_recipe": {"actions": [{"op": "fetch"}, {"op": "extract"}, {"op": "dedup_by"}]},
           "token_used": 1200}
    s = _step_summary("dsl_writer", upd)
    assert s["actions"] == 3
    assert s["has_loop"] is False

def test_summary_dsl_writer_with_loop():
    upd = {"dsl_recipe": {"actions": [{"op": "fetch"}, {"op": "loop"}, {"op": "extract"}]}}
    s = _step_summary("dsl_writer", upd)
    assert s["actions"] == 3
    assert s["has_loop"] is True

def test_summary_auditor():
    upd = {"audit_result": {"passed": False, "errors": [],
                            "llm_verdict": {"issues": ["只抓单页"], "suggested_fix": "加 loop"}},
           "attempt": 1}
    s = _step_summary("auditor", upd)
    assert s["passed"] is False
    assert s["attempt"] == 1
    assert s["issues"] == ["只抓单页"]

def test_summary_fetch_homepage():
    s = _step_summary("fetch_homepage", {"homepage": {"status": 200, "title": "Example Blog", "links": ["/a", "/b", "/c"]}})
    assert s["status"] == 200
    assert s["title"] == "Example Blog"
    assert s["links"] == 3

def test_summary_fetch_homepage_empty():
    s = _step_summary("fetch_homepage", {})
    assert s["status"] is None
    assert s["title"] == ""
    assert s["links"] == 0

def test_summary_capture_network():
    s = _step_summary("capture_network", {"network_captures": [
        {"api_url": "https://example.com/api/posts", "method": "GET", "status": 200},
        {"api_url": "https://example.com/api/tags", "method": "GET", "status": 200},
    ]})
    assert s["json_apis"] == 2
    assert "api/posts" in s["sample_urls"]

def test_summary_capture_network_empty():
    s = _step_summary("capture_network", {})
    assert s["json_apis"] == 0
    assert s["sample_urls"] == "无"

def test_summary_unknown_node():
    s = _step_summary("supervisor", {"some": "data"})
    assert s == {}
