# LangGraph Agent 架构重设计

**日期：** 2026-06-22
**状态：** 待用户评审
**项目：** `os-news-tracker` — Agent 采集引擎架构升级

---

## 1. 背景与动机

当前 `app/agent/` 的 Handoff Chain 使用自研 `LlmClient` 手写 HTTP 调用，缺少：

- **Checkpointing**：节点中途崩溃无法从断点恢复
- **Conditional edges**：质量评估全失败时只能退出，无法条件跳转
- **Streaming**：LLM 输出无法 token 级实时推送给前端
- **Multi-agent 扩展路径**：未来引入 Supervisor 管理多 source 并发时没有基础设施

本次改造用 **LangGraph StateGraph** 重写 Handoff Chain 的编排层，用 **LangChain LCEL** 替换各节点内部的 LLM 调用。

### 明确不做
- Enricher / DigestAgent / ProfileAdvisor 不受影响（保留 LlmClient）
- CrawlDAG 的 asyncio.Semaphore 并发逻辑不动（LangGraph Send API 不适合精细速率控制）
- 不引入 Human-in-the-loop（当前版本不需要）

---

## 2. 整体架构

### 2.1 当前 vs. 改后对比

| 层面 | 改前 | 改后 |
|------|------|------|
| 编排 | `AgentCrawlFetcher` 手写 `asyncio.run(_pipeline())` | LangGraph `StateGraph.ainvoke()` |
| LLM 调用 | `LlmClient.complete(prompt)` + `asyncio.to_thread` | LangChain LCEL `chain.ainvoke()` (原生 async) |
| 状态管理 | 局部变量在 coroutine 中传递 | `AgentState` TypedDict，节点间显式流转 |
| Checkpointing | 无 | `AsyncPostgresSaver`（每节点完成后自动 checkpoint）|
| 并行执行 | `asyncio.Semaphore` + `gather` | Quality/Summary 节点用 LangGraph `Send()` 扇出；CrawlDAG 保留 Semaphore |
| 条件跳转 | 无 | `quality_check_edge`：通过 → 继续；全丢 → END |
| Streaming | 无 | `graph.astream_events()` → SSE |

### 2.2 图结构

```
START
  │
  ▼
plan_node          — LangChain ChatOpenAI + 确定性 URL 验证
  │
  ▼
crawl_node         — asyncio.Semaphore 并发抓取（保留，不走 LangGraph）
  │
  ▼
quality_router     — Send() 扇出：每页生成一个 quality_node 实例
  │  (×N 并行)
  ▼
quality_reducer    — 汇总 + 阈值过滤
  │
  ├─[qualified_pages 为空]──────────────────────────► END
  │
  └─[qualified_pages 非空]
        │
        ▼
  summary_router   — Send() 扇出：每页生成一个 summary_node 实例
        │  (×M 并行)
        ▼
  summary_reducer  — 汇总 AgentItem
        │
        ▼
       END
```

---

## 3. AgentState — 统一状态模式

所有节点共享一个类型化状态对象，LangGraph 在每次节点执行后自动合并返回值。

```python
# backend/app/agent/schemas.py（新增，保留现有 dataclass 定义）
from typing import Annotated
from typing_extensions import TypedDict
import operator


class AgentState(TypedDict):
    # ── 输入配置 ────────────────────────────────────────────
    source_id: int
    root_url: str
    focus_areas: list[str]
    topic_groups: list[str]
    quality_threshold: int
    max_urls_per_run: int
    crawl_workers: int
    quality_workers: int
    summary_workers: int

    # ── plan_node 输出 ───────────────────────────────────────
    candidate_links: list[dict]           # [{url: str, text: str}]

    # ── crawl_node 输出 ──────────────────────────────────────
    raw_pages: list[dict]                 # 序列化 RawPage

    # ── Quality 扇出（per-instance 字段）────────────────────
    current_page: dict | None             # 当前单页，由 Send() 注入

    # ── Quality 扇出结果（Annotated 累积列表）────────────────
    # 多个并行 quality_node 各自返回一项，LangGraph 自动合并为列表
    qualified_pages: Annotated[list[dict], operator.add]

    # ── Summary 扇出（per-instance 字段）────────────────────
    current_qualified: dict | None        # 当前单页，由 Send() 注入

    # ── Summary 扇出结果 ─────────────────────────────────────
    agent_items: Annotated[list[dict], operator.add]
```

---

## 4. 节点实现

### 4.1 LangChain 模型工厂

```python
# backend/app/agent/llm.py（新文件）
from langchain_openai import ChatOpenAI
from app.config import get_settings


def make_chat_model(temperature: float = 0.2, **kwargs) -> ChatOpenAI:
    """创建指向内网 LLM 网关的 ChatOpenAI 实例。

    所有 Agent 节点通过此工厂获取 LLM，便于统一切换模型或配置。
    内网 LLM 网关兼容 OpenAI API，ChatOpenAI 的 base_url 参数直接适配。
    """
    s = get_settings()
    return ChatOpenAI(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model=s.llm_model,
        temperature=temperature,
        **kwargs,
    )
```

### 4.2 db 依赖注入方式

LangGraph node 函数的签名固定为 `(state: AgentState)` 或 `(state: AgentState, config: RunnableConfig)`。需要数据库会话的节点通过 **闭包注入**（在 `build_agent_graph` 时捕获 `db`）：

```python
# backend/app/agent/graph.py 里：
def build_agent_graph(db, checkpointer=None):
    async def _plan_node(state):
        return await plan_node(state, db=db)

    async def _quality_node(state):
        return await quality_node(state, db=db)

    workflow.add_node("plan_node", _plan_node)
    workflow.add_node("quality_node", _quality_node)
    ...
```

`nodes.py` 里的函数仍接受 `db` 关键字参数，保持可测试性（单元测试直接传 mock db）。

### 4.3 plan_node

```python
# backend/app/agent/nodes.py（新文件）
from app.agent.llm import make_chat_model
from app.agent.plan_agent import PlanAgent
from app.agent.schemas import AgentState


async def plan_node(state: AgentState, *, db) -> dict:
    """第①节点：从源站首页发现候选 URL。

    内部用 PlanAgent 的 _fetch_links 提取页面链接（确定性），
    再通过 LangChain LCEL 调用 LLM 做语义筛选（随机性）。
    LLM 输出经确定性验证层（同域、http 协议、SiteMemory）过滤后写入状态。
    """
    agent = PlanAgent(llm=make_chat_model(), db=db)
    plan = await agent.plan_async(state)      # plan_agent 提供 async 版
    return {"candidate_links": [{"url": u.url, "text": u.guessed_topic} for u in plan.urls]}


async def crawl_node(state: AgentState) -> dict:
    """第②节点：并发抓取所有候选 URL（保留 asyncio.Semaphore）。"""
    from app.agent.crawl_dag import CrawlDAG
    from app.agent.schemas import AgentSourceConfig, CrawlPlan, PlanUrl

    config = AgentSourceConfig(
        source_id=state["source_id"],
        focus_areas=state["focus_areas"],
        topic_groups=state["topic_groups"],
        crawl_workers=state["crawl_workers"],
        quality_workers=state["quality_workers"],
        summary_workers=state["summary_workers"],
        quality_threshold=state["quality_threshold"],
        max_urls_per_run=state["max_urls_per_run"],
    )
    plan = CrawlPlan(
        source_id=state["source_id"],
        urls=[PlanUrl(url=l["url"], guessed_topic=l.get("text", "")) for l in state["candidate_links"]],
    )
    dag = CrawlDAG()
    pages = await dag.execute(plan, config)
    return {"raw_pages": [{"url": p.url, "title": p.title, "content": p.content,
                           "guessed_topic": p.guessed_topic} for p in pages]}
```

### 4.3 Quality 扇出 + 节点 + 归约

```python
from langgraph.types import Send


def quality_router(state: AgentState) -> list[Send]:
    """质量扇出路由：为每个原始页面生成一个独立的 quality_node 实例。"""
    return [
        Send("quality_node", {**state, "current_page": page})
        for page in state["raw_pages"]
    ]


async def quality_node(state: AgentState, *, db) -> AgentState:
    """对单页进行 LLM 质量评估（并行实例）。"""
    from langchain_core.output_parsers import JsonOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from app.agent.quality_pool import QUALITY_PROMPT   # 从 quality_pool 导出 prompt 模板
    from app.agent.site_memory import SiteMemory

    page = state["current_page"]
    memory = SiteMemory()

    # SiteMemory 缓存命中 → 跳过 LLM
    cached = memory.get(db=db, source_id=state["source_id"], url=page["url"])
    if cached is not None:
        if cached.verdict == "discard":
            return {"qualified_pages": []}
        return {"qualified_pages": [{"page": page, "verdict": cached.verdict,
                                     "score": cached.quality_score or 0}]}

    chain = (
        ChatPromptTemplate.from_template(QUALITY_PROMPT)
        | make_chat_model()
        | JsonOutputParser()
    )
    result = await chain.ainvoke({
        "focus_areas": ", ".join(state["focus_areas"]),
        "url": page["url"],
        "title": page.get("title", ""),
        "content_preview": page.get("content", "")[:1500],
    })

    score = result.get("score", 0)
    verdict = result.get("verdict", "discard")
    if score < state["quality_threshold"]:
        verdict = "discard"

    # 写 SiteMemory
    from app.agent.schemas import QualityResult
    if result.get("should_remember") or verdict == "discard":
        memory.upsert(db=db, source_id=state["source_id"], url=page["url"],
                      result=QualityResult(score=score, reason=result.get("reason", ""),
                                           relevant_topic=result.get("relevant_topic", ""),
                                           verdict=verdict, should_remember=result.get("should_remember", False)))

    if verdict == "discard":
        return {"qualified_pages": []}
    return {"qualified_pages": [{"page": page, "verdict": verdict, "score": score}]}


def quality_join(state: AgentState) -> dict:
    """Quality 扇入节点（join point）。

    LangGraph 中，Send() 扇出的多个 quality_node 都指向此节点。
    `Annotated[list, operator.add]` 已自动将各实例返回的 qualified_pages 合并为列表。
    本节点无需额外逻辑，仅作为扇入汇聚点，使条件边有明确的出发节点。
    """
    return {}


def quality_check_edge(state: AgentState) -> str:
    """条件边：quality_join 完成后，决定是否继续 summary 阶段。"""
    if not state.get("qualified_pages"):
        return "__end__"
    return "summary_router"
```

### 4.4 Summary 扇出 + 节点 + 归约

```python
def summary_router(state: AgentState) -> list[Send]:
    """摘要扇出路由：为每个通过质量筛选的页面生成一个 summary_node 实例。"""
    return [
        Send("summary_node", {**state, "current_qualified": qp})
        for qp in state["qualified_pages"]
    ]


async def summary_node(state: AgentState) -> dict:
    """对单页生成自适应摘要（并行实例）。"""
    from langchain_core.output_parsers import JsonOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from app.agent.summary_pool import SUMMARY_PROMPT   # 从 summary_pool 导出 prompt 模板

    qp = state["current_qualified"]
    page = qp["page"]

    chain = (
        ChatPromptTemplate.from_template(SUMMARY_PROMPT)
        | make_chat_model()
        | JsonOutputParser()
    )
    result = await chain.ainvoke({
        "focus_areas": ", ".join(state["focus_areas"]),
        "topic_groups": ", ".join(state["topic_groups"]) if state["topic_groups"] else "无",
        "url": page["url"],
        "content": page.get("content", "")[:4000],
    })

    item = {
        "source_id": state["source_id"],
        "url": result.get("source_url", page["url"]),
        "title": result.get("title", ""),
        "topic_group": result.get("topic_group"),
        "content_type": result.get("content_type", "article"),
        "importance": result.get("importance", "低"),
        "body": result.get("body", ""),
        "key_facts": result.get("key_facts", []),
    }
    return {"agent_items": [item]}
```

---

## 5. 图组装

```python
# backend/app/agent/graph.py（新文件）
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.memory import MemorySaver

from app.agent.schemas import AgentState
from app.agent.nodes import (
    plan_node, crawl_node,
    quality_router, quality_node, quality_reducer, quality_check_edge,
    summary_router, summary_node,
)


def build_agent_graph(checkpointer=None) -> StateGraph:
    """构建 Agent 采集 StateGraph。

    Args:
        checkpointer: LangGraph checkpointer 实例。
                      None → 无持久化（测试）；
                      MemorySaver → 内存（开发）；
                      AsyncPostgresSaver → 生产数据库持久化。
    """
    workflow = StateGraph(AgentState)

    # ── 注册节点 ──────────────────────────────────
    # db 通过闭包注入（见上方「db 依赖注入方式」）
    workflow.add_node("plan_node",      lambda s: plan_node(s, db=db))
    workflow.add_node("crawl_node",     crawl_node)
    workflow.add_node("quality_node",   lambda s: quality_node(s, db=db))
    workflow.add_node("quality_join",   quality_join)
    workflow.add_node("summary_node",   summary_node)

    # ── 注册边 ────────────────────────────────────
    workflow.set_entry_point("plan_node")
    workflow.add_edge("plan_node",    "crawl_node")

    # quality 扇出：crawl_node → quality_router → [N × quality_node] → quality_join
    workflow.add_conditional_edges("crawl_node",    quality_router,  ["quality_node"])
    workflow.add_edge("quality_node", "quality_join")
    workflow.add_conditional_edges("quality_join",  quality_check_edge,
                                   {"summary_router": "summary_router_node", "__end__": END})

    # summary 扇出：summary_router_node → [M × summary_node] → END
    # summary_router_node 是一个包装节点，调用 summary_router() 返回 Send 列表
    workflow.add_node("summary_router_node", lambda s: None)  # 触发扇出的占位节点
    workflow.add_conditional_edges("summary_router_node", summary_router, ["summary_node"])
    workflow.add_edge("summary_node", END)

    return workflow.compile(checkpointer=checkpointer)


# ── 单例工厂（由 AgentCrawlFetcher 调用）─────────────────────────────
_graph_cache: dict = {}

async def get_agent_graph(db_url: str) -> StateGraph:
    if db_url not in _graph_cache:
        checkpointer = await AsyncPostgresSaver.from_conn_string(db_url)
        _graph_cache[db_url] = build_agent_graph(checkpointer=checkpointer)
    return _graph_cache[db_url]
```

---

## 6. AgentCrawlFetcher 调用方式更新

```python
# backend/app/fetchers/agent_crawl.py（修改）
class AgentCrawlFetcher:
    def fetch(self, source: Source) -> list[RawItem]:
        config_model = self._db.get(AgentSourceConfig, source.id)
        if config_model is None:
            return []

        initial_state: AgentState = {
            "source_id": source.id,
            "root_url": source.url,
            "focus_areas": config_model.focus_areas or [],
            "topic_groups": config_model.topic_groups or [],
            "quality_threshold": config_model.quality_threshold,
            "max_urls_per_run": config_model.max_urls_per_run,
            "crawl_workers": config_model.crawl_workers,
            "quality_workers": config_model.quality_workers,
            "summary_workers": config_model.summary_workers,
            # 以下字段由节点填充
            "candidate_links": [], "raw_pages": [], "qualified_pages": [],
            "agent_items": [], "current_page": None, "current_qualified": None,
        }

        thread_config = {"configurable": {"thread_id": f"agent-crawl-{source.id}"}}

        async def _run():
            from app.config import get_settings
            graph = await get_agent_graph(get_settings().database_url)
            result = await graph.ainvoke(initial_state, config=thread_config)
            return result["agent_items"]

        agent_items = asyncio.run(_run())
        return [_to_raw_item(item) for item in agent_items]
```

---

## 7. Streaming 接入（SSE 端点）

```python
# backend/app/api/agent_routes.py（新增端点）
from fastapi.responses import StreamingResponse

@router.post("/{source_id}/run/stream")
async def stream_agent_run(source_id: int, ...):
    """实时 SSE 推送 Agent 运行进度（token 级 streaming）。"""
    async def _event_generator():
        graph = await get_agent_graph(DATABASE_URL)
        async for event in graph.astream_events(initial_state, config, version="v2"):
            kind = event["event"]
            if kind == "on_chat_model_stream":
                token = event["data"]["chunk"].content
                if token:
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
            elif kind == "on_chain_end" and event["name"] in ("plan_node", "crawl_node"):
                yield f"data: {json.dumps({'type': 'node_done', 'node': event['name']})}\n\n"

    return StreamingResponse(_event_generator(), media_type="text/event-stream")
```

---

## 8. 新增依赖

```toml
# backend/pyproject.toml（新增）
"langchain>=0.3",
"langchain-openai>=0.2",
"langgraph>=0.2",
"langgraph-checkpoint-postgres>=0.1",
```

LangChain / LangGraph 均为 **MIT 许可证**，无法务风险。

---

## 9. 文件结构变化

```
backend/app/agent/
├── graph.py          ← 新：StateGraph 定义 + compile（build_agent_graph）
├── nodes.py          ← 新：plan_node / crawl_node / quality_node / summary_node 函数
├── llm.py            ← 新：make_chat_model() 工厂
├── plan_agent.py     ← 保留：URL 发现逻辑 + 确定性验证；新增 plan_async()
├── crawl_dag.py      ← 完全不动
├── quality_pool.py   ← 重构：提取 QUALITY_PROMPT 为模块级变量；Pool 类可废弃或保留兼容
├── summary_pool.py   ← 重构：提取 SUMMARY_PROMPT；同上
├── schemas.py        ← 新增 AgentState TypedDict；保留现有 dataclass
└── site_memory.py    ← 完全不动
```

---

## 10. 测试策略

| 类型 | 覆盖 |
|------|------|
| 单元测试 | 每个 node 函数独立测试（mock LangChain chain）；quality_check_edge 条件跳转；quality_router / summary_router 扇出数量 |
| 图集成测试 | 用 `MemorySaver` + mock node 验证图整体流向；验证 qualified_pages 为空时走 END |
| Checkpointing 测试 | 模拟 crawl_node 崩溃后从 checkpoint 恢复，验证不重跑 plan_node |
| Streaming 测试 | 验证 `astream_events` 正确过滤 `on_chat_model_stream` 事件 |

---

## 11. 迁移路径（不破坏现有功能）

1. 先安装依赖，并行保留现有 `AgentCrawlFetcher` 的 `asyncio.run(_pipeline())` 路径
2. 在 `app/agent/` 新建 `graph.py` / `nodes.py` / `llm.py`，通过环境变量 `USE_LANGGRAPH=1` 启用
3. 完整测试通过后，移除旧路径
4. Enricher / DigestAgent / ProfileAdvisor 保持不变（可按需后续迁移）
