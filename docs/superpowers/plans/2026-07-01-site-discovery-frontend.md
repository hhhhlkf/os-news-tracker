# 站点发现前端 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `/discover` 页面（智能探查 + 抓取模块），并补全前端对接所需的后端缺口。

**Architecture:** 新增 `DiscoveryPage`（react-router 路由 `/discover`），用 `@xyflow/react` 渲染 SiteDiscoveryGraph 环形流程图，轮询 `GET /discovery/runs/{id}` 的 `node_trace`/`current_step` 驱动节点状态动画；抓取方式库多选批量调 `POST /discovery/methods/{id}/fetch`。后端补两个缺口：`POST /discovery/suggest-name`、`node_trace` 条目增 `summary`。

**Tech Stack:** React 19 + Vite + TypeScript + TanStack Query + react-router-dom v7 + @xyflow/react v12（前端）；FastAPI + pytest + respx（后端）；vitest（前端测试）。

**Spec:** `docs/superpowers/specs/2026-07-01-site-discovery-frontend-design.md`

**Branch:** `feature/site-discovery-agent`（已在此分支）

---

## 与 spec 的偏差（已确认）

- **日志面板**：spec §3.5 设计为 SSE `/logs/stream`。后端无该端点（`log_stream.py` 不存在），但 `GET /news-run/logs` 已能读到 discovery 日志（`_execute_discovery` 经 `append_run_log` 写入同源 `run_logs` 内存表）。本计划**改用轮询 `GET /news-run/logs` 按 `run_id` 过滤**，不新建 SSE（YAGNI；1.5s 轮询近实时）。若后续要真·SSE 再独立加。
- **重试轮次**：后端 `retry_count` 字段从不写入（恒为 0）。前端**从 `node_trace` 推断**（`auditor` 出现次数 = 尝试轮次），不补后端。
- **进行中节点**：`node_trace` 只记已完成节点；"进行中"节点由前端按固定图顺序推断（见 Task 6），不补后端 running 事件。

---

## File Structure

**后端（补缺口）**
- Modify `backend/app/api/discovery_routes.py` — 加 `POST /discovery/suggest-name`
- Modify `backend/app/discovery/graph.py` — `_execute_discovery` 给 node_trace 条目加 `summary`
- Test `backend/tests/unit/discovery/test_suggest_name.py`（新建）
- Test `backend/tests/unit/discovery/test_node_trace_summary.py`（新建）

**前端（新建）**
- `frontend/src/types.ts`（改）— discovery 类型
- `frontend/src/api/client.ts`（改）— discovery API 函数
- `frontend/src/App.tsx`（改）— BrowserRouter + 路由
- `frontend/src/pages/DiscoveryPage.tsx`（新）— 页面组装
- `frontend/src/discovery/flowState.ts`（新）— 纯函数：从 node_trace 算节点状态（TDD 核心）
- `frontend/src/discovery/flowState.test.ts`（新）
- `frontend/src/components/DiscoveryPanel.tsx`（新）— 智能探查区（输入+状态编排）
- `frontend/src/components/DiscoveryFlowChart.tsx`（新）— @xyflow/react 环形图
- `frontend/src/components/DiscoveryNodeDetail.tsx`（新）— 点节点展开产出卡
- `frontend/src/components/DiscoveryLogPanel.tsx`（新）— 轮询日志面板
- `frontend/src/components/CrawlMethodList.tsx`（新）— 方式库列表+多选+批量抓取
- `frontend/src/components/CrawlMethodDetail.tsx`（新）— 方式详情抽屉
- `frontend/src/hooks/useDiscoveryLogs.ts`（新）— 轮询 /news-run/logs 按 run_id 过滤

**依赖**
- `frontend/package.json`（改）— 加 `react-router-dom`、`@xyflow/react`

---

## Task 1: 后端 — `POST /discovery/suggest-name`

**Files:**
- Modify: `backend/app/api/discovery_routes.py`（末尾追加）
- Test: `backend/tests/unit/discovery/test_suggest_name.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/unit/discovery/test_suggest_name.py
import respx
from fastapi.testclient import TestClient
from app.entry import app

client = TestClient(app)

@respx.mock
def test_suggest_name_from_title():
    respx.get("https://openanolis.cn/").respond(
        200, text="<html><head><title>OpenAnolis 开源社区</title></head><body></body></html>"
    )
    r = client.post("/discovery/suggest-name", json={"url": "https://openanolis.cn/"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "OpenAnolis 开源社区"

@respx.mock
def test_suggest_name_falls_back_to_domain():
    respx.get("https://no-title.example.org/").respond(200, text="<html><head></head></html>")
    r = client.post("/discovery/suggest-name", json={"url": "https://no-title.example.org/"})
    assert r.status_code == 200
    assert r.json()["name"] == "no-title.example.org"

@respx.mock
def test_suggest_name_on_fetch_error_falls_back_to_domain():
    respx.get("https://down.example.net/").respond(503)
    r = client.post("/discovery/suggest-name", json={"url": "https://down.example.net/"})
    assert r.status_code == 200
    assert r.json()["name"] == "down.example.net"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_suggest_name.py -v`
Expected: FAIL（404 No match found for `/discovery/suggest-name` 或路由不存在）

- [ ] **Step 3: 实现端点**

在 `backend/app/api/discovery_routes.py` 末尾追加：

```python
class SuggestNameRequest(BaseModel):
    url: HttpUrl


def _extract_title(html: str) -> str | None:
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title or None


@router.post("/suggest-name")
def suggest_name(body: SuggestNameRequest):
    """抓首页 <title> 作站点名；失败回退域名。不调 LLM（YAGNI）。"""
    from urllib.parse import urlparse
    import httpx
    site_url = str(body.url)
    domain = urlparse(site_url).netloc.removeprefix("www.")
    try:
        resp = httpx.get(site_url, timeout=8.0, follow_redirects=True,
                         headers={"User-Agent": "os-news-tracker/discovery"})
        if resp.status_code < 400:
            title = _extract_title(resp.text)
            if title:
                return {"name": title}
    except Exception:
        pass
    return {"name": domain}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_suggest_name.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/discovery_routes.py backend/tests/unit/discovery/test_suggest_name.py
git commit -m "Add POST /discovery/suggest-name endpoint for auto site naming"
```

---

## Task 2: 后端 — node_trace 条目增 `summary`

**Files:**
- Modify: `backend/app/discovery/graph.py`（`_execute_discovery` stream 循环 + 新 helper）
- Test: `backend/tests/unit/discovery/test_node_trace_summary.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/unit/discovery/test_node_trace_summary.py
from app.discovery.graph import _step_summary

def test_summary_explorer():
    upd = {"exploration": {"source_type": "rss", "list_url": "/feed.xml", "success": True}}
    s = _step_summary("explorer", upd)
    assert s["source_type"] == "rss"
    assert s["list_url"] == "/feed.xml"

def test_summary_validator():
    upd = {"url_rule": {"mode": "existing_url", "url_field": "link", "evidence": "existing_url"}}
    s = _step_summary("validator", upd)
    assert s["mode"] == "existing_url"
    assert s["evidence"] == "existing_url"

def test_summary_dsl_writer():
    upd = {"dsl_recipe": {"actions": [{"op": "fetch"}, {"op": "extract"}, {"op": "dedup_by"}]},
           "token_used": 1200}
    s = _step_summary("dsl_writer", upd)
    assert s["actions"] == 3
    assert s["has_loop"] is False

def test_summary_auditor():
    upd = {"audit_result": {"passed": False, "issues": ["只抓单页"]},
           "attempt": 1}
    s = _step_summary("auditor", upd)
    assert s["passed"] is False
    assert s["attempt"] == 1

def test_summary_unknown_node():
    s = _step_summary("fetch_homepage", {"homepage": {"status": 200}})
    assert s == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_node_trace_summary.py -v`
Expected: FAIL（`_step_summary` 不存在，ImportError）

- [ ] **Step 3: 实现 `_step_summary` 并接入 stream 循环**

在 `backend/app/discovery/graph.py` 新增（放在 `_execute_discovery` 之前）：

```python
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
```

修改 `_execute_discovery` 的 stream 循环（将 `entry` 增加 `summary`）。把：

```python
            for chunk in g.stream(initial, config=config, stream_mode="updates"):
                for node_name in chunk:
                    entry = {"step": node_name, "status": "done",
                             "ts": datetime.now(timezone.utc).isoformat()}
                    node_trace.append(entry)
```

改为：

```python
            for chunk in g.stream(initial, config=config, stream_mode="updates"):
                for node_name, update in chunk.items():
                    entry = {"step": node_name, "status": "done",
                             "ts": datetime.now(timezone.utc).isoformat(),
                             "summary": _step_summary(node_name, update or {})}
                    node_trace.append(entry)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/test_node_trace_summary.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 回归 — 确认既有 graph 测试不破**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/discovery/ -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/discovery/graph.py backend/tests/unit/discovery/test_node_trace_summary.py
git commit -m "Add per-step summary to discovery node_trace for frontend node detail"
```

---

## Task 3: 前端 — 装依赖 + discovery 类型

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/src/types.ts`

- [ ] **Step 1: 安装依赖**

Run: `cd frontend && npm install react-router-dom @xyflow/react`
Expected: package.json 增加 `react-router-dom`、`@xyflow/react`。

- [ ] **Step 2: 在 `frontend/src/types.ts` 末尾追加 discovery 类型**

```typescript
// ---- Discovery ----
export interface DiscoveryNodeTraceEntry {
  step: string;
  status: string;
  ts: string;
  summary?: Record<string, unknown>;
}

export interface DiscoveryRun {
  id: number;
  site_url: string;
  status: "running" | "completed" | "failed";
  resulting_method_id: number | null;
  llm_token_usage: number;
  node_trace: DiscoveryNodeTraceEntry[];
  retry_count: number;
  current_step: string | null;
  started_at: string | null;
  ended_at: string | null;
  error_message: string | null;
}

export interface DiscoveryRunSummary {
  id: number;
  site_url: string;
  status: string;
  resulting_method_id: number | null;
  llm_token_usage: number;
  started_at: string | null;
  ended_at: string | null;
  error_message: string | null;
}

export type CrawlMethodStatus = "active" | "disabled" | "failed";

export interface CrawlMethod {
  id: number;
  domain: string;
  entry_url: string;
  status: CrawlMethodStatus;
  signature: string;
  last_run_at: string | null;
  last_run_status: string | null;
}

export interface CrawlMethodDetail extends CrawlMethod {
  dsl_recipe: Record<string, unknown>;
}

export interface DiscoveryFetchResult {
  discovered_count: number;
  stored_count: number;
  items: Record<string, unknown>[];
  stats: Record<string, unknown>;
  message: string;
}

export interface SuggestNameResponse { name: string; }

export interface DiscoverRunResponse {
  status: "started" | "duplicate";
  run_id?: number;
  name?: string;
  existing_method?: {
    method_id: number; domain: string; signature: string;
    dsl_recipe: Record<string, unknown>; last_run_at: string | null; last_run_status: string | null;
  };
}
```

- [ ] **Step 3: 类型检查**

Run: `cd frontend && npx tsc -b --noEmit`
Expected: 无错误（新类型未被使用也应通过）

- [ ] **Step 4: 提交**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/types.ts
git commit -m "Add react-router-dom, @xyflow/react and discovery types"
```

---

## Task 4: 前端 — API client discovery 函数

**Files:**
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: 在 `client.ts` 追加 discovery API**

在文件顶部 import 块的 type import 列表加入：

```typescript
  CrawlMethod,
  CrawlMethodDetail,
  DiscoveryFetchResult,
  DiscoveryRun,
  DiscoveryRunSummary,
  DiscoverRunResponse,
  SuggestNameResponse,
```

在 `BASE` 定义后加 `const DISCOVERY_BASE = \`${BASE}/discovery\`;`，然后在文件末尾追加：

```typescript
export async function startDiscoveryRun(
  url: string, name?: string, force = false,
): Promise<DiscoverRunResponse> {
  const r = await fetch(`${DISCOVERY_BASE}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ url, name: name ?? null, force }),
  });
  return expectOk<DiscoverRunResponse>(r, "failed to start discovery run");
}

export async function getDiscoveryRun(runId: number): Promise<DiscoveryRun> {
  const r = await fetch(`${DISCOVERY_BASE}/runs/${runId}`, { headers: authHeaders() });
  return expectOk<DiscoveryRun>(r, "failed to load discovery run");
}

export async function listDiscoveryRuns(limit = 20): Promise<DiscoveryRunSummary[]> {
  const r = await fetch(`${DISCOVERY_BASE}/runs?limit=${limit}`, { headers: authHeaders() });
  return expectOk<DiscoveryRunSummary[]>(r, "failed to load discovery runs");
}

export async function suggestDiscoveryName(url: string): Promise<SuggestNameResponse> {
  const r = await fetch(`${DISCOVERY_BASE}/suggest-name`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ url }),
  });
  return expectOk<SuggestNameResponse>(r, "failed to suggest name");
}

export async function listDiscoveryMethods(): Promise<CrawlMethod[]> {
  const r = await fetch(`${DISCOVERY_BASE}/methods`, { headers: authHeaders() });
  return expectOk<CrawlMethod[]>(r, "failed to load crawl methods");
}

export async function getDiscoveryMethod(methodId: number): Promise<CrawlMethodDetail> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/${methodId}`, { headers: authHeaders() });
  return expectOk<CrawlMethodDetail>(r, "failed to load crawl method");
}

export async function patchDiscoveryMethod(
  methodId: number, status: CrawlMethodStatus,
): Promise<{ id: number; status: string }> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/${methodId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ status }),
  });
  return expectOk<{ id: number; status: string }>(r, "failed to patch method");
}

export async function deleteDiscoveryMethod(methodId: number): Promise<void> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/${methodId}`, {
    method: "DELETE", headers: authHeaders(),
  });
  if (r.status === 204 || r.ok) return;
  const body = await parseErrorBody(r);
  throw new ApiError(r.status, `failed to delete method (HTTP ${r.status})`, body);
}

export async function fetchDiscoveryMethod(methodId: number): Promise<DiscoveryFetchResult> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/${methodId}/fetch`, {
    method: "POST", headers: authHeaders(),
  });
  return expectOk<DiscoveryFetchResult>(r, "failed to fetch method");
}
```

- [ ] **Step 2: 类型检查**

Run: `cd frontend && npx tsc -b --noEmit`
Expected: 无错误

- [ ] **Step 3: 提交**

```bash
git add frontend/src/api/client.ts
git commit -m "Add discovery API client functions"
```

---

## Task 5: 前端 — 路由 / + /discover

**Files:**
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: 改 App.tsx 为 BrowserRouter + 路由**

```tsx
import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom";
import { HomePage } from "./pages/HomePage";
import { DiscoveryPage } from "./pages/DiscoveryPage";

function TopNav() {
  const linkStyle = ({ isActive }: { isActive: boolean }): React.CSSProperties => ({
    fontSize: 13, fontWeight: 700, color: isActive ? "#175cd3" : "#667085",
    textDecoration: "none", padding: "8px 12px", borderRadius: 8,
    background: isActive ? "#eff6ff" : "transparent",
  });
  return (
    <nav style={{ display: "flex", gap: 6, padding: "10px 24px", borderBottom: "1px solid #eaecf0" }}>
      <NavLink to="/" end style={linkStyle}>新闻流</NavLink>
      <NavLink to="/discover" style={linkStyle}>站点发现</NavLink>
    </nav>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <TopNav />
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/discover" element={<DiscoveryPage />} />
      </Routes>
    </BrowserRouter>
  );
}
```

> 注：`HomePage` 现有页头里的"返回新闻流"等链接此处由顶部 `TopNav` 统一承载；`HomePage` 自身不改。

- [ ] **Step 2: 临时占位 DiscoveryPage 以便编译**

在 `frontend/src/pages/DiscoveryPage.tsx` 写最小占位（Task 13 会替换）：

```tsx
export function DiscoveryPage() {
  return <div style={{ padding: 24 }}>站点发现（建设中）</div>;
}
```

- [ ] **Step 3: 跑现有测试 + 构建**

Run: `cd frontend && npx vitest run && npm run build`
Expected: 测试通过、构建成功

- [ ] **Step 4: 提交**

```bash
git add frontend/src/App.tsx frontend/src/pages/DiscoveryPage.tsx
git commit -m "Add react-router with / and /discover routes + top nav"
```

---

## Task 6: 前端 — 流程图状态纯逻辑（TDD 核心）

**Files:**
- Create: `frontend/src/discovery/flowState.ts`
- Test: `frontend/src/discovery/flowState.test.ts`

固定图顺序与节点定义：

```typescript
// 节点 id 与显示
export type FlowNodeId =
  | "fetch_homepage" | "capture_network" | "explorer"
  | "validator" | "dsl_writer" | "auditor" | "save_method";

export interface FlowNodeMeta { id: FlowNodeId; label: string; kind: "det" | "agent"; }

export const FLOW_NODES: FlowNodeMeta[] = [
  { id: "fetch_homepage", label: "抓首页", kind: "det" },
  { id: "capture_network", label: "抓网络请求", kind: "det" },
  { id: "explorer", label: "探查", kind: "agent" },
  { id: "validator", label: "验证URL", kind: "agent" },
  { id: "dsl_writer", label: "写配方", kind: "agent" },
  { id: "auditor", label: "审计", kind: "agent" },
  { id: "save_method", label: "存库", kind: "det" },
];

// 正向顺序（用于推断"下一个进行中"）
const FORWARD: FlowNodeId[] = [
  "fetch_homepage", "capture_network", "explorer",
  "validator", "dsl_writer", "auditor", "save_method",
];
```

- [ ] **Step 1: 写失败测试**

```typescript
// frontend/src/discovery/flowState.test.ts
import { describe, it, expect } from "vitest";
import { computeNodeStates, attemptCount } from "./flowState";
import type { DiscoveryNodeTraceEntry, DiscoveryRun } from "../types";

function trace(steps: string[]): DiscoveryNodeTraceEntry[] {
  return steps.map((s) => ({ step: s, status: "done", ts: "t" }));
}

const baseRun = (over: Partial<DiscoveryRun>): DiscoveryRun => ({
  id: 1, site_url: "u", status: "running", resulting_method_id: null,
  llm_token_usage: 0, node_trace: [], retry_count: 0, current_step: null,
  started_at: null, ended_at: null, error_message: null, ...over,
});

describe("computeNodeStates", () => {
  it("空 trace + running → 第一个节点进行中", () => {
    const s = computeNodeStates(baseRun({ node_trace: [] }));
    expect(s["fetch_homepage"]).toBe("running");
    expect(s["explorer"]).toBe("pending");
  });
  it("fetch_homepage 完成 → capture_network 进行中", () => {
    const s = computeNodeStates(baseRun({ node_trace: trace(["fetch_homepage"]), current_step: "fetch_homepage" }));
    expect(s["fetch_homepage"]).toBe("done");
    expect(s["capture_network"]).toBe("running");
    expect(s["explorer"]).toBe("pending");
  });
  it("completed → 全部 done", () => {
    const s = computeNodeStates(baseRun({
      status: "completed",
      node_trace: trace(FORWARD), current_step: "save_method",
    }));
    expect(s["save_method"]).toBe("done");
    expect(s["auditor"]).toBe("done");
  });
  it("failed → 进行中节点标 failed", () => {
    const s = computeNodeStates(baseRun({
      status: "failed", node_trace: trace(["fetch_homepage", "capture_network", "explorer"]),
      current_step: "explorer", error_message: "boom",
    }));
    expect(s["validator"]).toBe("failed"); // 推断的进行中节点
    expect(s["explorer"]).toBe("done");
  });
  it("重试：auditor 出现两次 → 写配方重新进行中", () => {
    const t = trace(["fetch_homepage", "capture_network", "explorer", "validator",
      "dsl_writer", "auditor", "dsl_writer"]);
    const s = computeNodeStates(baseRun({ node_trace: t, current_step: "dsl_writer" }));
    expect(s["auditor"]).toBe("done");
    expect(s["dsl_writer"]).toBe("running");
  });
  it("save_method 在 trace 里 → done，无进行中", () => {
    const s = computeNodeStates(baseRun({ status: "completed", node_trace: trace(FORWARD), current_step: "save_method" }));
    expect(Object.values(s).every((v) => v === "done")).toBe(true);
  });
});

describe("attemptCount", () => {
  it("无 auditor → 1 轮", () => {
    expect(attemptCount(trace(["fetch_homepage"]))).toBe(1);
  });
  it("auditor 出现 2 次 → 2 轮", () => {
    expect(attemptCount(trace(["explorer", "validator", "dsl_writer", "auditor", "dsl_writer", "auditor"]))).toBe(2);
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/discovery/flowState.test.ts`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `flowState.ts`**

```typescript
// frontend/src/discovery/flowState.ts
import type { DiscoveryRun, DiscoveryNodeTraceEntry } from "../types";

export type FlowNodeId =
  | "fetch_homepage" | "capture_network" | "explorer"
  | "validator" | "dsl_writer" | "auditor" | "save_method";

export interface FlowNodeMeta { id: FlowNodeId; label: string; kind: "det" | "agent"; }

export const FLOW_NODES: FlowNodeMeta[] = [
  { id: "fetch_homepage", label: "抓首页", kind: "det" },
  { id: "capture_network", label: "抓网络请求", kind: "det" },
  { id: "explorer", label: "探查", kind: "agent" },
  { id: "validator", label: "验证URL", kind: "agent" },
  { id: "dsl_writer", label: "写配方", kind: "agent" },
  { id: "auditor", label: "审计", kind: "agent" },
  { id: "save_method", label: "存库", kind: "det" },
];

const FORWARD: FlowNodeId[] = [
  "fetch_homepage", "capture_network", "explorer",
  "validator", "dsl_writer", "auditor", "save_method",
];

export type NodeState = "pending" | "running" | "done" | "failed";

export function attemptCount(trace: DiscoveryNodeTraceEntry[]): number {
  const n = trace.filter((e) => e.step === "auditor").length;
  return Math.max(1, n);
}

function lastStep(trace: DiscoveryNodeTraceEntry[]): FlowNodeId | null {
  for (let i = trace.length - 1; i >= 0; i--) {
    if (FORWARD.includes(trace[i].step as FlowNodeId)) return trace[i].step as FlowNodeId;
  }
  return null;
}

function nextAfter(step: FlowNodeId | null): FlowNodeId | null {
  if (step === null) return FORWARD[0];
  // 重试回退：auditor 之后若 trace 末尾是 dsl_writer/validator → 回退目标
  const i = FORWARD.indexOf(step);
  if (i < 0 || i + 1 >= FORWARD.length) return null;
  return FORWARD[i + 1];
}

export function computeNodeStates(run: DiscoveryRun): Record<FlowNodeId, NodeState> {
  const states = Object.fromEntries(FLOW_NODES.map((n) => [n.id, "pending"])) as Record<FlowNodeId, NodeState>;
  const done = new Set(run.node_trace.map((e) => e.step));
  for (const id of FORWARD) if (done.has(id)) states[id] = "done";

  if (run.status === "completed") {
    for (const id of FORWARD) if (done.has(id)) states[id] = "done";
    return states;
  }

  // 推断进行中节点：trace 末尾 step 的"下一个"
  const last = lastStep(run.node_trace);
  // 重试情形：末尾是 dsl_writer 或 validator（回退重跑），且 auditor 已在 done
  let running: FlowNodeId | null = null;
  if (last === "dsl_writer" || last === "validator") {
    running = last; // 回退目标正在重跑
  } else {
    running = nextAfter(last);
  }

  if (run.status === "failed") {
    if (running && states[running] !== "done") states[running] = "failed";
    return states;
  }

  // running
  if (running && states[running] !== "done") states[running] = "running";
  return states;
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/discovery/flowState.test.ts`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/discovery/flowState.ts frontend/src/discovery/flowState.test.ts
git commit -m "Add discovery flow node-state computation (pure, TDD)"
```

---

## Task 7: 前端 — DiscoveryFlowChart（@xyflow/react 环形图）

**Files:**
- Create: `frontend/src/components/DiscoveryFlowChart.tsx`

- [ ] **Step 1: 实现环形流程图组件**

```tsx
// frontend/src/components/DiscoveryFlowChart.tsx
import { useMemo } from "react";
import { ReactFlow, Background, BackgroundVariant, MarkerType, type Node, type Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { FLOW_NODES, computeNodeStates, attemptCount, type FlowNodeId, type NodeState } from "../discovery/flowState";
import type { DiscoveryRun } from "../types";

// 固定坐标：预处理竖排居中入环，4 agent 环形，存库底部居中
const POS: Record<FlowNodeId, { x: number; y: number }> = {
  fetch_homepage: { x: 96, y: 0 },
  capture_network: { x: 96, y: 80 },
  explorer: { x: 176, y: 170 },
  validator: { x: 290, y: 250 },
  dsl_writer: { x: 176, y: 330 },
  auditor: { x: 62, y: 250 },
  save_method: { x: 176, y: 410 },
};

const STATE_STYLE: Record<NodeState, { fill: string; stroke: string; color: string; dash?: string }> = {
  pending: { fill: "#f8fafc", stroke: "#cbd5e1", color: "#94a3b8", dash: "4 3" },
  running: { fill: "#eff6ff", stroke: "#175cd3", color: "#175cd3" },
  done: { fill: "#ecfdf3", stroke: "#059669", color: "#059669" },
  failed: { fill: "#fef2f2", stroke: "#dc2626", color: "#dc2626" },
};

function NodeBox({ data }: { data: { label: string; id: string; kind: "det" | "agent"; state: NodeState } }) {
  const s = STATE_STYLE[data.state];
  const isAgent = data.kind === "agent";
  const radius = isAgent ? "50%" : "12px";
  return (
    <div style={{
      width: 132, padding: "8px 10px", textAlign: "center",
      background: s.fill, border: `2px solid ${s.stroke}`, borderRadius: radius,
      color: s.color, fontSize: 13, fontWeight: 700,
      boxShadow: data.state === "running" ? "0 0 0 4px rgba(23,92,211,0.15)" : undefined,
      borderStyle: s.dash ? "dashed" : "solid",
    }}>
      <div>{data.label}</div>
      <div style={{ fontSize: 9, fontFamily: "JetBrains Mono, monospace", opacity: 0.7 }}>{data.id}</div>
    </div>
  );
}

const nodeTypes = { flow: NodeBox };

export function DiscoveryFlowChart({ run, onSelectNode, selectedNode }: {
  run: DiscoveryRun;
  onSelectNode?: (id: FlowNodeId) => void;
  selectedNode?: FlowNodeId | null;
}) {
  const states = useMemo(() => computeNodeStates(run), [run]);
  const attempt = useMemo(() => attemptCount(run.node_trace), [run.node_trace]);

  const nodes: Node[] = useMemo(() => FLOW_NODES.map((n) => ({
    id: n.id, type: "flow", position: POS[n.id],
    data: { label: n.label, id: n.id, kind: n.kind, state: states[n.id] },
    selectable: true, selected: selectedNode === n.id,
  })), [states, selectedNode]);

  const edges: Edge[] = useMemo(() => {
    const e = (id: string, s: FlowNodeId, t: FlowNodeId, opts?: Partial<Edge>): Edge => ({
      id, source: s, target: t,
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
      ...opts,
    });
    const isRetryTarget = run.node_trace.length > 0 &&
      (run.node_trace[run.node_trace.length - 1].step === "dsl_writer" ||
       run.node_trace[run.node_trace.length - 1].step === "validator");
    return [
      e("e1", "fetch_homepage", "capture_network"),
      e("e2", "capture_network", "explorer"),
      e("e3", "explorer", "validator"),
      e("e4", "validator", "dsl_writer"),
      e("e5", "dsl_writer", "auditor"),
      e("e6", "auditor", "save_method", { label: "通过", style: { stroke: "#059669" } }),
      e("e7", "auditor", "dsl_writer", {
        label: `不通过·重试(${attempt}/3)`, animated: isRetryTarget && run.status === "running",
        style: { stroke: "#d97706", strokeDasharray: "6 4" },
      }),
    ];
  }, [run, attempt]);

  return (
    <div style={{ position: "relative", height: 520 }}>
      <ReactFlow
        nodes={nodes} edges={edges} nodeTypes={nodeTypes}
        onNodeClick={(_, n) => onSelectNode?.(n.id as FlowNodeId)}
        nodesDraggable={false} nodesConnectable={false} elementsSelectable
        panOnDrag={false} zoomOnScroll={false} zoomOnPinch={false} panOnScroll={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="#e4e8ee" />
      </ReactFlow>
      <div style={{ position: "absolute", bottom: 4, left: 0, right: 0, textAlign: "center",
        fontSize: 11, color: "#667085" }}>
        圆 = AI agent　胶囊 = 确定性步骤　绿=完成 · 蓝=进行中 · 灰=待执行 · 红=失败
      </div>
    </div>
  );
}
```

> 注：边 `e6`(通过→存库) 与 `e7`(重试回边) 并存；`e7` 仅在重试时 `animated`。`attemptCount` 用作轮次标签。

- [ ] **Step 2: 类型检查 + 构建确认 reactflow 可渲染**

Run: `cd frontend && npx tsc -b --noEmit && npm run build`
Expected: 无错误、构建成功

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/DiscoveryFlowChart.tsx
git commit -m "Add DiscoveryFlowChart reactflow ring diagram"
```

---

## Task 8: 前端 — DiscoveryNodeDetail（点节点展开）

**Files:**
- Create: `frontend/src/components/DiscoveryNodeDetail.tsx`

- [ ] **Step 1: 实现**

```tsx
// frontend/src/components/DiscoveryNodeDetail.tsx
import { Fragment } from "react";
import { FLOW_NODES, type FlowNodeId } from "../discovery/flowState";
import type { DiscoveryNodeTraceEntry } from "../types";

const LABELS: Record<FlowNodeId, string> = Object.fromEntries(FLOW_NODES.map((n) => [n.id, n.label])) as Record<FlowNodeId, string>;

export function DiscoveryNodeDetail({ nodeId, entry }: {
  nodeId: FlowNodeId; entry?: DiscoveryNodeTraceEntry;
}) {
  const meta = FLOW_NODES.find((n) => n.id === nodeId);
  if (!meta) return null;
  const summary = (entry?.summary ?? {}) as Record<string, unknown>;
  const rows = Object.entries(summary).filter(([, v]) => v !== null && v !== undefined);
  return (
    <div style={{ border: "1px solid #b9d4ff", borderRadius: 10, background: "#f8fbff", padding: "12px 14px" }}>
      <div style={{ fontSize: 12, fontWeight: 800, color: "#175cd3", marginBottom: 8 }}>
        ▸ {LABELS[nodeId]} <span style={{ color: "#98a2b3", fontFamily: "JetBrains Mono, monospace", fontWeight: 500 }}>{nodeId}</span>
      </div>
      {rows.length === 0 ? (
        <div style={{ fontSize: 12, color: "#98a2b3" }}>该步尚无产出摘要。</div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 12px", fontSize: 12, color: "#344054" }}>
          {rows.map(([k, v]) => (
            <Fragment key={k}><span style={{ color: "#667085" }}>{k}</span><span style={{ fontFamily: "JetBrains Mono, monospace" }}>{String(v)}</span></Fragment>
          ))}
        </div>
      )}
      {entry?.status !== "done" && <div style={{ fontSize: 11, color: "#175cd3", fontStyle: "italic", marginTop: 8 }}>进行中…</div>}
    </div>
  );
}
```

- [ ] **Step 2: 类型检查**

Run: `cd frontend && npx tsc -b --noEmit`
Expected: 无错误

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/DiscoveryNodeDetail.tsx
git commit -m "Add DiscoveryNodeDetail card for per-step output"
```

---

## Task 9: 前端 — useDiscoveryLogs + DiscoveryLogPanel

**Files:**
- Create: `frontend/src/hooks/useDiscoveryLogs.ts`
- Create: `frontend/src/components/DiscoveryLogPanel.tsx`

- [ ] **Step 1: 实现 useDiscoveryLogs（轮询 /news-run/logs 按 run_id 过滤）**

```typescript
// frontend/src/hooks/useDiscoveryLogs.ts
import { useEffect, useRef, useState } from "react";
import { authHeaders } from "../auth";
import type { NewsRunLogEntry } from "../types";

export function useDiscoveryLogs(runId: number | null, enabled: boolean) {
  const [logs, setLogs] = useState<NewsRunLogEntry[]>([]);
  const lastId = useRef(0);
  useEffect(() => {
    if (!enabled || runId == null) return;
    let stop = false;
    async function poll() {
      try {
        const base = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";
        const r = await fetch(`${base}/news-run/logs?after_id=${lastId.current}`, { headers: authHeaders() });
        if (!r.ok) return;
        const data = (await r.json()) as { logs: NewsRunLogEntry[] };
        const mine = data.logs.filter((l) => Number((l as Record<string, unknown>).run_id) === runId);
        if (mine.length) lastId.current = Math.max(lastId.current, ...mine.map((l) => l.id));
        if (!stop) setLogs((prev) => [...prev, ...mine]);
      } catch { /* ignore */ }
    }
    poll();
    const t = window.setInterval(poll, 1500);
    return () => { stop = true; window.clearInterval(t); };
  }, [runId, enabled]);
  return logs;
}
```

- [ ] **Step 2: 实现 DiscoveryLogPanel**

```tsx
// frontend/src/components/DiscoveryLogPanel.tsx
import type { NewsRunLogEntry } from "../types";

function ts(t: string) {
  const d = new Date(t); return Number.isNaN(d.getTime()) ? "--:--:--" : d.toLocaleTimeString("zh-CN", { hour12: false });
}

export function DiscoveryLogPanel({ logs }: { logs: NewsRunLogEntry[] }) {
  const recent = logs.slice(-80).reverse();
  return (
    <div style={{ background: "#0b1220", borderRadius: 10, padding: "12px 14px",
      fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 12, lineHeight: 1.7, color: "#d0d5dd",
      display: "flex", flexDirection: "column", minHeight: 280 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10 }}>
        <span style={{ color: "#f8fafc", fontWeight: 700, fontSize: 13 }}>实时日志</span>
        <span style={{ color: "#98a2b3", fontSize: 11 }}>轮询 /news-run/logs</span>
      </div>
      <div style={{ overflowY: "auto", flex: 1 }}>
        {recent.length === 0 ? (
          <div style={{ color: "#98a2b3" }}>暂无日志。开始探查后这里实时显示各步进度。</div>
        ) : recent.map((l) => (
          <div key={l.id}>
            <span style={{ color: "#7cd4fd" }}>{ts(l.ts)}</span>{" "}
            <span style={{ color: "#a6f4c5" }}>[{l.stage}]</span>{" "}
            <span>{l.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: 类型检查**

Run: `cd frontend && npx tsc -b --noEmit`
Expected: 无错误

- [ ] **Step 4: 提交**

```bash
git add frontend/src/hooks/useDiscoveryLogs.ts frontend/src/components/DiscoveryLogPanel.tsx
git commit -m "Add discovery log panel polling /news-run/logs filtered by run_id"
```

---

## Task 10: 前端 — DiscoveryPanel（输入 + 状态编排）

**Files:**
- Create: `frontend/src/components/DiscoveryPanel.tsx`

- [ ] **Step 1: 实现 DiscoveryPanel**

```tsx
// frontend/src/components/DiscoveryPanel.tsx
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, getDiscoveryRun, startDiscoveryRun, suggestDiscoveryName } from "../api/client";
import { DiscoveryFlowChart } from "./DiscoveryFlowChart";
import { DiscoveryNodeDetail } from "./DiscoveryNodeDetail";
import { DiscoveryLogPanel } from "./DiscoveryLogPanel";
import { useDiscoveryLogs } from "../hooks/useDiscoveryLogs";
import { attemptCount, type FlowNodeId } from "../discovery/flowState";

export function DiscoveryPanel({ onMethodAdded }: { onMethodAdded?: (methodId: number) => void }) {
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [runId, setRunId] = useState<number | null>(null);
  const [dup, setDup] = useState<{ method_id: number; domain: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<FlowNodeId | null>(null);
  const qc = useQueryClient();

  const runQuery = useQuery({
    queryKey: ["discovery-run", runId],
    queryFn: () => getDiscoveryRun(runId!),
    enabled: runId != null,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1500 : false),
  });
  const logs = useDiscoveryLogs(runId, runId != null && runQuery.data?.status === "running");

  const startMut = useMutation({
    mutationFn: (vars: { url: string; name?: string; force: boolean }) =>
      startDiscoveryRun(vars.url, vars.name, vars.force),
    onSuccess: (res) => {
      setError(null);
      if (res.status === "started" && res.run_id != null) { setDup(null); setRunId(res.run_id); }
      else if (res.status === "duplicate" && res.existing_method) {
        setDup({ method_id: res.existing_method.method_id, domain: res.existing_method.domain });
      }
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "启动探查失败"),
  });

  const nameMut = useMutation({
    mutationFn: (u: string) => suggestDiscoveryName(u),
    onSuccess: (r) => setName(r.name),
    onError: () => {},
  });

  const running = runQuery.data?.status === "running";
  const completed = runQuery.data?.status === "completed";
  const failed = runQuery.data?.status === "failed";

  // 完成后通知新方式（useEffect + ref 守卫，避免渲染期副作用）
  const notifiedRef = useRef<number | null>(null);
  useEffect(() => {
    const mid = runQuery.data?.resulting_method_id;
    if (completed && mid != null && notifiedRef.current !== mid) {
      notifiedRef.current = mid;
      onMethodAdded?.(mid);
    }
  }, [completed, runQuery.data?.resulting_method_id, onMethodAdded]);

  const selectedEntry = runQuery.data?.node_trace.find((e) => e.step === selectedNode);

  return (
    <section style={{ background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>智能探查</div>
        {runQuery.data && (
          <div style={{
            fontSize: 12, fontWeight: 700, borderRadius: 999, padding: "3px 11px",
            color: running ? "#175cd3" : completed ? "#059669" : "#dc2626",
            background: running ? "#eff6ff" : completed ? "#ecfdf3" : "#fef2f2",
            border: `1px solid ${running ? "#b9d4ff" : completed ? "#a3e0c4" : "#fca5a5"}`,
          }}>
            {running ? `探查中 · 第 ${attemptCount(runQuery.data.node_trace)} / 3 轮` : completed ? "探查完成" : "探查失败"}
          </div>
        )}
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 14 }}>
        <input className="mock-input" placeholder="站点 URL，如 openanolis.cn/blog" value={url}
          onChange={(e) => setUrl(e.target.value)} style={{ flex: "1 1 260px", minWidth: 200 }} />
        <div style={{ display: "flex", gap: 6, flex: "1 1 210px", minWidth: 190 }}>
          <input className="mock-input" placeholder="名称（选填）" value={name}
            onChange={(e) => setName(e.target.value)} style={{ flex: 1 }} />
          <button type="button" onClick={() => url && nameMut.mutate(url)}
            disabled={!url || nameMut.isPending}
            style={btnGhost}>✨ 自动</button>
        </div>
        <button type="button" disabled={!url || running} onClick={() => startMut.mutate({ url, name: name || undefined, force: false })}
          style={running ? btnDisabled : btnPrimary}>{running ? "探查中…" : "开始探查"}</button>
      </div>

      {dup && (
        <div style={{ border: "1px solid #fec84b", background: "#fffaeb", color: "#b54708", borderRadius: 8, padding: "10px 12px", marginBottom: 12, fontSize: 13 }}>
          该域名已有爬取方式（{dup.domain}）。是否覆盖重新探查？
          <button type="button" style={{ ...btnPrimary, marginLeft: 12 }}
            onClick={() => { startMut.mutate({ url, name: name || undefined, force: true }); setDup(null); }}>覆盖重探</button>
          <button type="button" style={{ ...btnGhost, marginLeft: 8 }} onClick={() => setDup(null)}>取消</button>
        </div>
      )}

      {runQuery.data && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ border: "1px solid #eaecf0", borderRadius: 10, background: "#f8fafc", padding: 10 }}>
              <DiscoveryFlowChart run={runQuery.data} onSelectNode={setSelectedNode} selectedNode={selectedNode} />
            </div>
            {selectedNode && <DiscoveryNodeDetail nodeId={selectedNode} entry={selectedEntry} />}
            {completed && (
              <div style={{ border: "1px solid #a3e0c4", background: "#ecfdf3", color: "#059669", borderRadius: 8, padding: "10px 12px", fontSize: 13 }}>
                探查完成 · 已存入爬取方式库 <a style={{ color: "#175cd3", cursor: "pointer", marginLeft: 8 }} onClick={() => runQuery.data?.resulting_method_id && onMethodAdded?.(runQuery.data.resulting_method_id)}>查看新方式 →</a>
              </div>
            )}
            {failed && (
              <div style={{ border: "1px solid #fca5a5", background: "#fef2f2", color: "#b42318", borderRadius: 8, padding: "10px 12px", fontSize: 13 }}>
                探查失败：{runQuery.data.error_message ?? "未知错误"}
                <button type="button" style={{ ...btnPrimary, marginLeft: 12 }} onClick={() => startMut.mutate({ url, name: name || undefined, force: false })}>重新探查</button>
              </div>
            )}
          </div>
          <DiscoveryLogPanel logs={logs} />
        </div>
      )}
      {error && <div style={{ color: "#b42318", fontSize: 13, marginTop: 10 }}>{error}</div>}
    </section>
  );
}

const btnPrimary: React.CSSProperties = { border: "none", borderRadius: 999, padding: "9px 16px", background: "#175cd3", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const btnDisabled: React.CSSProperties = { ...btnPrimary, background: "#98a2b3", cursor: "not-allowed" };
const btnGhost: React.CSSProperties = { border: "1px solid #d0d5dd", background: "#fff", borderRadius: 8, padding: "9px 11px", fontSize: 12, color: "#475467", cursor: "pointer" };
```

> 注：`onMethodAdded` 由 `DiscoveryPage` 接收，触发方式库 invalidate + 高亮新方式（Task 13）。重复渲染调用 `onMethodAdded` 用 ref 守卫避免 effect 风暴（见 Task 13 优化）。

- [ ] **Step 2: 类型检查**

Run: `cd frontend && npx tsc -b --noEmit`
Expected: 无错误

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/DiscoveryPanel.tsx
git commit -m "Add DiscoveryPanel: input, run orchestration, states, duplicate handling"
```

---

## Task 11: 前端 — CrawlMethodList（多选 + 批量抓取）

**Files:**
- Create: `frontend/src/components/CrawlMethodList.tsx`

- [ ] **Step 1: 实现**

```tsx
// frontend/src/components/CrawlMethodList.tsx
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchDiscoveryMethod, listDiscoveryMethods } from "../api/client";
import type { CrawlMethod } from "../types";

type RowState = { kind: "idle" } | { kind: "running" } | { kind: "done"; discovered: number; stored: number } | { kind: "error"; msg: string };

export function CrawlMethodList({ onOpenMethod, highlightId }: {
  onOpenMethod?: (id: number) => void; highlightId?: number | null;
}) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [rowStates, setRowStates] = useState<Record<number, RowState>>({});
  const [summary, setSummary] = useState<string | null>(null);

  const list = useQuery({ queryKey: ["discovery-methods"], queryFn: listDiscoveryMethods });
  const fetchMut = useMutation({
    mutationFn: (id: number) => fetchDiscoveryMethod(id),
  });

  async function batchFetch() {
    const ids = [...selected];
    setSummary(null);
    let totalDisc = 0, totalStored = 0;
    await Promise.all(ids.map(async (id) => {
      setRowStates((s) => ({ ...s, [id]: { kind: "running" } }));
      try {
        const r = await fetchMut.mutateAsync(id);
        totalDisc += r.discovered_count; totalStored += r.stored_count;
        setRowStates((s) => ({ ...s, [id]: { kind: "done", discovered: r.discovered_count, stored: r.stored_count } }));
      } catch (e) {
        setRowStates((s) => ({ ...s, [id]: { kind: "error", msg: e instanceof ApiError ? e.message : "抓取失败" } }));
      }
    }));
    setSummary(`本次抓取 ${totalDisc} 条 · 入库 ${totalStored} 条`);
    await qc.invalidateQueries({ queryKey: ["discovery-methods"] });
  }

  function toggle(id: number, enabled: boolean) {
    setSelected((s) => { const n = new Set(s); enabled ? n.add(id) : n.delete(id); return n; });
  }

  return (
    <section style={{ background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16, marginTop: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>抓取模块 · 爬取方式库</div>
          <div style={{ fontSize: 12, color: "#667085", marginTop: 2 }}>勾选若干个一键抓取，结果进入新闻流。</div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 12, color: "#475467" }}>已选 <b style={{ color: "#101828" }}>{selected.size}</b> 个</span>
          <button type="button" style={btnPrimary} disabled={selected.size === 0} onClick={batchFetch}>抓取选中</button>
          <a style={{ fontSize: 12, color: "#667085", cursor: "pointer" }} onClick={() => setSelected(new Set())}>清空</a>
        </div>
      </div>

      {summary && <div style={{ fontSize: 13, color: "#059669", marginBottom: 10 }}>{summary} · <a style={{ color: "#175cd3", cursor: "pointer" }} onClick={() => (window.location.href = "/")}>查看入库条目 →</a></div>}

      <div style={{ display: "grid", gap: 8 }}>
        {(list.data ?? []).map((m) => (
          <MethodRow key={m.id} m={m} selected={selected.has(m.id)} state={rowStates[m.id]}
            onToggle={(en) => toggle(m.id, en)} onOpen={() => onOpenMethod?.(m.id)} highlight={highlightId === m.id} />
        ))}
        {list.data && list.data.length === 0 && (
          <div style={{ border: "1px dashed #d0d5dd", borderRadius: 8, padding: 16, color: "#667085", fontSize: 13 }}>
            还没有爬取方式。用上方"智能探查"为一个网站生成爬取方式。
          </div>
        )}
      </div>
    </section>
  );
}

function MethodRow({ m, selected, state, onToggle, onOpen, highlight }: {
  m: CrawlMethod; selected: boolean; state?: RowState;
  onToggle: (enabled: boolean) => void; onOpen: () => void; highlight: boolean;
}) {
  const disabled = m.status === "disabled";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, border: `1px solid ${selected ? "#b9d4ff" : highlight ? "#175cd3" : "#eaecf0"}`,
      borderRadius: 9, padding: "9px 11px", background: selected ? "#f8fbff" : highlight ? "#eff6ff" : "#fff" }}>
      <input type="checkbox" checked={selected} disabled={disabled} onChange={(e) => onToggle(e.target.checked)} />
      <div style={{ flex: 1, minWidth: 0, cursor: "pointer" }} onClick={onOpen}>
        <div style={{ fontSize: 14, fontWeight: 700, color: disabled ? "#98a2b3" : "#101828" }}>
          {m.domain} <span style={badge(m.status)}>{m.status}</span>
        </div>
        <div style={{ fontSize: 12, color: "#667085", wordBreak: "break-all" }}>{m.entry_url}</div>
      </div>
      <div style={{ fontSize: 12, color: "#475467", textAlign: "right", minWidth: 150 }}>
        {state?.kind === "running" && <span style={{ color: "#175cd3" }}>抓取中…</span>}
        {state?.kind === "done" && <>抓取 {state.discovered} · 入库 {state.stored}</>}
        {state?.kind === "error" && <span style={{ color: "#b42318" }}>{state.msg}</span>}
        {(!state || state.kind === "idle") && (m.last_run_status ? `${m.last_run_status}` : "未运行")}
      </div>
    </div>
  );
}

function badge(status: string): React.CSSProperties {
  const base: React.CSSProperties = { fontSize: 11, fontWeight: 700, padding: "2px 8px", borderRadius: 999, marginLeft: 6 };
  if (status === "active") return { ...base, background: "#ecfdf3", color: "#027a48" };
  if (status === "failed") return { ...base, background: "#fef2f2", color: "#b42318" };
  return { ...base, background: "#f2f4f7", color: "#667085" };
}

const btnPrimary: React.CSSProperties = { border: "none", borderRadius: 999, padding: "8px 16px", background: "#175cd3", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer" };
```

- [ ] **Step 2: 类型检查**

Run: `cd frontend && npx tsc -b --noEmit`
Expected: 无错误

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/CrawlMethodList.tsx
git commit -m "Add CrawlMethodList with multi-select batch fetch"
```

---

## Task 12: 前端 — CrawlMethodDetail 抽屉

**Files:**
- Create: `frontend/src/components/CrawlMethodDetail.tsx`

- [ ] **Step 1: 实现**

```tsx
// frontend/src/components/CrawlMethodDetail.tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, deleteDiscoveryMethod, getDiscoveryMethod, patchDiscoveryMethod } from "../api/client";

export function CrawlMethodDetail({ methodId, onClose }: { methodId: number; onClose: () => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["discovery-method", methodId], queryFn: () => getDiscoveryMethod(methodId) });
  const patchMut = useMutation({
    mutationFn: (status: "active" | "disabled") => patchDiscoveryMethod(methodId, status),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["discovery-methods"] }),
  });
  const delMut = useMutation({
    mutationFn: () => deleteDiscoveryMethod(methodId),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["discovery-methods"] }); onClose(); },
    onError: (e) => alert(e instanceof ApiError ? e.message : "删除失败"),
  });

  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,0.42)", display: "flex", justifyContent: "flex-end" }}>
      <div onClick={(e) => e.stopPropagation()} style={{ width: 560, maxWidth: "92vw", background: "#fff", height: "100%", overflowY: "auto", boxShadow: "-24px 0 48px rgba(16,24,40,0.16)", padding: 20 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <div style={{ fontSize: 18, fontWeight: 800 }}>{q.data?.domain ?? "加载中…"}</div>
          <a style={{ cursor: "pointer", color: "#667085" }} onClick={onClose}>关闭</a>
        </div>
        <div style={{ fontSize: 12, color: "#667085", wordBreak: "break-all", marginBottom: 12 }}>{q.data?.entry_url}</div>
        {q.data && (
          <>
            <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
              <button type="button" style={btnPrimary} disabled={patchMut.isPending}
                onClick={() => patchMut.mutate(q.data!.status === "active" ? "disabled" : "active")}>
                {q.data.status === "active" ? "禁用" : "启用"}
              </button>
              <button type="button" style={btnDanger} disabled={delMut.isPending}
                onClick={() => { if (confirm("删除该爬取方式？")) delMut.mutate(); }}>删除</button>
            </div>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 8 }}>DSL Recipe</div>
            <pre style={{ background: "#0b1220", color: "#d0d5dd", borderRadius: 8, padding: 12, fontSize: 12, overflow: "auto", fontFamily: "JetBrains Mono, monospace" }}>
{JSON.stringify(q.data.dsl_recipe, null, 2)}
            </pre>
          </>
        )}
      </div>
    </div>
  );
}

const btnPrimary: React.CSSProperties = { border: "none", borderRadius: 999, padding: "8px 16px", background: "#175cd3", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const btnDanger: React.CSSProperties = { border: "1px solid #fecdca", borderRadius: 999, padding: "8px 16px", background: "#fff", color: "#b42318", fontSize: 13, fontWeight: 700, cursor: "pointer" };
```

- [ ] **Step 2: 类型检查**

Run: `cd frontend && npx tsc -b --noEmit`
Expected: 无错误

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/CrawlMethodDetail.tsx
git commit -m "Add CrawlMethodDetail slide-over (DSL view, enable/disable, delete)"
```

---

## Task 13: 前端 — DiscoveryPage 组装 + 集成 + 状态

**Files:**
- Modify: `frontend/src/pages/DiscoveryPage.tsx`

- [ ] **Step 1: 实现 DiscoveryPage 组装**

```tsx
// frontend/src/pages/DiscoveryPage.tsx
import { useEffect, useState } from "react";
import { DiscoveryPanel } from "../components/DiscoveryPanel";
import { CrawlMethodList } from "../components/CrawlMethodList";
import { CrawlMethodDetail } from "../components/CrawlMethodDetail";

export function DiscoveryPage() {
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const [openMethod, setOpenMethod] = useState<number | null>(null);

  // 完成后高亮新方式 3 秒
  useEffect(() => {
    if (highlightId == null) return;
    const t = window.setTimeout(() => setHighlightId(null), 4000);
    return () => window.clearTimeout(t);
  }, [highlightId]);

  return (
    <div style={{ minHeight: "100vh", background: "#f5f7fb" }}>
      <div style={{ maxWidth: 1280, margin: "0 auto", padding: 24 }}>
        <header style={{ background: "#101828", color: "#f8fafc", borderRadius: 8, padding: 24, marginBottom: 20 }}>
          <div style={{ fontSize: 13, color: "#98a2b3", marginBottom: 10 }}>OS News Tracker</div>
          <h1 style={{ margin: 0, fontSize: 28, lineHeight: 1.2 }}>站点发现</h1>
          <p style={{ marginTop: 10, color: "#d0d5dd" }}>输入网站，AI 探查员摸清爬取门道，沉淀为可复用的爬取方式。</p>
        </header>

        <DiscoveryPanel onMethodAdded={(id) => setHighlightId(id)} />
        <CrawlMethodList onOpenMethod={setOpenMethod} highlightId={highlightId} />

        {openMethod != null && (
          <CrawlMethodDetail methodId={openMethod} onClose={() => setOpenMethod(null)} />
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: 类型检查 + 构建 + 全量测试**

Run: `cd frontend && npx tsc -b --noEmit && npm run build && npx vitest run`
Expected: 无错误、构建成功、测试全过

- [ ] **Step 3: 冒烟验证（手动，记录在 spec §11）**

启动后端与前端：后端 `uvicorn app.entry:app --port 8000`；前端 `npm run dev`。打开 `/discover`，输入 `https://openanolis.cn/`，点 ✨自动 命名，点开始探查，观察流程图节点逐个变绿、当前步蓝脉冲、日志面板滚动；完成后方式库出现新高亮行；勾选 + 抓取选中，观察行内抓取中→结果；点行打开抽屉查看 DSL、禁用、删除。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/pages/DiscoveryPage.tsx
git commit -m "Assemble DiscoveryPage: panel + method list + detail slide-over"
```

---

## Self-Review

**1. Spec 覆盖**
- 路由 `/discover` + TopNav → Task 5 ✓
- 智能探查输入 + ✨自动命名 → Task 1（后端）+ Task 10 ✓
- 环形流程图（reactflow，圆形 agent/胶囊 det）→ Task 7 ✓
- 节点状态动画（node_trace/current_step 驱动 + 推断进行中 + 重试）→ Task 6（纯逻辑）+ Task 7 ✓
- 节点详情卡 → Task 2（后端 summary）+ Task 8 ✓
- 日志面板 → Task 9（轮询 /news-run/logs，**非 SSE**，已声明偏差）✓
- 六态（空闲/运行/完成/失败/重试/去重）→ Task 10 ✓
- 抓取方式库 + 多选 + 批量抓取 + 行内态 + 汇总 → Task 11 ✓
- 方式详情抽屉（DSL/启停/删除）→ Task 12 ✓
- 完成高亮新方式 + 跳新闻流 → Task 13 ✓
- 后端补缺：suggest-name（Task 1）、node_trace summary（Task 2）✓
- 新依赖 react-router-dom / @xyflow/react → Task 3 ✓
- 回归不破 HomePage → Task 5/13 构建测试 ✓

**2. 占位符扫描**：无 TBD/TODO；每步含真实代码与命令。

**3. 类型一致性**：`FlowNodeId`、`NodeState`、`computeNodeStates`、`attemptCount`、`DiscoveryRun`/`CrawlMethod`/`CrawlMethodDetail` 在各 Task 间命名一致；API 函数名与 client.ts 一致。

**已知偏差（已声明）**：日志用轮询 `/news-run/logs` 而非 SSE；重试轮次从 node_trace 推断而非 `retry_count`；进行中节点前端推断。

---

## Execution Handoff

计划已保存至 `docs/superpowers/plans/2026-07-01-site-discovery-frontend.md`。两种执行方式：

**1. Subagent-Driven（推荐）** — 每个 Task 派一个独立 subagent 实现，任务间 review，迭代快。
**2. Inline Execution** — 在当前会话按 executing-plans 批量执行，带检查点 review。

选哪种？
