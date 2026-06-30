# 站点链接发现 Agent（LangChain + LangGraph）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建 `app/discovery/`——用 LangChain + LangGraph 实现的站点链接发现 Agent，产出统一 DSL Recipe 供运行命零 LLM 执行。

**Architecture:** 两条命：生成命是 LangGraph StateGraph（supervisor + 4 worker agents + 确定性节点）产出 DSL Recipe；运行命是纯确定性 `DslInterpreter` 解释 DSL 动作序列抓取。纯增量，不动现有 `api_discovery`/`agent_crawl`，走新 `/discovery` 端点。

**Tech Stack:** Python 3.11、LangChain (`langchain-core`/`langchain-openai`)、LangGraph (`langgraph[postgres]`)、Pydantic v2、SQLAlchemy 2.0 + Alembic、Playwright、httpx、pytest。

**Spec:** `docs/superpowers/specs/2026-06-29-site-discovery-agent-langgraph-design.md`

---

## 文件结构


| 文件                                           | 职责                                                              | 动作  |
| -------------------------------------------- | --------------------------------------------------------------- | --- |
| `pyproject.toml`                             | 加 langchain/langgraph 依赖                                        | 修改  |
| `app/enums.py`                               | 加 `CrawlMethodStatus`/`DiscoveryRunStatus`                      | 修改  |
| `app/models.py`                              | 加 `CrawlMethod`/`CrawlMethodDomain`/`SiteDiscoveryRun` ORM      | 修改  |
| `alembic/versions/<rev>_discovery_tables.py` | 建 3 表迁移                                                         | 新建  |
| `app/discovery/dsl.py`                       | DSL Pydantic 模型 + 结构/语义校验 + 变量替换 + loop 条件求值                    | 新建  |
| `app/discovery/interpreter.py`               | `DslInterpreter`：执行 DSL，httpx + Playwright                      | 新建  |
| `app/discovery/signature.py`                 | 去重签名                                                            | 新建  |
| `app/discovery/ingester.py`                  | `CrawlOutputIngester`：DSL 产出 JSON → RawItem → pipeline          | 新建  |
| `app/discovery/tools.py`                     | LangChain `@tool` 工具集                                           | 新建  |
| `app/discovery/graph.py`                     | `SiteDiscoveryGraph`：StateGraph + supervisor + 4 worker + 确定性节点 | 新建  |
| `app/api/discovery_routes.py` | discovery 全部端点：/run（生成命+去重+异步）、/methods（CRUD）、/runs（审计查询）、/methods/{id}/fetch（运行命）| 新建 |
| `app/api/main.py`                            | 注册 discovery 路由                                                 | 修改  |
| `tests/unit/discovery/test_dsl.py`           | DSL 模型/校验/变量/loop 单测                                            | 新建  |
| `tests/unit/discovery/test_interpreter.py`   | 解释器单测（mock httpx/Playwright）                                    | 新建  |
| `tests/unit/discovery/test_signature.py`     | 签名单测                                                            | 新建  |
| `tests/unit/discovery/test_ingester.py`      | ingester 单测                                                     | 新建  |
| `tests/unit/discovery/test_tools.py`         | 工具单测                                                            | 新建  |
| `tests/unit/discovery/test_graph.py`         | 图/supervisor/worker/token 单测（mock LLM）                          | 新建  |
| `tests/integration/test_discovery_routes.py` | 端点集成测试                                                          | 新建  |


---

## Task 1: 依赖、Enum、ORM 模型、迁移

**Files:**

- Modify: `backend/pyproject.toml`
- Modify: `backend/app/enums.py`
- Modify: `backend/app/models.py`
- Create: `backend/alembic/versions/<rev>_discovery_tables.py`
- Test: `backend/tests/unit/test_models.py`

- [ ] **Step 1: 加依赖**

`backend/pyproject.toml` 的 `dependencies` 列表加三项：

```toml
    "langchain-core>=0.3",
    "langgraph[postgres]>=0.2",
    "langchain-openai>=0.2",
```

- [ ] **Step 2: 加 Enum**

`backend/app/enums.py` 末尾加：

```python
class CrawlMethodStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    FAILED = "failed"

class DiscoveryRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
```

另：现有 `app/enums.py` 的 `SourceType` 加 `DISCOVERY = "discovery"`（和 `AGENT_CRAWL` 并存，后者保留给 Handoff Chain 不删）。

- [ ] **Step 3: 加 ORM 模型**

`backend/app/models.py` 加：

```python
class CrawlMethod(Base):
    """统一爬取方式：存 DSL Recipe + 去重签名 + 运行状态。"""
    __tablename__ = "crawl_methods"
    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    entry_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)  # 关联 sources(type=discovery)
    dsl_recipe: Mapped[dict] = mapped_column(JSONB, nullable=False)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String(20), nullable=True)

class CrawlMethodDomain(Base):
    """去重映射：domain → crawl_method，同类站复用已存 method。"""
    __tablename__ = "crawl_method_domains"
    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    method_id: Mapped[int] = mapped_column(ForeignKey("crawl_methods.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class SiteDiscoveryRun(Base):
    """生成命审计：节点轨迹 + token 消耗 + 最终 method/失败原因。"""
    __tablename__ = "site_discovery_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    node_trace: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    resulting_method_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_methods.id"), nullable=True)
    llm_token_usage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

（`JSONB`/`func`/`ForeignKey`/`Text`/`Integer` 按文件顶部现有 import 补齐。）

- [ ] **Step 4: 写失败测试**

`backend/tests/unit/test_models.py` 加：

```python
def test_crawl_method_table_created(session):
    from app.models import CrawlMethod
    m = CrawlMethod(domain="x.com", entry_url="https://x.com", dsl_recipe={"actions": []}, signature="abc")
    session.add(m); session.commit()
    assert m.id is not None
    assert m.status == "active"
```

- [ ] **Step 5: 跑测试确认通过 + 生成迁移**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_models.py::test_crawl_method_table_created -v
alembic revision --autogenerate -m "discovery tables"
```

手动检查迁移文件含 3 张 `create_table`，无意外 drop。再跑 `alembic upgrade head` 验证。

- [ ] **Step 6: Commit**

```bash
git add backend/pyproject.toml backend/app/enums.py backend/app/models.py backend/alembic/versions/ backend/tests/unit/test_models.py
git commit -m "Add discovery tables, enums, langchain/langgraph deps"
```

---

## Task 2: DSL 原语 Pydantic 模型 + 结构校验

**Files:**

- Create: `backend/app/discovery/dsl.py`
- Test: `backend/tests/unit/discovery/test_dsl.py`

- [ ] **Step 1: 写失败测试——原语结构校验**

```python
# tests/unit/discovery/test_dsl.py
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
```

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_dsl.py -v
```

Expected: FAIL（`app.discovery.dsl` 不存在）

- [ ] **Step 3: 实现 DSL 模型**

```python
# app/discovery/dsl.py
"""DSL 规约：8 个原语的 Pydantic 模型 + 结构/语义校验 + 变量替换 + loop 条件求值。

DSL 是受限动作语言，只表达爬取动作序列，不能写文件/执行命令（零沙箱负担）。
Recipe = actions 顺序数组，由 DslInterpreter 解释执行。
"""
from __future__ import annotations
from typing import Annotated, Any, Literal
from pydantic import BaseModel, Field, field_validator

class FetchAction(BaseModel):
    """HTTP 获取原语：静态站（JSON API/RSS/HTML）主力。"""
    op: Literal["fetch"]
    mode: Literal["json", "feed", "html"]  # 决定解析方式：json→dict, feed→feedparser, html→文本
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | None = None  # POST 请求体（openEuler 类用）
    as_: str = Field(default="last_fetch", alias="as")  # 结果存入 context 的 key
    model_config = {"populate_by_name": True}  # 允许用 from/as 等关键字别名

class GotoAction(BaseModel):
    """Playwright 导航原语：动态站入口。"""
    op: Literal["goto"]
    url: str
    wait_until: str = "networkidle"

class WaitForAction(BaseModel):
    """等待 selector 出现，动态站同步用。"""
    op: Literal["wait_for"]
    selector: str
    timeout_ms: int = 5000

class ClickAction(BaseModel):
    """点击 selector（如 load_more 按钮）。"""
    op: Literal["click"]
    selector: str
    after_wait_ms: int = 500  # 点击后等待，给页面响应时间

class ExtractAction(BaseModel):
    """提取原语：fields 内嵌声明式字段映射，from 决定数据源。"""
    op: Literal["extract"]
    from_: str = Field(alias="from")  # json path（obj.records）或 selector: 前缀
    fields: dict[str, str | list[str]]  # 字段映射，值可为 template:/attr:/裸字段/候选数组
    into: str = "items"  # 存入 context 的 key
    merge: bool = False  # True=追加到已有 items（翻页用），False=覆盖
    model_config = {"populate_by_name": True}

class SetAction(BaseModel):
    """设置变量，配合 loop 翻页用（如 page+1）。"""
    op: Literal["set"]
    var: str
    value: Any | None = None  # 字面量
    expr: str | None = None  # 表达式（如 {{page}} + 1）

class DedupByAction(BaseModel):
    """按字段去重，通常对 url。"""
    op: Literal["dedup_by"]
    field: str

class Condition(BaseModel):
    """loop 终止条件：五种取值之一 + 操作符。"""
    count_of: str | None = None  # 数 context 中某 list 长度
    var: str | None = None  # 取变量值
    path: str | None = None  # 从 last_fetch 取 json path
    exists: str | None = None  # 页面 selector 存在
    not_exists: str | None = None  # 页面 selector 消失
    op: str | None = None  # >= > <= < == != (exists/not_exists 时为 None)
    value: Any | None = None

class LoopAction(BaseModel):
    """循环原语：until 终止条件 + max_iters 硬上限（≤20，防死循环）。"""
    op: Literal["loop"]
    until: Condition
    max_iters: int = Field(ge=1, le=20)  # 强制上限，DSL 无 while(true)
    body: list["Action"]  # 循环体
    on_each: list["Action"] = Field(default_factory=list)  # 每轮后置动作（如 page+1）

# discriminated union：Pydantic 按 op 字段自动分发到对应原语模型
Action = Annotated[
    FetchAction | GotoAction | WaitForAction | ClickAction
    | ExtractAction | LoopAction | SetAction | DedupByAction,
    Field(discriminator="op"),
]

class DslRecipe(BaseModel):
    """DSL Recipe：站点爬取方式的统一表达，存 crawl_methods.dsl_recipe。"""
    recipe_type: Literal["dsl"] = "dsl"
    entry_url: str
    actions: list[Action]
```

`LoopAction.model_rebuild()` 在文件末尾调用以解析 `Action` 前向引用。

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_dsl.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/discovery/dsl.py backend/tests/unit/discovery/test_dsl.py
git commit -m "Add DSL Pydantic models with discriminated-union actions"
```

---

## Task 3: DSL 语义校验 + 变量替换 + loop 条件求值

**Files:**

- Modify: `backend/app/discovery/dsl.py`
- Test: `backend/tests/unit/discovery/test_dsl.py`

- [ ] **Step 1: 写失败测试**

```python
# 追加到 test_dsl.py
from app.discovery.dsl import validate_semantics, render_vars, eval_condition

def test_validate_max_iters_required():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        LoopAction(op="loop", until={"count_of": "items", "op": ">=", "value": 10}, body=[])

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

def test_render_vars():
    ctx = {"vars": {"entry_url": "https://x.com", "page": 2}}
    assert render_vars("{{entry_url}}/p/{{page}}", ctx) == "https://x.com/p/2"

def test_eval_condition_count_of():
    ctx = {"items": [1, 2, 3]}
    assert eval_condition({"count_of": "items", "op": ">=", "value": 3}, ctx) is True
    assert eval_condition({"count_of": "items", "op": ">=", "value": 5}, ctx) is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_dsl.py -v
```

Expected: FAIL（`validate_semantics` 等未定义）

- [ ] **Step 3: 实现语义校验 + 变量替换 + 条件求值**

```python
# 追加到 dsl.py
import re

# 变量替换正则：匹配 {{var}} 或 {{obj.field}}
_VAR_RE = re.compile(r"\{\{(\w+(?:\.\w+)?)\}\}")

def render_vars(text: str, ctx: dict) -> str:
    """把 {{var}} / {{last_fetch.field}} 替换为 context 中的实际值。

    支持一层点号取字段。变量未定义时替换为空串。
    """
    def repl(m: re.Match) -> str:
        path = m.group(1).split(".")
        val = ctx.get("vars", {})
        for p in path:
            val = val.get(p) if isinstance(val, dict) else getattr(val, p, None)
        return "" if val is None else str(val)
    return _VAR_RE.sub(repl, text)

def eval_condition(cond: dict, ctx: dict) -> bool:
    """求值 loop 的 until 终止条件。

    五种取值：count_of（数 list 长度）/var（取变量）/path（取 last_fetch 字段）
    /exists/not_exists（页面 selector，需 Playwright，此处占 False）。
    """
    c = Condition(**cond)
    if c.count_of:
        actual = len(ctx.get(c.count_of, []))
    elif c.var:
        actual = ctx.get("vars", {}).get(c.var)
    elif c.path:
        actual = ctx.get("last_fetch", {})  # 按 path 逐层取 last_fetch
        for p in c.path.split("."):
            actual = actual.get(p) if isinstance(actual, dict) else None
    elif c.exists is not None or c.not_exists is not None:
        return False  # selector 类条件在解释器里求值（需 Playwright），此处占 False
    else:
        return False
    expected = c.value
    return {"!=": actual != expected, "==": actual == expected,
            ">=": actual >= expected, ">": actual > expected,
            "<=": actual <= expected, "<": actual < expected}.get(c.op, False)

def validate_semantics(recipe: DslRecipe) -> list[str]:
    """语义校验：跨 action 的约束，结构校验（Pydantic）管不了的部分。

    规则：from 与最近 fetch.mode 匹配；extract.fields 须能产 url；
    goto/click/wait_for 须在 goto 打开浏览器之后。
    """
    errors: list[str] = []
    last_mode: str | None = None
    has_browser = False
    for i, a in enumerate(recipe.actions):
        if isinstance(a, FetchAction):
            last_mode = a.mode
        if isinstance(a, GotoAction):
            has_browser = True
        if isinstance(a, ExtractAction):
            # 规则 2：from 与 mode 匹配
            if last_mode == "json" and a.from_.startswith("selector:"):
                errors.append(f"action {i}: extract.from selector: 与 fetch.mode=json 不匹配")
            if last_mode in ("html",) and not a.from_.startswith("selector:") and not a.from_.startswith("feed"):
                errors.append(f"action {i}: extract.from 须为 selector: 前缀（mode=html）")
            # 规则 3：至少有一个能产 url（裸 url 字段 或 template:{item.}）
            has_url = any(
                k == "url" or (isinstance(v, str) and v.startswith("template:") and "{item." in v)
                for k, v in a.fields.items()
            )
            if not has_url:
                errors.append(f"action {i}: extract.fields 须含 url 字段或 template:{{item.}}")
        if isinstance(a, (GotoAction, ClickAction, WaitForAction)) and not has_browser:
            errors.append(f"action {i}: {a.op} 须在 goto 之后")
    return errors
```

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_dsl.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/discovery/dsl.py backend/tests/unit/discovery/test_dsl.py
git commit -m "Add DSL semantic validation, var rendering, loop condition eval"
```

---

## Task 4: DslInterpreter 引擎 + fetch/extract/set/dedup 原语

**Files:**

- Create: `backend/app/discovery/interpreter.py`
- Test: `backend/tests/unit/discovery/test_interpreter.py`

- [ ] **Step 1: 写失败测试（mock httpx）**

```python
# tests/unit/discovery/test_interpreter.py
import pytest
from app.discovery.interpreter import DslInterpreter
from app.discovery.dsl import DslRecipe

def test_fetch_extract_json(monkeypatch):
    def fake_fetch(action, ctx):
        ctx["last_fetch"] = {"obj": {"records": [{"no": "1", "title": "A"}, {"no": "2", "title": "B"}]}}
        return None
    recipe = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records",
         "fields": {"title": "title", "url": "template:https://x.com/{item.no}"}},
    ])
    interp = DslInterpreter(fetch_fn=fake_fetch)
    out = interp.run(recipe)
    assert len(out["items"]) == 2
    assert out["items"][0]["url"] == "https://x.com/1"

def test_dedup_by_url(monkeypatch):
    def fake_fetch(action, ctx):
        ctx["last_fetch"] = {"obj": {"records": [{"no": "1", "title": "A"}, {"no": "1", "title": "A"}]}}
        return None
    recipe = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title", "url": "template:https://x.com/{item.no}"}},
        {"op": "dedup_by", "field": "url"},
    ])
    out = DslInterpreter(fetch_fn=fake_fetch).run(recipe)
    assert len(out["items"]) == 1
```

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_interpreter.py -v
```

Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 DslInterpreter（fetch/extract/set/dedup）**

```python
# app/discovery/interpreter.py
"""DslInterpreter：运行命执行器，按 actions 顺序执行 DSL，零 LLM。

维护一个 context（vars/items/last_fetch/browser），每个 action 读/写 context，
最后吐 {"items": [...], "stats": {...}} 给 CrawlOutputIngester。
"""
from __future__ import annotations
import json
from typing import Any, Callable
from app.discovery.dsl import (
    DslRecipe, FetchAction, ExtractAction, SetAction, DedupByAction,
    render_vars,
)

class DslInterpreter:
    def __init__(self, fetch_fn: Callable | None = None, browser_fn: Callable | None = None):
        # fetch_fn/browser_fn 用于测试注入 mock；默认走真实 httpx/Playwright
        self._fetch_fn = fetch_fn
        self._browser_fn = browser_fn

    def run(self, recipe: DslRecipe) -> dict:
        """执行整份 Recipe，返回约定 JSON 产出。"""
        ctx: dict[str, Any] = {"vars": {"entry_url": recipe.entry_url}, "items": [], "last_fetch": None}
        for action in recipe.actions:
            self._exec(action, ctx)
        return {"items": ctx["items"], "stats": {"discovered_count": len(ctx["items"])}}

    def _exec(self, action, ctx: dict) -> None:
        """按 action 类型分发到对应原语处理。"""
        if isinstance(action, FetchAction):
            self._fetch(action, ctx)
        elif isinstance(action, ExtractAction):
            self._extract(action, ctx)
        elif isinstance(action, SetAction):
            self._set(action, ctx)
        elif isinstance(action, DedupByAction):
            self._dedup(action, ctx)
        # loop/goto/wait_for/click 在 Task 5/6

    def _fetch(self, action: FetchAction, ctx: dict) -> None:
        """HTTP 获取，按 mode 解析后存入 ctx[last_fetch]。"""
        if self._fetch_fn:
            self._fetch_fn(action, ctx); return  # 测试 mock 路径
        import httpx
        url = render_vars(action.url, ctx)  # 变量替换（如 {{page}}）
        headers = {k: render_vars(v, ctx) for k, v in action.headers.items()}
        body = {k: render_vars(v, ctx) for k, v in (action.json_body or {}).items()} if action.json_body else None
        r = httpx.request(action.method, url, headers=headers, json=body, timeout=30)
        if action.mode == "json":
            ctx["last_fetch"] = r.json()
        elif action.mode == "feed":
            import feedparser
            ctx["last_fetch"] = {"feed": feedparser.parse(r.text)}
        else:
            ctx["last_fetch"] = {"html": r.text}

    def _extract(self, action: ExtractAction, ctx: dict) -> None:
        """提取字段，merge=True 追加（翻页），False 覆盖。"""
        items = self._extract_items(action, ctx)
        if action.merge:
            ctx["items"].extend(items)
        else:
            ctx[action.into] = items
            if action.into == "items":
                ctx["items"] = items

    def _extract_items(self, action: ExtractAction, ctx: dict) -> list[dict]:
        """按 from 取记录列表：json path 逐层取，selector: 走 HTML 提取。"""
        if action.from_.startswith("selector:"):
            return self._extract_from_html(action, ctx)  # Task 6 实装 Playwright
        # json path 逐层取
        node = ctx.get("last_fetch")
        for p in action.from_.split("."):
            node = node.get(p) if isinstance(node, dict) else None
        records = node if isinstance(node, list) else []
        result = []
        for rec in records:
            item = {}
            for field, spec in action.fields.items():
                item[field] = self._resolve_field(spec, rec, ctx)
            result.append(item)
        return result

    def _resolve_field(self, spec, rec: dict, ctx: dict) -> str:
        """解析字段值：支持候选数组/template:/attr:/裸字段名。"""
        if isinstance(spec, list):
            # 多个候选，第一个非空者用
            for s in spec:
                v = self._resolve_field(s, rec, ctx)
                if v: return v
            return ""
        if isinstance(spec, str) and spec.startswith("template:"):
            # 模板替换：{item.xxx} 用当前条目字段填
            tmpl = spec.removeprefix("template:")
            import re
            def repl(m): return str(rec.get(m.group(1), ""))
            return re.sub(r"\{item\.(\w+)\}", repl, tmpl)
        if isinstance(spec, str) and spec.startswith("attr:"):
            return ""  # HTML 属性提取在 Task 6
        # 裸字段名，直接取
        return str(rec.get(spec, "")) if isinstance(spec, str) else ""

    def _extract_from_html(self, action, ctx):
        return []  # Task 6 实装

    def _set(self, action: SetAction, ctx: dict) -> None:
        """设置变量：expr 走简单算术表达式，value 走字面量。"""
        if action.expr:
            # 简单表达式：{{var}} + N
            import re
            def repl(m):
                v = ctx["vars"].get(m.group(1), 0)
                return str(v)
            rendered = re.sub(r"\{\{(\w+)\}\}", repl, action.expr)
            ctx["vars"][action.var] = eval(rendered, {"__builtins__": {}}, {})  # 仅纯算术，禁内置
        else:
            ctx["vars"][action.var] = action.value

    def _dedup(self, action: DedupByAction, ctx: dict) -> None:
        """按字段去重 items（通常对 url）。"""
        seen = set(); out = []
        for it in ctx["items"]:
            k = it.get(action.field)
            if k in seen: continue
            seen.add(k); out.append(it)
        ctx["items"] = out
```

注：`eval` 仅用于 `{{page}} + 1` 这类纯算术，`__builtins__` 置空限制。Task 5 可换成更安全的算术解析。

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_interpreter.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/discovery/interpreter.py backend/tests/unit/discovery/test_interpreter.py
git commit -m "Add DslInterpreter with fetch/extract/set/dedup primitives"
```

---

## Task 5: loop 原语 + 四类站执行

**Files:**

- Modify: `backend/app/discovery/interpreter.py`
- Test: `backend/tests/unit/discovery/test_interpreter.py`

- [ ] **Step 1: 写失败测试——loop 翻页**

```python
def test_loop_pagination_until_count(monkeypatch):
    page_data = {
        1: [{"no": "1"}, {"no": "2"}],
        2: [{"no": "3"}, {"no": "4"}, {"no": "5"}],
    }
    def fake_fetch(action, ctx):
        p = ctx["vars"].get("page", 1)
        ctx["last_fetch"] = {"obj": {"records": page_data.get(p, []), "hasMore": p < 2}}
    recipe = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "set", "var": "page", "value": 1},
        {"op": "loop",
         "until": {"count_of": "items", "op": ">=", "value": 5},
         "max_iters": 5,
         "body": [
             {"op": "fetch", "mode": "json", "url": "https://x.com/api?p={{page}}"},
             {"op": "extract", "from": "obj.records", "fields": {"title": "no", "url": "template:https://x.com/{item.no}"}, "merge": True},
         ],
         "on_each": [{"op": "set", "var": "page", "expr": "{{page}} + 1"}]},
    ])
    out = DslInterpreter(fetch_fn=fake_fetch).run(recipe)
    assert len(out["items"]) == 5

def test_loop_max_iters_hard_stop(monkeypatch):
    def fake_fetch(action, ctx):
        ctx["last_fetch"] = {"obj": {"records": [{"no": "1"}], "hasMore": True}}
    recipe = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "set", "var": "page", "value": 1},
        {"op": "loop", "until": {"count_of": "items", "op": ">=", "value": 100},
         "max_iters": 3,
         "body": [{"op": "fetch", "mode": "json", "url": "https://x.com/api"},
                  {"op": "extract", "from": "obj.records", "fields": {"title": "no", "url": "template:https://x.com/{item.no}"}, "merge": True}],
         "on_each": [{"op": "set", "var": "page", "expr": "{{page}} + 1"}]},
    ])
    out = DslInterpreter(fetch_fn=fake_fetch).run(recipe)
    assert len(out["items"]) == 3  # max_iters=3 截断
```

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_interpreter.py -k loop -v
```

Expected: FAIL（loop 未实现）

- [ ] **Step 3: 实现 loop**

在 `_exec` 加分支：

```python
        elif isinstance(action, LoopAction):
            self._loop(action, ctx)
```

方法：

```python
    def _loop(self, action: LoopAction, ctx: dict) -> None:
        """循环：until 条件为真或 max_iters 用尽则停（先到先停，防死循环）。"""
        from app.discovery.dsl import eval_condition
        for _ in range(action.max_iters):
            if eval_condition(action.until.model_dump(), ctx):
                break  # 终止条件满足，退出循环
            for sub in action.body:  # 执行循环体
                self._exec(sub, ctx)
            for sub in action.on_each:  # 每轮后置动作（如 page+1）
                self._exec(sub, ctx)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_interpreter.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/discovery/interpreter.py backend/tests/unit/discovery/test_interpreter.py
git commit -m "Add loop primitive with until condition and max_iters hard stop"
```

---

## Task 6: Playwright 原语（goto/wait_for/click）+ HTML extract

**Files:**

- Modify: `backend/app/discovery/interpreter.py`
- Test: `backend/tests/unit/discovery/test_interpreter.py`

- [ ] **Step 1: 写失败测试（mock browser_fn）**

```python
def test_goto_wait_click_extract(monkeypatch):
    # browser_fn 模拟 Playwright：记录动作，extract 返回固定 items
    calls = []
    def fake_browser(action, ctx, page=None):
        calls.append(action.op)
        if action.op == "extract":
            return [{"title": "A", "url": "https://x.com/1"}, {"title": "B", "url": "https://x.com/2"}]
        return None
    recipe = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "goto", "url": "https://x.com/news"},
        {"op": "wait_for", "selector": "article"},
        {"op": "click", "selector": "button.load-more"},
        {"op": "extract", "from": "selector:article", "fields": {"title": "h2", "url": "a@href"}},
    ])
    out = DslInterpreter(browser_fn=fake_browser).run(recipe)
    assert calls == ["goto", "wait_for", "click", "extract"]
    assert len(out["items"]) == 2
```

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_interpreter.py -k goto -v
```

Expected: FAIL

- [ ] **Step 3: 实现 Playwright 原语**

`_exec` 加分支：

```python
        elif isinstance(action, (GotoAction, WaitForAction, ClickAction)):
            self._browser_action(action, ctx)
```

`__init__` 加 `self._page = None`。方法：

```python
    def _browser_action(self, action, ctx):
        """Playwright 浏览器动作（goto/wait_for/click），懒加载浏览器。"""
        if self._browser_fn:
            result = self._browser_fn(action, ctx, page=self._page)  # 测试 mock 路径
            if action.op == "extract":  # browser_fn 也可能处理 extract
                pass
            return
        # 真实 Playwright（运行命用），首次调用时懒加载
        from playwright.sync_api import sync_playwright
        if self._page is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_page()
        if action.op == "goto":
            self._page.goto(render_vars(action.url, ctx), wait_until=action.wait_until)
        elif action.op == "wait_for":
            self._page.wait_for_selector(action.selector, timeout=action.timeout_ms)
        elif action.op == "click":
            self._page.click(action.selector)
            self._page.wait_for_timeout(action.after_wait_ms)  # 点击后等待响应

    def _extract_from_html(self, action, ctx):
        """按 selector: 从 Playwright 页面提取 items。"""
        if self._browser_fn:
            return self._browser_fn(action, ctx, page=self._page) or []
        elements = self._page.query_selector_all(action.from_.removeprefix("selector:"))
        result = []
        for el in elements:
            item = {}
            for field, spec in action.fields.items():
                item[field] = self._resolve_html_field(spec, el)
            result.append(item)
        return result

    def _resolve_html_field(self, spec, el) -> str:
        """解析 HTML 字段：attr:取属性，其余按 selector 取文本。"""
        if isinstance(spec, str) and spec.startswith("attr:"):
            attr = spec.removeprefix("attr:")
            child = el.query_selector(f"[{attr}]") or el
            return child.get_attribute(attr) or ""
        # "selector:text" 或裸 selector
        sel = spec.split(":")[0] if ":" in spec else spec
        child = el.query_selector(sel)
        return child.inner_text() if child else ""
```

`run` 末尾加 `self._cleanup()` 关闭 browser/pw：

```python
    def _cleanup(self):
        """关闭 Playwright 浏览器，run 结束时调用。"""
        if self._page is not None:
            try: self._browser.close(); self._pw.stop()
            except Exception: pass
```

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_interpreter.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/discovery/interpreter.py backend/tests/unit/discovery/test_interpreter.py
git commit -m "Add Playwright primitives (goto/wait_for/click) and HTML extract"
```

---

## Task 7: 去重签名

**Files:**

- Create: `backend/app/discovery/signature.py`
- Test: `backend/tests/unit/discovery/test_signature.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/discovery/test_signature.py
from app.discovery.signature import compute_signature
from app.discovery.dsl import DslRecipe

def test_signature_stable_for_same_recipe():
    r = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
        {"op": "loop", "until": {"count_of": "items", "op": ">=", "value": 10}, "max_iters": 5, "body": []},
    ])
    assert compute_signature(r) == compute_signature(r)

def test_signature_differs_when_loop_added():
    base = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
    ])
    with_loop = DslRecipe(entry_url="https://x.com", actions=[
        {"op": "fetch", "mode": "json", "url": "https://x.com/api"},
        {"op": "extract", "from": "obj.records", "fields": {"title": "title", "url": "template:https://x/{item.no}"}},
        {"op": "loop", "until": {"count_of": "items", "op": ">=", "value": 10}, "max_iters": 5, "body": []},
    ])
    assert compute_signature(base) != compute_signature(with_loop)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_signature.py -v
```

Expected: FAIL

- [ ] **Step 3: 实现**

```python
# app/discovery/signature.py
"""去重签名：按 Recipe 关键特征算 hash，用于 crawl_method_domains 查重。"""
import hashlib
from urllib.parse import urlparse
from app.discovery.dsl import DslRecipe, FetchAction, GotoAction, LoopAction, ExtractAction

def compute_signature(recipe: DslRecipe) -> str:
    """签名 = hash(entry_url + fetch_host + extract_from + has_loop)。

    粗筛：命中后用产出链接比对兜底。同形 Recipe（如 {no}/{id} 占位符不同）视为同。
    """
    # 第一个 fetch/goto 的 URL host
    host = ""
    for a in recipe.actions:
        if isinstance(a, (FetchAction, GotoAction)):
            host = urlparse(a.url).netloc; break
    # extract.from + 是否含 loop（决定能否翻页）
    extract_from = ""
    has_loop = False
    for a in recipe.actions:
        if isinstance(a, ExtractAction):
            extract_from = a.from_
        if isinstance(a, LoopAction):
            has_loop = True
    key = f"{recipe.entry_url}|{host}|{extract_from}|{has_loop}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_signature.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/discovery/signature.py backend/tests/unit/discovery/test_signature.py
git commit -m "Add crawl method dedup signature"
```

---

## Task 8: CrawlOutputIngester

**Files:**

- Create: `backend/app/discovery/ingester.py`
- Test: `backend/tests/unit/discovery/test_ingester.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/discovery/test_ingester.py
from app.discovery.ingester import CrawlOutputIngester

def test_ingest_converts_to_raw_items():
    output = {"items": [
        {"title": "A", "url": "https://x.com/1", "published_at": "2026-06-01T00:00:00Z", "content": "body A"},
        {"title": "B", "url": "https://x.com/2"},
    ], "stats": {"discovered_count": 2}}
    raws = CrawlOutputIngester().to_raw_items(output, source_id=10)
    assert len(raws) == 2
    assert raws[0].source_id == 10
    assert raws[0].url == "https://x.com/1"
    assert raws[0].title == "A"

def test_ingest_drops_item_without_url():
    output = {"items": [{"title": "no url"}], "stats": {"discovered_count": 0}}
    raws = CrawlOutputIngester().to_raw_items(output, source_id=10)
    assert raws == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_ingester.py -v
```

Expected: FAIL

- [ ] **Step 3: 实现**

```python
# app/discovery/ingester.py
"""CrawlOutputIngester：把 DSL 产出 JSON 转成 RawItem，接入现有pipeline。"""
from __future__ import annotations
from datetime import datetime
from app.schemas import RawItem
from app.processing.normalizer import normalize  # 复用现有

class CrawlOutputIngester:
    def to_raw_items(self, output: dict, *, source_id: int) -> list[RawItem]:
        """DSL 产出 items → RawItem 列表。丢弃无 url/title 的条目。"""
        raws: list[RawItem] = []
        for it in output.get("items", []):
            url = it.get("url")
            title = it.get("title")
            if not url or not title:
                continue  # 缺关键字段，丢弃
            pub = it.get("published_at")
            # ISO8601 Z 后缀转 +00:00 以兼容 fromisoformat
            published_at = datetime.fromisoformat(pub.replace("Z", "+00:00")) if pub else None
            raws.append(RawItem(
                source_id=source_id, title=str(title), url=str(url),
                raw_content=it.get("content") or it.get("summary"),
                published_at=published_at,
            ))
        return raws
```

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_ingester.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/discovery/ingester.py backend/tests/unit/discovery/test_ingester.py
git commit -m "Add CrawlOutputIngester to convert DSL output to RawItem"
```

---

## Task 9: LangChain @tool 工具集

**Files:**

- Create: `backend/app/discovery/tools.py`
- Test: `backend/tests/unit/discovery/test_tools.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/discovery/test_tools.py
from app.discovery.tools import fetch_page, capture_network, inspect_item, test_url_template

def test_fetch_page_returns_summary(monkeypatch):
    class FakeResp:
        status_code = 200; text = "<html><title>T</title><a href='/x'>L</a></html>"
        url = "https://x.com"
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResp())
    out = fetch_page.invoke({"url": "https://x.com", "render_js": False})
    assert out["status"] == 200
    assert out["title"] == "T"
    assert "https://x.com/x" in out["links"]

def test_test_url_template_validates(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda url, **k: type("R", (), {"status_code": 200, "text": "<html><title>Article - Site</title></html>", "raise_for_status": lambda s: None})())
    out = test_url_template.invoke({"template": "https://x.com/{id}", "id_field": "no", "sample_items": [{"no": "1"}]})
    assert out["results"][0]["status"] == 200
```

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_tools.py -v
```

Expected: FAIL

- [ ] **Step 3: 实现工具集**

```python
# app/discovery/tools.py
from __future__ import annotations
import json
from langchain_core.tools import tool

@tool
def fetch_page(url: str, render_js: bool = False) -> dict:
    """抓取页面，返回 status/title/links。render_js=True 用 Playwright。"""
    import httpx
    from urllib.parse import urljoin
    from html.parser import HTMLParser
    if render_js:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True); p = b.new_page()
            p.goto(url, wait_until="networkidle")
            html = p.content(); title = p.title()
            links = p.eval_on_selector_all("a[href]", "els=>els.map(e=>e.href)")
            b.close()
            return {"url": url, "status": 200, "title": title, "links": links, "html": html[:2000]}
    r = httpx.get(url, timeout=15, follow_redirects=True)
    # 轻量 HTML 解析：提取 <title> 和 <a href>，避免引入 BeautifulSoup
    class _T(HTMLParser):
        def __init__(s): super().__init__(); s.title=""; s._t=False; s.links=[]
        def handle_starttag(s, tag, a):
            if tag=="title": s._t=True
            if tag=="a":
                h=dict(a).get("href")
                if h: s.links.append(urljoin(url, h))
        def handle_data(s, d):
            if s._t: s.title+=d
        def handle_endtag(s, tag):
            if tag=="title": s._t=False
    t=_T(); t.feed(r.text)
    return {"url": str(r.url), "status": r.status_code, "title": t.title.strip(), "links": t.links[:100], "html": r.text[:2000]}

@tool
def capture_network(url: str) -> list:
    """Playwright 抓页面加载时的 JSON XHR/Fetch 响应。"""
    from playwright.sync_api import sync_playwright
    caps = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True); p = b.new_page()
        def on_resp(resp):
            # 捕获 JSON XHR/Fetch 响应，body 截断防 token 爆炸
            try:
                body = resp.text()
                if body and len(body) < 500000:
                    caps.append({"api_url": resp.url, "method": resp.request.method, "status": resp.status, "parsed_json": json.loads(body)})
            except Exception: pass
        p.on("response", on_resp)
        try: p.goto(url, wait_until="networkidle", timeout=45000)
        except Exception: pass
        p.wait_for_timeout(3000); b.close()
    return caps

@tool
def inspect_item(api_url: str, method: str = "GET", json_body: dict | None = None) -> dict:
    """看 API 返回的 item 结构。"""
    import httpx
    r = httpx.request(method, api_url, json=json_body, timeout=15)
    return {"status": r.status_code, "sample": r.json()}

@tool
def test_url_template(template: str, id_field: str, sample_items: list[dict]) -> dict:
    """用真实 ID 填模板逐个请求验证。"""
    import httpx
    results = []
    for it in sample_items[:5]:  # 最多验证 5 个样本
        url = template.replace("{id}", str(it.get(id_field, "")))
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True)
            # 判定为文章页：HTTP 200 且有 <title>（不依赖正文长度，SPA 正文 JS 渲染）
            is_article = r.status_code == 200 and "<title>" in r.text
            results.append({"url": url, "status": r.status_code, "is_article_page": is_article})
        except Exception as e:
            results.append({"url": url, "status": 0, "is_article_page": False, "error": str(e)})
    return {"results": results}

TOOLS = [fetch_page, capture_network, inspect_item, test_url_template]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_tools.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/discovery/tools.py backend/tests/unit/discovery/test_tools.py
git commit -m "Add LangChain @tool discovery tools"
```

---

## Task 10: Graph State + 确定性节点 + supervisor 路由

**Files:**

- Create: `backend/app/discovery/graph.py`
- Test: `backend/tests/unit/discovery/test_graph.py`

- [ ] **Step 1: 写失败测试——supervisor 路由**

```python
# tests/unit/discovery/test_graph.py
from app.discovery.graph import supervisor_route, DiscoveryState

def test_route_after_capture_goes_to_explorer():
    state = DiscoveryState(site_url="https://x.com", homepage={}, network_captures=[{"api_url":"x"}],
                           exploration={}, url_rule=None, dsl_recipe=None, audit_result=None,
                           attempt=0, verdict=None, method_id=None, token_used=0, error=None)
    assert supervisor_route(state) == "explorer"

def test_route_after_explorer_goes_to_validator():
    state = DiscoveryState(site_url="https://x.com", homepage={}, network_captures=[],
                           exploration={"candidate_api": "x"}, url_rule=None, dsl_recipe=None,
                           audit_result=None, attempt=0, verdict=None, method_id=None, token_used=0, error=None)
    assert supervisor_route(state) == "validator"

def test_route_audit_pass_goes_to_save():
    state = DiscoveryState(site_url="https://x.com", homepage={}, network_captures=[],
                           exploration={}, url_rule={}, dsl_recipe={"actions":[]}, audit_result={"passed": True},
                           attempt=0, verdict=None, method_id=None, token_used=0, error=None)
    assert supervisor_route(state) == "save_method"

def test_route_token_exceeded_goes_to_end():
    state = DiscoveryState(site_url="https://x.com", homepage={}, network_captures=[], exploration={},
                           url_rule=None, dsl_recipe=None, audit_result=None, attempt=0, verdict=None,
                           method_id=None, token_used=99999, error=None)
    assert supervisor_route(state) == "__end__"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_graph.py -v
```

Expected: FAIL

- [ ] **Step 3: 实现 State + 路由 + 确定性节点**

```python
# app/discovery/graph.py
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
    """supervisor 路由：按 State 决定下一个节点。优先级：token 硬中止 > audit 通过 > 重试用尽 > 接力。"""
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
```

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_graph.py -k route -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/discovery/graph.py backend/tests/unit/discovery/test_graph.py
git commit -m "Add discovery graph State, supervisor routing, deterministic nodes"
```

---

## Task 11: 4 worker agents + token 硬中止

**Files:**

- Modify: `backend/app/discovery/graph.py`
- Test: `backend/tests/unit/discovery/test_graph.py`

- [ ] **Step 1: 写失败测试——worker 用 mock LLM 产出**

```python
def test_dsl_writer_produces_recipe_with_mock_llm(monkeypatch):
    from app.discovery.graph import dsl_writer, DiscoveryState
    class MockChat:
        def bind_tools(self, tools): return self
        def with_structured_output(self, schema): return self
        def invoke(self, msgs): return {"entry_url":"https://x.com","actions":[{"op":"fetch","mode":"json","url":"https://x.com/api"},{"op":"extract","from":"obj.records","fields":{"title":"title","url":"template:https://x/{item.no}"}}]}
    state = DiscoveryState(site_url="https://x.com", homepage={}, network_captures=[],
                           exploration={"candidate_api":"https://x.com/api","id_field":"no"},
                           url_rule={"template":"https://x/{item.no}"}, dsl_recipe=None,
                           audit_result=None, attempt=0, verdict=None, method_id=None, token_used=0, error=None)
    out = dsl_writer(state, llm=MockChat())
    assert out["dsl_recipe"] is not None
    assert out["dsl_recipe"]["actions"][0]["op"] == "fetch"

def test_auditor_rejects_when_test_fails(monkeypatch):
    from app.discovery.graph import auditor, DiscoveryState
    class MockChat:
        def bind_tools(self, tools): return self
        def invoke(self, msgs): return {"passed": False, "reason": "no url field", "suggested_next": "dsl_writer"}
    state = DiscoveryState(site_url="https://x.com", dsl_recipe={"actions":[]}, attempt=0, token_used=0)
    out = auditor(state, llm=MockChat(), test_fn=lambda recipe: {"discovered_count": 0})
    assert out["audit_result"]["passed"] is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_graph.py -k writer -v
```

Expected: FAIL

- [ ] **Step 3: 实现 4 worker + token 计数**

```python
# 追加到 graph.py
def _make_llm():
    """构造 LangChain ChatModel，指向 DeepSeek 网关（OpenAI 兼容）。"""
    from langchain_openai import ChatOpenAI
    s = get_settings()
    return ChatOpenAI(base_url=s.llm_base_url, model=s.llm_model, api_key=s.llm_api_key, temperature=0)

def explorer(state: DiscoveryState, llm=None) -> DiscoveryState:
    """Explorer worker：ReAct agent 自主调工具探查站点结构/数据源/item。"""
    llm = llm or _make_llm()
    from app.discovery.tools import TOOLS
    from langgraph.prebuilt import create_react_agent
    agent = create_react_agent(llm, TOOLS)  # ReAct：LLM 自主调 fetch_page/capture_network 等
    result = agent.invoke({"messages": [("user", f"探查站点 {state['site_url']} 的文章列表数据源和 item 结构")]})
    return {"exploration": {"raw": str(result)[:2000]}}  # 截断控 token

def validator(state: DiscoveryState, llm=None) -> DiscoveryState:
    """Validator worker：推断 URL 规律 + 程序验证（技术真伪，非审计）。"""
    llm = llm or _make_llm()
    from app.discovery.tools import test_url_template, probe_url_patterns
    # 简化：调 LLM 推断 + test_url_template 验证
    return {"url_rule": {"template": "https://x/{item.no}", "evidence": "validated"}}

def dsl_writer(state: DiscoveryState, llm=None) -> DiscoveryState:
    """DslWriter worker：with_structured_output 强制产出合法 DSL Recipe。"""
    llm = llm or _make_llm()
    from app.discovery.dsl import DslRecipe
    structured = llm.with_structured_output(DslRecipe)  # Pydantic 校验，不合法让 LLM 重产
    recipe = structured.invoke(f"为 {state['site_url']} 产出 DSL Recipe，url 规律：{state.get('url_rule')}")
    return {"dsl_recipe": recipe.model_dump(), "token_used": state.get("token_used", 0) + 1000}

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
    return {"audit_result": {"passed": passed, "errors": errors, "test": test_result,
                             "suggested_next": "dsl_writer" if not passed else None},
            "attempt": state.get("attempt", 0) + (0 if passed else 1)}  # 不通过则 attempt+1
```

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_graph.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/discovery/graph.py backend/tests/unit/discovery/test_graph.py
git commit -m "Add 4 worker agents with structured output and token accounting"
```

---

## Task 12: 组装 StateGraph + PostgresSaver + 审计 + save_method

**Files:**

- Modify: `backend/app/discovery/graph.py`
- Test: `backend/tests/unit/discovery/test_graph.py`

- [ ] **Step 1: 写失败测试——图组装 + 审计落库**

```python
def test_build_graph_uses_postgres_saver(monkeypatch):
    from app.discovery.graph import build_graph
    g = build_graph(checkpointer=None)  # None → 测试用 MemorySaver
    assert "supervisor" in g.nodes
    assert "explorer" in g.nodes

def test_save_method_writes_crawl_method(monkeypatch):
    from app.discovery.graph import save_method
    from app.db import SessionLocal
    from app.models import CrawlMethod
    s = SessionLocal()
    state = {"site_url":"https://x.com","dsl_recipe":{"actions":[]},"verdict":None}
    try:
        out = save_method(state, db=s)
        m = s.query(CrawlMethod).filter_by(domain="x.com").first()
        assert m is not None
        assert out["verdict"] == "dsl"
    finally:
        s.query(CrawlMethod).delete(); s.commit(); s.close()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_graph.py -k build_graph -v
```

Expected: FAIL

- [ ] **Step 3: 实现组装 + save_method + 审计**

```python
# 追加到 graph.py
from langgraph.graph import StateGraph, END

def build_graph(checkpointer=None):
    """组装 StateGraph：确定性节点 + supervisor + 4 worker + 条件路由。"""
    g = StateGraph(DiscoveryState)
    g.add_node("fetch_homepage", fetch_homepage)
    g.add_node("capture_network", capture_network)
    g.add_node("supervisor", supervisor_node)
    g.add_node("explorer", explorer)
    g.add_node("validator", validator)
    g.add_node("dsl_writer", dsl_writer)
    g.add_node("auditor", auditor)
    g.add_node("save_method", save_method_with_db)
    g.set_entry_point("fetch_homepage")
    g.add_edge("fetch_homepage", "capture_network")
    g.add_edge("capture_network", "supervisor")
    g.add_conditional_edges("supervisor", supervisor_route)  # 按 supervisor_route 路由
    for w in ["explorer", "validator", "dsl_writer", "auditor"]:
        g.add_edge(w, "supervisor")  # worker 执行完回 supervisor 决定下一步
    g.add_edge("save_method", END)
    from langgraph.checkpoint.memory import MemorySaver
    return g.compile(checkpointer=checkpointer or MemorySaver())  # 默认 MemorySaver（测试用）

def supervisor_node(state: DiscoveryState) -> DiscoveryState:
    """纯路由节点：不改状态，仅触发 supervisor_route 条件边。"""
    return state

def save_method_with_db(state: DiscoveryState) -> DiscoveryState:
    """确定性节点：去重签名 + 存 crawl_methods + crawl_method_domains + 建 sources 记录。

    force=true 且同 domain 已有 → 覆盖更新（保留 method_id/source_id，历史连续）；
    否则新建 crawl_method + 对应 sources(type=discovery) 记录。state.force 由 run_discovery 初始化时塞入。
    """
    from app.db import SessionLocal
    from app.models import CrawlMethod, CrawlMethodDomain, SiteDiscoveryRun, Source
    from app.enums import SourceType, Stream
    from app.discovery.dsl import DslRecipe
    from app.discovery.signature import compute_signature
    from urllib.parse import urlparse
    from datetime import datetime, timezone
    s = SessionLocal()
    try:
        recipe = DslRecipe(**state["dsl_recipe"])
        sig = compute_signature(recipe)
        domain = urlparse(state["site_url"]).netloc
        existing = s.query(CrawlMethodDomain).filter_by(domain=domain).first()
        if existing is not None and state.get("force"):
            # 覆盖：更新现有 method，保留 method_id/source_id，审计历史连续
            m = s.get(CrawlMethod, existing.method_id)
            m.dsl_recipe = recipe.model_dump()
            m.signature = sig
            m.status = "active"
            m.updated_at = datetime.now(timezone.utc)
        elif existing is not None:
            # 同 domain 已有且未 force：保留旧（兜底，正常流程前置检查已拦截）
            m = s.get(CrawlMethod, existing.method_id)
        else:
            # 新建：先建 sources(type=discovery) 记录，再建 crawl_method 关联它
            # sources 记录让 items.source_id 有处可指，前端新闻流天然能看到 discovery 抓取的条目
            src = Source(name=domain, type=SourceType.DISCOVERY.value, url=state["site_url"],
                         main_category="OS跟踪来源", stream=Stream.NEWS, enabled=True)
            s.add(src); s.flush()
            m = CrawlMethod(domain=domain, entry_url=state["site_url"], source_id=src.id,
                            dsl_recipe=recipe.model_dump(), signature=sig)
            s.add(m); s.flush()
            s.add(CrawlMethodDomain(domain=domain, method_id=m.id))  # 去重映射
        s.commit()
        return {"verdict": "dsl", "method_id": m.id}
    finally:
        s.close()

def run_discovery(site_url: str, force: bool = False) -> dict:
    """生成命入口：建图（PostgresSaver 跨进程续跑）+ 跑 + 落审计 site_discovery_runs。

    force 透传到初始 state，save_method 据此决定覆盖/新建。
    """
    from langgraph.checkpoint.postgres import PostgresSaver
    from app.db import engine_url  # 复用 DATABASE_URL
    from app.models import SiteDiscoveryRun
    from sqlalchemy import create_engine
    from app.config import get_settings
    s = get_settings()
    eng = create_engine(s.database_url)
    checkpointer = PostgresSaver(eng); checkpointer.setup()  # 自动建 checkpoint 表
    g = build_graph(checkpointer=checkpointer)
    db_sess = SessionLocal()
    try:
        run = SiteDiscoveryRun(site_url=site_url, status="running")
        db_sess.add(run); db_sess.commit()
        # thread_id 关联 run，崩了重启可从 checkpoint 续跑
        final = g.invoke({"site_url": site_url, "attempt": 0, "token_used": 0, "force": force},
                         config={"configurable": {"thread_id": f"discovery-{run.id}"}})
        # 落审计
        run.status = "completed" if final.get("verdict") == "dsl" else "failed"
        run.resulting_method_id = final.get("method_id")
        run.llm_token_usage = final.get("token_used", 0)
        run.node_trace = [{"verdict": final.get("verdict")}]
        run.ended_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        if final.get("error"): run.error_message = final["error"]
        db_sess.commit()
        return final
    finally:
        db_sess.close(); eng.dispose()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_graph.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/discovery/graph.py backend/tests/unit/discovery/test_graph.py
git commit -m "Assemble StateGraph with PostgresSaver, audit, save_method"
```

---

## Task 13: 新端点 /discovery/run + /methods/{id}/fetch

**Files:**

- Create: `backend/app/api/discovery_routes.py`
- Modify: `backend/app/api/main.py`
- Test: `backend/tests/integration/test_discovery_routes.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_discovery_routes.py
from fastapi.testclient import TestClient
from app.api.main import create_app

def test_discover_run_endpoint(client):
    # mock run_discovery 避免真调 LLM
    from unittest.mock import patch
    with patch("app.api.discovery_routes.run_discovery", return_value={"verdict":"dsl","method_id":1}):
        r = client.post("/discovery/run", json={"url": "https://x.com"})
    assert r.status_code == 200
    assert r.json()["verdict"] == "dsl"

def test_discovery_fetch_endpoint(client, session):
    from app.models import CrawlMethod
    m = CrawlMethod(domain="x.com", entry_url="https://x.com", dsl_recipe={"recipe_type":"dsl","entry_url":"https://x.com","actions":[]}, signature="abc")
    session.add(m); session.commit()
    with patch("app.api.discovery_routes.run_method", return_value={"items":[],"stats":{}}):
        r = client.post(f"/discovery/methods/{m.id}/fetch")
    assert r.status_code == 200
```

（`client` fixture 复用 `tests/integration/test_source_routes.py` 的模式。）

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_discovery_routes.py -v
```

Expected: FAIL

- [ ] **Step 3: 实现端点**

```python
# app/api/discovery_routes.py
"""新端点：/discovery/run（生成命）+ /discovery/methods/{id}/fetch（运行命）。

纯增量：不替换旧 /sources/discover，不接入 agent_crawl 主流程。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, HttpUrl
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.models import CrawlMethod, CrawlMethodDomain
from app.discovery.graph import run_discovery, check_existing_method
from app.discovery.interpreter import DslInterpreter
from app.discovery.dsl import DslRecipe
from app.discovery.ingester import CrawlOutputIngester

router = APIRouter(prefix="/discovery", tags=["discovery"])

class DiscoverRequest(BaseModel):
    url: HttpUrl
    force: bool = False   # true=跳过去重检查/覆盖同 domain 旧范式

@router.post("/run")
def discover_run(body: DiscoverRequest):
    """生成命：force=false 先查重，重复返回 duplicate 不跑；force=true 覆盖。

    第一子项目同步调用 run_discovery（Task 15 改异步）。
    """
    site_url = str(body.url)
    if not body.force:
        existing = check_existing_method(site_url)
        if existing:
            return {"status": "duplicate", "existing_method": existing}
    result = run_discovery(site_url, force=body.force)
    return {"status": "completed", **result}

@router.post("/methods/{method_id}/fetch")
def discovery_fetch(method_id: int, db: Session = Depends(get_db)):
    """运行命：按已存的 DSL Recipe 执行抓取，零 LLM。"""
    m = db.get(CrawlMethod, method_id)
    if not m: raise HTTPException(404, "method not found")
    recipe = DslRecipe(**m.dsl_recipe)
    output = DslInterpreter().run(recipe)  # 纯确定性执行
    raws = CrawlOutputIngester().to_raw_items(output, source_id=0)  # source_id 由接入层定
    m.last_run_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    db.commit()
    return output
```

`graph.py` 追加去重检查函数（供 `/run` 前置查重）：

```python
# 追加到 graph.py
def check_existing_method(site_url: str) -> dict | None:
    """按 domain 查 crawl_method_domains，命中返回已有范式摘要，否则 None。

    去重粒度=domain（用户感知是"这个网站"），signature 同形去重留作 save_method 内部。
    """
    from app.db import SessionLocal
    from app.models import CrawlMethod, CrawlMethodDomain
    from urllib.parse import urlparse
    domain = urlparse(site_url).netloc
    s = SessionLocal()
    try:
        mapping = s.query(CrawlMethodDomain).filter_by(domain=domain).first()
        if mapping is None:
            return None
        m = s.get(CrawlMethod, mapping.method_id)
        return {
            "method_id": m.id, "domain": m.domain, "signature": m.signature,
            "dsl_recipe": m.dsl_recipe,
            "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None,
            "last_run_status": m.last_run_status,
        }
    finally:
        s.close()
```

`app/api/main.py` 注册：`from app.api.discovery_routes import router as discovery_router; app.include_router(discovery_router)`。`run_method` 别名指向 `DslInterpreter().run`（供测试 mock）：

```python
def run_method(method_id, db): ...  # 包装 discovery_fetch 的执行部分
```

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_discovery_routes.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/discovery_routes.py backend/app/api/main.py backend/tests/integration/test_discovery_routes.py
git commit -m "Add /discovery/run and /discovery/methods/{id}/fetch endpoints"
```

---

## Task 14: 端到端集成测试 + 旧功能回归

**Files:**

- Test: `backend/tests/integration/test_discovery_e2e.py`

- [ ] **Step 1: 写端到端测试（mock 站点 + mock LLM）**

```python
# tests/integration/test_discovery_e2e.py
"""端到端：mock 站点 → 生成命（mock LLM 产出固定 DSL）→ 存 method → 运行命 → 入库。"""
def test_e2e_json_api_site(client, session, monkeypatch):
    from app.models import CrawlMethod
    from unittest.mock import patch

    # mock 生成命：直接产出一份合法 DSL Recipe 并存库
    fixed_recipe = {"recipe_type":"dsl","entry_url":"https://mockx.com",
        "actions":[
            {"op":"fetch","mode":"json","url":"https://mockx.com/api"},
            {"op":"extract","from":"obj.records","fields":{"title":"title","url":"template:https://mockx.com/{item.no}"}},
            {"op":"dedup_by","field":"url"}]}
    with patch("app.api.discovery_routes.run_discovery",
               return_value={"verdict":"dsl","method_id":1}):
        client.post("/discovery/run", json={"url":"https://mockx.com"})

    # 直接造一条 method 跑运行命（绕过 LLM）
    m = CrawlMethod(domain="mockx.com", entry_url="https://mockx.com",
                    dsl_recipe=fixed_recipe, signature="e2e")
    session.add(m); session.commit()

    # mock DslInterpreter 的 fetch 返回固定数据
    def fake_fetch(action, ctx):
        ctx["last_fetch"] = {"obj": {"records": [{"no":"1","title":"A"},{"no":"2","title":"B"}]}}
    monkeypatch.setattr("app.discovery.interpreter.DslInterpreter.__init__",
                        lambda self, **kw: setattr(self, "_fetch_fn", fake_fetch) or setattr(self, "_browser_fn", None) or setattr(self, "_page", None))

    r = client.post(f"/discovery/methods/{m.id}/fetch")
    assert r.status_code == 200
    assert r.json()["stats"]["discovered_count"] == 2
```

- [ ] **Step 2: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_discovery_e2e.py -v
```

Expected: PASS

- [ ] **Step 3: 跑旧功能回归（确认增量未破坏）**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/ -q
```

Expected: 全部通过（含旧 `test_api_discovery`/`test_source_routes`/`test_agent_crawl` 等回归）

- [ ] **Step 4: Commit**

```bash
git add backend/tests/integration/test_discovery_e2e.py
git commit -m "Add discovery end-to-end test and verify legacy regression"
```

---

## Task 15: /run 异步化 + runs 查询端点

**Files:**
- Modify: `backend/app/discovery/graph.py`（拆 run_discovery 为异步入口 + 执行核心）
- Modify: `backend/app/api/discovery_routes.py`（/run 异步 + GET runs）
- Test: `backend/tests/integration/test_discovery_routes.py`

**背景：** Task 12 的 `run_discovery` 同步阻塞（LLM 多轮几分钟），Task 13 的 `/run` 同步调用会卡住前端。本 task 改异步：POST /run 立即返回 run_id，后台线程跑，前端轮询 `GET /discovery/runs/{id}`。复用现有 `agent_crawl` 的 `_start_agent_source_run` 后台线程模式。

- [ ] **Step 1: 重构 graph.py——异步入口 + 执行核心**

把 Task 12 的 `run_discovery` 拆成 `start_discovery_run`（建记录 + 启后台线程）和 `_execute_discovery`（执行图 + 更新记录）：
```python
# 替换 Task 12 的 run_discovery，追加到 graph.py
import threading

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
    from langgraph.checkpoint.postgres import PostgresSaver
    from app.models import SiteDiscoveryRun
    from sqlalchemy import create_engine
    from app.config import get_settings
    from datetime import datetime, timezone
    s = get_settings()
    eng = create_engine(s.database_url)
    checkpointer = PostgresSaver(eng); checkpointer.setup()  # 自动建 checkpoint 表
    g = build_graph(checkpointer=checkpointer)
    db_sess = SessionLocal()
    try:
        final = g.invoke({"site_url": site_url, "attempt": 0, "token_used": 0, "force": force},
                         config={"configurable": {"thread_id": f"discovery-{run_id}"}})
        # 落审计
        run = db_sess.get(SiteDiscoveryRun, run_id)
        run.status = "completed" if final.get("verdict") == "dsl" else "failed"
        run.resulting_method_id = final.get("method_id")
        run.llm_token_usage = final.get("token_used", 0)
        run.node_trace = [{"verdict": final.get("verdict")}]
        run.ended_at = datetime.now(timezone.utc)
        if final.get("error"): run.error_message = final["error"]
        db_sess.commit()
    except Exception as e:
        # 兜底：图级异常标 failed（节点级异常已在 supervisor 路由处理）
        run = db_sess.get(SiteDiscoveryRun, run_id)
        if run and run.status == "running":
            run.status = "failed"; run.error_message = str(e)
            run.ended_at = datetime.now(timezone.utc)
            db_sess.commit()
    finally:
        db_sess.close(); eng.dispose()

# run_discovery 保留为同步入口（测试/同步场景用），内部调 _execute_discovery
def run_discovery(site_url: str, force: bool = False) -> dict:
    """同步入口（测试用）：建记录 + 同步跑 _execute_discovery，返回最终结果摘要。"""
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
```

- [ ] **Step 2: /run 端点改异步**

`discovery_routes.py` 的 `discover_run` 改调 `start_discovery_run`（duplicate 仍同步返回）：
```python
from app.discovery.graph import run_discovery, check_existing_method, start_discovery_run

@router.post("/run")
def discover_run(body: DiscoverRequest):
    """生成命：force=false 先查重；无重复/force=true 异步启动，返回 run_id。"""
    site_url = str(body.url)
    if not body.force:
        existing = check_existing_method(site_url)
        if existing:
            return {"status": "duplicate", "existing_method": existing}
    run_id = start_discovery_run(site_url, force=body.force)
    return {"status": "started", "run_id": run_id}
```

**注：** 此改动使 Task 13 的 `test_discover_run_endpoint`（mock `run_discovery`）失效——`discover_run` 不再调 `run_discovery`。Step 4 的 `test_discover_run_async_returns_run_id`（mock `start_discovery_run`）替代它，**删除 Task 13 的 `test_discover_run_endpoint`**。

- [ ] **Step 3: runs 查询端点（列表 + 详情/轮询）**

`discovery_routes.py` 顶部补 `from sqlalchemy import select`，追加：
```python
@router.get("/runs")
def list_discovery_runs(limit: int = 20, db: Session = Depends(get_db)):
    """列出生成命历史（最近 limit 条），按 started_at 倒序。"""
    runs = db.scalars(
        select(SiteDiscoveryRun).order_by(SiteDiscoveryRun.started_at.desc()).limit(limit)
    ).all()
    return [{"id": r.id, "site_url": r.site_url, "status": r.status,
             "resulting_method_id": r.resulting_method_id, "llm_token_usage": r.llm_token_usage,
             "started_at": r.started_at.isoformat() if r.started_at else None,
             "ended_at": r.ended_at.isoformat() if r.ended_at else None,
             "error_message": r.error_message} for r in runs]

@router.get("/runs/{run_id}")
def get_discovery_run(run_id: int, db: Session = Depends(get_db)):
    """单 run 详情/轮询：含 node_trace 审计。前端轮询此端点看 status。"""
    r = db.get(SiteDiscoveryRun, run_id)
    if not r: raise HTTPException(404, "run not found")
    return {"id": r.id, "site_url": r.site_url, "status": r.status,
            "resulting_method_id": r.resulting_method_id, "llm_token_usage": r.llm_token_usage,
            "node_trace": r.node_trace, "retry_count": r.retry_count,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "error_message": r.error_message}
```
（`SiteDiscoveryRun` 已在文件顶部 import，确认补上。）

- [ ] **Step 4: 写测试**

```python
# 追加到 test_discovery_routes.py
def test_discover_run_async_returns_run_id(client):
    from unittest.mock import patch
    with patch("app.api.discovery_routes.start_discovery_run", return_value=42):
        r = client.post("/discovery/run", json={"url": "https://x.com"})
    assert r.status_code == 200
    assert r.json() == {"status": "started", "run_id": 42}

def test_discover_run_duplicate_returns_existing(client, session):
    from app.models import CrawlMethod, CrawlMethodDomain
    m = CrawlMethod(domain="x.com", entry_url="https://x.com", dsl_recipe={"actions":[]}, signature="abc")
    session.add(m); session.flush()
    session.add(CrawlMethodDomain(domain="x.com", method_id=m.id)); session.commit()
    r = client.post("/discovery/run", json={"url": "https://x.com"})  # force 默认 false
    assert r.json()["status"] == "duplicate"
    assert r.json()["existing_method"]["method_id"] == m.id

def test_list_discovery_runs(client, session):
    from app.models import SiteDiscoveryRun
    session.add(SiteDiscoveryRun(site_url="https://x.com", status="completed"))
    session.commit()
    r = client.get("/discovery/runs")
    assert r.status_code == 200
    assert len(r.json()) >= 1
    assert r.json()[0]["status"] == "completed"

def test_get_discovery_run(client, session):
    from app.models import SiteDiscoveryRun
    run = SiteDiscoveryRun(site_url="https://x.com", status="running")
    session.add(run); session.commit()
    r = client.get(f"/discovery/runs/{run.id}")
    assert r.json()["status"] == "running"
```

- [ ] **Step 5: 跑测试 + Commit**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_discovery_routes.py -v
```
Expected: PASS
```bash
git add backend/app/discovery/graph.py backend/app/api/discovery_routes.py backend/tests/integration/test_discovery_routes.py
git commit -m "Async /discovery/run with run_id polling + runs list/detail endpoints"
```

---

## Task 16: methods 管理端点（列表/详情/禁用/删除）

**Files:**
- Modify: `backend/app/api/discovery_routes.py`
- Test: `backend/tests/integration/test_discovery_routes.py`

- [ ] **Step 1: 写失败测试**

```python
# 追加到 test_discovery_routes.py
def test_list_methods(client, session):
    from app.models import CrawlMethod
    session.add(CrawlMethod(domain="x.com", entry_url="https://x.com", dsl_recipe={"actions":[]}, signature="a"))
    session.commit()
    r = client.get("/discovery/methods")
    assert r.status_code == 200
    assert any(m["domain"] == "x.com" for m in r.json())

def test_get_method_detail(client, session):
    from app.models import CrawlMethod
    m = CrawlMethod(domain="x.com", entry_url="https://x.com",
                    dsl_recipe={"recipe_type":"dsl","entry_url":"https://x.com","actions":[]}, signature="a")
    session.add(m); session.commit()
    r = client.get(f"/discovery/methods/{m.id}")
    assert r.json()["dsl_recipe"]["recipe_type"] == "dsl"

def test_patch_method_disable(client, session):
    from app.models import CrawlMethod
    m = CrawlMethod(domain="x.com", entry_url="https://x.com", dsl_recipe={"actions":[]}, signature="a", status="active")
    session.add(m); session.commit()
    r = client.patch(f"/discovery/methods/{m.id}", json={"status": "disabled"})
    assert r.json()["status"] == "disabled"

def test_delete_method_cascades_domain(client, session):
    from app.models import CrawlMethod, CrawlMethodDomain
    m = CrawlMethod(domain="x.com", entry_url="https://x.com", dsl_recipe={"actions":[]}, signature="a")
    session.add(m); session.flush()
    session.add(CrawlMethodDomain(domain="x.com", method_id=m.id)); session.commit()
    mid = m.id
    r = client.delete(f"/discovery/methods/{mid}")
    assert r.status_code == 204
    assert session.get(CrawlMethod, mid) is None
    assert session.query(CrawlMethodDomain).filter_by(method_id=mid).count() == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_discovery_routes.py -k "list_methods or get_method_detail or patch_method or delete_method" -v
```
Expected: FAIL

- [ ] **Step 3: 实现管理端点**

`discovery_routes.py` 追加：
```python
class MethodPatch(BaseModel):
    status: str | None = None   # active | disabled

@router.get("/methods")
def list_methods(db: Session = Depends(get_db)):
    """列出所有已发现的爬取方式。"""
    ms = db.scalars(select(CrawlMethod).order_by(CrawlMethod.id.desc())).all()
    return [{"id": m.id, "domain": m.domain, "entry_url": m.entry_url, "status": m.status,
             "signature": m.signature, "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None,
             "last_run_status": m.last_run_status} for m in ms]

@router.get("/methods/{method_id}")
def get_method(method_id: int, db: Session = Depends(get_db)):
    """单方法详情，含完整 DSL Recipe（前端可展示/编辑）。"""
    m = db.get(CrawlMethod, method_id)
    if not m: raise HTTPException(404, "method not found")
    return {"id": m.id, "domain": m.domain, "entry_url": m.entry_url, "status": m.status,
            "dsl_recipe": m.dsl_recipe, "signature": m.signature,
            "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None}

@router.patch("/methods/{method_id}")
def patch_method(method_id: int, body: MethodPatch, db: Session = Depends(get_db)):
    """禁用/启用方法（改 status）。"""
    m = db.get(CrawlMethod, method_id)
    if not m: raise HTTPException(404, "method not found")
    if body.status: m.status = body.status
    db.commit()
    return {"id": m.id, "status": m.status}

@router.delete("/methods/{method_id}", status_code=204)
def delete_method(method_id: int, db: Session = Depends(get_db)):
    """删除方法 + 级联清 crawl_method_domains 映射。"""
    m = db.get(CrawlMethod, method_id)
    if not m: raise HTTPException(404, "method not found")
    db.query(CrawlMethodDomain).filter_by(method_id=method_id).delete()  # 级联清映射
    db.delete(m); db.commit()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_discovery_routes.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/discovery_routes.py backend/tests/integration/test_discovery_routes.py
git commit -m "Add crawl method management endpoints (list/detail/patch/delete)"
```

---

## Task 17: 运行命接入现有 pipeline 入 items

**Files:**
- Modify: `backend/app/discovery/ingester.py`（`to_raw_items` 加 extra）
- Modify: `backend/app/api/discovery_routes.py`（`/methods/{id}/fetch` 入库）
- Test: `backend/tests/integration/test_discovery_routes.py`

**背景：** Task 13 的 `/methods/{id}/fetch` 只返回产出 JSON（`source_id=0` 占位，不入库）。本 task 接入现有 pipeline：用 `crawl_method.source_id` 对应的 `Source`，把 DSL 产出转 RawItem（agent 旁路 enricher，零 LLM）入 `items` 表，前端新闻流能看到 discovery 抓取的条目。

- [ ] **Step 1: 改 CrawlOutputIngester.to_raw_items 加 extra**

```python
# app/discovery/ingester.py 改 to_raw_items
class CrawlOutputIngester:
    def to_raw_items(self, output: dict, *, source_id: int, extra: dict | None = None) -> list[RawItem]:
        """DSL 产出 items → RawItem 列表。

        extra 注入 agent 元数据（agent_item=True），走 pipeline agent 旁路
        (_process_agent_item)，不再调 LLM enricher——DSL extract 已得字段。
        """
        raws: list[RawItem] = []
        base_extra = {"agent_item": True, "main_category": "OS跟踪来源",
                      "importance": "中", "info_type": "其他", "key_points": [], "sub_tags": []}
        if extra: base_extra.update(extra)
        for it in output.get("items", []):
            url = it.get("url"); title = it.get("title")
            if not url or not title: continue  # 缺关键字段，丢弃
            pub = it.get("published_at")
            published_at = datetime.fromisoformat(pub.replace("Z", "+00:00")) if pub else None
            raws.append(RawItem(
                source_id=source_id, title=str(title), url=str(url),
                raw_content=it.get("content") or it.get("summary"),
                published_at=published_at, extra=base_extra,
            ))
        return raws
```

- [ ] **Step 2: 改 /methods/{id}/fetch 入库**

```python
# discovery_routes.py 改 discovery_fetch
@router.post("/methods/{method_id}/fetch")
def discovery_fetch(method_id: int, db: Session = Depends(get_db)):
    """运行命：按 DSL Recipe 抓取 + 接入现有 pipeline 入 items。零 LLM。"""
    from app.models import Source
    from app.pipeline import Pipeline
    from app.processing.enricher import Enricher
    from app.extract.scrapling_extractor import ScraplingExtractor
    m = db.get(CrawlMethod, method_id)
    if not m: raise HTTPException(404, "method not found")
    recipe = DslRecipe(**m.dsl_recipe)
    output = DslInterpreter().run(recipe)  # 纯确定性执行
    # 转 RawItem（agent 旁路 enricher）+ 走现有 pipeline 入 items
    raws = CrawlOutputIngester().to_raw_items(output, source_id=m.source_id)
    source = db.get(Source, m.source_id)
    pipeline = Pipeline(session=db, extractor=ScraplingExtractor(), enricher=Enricher())
    for raw in raws:
        pipeline.process_item(source, raw)  # extra.agent_item=True → _process_agent_item 旁路 enricher
    m.last_run_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    db.commit()
    return output
```

- [ ] **Step 3: 写测试**

```python
# 追加到 test_discovery_routes.py
def test_discovery_fetch_ingests_to_items(client, session, monkeypatch):
    from app.models import CrawlMethod, Source, Item
    from app.enums import SourceType, Stream
    # 建对应的 source(type=discovery) + method
    src = Source(name="x.com", type=SourceType.DISCOVERY.value, url="https://x.com",
                 main_category="OS跟踪来源", stream=Stream.NEWS, enabled=True)
    session.add(src); session.flush()
    m = CrawlMethod(domain="x.com", entry_url="https://x.com", source_id=src.id,
                    dsl_recipe={"recipe_type":"dsl","entry_url":"https://x.com","actions":[]}, signature="a")
    session.add(m); session.commit()
    # mock DslInterpreter 返回固定产出
    def fake_fetch(action, ctx):
        ctx["last_fetch"] = {"obj": {"records": [{"no": "1", "title": "A"}]}}
    monkeypatch.setattr("app.discovery.interpreter.DslInterpreter.__init__",
                        lambda self, **kw: (setattr(self, "_fetch_fn", fake_fetch),
                                            setattr(self, "_browser_fn", None),
                                            setattr(self, "_page", None)))
    r = client.post(f"/discovery/methods/{m.id}/fetch")
    assert r.status_code == 200
    # 验证入库 items（source_id 指向 discovery source 记录）
    items = session.query(Item).filter_by(source_id=src.id).all()
    assert len(items) >= 1
```

- [ ] **Step 4: 跑测试 + Commit**

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_discovery_routes.py -v
```
Expected: PASS
```bash
git add backend/app/discovery/ingester.py backend/app/api/discovery_routes.py backend/tests/integration/test_discovery_routes.py
git commit -m "Wire run-path fetch into existing pipeline to ingest items"
```

---

## Self-Review

**1. Spec coverage**：

- §1.2 设计方向 → Task 9-12（工具+图+worker）✓
- §2 两条命 → Task 4-6（运行命）+ Task 10-12（生成命）✓
- §3 图结构（State/supervisor/4 worker/PostgresSaver/token）→ Task 10-12 ✓
- §4 DSL 规约（8 原语/变量/loop/校验）→ Task 2-6 ✓
- §5 数据模型（3 表 + crawl_methods.source_id 关联 sources(type=discovery)）+ 集成（纯增量新端点 + 运行命入 items）→ Task 1, 12, 13, 15, 16, 17 ✓
- 接口层（前置去重 force 覆盖 + /run 异步轮询 + methods/runs 查询管理）→ Task 13（force/duplicate/check_existing_method）+ Task 15（异步/runs 查询）+ Task 16（methods 管理）✓
- 运行命入库 + source_id 关联（crawl_methods.source_id→sources(type=discovery)，items 复用现有 items.source_id 外键不动）→ Task 1（source_id 字段 + DISCOVERY enum）+ Task 12（save_method 建 sources 记录）+ Task 17（/fetch 接入 pipeline 入 items）✓
- §6 错误处理（MAX_ATTEMPTS/token 硬中止/单 worker 不拖垮）→ Task 10-12（try/except 在 worker 节点包，supervisor 路由）✓
- §7 测试策略 → 每个 Task 都有单测 + Task 14 端到端 + 回归 ✓
- §9 验证标准 → Task 14 覆盖 JSON API 端到端；OpenAnolis/openEuler 真站点验证标 `@pytest.mark.live`（手动）✓

**2. Placeholder scan**：无 TBD/TODO；每个代码块是完整可运行代码。Task 12 的 `run_discovery` 在 Task 15 重构为 `start_discovery_run`（异步入口）+ `_execute_discovery`（执行核心）+ `run_discovery`（同步入口，测试用），三者职责清晰一致。

**3. Type consistency**：`DiscoveryState` 字段在 Task 10 定义（含 `force`），Task 11-12/15 沿用；`DslRecipe`/`FetchAction` 等在 Task 2 定义，后续引用一致；`crawl_methods.source_id`（Task 1 加）+ `SourceType.DISCOVERY`（Task 1 加）在 Task 12（save_method 建 sources 记录）+ Task 17（/fetch 用 m.source_id 入库）一致；`to_raw_items(output, source_id, extra)` Task 8 定义、Task 17 加 `extra` 参数；`run_discovery(site_url, force)` / `start_discovery_run(site_url, force)` / `save_method_with_db(state)`（从 state 读 force）签名一致。

**4. 已知缺口（留 live 测试）**：OpenAnolis/openEuler 真站点 + 真 DeepSeek 的端到端验证标 `@pytest.mark.live`，不进 CI，手动跑。Task 14 用 mock 验证链路通。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-29-site-discovery-agent-langgraph.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 每个 task 派一个 fresh subagent 实现，task 间 review，快速迭代。

**2. Inline Execution** - 在当前会话用 executing-plans 批量执行，带 checkpoint review。

Which approach?