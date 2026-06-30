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
