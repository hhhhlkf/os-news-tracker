"""DslInterpreter 单元测试 — mock fetch_fn，验证 fetch/extract/set/dedup 原语。"""

from app.discovery.interpreter import DslInterpreter
from app.discovery.dsl import DslRecipe


def test_fetch_extract_json():
    def fake_fetch(action, ctx):
        ctx["last_fetch"] = {"obj": {"records": [{"no": "1", "title": "A"}, {"no": "2", "title": "B"}]}}
    recipe = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records",
         "fields": {"title": "title", "url": "template:https://x.com/{item.no}"}},
    ])
    out = DslInterpreter(fetch_fn=fake_fetch).run(recipe)
    assert len(out["items"]) == 2
    assert out["items"][0]["url"] == "https://x.com/1"
    assert out["items"][0]["title"] == "A"


def test_dedup_by_url():
    def fake_fetch(action, ctx):
        ctx["last_fetch"] = {"obj": {"records": [{"no": "1", "title": "A"}, {"no": "1", "title": "A"}]}}
    recipe = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title", "url": "template:https://x.com/{item.no}"}},
        {"op": "dedup_by", "field": "url"},
    ])
    out = DslInterpreter(fetch_fn=fake_fetch).run(recipe)
    assert len(out["items"]) == 1


def test_set_var_and_extract_uses_var():
    def fake_fetch(action, ctx):
        ctx["last_fetch"] = {"obj": {"records": [{"no": "1", "title": "A"}]}}
    recipe = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "set", "var": "base", "value": "https://x.com"},
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records",
         "fields": {"title": "title", "url": "template:{{base}}/{item.no}"}},
    ])
    out = DslInterpreter(fetch_fn=fake_fetch).run(recipe)
    assert out["items"][0]["url"] == "https://x.com/1"


def test_extract_content_candidate_array():
    def fake_fetch(action, ctx):
        ctx["last_fetch"] = {"obj": {"records": [{"no": "1", "title": "A", "summary": "", "content": "body"}]}}
    recipe = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records",
         "fields": {"title": "title", "url": "template:https://x.com/{item.no}", "content": ["summary", "content"]}},
    ])
    out = DslInterpreter(fetch_fn=fake_fetch).run(recipe)
    assert out["items"][0]["content"] == "body"
