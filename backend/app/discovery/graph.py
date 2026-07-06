"""SiteDiscoveryGraph：生成命 LangGraph 图——State + supervisor 路由 + 确定性节点 + 4 worker。

supervisor 按 State 决定下一个 worker / 终止 / 转兜底；token 超 TOKEN_BUDGET 硬中止；
attempt 用尽判 failed。worker 在 Task 11 实装，图组装在 Task 12。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import TypedDict
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, END

from app.config import get_settings
from app.discovery.cancel import (
    DiscoveryCancelled,
    activate_run,
    deactivate_run,
    ensure_not_cancelled,
    register_run,
    unregister_run,
)

logger = logging.getLogger(__name__)

TOKEN_BUDGET = 50000  # 生成命 token 硬上限，超即中止
MAX_ATTEMPTS = 3      # 图级重试上限
WORKER_RETRY_LIMIT = 3  # 单个 LLM worker 本地重试上限
STALE_RUN_TIMEOUT_SECONDS = 1800       # running 超过 30 分钟判超时回收（定时巡检用）
STALE_RUN_PATROL_INTERVAL_MINUTES = 5  # 定时巡检间隔
DISCOVERY_LLM_TIMEOUT_SECONDS = 300.0

# node_name → 流程图节点标签 / 日志 stage 标签（前端渲染 [stage] source message · key=value）
_STAGE_LABELS = {
    "fetch_homepage": "抓首页",
    "capture_network": "抓网络请求",
    "supervisor": "路由",
    "explorer": "探查",
    "validator": "验证URL",
    "dsl_writer": "写配方",
    "auditor": "审计",
    "save_method": "存库",
}

# node_name → 日志描述
_STEP_MESSAGES = {
    "fetch_homepage": "抓取首页完成",
    "capture_network": "抓取网络请求完成",
    "supervisor": "路由决策完成",
    "explorer": "站点探查完成",
    "validator": "URL 规律验证完成",
    "dsl_writer": "DSL 配方编写完成",
    "auditor": "配方审计完成",
    "save_method": "配方存储完成",
}


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
    dsl_cycle_attempt: int  # dsl_writer <-> auditor 局部重写轮数
    verdict: str | None     # "dsl" | "failed"
    method_id: int | None   # 最终存入的 crawl_methods.id
    token_used: int         # 累计 token（硬中止用）
    force: bool             # true=覆盖同 domain 旧范式（去重覆盖用，Task 15）
    error: str | None
    name: str | None        # 站点别名（前端选填，不填自动用域名）
    explorer_agent_output: str | None
    explorer_synthesis_output: str | None
    explorer_parse_error: str | None
    explorer_trace_logs: list[dict] | None
    validator_llm_output: str | None
    dsl_writer_llm_output: str | None
    auditor_llm_output: str | None
    audit_input: dict | None
    retry_feedback: dict | None
    dsl_sanitize_warnings: list[str] | None
    run_id: int | None
    log_source: str | None


class ExplorationFetch(BaseModel):
    method: str = "GET"
    transport: str = "httpx"
    impersonate: str | None = None
    stealthy_headers: bool = True
    headers: dict = Field(default_factory=dict)
    query: dict = Field(default_factory=dict)
    json_body: dict | None = None


class ExplorationFormatLocator(BaseModel):
    kind: str = "unknown"
    value: str = ""


class ExplorationFields(BaseModel):
    id: str | None = None
    title: str | None = None
    url: str | None = None
    published_at: str | None = None
    summary: str | None = None
    content: str | None = None


class ExplorationHtmlSelectors(BaseModel):
    item_selector: str | None = None
    link_selector: str | None = None
    title_selector: str | None = None
    date_selector: str | None = None


class ExplorationSampleItem(BaseModel):
    raw: dict | str | None = None
    id: str | None = None
    title: str | None = None
    raw_url: str | None = None
    url: str | None = None
    published_at: str | None = None


class ExplorationUrlCandidate(BaseModel):
    mode: str = "unknown"
    url_field: str | None = None
    id_field: str | None = None
    template: str | None = None
    verification: str | None = None


class ExplorationPagination(BaseModel):
    type: str = "unknown"
    page_param: str | None = None
    size_param: str | None = None
    offset_param: str | None = None
    limit_param: str | None = None
    cursor_param: str | None = None
    next_path: str | None = None
    has_more_path: str | None = None
    start: int = 1
    size: int | None = None
    notes: str = ""


class ExplorationEvidence(BaseModel):
    tool: str
    summary: str


class ExplorationResult(BaseModel):
    source_type: str = "unknown"
    list_url: str | None = None
    fetch: ExplorationFetch = Field(default_factory=ExplorationFetch)
    format_locator: ExplorationFormatLocator = Field(default_factory=ExplorationFormatLocator)
    fields: ExplorationFields = Field(default_factory=ExplorationFields)
    html_selectors: ExplorationHtmlSelectors = Field(default_factory=ExplorationHtmlSelectors)
    sample_items: list[ExplorationSampleItem] = Field(default_factory=list)
    url_candidates: list[ExplorationUrlCandidate] = Field(default_factory=list)
    pagination: ExplorationPagination = Field(default_factory=ExplorationPagination)
    evidence: list[ExplorationEvidence] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    success: bool = False


def supervisor_route(state: DiscoveryState) -> str:
    """supervisor 路由：按 State 决定下一个节点。

    优先级：token 硬中止 > audit 通过 > 重试用尽 > 接力。
    """
    ensure_not_cancelled()
    if state.get("error") or state.get("verdict") == "failed":
        return "__end__"
    if state.get("token_used", 0) >= TOKEN_BUDGET:
        return "__end__"  # token 超预算，硬中止
    audit = state.get("audit_result")
    decision = audit.get("decision") if audit else None
    if audit and (audit.get("passed") or decision == "pass"):
        return "save_method"  # 审计通过，存方法
    if audit and not audit.get("passed") and state.get("attempt", 0) >= MAX_ATTEMPTS:
        return "__end__"  # 重试用尽，判 failed
    if decision == "reexplore":
        return "explorer"
    if decision == "rewrite" and state.get("url_rule") and not state.get("dsl_recipe"):
        return "dsl_writer"
    if audit and not audit.get("passed") and not state.get("exploration") and not state.get("url_rule") and not state.get("dsl_recipe"):
        return "explorer"  # 审计不通过后，重新进入探查
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
    ensure_not_cancelled()
    from app.discovery.tools import fetch_page
    out = _invoke_tool_node(fetch_page, {"url": state["site_url"], "render_js": False})
    return {"homepage": out}


def capture_network(state: DiscoveryState) -> DiscoveryState:
    """确定性节点：Playwright 抓 XHR/JSON，零 LLM。"""
    ensure_not_cancelled()
    from app.discovery.tools import capture_network as _cap
    caps = _invoke_tool_node(_cap, {"url": state["site_url"]})
    return {"network_captures": caps}


def save_method(state: DiscoveryState, db=None) -> DiscoveryState:
    """确定性节点：去重签名 + 存 crawl_methods + crawl_method_domains + 建 sources 记录。

    force=true 且同 domain 已有 → 覆盖更新（保留 method_id/source_id，历史连续）；
    否则新建 crawl_method + 对应 sources(type=discovery) 记录。
    db=None 时自建 SessionLocal（图运行命用）；传入 db 时复用（测试用，不负责关闭）。
    """
    ensure_not_cancelled()
    own_session = db is None
    if own_session:
        from app.db import SessionLocal
        db = SessionLocal()
    try:
        from datetime import datetime, timezone
        from urllib.parse import urlparse
        from app.enums import SourceType, Stream
        from app.models import CrawlMethod, CrawlMethodDomain, Source
        from app.discovery.dsl import DslRecipe
        from app.discovery.signature import compute_signature
        recipe = DslRecipe(**state["dsl_recipe"])
        sig = compute_signature(recipe)
        domain = urlparse(state["site_url"]).netloc
        existing = db.query(CrawlMethodDomain).filter_by(domain=domain).first()
        if existing is not None and state.get("force"):
            # 覆盖：更新现有 method，保留 method_id/source_id，审计历史连续
            m = db.get(CrawlMethod, existing.method_id)
            m.dsl_recipe = recipe.model_dump()
            m.signature = sig
            m.status = "active"
            m.updated_at = datetime.now(timezone.utc)
        elif existing is not None:
            # 同 domain 已有且未 force：保留旧（兜底，正常流程前置检查已拦截）
            m = db.get(CrawlMethod, existing.method_id)
        else:
            # 新建：先建 sources(type=discovery) 记录，再建 crawl_method 关联它
            src = Source(name=state.get("name") or domain, type=SourceType.DISCOVERY.value, url=state["site_url"],
                         main_category="OS跟踪来源", stream=Stream.NEWS.value, enabled=True)
            db.add(src); db.flush()
            m = CrawlMethod(domain=domain, entry_url=state["site_url"], source_id=src.id,
                            dsl_recipe=recipe.model_dump(), signature=sig)
            db.add(m); db.flush()
            db.add(CrawlMethodDomain(domain=domain, method_id=m.id))  # 去重映射
        db.commit()
        return {"verdict": "dsl", "method_id": m.id}
    finally:
        if own_session:
            db.close()


def _make_llm():
    """构造 LangChain ChatModel，指向内部 LLM 网关（OpenAI 兼容）。"""
    from langchain_openai import ChatOpenAI
    s = get_settings()
    return ChatOpenAI(
        base_url=s.llm_base_url, model=s.llm_model,
        api_key=s.llm_api_key, temperature=0, max_retries=4,
    )


def _is_retryable_llm_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in (
        " 500",
        " 502",
        " 503",
        " 504",
        "internalservererror",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "api connection error",
        "timeout",
    ))


EXPLORER_SYSTEM_PROMPT = """# 角色
你是"站点数据源探查员"，专为技术资讯/新闻网站摸清"文章列表是怎么获取的"。

# 职责边界（重要）
你只负责"发现"——找出列表数据源、字段结构、真实样本、以及详情页 URL 的可能规律候选并做初步验证。
你不要正式产出最终的 URL 规律（UrlRule），那是 validator 的职责。你在 url_candidates 里给出候选 + 初步验证结果即可。

# 任务
对给定站点，找出它的文章列表数据源（三者之一）：
- JSON API
- RSS/Atom
- 服务端渲染 HTML（SSR）
以及列表里每条文章的字段结构（id/slug/no、标题、链接、发布时间、摘要/正文片段），
并初步判断详情页 URL 规律。
你的产出是后续 validator 和 dsl_writer 的唯一信息来源，必须准确、具体、有工具调用证据，不能猜。

# 背景
本系统为每个站点生成一份"爬取配方"(DSL)，配方需要知道：
- 列表从哪个 URL 拿、是什么格式（json/rss/html）
- 列表记录在哪个 JSON path、RSS entry，或哪个 HTML selector 下
- 每条文章的字段怎么映射（id / 标题 / 链接 / 时间）
- 详情页 URL 是列表里直接给出，还是需要用 id/slug 拼出来
- 是否有分页，以及分页参数/终止条件是什么

# 可用工具
- fetch_page(url, render_js, transport, impersonate, stealthy_headers)：抓页面，返回 status/title/links/html/feed。
  transport=httpx 为默认；transport=scrapling 会用 Scrapling 浏览器指纹 HTTP 调取，适合 Oracle/Akamai 等普通 httpx/curl 403 但浏览器指纹可通的站点。
- capture_network(url)：用浏览器抓页面加载时的 XHR/Fetch JSON 响应，用于发现 SPA 隐藏 API。
- inspect_item(api_url, method, json_body)：看某个 API 返回的 item 结构。
- probe_html_entries(url, item_selector, link_selector, title_selector, date_selector)：按 selector 真实抽取 HTML 列表样本。
- test_url_template(template, id_field, sample_items)：用真实 id 填模板逐个请求，验证详情页能否打开。
- probe_url_patterns(base_url, id_value)：没头绪时批量试常见 URL pattern（/blog/{id}、/post/{id} 等）。

# 工作方式
1. 先 fetch_page(url, render_js=false) 看页面结构、title、links、html。
1.0 若 fetch_page 返回 403/401/429、首页为空、或候选 RSS/Atom 无法用默认 httpx 调取，
    必须再调用 fetch_page(url, transport="scrapling", impersonate="chrome", stealthy_headers=true)。
    如果 Scrapling 成功拿到 feed 或 HTML，必须在探查结论里保留 transport=scrapling 作为后续 DSL 配方的一部分。
1.1 若首页已直接出现多个同模式文章链接（如 /post/…、/blog/…、/articles/…、/entry/…），
    且这些链接看起来像真实文章详情页，同时还能看到 /page/2/ 之类分页链接，
    这本身就是强 HTML 列表证据。此时应优先判断为服务端渲染 HTML（SSR），
    不要因为没有 JSON API 就误判为"未确认"。
2. 检查页面是否有 RSS/Atom（优先）：
   - HTML 里 <link rel="alternate" type="application/rss+xml"> 或 type="application/atom+xml">
   - 常见路径 /feed、/rss、/atom、/feed.xml、/rss.xml
   若 RSS/Atom 可用，优先记录为 rss/atom 数据源（format_locator.kind=feed_entries）。
3. 若页面链接很少、内容靠 JS 加载、或 HTML 中没有真实文章链接（SPA），必须 capture_network(url)，
   寻找返回文章列表的 JSON XHR/Fetch。
4. 找到候选 JSON API 后，用 inspect_item 看 item 结构，确认：列表 path、id/slug/no 字段、
   title 字段、url/link 字段（或可拼详情页的 id 字段）、published_at/date/time 字段。
5. 若是服务端渲染 HTML，必须从页面中识别四个 selector 并各列 3 条真实样本：
   - item_selector：每条文章卡片/行的 selector
   - link_selector：文章链接 selector
   - title_selector：标题 selector
   - date_selector：发布时间 selector
   - 必要时调用 probe_html_entries(...)，不要只凭肉眼猜 selector。
6. 必须检查列表数据源是否分页：
   - JSON API：query/body 里是否有 page/pageSize/limit/offset/cursor；响应里是否有 total/hasMore/next/pageNo/cursor
   - HTML：分页链接、next 按钮、页码 URL 规律
   - RSS/Atom：通常不分页
   无法确认则 pagination.type=unknown。
7. 若列表 item 里没有直接 URL 但有 id/slug/no，可用 test_url_template 或 probe_url_patterns 做"初步验证"，
   把结果记进 url_candidates（只给候选 + 初步验证，不正式产出 UrlRule）。
8. 控制工具调用次数，信息足够后停止。
9. 如果 fetch_page 已经给出足够的首页文章链接证据，就直接在最终回答里明确写出：
   候选 source_type=html、文章链接模式、分页线索、仍未确认的 selector；
   不要机械继续寻找 JSON API。

# 最终回答规则（最高优先级）
- 你的最终回复不是系统最终结果，后端会再做结构化整理。
- 最终回复请用简洁中文陈述证据，不要输出 JSON，不要代码块。
- 最终回复控制在 8 行以内，尽量用短句/短 bullet，不要写长段落。
- 不要输出表格，不要逐条展开样本，不要复述文章正文或 textContent/content/body。
- 只保留决策必需信息：source_type、list_url、列表 path/selector、关键字段、分页线索、URL 候选、未确认项。
- 每类信息最多给 1~2 个代表性例子，不要罗列全部样本或全部接口。
- 只陈述有工具证据支持的结论；没有证据就明确写"未确认"。
- 优先说明：候选 source_type、候选 list_url、可能的列表 path/selector、字段映射、分页线索、URL 候选、剩余不确定点。

# 质量约束
- 不要凭空猜字段名、selector、URL 模板；凡写的都要能在工具结果里找到依据。
- sample/item 相关结论必须来自真实工具结果，严禁编造。
- 如果 capture_network / inspect_item 已经拿到足够证据，优先复用，不要重复探测。
- 如果没有找到可靠列表数据源，如实说明未确认，不要强行下结论。
"""


def _run_single_explorer_attempt(state: DiscoveryState, *, llm, user_message: str) -> DiscoveryState:
    from app.discovery.tools import TOOLS

    selected_tools = _select_explorer_tools(state, TOOLS)
    synth_raw = ""
    parse_error = None
    deterministic_candidates = _derive_exploration_candidates_from_state(state)
    try:
        result = _run_explorer_tool_loop(
            llm=llm,
            tools=selected_tools,
            user_message=user_message,
            max_rounds=3,
        )
        ensure_not_cancelled()
    except Exception as e:
        if not _is_react_tool_sequence_error(e):
            raise
        parse_error = str(e)
        exploration = _pick_best_deterministic_candidate(deterministic_candidates) or _unknown_exploration()
        return {
            "exploration": ExplorationResult(**exploration).model_dump(),
            "explorer_agent_output": "",
            "explorer_synthesis_output": synth_raw,
            "explorer_parse_error": parse_error,
        }
    agent_output = _extract_final_ai_content(result)
    trace_logs = _extract_explorer_log_events(result)
    deterministic_candidates.extend(_derive_exploration_candidates_from_result(state, result))
    try:
        synth_raw, exploration = _synthesize_exploration(
            site_url=state["site_url"],
            result=result,
            deterministic_candidates=deterministic_candidates,
        )
        matched_hint = _match_deterministic_candidate(exploration, deterministic_candidates)
        exploration = _apply_exploration_constraints(exploration, matched_hint)
    except Exception as e:
        parse_error = str(e)
        exploration = _pick_best_deterministic_candidate(deterministic_candidates) or _unknown_exploration()
    return {
        "exploration": ExplorationResult(**exploration).model_dump(),
        "explorer_agent_output": agent_output,
        "explorer_synthesis_output": synth_raw,
        "explorer_parse_error": parse_error,
        "explorer_trace_logs": trace_logs,
    }


def _build_explorer_retry_feedback(update: DiscoveryState) -> str:
    exploration = update.get("exploration") or {}
    return (
        f"source_type={exploration.get('source_type')} · success={exploration.get('success')} "
        f"· list_url={exploration.get('list_url') or '无'} · parse_error={update.get('explorer_parse_error') or '无'} "
        f"· notes={json.dumps((exploration.get('notes') or [])[:3], ensure_ascii=False)}"
    )


def _append_worker_attempt_log(
    state: DiscoveryState,
    *,
    node_name: str,
    attempt: int,
    status: str,
    detail: str,
) -> None:
    run_id = state.get("run_id")
    if run_id is None:
        return
    from app.run_logs import append_run_log

    stage = _STAGE_LABELS.get(node_name, node_name)
    source_label = state.get("log_source") or state.get("name") or state.get("site_url")
    outcome = "成功" if status == "success" else "失败"
    append_run_log(
        stage,
        f"本地重试第 {attempt} / {WORKER_RETRY_LIMIT} 轮{outcome} · {detail}",
        source=source_label,
        run_id=run_id,
        step=f"{node_name}_local_retry",
        worker_attempt=attempt,
        worker_attempt_limit=WORKER_RETRY_LIMIT,
        level="info" if status == "success" else "warning",
    )


def explorer(state: DiscoveryState, llm=None) -> DiscoveryState:
    """Explorer worker：ReAct 探证据，最终结果统一由程序对象产出。"""
    ensure_not_cancelled()
    llm = llm or _make_llm()
    worker_feedback = None
    last_update: DiscoveryState | None = None
    for attempt in range(1, WORKER_RETRY_LIMIT + 1):
        try:
            update = _run_single_explorer_attempt(
                state,
                llm=llm,
                user_message=_explorer_input_message(state, worker_retry_feedback=worker_feedback),
            )
        except Exception as exc:
            update = {
                "exploration": _unknown_exploration(),
                "explorer_agent_output": "",
                "explorer_synthesis_output": "",
                "explorer_parse_error": str(exc),
            }
        last_update = update
        if (update.get("exploration") or {}).get("success") is True:
            exploration = update.get("exploration") or {}
            _append_worker_attempt_log(
                state,
                node_name="explorer",
                attempt=attempt,
                status="success",
                detail=(
                    f"source_type={exploration.get('source_type')} · "
                    f"list_url={exploration.get('list_url') or '无'}"
                ),
            )
            return update
        worker_feedback = _build_explorer_retry_feedback(update)
        _append_worker_attempt_log(
            state,
            node_name="explorer",
            attempt=attempt,
            status="failed",
            detail=worker_feedback,
        )
    failed = dict(last_update or {})
    failed["verdict"] = "failed"
    failed["error"] = "探查连续失败 3 次，程序无法继续进行"
    return failed


def _select_explorer_tools(state: DiscoveryState, tools: list) -> list:
    """已有高质量网络证据时收紧工具集，避免重复探测。"""
    tool_map = {tool.name: tool for tool in tools}
    caps = state.get("network_captures") or []
    has_high_quality_json_capture = any(
        isinstance(cap.get("parsed_json"), dict)
        and _derive_json_api_exploration(
            site_url=state["site_url"],
            api_url=cap.get("api_url"),
            method=cap.get("method") or "GET",
            json_body=cap.get("request_json_body"),
            payload=cap.get("parsed_json"),
            evidence_tool="capture_network",
            evidence_note="tool selection probe",
            verification="captured_network_response",
        )
        for cap in caps
    )
    if not has_high_quality_json_capture:
        return tools
    preferred = ["fetch_page", "test_url_template", "test_path_join", "probe_url_patterns"]
    return [tool_map[name] for name in preferred if name in tool_map]


def _tool_message_content_to_value(content: object) -> object:
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return content
    return content


def _tool_message_to_value(message: object) -> object:
    return _tool_message_content_to_value(getattr(message, "content", None))


def _tool_node_output_messages(output: object) -> list:
    if isinstance(output, dict):
        messages = output.get("messages")
        if isinstance(messages, list):
            return messages
        return []
    if isinstance(output, list):
        return output
    return []


def _invoke_tool_node(tool, args: dict, *, call_id: str | None = None) -> object:
    """Execute a single LangChain tool through LangGraph ToolNode."""
    from langgraph.prebuilt import ToolNode
    from langgraph.runtime import DEFAULT_RUNTIME

    node = ToolNode([tool], handle_tool_errors=False)
    tool_call_id = call_id or f"{tool.name}_call"
    outputs = node.invoke(
        [
            {
                "name": tool.name,
                "args": args or {},
                "id": tool_call_id,
                "type": "tool_call",
            }
        ],
        runtime=DEFAULT_RUNTIME,
    )
    messages = _tool_node_output_messages(outputs)
    if not messages:
        return None
    return _tool_message_to_value(messages[0])


def _invoke_tool_node_messages(tools: list, tool_calls: list[dict]) -> list:
    """Execute model-requested tool calls through LangGraph ToolNode."""
    from langgraph.prebuilt import ToolNode
    from langgraph.runtime import DEFAULT_RUNTIME

    node = ToolNode(tools, handle_tool_errors=False)
    normalized_calls = [
        {
            "name": call.get("name"),
            "args": call.get("args") or {},
            "id": call.get("id"),
            "type": "tool_call",
        }
        for call in tool_calls
    ]
    return _tool_node_output_messages(node.invoke(normalized_calls, runtime=DEFAULT_RUNTIME))


def _extract_final_ai_content(result: dict) -> str:
    """从 ReAct 结果里取最后一条 AIMessage 的文本内容。"""
    from langchain_core.messages import AIMessage
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content:
            return msg.content
    return ""


def _run_explorer_tool_loop(*, llm, tools: list, user_message: str, max_rounds: int = 6) -> dict:
    """Run explorer's model loop while executing tools through LangGraph ToolNode."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    tool_map = {tool.name: tool for tool in tools}
    bound_llm = llm.bind_tools(tools) if hasattr(llm, "bind_tools") else llm
    full_messages: list = [
        SystemMessage(content=EXPLORER_SYSTEM_PROMPT),
        HumanMessage(content=user_message),
    ]
    model_messages: list = list(full_messages)

    for round_idx in range(max_rounds):
        ensure_not_cancelled()
        last_exc = None
        for retry_idx in range(4):
            try:
                raw_response = bound_llm.invoke(model_messages)
                break
            except Exception as exc:
                last_exc = exc
                if not _is_retryable_llm_error(exc) or retry_idx == 3:
                    raise
                time.sleep(1.0 * (retry_idx + 1))
        else:
            assert last_exc is not None
            raise last_exc
        ai_msg = raw_response if isinstance(raw_response, AIMessage) else AIMessage(content=str(raw_response))
        tool_calls = []
        for tool_idx, call in enumerate(getattr(ai_msg, "tool_calls", None) or []):
            tool_calls.append({
                "id": call.get("id") or f"tool_call_{round_idx}_{tool_idx}",
                "name": call.get("name"),
                "args": call.get("args") or {},
            })
        if tool_calls != list(getattr(ai_msg, "tool_calls", None) or []):
            ai_msg = ai_msg.model_copy(update={"tool_calls": tool_calls})
        full_messages.append(ai_msg)
        model_messages.append(_compact_ai_message_for_model(ai_msg))
        if not tool_calls:
            break
        for call in tool_calls:
            tool_name = call.get("name")
            if tool_name not in tool_map:
                raise ValueError(f"unknown explorer tool: {tool_name}")
        ensure_not_cancelled()
        raw_tool_messages = _invoke_tool_node_messages(tools, tool_calls)
        for raw_tool_msg in raw_tool_messages:
            ensure_not_cancelled()
            tool_name = getattr(raw_tool_msg, "name", None)
            tool_result = _tool_message_to_value(raw_tool_msg)
            summarized = _summarize_explorer_tool_result(tool_name, tool_result)
            content = json.dumps(summarized, ensure_ascii=False)
            tool_msg = ToolMessage(
                content=content,
                tool_call_id=getattr(raw_tool_msg, "tool_call_id", None),
                name=tool_name,
            )
            full_messages.append(tool_msg)
            model_messages.append(tool_msg)
    return {"messages": full_messages}


def _compact_ai_message_for_model(ai_msg):
    content = _truncate_text(getattr(ai_msg, "content", ""), 600)
    tool_calls = getattr(ai_msg, "tool_calls", None) or []
    if tool_calls != list(getattr(ai_msg, "tool_calls", None) or []):
        tool_calls = list(tool_calls)
    return ai_msg.model_copy(update={"content": content, "tool_calls": tool_calls})


def _summarize_explorer_tool_result(tool_name: str, tool_result: object) -> object:
    if not isinstance(tool_result, (dict, list)):
        return _truncate_text(str(tool_result), 600)
    if tool_name == "inspect_item" and isinstance(tool_result, dict):
        sample = tool_result.get("sample")
        return {
            "status": tool_result.get("status"),
            "sample_preview": _compact_explorer_value(sample, depth=3),
        }
    if tool_name == "capture_network" and isinstance(tool_result, list):
        caps = []
        for cap in tool_result[:3]:
            if isinstance(cap, dict):
                caps.append({
                    "api_url": cap.get("api_url"),
                    "method": cap.get("method"),
                    "status": cap.get("status"),
                    "request_json_body": _compact_explorer_value(cap.get("request_json_body"), depth=2),
                    "parsed_json_preview": _compact_explorer_value(cap.get("parsed_json"), depth=2),
                })
        return caps
    if tool_name in {"test_url_template", "test_path_join"} and isinstance(tool_result, dict):
        results = tool_result.get("results") or []
        valid = sum(1 for item in results if isinstance(item, dict) and item.get("is_article_page"))
        return {
            "validated": f"{valid}/{len(results)}",
            "results_preview": _compact_explorer_value(results[:3], depth=2),
        }
    if tool_name == "probe_url_patterns" and isinstance(tool_result, list):
        hits = [item for item in tool_result if isinstance(item, dict) and item.get("is_article_page")]
        return {
            "hits": len(hits),
            "results_preview": _compact_explorer_value(tool_result[:4], depth=2),
        }
    if tool_name == "fetch_page" and isinstance(tool_result, dict):
        return {
            "url": tool_result.get("url"),
            "status": tool_result.get("status"),
            "content_type": tool_result.get("content_type"),
            "transport": tool_result.get("transport"),
            "impersonate": tool_result.get("impersonate"),
            "stealthy_headers": tool_result.get("stealthy_headers"),
            "title": _truncate_text(tool_result.get("title"), 120),
            "links_sample": _compact_explorer_value((tool_result.get("links") or [])[:10], depth=1),
            "feed": _compact_explorer_value(tool_result.get("feed"), depth=2),
            "html_preview": _truncate_text(tool_result.get("html"), 800),
        }
    return _compact_explorer_value(tool_result, depth=3)


def _compact_explorer_value(value: object, *, depth: int = 2) -> object:
    skip_keys = {"textContent", "content", "body", "html", "markdown"}
    if depth <= 0:
        if isinstance(value, dict):
            compact: dict = {}
            for idx, (key, item) in enumerate(value.items()):
                if idx >= 6:
                    break
                if key in skip_keys:
                    continue
                compact[key] = _compact_explorer_value(item, depth=0)
            return compact
        if isinstance(value, list):
            return [_compact_explorer_value(item, depth=0) for item in value[:3]]
        if isinstance(value, str):
            return _truncate_text(value, 120)
        return value
    if isinstance(value, dict):
        compact: dict = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= 8:
                break
            if key in skip_keys:
                continue
            compact[key] = _compact_explorer_value(item, depth=depth - 1)
        return compact
    if isinstance(value, list):
        return [_compact_explorer_value(item, depth=depth - 1) for item in value[:4]]
    if isinstance(value, str):
        return _truncate_text(value, 160)
    return value


def _is_react_tool_sequence_error(exc: Exception) -> bool:
    text = str(exc)
    return (
        "tool_calls" in text
        and "tool_call_id" in text
        and "tool messages" in text
    )


def _derive_exploration_candidates_from_state(state: DiscoveryState) -> list[dict]:
    """从已有确定性证据构造候选池；不在这里预选唯一主候选。"""
    candidates: list[dict] = []
    homepage = state.get("homepage") or {}
    html = homepage.get("html")
    if isinstance(html, str):
        feed_exploration = _derive_feed_exploration_from_html(site_url=state["site_url"], html=html)
        if feed_exploration:
            candidates.append(feed_exploration)
        html_exploration = _derive_html_exploration_from_homepage(
            site_url=state["site_url"],
            html=html,
            links=homepage.get("links") or [],
        )
        if html_exploration:
            candidates.append(html_exploration)
    caps = state.get("network_captures") or []
    for cap in caps:
        parsed = cap.get("parsed_json")
        exploration = _derive_json_api_exploration(
            site_url=state["site_url"],
            api_url=cap.get("api_url"),
            method=cap.get("method") or "GET",
            json_body=cap.get("request_json_body"),
            payload=parsed,
            evidence_tool="capture_network",
            evidence_note="deterministic fallback from captured network response",
            verification="captured_network_response",
        )
        if exploration:
            candidates.append(exploration)
    return candidates


def _derive_feed_exploration_from_html(*, site_url: str, html: str) -> dict | None:
    m = re.search(
        r'<link[^>]+rel=["\'][^"\']*alternate[^"\']*["\'][^>]+type=["\']application/(rss\+xml|atom\+xml)["\'][^>]+href=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if not m:
        return None
    feed_type, href = m.group(1).lower(), m.group(2)
    source_type = "atom" if "atom" in feed_type else "rss"
    feed_url = urljoin(site_url, href)
    return ExplorationResult(
        source_type=source_type,
        list_url=feed_url,
        fetch={"method": "GET", "transport": "httpx", "headers": {}, "query": {}, "json_body": None},
        format_locator={"kind": "feed_entries", "value": "feed.entries"},
        evidence=[{
            "tool": "fetch_page",
            "summary": f"homepage alternate feed detected · href={feed_url}",
        }],
        notes=["deterministic fallback from homepage alternate feed link"],
        success=True,
    ).model_dump()


def _derive_html_exploration_from_homepage(*, site_url: str, html: str, links: list[str]) -> dict | None:
    article_links = _pick_homepage_article_links(site_url=site_url, links=links)
    if len(article_links) < 2:
        return None

    article_pattern = _infer_article_link_pattern(article_links)
    next_link = _pick_homepage_next_page_link(site_url=site_url, links=links)
    pagination: dict[str, object] = {
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
        "notes": "deterministic fallback from homepage article links",
    }
    if next_link:
        pagination["type"] = "html_next"
        pagination["next_path"] = urlparse(next_link).path or next_link

    selector = f'a[href*="{article_pattern}"]' if article_pattern else "a[href]"
    sample_items = [
        {
            "raw": {"url": link},
            "id": None,
            "title": None,
            "raw_url": link,
            "url": link,
            "published_at": None,
        }
        for link in article_links[:5]
    ]
    return ExplorationResult(
        source_type="html",
        list_url=site_url,
        fetch={"method": "GET", "transport": "httpx", "headers": {}, "query": {}, "json_body": None},
        format_locator={"kind": "html_selector", "value": selector},
        fields={
            "id": None,
            "title": None,
            "url": "url",
            "published_at": None,
            "summary": None,
            "content": None,
        },
        html_selectors={
            "item_selector": selector,
            "link_selector": "self",
            "title_selector": "self",
            "date_selector": "time",
        },
        sample_items=sample_items,
        url_candidates=[{
            "mode": "existing_url",
            "url_field": "url",
            "id_field": None,
            "template": None,
            "verification": "homepage_link_direct",
        }],
        pagination=pagination,
        evidence=[{
            "tool": "fetch_page",
            "summary": f"homepage article links detected · sample_items={len(sample_items)}"
                       + (f" · next_page={pagination['next_path']}" if pagination.get("next_path") else ""),
        }],
        notes=["deterministic fallback from homepage article links"],
        success=True,
    ).model_dump()


def _pick_homepage_article_links(*, site_url: str, links: list[str]) -> list[str]:
    site_host = urlparse(site_url).netloc
    article_patterns = (
        "/post/",
        "/posts/",
        "/blog/",
        "/blogs/",
        "/article/",
        "/articles/",
        "/entry/",
        "/entries/",
    )
    article_links: list[str] = []
    seen: set[str] = set()
    for link in links:
        if not isinstance(link, str):
            continue
        normalized = urljoin(site_url, link)
        parsed = urlparse(normalized)
        if parsed.netloc and parsed.netloc != site_host:
            continue
        path = parsed.path.lower()
        if not any(pattern in path for pattern in article_patterns):
            continue
        if path.endswith("/page/") or re.search(r"/page/\d+/?$", path):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        article_links.append(normalized)
    return article_links


def _pick_homepage_next_page_link(*, site_url: str, links: list[str]) -> str | None:
    site_host = urlparse(site_url).netloc
    for link in links:
        if not isinstance(link, str):
            continue
        normalized = urljoin(site_url, link)
        parsed = urlparse(normalized)
        if parsed.netloc and parsed.netloc != site_host:
            continue
        if re.search(r"/page/\d+/?$", parsed.path.lower()):
            return normalized
    return None


def _infer_article_link_pattern(article_links: list[str]) -> str | None:
    for token in ("/post/", "/posts/", "/blog/", "/blogs/", "/article/", "/articles/", "/entry/", "/entries/"):
        if any(token in urlparse(link).path.lower() for link in article_links):
            return token
    return None


def _derive_json_api_exploration(
    *,
    site_url: str,
    api_url: str | None,
    method: str,
    json_body: dict | None,
    payload: object,
    evidence_tool: str,
    evidence_note: str,
    verification: str,
) -> dict | None:
    best = _find_best_json_items_path(payload)
    if not best:
        return None
    path, items = best
    fields = _infer_item_fields(items[0], site_url)
    sample_items = _build_sample_items(items, site_url)
    if not sample_items:
        return None
    return ExplorationResult(
        source_type="json_api",
        list_url=api_url,
        fetch={
            "method": method or "GET",
            "transport": "httpx",
            "headers": {},
            "query": {},
            "json_body": json_body,
        },
        format_locator={"kind": "json_path", "value": path},
        fields=fields,
        html_selectors={
            "item_selector": None, "link_selector": None, "title_selector": None, "date_selector": None,
        },
        sample_items=sample_items,
        url_candidates=[{
            "mode": "existing_url" if fields.get("url") else "unknown",
            "url_field": fields.get("url"),
            "id_field": fields.get("id"),
            "template": None,
            "verification": verification,
        }],
        pagination={
            "type": "unknown", "page_param": None, "size_param": None, "offset_param": None,
            "limit_param": None, "cursor_param": None, "next_path": None, "has_more_path": None,
            "start": 1, "size": None, "notes": evidence_note,
        },
        evidence=[{
            "tool": evidence_tool,
            "summary": f"api_url={api_url} · items_path={path} · sample_items={len(sample_items)}",
        }],
        notes=[evidence_note],
        success=True,
    ).model_dump()


def _derive_exploration_candidates_from_result(state: DiscoveryState, result: dict) -> list[dict]:
    """从 ReAct 工具轨迹里提炼候选池。"""
    candidates: list[dict] = []
    for event in _iter_tool_events(result):
        if event["name"] == "fetch_page":
            payload = event.get("payload") or {}
            feed = payload.get("feed") if isinstance(payload, dict) else None
            if isinstance(feed, dict) and feed.get("item_count"):
                transport = payload.get("transport") or "httpx"
                candidates.append(ExplorationResult(
                    source_type="rss",
                    list_url=payload.get("url") or state["site_url"],
                    fetch={
                        "method": "GET",
                        "transport": transport,
                        "impersonate": payload.get("impersonate"),
                        "stealthy_headers": bool(payload.get("stealthy_headers", True)),
                        "headers": {},
                        "query": {},
                        "json_body": None,
                    },
                    format_locator={"kind": "feed_entries", "value": "feed.entries"},
                    sample_items=[
                        {
                            "raw": item,
                            "id": None,
                            "title": item.get("title"),
                            "raw_url": item.get("url"),
                            "url": item.get("url"),
                            "published_at": item.get("published_at"),
                        }
                        for item in (feed.get("sample_items") or [])[:5]
                        if isinstance(item, dict)
                    ],
                    evidence=[{
                        "tool": "fetch_page",
                        "summary": f"feed fetched via {transport} · item_count={feed.get('item_count')}",
                    }],
                    notes=[f"feed transport confirmed via {transport}"],
                    success=True,
                ).model_dump())
            continue
        if event["name"] != "inspect_item":
            continue
        payload = event.get("payload") or {}
        sample = payload.get("sample")
        exploration = _derive_json_api_exploration(
            site_url=state["site_url"],
            api_url=event["args"].get("api_url"),
            method=event["args"].get("method") or "GET",
            json_body=event["args"].get("json_body"),
            payload=sample,
            evidence_tool="inspect_item",
            evidence_note="deterministic fallback from inspect_item response",
            verification="inspect_item_response",
        )
        if exploration:
            candidates.append(exploration)
    return candidates


def _pick_best_deterministic_candidate(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    ranked = sorted(candidates, key=_deterministic_candidate_score, reverse=True)
    return ranked[0]


def _match_deterministic_candidate(exploration: dict, candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    list_url = exploration.get("list_url")
    source_type = exploration.get("source_type")
    for candidate in candidates:
        if candidate.get("list_url") == list_url and candidate.get("source_type") == source_type:
            return candidate
    if len(candidates) == 1:
        return candidates[0]
    return None


def _iter_tool_events(result: dict) -> list[dict]:
    from langchain_core.messages import AIMessage, ToolMessage

    pending_calls: list[dict] = []
    events: list[dict] = []
    for msg in result.get("messages", []):
        if isinstance(msg, AIMessage):
            for call in getattr(msg, "tool_calls", None) or []:
                pending_calls.append({
                    "id": call.get("id"),
                    "name": call.get("name"),
                    "args": call.get("args") or {},
                })
        elif isinstance(msg, ToolMessage):
            call_id = getattr(msg, "tool_call_id", None)
            call = None
            if call_id:
                for idx, pending in enumerate(pending_calls):
                    if pending.get("id") == call_id:
                        call = pending_calls.pop(idx)
                        break
            if call is None:
                call = pending_calls.pop(0) if pending_calls else {
                    "id": None,
                    "name": getattr(msg, "name", None),
                    "args": {},
                }
            events.append({
                "name": call.get("name") or getattr(msg, "name", None),
                "args": call.get("args") or {},
                "payload": _parse_tool_message_content(getattr(msg, "content", None)),
            })
    return events


def _parse_tool_message_content(content: object) -> object:
    import ast

    if isinstance(content, (dict, list)):
        return content
    if content is None:
        return None
    text = str(content).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return ast.literal_eval(text)
    except Exception:
        return None


def _deterministic_candidate_score(candidate: dict) -> int:
    score = 0
    fields = candidate.get("fields") or {}
    list_url = (candidate.get("list_url") or "").lower()
    fetch = candidate.get("fetch") or {}
    json_body = fetch.get("json_body") if isinstance(fetch, dict) else None
    sample_items = candidate.get("sample_items") or []

    if candidate.get("source_type") in ("rss", "atom"):
        score += 5
    if fields.get("title"):
        score += 4
    if fields.get("url"):
        score += 4
    elif fields.get("id"):
        score += 2
    if fields.get("published_at"):
        score += 2
    if sample_items:
        score += 1

    if any(token in list_url for token in ("tags", "archives", "stats", "count")):
        score -= 6
    if isinstance(json_body, dict) and json_body.get("want") == "archives":
        score -= 6
    if all(not fields.get(key) for key in ("title", "url", "id")):
        score -= 4
    return score


def _find_best_json_items_path(payload: object) -> tuple[str, list[dict]] | None:
    """在 JSON 响应里找最像文章列表的数组路径。"""
    candidates: list[tuple[int, str, list[dict]]] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, list) and node and all(isinstance(item, dict) for item in node[:5]):
            score = _score_item_list(node)
            if score > 0:
                candidates.append((score, path or "obj", node))
            return
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)

    walk(payload, "")
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, path, items = candidates[0]
    return path, items


def _score_item_list(items: list[dict]) -> int:
    first = items[0] if items else {}
    keys = set(first.keys())
    score = 0
    if keys & {"title", "name", "subject"}:
        score += 3
    if keys & {"url", "link", "href", "path"}:
        score += 3
    if keys & {"date", "published_at", "pubDate", "publishTime", "created_at", "time"}:
        score += 2
    if len(items) >= 2:
        score += 1
    return score


def _infer_item_fields(sample: dict, site_url: str) -> dict:
    def pick(*names: str) -> str | None:
        for name in names:
            if name in sample:
                return name
        return None

    return {
        "id": pick("id", "no", "slug", "uuid"),
        "title": pick("title", "name", "subject"),
        "url": pick("url", "link", "href", "path"),
        "published_at": pick("published_at", "date", "pubDate", "publishTime", "created_at", "time"),
        "summary": pick("summary", "description", "desc", "brief"),
        "content": pick("content", "textContent", "body", "text"),
    }


def _build_sample_items(items: list[dict], site_url: str) -> list[dict]:
    sample_items: list[dict] = []
    for item in items[:5]:
        url_field = None
        for candidate in ("url", "link", "href", "path"):
            if candidate in item and item.get(candidate):
                url_field = candidate
                break
        raw_url = item.get(url_field) if url_field else None
        raw_id = item.get("id") or item.get("no") or item.get("slug") or item.get("uuid")
        sample_items.append({
            "id": str(raw_id) if raw_id is not None else None,
            "raw_url": str(raw_url) if raw_url is not None else None,
            "url": urljoin(site_url, str(raw_url)) if raw_url else None,
            "title": item.get("title") or item.get("name") or item.get("subject"),
            "published_at": (
                item.get("published_at")
                or item.get("date")
                or item.get("pubDate")
                or item.get("publishTime")
                or item.get("created_at")
                or item.get("time")
            ),
            "raw": _sanitize_sample_raw(item),
        })
    return sample_items


def _sanitize_sample_raw(item: dict) -> dict:
    """保留 raw 主体，但删除键名里包含 content/text 的字段，避免无关长正文污染上下文。"""
    cloned = json.loads(json.dumps(item, ensure_ascii=False, default=str))
    return {
        key: value
        for key, value in cloned.items()
        if "content" not in key.lower() and "text" not in key.lower()
    }


def _stringify_worker_retry_feedback(feedback: object) -> str:
    if feedback is None:
        return ""
    if isinstance(feedback, str):
        return feedback
    try:
        return json.dumps(feedback, ensure_ascii=False)
    except Exception:
        return str(feedback)


def _append_worker_retry_prompt(prompt: str, heading: str, feedback: object | None) -> str:
    if not feedback:
        return prompt
    return f"{prompt}\n\n# {heading}\n{_stringify_worker_retry_feedback(feedback)}"


def _explorer_input_message(state: DiscoveryState, worker_retry_feedback: object | None = None) -> str:
    """为 explorer 组装上下文，把已抓到的首页/网络证据直接带给 agent。"""
    lines = [f"请探查站点 {state['site_url']} 的文章列表数据源和 item 结构。"]
    retry_feedback = state.get("retry_feedback") or {}
    if retry_feedback:
        lines.append("上一轮审计失败反馈（用于纠偏，但不要机械复用旧结论）：")
        lines.append(json.dumps(retry_feedback, ensure_ascii=False))
    if worker_retry_feedback:
        lines.append("上一轮探查失败反馈（请针对这些失败点纠偏后重试，不要重复同样结论）：")
        lines.append(_stringify_worker_retry_feedback(worker_retry_feedback))
    homepage = state.get("homepage") or {}
    if homepage:
        article_links = _pick_homepage_article_links(
            site_url=state["site_url"],
            links=homepage.get("links") or [],
        )
        next_link = _pick_homepage_next_page_link(
            site_url=state["site_url"],
            links=homepage.get("links") or [],
        )
        lines.append("已知首页证据：")
        lines.append(json.dumps({
            "title": homepage.get("title"),
            "status": homepage.get("status"),
            "links_sample": (homepage.get("links") or [])[:10],
        }, ensure_ascii=False))
        if article_links:
            lines.append("首页文章链接线索：")
            lines.append(json.dumps({
                "article_link_candidates": article_links[:5],
                "article_link_pattern": _infer_article_link_pattern(article_links),
                "next_page_candidate": next_link,
            }, ensure_ascii=False))
    caps = state.get("network_captures") or []
    if caps:
        lines.append("已抓到的网络请求证据（优先复用这些结果，必要时再调用 inspect_item 做二次请求）：")
        summarized = []
        for cap in caps[:6]:
            parsed = cap.get("parsed_json")
            preview = parsed
            if isinstance(parsed, dict):
                preview = dict(list(parsed.items())[:6])
            summarized.append({
                "api_url": cap.get("api_url"),
                "method": cap.get("method"),
                "status": cap.get("status"),
                "request_json_body": cap.get("request_json_body"),
                "parsed_json_preview": preview,
            })
        lines.append(json.dumps(summarized, ensure_ascii=False))
    return "\n".join(lines)


def _truncate_text(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    text = str(text).strip()
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _format_log_json_text(text: str | None) -> str | None:
    """Pretty-print JSON for log panels; fall back to full raw text."""
    if not text:
        return None
    cleaned = str(text).strip()
    if not cleaned:
        return None
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return cleaned
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def _extract_explorer_log_events(result: dict) -> list[dict]:
    """提取 explorer 可见输出轨迹，仅保留 AI 文本和工具结果摘要，不含 thinking。"""
    from langchain_core.messages import AIMessage, ToolMessage

    events: list[dict] = []
    for msg in result.get("messages", []):
        if isinstance(msg, AIMessage):
            content = _truncate_text(getattr(msg, "content", ""), 1200)
            if content:
                events.append({"kind": "ai", "content": content})
        elif isinstance(msg, ToolMessage):
            raw = getattr(msg, "content", None)
            content = _format_log_json_text(raw) or _truncate_text(raw, 1200)
            if content:
                events.append({
                    "kind": "tool",
                    "name": getattr(msg, "name", None) or "tool",
                    "content": content,
                })
    return events[-12:]


def _summarize_explorer_tool_content(tool_name: str | None, raw_content: str | None) -> str:
    """压缩第二阶段 evidence 中的工具输出，只保留 URL 判断所需线索。"""
    parsed = None
    try:
        parsed = json.loads(str(raw_content or "").strip())
    except (json.JSONDecodeError, TypeError):
        return _truncate_text(raw_content, 600)

    if tool_name == "capture_network" and isinstance(parsed, list):
        compact = []
        for item in parsed[:4]:
            if not isinstance(item, dict):
                continue
            compact.append({
                "api_url": item.get("api_url"),
                "method": item.get("method"),
                "status": item.get("status"),
                "request_json_body": item.get("request_json_body"),
            })
        return json.dumps(compact, ensure_ascii=False)

    if tool_name == "inspect_item" and isinstance(parsed, dict):
        sample = parsed.get("sample")
        sample_preview = None
        if isinstance(sample, dict):
            sample_preview = dict(list(sample.items())[:4])
        compact = {
            "status": parsed.get("status"),
            "sample_keys": list(sample.keys())[:8] if isinstance(sample, dict) else None,
            "sample_preview": sample_preview,
        }
        return json.dumps(compact, ensure_ascii=False)

    if tool_name == "fetch_page" and isinstance(parsed, dict):
        compact = {
            "status": parsed.get("status"),
            "content_type": parsed.get("content_type"),
            "transport": parsed.get("transport"),
            "impersonate": parsed.get("impersonate"),
            "stealthy_headers": parsed.get("stealthy_headers"),
            "title": parsed.get("title"),
            "links_count": len(parsed.get("links") or []),
            "feed": parsed.get("feed"),
        }
        return json.dumps(compact, ensure_ascii=False)

    if tool_name == "probe_html_entries" and isinstance(parsed, dict):
        compact = {
            "count": parsed.get("count"),
            "valid_count": parsed.get("valid_count"),
            "looks_like_article_list": parsed.get("looks_like_article_list"),
            "samples": parsed.get("samples", [])[:3],
        }
        return json.dumps(compact, ensure_ascii=False)

    return _truncate_text(json.dumps(parsed, ensure_ascii=False), 600)


def _explorer_evidence_payload(result: dict) -> list[dict]:
    """从 ReAct 轨迹提炼证据，供第二阶段结构化整理使用。"""
    from langchain_core.messages import AIMessage, ToolMessage

    payload: list[dict] = []
    for msg in result.get("messages", []):
        if isinstance(msg, AIMessage):
            entry = {"kind": "ai", "content": _truncate_text(getattr(msg, "content", ""), 320)}
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                entry["tool_calls"] = [
                    {
                        "name": call.get("name"),
                        "args": {
                            key: value
                            for key, value in (call.get("args") or {}).items()
                            if key in {"url", "api_url", "method", "id_field", "template", "base_url", "path_field"}
                        },
                    }
                    for call in tool_calls[:4]
                ]
            payload.append(entry)
        elif isinstance(msg, ToolMessage):
            payload.append({
                "kind": "tool",
                "name": getattr(msg, "name", None),
                "content": _summarize_explorer_tool_content(
                    getattr(msg, "name", None),
                    getattr(msg, "content", ""),
                ),
            })
    return payload[-8:]


def _synthesize_exploration(*, site_url: str, result: dict, deterministic_candidates: list[dict]) -> tuple[str, dict]:
    """第二阶段：根据 ReAct 证据整理结构化 exploration JSON。"""
    from app.llm.client import LlmClient

    evidence = _explorer_evidence_payload(result)
    ensure_not_cancelled()
    prompt = (
        "你是站点探查结果整理器。"
        "请根据给定站点探查证据，输出一个 JSON object。"
        "要求：\n"
        "1. 只输出 JSON object，不要解释，不要 Markdown；\n"
        "2. 所有字符串必须是合法 JSON 字符串；\n"
        "3. 如果证据不足，如实返回 source_type=unknown, success=false；\n"
        "4. 尽量保留证据中已经确认的字段和值。\n"
        "4.1 下面会提供一个程序提取出的候选池，它们只是候选，不是最终答案；由你来选择最像文章列表的那个。\n"
        "4.2 若候选像 tags/archives/count 统计接口，而不是文章列表，不要选它。\n"
        "5. 若 success=true 且 source_type=json_api，必须同时给出：list_url、format_locator.value（json path）、"
        "fields.title、至少 1 条 sample_items，以及 fields.url 或 fields.id 或 sample_items 中的 path/url。\n"
        "6. 若 success=true 且 source_type=rss/atom，必须给出 list_url 且 format_locator.kind=feed_entries。\n"
        "7. 若 success=true 且 source_type=html，必须给出 html_selectors 四项和 sample_items。\n\n"
        "输出 schema：\n"
        "{\n"
        '  "source_type": "json_api | rss | atom | html | unknown",\n'
        '  "list_url": "string or null",\n'
        '  "fetch": {"method": "GET | POST", "transport": "httpx | scrapling", "impersonate": null, "stealthy_headers": true, "headers": {}, "query": {}, "json_body": null},\n'
        '  "format_locator": {"kind": "json_path | feed_entries | html_selector | unknown", "value": "string"},\n'
        '  "fields": {"id": null, "title": null, "url": null, "published_at": null, "summary": null, "content": null},\n'
        '  "html_selectors": {"item_selector": null, "link_selector": null, "title_selector": null, "date_selector": null},\n'
        '  "sample_items": [{"id": null, "title": null, "raw_url": null, "url": null, "published_at": null, "raw": null}],\n'
        '  "url_candidates": [],\n'
        '  "pagination": {"type": "none | page_param | offset_limit | cursor | next_url | html_next | unknown", "page_param": null, "size_param": null, "offset_param": null, "limit_param": null, "cursor_param": null, "next_path": null, "has_more_path": null, "start": 1, "size": null, "notes": ""},\n'
        '  "evidence": [],\n'
        '  "notes": [],\n'
        '  "success": true\n'
        "}\n\n"
        f"站点 URL: {site_url}\n"
        f"程序提取的候选池（供你自行选择，不要机械接受）:\n{json.dumps(deterministic_candidates, ensure_ascii=False)}\n\n"
        f"ReAct 证据轨迹:\n{json.dumps(evidence, ensure_ascii=False)}\n"
    )
    raw = LlmClient().complete(
        prompt,
        temperature=0.0,
        response_format={"type": "json_object"},
        timeout=DISCOVERY_LLM_TIMEOUT_SECONDS,
    )
    parsed = _parse_strict_exploration_output(raw)
    return raw, parsed


def _exploration_value_empty(value: object) -> bool:
    if value in (None, "", [], {}):
        return True
    if isinstance(value, str) and value.strip().lower() == "unknown":
        return True
    return False


def _fill_exploration_gaps(exploration: dict, hint: dict | None) -> dict:
    """从 deterministic hint 补全 synth 遗漏的空字段，不覆盖 synth 已有非空值。"""
    if not hint or not exploration.get("success"):
        return ExplorationResult(**exploration).model_dump()
    out = ExplorationResult(**exploration).model_dump()
    hint_obj = ExplorationResult(**hint).model_dump()
    for key in ("source_type", "list_url"):
        if _exploration_value_empty(out.get(key)) and not _exploration_value_empty(hint_obj.get(key)):
            out[key] = hint_obj[key]
    for key in ("fetch", "format_locator", "fields", "html_selectors", "pagination"):
        merged = dict(out.get(key) or {})
        for sub_key, value in (hint_obj.get(key) or {}).items():
            if _exploration_value_empty(merged.get(sub_key)) and not _exploration_value_empty(value):
                merged[sub_key] = value
        out[key] = merged
    for key in ("sample_items", "url_candidates", "evidence"):
        if not out.get(key) and hint_obj.get(key):
            out[key] = hint_obj[key]
    if not out.get("notes") and hint_obj.get("notes"):
        out["notes"] = hint_obj["notes"]
    return ExplorationResult(**out).model_dump()


def _sample_has_url_clue(sample: dict) -> bool:
    if sample.get("url") or sample.get("id"):
        return True
    raw = sample.get("raw")
    if not isinstance(raw, dict):
        return False
    return bool(raw.get("url") or raw.get("link") or raw.get("href") or raw.get("path") or raw.get("id"))


def _exploration_constraint_errors(exploration: dict) -> list[str]:
    """success=true 时按 source_type 检查关键字段是否齐全。"""
    if not exploration.get("success"):
        return []
    source_type = exploration.get("source_type") or "unknown"
    if source_type == "unknown":
        return ["success=true but source_type=unknown"]

    if source_type == "json_api":
        errors: list[str] = []
        if _exploration_value_empty(exploration.get("list_url")):
            errors.append("missing list_url")
        format_locator = exploration.get("format_locator") or {}
        value = format_locator.get("value")
        if _exploration_value_empty(value) or value == "unknown":
            errors.append("missing format_locator.value")
        fields = exploration.get("fields") or {}
        if _exploration_value_empty(fields.get("title")):
            errors.append("missing fields.title")
        samples = exploration.get("sample_items") or []
        if not samples:
            errors.append("missing sample_items")
        has_url_clue = bool(fields.get("url") or fields.get("id"))
        if not has_url_clue:
            has_url_clue = any(_sample_has_url_clue(s) for s in samples)
        if not has_url_clue:
            errors.append("missing url/path/id clue for validator")
        return errors

    if source_type in ("rss", "atom"):
        errors = []
        if _exploration_value_empty(exploration.get("list_url")):
            errors.append("missing list_url")
        format_locator = exploration.get("format_locator") or {}
        if format_locator.get("kind") != "feed_entries":
            errors.append("format_locator.kind must be feed_entries")
        return errors

    if source_type == "html":
        errors = []
        selectors = exploration.get("html_selectors") or {}
        for key in ("item_selector", "link_selector", "title_selector", "date_selector"):
            if _exploration_value_empty(selectors.get(key)):
                errors.append(f"missing html_selectors.{key}")
        if not exploration.get("sample_items"):
            errors.append("missing sample_items")
        return errors

    return []


def _apply_exploration_constraints(exploration: dict, hint: dict | None) -> dict:
    """补全空字段后做硬性校验；不满足则降 success=false 并记录原因。"""
    filled = _fill_exploration_gaps(exploration, hint)
    errors = _exploration_constraint_errors(filled)
    if not errors:
        return filled
    notes = list(filled.get("notes") or [])
    notes.extend(f"constraint failed: {err}" for err in errors)
    filled["success"] = False
    filled["notes"] = notes
    return ExplorationResult(**filled).model_dump()


def _normalize_exploration_evidence(parsed: dict) -> dict:
    evidence = parsed.get("evidence")
    if not isinstance(evidence, list):
        return parsed
    normalized: list[dict] = []
    for item in evidence:
        if isinstance(item, dict):
            normalized.append(item)
            continue
        text = str(item).strip()
        if not text:
            continue
        tool, sep, summary = text.partition(":")
        if sep:
            normalized.append({"tool": tool.strip(), "summary": summary.strip()})
        else:
            normalized.append({"tool": "unknown", "summary": text})
    parsed["evidence"] = normalized
    return parsed


def _normalize_exploration_sample_items(parsed: dict) -> dict:
    sample_items = parsed.get("sample_items")
    if not isinstance(sample_items, list):
        return parsed
    normalized: list[dict | object] = []
    for item in sample_items:
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        row = dict(item)
        if row.get("id") is not None and not isinstance(row.get("id"), str):
            row["id"] = str(row["id"])
        normalized.append(row)
    parsed["sample_items"] = normalized
    return parsed


def _parse_strict_exploration_output(content: str) -> dict:
    """严格解析 explorer 输出：必须是可直接 json.loads 的单个对象且满足 schema。"""
    if not content:
        raise ValueError("explorer output is empty")
    try:
        parsed = json.loads(content)
    except Exception as e:
        raise ValueError(f"strict exploration json parse failed: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("strict exploration output must be a JSON object")
    parsed = _normalize_exploration_evidence(parsed)
    parsed = _normalize_exploration_sample_items(parsed)
    return ExplorationResult(**parsed).model_dump()


def _unknown_exploration() -> dict:
    return ExplorationResult(source_type="unknown", success=False).model_dump()


def _json_parse_error_message(content: str) -> str:
    """给原始文本生成更直接的 JSON 解析错误信息。"""
    import re
    try:
        json.loads(content)
        return "unknown parse failure"
    except Exception as e:
        direct = f"{type(e).__name__}: {e}"
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
    if m:
        try:
            json.loads(m.group(1))
        except Exception as e:
            return f"{type(e).__name__}: {e}"
    return direct


def _parse_json_or_fallback(content: str) -> dict:
    """解析 LLM 最终回复为 JSON；失败时剥代码块重试，仍失败则回退 unknown。"""
    import re
    if not content:
        return {"source_type": "unknown", "success": False, "raw": ""}
    try:
        return json.loads(content)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return {"source_type": "unknown", "success": False, "raw": content[:2000]}


def _repair_loop_until(loop_action: dict) -> None:
    until = loop_action.get("until")
    if not isinstance(until, dict):
        return
    has_target = any(until.get(key) is not None for key in ("count_of", "var", "path", "exists", "not_exists"))
    if has_target:
        return
    for sub in loop_action.get("body") or []:
        if sub.get("op") == "extract" and sub.get("from"):
            until.update({
                "count_of": None,
                "var": None,
                "path": sub["from"],
                "exists": None,
                "not_exists": None,
                "op": "==",
                "value": [],
            })
            return


def _sanitize_recipe_actions(actions: list[dict], *, path: str = "actions") -> tuple[list[dict], list[str]]:
    sanitized: list[dict] = []
    warnings: list[str] = []
    for idx, action in enumerate(actions):
        action_path = f"{path}[{idx}]"
        current = dict(action)
        if current.get("op") == "fetch":
            if current.get("query") is None:
                current["query"] = {}
                warnings.append(f"{action_path}.fetch.query was null and sanitized to {{}}")
            elif isinstance(current.get("query"), dict):
                current["query"] = {k: str(v) for k, v in current["query"].items()}
        if current.get("op") == "extract" and isinstance(current.get("fields"), dict):
            current["fields"] = {k: v for k, v in current["fields"].items() if v is not None}
        if current.get("op") == "loop":
            if isinstance(current.get("body"), list):
                current["body"], body_warnings = _sanitize_recipe_actions(current["body"], path=f"{action_path}.loop.body")
                warnings.extend(body_warnings)
            if isinstance(current.get("on_each"), list):
                current["on_each"], each_warnings = _sanitize_recipe_actions(current["on_each"], path=f"{action_path}.loop.on_each")
                warnings.extend(each_warnings)
            _repair_loop_until(current)
        sanitized.append(current)
    return sanitized, warnings


def _sanitize_recipe_dict(recipe_dict: dict) -> dict:
    return _sanitize_recipe(recipe_dict)[0]


def _sanitize_recipe(recipe_dict: dict) -> tuple[dict, list[str]]:
    out = dict(recipe_dict)
    if isinstance(out.get("actions"), list):
        out["actions"], warnings = _sanitize_recipe_actions(out["actions"])
    else:
        warnings = []
    return out, warnings


class UrlRule(BaseModel):
    """LLM 推断的 URL 规律：mode + 模板/id 字段/url 字段 + 样本（供程序验证）。"""
    mode: str = "unknown"                       # existing_url | path_join | template | unknown
    template: str | None = None                 # 仅 mode=template 时填，用 {id} 占位
    base_url: str | None = None                 # 仅 mode=path_join 时填，和 path_field 拼接
    path_field: str | None = None               # 仅 mode=path_join 时填，相对路径字段名
    id_field: str | None = None                 # 列表里充当 id 的字段名（原字段名）
    url_field: str | None = None                # 列表里直接给出 URL 的字段名（mode=existing_url）
    sample_items: list[dict] = Field(default_factory=list)  # [{id,url,title,raw}]
    validation_samples: list[dict] = Field(default_factory=list)  # 验证样例 URL/值/状态
    confidence: str = "low"                     # high | medium | low
    reason: str = ""


def _first_sample_id(rule: UrlRule) -> str:
    """取首个样本的 id 值（新 sample shape: {id,...}），供 probe_url_patterns 探测；无则回退 "1"。"""
    if not rule.sample_items:
        return "1"
    val = rule.sample_items[0].get("id")
    return str(val) if val is not None else "1"


VALIDATOR_PROMPT = """# 角色
你是"URL 规律推断员"。

# 职责边界（重要）
你负责"正式产出" UrlRule——基于 explorer 的候选（url_candidates + fields + sample_items），
判定详情页 URL 的来源模式并给出最终模板/字段，再由程序拿真实样本请求验证。
explorer 只给了候选，最终的 UrlRule 由你产出。

# 任务
判断文章详情页 URL 如何获得（四选一）：
1. 列表 item 已经直接给出 URL → mode=existing_url
2. 列表给的是相对路径字段（如 path）→ mode=path_join
3. 需要用 id/slug/no 拼 URL 模板 → mode=template
4. 无法判断 → mode=unknown
你的输出会被程序拿真实样本请求验证，不通过会回退让你重提。

# 背景
- 如果列表里已有 url/link 字段，应直接使用该字段（mode=existing_url），不要多此一举去推模板。
- 如果列表里给的是 path/href 这类相对路径字段，应输出 mode=path_join，填写 base_url 与 path_field。
- 如果列表只有 id/slug/no，则需要推断模板，如 https://x.com/blog/{id}（mode=template）。
- 如果没有把握，不要猜，mode=unknown。

# 输入
- 站点 URL：{site_url}
- 探查结果：{exploration}

# 输出
必须按 UrlRule schema 结构化输出：

{
  "mode": "existing_url | path_join | template | unknown",
  "template": "详情页 URL 模板；仅 mode=template 时填写，必须使用 {id} 占位；否则 null",
  "base_url": "path_join 时的站点根 URL；仅 mode=path_join 时填写；否则 null",
  "path_field": "列表文章里提供相对路径的字段名；仅 mode=path_join 时填写；否则 null",
  "id_field": "列表文章里充当 id 的字段名；仅 mode=template 时填写；否则 null",
  "url_field": "列表文章里直接给出 URL 的字段名；仅 mode=existing_url 时填写；否则 null",
  "sample_items": [
    {
      "id": "真实 id/slug/no；没有则 null",
      "raw_url": "原始链接字段值；可能是相对路径如 /xx/yy，也可能是完整 URL；没有则 null",
      "url": "补全后的可访问 URL；没有则 null",
      "title": "真实标题；没有则 null",
      "raw": "来自 exploration 的原始样本片段"
    }
  ],
  "confidence": "high | medium | low",
  "reason": "为什么这样判断，必须引用 exploration 中的字段/样本/验证结果"
}

已有 URL 字段的例子：
{"mode": "existing_url", "template": null, "base_url": null, "path_field": null, "id_field": null, "url_field": "url", "sample_items": [...], "confidence": "high", "reason": "exploration.fields.url=link，sample_items.raw_url 本身就是完整详情页链接"}

相对路径字段的例子：
{"mode": "path_join", "template": null, "base_url": "https://www.openeuler.org", "path_field": "path", "id_field": null, "url_field": null, "sample_items": [...], "confidence": "high", "reason": "sample_items.raw_url 或 sample_items.raw.path 是相对路径，需与站点根 URL 拼接"}

# 质量约束
- 如果 exploration.fields.url 对应的 sample_items.raw_url 本身就是完整链接，优先 mode=existing_url，不要强推 template。
- 如果 sample_items.raw_url 或 raw 样本里的字段值像 /xx/yy 这种相对路径，优先 mode=path_join，不要伪装成 template + {id}。
- template 必须使用 {id} 占位符，不要写 {item.no}。
- sample_items 必须来自 exploration.sample_items，严禁编造。
- sample_items 数量 3~5 个；不足则给已有数量并说明。
- 没把握就 mode=unknown，不要为了输出模板而猜。
- id_field 必须是 exploration 里真实存在的字段名。
- path_field 必须是 exploration 里真实存在的字段名。
"""


def validator(state: DiscoveryState, llm=None) -> DiscoveryState:
    """Validator worker：LLM 判 URL 来源模式 + test_url_template 程序验证（技术真伪，非审计）。

    mode=existing_url：列表已有 url 字段，直接用，无需模板验证。
    mode=template：test_url_template 拿真实 id 逐个请求验证。
    mode=unknown / 验证失败：probe_url_patterns 批量试常见 pattern 兜底。
    都不中 → evidence="unverified"。
    """
    ensure_not_cancelled()
    from app.discovery.tools import probe_html_entries, test_path_join, test_url_template, probe_url_patterns
    exploration = state.get("exploration") or {}
    if exploration.get("source_type") == "html":
        selectors = exploration.get("html_selectors") or {}
        item_selector = selectors.get("item_selector")
        if item_selector:
            probe = _invoke_tool_node(probe_html_entries, {
                "url": exploration.get("list_url") or state["site_url"],
                "item_selector": item_selector,
                "link_selector": selectors.get("link_selector"),
                "title_selector": selectors.get("title_selector"),
                "date_selector": selectors.get("date_selector"),
            })
            samples = probe.get("samples") or []
            if probe.get("looks_like_article_list"):
                return {"url_rule": {
                    "mode": "existing_url",
                    "url_field": "url",
                    "id_field": None,
                    "evidence": f"html_validated {probe.get('valid_count')}/{probe.get('count')}",
                    "confidence": "high",
                    "validation_samples": samples[:5],
                }, "audit_result": None, "validator_llm_output": json.dumps(probe, ensure_ascii=False)}
    worker_feedback = None
    last_out: DiscoveryState | None = None
    for attempt in range(1, WORKER_RETRY_LIMIT + 1):
        prompt = (VALIDATOR_PROMPT
                  .replace("{site_url}", state["site_url"])
                  .replace("{exploration}", json.dumps(exploration, ensure_ascii=False)))
        prompt = _append_worker_retry_prompt(prompt, "上一轮验证URL失败反馈", worker_feedback)
        try:
            if llm is None:
                from app.llm.client import LlmClient
                ensure_not_cancelled()
                raw = LlmClient().complete(
                    prompt,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    timeout=DISCOVERY_LLM_TIMEOUT_SECONDS,
                )
                rule_obj = UrlRule(**_parse_json_or_fallback(raw))
            else:
                structured = llm.with_structured_output(UrlRule)
                rule = structured.invoke(prompt)
                rule_obj = rule if isinstance(rule, UrlRule) else UrlRule(**rule)
                raw = json.dumps(rule_obj.model_dump(), ensure_ascii=False)
        except Exception as exc:
            last_out = {"url_rule": None, "validator_llm_output": "", "audit_result": None}
            worker_feedback = f"exception={exc}"
            _append_worker_attempt_log(
                state,
                node_name="validator",
                attempt=attempt,
                status="failed",
                detail=worker_feedback,
            )
            continue
        if rule_obj.mode == "template" and rule_obj.id_field == "path" and not rule_obj.path_field:
            rule_obj = rule_obj.model_copy(update={
                "mode": "path_join",
                "base_url": state["site_url"],
                "path_field": "path",
                "template": None,
                "id_field": None,
            })

        if rule_obj.mode == "existing_url" and rule_obj.url_field:
            _append_worker_attempt_log(
                state,
                node_name="validator",
                attempt=attempt,
                status="success",
                detail=f"mode=existing_url · evidence=existing_url · url_field={rule_obj.url_field}",
            )
            return {"url_rule": {
                "mode": "existing_url", "url_field": rule_obj.url_field,
                "id_field": rule_obj.id_field, "evidence": "existing_url",
                "confidence": rule_obj.confidence, "validation_samples": rule_obj.validation_samples,
            }, "audit_result": None, "validator_llm_output": raw}

        if rule_obj.mode == "path_join" and rule_obj.base_url and rule_obj.path_field and rule_obj.sample_items:
            test_out = _invoke_tool_node(test_path_join, {
                "base_url": rule_obj.base_url,
                "path_field": rule_obj.path_field,
                "sample_items": rule_obj.sample_items,
            })
            results = test_out.get("results", [])
            valid = [r for r in results if r.get("is_article_page")]
            if valid:
                _append_worker_attempt_log(
                    state,
                    node_name="validator",
                    attempt=attempt,
                    status="success",
                    detail=f"mode=path_join · evidence=validated {len(valid)}/{len(results)} · path_field={rule_obj.path_field}",
                )
                return {"url_rule": {
                    "mode": "path_join", "base_url": rule_obj.base_url,
                    "path_field": rule_obj.path_field,
                    "evidence": f"validated {len(valid)}/{len(results)}",
                    "confidence": rule_obj.confidence,
                    "validation_samples": results[:5],
                }, "audit_result": None, "validator_llm_output": raw}

        if rule_obj.mode == "template" and rule_obj.template and rule_obj.sample_items:
            test_out = _invoke_tool_node(test_url_template, {
                "template": rule_obj.template,
                "id_field": "id",
                "sample_items": rule_obj.sample_items,
            })
            results = test_out.get("results", [])
            valid = [r for r in results if r.get("is_article_page")]
            if valid:
                _append_worker_attempt_log(
                    state,
                    node_name="validator",
                    attempt=attempt,
                    status="success",
                    detail=f"mode=template · evidence=validated {len(valid)}/{len(results)} · template={rule_obj.template}",
                )
                return {"url_rule": {
                    "mode": "template", "template": rule_obj.template,
                    "id_field": rule_obj.id_field, "evidence": f"validated {len(valid)}/{len(results)}",
                    "confidence": rule_obj.confidence,
                    "validation_samples": results[:5],
                }, "audit_result": None, "validator_llm_output": raw}

        probe_out = _invoke_tool_node(probe_url_patterns, {
            "base_url": state["site_url"],
            "id_value": _first_sample_id(rule_obj),
        })
        hit = next((p for p in probe_out if p.get("is_article_page")), None)
        if hit:
            _append_worker_attempt_log(
                state,
                node_name="validator",
                attempt=attempt,
                status="success",
                detail=f"mode=template · evidence=probed: {hit['pattern']} · template={urljoin(state['site_url'], hit['pattern'])}",
            )
            return {"url_rule": {
                "mode": "template", "template": urljoin(state["site_url"], hit["pattern"]),
                "id_field": rule_obj.id_field, "evidence": f"probed: {hit['pattern']}",
                "confidence": "low",
                "validation_samples": [hit],
            }, "audit_result": None, "validator_llm_output": raw}
        last_out = {"url_rule": {
            "mode": rule_obj.mode, "template": rule_obj.template,
            "base_url": rule_obj.base_url, "path_field": rule_obj.path_field,
            "id_field": rule_obj.id_field, "url_field": rule_obj.url_field,
            "evidence": "unverified", "confidence": rule_obj.confidence,
            "validation_samples": rule_obj.validation_samples,
        }, "audit_result": None, "validator_llm_output": raw}
        worker_feedback = (
            f"mode={rule_obj.mode} · confidence={rule_obj.confidence} "
            f"· evidence=unverified · reason={rule_obj.reason}"
        )
        _append_worker_attempt_log(
            state,
            node_name="validator",
            attempt=attempt,
            status="failed",
            detail=worker_feedback,
        )
    failed = dict(last_out or {})
    failed["verdict"] = "failed"
    failed["error"] = "验证URL连续失败 3 次，程序无法继续进行"
    return failed


DSL_WRITER_PROMPT = """# 角色
你是"爬取配方编写员"。

# 任务
为给定站点编写一份 DSL 爬取配方 Recipe。配方被纯确定性执行器运行后，必须能抓到文章列表，每条至少包含 title 和 url。

# 背景
本系统有一套 DSL，由动作序列组成。配方一旦存库就会反复跑，不能依赖 LLM，不能写死具体文章 id。
执行器按动作顺序执行，fetch 结果存进上下文 last_fetch，extract 从 last_fetch 取记录。

# DSL 动作格式（严格：每个动作必须是 JSON object，必须有 op 字段，字段名固定如下）

1. fetch
{"op": "fetch", "mode": "json | feed | html", "url": "...", "method": "GET | POST", "transport": "httpx | scrapling", "impersonate": "chrome 或 null", "stealthy_headers": true, "headers": {}, "query": {}, "json_body": null, "as": "last_fetch"}

2. goto
{"op": "goto", "url": "..."}

3. wait_for
{"op": "wait_for", "selector": "..."}

4. click
{"op": "click", "selector": "..."}

5. extract
{"op": "extract", "from": "json path | feed.entries | selector:...", "fields": {"title": "...", "url": "...", "published_at": "...或 null", "summary": "...或 null", "content": "...或 null"}, "into": "items", "merge": false}

extract.fields 字段值规则：
- 裸字段名：从 JSON/RSS item 取该字段。
- template: 前缀：拼接 URL，用 {item.字段名} 引用当前条目，如 template:https://x.com/blog/{item.no}。
- attr: 前缀：HTML 取属性，如 attr:href。
- 候选数组：按顺序取第一个非空值，如 ["url", "link"]。

6. set
{"op": "set", "var": "page", "value": 1, "expr": "{{page}} + 1"}
value 和 expr 二选一（expr 用 {{var}} 算术，翻页用）。

7. loop
{"op": "loop", "until": {"kind": "count_of | var | path | exists | not_exists", "target": "items | data.hasMore | next", "op": "== | != | > | >= | < | <=", "value": 0}, "max_iters": 5, "body": [], "on_each": []}
max_iters 必须 1~20。

8. dedup_by
{"op": "dedup_by", "field": "url"}

# 输入
- 站点 URL：{site_url}
- URL 规律：{url_rule}
- 探查结果：{exploration}
- 上一轮审计反馈：{retry_feedback}

# 输出
必须按 DslRecipe schema 结构化输出：

{
  "entry_url": "{site_url}",
  "actions": [],
  "notes": []
}

# 编写规则
1. 根据 exploration.source_type 选 fetch.mode：json_api→json；rss/atom→feed；html→html。
2. 第一阶段必须 fetch 列表数据源：url 用 exploration.list_url；method/query/json_body/headers 用 exploration.fetch。
2.1 如果 exploration.fetch.transport=scrapling，所有对应 fetch 动作必须原样写入 transport、impersonate、stealthy_headers；这是探查阶段验证出的调取配方，不允许丢失。
3. 第二阶段必须 extract：
   - json_api：from 用 exploration.format_locator.value
   - rss/atom：from 用 feed.entries
   - html：from 用 selector:{exploration.html_selectors.item_selector}
4. extract.fields 必须产出 title 和 url：
   - title 从 exploration.fields.title 或 html title_selector 来。
   - 如果 url_rule.mode=existing_url：url 直接用 url_rule.url_field（裸字段名），不要拼模板。
   - 如果 url_rule.mode=path_join：url 用 template:{base_url}/{item.<path_field>}。
     例如 base_url=https://www.openeuler.org、path_field=path，则 DSL 写 template:https://www.openeuler.org/{item.path}。
   - 如果 url_rule.mode=template：url 用 template:，并把 {id} 转成 {item.<id_field>}。
     例如 url_rule.template=https://x.com/blog/{id}、id_field=no，则 DSL 写 template:https://x.com/blog/{item.no}。
   - 如果 HTML 链接来自 link_selector：url 用 attr:href，并确保 extract 的 from（item selector）能定位到含链接的元素。
5. 如果 exploration.pagination.type 不是 none/null/unknown，必须写 set+loop 翻页：
   - loop.max_iters 1~20；每轮 fetch 下一页；extract 用 merge=true 追加 items；
   - 有 has_more_path/next_path 时用它作 until 条件。
6. 最后必须 dedup_by url。
7. source_type=html 且需浏览器交互时才允许 goto/wait_for/click；click/wait_for 必须在 goto 之后。
8. 不要写死具体文章 id。
9. 不要编造字段名、json path、selector、URL——都从 exploration 取。
10. 如果 exploration 不足以写出可运行 Recipe，返回 actions=[]，并在 notes 说明缺什么。

# 质量约束
- extract.fields.url 必须可用：已有 URL 字段（mode=existing_url）或 template 拼接（mode=template）二选一。
- extract.from 必须和 fetch.mode 匹配（json→json path；feed→feed.entries；html→selector: 前缀）。
- loop.max_iters 必须 1~20。
- fetch URL、字段名、selector、json path 必须来自 exploration。
- transport/impersonate/stealthy_headers 必须来自 exploration.fetch；默认 httpx，但一旦探查证据为 scrapling，配方必须写 scrapling。
- 配方执行后应能抓到 ≥1 条带 title 和 url 的文章。
"""


def _fetch_transport_fields(fetch_cfg: dict) -> dict:
    stealthy_headers = fetch_cfg.get("stealthy_headers")
    if stealthy_headers is None:
        stealthy_headers = True
    out = {
        "transport": fetch_cfg.get("transport") or "httpx",
        "stealthy_headers": bool(stealthy_headers),
    }
    if fetch_cfg.get("impersonate"):
        out["impersonate"] = fetch_cfg.get("impersonate")
    return out


def dsl_writer(state: DiscoveryState, llm=None) -> DiscoveryState:
    """DslWriter worker：with_structured_output 强制产出合法 DSL Recipe（喂 site_url+url_rule+exploration）。"""
    ensure_not_cancelled()
    from app.discovery.dsl import DslRecipe

    def finalize_recipe(recipe_dict: dict, *, raw_output: str | None = None) -> DiscoveryState:
        sanitized_recipe, sanitize_warnings = _sanitize_recipe(recipe_dict)
        notes = list(sanitized_recipe.get("notes") or [])
        for warning in sanitize_warnings:
            note = f"sanitize warning: {warning}"
            if note not in notes:
                notes.append(note)
        if notes:
            sanitized_recipe["notes"] = notes
        out: DiscoveryState = {
            "dsl_recipe": DslRecipe(**sanitized_recipe).model_dump(),
            "token_used": state.get("token_used", 0) + 1000,
            "dsl_sanitize_warnings": sanitize_warnings,
            "audit_result": None,
            "retry_feedback": None,
        }
        if raw_output is not None:
            out["dsl_writer_llm_output"] = raw_output
        return out

    exploration = state.get("exploration") or {}
    url_rule = state.get("url_rule") or {}
    retry_feedback = state.get("retry_feedback") or {}
    if exploration.get("source_type") in ("rss", "atom") and exploration.get("list_url"):
        fetch_cfg = exploration.get("fetch") or {}
        recipe_dict = {
            "recipe_type": "dsl",
            "entry_url": state["site_url"],
            "actions": [
                {
                    "op": "fetch",
                    "mode": "feed",
                    "url": exploration.get("list_url"),
                    "method": fetch_cfg.get("method", "GET"),
                    **_fetch_transport_fields(fetch_cfg),
                    "headers": fetch_cfg.get("headers", {}),
                    "query": fetch_cfg.get("query", {}),
                    "json_body": None,
                    "as": "last_fetch",
                },
                {
                    "op": "extract",
                    "from": "feed.entries",
                    "fields": {
                        "title": "title",
                        "url": "link",
                        "published_at": ["published", "updated"],
                        "summary": ["summary", "description"],
                    },
                    "into": "items",
                    "merge": False,
                },
                {"op": "dedup_by", "field": "url"},
            ],
            "notes": [f"deterministic {exploration.get('source_type')} recipe"],
        }
        if (fetch_cfg.get("transport") or "httpx") == "scrapling":
            recipe_dict["notes"].append("scrapling transport recipe preserved from explorer")
        return finalize_recipe(recipe_dict)
    if exploration.get("source_type") == "html":
        selectors = exploration.get("html_selectors") or {}
        item_selector = selectors.get("item_selector")
        if item_selector:
            fetch_cfg = exploration.get("fetch") or {}
            title_selector = selectors.get("title_selector") or "self"
            date_selector = selectors.get("date_selector")
            actions = [
                {
                    "op": "fetch",
                    "mode": "html",
                    "url": exploration.get("list_url") or state["site_url"],
                    "method": "GET",
                    **_fetch_transport_fields(fetch_cfg),
                    "headers": fetch_cfg.get("headers", {}),
                    "query": fetch_cfg.get("query", {}),
                    "json_body": None,
                    "as": "last_fetch",
                },
                {
                    "op": "extract",
                    "from": f"selector:{item_selector}",
                    "fields": {
                        "title": title_selector if title_selector != "self" else "self",
                        "url": "attr:href",
                        **({"published_at": date_selector} if date_selector else {}),
                    },
                    "into": "items",
                    "merge": False,
                },
            ]
            pagination = exploration.get("pagination") or {}
            next_path = pagination.get("next_path") if pagination.get("type") == "html_next" else None
            next_template = _build_html_next_page_template(state["site_url"], next_path) if next_path else None
            if next_template:
                actions.extend([
                    {"op": "set", "var": "page", "value": 2},
                    {
                        "op": "loop",
                        "until": {"count_of": "items", "op": ">=", "value": 50},
                        "max_iters": 8,
                        "body": [
                            {
                                "op": "fetch",
                                "mode": "html",
                                "url": next_template,
                                "method": "GET",
                                **_fetch_transport_fields(fetch_cfg),
                                "headers": fetch_cfg.get("headers", {}),
                                "query": {},
                                "json_body": None,
                                "as": "last_fetch",
                            },
                            {
                                "op": "extract",
                                "from": f"selector:{item_selector}",
                                "fields": {
                                    "title": title_selector if title_selector != "self" else "self",
                                    "url": "attr:href",
                                    **({"published_at": date_selector} if date_selector else {}),
                                },
                                "into": "items",
                                "merge": True,
                            },
                        ],
                        "on_each": [{"op": "set", "var": "page", "expr": "{{page}} + 1"}],
                    },
                ])
            actions.append({"op": "dedup_by", "field": "url"})
            notes = ["deterministic html recipe"]
            if next_template:
                notes.append(f"deterministic html pagination via {next_template}")
            recipe_dict = {
                "recipe_type": "dsl",
                "entry_url": state["site_url"],
                "actions": actions,
                "notes": notes,
            }
            return finalize_recipe(recipe_dict)
    if url_rule.get("mode") == "path_join" and url_rule.get("base_url") and url_rule.get("path_field"):
        fields = dict(exploration.get("fields") or {})
        path_field = url_rule["path_field"]
        base_url = str(url_rule["base_url"]).rstrip("/")
        fields["url"] = f"template:{base_url}/{{item.{path_field}}}"
        pagination = exploration.get("pagination") or {}
        fetch_cfg = exploration.get("fetch") or {}
        format_value = (exploration.get("format_locator") or {}).get("value", "obj.records")
        if pagination.get("type") == "page_param" and pagination.get("page_param"):
            page_param = pagination["page_param"]
            start = pagination.get("start", 1) or 1
            loop_query = dict(fetch_cfg.get("query") or {})
            if page_param in loop_query:
                loop_query[page_param] = f"{{{{{page_param}}}}}"
            loop_json_body = dict(fetch_cfg.get("json_body") or {})
            if page_param in loop_json_body:
                loop_json_body[page_param] = f"{{{{{page_param}}}}}"
            if pagination.get("has_more_path"):
                until_condition = {
                    "path": pagination["has_more_path"],
                    "op": "==",
                    "value": False,
                }
                pagination_note = f"deterministic has_more_path loop via {pagination['has_more_path']}"
            else:
                until_condition = {"path": format_value, "op": "==", "value": []}
                pagination_note = "deterministic empty-page loop fallback"
            actions = [
                {"op": "set", "var": page_param, "value": start},
                {
                    "op": "loop",
                    "until": until_condition,
                    "max_iters": 10,
                    "body": [
                        {
                            "op": "fetch",
                            "mode": "json",
                            "url": exploration.get("list_url") or state["site_url"],
                            "method": fetch_cfg.get("method", "GET"),
                            **_fetch_transport_fields(fetch_cfg),
                            "headers": fetch_cfg.get("headers", {}),
                            "query": loop_query,
                            "json_body": loop_json_body or None,
                            "as": "last_fetch",
                        },
                        {
                            "op": "extract",
                            "from": format_value,
                            "fields": {k: v for k, v in fields.items() if v is not None},
                            "into": "items",
                            "merge": True,
                        },
                    ],
                    "on_each": [{"op": "set", "var": page_param, "expr": f"{{{{{page_param}}}}} + 1"}],
                },
                {"op": "dedup_by", "field": "url"},
            ]
            recipe_dict = {
                "recipe_type": "dsl",
                "entry_url": state["site_url"],
                "actions": actions,
                "notes": ["deterministic path_join recipe", "deterministic page_param loop", pagination_note],
            }
            return finalize_recipe(recipe_dict)
        recipe_dict = {
            "recipe_type": "dsl",
            "entry_url": state["site_url"],
            "actions": [
                {
                    "op": "fetch",
                    "mode": "json",
                    "url": exploration.get("list_url") or state["site_url"],
                    "method": fetch_cfg.get("method", "GET"),
                    **_fetch_transport_fields(fetch_cfg),
                    "headers": fetch_cfg.get("headers", {}),
                    "query": fetch_cfg.get("query", {}),
                    "json_body": fetch_cfg.get("json_body"),
                    "as": "last_fetch",
                },
                {
                    "op": "extract",
                    "from": (exploration.get("format_locator") or {}).get("value", "obj.records"),
                    "fields": {k: v for k, v in fields.items() if v is not None},
                    "into": "items",
                    "merge": False,
                },
                {"op": "dedup_by", "field": "url"},
            ],
            "notes": ["deterministic path_join recipe"],
        }
        return finalize_recipe(recipe_dict)
    prompt = (DSL_WRITER_PROMPT
              .replace("{site_url}", state["site_url"])
              .replace("{url_rule}", json.dumps(url_rule, ensure_ascii=False))
              .replace("{exploration}", json.dumps(exploration, ensure_ascii=False))
              .replace("{retry_feedback}", json.dumps(retry_feedback, ensure_ascii=False)))
    worker_feedback = None
    last_out: DiscoveryState | None = None
    for attempt in range(1, WORKER_RETRY_LIMIT + 1):
        attempt_prompt = _append_worker_retry_prompt(prompt, "上一轮写配方失败反馈", worker_feedback)
        try:
            if llm is None:
                from app.llm.client import LlmClient

                ensure_not_cancelled()
                raw = LlmClient().complete(
                    attempt_prompt,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    timeout=DISCOVERY_LLM_TIMEOUT_SECONDS,
                )
                recipe_dict = _parse_json_or_fallback(raw)
            else:
                structured = llm.with_structured_output(DslRecipe)
                recipe = structured.invoke(attempt_prompt)
                if isinstance(recipe, DslRecipe):
                    recipe_dict = recipe.model_dump()
                else:
                    recipe_dict = recipe
                raw = json.dumps(recipe_dict, ensure_ascii=False)
        except Exception as exc:
            last_out = {"dsl_recipe": None, "dsl_writer_llm_output": "", "audit_result": None}
            worker_feedback = f"exception={exc}"
            _append_worker_attempt_log(
                state,
                node_name="dsl_writer",
                attempt=attempt,
                status="failed",
                detail=worker_feedback,
            )
            continue
        out = finalize_recipe(recipe_dict, raw_output=raw)
        actions = (out.get("dsl_recipe") or {}).get("actions") or []
        if actions:
            _append_worker_attempt_log(
                state,
                node_name="dsl_writer",
                attempt=attempt,
                status="success",
                detail=f"actions={len(actions)} · has_loop={any(a.get('op') == 'loop' for a in actions)}",
            )
            return out
        last_out = out
        worker_feedback = (
            f"actions={len(actions)} · notes={json.dumps(((out.get('dsl_recipe') or {}).get('notes') or [])[:5], ensure_ascii=False)} "
            f"· raw_output_preview={_truncate_text(raw, 300)}"
        )
        _append_worker_attempt_log(
            state,
            node_name="dsl_writer",
            attempt=attempt,
            status="failed",
            detail=worker_feedback,
        )
    failed = dict(last_out or {})
    failed["verdict"] = "failed"
    failed["error"] = "写配方连续失败 3 次，程序无法继续进行"
    return failed


def _build_html_next_page_template(site_url: str, next_path: str | None) -> str | None:
    if not next_path:
        return None
    normalized = urljoin(site_url, next_path)
    parsed = urlparse(normalized)
    path = parsed.path
    if not re.search(r"/page/\d+/?$", path):
        return None
    templated_path = re.sub(r"/page/\d+/?$", "/page/{{page}}/", path)
    rebuilt = parsed._replace(path=templated_path, query="", fragment="")
    return rebuilt.geturl()


class AuditVerdict(BaseModel):
    """LLM 对实跑抓取结果的质量评判。"""
    passed: bool                          # 综合：这份配方值得存吗
    is_real_content: bool                 # 抓到的是真文章，不是反爬/错误/占位/无关页
    has_pagination: bool                  # 实现了翻页抓多页，不是只抓单页
    not_blocked: bool                     # 没被页面限制/反爬挡住
    value_assessment: str = ""            # 一句话价值评估
    issues: list[str] = Field(default_factory=list)  # 发现的问题
    suggested_fix: str | None = None      # 给 dsl_writer 的修改建议（不通过时）


def _coerce_boolish(value: object) -> object:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return value


def _normalize_audit_verdict(parsed: dict) -> dict:
    if not isinstance(parsed, dict):
        return parsed

    for key in ("passed", "is_real_content", "has_pagination", "not_blocked"):
        parsed[key] = _coerce_boolish(parsed.get(key))

    value_assessment = parsed.get("value_assessment")
    if isinstance(value_assessment, list):
        parsed["value_assessment"] = "\n".join(
            str(item).strip() for item in value_assessment if str(item).strip()
        )
    elif value_assessment is not None and not isinstance(value_assessment, str):
        parsed["value_assessment"] = str(value_assessment)

    issues = parsed.get("issues")
    if isinstance(issues, str):
        parsed["issues"] = [issues] if issues.strip() else []
    elif isinstance(issues, list):
        parsed["issues"] = [
            str(item).strip() for item in issues
            if item is not None and str(item).strip()
        ]
    elif issues is None:
        parsed["issues"] = []
    else:
        text = str(issues).strip()
        parsed["issues"] = [text] if text else []

    suggested_fix = parsed.get("suggested_fix")
    if isinstance(suggested_fix, list):
        parsed["suggested_fix"] = "\n".join(
            str(item).strip() for item in suggested_fix if str(item).strip()
        ) or None
    elif suggested_fix is not None and not isinstance(suggested_fix, str):
        text = str(suggested_fix).strip()
        parsed["suggested_fix"] = text or None

    return parsed


_AUDIT_PROMPT = """# 角色
你是"爬取配方审计员"，负责评判一份配方的**实跑抓取结果**有没有价值、是否全面、是否被反爬/页面限制挡住。

# 任务
看程序按这份配方真实抓到的条目，判断以下四件事，综合给出"是否值得把这份配方存下来"：
1. 抓到的是不是真文章——不是反爬验证页、错误页(403/404)、登录页、占位内容、JS 未渲染的空壳、或与该站无关的页面。
2. 抓取是否全面——有没有实现翻页抓多页，还是只抓了单页就停了（看配方里有没有 loop 动作，以及抓到的条数是否像多页累加）。
3. 有没有被页面限制/反爬挡住——条目很少、内容为空、标题异常、或明显被截断/被挡的迹象。
4. 整体有没有抓取价值——值得存进新闻流吗。

# 背景
这份配方会被反复执行来抓这个站点的文章。如果配方只抓单页、抓到反爬页、或抓到一堆无用页面，
存进来的"新闻"就是垃圾。所以审计要看**真实抓到的内容**，不能只看条数够不够。
静态校验（结构合法性）已由程序完成，你专注看实跑结果的质量。

# 输入
- 站点 URL：{site_url}
- 配方摘要（动作序列 + 是否有翻页 loop）：{recipe_summary}
- 静态校验错误（若有）：{errors}
- 实跑统计：抓到 {discovered_count} 条
- 实跑抓到的条目样本（最多 {n} 条，含 title/url/正文片段）：{items_sample}

# 输出（结构化 AuditVerdict）
- passed：综合判断，true=这份配方值得存，false=不通过。
- is_real_content：抓到的是真文章吗（false=反爬页/错误页/占位/无关页面）。
- has_pagination：实现了翻页抓多页吗（false=只抓单页）。依据：配方有 loop 动作且条数像多页累加→true；配方无 loop 或条数明显只够一页→false。
- not_blocked：没被反爬/页面限制挡住吗（false=有被挡迹象）。
- value_assessment：一句话价值评估。
- issues：发现的问题列表（如"只抓到单页，未实现翻页"、"标题疑似反爬验证页"、"正文为空，疑似 JS 未渲染"）。
- suggested_fix：不通过时给配方编写员的修改建议（如"加 loop 翻页直到抓满"、"extract 的 from 选错了"、"换 render_js=true / 加反爬绕过"）。

# 质量约束
- 只看真实抓到的条目判断，不要凭配方结构猜结果。
- 条目标题含"验证/403/access denied/请验证/robot"或正文为空/全是 JS 占位 → is_real_content=false。
- 配方里没有 loop 动作，且条数像单页量（如 ≤20 且无明显分页截断）→ has_pagination=false。
- 条数很少（如 <3）且不像正常分页截断 → 怀疑被限制，not_blocked=false。
- 不通过必须给具体 issues + suggested_fix；通过时 issues 可为空。
- 不要吹毛求疵：抓到多条真文章、有翻页、没被挡 → 通过。
"""


def _run_recipe_for_audit(recipe: DslRecipe) -> dict:
    """实跑配方拿真实产出（生产 auditor 用）；失败返回空产出 + error。"""
    from app.discovery.interpreter import DslExecutionPartialError, DslInterpreter
    try:
        return DslInterpreter().run(recipe, max_items=50)
    except DslExecutionPartialError as e:
        return {"items": e.items, "stats": e.stats, "error": str(e)}
    except Exception as e:
        return {"items": [], "stats": {"discovered_count": 0}, "error": str(e)}


def _build_retry_feedback(*, recipe: DslRecipe, test_result: dict, llm_verdict: dict) -> dict:
    errors = list(llm_verdict.get("issues") or [])
    errors.extend(
        note for note in (recipe.notes or [])
        if isinstance(note, str) and note.startswith("sanitize warning:")
    )
    runtime_error = test_result.get("error")
    if runtime_error:
        errors.append(f"runtime error: {runtime_error}")
    feedback = {
        "issues": errors[:6],
        "suggested_fix": llm_verdict.get("suggested_fix"),
        "last_runtime_error": runtime_error,
        "failed_recipe_summary": _recipe_summary(recipe),
    }
    return feedback


def _recipe_summary(recipe: DslRecipe) -> dict:
    """配方结构摘要给 LLM 看（控 token）：动作序列 + 是否有翻页 loop。"""
    ops = []
    has_loop = False
    for a in recipe.actions:
        if a.op == "loop":
            has_loop = True
            ops.append({"op": "loop", "max_iters": a.max_iters})
        elif a.op == "fetch":
            ops.append({"op": "fetch", "mode": a.mode, "url": a.url})
        elif a.op == "extract":
            ops.append({"op": "extract", "from": a.from_, "fields": a.fields})
        else:
            ops.append({"op": a.op})
    return {"has_loop": has_loop, "actions": ops}


def _llm_audit_quality(llm, site_url: str, recipe: DslRecipe, items: list, errors: list) -> tuple[str, dict]:
    """调 LLM 评判实跑抓取结果的价值/全面性/反爬/翻页，返回可见输出文本与 verdict dict。"""
    sample = _audit_items_sample(items)
    prompt = _AUDIT_PROMPT.format(
        site_url=site_url,
        recipe_summary=json.dumps(_recipe_summary(recipe), ensure_ascii=False),
        errors=json.dumps(errors, ensure_ascii=False),
        items_sample=json.dumps(sample, ensure_ascii=False),
        n=len(sample),
        discovered_count=len(items),
    )
    if llm is None:
        from app.llm.client import LlmClient

        ensure_not_cancelled()
        raw = LlmClient().complete(
            prompt,
            temperature=0.0,
            response_format={"type": "json_object"},
            timeout=DISCOVERY_LLM_TIMEOUT_SECONDS,
        )
        parsed = _normalize_audit_verdict(_parse_json_or_fallback(raw))
        return raw, AuditVerdict(**parsed).model_dump()
    structured = llm.with_structured_output(AuditVerdict)
    verdict = structured.invoke(prompt)
    if isinstance(verdict, AuditVerdict):
        verdict_dict = verdict.model_dump()
    else:
        verdict_dict = AuditVerdict(**_normalize_audit_verdict(verdict)).model_dump()
    return json.dumps(verdict_dict, ensure_ascii=False), verdict_dict


def _audit_items_sample(items: list) -> list[dict]:
    sample = []
    for it in items[:8]:
        s = {"title": it.get("title"), "url": it.get("url")}
        content = it.get("content") or it.get("summary") or ""
        if content:
            s["content_snippet"] = str(content)[:200]
        sample.append(s)
    return sample


def auditor(state: DiscoveryState, llm=None, test_fn=None) -> DiscoveryState:
    """Auditor worker：实跑配方 → LLM 评判抓取价值/全面性/反爬/翻页 → 结合静态校验判通过。

    区别于 validator：validator 验单条 URL 规律真伪，auditor 复核整份 Recipe 的实跑结果质量。
    test_fn: 注入"跑配方返回产出"的函数（测试 mock）；None → 真跑 DslInterpreter（生产）。
    """
    ensure_not_cancelled()
    from app.discovery.dsl import DslRecipe, validate_semantics
    recipe = DslRecipe(**state["dsl_recipe"])
    errors = validate_semantics(recipe)  # 静态审：结构合理性
    sanitize_warnings = list(state.get("dsl_sanitize_warnings") or [])
    if not sanitize_warnings:
        sanitize_warnings = [
            note.removeprefix("sanitize warning: ").strip()
            for note in (recipe.notes or [])
            if isinstance(note, str) and note.startswith("sanitize warning:")
        ]
    errors = [*errors, *[f"dsl sanitize warning: {warning}" for warning in sanitize_warnings]]
    # 动态审：实跑配方拿真实产出（测试可注入 mock，生产真跑）
    test_result = test_fn(recipe) if test_fn is not None else _run_recipe_for_audit(recipe)
    if test_result.get("error"):
        errors = [*errors, f"runtime error: {test_result['error']}"]
    items = test_result.get("items", [])
    discovered_count = test_result.get("stats", {}).get("discovered_count", len(items))
    audit_input = {
        "recipe_summary": _recipe_summary(recipe),
        "errors": errors,
        "dsl_sanitize_warnings": sanitize_warnings,
        "discovered_count": discovered_count,
        "items_sample": _audit_items_sample(items),
    }
    if test_result.get("error"):
        audit_input["runtime_error"] = test_result["error"]
    # LLM 审：看真实抓到的条目，判价值/全面性/反爬/翻页
    auditor_llm_output, llm_verdict = _llm_audit_quality(llm, state["site_url"], recipe, items, errors)
    # 通过 = 无静态错误 + 抓到至少 1 条 + LLM 判值得存
    passed = (not errors) and discovered_count >= 1 and llm_verdict.get("passed", False)
    current_cycle_attempt = int(state.get("dsl_cycle_attempt", 0) or 0)
    if passed:
        decision = "pass"
    elif llm_verdict.get("suggested_fix") or discovered_count >= 1:
        decision = "rewrite"
    else:
        decision = "reexplore"
    next_cycle_attempt = 0 if passed else (current_cycle_attempt + 1 if decision == "rewrite" else 0)
    if decision == "rewrite" and next_cycle_attempt >= 3:
        decision = "reexplore"
        next_cycle_attempt = 0
    out = {
        "audit_result": {
            "passed": passed, "errors": errors, "test": test_result,
            "llm_verdict": llm_verdict,
            "dsl_sanitize_warnings": sanitize_warnings,
            "decision": decision,
            "suggested_next": "dsl_writer" if decision == "rewrite" else ("explorer" if decision == "reexplore" else None),
        },
        "audit_input": audit_input,
        "auditor_llm_output": auditor_llm_output,
        "attempt": state.get("attempt", 0) + (1 if decision == "reexplore" else 0),
        "dsl_cycle_attempt": next_cycle_attempt,
    }
    if not passed:
        if decision == "reexplore":
            out["exploration"] = {}
            out["url_rule"] = None
        else:
            out["exploration"] = state.get("exploration") or {}
            out["url_rule"] = state.get("url_rule")
        out["dsl_recipe"] = None
        out["retry_feedback"] = _build_retry_feedback(
            recipe=recipe,
            test_result=test_result,
            llm_verdict=llm_verdict,
        )
    else:
        out["retry_feedback"] = None
    return out


# --- Task 12: graph assembly + run entrypoint ---

def supervisor_node(state: DiscoveryState) -> DiscoveryState:
    """纯路由节点：不改状态，仅触发 supervisor_route 条件边。"""
    return state


def build_graph(checkpointer=None):
    """组装 StateGraph：确定性节点 + supervisor + 4 worker + 条件路由。

    checkpointer=None 时用 MemorySaver（测试用）；生产传 PostgresSaver 跨进程续跑。
    """
    from langgraph.checkpoint.memory import MemorySaver
    g = StateGraph(DiscoveryState)
    g.add_node("fetch_homepage", fetch_homepage)
    g.add_node("capture_network", capture_network)
    g.add_node("supervisor", supervisor_node)
    g.add_node("explorer", explorer)
    g.add_node("validator", validator)
    g.add_node("dsl_writer", dsl_writer)
    g.add_node("auditor", auditor)
    g.add_node("save_method", save_method)
    g.set_entry_point("fetch_homepage")
    g.add_edge("fetch_homepage", "capture_network")
    g.add_edge("capture_network", "supervisor")
    g.add_conditional_edges("supervisor", supervisor_route)  # 按 supervisor_route 路由
    for w in ["explorer", "validator", "dsl_writer", "auditor"]:
        g.add_edge(w, "supervisor")  # worker 执行完回 supervisor 决定下一步
    g.add_edge("save_method", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


def _to_psycopg_conn_string(database_url: str) -> str:
    """SQLAlchemy DATABASE_URL → psycopg conn info string（剥 +psycopg/+psycopg2 驱动后缀）。"""
    from sqlalchemy.engine import make_url
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError(f"run_discovery 需 Postgres，当前 DATABASE_URL 驱动为 {url.drivername}")
    # hide_password=False：保留真实密码供 psycopg 连接（默认会掩成 ***）
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def start_discovery_run(site_url: str, force: bool = False, name: str | None = None) -> int:
    """异步触发生成命：建 site_discovery_runs 记录 + 后台线程跑 _execute_discovery。

    复用现有 agent_crawl 的 _start_agent_source_run 后台线程模式。返回 run_id 供轮询。
    """
    from app.db import SessionLocal
    from app.models import SiteDiscoveryRun
    s = SessionLocal()
    try:
        run = SiteDiscoveryRun(site_url=site_url, status="running")
        s.add(run); s.commit()
        run_id = run.id
    finally:
        s.close()
    register_run(run_id)
    threading.Thread(
        target=_execute_discovery, args=(run_id, site_url, force, name),
        daemon=True, name=f"discovery-run-{run_id}",
    ).start()
    return run_id


def _step_summary(node_name: str, update: dict) -> dict:
    """从节点的 state update 提取该步产出摘要，供前端节点详情卡展示。"""
    if node_name == "fetch_homepage":
        h = update.get("homepage") or {}
        title = (h.get("title") or "")[:80]
        return {"status": h.get("status"), "title": title,
                "links": len(h.get("links") or [])}
    if node_name == "capture_network":
        caps = update.get("network_captures") or []
        api_urls = [c.get("api_url", "") for c in caps[:5]]
        return {"json_apis": len(caps),
                "sample_urls": ", ".join(api_urls) if api_urls else "无"}
    if node_name == "explorer":
        e = update.get("exploration") or {}
        return {
            "source_type": e.get("source_type"),
            "list_url": e.get("list_url"),
            "success": e.get("success"),
            "agent_output_preview": _truncate_text(update.get("explorer_agent_output"), 180),
            "synthesis_preview": _truncate_text(update.get("explorer_synthesis_output"), 180),
            "parse_error": update.get("explorer_parse_error"),
        }
    if node_name == "validator":
        u = update.get("url_rule") or {}
        return {
            "mode": u.get("mode"),
            "template": u.get("template"),
            "base_url": u.get("base_url"),
            "path_field": u.get("path_field"),
            "id_field": u.get("id_field"),
            "evidence": u.get("evidence"),
            "validation_samples": u.get("validation_samples"),
        }
    if node_name == "dsl_writer":
        r = update.get("dsl_recipe") or {}
        actions = r.get("actions") or []
        return {"actions": len(actions), "has_loop": any(a.get("op") == "loop" for a in actions)}
    if node_name == "auditor":
        a = update.get("audit_result") or {}
        lv = a.get("llm_verdict") or {}
        return {
            "passed": a.get("passed"),
            "decision": a.get("decision"),
            "issues": lv.get("issues"),
            "attempt": update.get("attempt"),
            "dsl_cycle_attempt": update.get("dsl_cycle_attempt"),
        }
    return {}


def _display_step_name(node_name: str) -> str:
    """给日志 detail 里的 next= 使用可读节点名。"""
    return _STAGE_LABELS.get(node_name, node_name)


def _step_log_detail(node_name: str, update: dict, state: DiscoveryState | None = None) -> str:
    """从节点产出提取关键信息，拼成日志尾部详情（· key=value 格式）。"""
    if node_name == "fetch_homepage":
        h = update.get("homepage") or {}
        status = h.get("status", "?")
        title = (h.get("title") or "")[:60]
        n_links = len(h.get("links") or [])
        return f" · status={status} · title={title} · links={n_links}"
    if node_name == "capture_network":
        caps = update.get("network_captures") or []
        sample = ", ".join((c.get("api_url") or "")[:80] for c in caps[:3] if c.get("api_url"))
        return f" · 捕获 {len(caps)} 个 JSON API · sample={sample or '无'}"
    if node_name == "supervisor":
        next_step = supervisor_route(state or DiscoveryState())
        return f" · next={_display_step_name(next_step)}"
    if node_name == "explorer":
        e = update.get("exploration") or {}
        st = e.get("source_type", "?")
        status = e.get("success")
        list_url = _truncate_text(e.get("list_url"), 120) or "无"
        return f" · source_type={st} · success={status} · list_url={list_url}"
    if node_name == "validator":
        u = update.get("url_rule") or {}
        template = _truncate_text(u.get("template"), 120) or "无"
        url_field = u.get("url_field") or "无"
        path_field = u.get("path_field") or "无"
        id_field = u.get("id_field") or "无"
        samples = u.get("validation_samples") or []
        sample_values = ", ".join(str(s.get("sample_value") or s.get("url") or "") for s in samples[:2] if (s.get("sample_value") or s.get("url")))
        return (
            f" · mode={u.get('mode', '?')} · evidence={u.get('evidence', '?')}"
            f" · url_field={url_field} · path_field={path_field} · id_field={id_field}"
            f" · template={template} · sample_values={sample_values or '无'}"
        )
    if node_name == "dsl_writer":
        r = update.get("dsl_recipe") or {}
        actions = r.get("actions") or []
        has_loop = any(a.get("op") == "loop" for a in actions)
        ops = " > ".join(str(a.get("op")) for a in actions[:6] if a.get("op"))
        return f" · actions={len(actions)} · has_loop={has_loop} · ops={ops or '无'}"
    if node_name == "auditor":
        a = update.get("audit_result") or {}
        passed = a.get("passed")
        decision = a.get("decision") or "?"
        issues = a.get("llm_verdict", {}).get("issues") or []
        issues_text = "; ".join(str(i) for i in issues[:3]) or "无"
        cycle_attempt = update.get("dsl_cycle_attempt")
        return (
            f" · passed={passed} · decision={decision}"
            f" · dsl_cycle_attempt={cycle_attempt if cycle_attempt is not None else '无'}"
            f" · attempt={update.get('attempt')} · issues={issues_text}"
        )
    return ""


def _execute_discovery(run_id: int, site_url: str, force: bool, name: str | None = None) -> None:
    """后台线程执行核心：建图（PostgresSaver）+ stream 逐节点跑 + 实时更新 node_trace。

    进程崩了可从 PostgresSaver checkpoint 跨进程续跑（thread_id 关联 run_id）。
    用 g.stream(stream_mode="updates") 逐节点产出 → 实时写 node_trace 到 DB 供前端轮询 + log。
    日志复用 append_run_log（与 agent_crawl 同套格式：[stage] source message · key=value）。
    """
    from datetime import datetime, timezone
    from langgraph.checkpoint.postgres import PostgresSaver
    from app.db import SessionLocal
    from app.models import SiteDiscoveryRun
    from app.run_logs import append_run_log
    s = get_settings()
    source_label = name or site_url
    token = activate_run(run_id)
    with PostgresSaver.from_conn_string(_to_psycopg_conn_string(s.database_url)) as checkpointer:
        checkpointer.setup()  # 自动建 checkpoint 表
        g = build_graph(checkpointer=checkpointer)
        db_sess = SessionLocal()
        try:
            config = {"configurable": {"thread_id": f"discovery-{run_id}"}}
            initial = {
                "site_url": site_url,
                "attempt": 0,
                "token_used": 0,
                "force": force,
                "name": name,
                "run_id": run_id,
                "log_source": source_label,
            }
            node_trace: list = []
            current_state: DiscoveryState = dict(initial)
            append_run_log("任务", "Discovery 探查开始", source=source_label,
                           run_id=run_id, url=site_url, force=force)
            # 逐节点 stream → 实时更新 node_trace 供前端轮询 + log 输出
            for chunk in g.stream(initial, config=config, stream_mode="updates"):
                ensure_not_cancelled()
                for node_name, update in chunk.items():
                    current_state.update(update or {})
                    entry = {"step": node_name, "status": "done",
                             "ts": datetime.now(timezone.utc).isoformat(),
                             "summary": _step_summary(node_name, update or {})}
                    node_trace.append(entry)
                    stage = _STAGE_LABELS.get(node_name, node_name)
                    base_msg = _STEP_MESSAGES.get(node_name, f"步骤 {node_name} 完成")
                    detail = _step_log_detail(node_name, update or {}, current_state)
                    append_run_log(stage, base_msg + detail, source=source_label,
                                   run_id=run_id, step=node_name, trace_count=len(node_trace))
                    if node_name == "explorer":
                        _append_explorer_generation_logs(
                            append_run_log=append_run_log,
                            run_id=run_id,
                            source_label=source_label,
                            update=update or {},
                        )
                    if node_name == "validator":
                        _append_validator_logs(
                            append_run_log=append_run_log,
                            run_id=run_id,
                            source_label=source_label,
                            update=update or {},
                        )
                    if node_name == "dsl_writer":
                        _append_dsl_writer_logs(
                            append_run_log=append_run_log,
                            run_id=run_id,
                            source_label=source_label,
                            update=update or {},
                        )
                    if node_name == "auditor":
                        _append_auditor_logs(
                            append_run_log=append_run_log,
                            run_id=run_id,
                            source_label=source_label,
                            update=update or {},
                        )
                    logger.info("discovery run %s: step=%s done (%d steps so far)",
                                run_id, node_name, len(node_trace))
                    # 实时写 DB 供前端轮询 GET /discovery/runs/{id}
                    r = db_sess.get(SiteDiscoveryRun, run_id)
                    r.node_trace = list(node_trace)
                    db_sess.commit()
            # 取最终状态
            state_snapshot = g.get_state(config)
            final = state_snapshot.values if state_snapshot else {}
            run = db_sess.get(SiteDiscoveryRun, run_id)
            run.status = "completed" if final.get("verdict") == "dsl" else "failed"
            run.resulting_method_id = final.get("method_id")
            run.llm_token_usage = final.get("token_used", 0)
            run.node_trace = node_trace  # 最终完整 trace
            run.ended_at = datetime.now(timezone.utc)
            if final.get("error"):
                run.error_message = final["error"]
            db_sess.commit()
            append_run_log("任务", f"Discovery 探查结束 · verdict={final.get('verdict')}",
                           source=source_label, run_id=run_id,
                           status=run.status, steps=len(node_trace),
                           token_used=final.get("token_used", 0))
            logger.info("discovery run %s: finished, verdict=%s, %d steps traced",
                        run_id, final.get("verdict"), len(node_trace))
        except DiscoveryCancelled as e:
            db_sess.rollback()
            run = db_sess.get(SiteDiscoveryRun, run_id)
            if run:
                run.status = "cancelled"
                run.error_message = str(e)
                run.ended_at = datetime.now(timezone.utc)
                db_sess.commit()
            append_run_log("任务", f"Discovery 已取消 · {e}", source=source_label,
                           run_id=run_id, level="warning")
            logger.info("discovery run %s: cancelled", run_id)
        except Exception as e:
            # 兜底：图级异常标 failed（节点级异常已在 supervisor 路由处理）
            db_sess.rollback()
            run = db_sess.get(SiteDiscoveryRun, run_id)
            if run and run.status == "running":
                run.status = "failed"; run.error_message = str(e)
                run.ended_at = datetime.now(timezone.utc)
                db_sess.commit()
            append_run_log("任务", f"Discovery 探查失败 · {e}", source=source_label,
                           run_id=run_id, level="error")
            logger.exception("discovery run %s: failed with exception", run_id)
        finally:
            db_sess.close()
            deactivate_run(token)
            unregister_run(run_id)


def _append_explorer_generation_logs(*, append_run_log, run_id: int, source_label: str, update: dict) -> None:
    """记录 explorer 失败信息；不在日志面板中展开 LLM 原始输出。"""
    err = update.get("explorer_parse_error")
    if err:
        append_run_log(
            "探查",
            f"explorer 整理失败 · {err}",
            source=source_label,
            run_id=run_id,
            level="warning",
            step="explorer_parse_error",
        )


def _append_dsl_writer_logs(*, append_run_log, run_id: int, source_label: str, update: dict) -> None:
    """把 dsl_writer 的完整 recipe 写入日志面板，便于直接排查配方内容。"""
    recipe = update.get("dsl_recipe")
    warnings = update.get("dsl_sanitize_warnings") or []
    if warnings:
        append_run_log(
            "写配方",
            f"写配方告警 · {json.dumps(warnings, ensure_ascii=False)}",
            source=source_label,
            run_id=run_id,
            step="dsl_writer_warning",
            level="warning",
        )
    if not recipe:
        return
    append_run_log(
        "写配方",
        f"DSL 全量输出 · {json.dumps(recipe, ensure_ascii=False, indent=2)}",
        source=source_label,
        run_id=run_id,
        step="dsl_writer_recipe",
    )


def _append_validator_logs(*, append_run_log, run_id: int, source_label: str, update: dict) -> None:
    """把 validator 的完整规则与验证样例写入日志。"""
    rule = update.get("url_rule")
    if not rule:
        return
    append_run_log(
        "验证URL",
        f"URL 规律全量输出 · {json.dumps(rule, ensure_ascii=False, indent=2)}",
        source=source_label,
        run_id=run_id,
        step="validator_rule",
    )


def _append_auditor_logs(*, append_run_log, run_id: int, source_label: str, update: dict) -> None:
    """把 auditor 简要结论写入日志。"""
    audit_result = update.get("audit_result") or {}
    decision = audit_result.get("decision")
    if decision:
        cycle_attempt = int(update.get("dsl_cycle_attempt", 0) or 0)
        if decision == "rewrite":
            msg = f"局部循环决策 · 继续写配方 · 第 {cycle_attempt} / 3 轮"
        elif decision == "reexplore":
            msg = "局部循环决策 · 返回探查 · 写配方局部循环已结束"
        else:
            msg = "局部循环决策 · 审计通过 · 准备存库"
        append_run_log(
            "审计",
            msg,
            source=source_label,
            run_id=run_id,
            step="auditor_decision",
            level="warning" if decision != "pass" else "info",
        )


def run_discovery(site_url: str, force: bool = False, name: str | None = None) -> dict:
    """同步入口（测试/同步场景用）：建记录 + 同步跑 _execute_discovery，返回最终结果摘要。"""
    from app.db import SessionLocal
    from app.models import SiteDiscoveryRun
    s = SessionLocal()
    try:
        run = SiteDiscoveryRun(site_url=site_url, status="running")
        s.add(run); s.commit(); run_id = run.id
    finally:
        s.close()
    _execute_discovery(run_id, site_url, force, name)
    s = SessionLocal()
    try:
        run = s.get(SiteDiscoveryRun, run_id)
        return {"verdict": "dsl" if run.status == "completed" else "failed",
                "method_id": run.resulting_method_id, "run_id": run_id}
    finally:
        s.close()


def check_existing_method(site_url: str, db=None) -> dict | None:
    """按 domain 查 crawl_method_domains，命中返回已有范式摘要，否则 None。

    去重粒度=domain（用户感知是"这个网站"），signature 同形去重留作 save_method 内部。
    db=None 时自建 SessionLocal；传入 db 时复用（路由层注入请求 session）。
    """
    own_session = db is None
    if own_session:
        from app.db import SessionLocal
        db = SessionLocal()
    try:
        from urllib.parse import urlparse
        from app.models import CrawlMethod, CrawlMethodDomain
        domain = urlparse(site_url).netloc
        mapping = db.query(CrawlMethodDomain).filter_by(domain=domain).first()
        if mapping is None:
            return None
        m = db.get(CrawlMethod, mapping.method_id)
        return {
            "method_id": m.id, "domain": m.domain, "signature": m.signature,
            "dsl_recipe": m.dsl_recipe,
            "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None,
            "last_run_status": m.last_run_status,
        }
    finally:
        if own_session:
            db.close()


def reclaim_stale_runs(older_than_seconds: int | None = None, db=None) -> int:
    """回收遗留 running 的 site_discovery_runs。

    older_than_seconds=None：回收所有 running（启动用——本进程无对应线程，全是孤儿）。
    older_than_seconds=N：只回收 started_at 早于 now-N 的 running（定时巡检用——
      活着的长 run 不会被误杀，只有卡了 N 秒以上的才判超时回收）。
    已有 error_message 不覆盖。db=None 自建 SessionLocal；传入则复用（测试用）。
    """
    own_session = db is None
    if own_session:
        from app.db import SessionLocal
        db = SessionLocal()
    try:
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select
        from app.models import SiteDiscoveryRun
        query = select(SiteDiscoveryRun).where(SiteDiscoveryRun.status == "running")
        if older_than_seconds is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
            query = query.where(SiteDiscoveryRun.started_at < cutoff)
        stale = db.scalars(query).all()
        now = datetime.now(timezone.utc)
        if older_than_seconds is None:
            msg = "进程重启时回收：run 未正常结束（遗留 running）"
        else:
            msg = f"定时巡检回收：run 运行超过 {older_than_seconds}s 未完成，判超时"
        for run in stale:
            run.status = "failed"
            run.ended_at = now
            if not run.error_message:
                run.error_message = msg
        db.commit()
        return len(stale)
    finally:
        if own_session:
            db.close()
