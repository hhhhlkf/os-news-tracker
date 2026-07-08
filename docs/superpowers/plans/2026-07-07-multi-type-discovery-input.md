-- Active: 1781163446101@@127.0.0.1@5432@osnews_empty_test
# Multi-Type Discovery Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade `DiscoveryPanel` from a website-only discovery form into a strict multi-type discovery console with explicit route selection, `XX：XXX` parsing, route-aware auto naming, persisted route state, and route/name-first logs.

**Architecture:** Keep the existing `DiscoveryPage` and `DiscoveryPanel` layout, but split the new behavior into three bounded layers: shared request/response contracts, frontend route-input parsing and persistence, and final start/name/log integration. The backend remains incremental: expand the existing discovery endpoints so the frontend can submit locked route state instead of relying on implicit backend guesses.

**Tech Stack:** React 19, TypeScript, TanStack Query, inline styles, FastAPI, Pydantic, existing `/discovery/*` APIs, existing discovery run log pipeline

**Spec:** `docs/superpowers/specs/2026-07-07-multi-type-discovery-input-design.md`

**Commit Policy:** Do not create commits during execution unless the user explicitly asks for a commit.

---

## Scope Check

This spec is one subsystem, not multiple independent projects. The work can be implemented as one focused plan with three user-visible checkpoints:

1. Shared route contract and backend readiness
2. Frontend input structure, strict parsing, and disabled-state validation
3. Route-aware naming, start submission, and route/name logs

Each task below ends with a **mandatory user verification gate**. Do not begin the next task until the user has manually tried the current module and confirmed it passes.

---

## File Change Map

| File | Action | Responsibility |
| --- | --- | --- |
| `backend/app/api/discovery_routes.py` | Modify | Accept multi-type discovery start/name requests and emit route/name logs |
| `backend/app/discovery/multi_graph.py` | Modify | Honor explicit route choice and fixed-name rules for website / wechat / internal forum |
| `backend/app/discovery/naming.py` | Modify | Centralize prefixed display-name helpers used by route-aware naming |
| `frontend/src/types.ts` | Modify | Add route-type unions and multi-type discovery request/response shapes |
| `frontend/src/api/client.ts` | Modify | Upgrade discovery start/name functions to send locked route state |
| `frontend/src/discovery/routeInput.ts` | Create | Parse `XX：XXX`, validate by route kind, compute resolved route state |
| `frontend/src/components/DiscoveryPanel.tsx` | Modify | Add route selector, strict input field, persistence, disabled-state logic, route-aware naming, route-aware start |
| `frontend/src/components/DiscoveryLogPanel.tsx` | Modify | Recognize and label new `命名` stage clearly beside `路由` |
| `frontend/src/hooks/useDiscoveryLogs.ts` | Modify | Include `命名` in discovery-related log filtering |

---

## Shared Contracts

Use these exact frontend names unless execution reveals an existing naming conflict:

```ts
export type DiscoveryRouteType = "website" | "wechat_search" | "wechat_history" | "internal_forum";

export type DiscoveryRouteSource = "explicit" | "inferred";

export interface ParsedDiscoveryInput {
  prefix: "网页" | "微信搜索" | "微信公众号" | "司内论坛" | null;
  value: string;
  formatValid: boolean;
}

export interface ResolvedDiscoveryRoute {
  selectedRouteType: DiscoveryRouteType | null;
  resolvedRouteType: DiscoveryRouteType | null;
  routeSource: DiscoveryRouteSource;
  prefixMatchesSelection: boolean;
  validationError: string | null;
}
```

Use these exact backend request fields unless a stronger existing contract is discovered:

```json
{
  "input": "微信公众号：龙蜥社区",
  "selected_route_type": "wechat_history",
  "resolved_route_type": "wechat_history",
  "route_source": "explicit",
  "name": "公众号：龙蜥社区",
  "force": false
}
```

---

### Task 1: Shared Route Contract and Backend Readiness

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `backend/app/api/discovery_routes.py`
- Modify: `backend/app/discovery/multi_graph.py`
- Modify: `backend/app/discovery/naming.py`

- [ ] **Step 1: Add the route-type and request/response shapes to `frontend/src/types.ts`**

Append the following beside the existing discovery types:

```ts
export type DiscoveryRouteType = "website" | "wechat_search" | "wechat_history" | "internal_forum";

export type DiscoveryRouteSource = "explicit" | "inferred";

export interface DiscoveryRouteInfo {
  selected_route_type: DiscoveryRouteType | null;
  resolved_route_type: DiscoveryRouteType | null;
  route_source: DiscoveryRouteSource;
}

export interface MultiDiscoveryStartRequest extends DiscoveryRouteInfo {
  input: string;
  force: boolean;
  name?: string | null;
}

export interface MultiDiscoveryNameRequest extends DiscoveryRouteInfo {
  input: string;
}

export interface MultiDiscoveryStartResponse {
  status: "started" | "duplicate" | "completed" | "accepted";
  run_id?: number | null;
  method_id?: number | null;
  name?: string;
  route?: {
    kind: string;
    input_type: string;
    normalized_input: string;
  };
  resolved_route_type?: DiscoveryRouteType | null;
  route_source?: DiscoveryRouteSource;
  existing_method?: {
    method_id: number;
    domain: string;
    signature: string;
    dsl_recipe: Record<string, unknown>;
    last_run_at: string | null;
    last_run_status: string | null;
  };
}

export interface MultiDiscoveryNameResponse {
  name: string;
  resolved_route_type: DiscoveryRouteType | null;
}
```

- [ ] **Step 2: Upgrade `frontend/src/api/client.ts` to use route-aware discovery requests**

Replace the website-only helper signatures with route-aware ones:

```ts
export async function startDiscoveryRun(
  request: MultiDiscoveryStartRequest,
): Promise<MultiDiscoveryStartResponse> {
  const r = await fetch(`${DISCOVERY_BASE}/multi-run`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<MultiDiscoveryStartResponse>(r, "failed to start discovery run");
}

export async function suggestDiscoveryName(
  request: MultiDiscoveryNameRequest,
): Promise<MultiDiscoveryNameResponse> {
  const r = await fetchWithTimeout(
    `${DISCOVERY_BASE}/suggest-name`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(request),
    },
    SUGGEST_NAME_TIMEOUT_MS,
  );
  return expectOk<MultiDiscoveryNameResponse>(r, "failed to suggest name");
}
```

The old positional signatures:

```ts
startDiscoveryRun(url: string, name?: string, force = false)
suggestDiscoveryName(url: string)
```

must be removed so later tasks cannot accidentally keep using website-only semantics.

- [ ] **Step 3: Add backend request models and route-type mapping in `backend/app/api/discovery_routes.py`**

Near the existing multi-discovery request section, define a stricter request body that carries the locked route state:

```py
class RouteType(str, Enum):
    WEBSITE = "website"
    WECHAT_SEARCH = "wechat_search"
    WECHAT_HISTORY = "wechat_history"
    INTERNAL_FORUM = "internal_forum"


class RouteSource(str, Enum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"


class MultiDiscoverRequest(BaseModel):
    input: str
    force: bool = False
    name: str | None = None
    selected_route_type: RouteType | None = None
    resolved_route_type: RouteType | None = None
    route_source: RouteSource = RouteSource.INFERRED
```

Then add a helper that translates frontend route types into the backend hint contract already used by `source_router_for_input()`:

```py
def _route_type_to_hints(route_type: RouteType | None) -> dict[str, Any]:
    if route_type == RouteType.WEBSITE:
        return {"source_kind": "website"}
    if route_type == RouteType.WECHAT_SEARCH:
        return {"source_kind": "wechat_search"}
    if route_type == RouteType.WECHAT_HISTORY:
        return {"source_kind": "wechat_history"}
    if route_type == RouteType.INTERNAL_FORUM:
        return {"source_kind": "internal_forum"}
    return {}
```

- [ ] **Step 4: Make `/discovery/multi-run` honor explicit route choice and emit route logs**

Replace the current body forwarding with a route-aware call:

```py
@router.post("/multi-run")
def discover_multi_run(body: MultiDiscoverRequest):
    from app.run_logs import append_run_log

    effective_route = body.resolved_route_type or body.selected_route_type
    append_run_log(
        "路由",
        "已锁定最终路由",
        input=body.input,
        selected_route_type=body.selected_route_type.value if body.selected_route_type else None,
        resolved_route_type=effective_route.value if effective_route else None,
        route_source=body.route_source.value,
    )

    return start_multi_discovery_run(
        body.input,
        force=body.force,
        name=body.name,
        hints=_route_type_to_hints(effective_route),
        selected_route_type=effective_route.value if effective_route else None,
        route_source=body.route_source.value,
    )
```

This step must preserve the existing return shape for website and wechat routes, while adding `resolved_route_type` and `route_source` to the response payload.

- [ ] **Step 5: Extend `backend/app/discovery/multi_graph.py` to honor explicit routes instead of guessing**

Change `start_multi_discovery_run()` so it accepts the explicit route selection from the API layer:

```py
def start_multi_discovery_run(
    raw_input: str,
    *,
    force: bool = False,
    name: str | None = None,
    hints: dict[str, Any] | None = None,
    selected_route_type: str | None = None,
    route_source: str = "inferred",
) -> dict[str, Any]:
```

Inside the function:

1. If `selected_route_type == "website"`, force `route.kind == "website"` semantics.
2. If `selected_route_type == "wechat_search"`, force `route.kind == "wechat"` with `input_type == "wechat_search"`.
3. If `selected_route_type == "wechat_history"`, force `route.kind == "wechat"` with history/account semantics.
4. If `selected_route_type == "internal_forum"`, return the internal branch artifact instead of falling back to generic text routing.

Return fields must include:

```py
"resolved_route_type": selected_route_type or inferred_value,
"route_source": route_source,
```

- [ ] **Step 6: Centralize fixed prefixed names in `backend/app/discovery/naming.py`**

Add explicit helpers so frontend and backend can share one naming policy:

```py
WECHAT_SEARCH_NAME_PREFIX = "微信搜索："
WECHAT_HISTORY_NAME_PREFIX = "公众号："
INTERNAL_FORUM_NAME_PREFIX = "司内论坛："


def format_wechat_search_display_name(value: str) -> str:
    return f"{WECHAT_SEARCH_NAME_PREFIX}{normalize_site_name(value) or value.strip()}"


def format_wechat_history_display_name(value: str) -> str:
    return f"{WECHAT_HISTORY_NAME_PREFIX}{normalize_site_name(value) or value.strip()}"


def format_internal_forum_display_name(value: str) -> str:
    return f"{INTERNAL_FORUM_NAME_PREFIX}{normalize_site_name(value) or value.strip()}"
```

Use these helpers in `multi_graph.py` instead of hard-coded string assembly.

- [ ] **Step 7: Run focused backend verification**

Run:

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_multi_discovery_routes_live.py -k "not live" -v
```

Expected:

```text
no tests ran
```

This repository currently keeps route coverage in live tests only, so the real verification for this task is an API smoke test against a running backend.

Then do an API smoke check manually with the dev server running:

```bash
curl -s http://localhost:8000/discovery/suggest-name \
  -H "Content-Type: application/json" \
  -d '{"input":"微信搜索：openanolis","selected_route_type":"wechat_search","resolved_route_type":"wechat_search","route_source":"explicit"}'
```

Expected: JSON containing:

```json
{"name":"微信搜索：openanolis","resolved_route_type":"wechat_search"}
```

- [ ] **Step 8: Mandatory user verification gate**

Stop here and ask the user to verify one thing manually before any frontend UI changes continue:

1. API-level `suggest-name` returns the fixed prefixed name for a non-website route.
2. API-level `multi-run` response echoes the locked route metadata.

Do not start Task 2 until the user says this backend/contract layer is acceptable.

---

### Task 2: Frontend Route Selector, Strict Input Parsing, and Disabled-State Validation

**Files:**
- Create: `frontend/src/discovery/routeInput.ts`
- Modify: `frontend/src/components/DiscoveryPanel.tsx`

- [ ] **Step 1: Create `frontend/src/discovery/routeInput.ts` as the single parsing/validation unit**

Create this file with pure helpers instead of inlining all parsing logic into `DiscoveryPanel.tsx`:

```ts
import type { DiscoveryRouteSource, DiscoveryRouteType } from "../types";

export const ROUTE_LABEL_TO_TYPE: Record<string, DiscoveryRouteType> = {
  "网页": "website",
  "微信搜索": "wechat_search",
  "微信公众号": "wechat_history",
  "司内论坛": "internal_forum",
};

export interface RouteInputState {
  prefix: string | null;
  value: string;
  selectedRouteType: DiscoveryRouteType | null;
  resolvedRouteType: DiscoveryRouteType | null;
  routeSource: DiscoveryRouteSource;
  validationError: string | null;
}
```

Add a parser with the exact signature:

```ts
export function parseDiscoveryInput(rawInput: string): { prefix: string | null; value: string; formatValid: boolean } {
  const trimmed = rawInput.trim();
  const parts = trimmed.split("：");
  if (parts.length !== 2) return { prefix: null, value: "", formatValid: false };
  return {
    prefix: parts[0]?.trim() || null,
    value: parts[1]?.trim() || "",
    formatValid: true,
  };
}
```

- [ ] **Step 2: Implement one validation function that owns all disabled-state logic**

In the same file, add:

```ts
export function resolveDiscoveryRouteState(
  rawInput: string,
  selectedRouteType: DiscoveryRouteType | null,
): RouteInputState {
  const parsed = parseDiscoveryInput(rawInput);
  if (!rawInput.trim()) {
    return { ...parsed, selectedRouteType, resolvedRouteType: selectedRouteType, routeSource: "explicit", validationError: "请输入探查内容。" };
  }
  if (!parsed.formatValid || !parsed.prefix) {
    return { ...parsed, selectedRouteType, resolvedRouteType: selectedRouteType, routeSource: "explicit", validationError: "请输入严格格式：XX：XXX。" };
  }
  const inferred = ROUTE_LABEL_TO_TYPE[parsed.prefix] ?? null;
  if (!inferred) {
    return { ...parsed, selectedRouteType, resolvedRouteType: null, routeSource: "explicit", validationError: "前缀必须是 网页、微信搜索、微信公众号、司内论坛 之一。" };
  }
  if (!parsed.value) {
    return { ...parsed, selectedRouteType, resolvedRouteType: inferred, routeSource: "explicit", validationError: "冒号后面的内容不能为空。" };
  }
  if (selectedRouteType && inferred !== selectedRouteType) {
    return { ...parsed, selectedRouteType, resolvedRouteType: selectedRouteType, routeSource: "explicit", validationError: "输入前缀必须与路由路径选择一致。" };
  }
  return validateRouteValue(parsed.value, selectedRouteType ?? inferred, parsed.prefix);
}
```

Then add `validateRouteValue()` in the same file to enforce:

- website = `http/https` URL only
- wechat_search = non-empty keyword and not URL
- wechat_history = non-empty account name or `mp.weixin.qq.com` URL
- internal_forum = non-empty keyword

- [ ] **Step 3: Replace the current URL-only top controls in `frontend/src/components/DiscoveryPanel.tsx`**

Refactor the persisted state shape from:

```ts
interface DiscoveryPanelPersistedState {
  url?: string;
  name?: string;
  runId?: number | null;
  selectedNode?: FlowNodeId | null;
  expanded?: boolean;
}
```

to:

```ts
interface DiscoveryPanelPersistedState {
  rawInput?: string;
  selectedRouteType?: DiscoveryRouteType | null;
  resolvedRouteType?: DiscoveryRouteType | null;
  routeSource?: DiscoveryRouteSource;
  parsedPrefix?: string | null;
  parsedValue?: string;
  name?: string;
  runId?: number | null;
  selectedNode?: FlowNodeId | null;
  expanded?: boolean;
}
```

Then replace the current website-only input row with a two-row control section:

```tsx
<div style={controlGrid}>
  <div style={routeRow}>
    <label style={fieldLabel}>路由路径</label>
    <select value={selectedRouteType ?? ""} onChange={...} style={selectBase}>
      <option value="">请选择</option>
      <option value="website">网页</option>
      <option value="wechat_search">微信搜索</option>
      <option value="wechat_history">微信公众号</option>
      <option value="internal_forum">司内论坛</option>
    </select>
    <input
      placeholder="例如：网页：https://example.com"
      value={rawInput}
      onChange={...}
      style={inputBase}
    />
  </div>
  <div style={actionRow}>{/* keep name + buttons here */}</div>
</div>
```

- [ ] **Step 4: Derive button disabled states from the parser instead of ad-hoc string checks**

Replace:

```ts
const startDisabled = !url || startBusy;
```

with:

```ts
const routeState = resolveDiscoveryRouteState(rawInput, selectedRouteType);
const startDisabled = Boolean(routeState.validationError) || startBusy;
const autoNameDisabled = Boolean(routeState.validationError) || nameMut.isPending;
```

Display the validation error inline:

```tsx
{routeState.validationError && (
  <div style={{ color: "#b42318", fontSize: 13, marginTop: 10 }}>
    {routeState.validationError}
  </div>
)}
```

- [ ] **Step 5: Persist parsed and selected route state to `localStorage`**

Update the existing `writeDiscoveryPanelState()` effect to save:

```ts
writeDiscoveryPanelState({
  rawInput,
  selectedRouteType,
  resolvedRouteType: routeState.resolvedRouteType,
  routeSource: routeState.routeSource,
  parsedPrefix: routeState.prefix,
  parsedValue: routeState.value,
  name,
  runId,
  selectedNode,
  expanded,
});
```

On initial load, restore `rawInput` and `selectedRouteType` before any network activity.

- [ ] **Step 6: Run a focused frontend build check**

Run:

```bash
cd frontend && npm run build
```

Expected: build succeeds with no TypeScript errors.

- [ ] **Step 7: Mandatory user verification gate**

Stop here and ask the user to manually verify all four conditions in the browser:

1. `网页：https://example.com` with route `网页` enables the start button.
2. `微信搜索：openanolis` with route `网页` keeps the start button disabled.
3. `微信公众号：https://example.com` shows a type error.
4. Refreshing the page preserves the selected route and the raw strict-format input.

Do not start Task 3 until the user confirms the parsing, validation, and persistence behavior feels right.

---

### Task 3: Route-Aware Auto Naming, Locked Start Submission, and Route/Name Logs

**Files:**
- Modify: `frontend/src/components/DiscoveryPanel.tsx`
- Modify: `frontend/src/components/DiscoveryLogPanel.tsx`
- Modify: `frontend/src/hooks/useDiscoveryLogs.ts`
- Modify: `backend/app/api/discovery_routes.py`

- [ ] **Step 1: Make `✨ 自动` send the locked route state instead of a bare website URL**

In `DiscoveryPanel.tsx`, replace the current mutation:

```ts
const nameMut = useMutation({
  mutationFn: (u: string) => suggestDiscoveryName(u),
  ...
});
```

with:

```ts
const nameMut = useMutation({
  mutationFn: () =>
    suggestDiscoveryName({
      input: rawInput,
      selected_route_type: selectedRouteType,
      resolved_route_type: routeState.resolvedRouteType,
      route_source: routeState.routeSource,
    }),
  onSuccess: (r) => {
    setName(r.name);
    setNameError(null);
  },
  onError: (e) => {
    const message = e instanceof Error && e.message ? e.message : "自动命名失败，请稍后重试";
    setNameError(message);
  },
});
```

The button should now use:

```tsx
disabled={autoNameDisabled}
```

- [ ] **Step 2: Make `开始探查` submit the locked route state**

Replace the current start mutation input:

```ts
startMut.mutate({ url, name: name || undefined, force });
```

with:

```ts
startMut.mutate({
  input: rawInput,
  selected_route_type: selectedRouteType,
  resolved_route_type: routeState.resolvedRouteType,
  route_source: routeState.routeSource,
  name: name || undefined,
  force,
});
```

Update the mutation type accordingly:

```ts
mutationFn: (request: MultiDiscoveryStartRequest) => startDiscoveryRun(request)
```

Also update duplicate handling so it still works when the backend returns `duplicate` for website routes.

- [ ] **Step 3: Add a `命名` stage to the frontend log display**

In `frontend/src/components/DiscoveryLogPanel.tsx`, extend `stageLabels`:

```ts
const stageLabels: Record<string, string> = {
  "任务": "任务",
  "抓首页": "抓首页",
  "抓网络请求": "抓网络请求",
  "路由": "路由",
  "命名": "命名",
  "探查": "探查",
  "验证URL": "验证URL",
  "写配方": "写配方",
  "审计": "审计",
  "存库": "存库",
  "抓方式": "抓方式",
  run: "任务",
  process: "处理",
};
```

Do not add a new top-level filter tab for `命名`; it should stay grouped under discovery logs.

- [ ] **Step 4: Include `命名` in discovery-related log filtering**

In `frontend/src/hooks/useDiscoveryLogs.ts`, extend:

```ts
const DISCOVERY_RELATED_STAGES = new Set([
  "任务",
  "抓首页",
  "抓网络请求",
  "路由",
  "命名",
  "探查",
  "验证URL",
  "写配方",
  "审计",
  "存库",
  "抓方式",
]);
```

- [ ] **Step 5: Emit backend `命名` logs for route-aware auto naming**

In `backend/app/api/discovery_routes.py`, inside the upgraded suggest-name handler, write logs before returning:

```py
append_run_log(
    "命名",
    "自动生成名称",
    input=body.input,
    resolved_route_type=resolved_route_type,
    name=generated_name,
)
```

For `/discovery/multi-run`, after route locking but before dispatch:

```py
append_run_log(
    "探查",
    "已按已保存分支启动",
    input=body.input,
    resolved_route_type=effective_route.value if effective_route else None,
    route_source=body.route_source.value,
    name=body.name,
)
```

- [ ] **Step 6: Run frontend build verification again**

Run:

```bash
cd frontend && npm run build
```

Expected: build succeeds with the updated mutation signatures and restored persisted state logic.

- [ ] **Step 7: Manual end-to-end verification with the user in the browser**

Verify these exact flows manually:

1. Route `网页` + input `网页：https://example.com` + click `✨ 自动` ⇒ name becomes `网站：...`.
2. Route `微信搜索` + input `微信搜索：openanolis` + click `✨ 自动` ⇒ name becomes `微信搜索：openanolis`.
3. Route `微信公众号` + valid account string + click `✨ 自动` ⇒ name becomes `公众号：...`.
4. Click `开始探查` on a legal input and confirm the log panel shows both `路由` and `命名` entries before or at run startup.

- [ ] **Step 8: Mandatory user verification gate**

Stop and wait for the user to say the module is acceptable after they try it themselves.

Only after the user approves this module should implementation be considered complete.

---

## Self-Review Checklist

### Spec coverage

- Spec §§5–6 (input structure and strict validation): covered by Task 2.
- Spec §7 (route-aware naming): covered by Task 3, plus naming helpers in Task 1.
- Spec §§8–9 (frontend + backend locked route state): covered by Tasks 1–3.
- Spec §10 (route/name-first logs): covered by Task 3.
- Spec §11 (incremental API upgrade): covered by Task 1 and Task 3.
- Spec §§14–15 (manual verification gate, no frontend tests required): reflected at the end of every task.

### Placeholder scan

This plan intentionally avoids `TODO`, `TBD`, “similar to above”, and “write tests later”. Each task lists exact files, exact field names, and concrete verification steps.

### Type consistency

Use the same route union across tasks:

- `website`
- `wechat_search`
- `wechat_history`
- `internal_forum`

Use the same route source union across tasks:

- `explicit`
- `inferred`

Do not introduce alternate names like `web`, `wechat`, `forum`, `selected_kind`, or `source_kind` in the frontend contract layer.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-07-multi-type-discovery-input.md`. Two execution options:

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
