"""SiteDiscoveryGraph 单元测试 — supervisor 路由优先级 + 接力顺序。"""

import json

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


_DSL_RECIPE_MOCK = {
    "entry_url": "https://x.com",
    "actions": [
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records",
         "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
    ],
}


class _MockChat:
    """Mock LangChain ChatModel：bind_tools/with_structured_output 返回 self，invoke 返回固定产出。

    传 returns= 可定制（auditor 用 AuditVerdict dict，dsl_writer 用默认 recipe dict）。
    """

    def __init__(self, returns=None):
        self._returns = returns if returns is not None else _DSL_RECIPE_MOCK

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema):
        return self

    def invoke(self, msgs):
        return self._returns


def test_dsl_writer_produces_recipe_with_mock_llm():
    from app.discovery.graph import dsl_writer
    state = _state(
        exploration={"candidate_api": "https://x.com/api", "id_field": "no"},
        url_rule={"template": "https://x/{item.no}"},
    )
    out = dsl_writer(state, llm=_MockChat())
    assert out["dsl_recipe"] is not None
    assert out["dsl_recipe"]["actions"][0]["op"] == "fetch"


def test_dsl_writer_path_join_shortcut_produces_template_url():
    from app.discovery.graph import dsl_writer
    state = _state(
        site_url="https://www.openeuler.org",
        exploration={
            "list_url": "https://www.openeuler.org/api-search/search/sort/blog",
            "fetch": {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "query": {},
                "json_body": {"category": "blog", "page": 1, "pageSize": 12},
            },
            "format_locator": {"kind": "json_path", "value": "obj.records"},
            "fields": {"title": "title", "published_at": "date", "summary": "summary", "content": "textContent"},
        },
        url_rule={"mode": "path_join", "base_url": "https://www.openeuler.org", "path_field": "path"},
    )
    out = dsl_writer(state)
    extract = next(a for a in out["dsl_recipe"]["actions"] if a["op"] == "extract")
    assert extract["fields"]["url"] == "template:https://www.openeuler.org{item.path}"


def test_dsl_writer_increments_token_usage():
    from app.discovery.graph import dsl_writer
    state = _state(token_used=500, url_rule={"template": "https://x/{item.no}"})
    out = dsl_writer(state, llm=_MockChat())
    assert out["token_used"] == 1500  # +1000 per dsl_writer call


def test_dsl_writer_uses_json_object_llm_path_in_production(monkeypatch):
    from app.discovery import graph as graph_mod

    captured = {}

    def fake_complete(self, prompt, *, temperature=0.2, response_format=None):
        captured["prompt"] = prompt
        captured["temperature"] = temperature
        captured["response_format"] = response_format
        return json.dumps(_DSL_RECIPE_MOCK, ensure_ascii=False)

    monkeypatch.setattr("app.llm.client.LlmClient.complete", fake_complete)
    state = _state(
        site_url="https://x.com",
        exploration={"candidate_api": "https://x.com/api", "fields": {"title": "title"}},
        url_rule={"mode": "template", "template": "https://x/{id}", "id_field": "id"},
    )
    out = graph_mod.dsl_writer(state)
    assert captured["response_format"] == {"type": "json_object"}
    assert out["dsl_recipe"]["actions"][0]["op"] == "fetch"


_AUDIT_PASS = {"passed": True, "is_real_content": True, "has_pagination": True,
               "not_blocked": True, "value_assessment": "真文章+有翻页", "issues": [], "suggested_fix": None}
_AUDIT_FAIL_SINGLE_PAGE = {"passed": False, "is_real_content": True, "has_pagination": False,
                           "not_blocked": True, "value_assessment": "只抓单页",
                           "issues": ["只抓到单页，未实现翻页"], "suggested_fix": "加 loop 翻页"}
_AUDIT_FAIL_ANTIBOT = {"passed": False, "is_real_content": False, "has_pagination": False,
                       "not_blocked": False, "value_assessment": "反爬验证页",
                       "issues": ["抓到反爬验证页，正文为空"], "suggested_fix": "换 render_js / 加反爬绕过"}


_EXPLORATION_JSON_API = {
    "source_type": "json_api",
    "list_url": "https://x.com/api",
    "fetch": {"method": "GET", "headers": {}, "query": {}, "json_body": None},
    "format_locator": {"kind": "json_path", "value": "obj.records"},
    "fields": {
        "id": "id",
        "title": "title",
        "url": "url",
        "published_at": "published_at",
        "summary": "summary",
        "content": "content",
    },
    "html_selectors": {
        "item_selector": None,
        "link_selector": None,
        "title_selector": None,
        "date_selector": None,
    },
    "sample_items": [
        {
            "raw": {"id": "1", "title": "A", "url": "https://x.com/a", "published_at": "2026-07-01"},
            "id": "1",
            "title": "A",
            "url": "https://x.com/a",
            "published_at": "2026-07-01",
        }
    ],
    "url_candidates": [
        {
            "mode": "existing_url",
            "url_field": "url",
            "id_field": None,
            "template": None,
            "verification": "1/1 opened",
        }
    ],
    "pagination": {
        "type": "none",
        "page_param": None,
        "size_param": None,
        "offset_param": None,
        "limit_param": None,
        "cursor_param": None,
        "next_path": None,
        "has_more_path": None,
        "start": 1,
        "size": None,
        "notes": "single page",
    },
    "evidence": [{"tool": "capture_network", "summary": "api_url=https://x.com/api · items_path=obj.records"}],
    "notes": ["无"],
    "success": True,
}


def test_auditor_rejects_when_no_items_crawled():
    from app.discovery.graph import auditor
    state = _state(dsl_recipe={"entry_url": "https://x.com", "actions": []}, attempt=0)
    out = auditor(state, llm=_MockChat(returns=_AUDIT_FAIL_ANTIBOT),
                  test_fn=lambda recipe: {"items": [], "stats": {"discovered_count": 0}})
    assert out["audit_result"]["passed"] is False
    assert out["attempt"] == 1  # 不通过则 attempt+1


def test_auditor_passes_when_llm_approves_real_items():
    from app.discovery.graph import auditor
    recipe_dict = {
        "entry_url": "https://x.com",
        "actions": [
            {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
            {"op": "extract", "from": "obj.records",
             "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
            {"op": "loop", "until": {"count_of": "items", "op": ">=", "value": 20},
             "max_iters": 5, "body": []},
        ],
    }
    items = [{"title": f"文章{i}", "url": f"https://x.com/{i}"} for i in range(12)]
    state = _state(dsl_recipe=recipe_dict, attempt=0)
    out = auditor(state, llm=_MockChat(returns=_AUDIT_PASS),
                  test_fn=lambda recipe: {"items": items, "stats": {"discovered_count": 12}})
    assert out["audit_result"]["passed"] is True
    assert out["attempt"] == 0  # 通过则 attempt 不增
    assert out["audit_result"]["llm_verdict"]["has_pagination"] is True


def test_auditor_uses_json_object_llm_path_in_production(monkeypatch):
    from app.discovery.graph import auditor

    captured = {}

    def fake_complete(self, prompt, *, temperature=0.2, response_format=None):
        captured["prompt"] = prompt
        captured["temperature"] = temperature
        captured["response_format"] = response_format
        return json.dumps(_AUDIT_PASS, ensure_ascii=False)

    monkeypatch.setattr("app.llm.client.LlmClient.complete", fake_complete)
    state = _state(dsl_recipe={"entry_url": "https://x.com", "actions": []}, attempt=0)
    out = auditor(
        state,
        test_fn=lambda recipe: {
            "items": [{"title": "A", "url": "https://x.com/a", "content": "body"}],
            "stats": {"discovered_count": 1},
        },
    )
    assert captured["response_format"] == {"type": "json_object"}
    assert out["audit_result"]["passed"] is True


def test_auditor_flags_single_page_no_pagination():
    from app.discovery.graph import auditor
    recipe_dict = {  # 无 loop → 单页
        "entry_url": "https://x.com",
        "actions": [
            {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
            {"op": "extract", "from": "obj.records",
             "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
        ],
    }
    items = [{"title": "A", "url": "https://x.com/1"}]
    state = _state(dsl_recipe=recipe_dict, attempt=0)
    out = auditor(state, llm=_MockChat(returns=_AUDIT_FAIL_SINGLE_PAGE),
                  test_fn=lambda recipe: {"items": items, "stats": {"discovered_count": 1}})
    assert out["audit_result"]["passed"] is False
    assert out["audit_result"]["llm_verdict"]["has_pagination"] is False
    assert out["attempt"] == 1


def test_auditor_flags_antibot_content():
    from app.discovery.graph import auditor
    recipe_dict = {
        "entry_url": "https://x.com",
        "actions": [
            {"op": "fetch", "mode": "html", "url": "https://x.com"},
            {"op": "extract", "from": "selector:div", "fields": {"title": "h1", "url": "attr:href"}},
        ],
    }
    items = [{"title": "Access Denied", "url": "https://x.com/denied", "content": ""}]
    state = _state(dsl_recipe=recipe_dict, attempt=0)
    out = auditor(state, llm=_MockChat(returns=_AUDIT_FAIL_ANTIBOT),
                  test_fn=lambda recipe: {"items": items, "stats": {"discovered_count": 1}})
    assert out["audit_result"]["passed"] is False
    assert out["audit_result"]["llm_verdict"]["is_real_content"] is False


# --- explorer worker ---

def test_explorer_uses_second_stage_to_structure_freeform_agent_output(monkeypatch):
    """explorer 允许第一阶段输出自由文本，并交给第二阶段整理。"""
    from langchain_core.messages import AIMessage
    from app.discovery import graph as graph_mod

    class _FakeAgent:
        def invoke(self, args):
            return {"messages": [AIMessage(content="已发现一个 JSON API，字段看起来完整，建议后续整理。")]}

    def fake_create_react_agent(llm, tools, prompt=None, **kw):
        assert prompt == graph_mod.EXPLORER_SYSTEM_PROMPT  # 确认系统 prompt 传进去了
        return _FakeAgent()

    def fake_synthesize_exploration(*, site_url, result, deterministic_exploration):
        assert site_url == "https://x.com"
        assert deterministic_exploration is None
        return json.dumps(_EXPLORATION_JSON_API, ensure_ascii=False), _EXPLORATION_JSON_API

    monkeypatch.setattr("langgraph.prebuilt.create_react_agent", fake_create_react_agent)
    monkeypatch.setattr(graph_mod, "_synthesize_exploration", fake_synthesize_exploration)
    out = graph_mod.explorer(_state(site_url="https://x.com"), llm=_MockChat())
    assert out["exploration"]["source_type"] == "json_api"
    assert out["exploration"]["list_url"] == "https://x.com/api"
    assert out["exploration"]["success"] is True
    assert out["explorer_parse_error"] is None


def test_explorer_returns_unknown_json_when_synthesis_fails_without_deterministic_evidence(monkeypatch):
    """第二阶段失败且无确定性证据时，explorer 仍返回合法 unknown 结果。"""
    from langchain_core.messages import AIMessage
    from app.discovery import graph as graph_mod

    class _FakeAgent:
        def invoke(self, args):
            return {"messages": [AIMessage(content="抱歉，我无法探查该站点。")]}

    monkeypatch.setattr("langgraph.prebuilt.create_react_agent",
                        lambda llm, tools, prompt=None, **kw: _FakeAgent())
    monkeypatch.setattr(
        graph_mod,
        "_synthesize_exploration",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("second stage failed")),
    )
    out = graph_mod.explorer(_state(site_url="https://x.com"), llm=_MockChat())
    assert out["exploration"]["source_type"] == "unknown"
    assert out["exploration"]["success"] is False
    assert out["explorer_parse_error"] == "second stage failed"


def test_explorer_passes_existing_network_captures_to_agent(monkeypatch):
    from app.discovery import graph as graph_mod

    seen = {}

    class _FakeAgent:
        def invoke(self, args):
            seen["args"] = args
            return {"messages": []}

    monkeypatch.setattr(
        "langgraph.prebuilt.create_react_agent",
        lambda llm, tools, prompt=None, **kw: _FakeAgent(),
    )
    graph_mod.explorer(_state(
        site_url="https://x.com",
        network_captures=[{
            "api_url": "https://x.com/api/list",
            "method": "POST",
            "parsed_json": {"obj": {"records": [{"id": "1", "title": "A"}]}},
            "request_json_body": {"category": "blog", "page": 1, "pageSize": 12},
        }],
        homepage={"title": "Example Site", "links": ["https://x.com/a"]},
    ), llm=_MockChat())
    user_msg = seen["args"]["messages"][0][1]
    assert "https://x.com/api/list" in user_msg
    assert "records" in user_msg
    assert "category" in user_msg


def test_explorer_falls_back_to_deterministic_exploration_when_synthesis_fails(monkeypatch):
    from langchain_core.messages import AIMessage
    from app.discovery import graph as graph_mod

    class _FakeAgent:
        def invoke(self, args):
            return {"messages": [AIMessage(content="我看到了候选 API，但把最终结构化结果交给整理器。")]}

    monkeypatch.setattr(
        "langgraph.prebuilt.create_react_agent",
        lambda llm, tools, prompt=None, **kw: _FakeAgent(),
    )
    monkeypatch.setattr(
        graph_mod,
        "_synthesize_exploration",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("second stage failed")),
    )
    out = graph_mod.explorer(_state(site_url="https://x.com"), llm=_MockChat())
    assert out["exploration"]["source_type"] == "unknown"
    assert out["exploration"]["success"] is False
    assert out["explorer_parse_error"] == "second stage failed"


def test_explorer_uses_inspect_item_evidence_for_deterministic_fallback(monkeypatch):
    from langchain_core.messages import AIMessage, ToolMessage
    from app.discovery import graph as graph_mod

    class _FakeAgent:
        def invoke(self, args):
            return {
                "messages": [
                    AIMessage(
                        content="我先检查候选 API 的 item 结构。",
                        tool_calls=[{
                            "name": "inspect_item",
                            "args": {
                                "api_url": "https://www.openeuler.org/api-search/search/sort/blog",
                                "method": "POST",
                                "json_body": {"category": "blog", "page": 1, "pageSize": 12},
                            },
                            "id": "call_inspect_1",
                        }],
                    ),
                    ToolMessage(
                        name="inspect_item",
                        tool_call_id="call_inspect_1",
                        content=json.dumps({
                            "status": 200,
                            "sample": {
                                "obj": {
                                    "records": [
                                        {"path": "/zh/blog/a", "title": "A", "date": "2026-07-01", "summary": "S1"},
                                        {"path": "/zh/blog/b", "title": "B", "date": "2026-07-02", "summary": "S2"},
                                    ]
                                }
                            },
                        }, ensure_ascii=False),
                    ),
                    AIMessage(content="找到候选 API，等待结构化整理。"),
                ]
            }

    monkeypatch.setattr(
        "langgraph.prebuilt.create_react_agent",
        lambda llm, tools, prompt=None, **kw: _FakeAgent(),
    )
    monkeypatch.setattr(
        graph_mod,
        "_synthesize_exploration",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("second stage failed")),
    )
    out = graph_mod.explorer(_state(site_url="https://x.com"), llm=_MockChat())
    assert out["exploration"]["source_type"] == "json_api"
    assert out["exploration"]["success"] is True
    assert out["exploration"]["list_url"] == "https://www.openeuler.org/api-search/search/sort/blog"
    assert out["exploration"]["fetch"]["method"] == "POST"
    assert out["exploration"]["fetch"]["json_body"]["category"] == "blog"
    assert out["exploration"]["format_locator"]["value"] == "obj.records"


def test_explorer_prefers_deterministic_network_capture_when_synthesis_returns_unknown(monkeypatch):
    from langchain_core.messages import AIMessage
    from app.discovery import graph as graph_mod

    class _FakeAgent:
        def invoke(self, args):
            return {"messages": [AIMessage(content="capture_network 已经给了主要证据，我没有更多补充。")]}

    monkeypatch.setattr(
        "langgraph.prebuilt.create_react_agent",
        lambda llm, tools, prompt=None, **kw: _FakeAgent(),
    )
    monkeypatch.setattr(
        graph_mod,
        "_synthesize_exploration",
        lambda **kwargs: (
            json.dumps({"source_type": "unknown", "success": False}, ensure_ascii=False),
            {"source_type": "unknown", "success": False},
        ),
    )
    out = graph_mod.explorer(_state(
        site_url="https://www.openeuler.org",
        network_captures=[{
            "api_url": "https://www.openeuler.org/api-search/search/sort/blog",
            "method": "POST",
            "status": 200,
            "request_json_body": {"category": "blog", "page": 1, "pageSize": 12},
            "parsed_json": {
                "obj": {
                    "records": [
                        {"path": "/zh/blog/a", "title": "A", "date": "2026-07-01", "summary": "S1"},
                        {"path": "/zh/blog/b", "title": "B", "date": "2026-07-02", "summary": "S2"},
                    ]
                }
            },
        }],
    ), llm=_MockChat())
    assert out["exploration"]["source_type"] == "json_api"
    assert out["exploration"]["success"] is True
    assert out["exploration"]["list_url"] == "https://www.openeuler.org/api-search/search/sort/blog"
    assert out["exploration"]["format_locator"]["value"] == "obj.records"
    assert out["exploration"]["fields"]["url"] == "path"
    assert out["exploration"]["sample_items"][0]["url"] == "https://www.openeuler.org/zh/blog/a"


# --- validator worker ---

def _fake_tool(ret):
    """假 @tool：.invoke 忽略入参，固定返回 ret。"""
    class _T:
        def invoke(self, args):
            return ret
    return _T()


def _mock_chat(guess):
    """假 ChatModel：with_structured_output 返回 self，invoke 返回 guess dict。"""
    class _C:
        def with_structured_output(self, schema):
            return self

        def invoke(self, msgs):
            return guess
    return _C()


_GUESS_TEMPLATE = {
    "mode": "template",
    "template": "https://x.com/blog/{id}",
    "id_field": "no",
    "url_field": None,
    "sample_items": [{"id": "1", "url": None, "title": "A", "raw": {}},
                     {"id": "2", "url": None, "title": "B", "raw": {}}],
    "confidence": "high",
    "reason": "exploration 有 id 字段 no",
}
_GUESS_EXISTING_URL = {
    "mode": "existing_url",
    "template": None,
    "id_field": None,
    "url_field": "link",
    "sample_items": [{"id": "1", "url": "https://x.com/a", "title": "A", "raw": {}}],
    "confidence": "high",
    "reason": "列表已有 link 字段",
}
_GUESS_PATH_JOIN = {
    "mode": "path_join",
    "template": None,
    "base_url": "https://www.openeuler.org",
    "path_field": "path",
    "id_field": None,
    "url_field": None,
    "sample_items": [
        {"id": None, "url": None, "title": "A", "raw": {"path": "/zh/blog/a"}},
        {"id": None, "url": None, "title": "B", "raw": {"path": "/zh/blog/b"}},
    ],
    "confidence": "high",
    "reason": "列表给的是 path，相对路径需与 base_url 拼接",
}
_GUESS_UNKNOWN = {
    "mode": "unknown",
    "template": None,
    "id_field": None,
    "url_field": None,
    "sample_items": [{"id": "1", "url": None, "title": "A", "raw": {}}],
    "confidence": "low",
    "reason": "无头绪",
}


def test_validator_existing_url_mode_skips_template_test(monkeypatch):
    """mode=existing_url：列表已有 url 字段，直接用，不调 test_url_template。"""
    monkeypatch.setattr("app.discovery.tools.test_url_template", _fake_tool({"results": []}))
    monkeypatch.setattr("app.discovery.tools.probe_url_patterns", _fake_tool([]))
    from app.discovery.graph import validator
    state = _state(site_url="https://x.com", exploration={"fields": {"url": "link"}})
    out = validator(state, llm=_mock_chat(_GUESS_EXISTING_URL))
    assert out["url_rule"]["evidence"] == "existing_url"
    assert out["url_rule"]["mode"] == "existing_url"
    assert out["url_rule"]["url_field"] == "link"


def test_validator_uses_json_object_llm_path_in_production(monkeypatch):
    monkeypatch.setattr("app.discovery.tools.test_url_template", _fake_tool({"results": []}))
    monkeypatch.setattr("app.discovery.tools.probe_url_patterns", _fake_tool([]))
    from app.discovery import graph as graph_mod

    captured = {}

    def fake_complete(self, prompt, *, temperature=0.2, response_format=None):
        captured["prompt"] = prompt
        captured["temperature"] = temperature
        captured["response_format"] = response_format
        return json.dumps(_GUESS_EXISTING_URL, ensure_ascii=False)

    monkeypatch.setattr("app.llm.client.LlmClient.complete", fake_complete)
    out = graph_mod.validator(_state(site_url="https://x.com", exploration={"fields": {"url": "link"}}))
    assert captured["response_format"] == {"type": "json_object"}
    assert out["url_rule"]["mode"] == "existing_url"
    assert out["url_rule"]["url_field"] == "link"


def test_validator_validates_when_test_url_template_succeeds(monkeypatch):
    monkeypatch.setattr("app.discovery.tools.test_url_template",
                        _fake_tool({"results": [{"url": "https://x.com/blog/1", "status": 200, "is_article_page": True}]}))
    monkeypatch.setattr("app.discovery.tools.probe_url_patterns", _fake_tool([]))
    from app.discovery.graph import validator
    state = _state(site_url="https://x.com", exploration={"candidate_api": "https://x.com/api"})
    out = validator(state, llm=_mock_chat(_GUESS_TEMPLATE))
    assert out["url_rule"]["evidence"].startswith("validated")
    assert out["url_rule"]["template"] == "https://x.com/blog/{id}"
    assert out["url_rule"]["id_field"] == "no"
    assert out["url_rule"]["mode"] == "template"


def test_validator_validates_path_join_with_real_field_name(monkeypatch):
    monkeypatch.setattr(
        "app.discovery.tools.test_path_join",
        _fake_tool({"results": [
            {
                "sample_value": "/zh/blog/a",
                "url": "https://www.openeuler.org/zh/blog/a",
                "status": 200,
                "is_article_page": True,
            },
            {
                "sample_value": "/zh/blog/b",
                "url": "https://www.openeuler.org/zh/blog/b",
                "status": 200,
                "is_article_page": True,
            },
        ]}),
    )
    monkeypatch.setattr("app.discovery.tools.test_url_template", _fake_tool({"results": []}))
    monkeypatch.setattr("app.discovery.tools.probe_url_patterns", _fake_tool([]))
    from app.discovery.graph import validator
    state = _state(site_url="https://www.openeuler.org", exploration={"fields": {"url": "path"}})
    out = validator(state, llm=_mock_chat(_GUESS_PATH_JOIN))
    assert out["url_rule"]["mode"] == "path_join"
    assert out["url_rule"]["base_url"] == "https://www.openeuler.org"
    assert out["url_rule"]["path_field"] == "path"
    assert out["url_rule"]["evidence"] == "validated 2/2"
    assert out["url_rule"]["validation_samples"][0]["sample_value"] == "/zh/blog/a"


def test_validator_falls_back_to_probe_when_test_fails(monkeypatch):
    monkeypatch.setattr("app.discovery.tools.test_url_template",
                        _fake_tool({"results": [{"url": "https://x.com/blog/1", "status": 404, "is_article_page": False}]}))
    monkeypatch.setattr("app.discovery.tools.probe_url_patterns",
                        _fake_tool([{"pattern": "/post/{id}", "generated_url": "https://x.com/post/1",
                                     "status": 200, "is_article_page": True}]))
    from app.discovery.graph import validator
    state = _state(site_url="https://x.com")
    out = validator(state, llm=_mock_chat(_GUESS_TEMPLATE))
    assert out["url_rule"]["evidence"].startswith("probed")
    assert out["url_rule"]["template"] == "https://x.com/post/{id}"


def test_validator_returns_unverified_when_both_fail(monkeypatch):
    monkeypatch.setattr("app.discovery.tools.test_url_template",
                        _fake_tool({"results": [{"url": "https://x.com/blog/1", "status": 404, "is_article_page": False}]}))
    monkeypatch.setattr("app.discovery.tools.probe_url_patterns",
                        _fake_tool([{"pattern": "/post/{id}", "generated_url": "https://x.com/post/1",
                                     "status": 404, "is_article_page": False}]))
    from app.discovery.graph import validator
    state = _state(site_url="https://x.com")
    out = validator(state, llm=_mock_chat(_GUESS_TEMPLATE))
    assert out["url_rule"]["evidence"] == "unverified"
    assert out["url_rule"]["template"] == "https://x.com/blog/{id}"  # 回退到 LLM 模板


def test_validator_probes_when_llm_gives_unknown(monkeypatch):
    monkeypatch.setattr("app.discovery.tools.test_url_template", _fake_tool({"results": []}))
    monkeypatch.setattr("app.discovery.tools.probe_url_patterns",
                        _fake_tool([{"pattern": "/p/{id}", "generated_url": "https://x.com/p/1",
                                     "status": 200, "is_article_page": True}]))
    from app.discovery.graph import validator
    state = _state(site_url="https://x.com")
    out = validator(state, llm=_mock_chat(_GUESS_UNKNOWN))
    assert out["url_rule"]["evidence"].startswith("probed")
    assert out["url_rule"]["template"] == "https://x.com/p/{id}"


# --- Task 12: graph assembly + save_method ---

def test_build_graph_uses_postgres_saver():
    """build_graph(checkpointer=None) 用 MemorySaver 编译，含 supervisor/explorer/save_method 节点。"""
    from app.discovery.graph import build_graph
    g = build_graph(checkpointer=None)  # None → MemorySaver（测试用）
    assert "supervisor" in g.nodes
    assert "explorer" in g.nodes
    assert "save_method" in g.nodes


def test_save_method_writes_crawl_method():
    """save_method 写 crawl_methods + crawl_method_domains + sources(type=discovery)，返回 verdict=dsl。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Base, CrawlMethod, CrawlMethodDomain
    from app.discovery.graph import save_method
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    state = {
        "site_url": "https://x.com",
        "dsl_recipe": {"entry_url": "https://x.com", "actions": []},
        "verdict": None,
    }
    try:
        out = save_method(state, db=s)
        m = s.query(CrawlMethod).filter_by(domain="x.com").first()
        assert m is not None
        assert m.entry_url == "https://x.com"
        assert m.status == "active"
        assert out["verdict"] == "dsl"
        assert out["method_id"] == m.id
        # 去重映射也写入
        assert s.query(CrawlMethodDomain).filter_by(domain="x.com").first() is not None
    finally:
        s.close()
        engine.dispose()


def test_reclaim_stale_runs_marks_leftover_running_as_failed():
    """启动回收：所有遗留 running 标 failed（带 error_message + ended_at），completed/failed 不动。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Base, SiteDiscoveryRun
    from app.discovery.graph import reclaim_stale_runs
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    try:
        s.add(SiteDiscoveryRun(site_url="https://a.com", status="running"))
        s.add(SiteDiscoveryRun(site_url="https://b.com", status="running"))
        s.add(SiteDiscoveryRun(site_url="https://c.com", status="completed"))
        s.add(SiteDiscoveryRun(site_url="https://d.com", status="failed", error_message="old err"))
        s.commit()

        n = reclaim_stale_runs(db=s)

        assert n == 2  # 只回收 2 个 running
        assert s.query(SiteDiscoveryRun).filter_by(status="running").count() == 0
        failed = s.query(SiteDiscoveryRun).filter_by(status="failed").all()
        assert len(failed) == 3  # 原 1 个 failed + 回收的 2 个
        # 回收的 2 个有 error_message + ended_at
        reclaimed = [r for r in failed if r.site_url in ("https://a.com", "https://b.com")]
        assert len(reclaimed) == 2
        for r in reclaimed:
            assert r.error_message is not None
            assert r.ended_at is not None
        # completed / 原 failed 不动
        assert s.query(SiteDiscoveryRun).filter_by(status="completed").count() == 1
        old_failed = s.query(SiteDiscoveryRun).filter_by(site_url="https://d.com").first()
        assert old_failed.error_message == "old err"  # 不被覆盖
    finally:
        s.close()
        engine.dispose()


def test_reclaim_stale_runs_timeout_only_reclaims_old():
    """定时巡检模式（older_than_seconds=N）：只回收 started_at 早于 cutoff 的 running，新 run 和 completed 不动。"""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Base, SiteDiscoveryRun
    from app.discovery.graph import reclaim_stale_runs
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    try:
        old_started = datetime.now(timezone.utc) - timedelta(hours=1)
        s.add(SiteDiscoveryRun(site_url="https://old.com", status="running", started_at=old_started))
        s.add(SiteDiscoveryRun(site_url="https://fresh.com", status="running"))  # server_default=now
        s.add(SiteDiscoveryRun(site_url="https://done.com", status="completed"))
        s.commit()

        n = reclaim_stale_runs(older_than_seconds=1800, db=s)  # 30 min cutoff

        assert n == 1  # 只收老的 1 个
        old = s.query(SiteDiscoveryRun).filter_by(site_url="https://old.com").first()
        assert old.status == "failed"
        assert "超时" in old.error_message
        # 新 run 没到超时，不动
        assert s.query(SiteDiscoveryRun).filter_by(site_url="https://fresh.com").first().status == "running"
        # completed 不动
        assert s.query(SiteDiscoveryRun).filter_by(site_url="https://done.com").first().status == "completed"
    finally:
        s.close()
        engine.dispose()
