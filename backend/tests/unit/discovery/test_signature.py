"""去重签名单元测试 — 验证签名稳定性 + loop 影响签名。"""

from app.discovery.signature import compute_signature
from app.discovery.dsl import DslRecipe


def test_signature_stable_for_same_recipe():
    r = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
        {"op": "loop", "until": {"count_of": "items", "op": ">=", "value": 10}, "max_iters": 5, "body": []},
    ])
    assert compute_signature(r) == compute_signature(r)


def test_signature_differs_when_loop_added():
    base = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
    ])
    with_loop = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
        {"op": "loop", "until": {"count_of": "items", "op": ">=", "value": 10}, "max_iters": 5, "body": []},
    ])
    assert compute_signature(base) != compute_signature(with_loop)


def test_signature_differs_when_fetch_url_host_differs():
    a = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
    ])
    b = DslRecipe(entry_url="https://y.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://y.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title", "url": "template:https://y/{item.no}"}},
    ])
    assert compute_signature(a) != compute_signature(b)
