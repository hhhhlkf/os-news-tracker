"""SiteDiscoveryGraph：生成命 LangGraph 图——State + supervisor 路由 + 确定性节点 + 4 worker。

supervisor 按 State 决定下一个 worker / 终止 / 转兜底；token 超 TOKEN_BUDGET 硬中止；
attempt 用尽判 failed。worker 在 Task 11 实装，图组装在 Task 12。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TypedDict
from urllib.parse import urljoin

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
STALE_RUN_TIMEOUT_SECONDS = 1800       # running 超过 30 分钟判超时回收（定时巡检用）
STALE_RUN_PATROL_INTERVAL_MINUTES = 5  # 定时巡检间隔

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
    verdict: str | None     # "dsl" | "failed"
    method_id: int | None   # 最终存入的 crawl_methods.id
    token_used: int         # 累计 token（硬中止用）
    force: bool             # true=覆盖同 domain 旧范式（去重覆盖用，Task 15）
    error: str | None
    name: str | None        # 站点别名（前端选填，不填自动用域名）
    explorer_agent_output: str | None
    explorer_synthesis_output: str | None
    explorer_parse_error: str | None


class ExplorationFetch(BaseModel):
    method: str = "GET"
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
    ensure_not_cancelled()
    from app.discovery.tools import fetch_page
    out = fetch_page.invoke({"url": state["site_url"], "render_js": False})
    return {"homepage": out}


def capture_network(state: DiscoveryState) -> DiscoveryState:
    """确定性节点：Playwright 抓 XHR/JSON，零 LLM。"""
    ensure_not_cancelled()
    from app.discovery.tools import capture_network as _cap
    caps = _cap.invoke({"url": state["site_url"]})
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
        api_key=s.llm_api_key, temperature=0,
    )


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
- fetch_page(url, render_js)：抓页面，返回 status/title/links/html。render_js=true 用浏览器。
- capture_network(url)：用浏览器抓页面加载时的 XHR/Fetch JSON 响应，用于发现 SPA 隐藏 API。
- inspect_item(api_url, method, json_body)：看某个 API 返回的 item 结构。
- test_url_template(template, id_field, sample_items)：用真实 id 填模板逐个请求，验证详情页能否打开。
- probe_url_patterns(base_url, id_value)：没头绪时批量试常见 URL pattern（/blog/{id}、/post/{id} 等）。

# 工作方式
1. 先 fetch_page(url, render_js=false) 看页面结构、title、links、html。
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
6. 必须检查列表数据源是否分页：
   - JSON API：query/body 里是否有 page/pageSize/limit/offset/cursor；响应里是否有 total/hasMore/next/pageNo/cursor
   - HTML：分页链接、next 按钮、页码 URL 规律
   - RSS/Atom：通常不分页
   无法确认则 pagination.type=unknown。
7. 若列表 item 里没有直接 URL 但有 id/slug/no，可用 test_url_template 或 probe_url_patterns 做"初步验证"，
   把结果记进 url_candidates（只给候选 + 初步验证，不正式产出 UrlRule）。
8. 控制工具调用次数，信息足够后停止。

# 最终回答规则（最高优先级）
- 你的最终回复不是系统最终结果，后端会再做结构化整理。
- 最终回复请用简洁中文陈述证据，不要输出 JSON，不要代码块。
- 只陈述有工具证据支持的结论；没有证据就明确写"未确认"。
- 优先说明：候选 source_type、候选 list_url、可能的列表 path/selector、字段映射、分页线索、URL 候选、剩余不确定点。

# 质量约束
- 不要凭空猜字段名、selector、URL 模板；凡写的都要能在工具结果里找到依据。
- sample/item 相关结论必须来自真实工具结果，严禁编造。
- 如果 capture_network / inspect_item 已经拿到足够证据，优先复用，不要重复探测。
- 如果没有找到可靠列表数据源，如实说明未确认，不要强行下结论。
"""


def explorer(state: DiscoveryState, llm=None) -> DiscoveryState:
    """Explorer worker：ReAct 探证据，最终结果统一由程序对象产出。"""
    ensure_not_cancelled()
    llm = llm or _make_llm()
    from app.discovery.tools import TOOLS
    from langgraph.prebuilt import create_react_agent
    agent = create_react_agent(llm, TOOLS, prompt=EXPLORER_SYSTEM_PROMPT)
    result = agent.invoke({
        "messages": [("user", _explorer_input_message(state))],
    })
    ensure_not_cancelled()
    agent_output = _extract_final_ai_content(result)
    synth_raw = ""
    parse_error = None
    deterministic_exploration = (
        _derive_exploration_from_state(state)
        or _derive_exploration_from_result(state, result)
    )
    try:
        synth_raw, exploration = _synthesize_exploration(
            site_url=state["site_url"],
            result=result,
            deterministic_exploration=deterministic_exploration,
        )
        exploration = _merge_exploration(deterministic_exploration, exploration)
    except Exception as e:
        parse_error = str(e)
        exploration = deterministic_exploration or _unknown_exploration()
    return {
        "exploration": ExplorationResult(**exploration).model_dump(),
        "explorer_agent_output": agent_output,
        "explorer_synthesis_output": synth_raw,
        "explorer_parse_error": parse_error,
    }


def _extract_final_ai_content(result: dict) -> str:
    """从 ReAct 结果里取最后一条 AIMessage 的文本内容。"""
    from langchain_core.messages import AIMessage
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content:
            return msg.content
    return ""


def _derive_exploration_from_state(state: DiscoveryState) -> dict | None:
    """从已有确定性证据直接拼 exploration，减少对 LLM 结构化稳定性的依赖。"""
    homepage = state.get("homepage") or {}
    html = homepage.get("html")
    if isinstance(html, str):
        feed_exploration = _derive_feed_exploration_from_html(site_url=state["site_url"], html=html)
        if feed_exploration:
            return feed_exploration
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
            return exploration
    return None


def _derive_feed_exploration_from_html(*, site_url: str, html: str) -> dict | None:
    import re

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
        fetch={"method": "GET", "headers": {}, "query": {}, "json_body": None},
        format_locator={"kind": "feed_entries", "value": "feed.entries"},
        evidence=[{
            "tool": "fetch_page",
            "summary": f"homepage alternate feed detected · href={feed_url}",
        }],
        notes=["deterministic fallback from homepage alternate feed link"],
        success=True,
    ).model_dump()


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


def _derive_exploration_from_result(state: DiscoveryState, result: dict) -> dict | None:
    """从 ReAct 工具轨迹里提炼确定性 exploration。"""
    for event in _iter_tool_events(result):
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
            return exploration
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
        sample_items.append({
            "id": item.get("id") or item.get("no") or item.get("slug") or item.get("uuid"),
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
            "raw": item,
        })
    return sample_items


def _explorer_input_message(state: DiscoveryState) -> str:
    """为 explorer 组装上下文，把已抓到的首页/网络证据直接带给 agent。"""
    lines = [f"请探查站点 {state['site_url']} 的文章列表数据源和 item 结构。"]
    homepage = state.get("homepage") or {}
    if homepage:
        lines.append("已知首页证据：")
        lines.append(json.dumps({
            "title": homepage.get("title"),
            "status": homepage.get("status"),
            "links_sample": (homepage.get("links") or [])[:10],
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


def _explorer_evidence_payload(result: dict) -> list[dict]:
    """从 ReAct 轨迹提炼证据，供第二阶段结构化整理使用。"""
    from langchain_core.messages import AIMessage, ToolMessage

    payload: list[dict] = []
    for msg in result.get("messages", []):
        if isinstance(msg, AIMessage):
            entry = {"kind": "ai", "content": _truncate_text(getattr(msg, "content", ""), 1200)}
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                entry["tool_calls"] = [
                    {"name": call.get("name"), "args": call.get("args")}
                    for call in tool_calls[:8]
                ]
            payload.append(entry)
        elif isinstance(msg, ToolMessage):
            payload.append({
                "kind": "tool",
                "name": getattr(msg, "name", None),
                "content": _truncate_text(getattr(msg, "content", ""), 2400),
            })
    return payload[-12:]


def _synthesize_exploration(*, site_url: str, result: dict, deterministic_exploration: dict | None) -> tuple[str, dict]:
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
        "4. 尽量保留证据中已经确认的字段和值。\n\n"
        "输出 schema：\n"
        "{\n"
        '  "source_type": "json_api | rss | atom | html | unknown",\n'
        '  "list_url": "string or null",\n'
        '  "fetch": {"method": "GET | POST", "headers": {}, "query": {}, "json_body": null},\n'
        '  "format_locator": {"kind": "json_path | feed_entries | html_selector | unknown", "value": "string"},\n'
        '  "fields": {"id": null, "title": null, "url": null, "published_at": null, "summary": null, "content": null},\n'
        '  "html_selectors": {"item_selector": null, "link_selector": null, "title_selector": null, "date_selector": null},\n'
        '  "sample_items": [],\n'
        '  "url_candidates": [],\n'
        '  "pagination": {"type": "none | page_param | offset_limit | cursor | next_url | html_next | unknown", "page_param": null, "size_param": null, "offset_param": null, "limit_param": null, "cursor_param": null, "next_path": null, "has_more_path": null, "start": 1, "size": null, "notes": ""},\n'
        '  "evidence": [],\n'
        '  "notes": [],\n'
        '  "success": true\n'
        "}\n\n"
        f"站点 URL: {site_url}\n"
        f"程序已提取的确定性候选（优先保留已确认字段）:\n{json.dumps(deterministic_exploration or {}, ensure_ascii=False)}\n\n"
        f"ReAct 证据轨迹:\n{json.dumps(evidence, ensure_ascii=False)}\n"
    )
    raw = LlmClient().complete(
        prompt,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    parsed = _parse_strict_exploration_output(raw)
    merged = _merge_exploration(deterministic_exploration, parsed)
    return raw, merged


def _merge_exploration(base: dict | None, overlay: dict | None) -> dict:
    if not base:
        return ExplorationResult(**(overlay or {})).model_dump()
    if not overlay:
        return ExplorationResult(**base).model_dump()

    base_obj = ExplorationResult(**base).model_dump()
    overlay_obj = ExplorationResult(**overlay).model_dump()

    if base_obj.get("success") and not overlay_obj.get("success"):
        return base_obj
    if overlay_obj.get("success") and not base_obj.get("success"):
        return overlay_obj

    merged = dict(base_obj)
    for key in ("source_type", "list_url"):
        if _is_empty_value(merged.get(key)) and not _is_empty_value(overlay_obj.get(key)):
            merged[key] = overlay_obj.get(key)

    for key in ("fetch", "format_locator", "fields", "html_selectors", "pagination"):
        merged[key] = _merge_mapping_values(merged.get(key) or {}, overlay_obj.get(key) or {})

    for key in ("sample_items", "url_candidates", "evidence", "notes"):
        if not merged.get(key) and overlay_obj.get(key):
            merged[key] = overlay_obj.get(key)

    merged["success"] = bool(base_obj.get("success") or overlay_obj.get("success"))
    return ExplorationResult(**merged).model_dump()


def _merge_mapping_values(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        if _is_empty_value(merged.get(key)) and not _is_empty_value(value):
            merged[key] = value
    return merged


def _is_empty_value(value: object) -> bool:
    return value in (None, "", [], {})


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
      "url": "真实 URL；没有则 null",
      "title": "真实标题；没有则 null",
      "raw": "来自 exploration 的原始样本片段"
    }
  ],
  "confidence": "high | medium | low",
  "reason": "为什么这样判断，必须引用 exploration 中的字段/样本/验证结果"
}

已有 URL 字段的例子：
{"mode": "existing_url", "template": null, "base_url": null, "path_field": null, "id_field": null, "url_field": "url", "sample_items": [...], "confidence": "high", "reason": "exploration.fields.url=link，sample_items 已有真实详情页链接"}

相对路径字段的例子：
{"mode": "path_join", "template": null, "base_url": "https://www.openeuler.org", "path_field": "path", "id_field": null, "url_field": null, "sample_items": [...], "confidence": "high", "reason": "sample_items.raw.path 是相对路径，需与站点根 URL 拼接"}

# 质量约束
- 如果 exploration.fields.url 或 sample_items.url 已有真实详情页链接，优先 mode=existing_url，不要强推 template。
- 如果 raw 样本里字段值像 /xx/yy 这种相对路径，优先 mode=path_join，不要伪装成 template + {id}。
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
    from app.discovery.tools import test_path_join, test_url_template, probe_url_patterns
    exploration = state.get("exploration") or {}
    prompt = (VALIDATOR_PROMPT
              .replace("{site_url}", state["site_url"])
              .replace("{exploration}", json.dumps(exploration, ensure_ascii=False)))
    if llm is None:
        from app.llm.client import LlmClient
        ensure_not_cancelled()
        raw = LlmClient().complete(
            prompt,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        rule_obj = UrlRule(**_parse_json_or_fallback(raw))
    else:
        structured = llm.with_structured_output(UrlRule)
        rule = structured.invoke(prompt)
        rule_obj = rule if isinstance(rule, UrlRule) else UrlRule(**rule)
    if rule_obj.mode == "template" and rule_obj.id_field == "path" and not rule_obj.path_field:
        rule_obj = rule_obj.model_copy(update={
            "mode": "path_join",
            "base_url": state["site_url"],
            "path_field": "path",
            "template": None,
            "id_field": None,
        })

    # mode=existing_url：列表已有 url 字段，无需模板验证
    if rule_obj.mode == "existing_url" and rule_obj.url_field:
        return {"url_rule": {
            "mode": "existing_url", "url_field": rule_obj.url_field,
            "id_field": rule_obj.id_field, "evidence": "existing_url",
            "confidence": rule_obj.confidence, "validation_samples": rule_obj.validation_samples,
        }}

    if rule_obj.mode == "path_join" and rule_obj.base_url and rule_obj.path_field and rule_obj.sample_items:
        test_out = test_path_join.invoke({
            "base_url": rule_obj.base_url,
            "path_field": rule_obj.path_field,
            "sample_items": rule_obj.sample_items,
        })
        results = test_out.get("results", [])
        valid = [r for r in results if r.get("is_article_page")]
        if valid:
            return {"url_rule": {
                "mode": "path_join", "base_url": rule_obj.base_url,
                "path_field": rule_obj.path_field,
                "evidence": f"validated {len(valid)}/{len(results)}",
                "confidence": rule_obj.confidence,
                "validation_samples": results[:5],
            }}

    # mode=template：test_url_template 程序验证（sample 里 id 值放在 "id" 键）
    if rule_obj.mode == "template" and rule_obj.template and rule_obj.sample_items:
        test_out = test_url_template.invoke({
            "template": rule_obj.template,
            "id_field": "id",  # sample_items 用 "id" 键存 id 值
            "sample_items": rule_obj.sample_items,
        })
        results = test_out.get("results", [])
        valid = [r for r in results if r.get("is_article_page")]
        if valid:
            return {"url_rule": {
                "mode": "template", "template": rule_obj.template,
                "id_field": rule_obj.id_field, "evidence": f"validated {len(valid)}/{len(results)}",
                "confidence": rule_obj.confidence,
                "validation_samples": results[:5],
            }}

    # mode=unknown / template 验证失败 → probe_url_patterns 兜底
    probe_out = probe_url_patterns.invoke({
        "base_url": state["site_url"],
        "id_value": _first_sample_id(rule_obj),
    })
    hit = next((p for p in probe_out if p.get("is_article_page")), None)
    if hit:
        return {"url_rule": {
            "mode": "template", "template": urljoin(state["site_url"], hit["pattern"]),
            "id_field": rule_obj.id_field, "evidence": f"probed: {hit['pattern']}",
            "confidence": "low",
            "validation_samples": [hit],
        }}
    return {"url_rule": {
        "mode": rule_obj.mode, "template": rule_obj.template,
        "base_url": rule_obj.base_url, "path_field": rule_obj.path_field,
        "id_field": rule_obj.id_field, "url_field": rule_obj.url_field,
        "evidence": "unverified", "confidence": rule_obj.confidence,
        "validation_samples": rule_obj.validation_samples,
    }}


DSL_WRITER_PROMPT = """# 角色
你是"爬取配方编写员"。

# 任务
为给定站点编写一份 DSL 爬取配方 Recipe。配方被纯确定性执行器运行后，必须能抓到文章列表，每条至少包含 title 和 url。

# 背景
本系统有一套 DSL，由动作序列组成。配方一旦存库就会反复跑，不能依赖 LLM，不能写死具体文章 id。
执行器按动作顺序执行，fetch 结果存进上下文 last_fetch，extract 从 last_fetch 取记录。

# DSL 动作格式（严格：每个动作必须是 JSON object，必须有 op 字段，字段名固定如下）

1. fetch
{"op": "fetch", "mode": "json | feed | html", "url": "...", "method": "GET | POST", "headers": {}, "query": {}, "json_body": null, "as": "last_fetch"}

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
3. 第二阶段必须 extract：
   - json_api：from 用 exploration.format_locator.value
   - rss/atom：from 用 feed.entries
   - html：from 用 selector:{exploration.html_selectors.item_selector}
4. extract.fields 必须产出 title 和 url：
   - title 从 exploration.fields.title 或 html title_selector 来。
   - 如果 url_rule.mode=existing_url：url 直接用 url_rule.url_field（裸字段名），不要拼模板。
   - 如果 url_rule.mode=path_join：url 用 template:{base_url}{item.<path_field>}。
     例如 base_url=https://www.openeuler.org、path_field=path，则 DSL 写 template:https://www.openeuler.org{item.path}。
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
- 配方执行后应能抓到 ≥1 条带 title 和 url 的文章。
"""


def dsl_writer(state: DiscoveryState, llm=None) -> DiscoveryState:
    """DslWriter worker：with_structured_output 强制产出合法 DSL Recipe（喂 site_url+url_rule+exploration）。"""
    ensure_not_cancelled()
    from app.discovery.dsl import DslRecipe
    exploration = state.get("exploration") or {}
    url_rule = state.get("url_rule") or {}
    if url_rule.get("mode") == "path_join" and url_rule.get("base_url") and url_rule.get("path_field"):
        fields = dict(exploration.get("fields") or {})
        path_field = url_rule["path_field"]
        fields["url"] = f"template:{url_rule['base_url']}{{item.{path_field}}}"
        recipe_dict = {
            "recipe_type": "dsl",
            "entry_url": state["site_url"],
            "actions": [
                {
                    "op": "fetch",
                    "mode": "json",
                    "url": exploration.get("list_url") or state["site_url"],
                    "method": (exploration.get("fetch") or {}).get("method", "GET"),
                    "headers": (exploration.get("fetch") or {}).get("headers", {}),
                    "query": (exploration.get("fetch") or {}).get("query", {}),
                    "json_body": (exploration.get("fetch") or {}).get("json_body"),
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
        return {"dsl_recipe": DslRecipe(**recipe_dict).model_dump(), "token_used": state.get("token_used", 0) + 1000}
    prompt = (DSL_WRITER_PROMPT
              .replace("{site_url}", state["site_url"])
              .replace("{url_rule}", json.dumps(url_rule, ensure_ascii=False))
              .replace("{exploration}", json.dumps(exploration, ensure_ascii=False)))
    if llm is None:
        from app.llm.client import LlmClient

        ensure_not_cancelled()
        raw = LlmClient().complete(
            prompt,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        recipe_dict = DslRecipe(**_parse_json_or_fallback(raw)).model_dump()
    else:
        structured = llm.with_structured_output(DslRecipe)  # 测试 mock 路径保留
        recipe = structured.invoke(prompt)
        # with_structured_output 真实路径返回 DslRecipe 实例；部分后端/mock 返回 dict —— 统一转 dict
        if isinstance(recipe, DslRecipe):
            recipe_dict = recipe.model_dump()
        else:
            recipe_dict = DslRecipe(**recipe).model_dump()
    return {"dsl_recipe": recipe_dict, "token_used": state.get("token_used", 0) + 1000}


class AuditVerdict(BaseModel):
    """LLM 对实跑抓取结果的质量评判。"""
    passed: bool                          # 综合：这份配方值得存吗
    is_real_content: bool                 # 抓到的是真文章，不是反爬/错误/占位/无关页
    has_pagination: bool                  # 实现了翻页抓多页，不是只抓单页
    not_blocked: bool                     # 没被页面限制/反爬挡住
    value_assessment: str = ""            # 一句话价值评估
    issues: list[str] = Field(default_factory=list)  # 发现的问题
    suggested_fix: str | None = None      # 给 dsl_writer 的修改建议（不通过时）


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
    from app.discovery.interpreter import DslInterpreter
    try:
        return DslInterpreter().run(recipe)
    except Exception as e:
        return {"items": [], "stats": {"discovered_count": 0}, "error": str(e)}


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


def _llm_audit_quality(llm, site_url: str, recipe: DslRecipe, items: list, errors: list) -> dict:
    """调 LLM 评判实跑抓取结果的价值/全面性/反爬/翻页，返回 AuditVerdict dict。"""
    sample = []
    for it in items[:8]:
        s = {"title": it.get("title"), "url": it.get("url")}
        content = it.get("content") or it.get("summary") or ""
        if content:
            s["content_snippet"] = str(content)[:200]
        sample.append(s)
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
        )
        return AuditVerdict(**_parse_json_or_fallback(raw)).model_dump()
    structured = llm.with_structured_output(AuditVerdict)
    verdict = structured.invoke(prompt)
    if isinstance(verdict, AuditVerdict):
        return verdict.model_dump()
    return AuditVerdict(**verdict).model_dump()


def auditor(state: DiscoveryState, llm=None, test_fn=None) -> DiscoveryState:
    """Auditor worker：实跑配方 → LLM 评判抓取价值/全面性/反爬/翻页 → 结合静态校验判通过。

    区别于 validator：validator 验单条 URL 规律真伪，auditor 复核整份 Recipe 的实跑结果质量。
    test_fn: 注入"跑配方返回产出"的函数（测试 mock）；None → 真跑 DslInterpreter（生产）。
    """
    ensure_not_cancelled()
    from app.discovery.dsl import DslRecipe, validate_semantics
    recipe = DslRecipe(**state["dsl_recipe"])
    errors = validate_semantics(recipe)  # 静态审：结构合理性
    # 动态审：实跑配方拿真实产出（测试可注入 mock，生产真跑）
    test_result = test_fn(recipe) if test_fn is not None else _run_recipe_for_audit(recipe)
    items = test_result.get("items", [])
    discovered_count = test_result.get("stats", {}).get("discovered_count", len(items))
    # LLM 审：看真实抓到的条目，判价值/全面性/反爬/翻页
    llm_verdict = _llm_audit_quality(llm, state["site_url"], recipe, items, errors)
    # 通过 = 无静态错误 + 抓到至少 1 条 + LLM 判值得存
    passed = (not errors) and discovered_count >= 1 and llm_verdict.get("passed", False)
    return {
        "audit_result": {
            "passed": passed, "errors": errors, "test": test_result,
            "llm_verdict": llm_verdict,
            "suggested_next": "dsl_writer" if not passed else None,
        },
        "attempt": state.get("attempt", 0) + (0 if passed else 1),  # 不通过则 attempt+1
    }


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
        return {"passed": a.get("passed"), "issues": lv.get("issues"),
                "attempt": update.get("attempt")}
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
        issues = a.get("llm_verdict", {}).get("issues") or []
        issues_text = "; ".join(str(i) for i in issues[:3]) or "无"
        return f" · passed={passed} · attempt={update.get('attempt')} · issues={issues_text}"
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
            initial = {"site_url": site_url, "attempt": 0, "token_used": 0, "force": force, "name": name}
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
    """把 explorer 两阶段生成内容写入日志面板，便于前端排查。"""
    raw = _truncate_text(update.get("explorer_agent_output"), 600)
    if raw:
        append_run_log(
            "探查",
            f"explorer 原始输出 · {raw}",
            source=source_label,
            run_id=run_id,
            step="explorer_raw",
        )
    synth = _truncate_text(update.get("explorer_synthesis_output"), 600)
    if synth:
        append_run_log(
            "探查",
            f"explorer 结构化整理 · {synth}",
            source=source_label,
            run_id=run_id,
            step="explorer_structured",
        )
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
