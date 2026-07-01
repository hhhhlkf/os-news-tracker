# 站点发现模块 · 前端设计

**日期：** 2026-07-01
**状态：** 待用户评审
**项目：** `os-news-tracker` — 为已完成的站点发现后端（`app/discovery/` + `/discovery/*` 端点）设计并实现前端页面

---

## 1. 背景与目标

后端站点发现 Agent（`app/discovery/graph.py` 的 SiteDiscoveryGraph LangGraph）与 `/discovery/*` 端点已基本完成，提供两条命：

- **生成命**：输入站点 URL，LangGraph 多 Agent 协作探查"这个网站怎么爬"，产出一份可复用的 DSL Recipe 存入 `crawl_methods`。
- **运行命**：对已存方式调用 `POST /discovery/methods/{id}/fetch`，按 DSL 纯确定性抓取，经现有 pipeline（Enricher 富化+打分）入库，条目进入新闻流。

本 spec 设计承载这两条命的前端页面 **DiscoveryPage**，分两个模块：

1. **智能探查模块**：仅输入网站 + 名称（名称可 LLM 自动生成），实时展示探查进度（按 LangGraph 流程图动画流转），完成后沉淀到爬取方式库。
2. **抓取模块**：从爬取方式库中选取若干个已规定好爬取步骤的方式，一键批量抓取，结果进入新闻流。按钮从简。

**设计要求**：完善信息/流程提示；探查进度按 agent 流程图实时动画；按钮少、界面美观易用、实时展示到位。

**当前前端现状**：V1 单 `HomePage`（无路由、无 discovery UI、`react-router-dom` 未安装、`index.css` 仅有基础样式）。本 spec 为纯增量：新增 `/discover` 路由与页面，不动现有 HomePage 新闻流。

---

## 2. 信息架构与路由

- 新增依赖 `react-router-dom`（当前未安装）。`App.tsx` 改为 `BrowserRouter`：
  - `/` → 现有 `HomePage`（新闻流，不动）
  - `/discover` → 新 `DiscoveryPage`
- 两个页头互通链接：HomePage 头部加"站点发现 →"跳 `/discover`；DiscoveryPage 头部"← 返回新闻流"跳 `/`。
- 不引入鉴权守卫（当前前端未接 auth；`auth.ts` 存在但 `App.tsx` 未用）。
- 不引入 CSS 设计令牌重构；沿用现有内联样式 + 现有调色板（见 §8）。

---

## 3. 智能探查模块

页面上方区域。两态自适应：**空闲态**仅留输入栏，方式库为主体；**运行/完成/失败态**展开流程图区。

### 3.1 输入栏

一行：URL 输入（必填）+ 名称输入（选填，带 `✨ 自动` 按钮）+ 主按钮。

- `✨ 自动`：调新增 `POST /discovery/suggest-name {url}` → `{name}`（后端抓首页 `<title>`，可选一次极短 LLM 润色），回填名称输入框，用户可改。
- 主按钮：空闲态"开始探查"；运行态禁用为"探查中…"。
- 提交：`POST /discovery/run {url, name?, force:false}`。
  - 返回 `{status:"started", run_id}` → 进入运行态，开始轮询。
  - 返回 `{status:"duplicate", existing_method}` → 内联确认"该域名已有爬取方式（{domain}），是否覆盖重新探查？"，确认则 `force:true` 重发。

### 3.2 环形流程图（reactflow）

用 `@xyflow/react` 渲染 SiteDiscoveryGraph 的概念流程图（**UI 概念图，非后端图 1:1 映射**；后端 supervisor 路由细节不在 UI 展示）：

```
[抓首页] → [抓网络请求]   （确定性·胶囊形·不烧 token）
              ↓
        ╭─── 环形 ───╮
       探查 → 验证URL
        ↑          ↓
       审计 ← 写配方        （AI agent·圆形·烧 token·最多 3 轮）
        ╰─ 不通过·重试 ─╯
        ↓ 通过
       [存库]               （确定性·胶囊形）
```

- **节点形状**：4 个 AI agent = 圆形；3 个确定性步骤（抓首页/抓网络请求/存库）= 胶囊形。视觉区分"AI 推理 vs 确定性程序"。
- **节点标签**：友好中文名（探查/验证URL/写配方/审计/抓首页/抓网络请求/存库）领头，原始节点名（explorer/validator/dsl_writer/auditor/fetch_homepage/capture_network/save_method）作小角标（鼠标悬停 tooltip）。
- **边**：由 reactflow 自动绘制，箭头（`markerEnd: MarkerType.ArrowClosed`）自动吸附节点边缘，无缝隙。
  - 正向边：抓首页→抓网络请求→探查→验证URL→写配方→审计（bezier）。
  - 重试回边：审计 → 接力起点（环回边，橙色虚线 `animated:true`；概念上表示"审计不通过则重跑接力"，抽象后端 supervisor 的回退路由——实际回退到 写配方/验证 由 supervisor 决定，UI 不区分）。
  - 出环边：审计→存库（绿色，"通过"）。
- **节点定位**：固定坐标（节点少且固定，不需 dagre 自动布局）。环形居中，预处理胶囊居中竖排从顶部入环，存库居中在底部出环。胶囊等宽、间距舒展。
- **guide ring**：极淡虚线引导环，让"环形"作为形状本身可见。

### 3.3 节点状态与动画（轮询 `GET /discovery/runs/{id}` 驱动）

每 ~1.5s 轮询 run 详情，按 `node_trace` + `current_step` 驱动节点状态：

| 状态 | 样式 |
|------|------|
| 待执行 | 灰、虚线描边 |
| 进行中（=current_step） | 蓝、脉冲 halo、`animated` 边流动 |
| 完成 | 绿、✓ |
| 失败 | 红、✗ |

- **运行中**：`node_trace` 逐步增长，节点依次 待执行→进行中→完成；当前步高亮。
- **重试**：审计不通过、`retry_count+1` 时，橙色回边 `animated` 流动（marching ants），"第 N/3 轮"刷新，相关节点重置为进行中。
- **完成（verdict=dsl）**：全节点绿 + 存库 ✓；成功条"探查完成 · 已存入爬取方式库"+ "查看新方式 →"（滚动并高亮方式库中新增行）；输入栏恢复，显示"再探查一个"。
- **失败（verdict=failed / token 超预算 / 重试用尽 / 异常）**：失败节点红 ✗；错误条显示 `error_message` + "重新探查"按钮（重新提交）。

### 3.4 节点详情（点节点展开）

点击任意节点 → 在流程图下方显示该步产出卡（来自 `node_trace` 该步 `summary`）：

- 探查：`source_type` / `list_url` / `items` / `url 字段` / `pagination`
- 验证URL：`mode` / `template` / `evidence`
- 写配方：DSL `actions` 数量 / 是否含 loop
- 审计：`passed` / `issues` / `suggested_fix`
- 抓首页 / 抓网络请求：status / 关键摘要

> 需后端在每个 node_trace 条目带 `summary`（见 §6）。

### 3.5 实时日志面板

流程图右侧深色面板（`#0b1220`、JetBrains Mono），接 SSE `EventSource('/api/logs/stream')`，过滤 `discovery`/`explorer`/`validator`/`dsl_writer`/`auditor` 阶段日志，按时间倒序滚动。复用项目既有 SseLogHandler + `/logs/stream`。

### 3.6 状态汇总

| 态 | 上方区域 |
|----|---------|
| 空闲 | 仅输入栏；流程图收起 |
| 运行 | 输入栏锁定；流程图展开（reactflow 挂载）；轮询 + SSE |
| 完成 | 流程图全绿快照 + 成功条 + "查看新方式"；输入栏恢复 |
| 失败 | 失败节点红 + 错误条 + "重新探查" |
| 去重 | 内联覆盖确认（不进运行态） |

---

## 4. 抓取模块 · 爬取方式库

页面下方区域（DiscoveryPage 第二段）。

### 4.1 列表

`GET /discovery/methods` → 行：

| 列 | 内容 |
|----|------|
| 勾选框 | 多选（disabled 方式禁用） |
| 名称 | `domain`（或别名） |
| entry_url | 灰色小字 |
| 状态徽标 | active（绿）/ disabled（灰）/ failed（红） |
| 上次结果 | "抓取 N · 入库 M · 时间" 或 "失败：原因" |

- **点行** → 右侧滑出抽屉（复用 HomePage 既有点行抽屉模式）：完整 DSL recipe（只读、格式化展示 `actions`）+ 启用/禁用开关（`PATCH`）+ 删除（`DELETE`，二次确认）。
- **列表行除勾选框外无其他按钮**（按钮从简；管理操作收进抽屉）。

### 4.2 批量抓取

- 列表头：`已选 N 个` + 主按钮`抓取选中`（pill、蓝）+ 文字链接`清空`。
- 点`抓取选中`：对每个选中方式并行 `POST /discovery/methods/{id}/fetch`（`Promise.all`）。
  - 行内态：转圈"抓取中…" → "抓取 N · 入库 M" 或错误原因。
  - 顶部汇总条："本次抓取 X 条 · 入库 Y 条"。
- 抓取完成后条目进入新闻流；底部链接"查看入库条目 →"跳 `/`。
- disabled 方式不可勾选（勾选框禁用、行变灰）。

---

## 5. 数据与 API 接线

### 5.1 TanStack Query

| queryKey | 来源 | 轮询 |
|----------|------|------|
| `["discovery-run", run_id]` | `GET /discovery/runs/{id}` | 运行中每 ~1.5s；完成/失败停 |
| `["discovery-methods"]` | `GET /discovery/methods` | run 完成后、批量抓取后 invalidate |
| `["discovery-method", id]` | `GET /discovery/methods/{id}` | 抽屉打开时 |

### 5.2 端点清单（现有 + 新增）

**现有（`app/api/discovery_routes.py`）**

| 端点 | 用途 |
|------|------|
| `POST /discovery/run` | 启动生成命 `{url, name?, force?}` |
| `GET /discovery/runs` | 历史列表 |
| `GET /discovery/runs/{id}` | 单 run 详情（含 `node_trace`）— 前端轮询 |
| `GET /discovery/methods` | 方式列表 |
| `GET /discovery/methods/{id}` | 方式详情（含 `dsl_recipe`） |
| `PATCH /discovery/methods/{id}` | 启用/禁用 `{status}` |
| `DELETE /discovery/methods/{id}` | 删除 |
| `POST /discovery/methods/{id}/fetch` | 运行命：抓取+入库 |

**新增（后端增强，见 §6）**

| 端点 | 用途 |
|------|------|
| `POST /discovery/suggest-name` | `{url}` → `{name}`（LLM 自动命名） |

### 5.3 API client / 类型

- `frontend/src/api/client.ts` 新增：`startDiscoveryRun`、`getDiscoveryRun`、`listDiscoveryMethods`、`getDiscoveryMethod`、`patchDiscoveryMethod`、`deleteDiscoveryMethod`、`fetchDiscoveryMethod`、`suggestDiscoveryName`。均带 `authHeaders()`，复用 `ApiError` 模式。
- `frontend/src/types.ts` 新增：`DiscoveryRun`、`DiscoveryNodeTraceEntry`、`CrawlMethod`、`CrawlMethodDetail`、`DiscoveryFetchResult`、`SuggestNameResponse`。

### 5.4 SSE 日志

当前前端未接 SSE（HomePage 用轮询 `/news-run/logs`）。本页新增 `useDiscoveryLogStream` hook，用 `EventSource('/api/logs/stream')` 按 `stage` 过滤 discovery 相关日志，渲染至日志面板，带 `Last-Event-ID` 自动重连。后端 `/logs/stream` 端点复用既有 `SseLogHandler`（CLAUDE.md V2 已规划）；实现时若该端点未就绪，作为后端任务补齐（见 §6）。

---

## 6. 后端改动（本 spec 范围）

> 探查实时进度/命名的后端依赖。截至本 spec，部分已实现（commit `59bb99d` / `ca61c0d`），标注状态。

**已实现 ✓（前端可直接对接）**

1. **流式 node_trace**：`_execute_discovery` 已用 `g.stream(stream_mode="updates")` 逐节点写 `node_trace`（每步 `{step, status, ts}` 实时落库）。
2. **`GET /discovery/runs/{id}` 增 `current_step`**：已返回 `current_step`（= node_trace 末条 step）。
3. **graph logger + run_logs**：每步 `logger.info` + `append_run_log`（与 agent_crawl 同套格式）→ 供 SSE `/logs/stream` 推送 / 日志端点轮询（若 `/logs/stream` 未就绪，补齐）。

**待实现 ✗（纳入本 spec 实现）**

4. **`POST /discovery/suggest-name`**：新端点，`{url}` → `{name}`（抓首页 `<title>`，可选一次极短 LLM 润色）。
5. **node_trace 条目增 `summary`**：当前条目仅 `{step, status, ts}`；需在各 worker 节点产出时附带简短 `summary`（探查→source_type/list_url；验证→url_rule mode/evidence；写配方→actions 数/has_loop；审计→passed/issues），写入对应 node_trace 条目，供 §3.4 节点详情卡展示。

---

## 7. 前端结构与组件

```
frontend/src/
├── App.tsx                      # 改：BrowserRouter + Routes(/, /discover)
├── pages/
│   └── DiscoveryPage.tsx        # 新：/discover 页面，组合两模块
├── components/
│   ├── DiscoveryPanel.tsx       # 新：智能探查区（输入栏 + 状态编排）
│   ├── DiscoveryFlowChart.tsx   # 新：@xyflow/react 环形流程图 + 节点状态/动画
│   ├── DiscoveryNodeDetail.tsx  # 新：点节点展开的产出卡
│   ├── DiscoveryLogPanel.tsx    # 新：SSE 日志面板（深色 mono）
│   ├── CrawlMethodList.tsx      # 新：抓取方式库列表 + 多选 + 批量抓取
│   └── CrawlMethodDetail.tsx    # 新：方式详情抽屉（DSL/启停/删除）
├── hooks/
│   └── useDiscoveryLogStream.ts # 新：EventSource 过滤 discovery 阶段
├── api/client.ts                # 改：加 discovery 系列函数
└── types.ts                     # 改：加 discovery 类型
```

**自定义 reactflow 节点**：
- `AgentNode`（圆形）：接受 `state`（pending/running/done/failed），running 时脉冲 halo。
- `DeterministicNode`（胶囊形）：接受 `state`。

**节点状态映射**：`DiscoveryFlowChart` 接收 `node_trace` + `current_step` + `status` + `retry_count`，计算每个节点状态，传给自定义节点；边按状态着色/动画。

---

## 8. 视觉方向与设计令牌

沿用现有设计语言（与 HomePage/AgentRunControl 等一致），不重构令牌：

| 令牌 | 值 |
|------|-----|
| 背景 | `#f5f7fb` |
| 墨色 | `#101828` |
| 主色 | `#175cd3` |
| 边框 | `#d0d5dd` |
| 成功 | `#059669` / `#ecfdf3` |
| 警告/重试 | `#d97706` |
| 失败 | `#dc2626` / `#fef2f2` |
| 日志面板 | `#0b1220` + JetBrains Mono |

- 圆角 8–10px，主按钮 pill（`border-radius:999`），内联样式。
- **签名元素**：动态环形流程图（reactflow + 脉冲 + 流动边）是页面记忆点；其余克制。

---

## 9. 新增依赖

- `react-router-dom`（路由）
- `@xyflow/react`（流程图）

---

## 10. 范围外

- 不动现有 HomePage 新闻流与既有 agent_crawl（Handoff Chain）UI。
- 不接鉴权守卫（auth 未启用）。
- 不重构 `index.css` 设计令牌。
- 不做 DSL recipe 可视化编辑（详情只读展示）。
- 不做 discovery 历史列表页（`GET /discovery/runs` 端点保留，前端暂不展示历史；后续可加）。

---

## 11. 验证标准

- `/discover` 页面可访问，与 `/` 互通跳转。
- 输入 URL + 自动命名 + 开始探查，流程图按 node_trace 实时动画（节点逐个进行中→完成，当前步高亮，重试时回边流动 + 轮次刷新）。
- 完成：全绿 + 成功条 + 新方式高亮出现在方式库。
- 失败：失败节点红 + 错误条 + 重新探查可用。
- 去重：重复域名弹覆盖确认，`force=true` 覆盖成功。
- 方式库：多选 + 抓取选中，行内抓取中/结果/错误态正确，汇总条正确，结果进入新闻流。
- 方式详情抽屉：DSL 只读展示、启停、删除生效。
- SSE 日志面板实时显示 discovery 阶段日志。
- 后端：`suggest-name` 可用、`node_trace` 条目带 `summary`（`g.stream` 逐节点写、`current_step`、graph logger 已就绪）。
- 既有 HomePage 与 agent_crawl 功能回归不受影响。

---

## 12. 可视化参考

设计过程中的静态原型（SVG 草图，仅供回顾，非最终实现）位于 `.superpowers/brainstorm/`（已 gitignore）：
- `layout-v8.html`（运行态布局）、`states.html`（完成/重试/失败三态）、`fullpage.html`（运行态完整页面）。

> 最终流程图由 `@xyflow/react` 渲染，箭头吸附节点边缘，非手绘 SVG。
