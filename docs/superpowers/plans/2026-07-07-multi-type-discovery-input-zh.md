# 多类型智能探查输入改造 — 实施计划

> **面向 agent 工作器：** 必需的子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐步实施本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 将 `DiscoveryPanel` 从“仅支持网站 URL 的探查表单”升级为“严格多类型探查控制台”，支持显式路由选择、`XX：XXX` 格式解析、基于路由的自动命名、路由状态持久化，以及“路由/命名优先”的日志呈现。

**架构：** 保持现有 `DiscoveryPage` 与 `DiscoveryPanel` 的整体布局不变，但把新增行为拆成三个边界清晰的层：共享请求/响应契约、前端路由输入解析与持久化、以及最终的启动/命名/日志联动。后端只做增量改造：扩展现有 discovery 端点，使前端可以提交“已锁定的最终路由状态”，而不是继续依赖隐式猜测。

**技术栈：** React 19、TypeScript、TanStack Query、内联样式、FastAPI、Pydantic、现有 `/discovery/`* API、现有 discovery 运行日志链路

**规范：** `docs/superpowers/specs/2026-07-07-multi-type-discovery-input-design.md`

**提交策略：** 执行过程中不要创建 commit，除非用户明确要求提交。

---

## 范围检查

这份 spec 只覆盖一个子系统，不是多个彼此独立的项目。实施时可以聚焦为一个计划，并拆成三个用户可感知的检查点：

1. 共享路由契约与后端就绪
2. 前端输入结构、严格解析与按钮禁用校验
3. 基于路由的自动命名、启动提交流程与路由/命名日志

下面每个任务末尾都带有一个**强制的用户验证 gate**。在用户亲自试用并确认当前模块通过之前，不要开始下一个任务。

---

## 文件变更地图


| 文件                                              | 操作  | 职责                                     |
| ----------------------------------------------- | --- | -------------------------------------- |
| `backend/app/api/discovery_routes.py`           | 修改  | 接收多类型探查启动/命名请求，并产出路由/命名日志              |
| `backend/app/discovery/multi_graph.py`          | 修改  | 尊重显式路由选择，并执行网站 / 微信 / 司内论坛的固定命名规则      |
| `backend/app/discovery/naming.py`               | 修改  | 集中管理带前缀的展示名 helper，供路由感知命名复用           |
| `frontend/src/types.ts`                         | 修改  | 添加路由类型 union 和多类型 discovery 请求/响应类型    |
| `frontend/src/api/client.ts`                    | 修改  | 升级 discovery 启动/命名函数，提交已锁定的路由状态        |
| `frontend/src/discovery/routeInput.ts`          | 创建  | 解析 `XX：XXX`、按路由类型校验、计算最终路由状态           |
| `frontend/src/components/DiscoveryPanel.tsx`    | 修改  | 添加路由选择框、严格输入框、持久化、按钮禁用逻辑、路由感知命名、路由感知启动 |
| `frontend/src/components/DiscoveryLogPanel.tsx` | 修改  | 识别并清晰标记新的 `命名` 阶段                      |
| `frontend/src/hooks/useDiscoveryLogs.ts`        | 修改  | 在 discovery 相关日志过滤中包含 `命名`             |


---

## 共享契约

除非执行过程中发现与现有代码强冲突，否则前端统一使用以下命名：

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

除非发现更强的现有契约，否则后端请求体统一使用以下字段：

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

### 任务 1：共享路由契约与后端就绪

**文件：**

- 修改：`frontend/src/types.ts`
- 修改：`frontend/src/api/client.ts`
- 修改：`backend/app/api/discovery_routes.py`
- 修改：`backend/app/discovery/multi_graph.py`
- 修改：`backend/app/discovery/naming.py`

- [ ] **步骤 1：在 `frontend/src/types.ts` 中添加路由类型与请求/响应结构**

把以下内容追加到现有 discovery 类型附近：

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

- [ ] **步骤 2：升级 `frontend/src/api/client.ts`，使用路由感知的 discovery 请求**

把目前“只支持网站”的 helper 签名替换为以下路由感知版本：

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

旧的按位置传参签名：

```ts
startDiscoveryRun(url: string, name?: string, force = false)
suggestDiscoveryName(url: string)
```

必须删除，避免后续任务误继续沿用“仅网站”的语义。

- [ ] **步骤 3：在 `backend/app/api/discovery_routes.py` 中添加后端请求模型与路由类型映射**

在现有 multi-discovery 请求定义附近，新增一个更严格的请求体，显式携带“已锁定的路由状态”：

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

然后添加一个 helper，把前端路由类型翻译成 `source_router_for_input()` 已在使用的后端 hint 契约：

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

- [ ] **步骤 4：让 `/discovery/multi-run` 尊重显式路由选择，并输出路由日志**

把当前直接转发 body 的逻辑替换成路由感知版本：

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

这一步必须保留当前 website / wechat 路由已有的返回结构，同时把 `resolved_route_type` 和 `route_source` 补进响应。

- [ ] **步骤 5：扩展 `backend/app/discovery/multi_graph.py`，显式路由优先，不再只靠猜测**

把 `start_multi_discovery_run()` 改成接受来自 API 层的显式路由选择：

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

函数内部需要做到：

1. 若 `selected_route_type == "website"`，强制走 `route.kind == "website"` 语义
2. 若 `selected_route_type == "wechat_search"`，强制走 `route.kind == "wechat"` 且 `input_type == "wechat_search"`
3. 若 `selected_route_type == "wechat_history"`，强制走 `route.kind == "wechat"` 的公众号历史/账号语义
4. 若 `selected_route_type == "internal_forum"`，返回司内论坛分支 artifact，而不是退回到泛文本默认路由

返回值里必须包含：

```py
"resolved_route_type": selected_route_type or inferred_value,
"route_source": route_source,
```

- [ ] **步骤 6：在 `backend/app/discovery/naming.py` 中集中管理固定前缀名称 helper**

添加以下显式 helper，让前后端共享一套命名策略：

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

随后在 `multi_graph.py` 中改为调用这些 helper，不再手写字符串拼接。

- [ ] **步骤 7：执行聚焦的后端验证**

运行：

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_multi_discovery_routes_live.py -k "not live" -v
```

预期：

```text
no tests ran
```

这个仓库目前把多路由覆盖主要留在 live 测试里，所以本任务真正的验证方式是：在后端服务运行时做 API smoke check。

然后执行一条手动 smoke check：

```bash
curl -s http://localhost:8000/discovery/suggest-name \
  -H "Content-Type: application/json" \
  -d '{"input":"微信搜索：openanolis","selected_route_type":"wechat_search","resolved_route_type":"wechat_search","route_source":"explicit"}'
```

预期返回 JSON，至少包含：

```json
{"name":"微信搜索：openanolis","resolved_route_type":"wechat_search"}
```

- [ ] **步骤 8：强制用户验证 gate**

在继续做任何前端 UI 改动之前，先停下来让用户手动确认两件事：

1. API 级别的 `suggest-name` 对非网站路由能返回固定前缀名称
2. API 级别的 `multi-run` 响应会回显已锁定的路由元数据

在用户明确表示这一层的后端/契约可以接受之前，不要开始任务 2。

---

### 任务 2：前端路由选择框、严格输入解析与按钮禁用校验

**文件：**

- 创建：`frontend/src/discovery/routeInput.ts`
- 修改：`frontend/src/components/DiscoveryPanel.tsx`

- [ ] **步骤 1：创建 `frontend/src/discovery/routeInput.ts`，作为唯一的解析/校验单元**

创建这个文件，用纯函数承载解析与校验逻辑，不要把所有判断都直接塞回 `DiscoveryPanel.tsx`：

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

然后添加一个解析器，签名固定如下：

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

- [ ] **步骤 2：实现一个单一校验函数，统一拥有所有按钮禁用逻辑**

在同一个文件里新增：

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

然后在同文件中补 `validateRouteValue()`，用来强制执行：

- website = 只能是 `http/https` URL
- wechat_search = 非空关键词，且不能是 URL
- wechat_history = 非空公众号名，或 `mp.weixin.qq.com` URL
- internal_forum = 非空检索词

- [ ] **步骤 3：替换 `frontend/src/components/DiscoveryPanel.tsx` 顶部现有的“仅 URL”控件**

把持久化状态结构从：

```ts
interface DiscoveryPanelPersistedState {
  url?: string;
  name?: string;
  runId?: number | null;
  selectedNode?: FlowNodeId | null;
  expanded?: boolean;
}
```

改成：

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

然后把当前顶部的“网站 URL 输入行”替换成两行控制区：

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
  <div style={actionRow}>{/* 这里保留名称输入 + 按钮 */}</div>
</div>
```

- [ ] **步骤 4：让按钮禁用状态完全来自解析器，而不是临时字符串判断**

把：

```ts
const startDisabled = !url || startBusy;
```

替换为：

```ts
const routeState = resolveDiscoveryRouteState(rawInput, selectedRouteType);
const startDisabled = Boolean(routeState.validationError) || startBusy;
const autoNameDisabled = Boolean(routeState.validationError) || nameMut.isPending;
```

同时在界面上内联展示校验错误：

```tsx
{routeState.validationError && (
  <div style={{ color: "#b42318", fontSize: 13, marginTop: 10 }}>
    {routeState.validationError}
  </div>
)}
```

- [ ] **步骤 5：把解析结果与路由选择一起保存进 `localStorage`**

更新现有 `writeDiscoveryPanelState()` 的 effect，写入：

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

初次加载时，在任何网络请求之前恢复 `rawInput` 和 `selectedRouteType`。

- [ ] **步骤 6：执行一次聚焦的前端 build 检查**

运行：

```bash
cd frontend && npm run build
```

预期：构建成功，没有 TypeScript 错误。

- [ ] **步骤 7：强制用户验证 gate**

停下来，请用户在浏览器里亲自验证以下四件事：

1. 选择 `网页`，输入 `网页：https://example.com` 时，开始按钮可用
2. 选择 `网页`，输入 `微信搜索：openanolis` 时，开始按钮保持禁用
3. 选择 `微信公众号`，输入 `微信公众号：https://example.com` 时，会出现类型错误
4. 刷新页面后，已选路由类型和原始严格格式输入都能恢复

在用户明确确认“解析、校验、持久化行为符合预期”之前，不要开始任务 3。

---

### 任务 3：基于路由的自动命名、锁定路由启动提交、以及路由/命名日志

**文件：**

- 修改：`frontend/src/components/DiscoveryPanel.tsx`
- 修改：`frontend/src/components/DiscoveryLogPanel.tsx`
- 修改：`frontend/src/hooks/useDiscoveryLogs.ts`
- 修改：`backend/app/api/discovery_routes.py`

- [ ] **步骤 1：让 `✨ 自动` 发送“已锁定路由状态”，而不是只传网站 URL**

在 `DiscoveryPanel.tsx` 中，把当前 mutation：

```ts
const nameMut = useMutation({
  mutationFn: (u: string) => suggestDiscoveryName(u),
  ...
});
```

替换为：

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

按钮禁用态改为：

```tsx
disabled={autoNameDisabled}
```

- [ ] **步骤 2：让 `开始探查` 提交“已锁定的最终路由状态”**

把当前启动 mutation 的入参：

```ts
startMut.mutate({ url, name: name || undefined, force });
```

替换为：

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

并同步更新 mutation 类型：

```ts
mutationFn: (request: MultiDiscoveryStartRequest) => startDiscoveryRun(request)
```

同时要保留网站路由下已有的 duplicate handling，使后端返回 `duplicate` 时仍可覆盖重探。

- [ ] **步骤 3：在前端日志展示中加入 `命名` 阶段**

在 `frontend/src/components/DiscoveryLogPanel.tsx` 中扩展 `stageLabels`：

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

不要新增一个单独的 `命名` 顶层过滤 tab；它应继续归在 discovery 日志组里。

- [ ] **步骤 4：把 `命名` 加入 discovery 相关日志过滤**

在 `frontend/src/hooks/useDiscoveryLogs.ts` 中扩展：

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

- [ ] **步骤 5：后端为路由感知命名产出 `命名` 日志**

在 `backend/app/api/discovery_routes.py` 中，升级后的 suggest-name handler 里，在返回前写入日志：

```py
append_run_log(
    "命名",
    "自动生成名称",
    input=body.input,
    resolved_route_type=resolved_route_type,
    name=generated_name,
)
```

对于 `/discovery/multi-run`，在锁定路由之后、真正 dispatch 之前写入：

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

- [ ] **步骤 6：再次执行前端 build 验证**

运行：

```bash
cd frontend && npm run build
```

预期：在 mutation 签名调整、持久化逻辑保留后，构建仍然成功。

- [ ] **步骤 7：和用户一起做手动端到端验证**

手动验证以下精确流程：

1. 路由 `网页` + 输入 `网页：https://example.com` + 点击 `✨ 自动` ⇒ 名称变为 `网站：...`
2. 路由 `微信搜索` + 输入 `微信搜索：openanolis` + 点击 `✨ 自动` ⇒ 名称变为 `微信搜索：openanolis`
3. 路由 `微信公众号` + 合法账号字符串 + 点击 `✨ 自动` ⇒ 名称变为 `公众号：...`
4. 对一个合法输入点击 `开始探查`，确认日志面板会在运行开始前或开始时看到 `路由` 和 `命名` 两类日志

- [ ] **步骤 8：强制用户验证 gate**

停下来，等待用户亲自试用并明确表示当前模块可以接受。

只有在用户批准之后，才算整个实现完成。

---

## 自检清单

### Spec 覆盖

- Spec §§5–6（输入结构与严格校验）：由任务 2 覆盖
- Spec §7（基于路由的命名）：由任务 3 覆盖，任务 1 同时补充命名 helper
- Spec §§8–9（前后端共同锁定路由状态）：由任务 1–3 覆盖
- Spec §10（路由/命名优先日志）：由任务 3 覆盖
- Spec §11（增量 API 升级）：由任务 1 和任务 3 覆盖
- Spec §§14–15（人工验证 gate、前端不强制写测试）：在每个任务末尾体现

### 占位词扫描

本计划刻意避免使用 `TODO`、`TBD`、“类似上面”、“以后再写测试”这类占位表达。每个任务都列出了明确文件、明确字段名和具体验证步骤。

### 类型一致性

所有任务统一使用同一套路由 union：

- `website`
- `wechat_search`
- `wechat_history`
- `internal_forum`

所有任务统一使用同一套路由来源 union：

- `explicit`
- `inferred`

不要在前端契约层再引入 `web`、`wechat`、`forum`、`selected_kind`、`source_kind` 之类的替代名称。

---

## 执行交接

计划已完成并保存到 `docs/superpowers/plans/2026-07-07-multi-type-discovery-input.md`。执行有两种方式：

1. **Subagent-Driven（推荐）**：我为每个任务派一个新的实现 subagent，中间做 review，并在每个模块后停下来等你验证
2. **Inline Execution**：我在当前会话里按计划逐任务实施，并在每个模块后停下来等你验证

你想选哪种方式？