"""SiteDiscoveryGraph：生成命 LangGraph 图——State + supervisor 路由 + 确定性节点 + 4 worker。

supervisor 按 State 决定下一个 worker / 终止 / 转兜底；token 超 TOKEN_BUDGET 硬中止；
attempt 用尽判 failed。worker 在 Task 11 实装，图组装在 Task 12。
"""

from __future__ import annotations

import json
import threading
from typing import TypedDict
from urllib.parse import urljoin

from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, END

from app.config import get_settings

TOKEN_BUDGET = 50000  # 生成命 token 硬上限，超即中止
MAX_ATTEMPTS = 3      # 图级重试上限
STALE_RUN_TIMEOUT_SECONDS = 1800       # running 超过 30 分钟判超时回收（定时巡检用）
STALE_RUN_PATROL_INTERVAL_MINUTES = 5  # 定时巡检间隔


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
            src = Source(name=domain, type=SourceType.DISCOVERY.value, url=state["site_url"],
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


class UrlRule(BaseModel):
    """LLM 推断的 URL 规律：模板 + id 字段 + 样本（供 test_url_template 程序验证）。"""
    template: str | None = None
    id_field: str = "id"
    sample_items: list[dict] = Field(default_factory=list)


def _first_sample_id(rule: UrlRule) -> str:
    """取首个样本的 id 值，供 probe_url_patterns 探测；无样本时回退 "1"。"""
    if not rule.sample_items:
        return "1"
    val = rule.sample_items[0].get(rule.id_field)
    return str(val) if val is not None else "1"


def validator(state: DiscoveryState, llm=None) -> DiscoveryState:
    """Validator worker：LLM 推断 URL 规律 + test_url_template 程序验证（技术真伪，非审计）。

    主路：LLM 提 template + sample_items → test_url_template 拿真实 ID 逐个请求验证。
    兜底：验证不通过或 LLM 没头绪 → probe_url_patterns 批量试常见 pattern（灵感来源）。
    都不中 → evidence="unverified"，template 回退到 LLM 提的（若有）。
    区别于 auditor：validator 验单条 URL 规律真伪，auditor 复核整份 Recipe 合理性/达标。
    """
    llm = llm or _make_llm()
    from app.discovery.tools import test_url_template, probe_url_patterns
    structured = llm.with_structured_output(UrlRule)
    rule = structured.invoke(
        f"基于探查结果 {state.get('exploration')} 为站点 {state['site_url']} "
        f"推断文章详情页 URL 模板。返回 template（含 {{id}} 占位符）、id_field、sample_items（真实样本）。"
    )
    # with_structured_output 真实路径返回 UrlRule 实例；mock/部分后端返回 dict —— 统一归一
    rule_obj = rule if isinstance(rule, UrlRule) else UrlRule(**rule)

    # 主路：test_url_template 程序验证（需 template + sample）
    if rule_obj.template and rule_obj.sample_items:
        test_out = test_url_template.invoke({
            "template": rule_obj.template,
            "id_field": rule_obj.id_field,
            "sample_items": rule_obj.sample_items,
        })
        valid = [r for r in test_out.get("results", []) if r.get("is_article_page")]
        if valid:
            return {"url_rule": {
                "template": rule_obj.template,
                "id_field": rule_obj.id_field,
                "evidence": "validated",
                "verified": True,
            }}

    # 兜底：probe_url_patterns 批量试常见 pattern（LLM 没头绪 / 验证失败）
    probe_out = probe_url_patterns.invoke({
        "base_url": state["site_url"],
        "id_value": _first_sample_id(rule_obj),
    })
    hit = next((p for p in probe_out if p.get("is_article_page")), None)
    if hit:
        return {"url_rule": {
            "template": urljoin(state["site_url"], hit["pattern"]),
            "id_field": rule_obj.id_field,
            "evidence": "probed",
            "verified": True,
        }}
    return {"url_rule": {
        "template": rule_obj.template,
        "id_field": rule_obj.id_field,
        "evidence": "unverified",
        "verified": False,
    }}


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


def start_discovery_run(site_url: str, force: bool = False) -> int:
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
        target=_execute_discovery, args=(run_id, site_url, force),
        daemon=True, name=f"discovery-run-{run_id}",
    ).start()
    return run_id


def _execute_discovery(run_id: int, site_url: str, force: bool) -> None:
    """后台线程执行核心：建图（PostgresSaver）+ 跑 + 更新 site_discovery_runs。

    进程崩了可从 PostgresSaver checkpoint 跨进程续跑（thread_id 关联 run_id）。
    """
    from datetime import datetime, timezone
    from langgraph.checkpoint.postgres import PostgresSaver
    from app.db import SessionLocal
    from app.models import SiteDiscoveryRun
    s = get_settings()
    with PostgresSaver.from_conn_string(_to_psycopg_conn_string(s.database_url)) as checkpointer:
        checkpointer.setup()  # 自动建 checkpoint 表
        g = build_graph(checkpointer=checkpointer)
        db_sess = SessionLocal()
        try:
            # thread_id 关联 run，崩了重启可从 checkpoint 续跑
            final = g.invoke(
                {"site_url": site_url, "attempt": 0, "token_used": 0, "force": force},
                config={"configurable": {"thread_id": f"discovery-{run_id}"}},
            )
            run = db_sess.get(SiteDiscoveryRun, run_id)
            run.status = "completed" if final.get("verdict") == "dsl" else "failed"
            run.resulting_method_id = final.get("method_id")
            run.llm_token_usage = final.get("token_used", 0)
            run.node_trace = [{"verdict": final.get("verdict")}]
            run.ended_at = datetime.now(timezone.utc)
            if final.get("error"):
                run.error_message = final["error"]
            db_sess.commit()
        except Exception as e:
            # 兜底：图级异常标 failed（节点级异常已在 supervisor 路由处理）
            db_sess.rollback()
            run = db_sess.get(SiteDiscoveryRun, run_id)
            if run and run.status == "running":
                run.status = "failed"; run.error_message = str(e)
                run.ended_at = datetime.now(timezone.utc)
                db_sess.commit()
        finally:
            db_sess.close()


def run_discovery(site_url: str, force: bool = False) -> dict:
    """同步入口（测试/同步场景用）：建记录 + 同步跑 _execute_discovery，返回最终结果摘要。"""
    from app.db import SessionLocal
    from app.models import SiteDiscoveryRun
    s = SessionLocal()
    try:
        run = SiteDiscoveryRun(site_url=site_url, status="running")
        s.add(run); s.commit(); run_id = run.id
    finally:
        s.close()
    _execute_discovery(run_id, site_url, force)
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
