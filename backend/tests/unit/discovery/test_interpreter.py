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


def test_loop_pagination_until_count():
    page_data = {
        1: [{"no": "1"}, {"no": "2"}],
        2: [{"no": "3"}, {"no": "4"}, {"no": "5"}],
    }
    def fake_fetch(action, ctx):
        p = ctx["vars"].get("page", 1)
        ctx["last_fetch"] = {"obj": {"records": page_data.get(p, []), "hasMore": p < 2}}
    recipe = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "set", "var": "page", "value": 1},
        {"op": "loop",
         "until": {"count_of": "items", "op": ">=", "value": 5},
         "max_iters": 5,
         "body": [
             {"op": "fetch", "mode": "json", "url": "https://x.com/api?p={{page}}"},
             {"op": "extract", "from": "obj.records", "fields": {"title": "no", "url": "template:https://x.com/{item.no}"}, "merge": True},
         ],
         "on_each": [{"op": "set", "var": "page", "expr": "{{page}} + 1"}]},
    ])
    out = DslInterpreter(fetch_fn=fake_fetch).run(recipe)
    assert len(out["items"]) == 5


def test_loop_max_iters_hard_stop():
    def fake_fetch(action, ctx):
        ctx["last_fetch"] = {"obj": {"records": [{"no": "1"}], "hasMore": True}}
    recipe = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "set", "var": "page", "value": 1},
        {"op": "loop", "until": {"count_of": "items", "op": ">=", "value": 100},
         "max_iters": 3,
         "body": [{"op": "fetch", "mode": "json", "url": "https://x.com/api"},
                  {"op": "extract", "from": "obj.records", "fields": {"title": "no", "url": "template:https://x.com/{item.no}"}, "merge": True}],
         "on_each": [{"op": "set", "var": "page", "expr": "{{page}} + 1"}]},
    ])
    out = DslInterpreter(fetch_fn=fake_fetch).run(recipe)
    assert len(out["items"]) == 3  # max_iters=3 截断


def test_goto_wait_click_extract():
    # browser_fn 模拟 Playwright：记录动作，extract 返回固定 items
    calls = []
    def fake_browser(action, ctx, page=None):
        calls.append(action.op)
        if action.op == "extract":
            return [{"title": "A", "url": "https://x.com/1"}, {"title": "B", "url": "https://x.com/2"}]
        return None
    recipe = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "goto", "url": "https://x.com/news"},
        {"op": "wait_for", "selector": "article"},
        {"op": "click", "selector": "button.load-more"},
        {"op": "extract", "from": "selector:article", "fields": {"title": "h2", "url": "a@href"}},
    ])
    out = DslInterpreter(browser_fn=fake_browser).run(recipe)
    assert calls == ["goto", "wait_for", "click", "extract"]
    assert len(out["items"]) == 2
