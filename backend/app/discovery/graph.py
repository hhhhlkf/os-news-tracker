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

logger = logging.getLogger(__name__)

TOKEN_BUDGET = 50000  # 生成命 token 硬上限，超即中止
MAX_ATTEMPTS = 3      # 图级重试上限
STALE_RUN_TIMEOUT_SECONDS = 1800       # running 超过 30 分钟判超时回收（定时巡检用）
STALE_RUN_PATROL_INTERVAL_MINUTES = 5  # 定时巡检间隔

# node_name → 日志 stage 标签（前端渲染 [stage] source message · key=value）
_STAGE_LABELS = {
    "fetch_homepage": "探查",
    "capture_network": "探查",
    "supervisor": "路由",
    "explorer": "探查",
    "validator": "验证",
    "dsl_writer": "配方",
    "auditor": "审计",
    "save_method": "存储",
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


def save_method(state: DiscoveryState, db=None) -> DiscoveryState:
    """确定性节点：去重签名 + 存 crawl_methods + crawl_method_domains + 建 sources 记录。

    force=true 且同 domain 已有 → 覆盖更新（保留 method_id/source_id，历史连续）；
    否则新建 crawl_method + 对应 sources(type=discovery) 记录。
    db=None 时自建 SessionLocal（图运行命用）；传入 db 时复用（测试用，不负责关闭）。
    """
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

# 输出（最终回复必须是 JSON，不要 Markdown，不要解释，不要包代码块）
固定字段：
{
  "source_type": "json_api | rss | atom | html | unknown",
  "list_url": "文章列表数据源 URL；失败则 null",
  "fetch": {"method": "GET | POST", "headers": {}, "query": {}, "json_body": null},
  "format_locator": {"kind": "json_path | feed_entries | html_selector | unknown", "value": "如 data.items / feed.entries / selector:article.card"},
  "fields": {"id": "字段名或 null", "title": "字段名或 selector 或 null", "url": "字段名或 selector 或 null", "published_at": "字段名或 selector 或 null", "summary": "...或 null", "content": "...或 null"},
  "html_selectors": {"item_selector": "仅 html 需要；否则 null", "link_selector": "...", "title_selector": "...", "date_selector": "..."},
  "sample_items": [
    {"raw": "保留原始 item 里 id/title/url/date 相关字段（不要只给摘要）", "id": "真实 id/slug/no 或 null", "title": "真实标题或 null", "url": "真实详情页 URL 或 null", "published_at": "真实发布时间或 null"}
  ],
  "url_candidates": [
    {"mode": "existing_url | template_candidate | unknown", "url_field": "列表已有 URL 时写字段名；否则 null", "id_field": "需拼 URL 时写 id 字段名；否则 null", "template": "如 https://x.com/blog/{id}；没把握则 null", "verification": "如 3/3 opened；未验证则 null"}
  ],
  "pagination": {"type": "none | page_param | offset_limit | cursor | next_url | html_next | unknown", "page_param": "如 page 或 null", "size_param": "如 pageSize 或 null", "offset_param": "如 offset 或 null", "limit_param": "如 limit 或 null", "cursor_param": "如 cursor 或 null", "next_path": "响应里 next URL/cursor path 或 null", "has_more_path": "响应里 hasMore path 或 null", "start": 1, "size": null, "notes": "无法确认则说明原因"},
  "evidence": [{"tool": "fetch_page | capture_network | inspect_item | test_url_template | probe_url_patterns", "summary": "关键证据摘要，必须具体到 URL、path、字段、样本数量"}],
  "notes": ["反爬 / 需要 JS / 需要登录 / POST / headers / 其它；没有则写 无"],
  "success": true
}

# 质量约束
- 必须输出 JSON，不要自由文本。
- 不要凭空猜字段名、selector、URL 模板；凡写的都要有工具调用证据（记进 evidence）。
- sample_items 必须给 3~5 条真实原始 item（保留 id/title/url/published_at 相关字段），严禁编造。
- source_type=html 时必须给 html_selectors（item/link/title/date）。
- source_type=json_api 时必须给 format_locator.value（json path）。
- source_type=rss/atom 时 format_locator.kind=feed_entries、value=feed.entries。
- 如果列表已有 url/link 字段，在 url_candidates 里记 mode=existing_url，不要强行推模板。
- 站点有反爬、JS 渲染、需登录、POST body、特殊 headers，要明确写在 notes。
- 探查失败或没找到列表数据源，如实输出 source_type=unknown、success=false。
"""


def explorer(state: DiscoveryState, llm=None) -> DiscoveryState:
    """Explorer worker：ReAct agent 自主调工具探查站点，最终产出结构化 JSON（exploration）。"""
    llm = llm or _make_llm()
    from app.discovery.tools import TOOLS
    from langgraph.prebuilt import create_react_agent
    agent = create_react_agent(llm, TOOLS, prompt=EXPLORER_SYSTEM_PROMPT)
    result = agent.invoke({
        "messages": [("user", f"请探查站点 {state['site_url']} 的文章列表数据源和 item 结构。")],
    })
    # 取最后一条 AIMessage 的内容，按 prompt 要求是 JSON
    final_content = _extract_final_ai_content(result)
    return {"exploration": _parse_json_or_fallback(final_content)}


def _extract_final_ai_content(result: dict) -> str:
    """从 ReAct 结果里取最后一条 AIMessage 的文本内容。"""
    from langchain_core.messages import AIMessage
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content:
            return msg.content
    return ""


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
    mode: str = "unknown"                       # existing_url | template | unknown
    template: str | None = None                 # 仅 mode=template 时填，用 {id} 占位
    id_field: str | None = None                 # 列表里充当 id 的字段名（原字段名）
    url_field: str | None = None                # 列表里直接给出 URL 的字段名（mode=existing_url）
    sample_items: list[dict] = Field(default_factory=list)  # [{id,url,title,raw}]
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
判断文章详情页 URL 如何获得（三选一）：
1. 列表 item 已经直接给出 URL → mode=existing_url
2. 需要用 id/slug/no 拼 URL 模板 → mode=template
3. 无法判断 → mode=unknown
你的输出会被程序拿真实样本请求验证，不通过会回退让你重提。

# 背景
- 如果列表里已有 url/link 字段，应直接使用该字段（mode=existing_url），不要多此一举去推模板。
- 如果列表只有 id/slug/no，则需要推断模板，如 https://x.com/blog/{id}（mode=template）。
- 如果没有把握，不要猜，mode=unknown。

# 输入
- 站点 URL：{site_url}
- 探查结果：{exploration}

# 输出
必须按 UrlRule schema 结构化输出：

{
  "mode": "existing_url | template | unknown",
  "template": "详情页 URL 模板；仅 mode=template 时填写，必须使用 {id} 占位；否则 null",
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
{"mode": "existing_url", "template": null, "id_field": null, "url_field": "url", "sample_items": [...], "confidence": "high", "reason": "exploration.fields.url=link，sample_items 已有真实详情页链接"}

# 质量约束
- 如果 exploration.fields.url 或 sample_items.url 已有真实详情页链接，优先 mode=existing_url，不要强推 template。
- template 必须使用 {id} 占位符，不要写 {item.no}。
- sample_items 必须来自 exploration.sample_items，严禁编造。
- sample_items 数量 3~5 个；不足则给已有数量并说明。
- 没把握就 mode=unknown，不要为了输出模板而猜。
- id_field 必须是 exploration 里真实存在的字段名。
"""


def validator(state: DiscoveryState, llm=None) -> DiscoveryState:
    """Validator worker：LLM 判 URL 来源模式 + test_url_template 程序验证（技术真伪，非审计）。

    mode=existing_url：列表已有 url 字段，直接用，无需模板验证。
    mode=template：test_url_template 拿真实 id 逐个请求验证。
    mode=unknown / 验证失败：probe_url_patterns 批量试常见 pattern 兜底。
    都不中 → evidence="unverified"。
    """
    llm = llm or _make_llm()
    from app.discovery.tools import test_url_template, probe_url_patterns
    exploration = state.get("exploration") or {}
    prompt = (VALIDATOR_PROMPT
              .replace("{site_url}", state["site_url"])
              .replace("{exploration}", json.dumps(exploration, ensure_ascii=False)))
    structured = llm.with_structured_output(UrlRule)
    rule = structured.invoke(prompt)
    rule_obj = rule if isinstance(rule, UrlRule) else UrlRule(**rule)

    # mode=existing_url：列表已有 url 字段，无需模板验证
    if rule_obj.mode == "existing_url" and rule_obj.url_field:
        return {"url_rule": {
            "mode": "existing_url", "url_field": rule_obj.url_field,
            "id_field": rule_obj.id_field, "evidence": "existing_url",
            "confidence": rule_obj.confidence,
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
        }}
    return {"url_rule": {
        "mode": rule_obj.mode, "template": rule_obj.template,
        "id_field": rule_obj.id_field, "url_field": rule_obj.url_field,
        "evidence": "unverified", "confidence": rule_obj.confidence,
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
    llm = llm or _make_llm()
    from app.discovery.dsl import DslRecipe
    exploration = state.get("exploration") or {}
    url_rule = state.get("url_rule") or {}
    prompt = (DSL_WRITER_PROMPT
              .replace("{site_url}", state["site_url"])
              .replace("{url_rule}", json.dumps(url_rule, ensure_ascii=False))
              .replace("{exploration}", json.dumps(exploration, ensure_ascii=False)))
    structured = llm.with_structured_output(DslRecipe)  # Pydantic 校验，不合法让 LLM 重产
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
    llm = llm or _make_llm()
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
    threading.Thread(
        target=_execute_discovery, args=(run_id, site_url, force, name),
        daemon=True, name=f"discovery-run-{run_id}",
    ).start()
    return run_id


def _step_summary(node_name: str, update: dict) -> dict:
    """从节点的 state update 提取该步产出摘要，供前端节点详情卡展示。"""
    if node_name == "explorer":
        e = update.get("exploration") or {}
        return {"source_type": e.get("source_type"), "list_url": e.get("list_url"),
                "success": e.get("success")}
    if node_name == "validator":
        u = update.get("url_rule") or {}
        return {"mode": u.get("mode"), "template": u.get("template"),
                "evidence": u.get("evidence")}
    if node_name == "dsl_writer":
        r = update.get("dsl_recipe") or {}
        actions = r.get("actions") or []
        return {"actions": len(actions), "has_loop": any(a.get("op") == "loop" for a in actions)}
    if node_name == "auditor":
        a = update.get("audit_result") or {}
        return {"passed": a.get("passed"), "issues": a.get("issues"),
                "attempt": update.get("attempt")}
    return {}


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
    with PostgresSaver.from_conn_string(_to_psycopg_conn_string(s.database_url)) as checkpointer:
        checkpointer.setup()  # 自动建 checkpoint 表
        g = build_graph(checkpointer=checkpointer)
        db_sess = SessionLocal()
        try:
            config = {"configurable": {"thread_id": f"discovery-{run_id}"}}
            initial = {"site_url": site_url, "attempt": 0, "token_used": 0, "force": force, "name": name}
            node_trace: list = []
            append_run_log("任务", "Discovery 探查开始", source=source_label,
                           run_id=run_id, url=site_url, force=force)
            # 逐节点 stream → 实时更新 node_trace 供前端轮询 + log 输出
            for chunk in g.stream(initial, config=config, stream_mode="updates"):
                for node_name, update in chunk.items():
                    entry = {"step": node_name, "status": "done",
                             "ts": datetime.now(timezone.utc).isoformat(),
                             "summary": _step_summary(node_name, update or {})}
                    node_trace.append(entry)
                    stage = _STAGE_LABELS.get(node_name, node_name)
                    msg = _STEP_MESSAGES.get(node_name, f"步骤 {node_name} 完成")
                    append_run_log(stage, msg, source=source_label,
                                   step=node_name, trace_count=len(node_trace))
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
