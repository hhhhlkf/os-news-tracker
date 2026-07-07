# 多源发现图谱（Multi-Source Discovery Graph）实施计划

> **面向 Agentic Worker 的提示：** 必须使用的子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 来逐任务实施本计划。各步骤使用 checkbox（`- [ ]`）语法进行追踪。

**目标：** 新增一个多源发现图谱，可路由普通网站、微信公众号输入以及内部 KM/iWiki 标记，并完整实现微信分支作为首个非网站来源。

**架构：** 保持现有网站发现图谱稳定不变，新增独立的多源入口路径。普通网站输入委托给旧图谱；微信输入生成并审核 `multi_dsl`；KM/iWiki 输入被识别并以纯契约式的内部 MCP 构件表示，供后续单独的 KM/iWiki 实施方案参考。

**技术栈：** Python 3.11+, FastAPI, LangGraph, LangChain tools/ToolNode, Pydantic v2, httpx, 现有 SQLAlchemy 模型, 现有 discovery run 持久化。

---

## 规划规则

- 控制任务数量。每个任务必须产出一个完整、可独立外部运行的切片。
- 不为部分骨架编写测试。本计划新增的每个测试必须能跑通真实图谱/API 流程以及真实网络/工具行为。
- 不修改 `backend/app/agent/` 或 `backend/app/fetchers/agent_crawl.py`。
- 不重写旧的 `dsl_writer()` / `auditor()`；当 `branch_kind=website` 时通过 `multi_dsl_writer()` / `multi_auditor()` 调用它们。
- 不在 DSL、数据库 JSON、节点追踪或日志中存储微信 cookies、tokens 或 headers。

---

## 文件映射

新增文件：

- `backend/app/discovery/multi_graph.py`  
  多源状态、输入规范化、路由、分支执行、`multi_dsl_writer`、`multi_auditor`、异步运行入口。

- `backend/app/discovery/multi_dsl.py`  
  `MultiDslRecipe`、微信动作、MCP 动作契约、共享的提取/去重动作别名、语义校验。

- `backend/app/discovery/multi_interpreter.py`  
  `multi_dsl` 的运行时调度器、微信动作执行、提取和去重输出规范化。

- `backend/app/discovery/wechat_tools.py`  
  内部微信工具，借鉴 `weixin_search_mcp` 和 `wechatarticles` 的思路适配实现。

- `backend/tests/integration/test_multi_discovery_website_live.py`  
  通过 `/discovery/multi-run` 的网站路由实时测试。

- `backend/tests/integration/test_multi_discovery_wechat_search_live.py`  
  微信关键词搜索路由实时测试。

- `backend/tests/integration/test_multi_discovery_wechat_history_live.py`  
  微信账号/历史记录路由实时测试，使用 `WECHAT_MP_COOKIE` 和 `WECHAT_MP_TOKEN`。

修改文件：

- `backend/app/api/discovery_routes.py`  
  新增 `POST /discovery/multi-run`；使 method fetch 按 `recipe_type` 分发。

- `backend/app/config.py`  
  新增可选的微信默认 profile 环境变量字段。

- `backend/app/discovery/graph.py`  
  仅从新代码中 import/调用现有函数。除非需要窄范围的兼容性导出，否则不修改旧图谱逻辑。

---

## 任务 1：多源运行入口与真实网站委托

**完整切片：** `/discovery/multi-run` 接受普通网站 URL，将其路由为 `website`，委托给现有 discovery run，并产生一个与旧网站流程完全相同的方法（method）。`[KM]` / `[iWiki]` 输入返回结构化的 `internal_mcp` 契约状态，而不是一个虚假的成功方法。

**涉及文件：**

- 新建：`backend/app/discovery/multi_graph.py`
- 新建：`backend/app/discovery/multi_dsl.py`
- 新建：`backend/app/discovery/multi_interpreter.py`
- 修改：`backend/app/api/discovery_routes.py`
- 测试：`backend/tests/integration/test_multi_discovery_website_live.py`

- [ ] **步骤 1：添加多源路由模型**

在 `backend/app/discovery/multi_graph.py` 中定义路由和状态模型：

```python
from __future__ import annotations

from typing import Any, Literal, TypedDict
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.discovery.graph import (
    DiscoveryState,
    auditor,
    dsl_writer,
    start_discovery_run,
)

BranchKind = Literal["website", "wechat", "internal_mcp", "unsupported"]


class SourceRoute(BaseModel):
    kind: BranchKind
    confidence: float = Field(ge=0.0, le=1.0)
    normalized_input: str
    input_type: str
    markers: list[str] = Field(default_factory=list)
    reason: str
    suggested_branch: BranchKind


class MultiDiscoveryState(DiscoveryState, total=False):
    raw_input: str
    normalized_input: str
    source_route: dict[str, Any]
    branch_kind: str
    branch_artifact: dict[str, Any]
    branch_trace_logs: list[dict[str, Any]]
    multi_dsl_recipe: dict[str, Any] | None
    multi_audit_result: dict[str, Any] | None
```

- [ ] **步骤 2：实现确定性优先路由**

在 `multi_graph.py` 中添加以下函数：

```python
def normalize_input(raw_input: str) -> str:
    return " ".join((raw_input or "").strip().split())


def source_router_for_input(raw_input: str, hints: dict[str, Any] | None = None) -> SourceRoute:
    normalized = normalize_input(raw_input)
    hints = hints or {}
    hinted_kind = hints.get("source_kind")
    lower = normalized.lower()
    markers: list[str] = []

    if "[km]" in lower:
        markers.append("KM")
    if "[iwiki]" in lower:
        markers.append("iWiki")

    if markers:
        return SourceRoute(
            kind="internal_mcp",
            confidence=1.0,
            normalized_input=normalized,
            input_type="internal_query",
            markers=markers,
            reason="input contains internal source marker",
            suggested_branch="internal_mcp",
        )

    if hinted_kind == "wechat":
        return SourceRoute(
            kind="wechat",
            confidence=1.0,
            normalized_input=normalized,
            input_type=_classify_wechat_input(normalized),
            reason="source_kind hint requested wechat",
            suggested_branch="wechat",
        )

    parsed = urlparse(normalized)
    if parsed.scheme in {"http", "https"}:
        host = parsed.netloc.lower()
        if host.endswith("mp.weixin.qq.com"):
            return SourceRoute(
                kind="wechat",
                confidence=1.0,
                normalized_input=normalized,
                input_type="wechat_history_url",
                reason="mp.weixin.qq.com URL",
                suggested_branch="wechat",
            )
        return SourceRoute(
            kind="website",
            confidence=1.0,
            normalized_input=normalized,
            input_type="url",
            reason="ordinary URL",
            suggested_branch="website",
        )

    return SourceRoute(
        kind="unsupported",
        confidence=0.7,
        normalized_input=normalized,
        input_type="keyword",
        reason="plain keyword input needs an explicit source_kind hint",
        suggested_branch="unsupported",
    )


def _classify_wechat_input(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        return "wechat_history_url"
    return "wechat_account"
```

- [ ] **步骤 3：添加网站委托运行入口**

在 `multi_graph.py` 中添加一个实用入口，将网站输入委托给现有异步运行，并对非网站输入返回结构化的分支结果：

```python
def start_multi_discovery_run(
    raw_input: str,
    *,
    force: bool = False,
    name: str | None = None,
    hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    route = source_router_for_input(raw_input, hints)
    if route.kind == "website":
        run_id = start_discovery_run(route.normalized_input, force=force, name=name)
        return {
            "status": "started",
            "run_id": run_id,
            "route": route.model_dump(),
            "delegated": "website_discovery",
        }
    return {
        "status": "accepted",
        "run_id": None,
        "route": route.model_dump(),
        "branch_artifact": _contract_artifact(route),
    }


def _contract_artifact(route: SourceRoute) -> dict[str, Any]:
    if route.kind == "internal_mcp":
        return {
            "source_kind": "internal_mcp",
            "status": "needs_implementation",
            "markers": route.markers,
            "query": route.normalized_input,
            "mcp_action_contract": {
                "op": "mcp_call",
                "server": "km | iwiki",
                "tool": "search_articles",
                "args": {"query": route.normalized_input, "author": None},
                "as": "last_fetch",
            },
        }
    return {
        "source_kind": route.kind,
        "status": "unsupported",
        "reason": route.reason,
    }
```

- [ ] **步骤 4：添加 `multi_dsl.py` 和 `multi_interpreter.py` 最小化契约**

创建 `backend/app/discovery/multi_dsl.py`：

```python
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class McpCallAction(BaseModel):
    op: Literal["mcp_call"]
    server: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    as_: str = Field(default="last_fetch", alias="as")
    model_config = {"populate_by_name": True}


class MultiDslRecipe(BaseModel):
    recipe_type: Literal["multi_dsl"] = "multi_dsl"
    source_kind: Literal["wechat", "internal_mcp"]
    entry: str
    auth_ref: str | None = None
    requires_auth: bool = False
    actions: list[dict[str, Any]]
    notes: list[str] = Field(default_factory=list)
```

创建 `backend/app/discovery/multi_interpreter.py`：

```python
from __future__ import annotations

from app.discovery.multi_dsl import MultiDslRecipe


class MultiDslInterpreter:
    def run(self, recipe: MultiDslRecipe) -> dict:
        return {
            "items": [],
            "stats": {
                "discovered_count": 0,
                "status": "needs_implementation",
                "source_kind": recipe.source_kind,
            },
        }
```

- [ ] **步骤 5：添加 API 端点与 recipe 分发**

修改 `backend/app/api/discovery_routes.py`：

```python
from typing import Any

from app.discovery.multi_dsl import MultiDslRecipe
from app.discovery.multi_graph import start_multi_discovery_run
from app.discovery.multi_interpreter import MultiDslInterpreter


class MultiDiscoverRequest(BaseModel):
    input: str
    force: bool = False
    name: str | None = None
    hints: dict[str, Any] | None = None


@router.post("/multi-run")
def discover_multi_run(body: MultiDiscoverRequest):
    return start_multi_discovery_run(
        body.input,
        force=body.force,
        name=body.name,
        hints=body.hints,
    )
```

将 `run_method(recipe: DslRecipe)` 更新为一个基于 dict 的分发辅助函数，同时保持现有调用者不变：

```python
def run_method(recipe: DslRecipe | MultiDslRecipe | dict) -> dict:
    if isinstance(recipe, dict):
        recipe_type = recipe.get("recipe_type")
        if recipe_type == "multi_dsl":
            return MultiDslInterpreter().run(MultiDslRecipe(**recipe))
        return DslInterpreter().run(DslRecipe(**recipe))
    if isinstance(recipe, MultiDslRecipe):
        return MultiDslInterpreter().run(recipe)
    return DslInterpreter().run(recipe)
```

在 `discovery_fetch` 中，将 `recipe = DslRecipe(**m.dsl_recipe)` 替换为：

```python
recipe = m.dsl_recipe
```

并保持 `output = run_method(recipe)` 不变。

- [ ] **步骤 6：添加真实网站实时测试**

创建 `backend/tests/integration/test_multi_discovery_website_live.py`：

```python
import os
import time

import pytest


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_MULTI_DISCOVERY_WEBSITE") != "1",
    reason="set RUN_LIVE_MULTI_DISCOVERY_WEBSITE=1 to run live multi website discovery",
)
def test_multi_run_delegates_website_to_existing_discovery(client):
    url = os.getenv("MULTI_DISCOVERY_WEBSITE_URL", "https://blogs.oracle.com/linux/feed")
    response = client.post(
        "/discovery/multi-run",
        json={"input": url, "force": True, "name": "multi website live"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "started"
    assert payload["route"]["kind"] == "website"
    run_id = payload["run_id"]

    deadline = time.time() + 240
    run_payload = None
    while time.time() < deadline:
        run_response = client.get(f"/discovery/runs/{run_id}")
        assert run_response.status_code == 200
        run_payload = run_response.json()
        if run_payload["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(2)

    assert run_payload is not None
    assert run_payload["status"] == "completed", run_payload
    assert run_payload["resulting_method_id"]
```

- [ ] **步骤 7：运行真实验证**

在 `backend/` 下运行：

```bash
ENABLE_SCHEDULER=0 RUN_LIVE_MULTI_DISCOVERY_WEBSITE=1 MULTI_DISCOVERY_WEBSITE_URL=https://blogs.oracle.com/linux/feed python -m pytest tests/integration/test_multi_discovery_website_live.py -v -s
```

预期：测试启动 `/discovery/multi-run`，委托给旧网站发现流程，等待完成，并收到一个真实的 `resulting_method_id`。

- [ ] **步骤 8：提交**

```bash
git add backend/app/discovery/multi_graph.py backend/app/discovery/multi_dsl.py backend/app/discovery/multi_interpreter.py backend/app/api/discovery_routes.py backend/tests/integration/test_multi_discovery_website_live.py
git commit -m "Add multi discovery website delegation"
```

---

## 任务 2：微信关键词搜索 Multi DSL

**完整切片：** `/discovery/multi-run` 配合 `hints.source_kind=wechat` 和关键词输入生成 `multi_dsl`，通过真实搜狗微信搜索进行审核，保存方法（method），且 `/discovery/methods/{id}/fetch` 返回真实条目。

**涉及文件：**

- 修改：`backend/app/discovery/multi_graph.py`
- 修改：`backend/app/discovery/multi_dsl.py`
- 修改：`backend/app/discovery/multi_interpreter.py`
- 新建：`backend/app/discovery/wechat_tools.py`
- 测试：`backend/tests/integration/test_multi_discovery_wechat_search_live.py`

- [ ] **步骤 1：添加微信搜索动作 Schema**

在 `backend/app/discovery/multi_dsl.py` 中添加：

```python
class WechatSearchArticlesAction(BaseModel):
    op: Literal["wechat_search_articles"]
    query: str
    limit: int = Field(default=20, ge=1, le=100)
    as_: str = Field(default="last_fetch", alias="as")
    model_config = {"populate_by_name": True}
```

- [ ] **步骤 2：实现内部微信搜索工具**

创建 `backend/app/discovery/wechat_tools.py`：

```python
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

import httpx

SOGOU_WEIXIN_URL = "https://weixin.sogou.com/weixin"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def wechat_search_articles(query: str, limit: int = 20) -> dict:
    items: list[dict] = []
    with httpx.Client(headers=DEFAULT_HEADERS, timeout=20, follow_redirects=True) as client:
        response = client.get(SOGOU_WEIXIN_URL, params={"type": "2", "query": query, "ie": "utf8"})
        if response.status_code in {403, 429}:
            return {"status": "rate_limited", "items": [], "raw_status": response.status_code}
        response.raise_for_status()
        for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', response.text, re.S):
            href = html.unescape(match.group(1))
            title = _strip_tags(match.group(2))
            if "mp.weixin.qq.com" not in href and "url=" in href:
                href = _extract_redirect_url(href) or href
            if "mp.weixin.qq.com" not in href or not title:
                continue
            items.append({"title": title, "url": href, "published_at": None, "summary": "", "content": ""})
            if len(items) >= limit:
                break
    return {
        "status": "ok" if items else "empty",
        "items": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(" ".join(value.split()))


def _extract_redirect_url(value: str) -> str | None:
    parsed = urlparse(html.unescape(value))
    query = parse_qs(parsed.query)
    target = query.get("url", [None])[0]
    return unquote(target) if target else None
```

- [ ] **步骤 3：在解释器中执行微信搜索动作**

在 `backend/app/discovery/multi_interpreter.py` 中，将最初的纯契约式 `run()` 替换为：

```python
from app.discovery.wechat_tools import wechat_search_articles


class MultiDslInterpreter:
    def run(self, recipe: MultiDslRecipe) -> dict:
        ctx: dict = {"items": [], "last_fetch": None}
        for action in recipe.actions:
            op = action.get("op")
            if op == "wechat_search_articles":
                ctx["last_fetch"] = wechat_search_articles(
                    query=action["query"],
                    limit=int(action.get("limit") or 20),
                )
            elif op == "extract":
                source = ctx.get("last_fetch") or {}
                ctx["items"] = list(source.get("items") or [])
            elif op == "dedup_by":
                ctx["items"] = _dedup(ctx["items"], action.get("field") or "url")
            else:
                return {"items": [], "stats": {"status": "unsupported_action", "op": op}}
        return {
            "items": ctx["items"],
            "stats": {
                "discovered_count": len(ctx["items"]),
                "status": (ctx.get("last_fetch") or {}).get("status", "ok"),
                "source_kind": recipe.source_kind,
            },
        }


def _dedup(items: list[dict], field: str) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        key = str(item.get(field) or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
```

- [ ] **步骤 4：添加微信关键词图谱路径**

在 `multi_graph.py` 中实现关键词微信分支生成：

```python
def build_wechat_search_recipe(entry: str, *, limit: int = 20) -> dict[str, Any]:
    return {
        "recipe_type": "multi_dsl",
        "source_kind": "wechat",
        "entry": entry,
        "auth_ref": None,
        "requires_auth": False,
        "actions": [
            {"op": "wechat_search_articles", "query": entry, "limit": limit, "as": "last_fetch"},
            {
                "op": "extract",
                "from": "last_fetch.items",
                "fields": {
                    "title": "title",
                    "url": "url",
                    "published_at": "published_at",
                    "summary": "summary",
                    "content": "content",
                },
                "into": "items",
                "merge": False,
            },
            {"op": "dedup_by", "field": "url"},
        ],
        "notes": ["WeChat keyword search uses public Sogou WeChat search results."],
    }
```

扩展 `start_multi_discovery_run()`，使得 `route.kind == "wechat"` 且 `route.input_type == "wechat_account"` 时，仅在 hints 包含 `mode="search"` 或 `hints["wechat_mode"] == "search"` 时处理：

```python
if route.kind == "wechat" and (hints or {}).get("wechat_mode") == "search":
    recipe = build_wechat_search_recipe(route.normalized_input, limit=int((hints or {}).get("limit") or 20))
    return _run_and_save_multi_recipe(route=route, recipe=recipe, force=force, name=name)
```

在 `multi_graph.py` 中添加 `_run_and_save_multi_recipe()`。它应调用 `MultiDslInterpreter().run(MultiDslRecipe(**recipe))`，要求至少有一个条目，使用 `save_method` 所使用的相同模型保存 `CrawlMethod`，并返回 `{"status": "completed", "method_id": method.id, "route": ...}`。如果现有的签名辅助函数可 import，则复用；否则，从 `source_kind`、`entry` 和动作 JSON 计算出一个确定性 SHA256。

- [ ] **步骤 5：添加实时关键词搜索测试**

创建 `backend/tests/integration/test_multi_discovery_wechat_search_live.py`：

```python
import os

import pytest


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_WECHAT_SEARCH") != "1",
    reason="set RUN_LIVE_WECHAT_SEARCH=1 to run live WeChat search discovery",
)
def test_multi_run_wechat_keyword_search_generates_fetchable_method(client):
    query = os.getenv("WECHAT_SEARCH_QUERY", "Linux")
    response = client.post(
        "/discovery/multi-run",
        json={
            "input": query,
            "force": True,
            "name": "wechat search live",
            "hints": {"source_kind": "wechat", "wechat_mode": "search", "limit": 5},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed", payload
    method_id = payload["method_id"]

    fetch_response = client.post(f"/discovery/methods/{method_id}/fetch", json=None)
    assert fetch_response.status_code == 200
    fetch_payload = fetch_response.json()
    assert fetch_payload["items"]
    assert all(item.get("title") and item.get("url") for item in fetch_payload["items"])
```

- [ ] **步骤 6：运行真实验证**

在 `backend/` 下运行：

```bash
ENABLE_SCHEDULER=0 RUN_LIVE_WECHAT_SEARCH=1 WECHAT_SEARCH_QUERY=Linux python -m pytest tests/integration/test_multi_discovery_wechat_search_live.py -v -s
```

预期：测试执行真实搜狗微信搜索，保存一个 `multi_dsl` 方法，并通过 `/discovery/methods/{id}/fetch` 拉取真实条目。

- [ ] **步骤 7：提交**

```bash
git add backend/app/discovery/multi_graph.py backend/app/discovery/multi_dsl.py backend/app/discovery/multi_interpreter.py backend/app/discovery/wechat_tools.py backend/tests/integration/test_multi_discovery_wechat_search_live.py
git commit -m "Add WeChat search discovery path"
```

---

## 任务 3：微信账号与历史记录发现（含默认认证配置）

**完整切片：** `/discovery/multi-run` 配合微信公众号名称/ID 或历史记录 URL，使用来自环境变量/配置的 `wechat_mp_default`，解析账号元数据，按 `limit` 拉取最近历史记录，保存 `fakeid`/`__biz` 以及原始输入，并可拉取真实历史记录条目。若认证缺失或无效，则保存一个 `pending_auth` 方法而不泄露密钥。

**涉及文件：**

- 修改：`backend/app/config.py`
- 修改：`backend/app/discovery/wechat_tools.py`
- 修改：`backend/app/discovery/multi_dsl.py`
- 修改：`backend/app/discovery/multi_interpreter.py`
- 修改：`backend/app/discovery/multi_graph.py`
- 测试：`backend/tests/integration/test_multi_discovery_wechat_history_live.py`

- [ ] **步骤 1：添加默认微信 Profile 配置**

在 `backend/app/config.py` 中，向现有 settings 模型添加可选设置：

```python
wechat_mp_cookie: str | None = None
wechat_mp_token: str | None = None
wechat_mp_profile_name: str = "wechat_mp_default"
```

- [ ] **步骤 2：添加认证 Profile 解析器**

在 `wechat_tools.py` 中添加：

```python
from app.config import get_settings


def resolve_wechat_auth_profile(auth_ref: str | None) -> dict:
    settings = get_settings()
    expected = settings.wechat_mp_profile_name or "wechat_mp_default"
    if auth_ref not in {None, expected}:
        return {"status": "auth_invalid", "reason": "unknown auth_ref"}
    if not settings.wechat_mp_cookie or not settings.wechat_mp_token:
        return {"status": "pending_auth", "reason": "WECHAT_MP_COOKIE or WECHAT_MP_TOKEN is not configured"}
    return {
        "status": "ok",
        "auth_ref": expected,
        "cookie": settings.wechat_mp_cookie,
        "token": settings.wechat_mp_token,
    }
```

- [ ] **步骤 3：实现账号解析和历史记录拉取**

在 `wechat_tools.py` 中添加微信 MP 端点：

```python
MP_SEARCH_BIZ_URL = "https://mp.weixin.qq.com/cgi-bin/searchbiz"
MP_APPMSG_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg"


def wechat_resolve_account(nickname_or_account_id: str, auth_ref: str = "wechat_mp_default") -> dict:
    auth = resolve_wechat_auth_profile(auth_ref)
    if auth["status"] != "ok":
        return auth
    headers = {**DEFAULT_HEADERS, "Cookie": auth["cookie"]}
    params = {
        "action": "search_biz",
        "token": auth["token"],
        "lang": "zh_CN",
        "f": "json",
        "ajax": "1",
        "random": "0.1",
        "query": nickname_or_account_id,
        "begin": "0",
        "count": "5",
    }
    with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as client:
        response = client.get(MP_SEARCH_BIZ_URL, params=params)
    if response.status_code in {403, 429}:
        return {"status": "rate_limited", "items": []}
    data = response.json()
    accounts = data.get("list") or []
    if not accounts:
        return {"status": "needs_resolver", "accounts": []}
    first = accounts[0]
    return {
        "status": "ok",
        "nickname": first.get("nickname") or nickname_or_account_id,
        "account_id": nickname_or_account_id,
        "fakeid": first.get("fakeid"),
        "__biz": first.get("fakeid"),
        "accounts": accounts,
    }


def wechat_fetch_account_history(
    *,
    nickname: str | None,
    account_id: str | None,
    fakeid: str | None,
    biz: str | None,
    limit: int = 20,
    fetch_content: bool = False,
    auth_ref: str = "wechat_mp_default",
) -> dict:
    auth = resolve_wechat_auth_profile(auth_ref)
    if auth["status"] != "ok":
        return {**auth, "items": []}
    resolved = None
    if not fakeid:
        resolved = wechat_resolve_account(nickname or account_id or "", auth_ref)
        if resolved.get("status") != "ok":
            return {**resolved, "items": []}
        fakeid = resolved.get("fakeid")
        biz = resolved.get("__biz")
        nickname = resolved.get("nickname") or nickname
    headers = {**DEFAULT_HEADERS, "Cookie": auth["cookie"]}
    items: list[dict] = []
    begin = 0
    page_size = min(10, max(1, limit))
    with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as client:
        while len(items) < limit:
            params = {
                "action": "list_ex",
                "begin": str(begin),
                "count": str(page_size),
                "fakeid": fakeid,
                "type": "9",
                "query": "",
                "token": auth["token"],
                "lang": "zh_CN",
                "f": "json",
                "ajax": "1",
            }
            response = client.get(MP_APPMSG_URL, params=params)
            if response.status_code in {403, 429}:
                return {"status": "rate_limited", "items": items}
            data = response.json()
            raw_items = data.get("app_msg_list") or []
            if not raw_items:
                break
            for raw in raw_items:
                items.append(_normalize_mp_article(raw))
                if len(items) >= limit:
                    break
            begin += len(raw_items)
    if fetch_content:
        for item in items:
            content = wechat_fetch_article_content(item["url"], auth_ref=auth_ref)
            if content.get("status") == "ok":
                item["content"] = content.get("content") or ""
    return {
        "status": "ok" if items else "empty",
        "items": items,
        "nickname": nickname,
        "account_id": account_id,
        "fakeid": fakeid,
        "__biz": biz,
        "limit": limit,
        "fetch_content": fetch_content,
    }


def _normalize_mp_article(raw: dict) -> dict:
    return {
        "title": raw.get("title") or "",
        "url": raw.get("link") or "",
        "published_at": raw.get("update_time"),
        "summary": raw.get("digest") or "",
        "content": "",
    }
```

- [ ] **步骤 4：实现文章内容拉取**

在 `wechat_tools.py` 中添加：

```python
def wechat_fetch_article_content(url: str, auth_ref: str | None = None) -> dict:
    headers = dict(DEFAULT_HEADERS)
    auth = resolve_wechat_auth_profile(auth_ref) if auth_ref else {"status": "pending_auth"}
    if auth.get("status") == "ok":
        headers["Cookie"] = auth["cookie"]
    with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as client:
        response = client.get(url)
    if response.status_code in {403, 429}:
        return {"status": "rate_limited", "content": ""}
    if response.status_code >= 400:
        return {"status": "unsupported", "content": ""}
    match = re.search(r'<div[^>]+id="js_content"[^>]*>(.*?)</div>', response.text, re.S)
    if not match:
        return {"status": "empty", "content": ""}
    return {"status": "ok", "content": _strip_tags(match.group(1))}
```

- [ ] **步骤 5：添加账号历史记录动作 Schema 与解释器执行**

在 `multi_dsl.py` 中添加：

```python
class WechatFetchAccountHistoryAction(BaseModel):
    op: Literal["wechat_fetch_account_history"]
    nickname: str | None = None
    account_id: str | None = None
    fakeid: str | None = None
    biz: str | None = Field(default=None, alias="__biz")
    limit: int = Field(default=20, ge=1, le=100)
    fetch_content: bool = False
    auth_ref: str = "wechat_mp_default"
    as_: str = Field(default="last_fetch", alias="as")
    model_config = {"populate_by_name": True}
```

在 `multi_interpreter.py` 中添加：

```python
from app.discovery.wechat_tools import wechat_fetch_account_history
```

并处理该操作码：

```python
if op == "wechat_fetch_account_history":
    ctx["last_fetch"] = wechat_fetch_account_history(
        nickname=action.get("nickname"),
        account_id=action.get("account_id"),
        fakeid=action.get("fakeid"),
        biz=action.get("__biz"),
        limit=int(action.get("limit") or 20),
        fetch_content=bool(action.get("fetch_content")),
        auth_ref=action.get("auth_ref") or "wechat_mp_default",
    )
```

- [ ] **步骤 6：添加账号/历史记录图谱路径**

在 `multi_graph.py` 中添加：

```python
def build_wechat_history_recipe(
    entry: str,
    artifact: dict[str, Any],
    *,
    limit: int = 20,
    fetch_content: bool = False,
) -> dict[str, Any]:
    return {
        "recipe_type": "multi_dsl",
        "source_kind": "wechat",
        "entry": entry,
        "auth_ref": "wechat_mp_default",
        "requires_auth": True,
        "actions": [
            {
                "op": "wechat_fetch_account_history",
                "nickname": artifact.get("nickname") or entry,
                "account_id": artifact.get("account_id"),
                "fakeid": artifact.get("fakeid"),
                "__biz": artifact.get("__biz"),
                "limit": limit,
                "fetch_content": fetch_content,
                "auth_ref": "wechat_mp_default",
                "as": "last_fetch",
            },
            {
                "op": "extract",
                "from": "last_fetch.items",
                "fields": {
                    "title": "title",
                    "url": "url",
                    "published_at": "published_at",
                    "summary": "summary",
                    "content": "content",
                },
                "into": "items",
                "merge": False,
            },
            {"op": "dedup_by", "field": "url"},
        ],
        "notes": ["WeChat account history uses the default wechat_mp_default auth profile."],
    }
```

在 `start_multi_discovery_run()` 中扩展微信非搜索输入：

```python
if route.kind == "wechat":
    limit = int((hints or {}).get("limit") or 20)
    fetch_content = bool((hints or {}).get("fetch_content", False))
    artifact = {"source_kind": "wechat", "nickname": route.normalized_input, "auth_ref": "wechat_mp_default"}
    recipe = build_wechat_history_recipe(route.normalized_input, artifact, limit=limit, fetch_content=fetch_content)
    return _run_and_save_multi_recipe(route=route, recipe=recipe, force=force, name=name)
```

- [ ] **步骤 7：不泄露密钥的前提下处理 pending/auth 状态**

在 `_run_and_save_multi_recipe()` 中对解释器输出进行分类：

```python
status = (output.get("stats") or {}).get("status")
items = output.get("items") or []
if status in {"pending_auth", "auth_invalid"}:
    method_status = status
elif status in {"rate_limited", "captcha_required"}:
    method_status = "retry_later"
elif len(items) >= _required_count(recipe):
    method_status = "active"
else:
    method_status = "failed"
```

使用：

```python
def _required_count(recipe: dict[str, Any]) -> int:
    limit = 20
    for action in recipe.get("actions") or []:
        if action.get("op") in {"wechat_fetch_account_history", "wechat_search_articles"}:
            limit = int(action.get("limit") or limit)
            break
    return min(5, max(1, int(limit * 0.25)))
```

在保存 `dsl_recipe` 或 `node_trace` 之前，确保没有任何名为 `cookie`、`token`、`authorization` 或 `headers` 的键存在。

- [ ] **步骤 8：添加实时历史记录测试**

创建 `backend/tests/integration/test_multi_discovery_wechat_history_live.py`：

```python
import os

import pytest


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_WECHAT_HISTORY") != "1",
    reason="set RUN_LIVE_WECHAT_HISTORY=1 to run live WeChat history discovery",
)
@pytest.mark.skipif(
    not os.getenv("WECHAT_MP_COOKIE") or not os.getenv("WECHAT_MP_TOKEN"),
    reason="WECHAT_MP_COOKIE and WECHAT_MP_TOKEN are required",
)
def test_multi_run_wechat_account_history_generates_fetchable_method(client):
    account = os.getenv("WECHAT_HISTORY_ACCOUNT", "腾讯技术工程")
    response = client.post(
        "/discovery/multi-run",
        json={
            "input": account,
            "force": True,
            "name": "wechat history live",
            "hints": {"source_kind": "wechat", "limit": 5, "fetch_content": False},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed", payload
    method_id = payload["method_id"]

    method_response = client.get(f"/discovery/methods/{method_id}")
    assert method_response.status_code == 200
    method_payload = method_response.json()
    recipe = method_payload["dsl_recipe"]
    assert recipe["recipe_type"] == "multi_dsl"
    assert recipe["auth_ref"] == "wechat_mp_default"
    assert "WECHAT_MP_COOKIE" not in str(recipe)
    assert "WECHAT_MP_TOKEN" not in str(recipe)

    fetch_response = client.post(f"/discovery/methods/{method_id}/fetch", json=None)
    assert fetch_response.status_code == 200
    fetch_payload = fetch_response.json()
    assert fetch_payload["items"]
    assert all(item.get("title") and item.get("url") for item in fetch_payload["items"])
```

- [ ] **步骤 9：运行真实验证**

在 `backend/` 下运行：

```bash
ENABLE_SCHEDULER=0 RUN_LIVE_WECHAT_HISTORY=1 WECHAT_HISTORY_ACCOUNT=腾讯技术工程 WECHAT_MP_COOKIE="$WECHAT_MP_COOKIE" WECHAT_MP_TOKEN="$WECHAT_MP_TOKEN" python -m pytest tests/integration/test_multi_discovery_wechat_history_live.py -v -s
```

预期：测试通过配置的默认 profile 解析真实微信公众号，保存一个不含密钥的 `multi_dsl` 方法，并拉取真实的最近历史记录条目。

- [ ] **步骤 10：提交**

```bash
git add backend/app/config.py backend/app/discovery/multi_graph.py backend/app/discovery/multi_dsl.py backend/app/discovery/multi_interpreter.py backend/app/discovery/wechat_tools.py backend/tests/integration/test_multi_discovery_wechat_history_live.py
git commit -m "Add WeChat account history discovery"
```

---

## 任务 4：最终回归验证与文档清理

**完整切片：** 所有已实现的来源类型（source kind）仍通过相同的公开 API 界面运行，旧网站发现保持完整，实时测试记录真实的能力边界。

**涉及文件：**

- 修改：`docs/superpowers/specs/2026-07-06-multi-source-discovery-graph-design.md`（仅当实施中发现改变已批准设计的真实约束时才修改）
- 修改：`AGENTS.md` / `CLAUDE.md`（仅当发现经用户批准的新项目级规则时才修改）

- [ ] **步骤 1：运行语法/导入检查**

在 `backend/` 下运行：

```bash
python -m py_compile app/discovery/multi_graph.py app/discovery/multi_dsl.py app/discovery/multi_interpreter.py app/discovery/wechat_tools.py app/api/discovery_routes.py app/config.py
```

预期：无语法错误。

- [ ] **步骤 2：运行现有 Discovery DSL 检查**

在 `backend/` 下运行：

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_dsl.py -v
```

预期：现有 DSL 测试仍然通过，证明旧 DSL 语义未被破坏。

- [ ] **步骤 3：运行匹配已配置能力的真实实时测试**

网站：

```bash
ENABLE_SCHEDULER=0 RUN_LIVE_MULTI_DISCOVERY_WEBSITE=1 MULTI_DISCOVERY_WEBSITE_URL=https://blogs.oracle.com/linux/feed python -m pytest tests/integration/test_multi_discovery_website_live.py -v -s
```

微信关键词搜索：

```bash
ENABLE_SCHEDULER=0 RUN_LIVE_WECHAT_SEARCH=1 WECHAT_SEARCH_QUERY=Linux python -m pytest tests/integration/test_multi_discovery_wechat_search_live.py -v -s
```

微信历史记录，仅在认证环境变量存在时运行：

```bash
ENABLE_SCHEDULER=0 RUN_LIVE_WECHAT_HISTORY=1 WECHAT_HISTORY_ACCOUNT=腾讯技术工程 WECHAT_MP_COOKIE="$WECHAT_MP_COOKIE" WECHAT_MP_TOKEN="$WECHAT_MP_TOKEN" python -m pytest tests/integration/test_multi_discovery_wechat_history_live.py -v -s
```

预期：网站和搜索在有公共网络访问时通过；历史记录在微信 MP 认证环境有效时通过，否则测试被环境变量门控跳过。

- [ ] **步骤 4：提交最终清理**

```bash
git status --short
git add docs/superpowers/specs/2026-07-06-multi-source-discovery-graph-design.md AGENTS.md CLAUDE.md
git diff --cached --quiet || git commit -m "Document multi discovery implementation constraints"
```

仅当实施过程中发现真实、经用户批准的文档变更时，才在此任务中提交文档。
