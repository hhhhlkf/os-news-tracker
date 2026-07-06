# Multi-Source Discovery Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a multi-source discovery graph that routes ordinary websites, WeChat official account inputs, and internal KM/iWiki markers while fully implementing the WeChat branch as the first real non-website source.

**Architecture:** Keep the existing website discovery graph stable and add a separate multi-source entry path. Ordinary website inputs delegate to the old graph; WeChat inputs produce and audit `multi_dsl`; KM/iWiki inputs are recognized and represented by a contract-only internal MCP artifact for a separate KM/iWiki implementation spec.

**Tech Stack:** Python 3.11+, FastAPI, LangGraph, LangChain tools/ToolNode, Pydantic v2, httpx, existing SQLAlchemy models, existing discovery run persistence.

---

## Planning Rules

- Keep the task count low. Each task must produce a complete, externally runnable slice.
- Do not write tests for partial skeletons. Each test added by this plan must run through a real graph/API flow and real network/tool behavior.
- Do not modify `backend/app/agent/` or `backend/app/fetchers/agent_crawl.py`.
- Do not rewrite old `dsl_writer()` / `auditor()`; call them through `multi_dsl_writer()` / `multi_auditor()` when `branch_kind=website`.
- Do not store WeChat cookies, tokens, or headers in DSL, database JSON, node trace, or logs.

---

## File Map

Create:

- `backend/app/discovery/multi_graph.py`  
  Multi-source state, input normalization, routing, branch execution, `multi_dsl_writer`, `multi_auditor`, async run entrypoint.

- `backend/app/discovery/multi_dsl.py`  
  `MultiDslRecipe`, WeChat actions, MCP action contract, shared extract/dedup action aliases, semantic validation.

- `backend/app/discovery/multi_interpreter.py`  
  Runtime dispatcher for `multi_dsl`, WeChat action execution, extraction and dedup output normalization.

- `backend/app/discovery/wechat_tools.py`  
  Internal WeChat tools adapted from the ideas in `weixin_search_mcp` and `wechatarticles`.

- `backend/tests/integration/test_multi_discovery_website_live.py`  
  Live website route test through `/discovery/multi-run`.

- `backend/tests/integration/test_multi_discovery_wechat_search_live.py`  
  Live WeChat keyword-search route test.

- `backend/tests/integration/test_multi_discovery_wechat_history_live.py`  
  Live WeChat account/history route test using `WECHAT_MP_COOKIE` and `WECHAT_MP_TOKEN`.

Modify:

- `backend/app/api/discovery_routes.py`  
  Add `POST /discovery/multi-run`; make method fetch dispatch by `recipe_type`.

- `backend/app/config.py`  
  Add optional WeChat default profile env fields.

- `backend/app/discovery/graph.py`  
  Only import/call existing functions from new code. Do not change old graph logic unless a narrow compatibility export is needed.

---

## Task 1: Multi-Run Entry With Real Website Delegation

**Complete slice:** `/discovery/multi-run` accepts normal website URLs, routes them as `website`, delegates to the existing discovery run, and produces a real method exactly as the old website flow does. `[KM]` / `[iWiki]` inputs return a structured `internal_mcp` contract state, not a fake successful method.

**Files:**

- Create: `backend/app/discovery/multi_graph.py`
- Create: `backend/app/discovery/multi_dsl.py`
- Create: `backend/app/discovery/multi_interpreter.py`
- Modify: `backend/app/api/discovery_routes.py`
- Test: `backend/tests/integration/test_multi_discovery_website_live.py`

- [ ] **Step 1: Add multi-source route models**

In `backend/app/discovery/multi_graph.py`, define the route and state models:

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

- [ ] **Step 2: Implement deterministic-first routing**

Add these functions to `multi_graph.py`:

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

- [ ] **Step 3: Add website delegation run entrypoint**

Add a pragmatic entrypoint in `multi_graph.py` that delegates website inputs to the existing async run and returns structured branch results for non-website inputs:

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

- [ ] **Step 4: Add `multi_dsl.py` and `multi_interpreter.py` minimal contracts**

Create `backend/app/discovery/multi_dsl.py`:

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

Create `backend/app/discovery/multi_interpreter.py`:

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

- [ ] **Step 5: Add the API endpoint and recipe dispatch**

Modify `backend/app/api/discovery_routes.py`:

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

Update `run_method(recipe: DslRecipe)` into a dict-dispatching helper while preserving existing callers:

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

In `discovery_fetch`, replace `recipe = DslRecipe(**m.dsl_recipe)` with:

```python
recipe = m.dsl_recipe
```

and keep `output = run_method(recipe)`.

- [ ] **Step 6: Add the real website live test**

Create `backend/tests/integration/test_multi_discovery_website_live.py`:

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

- [ ] **Step 7: Run real verification**

Run from `backend/`:

```bash
ENABLE_SCHEDULER=0 RUN_LIVE_MULTI_DISCOVERY_WEBSITE=1 MULTI_DISCOVERY_WEBSITE_URL=https://blogs.oracle.com/linux/feed python -m pytest tests/integration/test_multi_discovery_website_live.py -v -s
```

Expected: the test starts `/discovery/multi-run`, delegates to the old website discovery, waits for completion, and receives a real `resulting_method_id`.

- [ ] **Step 8: Commit**

```bash
git add backend/app/discovery/multi_graph.py backend/app/discovery/multi_dsl.py backend/app/discovery/multi_interpreter.py backend/app/api/discovery_routes.py backend/tests/integration/test_multi_discovery_website_live.py
git commit -m "Add multi discovery website delegation"
```

---

## Task 2: WeChat Keyword Search Multi DSL

**Complete slice:** `/discovery/multi-run` with `hints.source_kind=wechat` and keyword input generates a `multi_dsl`, audits it by real Sogou WeChat search, saves a method, and `/discovery/methods/{id}/fetch` returns real items.

**Files:**

- Modify: `backend/app/discovery/multi_graph.py`
- Modify: `backend/app/discovery/multi_dsl.py`
- Modify: `backend/app/discovery/multi_interpreter.py`
- Create: `backend/app/discovery/wechat_tools.py`
- Test: `backend/tests/integration/test_multi_discovery_wechat_search_live.py`

- [ ] **Step 1: Add WeChat search action schema**

In `backend/app/discovery/multi_dsl.py`, add:

```python
class WechatSearchArticlesAction(BaseModel):
    op: Literal["wechat_search_articles"]
    query: str
    limit: int = Field(default=20, ge=1, le=100)
    as_: str = Field(default="last_fetch", alias="as")
    model_config = {"populate_by_name": True}
```

- [ ] **Step 2: Implement internal WeChat search tool**

Create `backend/app/discovery/wechat_tools.py`:

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

- [ ] **Step 3: Execute WeChat search actions in the interpreter**

In `backend/app/discovery/multi_interpreter.py`, replace the initial contract-only `run()` with:

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

- [ ] **Step 4: Add WeChat keyword graph path**

In `multi_graph.py`, implement keyword WeChat branch generation:

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

Extend `start_multi_discovery_run()` for `route.kind == "wechat"` and `route.input_type == "wechat_account"` only when hints contain `mode="search"` or `hints["wechat_mode"] == "search"`:

```python
if route.kind == "wechat" and (hints or {}).get("wechat_mode") == "search":
    recipe = build_wechat_search_recipe(route.normalized_input, limit=int((hints or {}).get("limit") or 20))
    return _run_and_save_multi_recipe(route=route, recipe=recipe, force=force, name=name)
```

Add `_run_and_save_multi_recipe()` in `multi_graph.py`. It should call `MultiDslInterpreter().run(MultiDslRecipe(**recipe))`, require at least one item, save a `CrawlMethod` using the same model used by `save_method`, and return `{"status": "completed", "method_id": method.id, "route": ...}`. Reuse the existing signature helper if it is importable; if not, compute a deterministic SHA256 from `source_kind`, `entry`, and action JSON.

- [ ] **Step 5: Add live keyword search test**

Create `backend/tests/integration/test_multi_discovery_wechat_search_live.py`:

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

- [ ] **Step 6: Run real verification**

Run from `backend/`:

```bash
ENABLE_SCHEDULER=0 RUN_LIVE_WECHAT_SEARCH=1 WECHAT_SEARCH_QUERY=Linux python -m pytest tests/integration/test_multi_discovery_wechat_search_live.py -v -s
```

Expected: the test performs real Sogou WeChat search, saves a `multi_dsl` method, and fetches real items through `/discovery/methods/{id}/fetch`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/discovery/multi_graph.py backend/app/discovery/multi_dsl.py backend/app/discovery/multi_interpreter.py backend/app/discovery/wechat_tools.py backend/tests/integration/test_multi_discovery_wechat_search_live.py
git commit -m "Add WeChat search discovery path"
```

---

## Task 3: WeChat Account And History Discovery With Default Auth Profile

**Complete slice:** `/discovery/multi-run` with a WeChat account name/ID or history URL uses `wechat_mp_default` from env/config, resolves account metadata, fetches recent history with `limit`, saves `fakeid`/`__biz` plus original input, and fetches real history items. If auth is missing or invalid, it saves a `pending_auth` method without leaking secrets.

**Files:**

- Modify: `backend/app/config.py`
- Modify: `backend/app/discovery/wechat_tools.py`
- Modify: `backend/app/discovery/multi_dsl.py`
- Modify: `backend/app/discovery/multi_interpreter.py`
- Modify: `backend/app/discovery/multi_graph.py`
- Test: `backend/tests/integration/test_multi_discovery_wechat_history_live.py`

- [ ] **Step 1: Add default WeChat profile settings**

In `backend/app/config.py`, add optional settings to the existing settings model:

```python
wechat_mp_cookie: str | None = None
wechat_mp_token: str | None = None
wechat_mp_profile_name: str = "wechat_mp_default"
```

- [ ] **Step 2: Add auth profile resolver**

In `wechat_tools.py`, add:

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

- [ ] **Step 3: Implement account resolve and history fetch**

In `wechat_tools.py`, add WeChat MP endpoints:

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

- [ ] **Step 4: Implement article content fetch**

Add to `wechat_tools.py`:

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

- [ ] **Step 5: Add account history action schema and interpreter execution**

In `multi_dsl.py`, add:

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

In `multi_interpreter.py`, add:

```python
from app.discovery.wechat_tools import wechat_fetch_account_history
```

and handle the op:

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

- [ ] **Step 6: Add account/history graph path**

In `multi_graph.py`, add:

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

Extend WeChat non-search input in `start_multi_discovery_run()`:

```python
if route.kind == "wechat":
    limit = int((hints or {}).get("limit") or 20)
    fetch_content = bool((hints or {}).get("fetch_content", False))
    artifact = {"source_kind": "wechat", "nickname": route.normalized_input, "auth_ref": "wechat_mp_default"}
    recipe = build_wechat_history_recipe(route.normalized_input, artifact, limit=limit, fetch_content=fetch_content)
    return _run_and_save_multi_recipe(route=route, recipe=recipe, force=force, name=name)
```

- [ ] **Step 7: Handle pending/auth statuses without leaking secrets**

In `_run_and_save_multi_recipe()`, classify interpreter output:

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

Use:

```python
def _required_count(recipe: dict[str, Any]) -> int:
    limit = 20
    for action in recipe.get("actions") or []:
        if action.get("op") in {"wechat_fetch_account_history", "wechat_search_articles"}:
            limit = int(action.get("limit") or limit)
            break
    return min(5, max(1, int(limit * 0.25)))
```

Before saving `dsl_recipe` or `node_trace`, ensure no key named `cookie`, `token`, `authorization`, or `headers` is present.

- [ ] **Step 8: Add live history test**

Create `backend/tests/integration/test_multi_discovery_wechat_history_live.py`:

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

- [ ] **Step 9: Run real verification**

Run from `backend/`:

```bash
ENABLE_SCHEDULER=0 RUN_LIVE_WECHAT_HISTORY=1 WECHAT_HISTORY_ACCOUNT=腾讯技术工程 WECHAT_MP_COOKIE="$WECHAT_MP_COOKIE" WECHAT_MP_TOKEN="$WECHAT_MP_TOKEN" python -m pytest tests/integration/test_multi_discovery_wechat_history_live.py -v -s
```

Expected: the test resolves a real WeChat account through the configured default profile, saves a `multi_dsl` method without secrets, and fetches real recent history items.

- [ ] **Step 10: Commit**

```bash
git add backend/app/config.py backend/app/discovery/multi_graph.py backend/app/discovery/multi_dsl.py backend/app/discovery/multi_interpreter.py backend/app/discovery/wechat_tools.py backend/tests/integration/test_multi_discovery_wechat_history_live.py
git commit -m "Add WeChat account history discovery"
```

---

## Task 4: Final Regression Pass And Documentation Cleanup

**Complete slice:** All implemented source kinds still run through the same public API surface, old website discovery remains intact, and the live tests document the real capability boundaries.

**Files:**

- Modify: `docs/superpowers/specs/2026-07-06-multi-source-discovery-graph-design.md` only if implementation discovers a real constraint that changes the approved design.
- Modify: `AGENTS.md` / `CLAUDE.md` only if a new project-wide rule is discovered and approved by the user.

- [ ] **Step 1: Run syntax/import checks**

Run from `backend/`:

```bash
python -m py_compile app/discovery/multi_graph.py app/discovery/multi_dsl.py app/discovery/multi_interpreter.py app/discovery/wechat_tools.py app/api/discovery_routes.py app/config.py
```

Expected: no syntax errors.

- [ ] **Step 2: Run existing discovery DSL checks**

Run from `backend/`:

```bash
ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_dsl.py -v
```

Expected: existing DSL tests still pass, proving old DSL semantics were not broken.

- [ ] **Step 3: Run the real live tests that match configured capabilities**

Website:

```bash
ENABLE_SCHEDULER=0 RUN_LIVE_MULTI_DISCOVERY_WEBSITE=1 MULTI_DISCOVERY_WEBSITE_URL=https://blogs.oracle.com/linux/feed python -m pytest tests/integration/test_multi_discovery_website_live.py -v -s
```

WeChat keyword search:

```bash
ENABLE_SCHEDULER=0 RUN_LIVE_WECHAT_SEARCH=1 WECHAT_SEARCH_QUERY=Linux python -m pytest tests/integration/test_multi_discovery_wechat_search_live.py -v -s
```

WeChat history, only when auth env exists:

```bash
ENABLE_SCHEDULER=0 RUN_LIVE_WECHAT_HISTORY=1 WECHAT_HISTORY_ACCOUNT=腾讯技术工程 WECHAT_MP_COOKIE="$WECHAT_MP_COOKIE" WECHAT_MP_TOKEN="$WECHAT_MP_TOKEN" python -m pytest tests/integration/test_multi_discovery_wechat_history_live.py -v -s
```

Expected: website and search pass when public network access is available; history passes when WeChat MP auth env is valid, otherwise the test is skipped by env gates.

- [ ] **Step 4: Commit final cleanup**

```bash
git status --short
git add docs/superpowers/specs/2026-07-06-multi-source-discovery-graph-design.md AGENTS.md CLAUDE.md
git diff --cached --quiet || git commit -m "Document multi discovery implementation constraints"
```

Only commit docs in this task when implementation uncovered a real, user-approved documentation change.
