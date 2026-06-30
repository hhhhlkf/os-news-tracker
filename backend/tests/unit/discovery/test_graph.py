"""SiteDiscoveryGraph 单元测试 — supervisor 路由优先级 + 接力顺序。"""

from app.discovery.graph import supervisor_route, DiscoveryState, TOKEN_BUDGET, MAX_ATTEMPTS


def _state(**overrides) -> DiscoveryState:
    base = dict(
        site_url="https://x.com", homepage={}, network_captures=[], exploration={},
        url_rule=None, dsl_recipe=None, audit_result=None,
        attempt=0, verdict=None, method_id=None, token_used=0, error=None,
    )
    base.update(overrides)
    return DiscoveryState(**base)


def test_route_after_capture_goes_to_explorer():
    state = _state(network_captures=[{"api_url": "x"}], exploration={})
    assert supervisor_route(state) == "explorer"


def test_route_after_explorer_goes_to_validator():
    state = _state(exploration={"candidate_api": "x"}, url_rule=None)
    assert supervisor_route(state) == "validator"


def test_route_after_dsl_writer_goes_to_auditor():
    state = _state(url_rule={"template": "https://x/{item.no}"}, dsl_recipe={"actions": []})
    assert supervisor_route(state) == "auditor"


def test_route_audit_pass_goes_to_save():
    state = _state(url_rule={}, dsl_recipe={"actions": []}, audit_result={"passed": True})
    assert supervisor_route(state) == "save_method"


def test_route_audit_fail_attempts_exhausted_goes_to_end():
    state = _state(audit_result={"passed": False}, attempt=MAX_ATTEMPTS)
    assert supervisor_route(state) == "__end__"


def test_route_token_exceeded_goes_to_end():
    state = _state(token_used=TOKEN_BUDGET + 1)
    assert supervisor_route(state) == "__end__"


class _MockChat:
    """Mock LangChain ChatModel：bind_tools/with_structured_output 返回 self，invoke 返回固定产出。"""

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema):
        return self

    def invoke(self, msgs):
        return {
            "entry_url": "https://x.com",
            "actions": [
                {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
                {"op": "extract", "from": "obj.records",
                 "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
            ],
        }


def test_dsl_writer_produces_recipe_with_mock_llm():
    from app.discovery.graph import dsl_writer
    state = _state(
        exploration={"candidate_api": "https://x.com/api", "id_field": "no"},
        url_rule={"template": "https://x/{item.no}"},
    )
    out = dsl_writer(state, llm=_MockChat())
    assert out["dsl_recipe"] is not None
    assert out["dsl_recipe"]["actions"][0]["op"] == "fetch"


def test_dsl_writer_increments_token_usage():
    from app.discovery.graph import dsl_writer
    state = _state(token_used=500, url_rule={"template": "https://x/{item.no}"})
    out = dsl_writer(state, llm=_MockChat())
    assert out["token_used"] == 1500  # +1000 per dsl_writer call


def test_auditor_rejects_when_test_fails():
    from app.discovery.graph import auditor
    state = _state(dsl_recipe={"entry_url": "https://x.com", "actions": []}, attempt=0)
    out = auditor(state, llm=_MockChat(), test_fn=lambda recipe: {"discovered_count": 0})
    assert out["audit_result"]["passed"] is False
    assert out["attempt"] == 1  # 不通过则 attempt+1


def test_auditor_passes_when_test_meets_threshold():
    from app.discovery.graph import auditor
    recipe_dict = {
        "entry_url": "https://x.com",
        "actions": [
            {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
            {"op": "extract", "from": "obj.records",
             "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
        ],
    }
    state = _state(dsl_recipe=recipe_dict, attempt=0)
    out = auditor(state, llm=_MockChat(), test_fn=lambda recipe: {"discovered_count": 10})
    assert out["audit_result"]["passed"] is True
    assert out["attempt"] == 0  # 通过则 attempt 不增
