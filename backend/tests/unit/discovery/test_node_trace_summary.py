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

def test_summary_auditor():
    upd = {"audit_result": {"passed": False, "issues": ["只抓单页"]},
           "attempt": 1}
    s = _step_summary("auditor", upd)
    assert s["passed"] is False
    assert s["attempt"] == 1

def test_summary_unknown_node():
    s = _step_summary("fetch_homepage", {"homepage": {"status": 200}})
    assert s == {}
