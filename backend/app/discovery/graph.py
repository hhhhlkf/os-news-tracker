"""SiteDiscoveryGraph：生成命 LangGraph 图——State + supervisor 路由 + 确定性节点 + 4 worker。

supervisor 按 State 决定下一个 worker / 终止 / 转兜底；token 超 TOKEN_BUDGET 硬中止；
attempt 用尽判 failed。worker 在 Task 11 实装，图组装在 Task 12。
"""

from __future__ import annotations

from typing import TypedDict

from app.config import get_settings

TOKEN_BUDGET = 50000  # 生成命 token 硬上限，超即中止
MAX_ATTEMPTS = 3      # 图级重试上限


class DiscoveryState(TypedDict, total=False):
    """图状态：跨节点流转，total=False 允许字段可选（节点只返回变更的字段）。"""
    site_url: str
    homepage: dict          # fetch_homepage 产出
    network_captures: list  # capture_network 产出：XHR/JSON
    exploration: dict       # explorer 产出：候选 API/item 结构
    url_rule: dict | None   # validator 产出：URL 模板 + 证据
    dsl_recipe: dict | None # dsl_writer 产出
    audit_result: dict | None  # auditor 产出：通过/不通过 + 原因
    attempt: int            # 重试轮数
    verdict: str | None     # "dsl" | "failed"
    method_id: int | None   # 最终存入的 crawl_methods.id
    token_used: int         # 累计 token（硬中止用）
    force: bool             # true=覆盖同 domain 旧范式（去重覆盖用，Task 15）
    error: str | None


def supervisor_route(state: DiscoveryState) -> str:
    """supervisor 路由：按 State 决定下一个节点。

    优先级：token 硬中止 > audit 通过 > 重试用尽 > 接力。
    """
    if state.get("token_used", 0) >= TOKEN_BUDGET:
        return "__end__"  # token 超预算，硬中止
    audit = state.get("audit_result")
    if audit and audit.get("passed"):
        return "save_method"  # 审计通过，存方法
    if audit and not audit.get("passed") and state.get("attempt", 0) >= MAX_ATTEMPTS:
        return "__end__"  # 重试用尽，判 failed
    # 接力顺序：explorer → validator → dsl_writer → auditor
    if state.get("dsl_recipe") and not audit:
        return "auditor"
    if state.get("url_rule") and not state.get("dsl_recipe"):
        return "dsl_writer"
    if state.get("exploration") and not state.get("url_rule"):
        return "validator"
    if state.get("network_captures") is not None and not state.get("exploration"):
        return "explorer"
    return "explorer"


def fetch_homepage(state: DiscoveryState) -> DiscoveryState:
    """确定性节点：抓首页 html/links，零 LLM。"""
    from app.discovery.tools import fetch_page
    out = fetch_page.invoke({"url": state["site_url"], "render_js": False})
    return {"homepage": out}


def capture_network(state: DiscoveryState) -> DiscoveryState:
    """确定性节点：Playwright 抓 XHR/JSON，零 LLM。"""
    from app.discovery.tools import capture_network as _cap
    caps = _cap.invoke({"url": state["site_url"]})
    return {"network_captures": caps}


def save_method(state: DiscoveryState) -> DiscoveryState:
    """确定性节点占位：实装在 Task 12（含 DB 写入）。"""
    return {"verdict": "dsl"}


def _make_llm():
    """构造 LangChain ChatModel，指向内部 LLM 网关（OpenAI 兼容）。"""
    from langchain_openai import ChatOpenAI
    s = get_settings()
    return ChatOpenAI(
        base_url=s.llm_base_url, model=s.llm_model,
        api_key=s.llm_api_key, temperature=0,
    )


def explorer(state: DiscoveryState, llm=None) -> DiscoveryState:
    """Explorer worker：ReAct agent 自主调工具探查站点结构/数据源/item。"""
    llm = llm or _make_llm()
    from app.discovery.tools import TOOLS
    from langgraph.prebuilt import create_react_agent
    agent = create_react_agent(llm, TOOLS)  # ReAct：LLM 自主调 fetch_page/capture_network 等
    result = agent.invoke({
        "messages": [("user", f"探查站点 {state['site_url']} 的文章列表数据源和 item 结构")],
    })
    return {"exploration": {"raw": str(result)[:2000]}}  # 截断控 token


def validator(state: DiscoveryState, llm=None) -> DiscoveryState:
    """Validator worker：推断 URL 规律 + 程序验证（技术真伪，非审计）。

    占位实现返回固定 url_rule；后续接入 test_url_template 做真实验证。
    """
    llm = llm or _make_llm()
    return {"url_rule": {"template": "https://x/{item.no}", "evidence": "validated"}}


def dsl_writer(state: DiscoveryState, llm=None) -> DiscoveryState:
    """DslWriter worker：with_structured_output 强制产出合法 DSL Recipe。"""
    llm = llm or _make_llm()
    from app.discovery.dsl import DslRecipe
    structured = llm.with_structured_output(DslRecipe)  # Pydantic 校验，不合法让 LLM 重产
    recipe = structured.invoke(
        f"为 {state['site_url']} 产出 DSL Recipe，url 规律：{state.get('url_rule')}"
    )
    # with_structured_output 真实路径返回 DslRecipe 实例；部分后端/mock 返回 dict —— 统一转 dict
    if isinstance(recipe, DslRecipe):
        recipe_dict = recipe.model_dump()
    else:
        recipe_dict = DslRecipe(**recipe).model_dump()
    return {"dsl_recipe": recipe_dict, "token_used": state.get("token_used", 0) + 1000}


def auditor(state: DiscoveryState, llm=None, test_fn=None) -> DiscoveryState:
    """Auditor worker：独立审计复核 DSL Recipe（结构校验 + 实跑测试达标判定）。

    区别于 validator：validator 验单条 URL 规律真伪，auditor 复核整份 Recipe 合理性/达标。
    """
    llm = llm or _make_llm()
    from app.discovery.dsl import DslRecipe, validate_semantics
    recipe = DslRecipe(**state["dsl_recipe"])
    errors = validate_semantics(recipe)  # 静态审：结构合理性
    test_result = (test_fn or (lambda r: {"discovered_count": 0}))(recipe)  # 动态审：实跑
    passed = not errors and test_result.get("discovered_count", 0) >= 10  # 达标判定
    return {
        "audit_result": {
            "passed": passed, "errors": errors, "test": test_result,
            "suggested_next": "dsl_writer" if not passed else None,
        },
        "attempt": state.get("attempt", 0) + (0 if passed else 1),  # 不通过则 attempt+1
    }
