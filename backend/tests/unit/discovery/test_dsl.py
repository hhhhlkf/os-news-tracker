"""DSL 规约单元测试 — 验证原语 Pydantic 模型与 discriminated union。"""

import pytest
from pydantic import ValidationError
from app.discovery.dsl import DslRecipe, FetchAction, ExtractAction, LoopAction


def test_fetch_action_valid():
    a = FetchAction(op="fetch", mode="json", url="https://x.com/api")
    assert a.method == "GET"


def test_fetch_action_rejects_bad_mode():
    with pytest.raises(ValidationError):
        FetchAction(op="fetch", mode="xml", url="https://x.com")


def test_extract_action_with_template_field():
    a = ExtractAction(op="extract", from_="obj.records",
                      fields={"title": "title", "url": "template:https://x/{item.no}"})
    assert a.fields["url"].startswith("template:")


def test_dsl_recipe_discriminated_union():
    r = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title"}},
    ])
    assert len(r.actions) == 2
    assert isinstance(r.actions[0], FetchAction)
    assert isinstance(r.actions[1], ExtractAction)


def test_loop_action_max_iters_bounds():
    # max_iters 必填且 1≤≤20
    with pytest.raises(ValidationError):
        LoopAction(op="loop", until={"count_of": "items", "op": ">=", "value": 10}, body=[])
    with pytest.raises(ValidationError):
        LoopAction(op="loop", until={"count_of": "items", "op": ">=", "value": 10}, max_iters=0, body=[])
    with pytest.raises(ValidationError):
        LoopAction(op="loop", until={"count_of": "items", "op": ">=", "value": 10}, max_iters=21, body=[])


def test_loop_action_valid():
    a = LoopAction(op="loop", until={"count_of": "items", "op": ">=", "value": 10},
                   max_iters=5, body=[{"op": "fetch", "mode": "json", "url": "https://x.com/api"}])
    assert a.max_iters == 5
    assert len(a.body) == 1
    assert isinstance(a.body[0], FetchAction)


from app.discovery.dsl import validate_semantics, render_vars, eval_condition


def test_validate_extract_needs_url_field():
    r = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title"}},
    ])
    errors = validate_semantics(r)
    assert any("url" in e for e in errors)


def test_validate_from_matches_mode():
    r = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "selector:div", "fields": {"url": "template:https://x/{item.id}"}},
    ])
    errors = validate_semantics(r)
    assert any("from" in e for e in errors)


def test_validate_passes_when_url_field_present():
    r = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
    ])
    assert validate_semantics(r) == []


def test_render_vars():
    ctx = {"vars": {"entry_url": "https://x.com", "page": 2}}
    assert render_vars("{{entry_url}}/p/{{page}}", ctx) == "https://x.com/p/2"


def test_render_vars_missing_var_to_empty():
    ctx = {"vars": {"entry_url": "https://x.com"}}
    assert render_vars("{{entry_url}}/{{missing}}", ctx) == "https://x.com/"


def test_eval_condition_count_of():
    ctx = {"items": [1, 2, 3]}
    assert eval_condition({"count_of": "items", "op": ">=", "value": 3}, ctx) is True
    assert eval_condition({"count_of": "items", "op": ">=", "value": 5}, ctx) is False


def test_eval_condition_var():
    ctx = {"vars": {"page": 3}}
    assert eval_condition({"var": "page", "op": ">", "value": 2}, ctx) is True
    assert eval_condition({"var": "page", "op": ">", "value": 5}, ctx) is False


def test_eval_condition_missing_path_with_order_comparator_returns_false():
    ctx = {"last_fetch": None}
    assert eval_condition({"path": "obj.records", "op": ">=", "value": []}, ctx) is False
