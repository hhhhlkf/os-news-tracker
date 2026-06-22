# Agent Crawl 首页集成与多阶段控制 — 实施计划

> **面向实施 Agent 的说明：** 按任务顺序推进；每完成一个任务就更新 checkbox 状态。先补测试，再补实现；每个阶段结束都要做一次最小验证。

**目标：** 在现有首页“新闻处理控制”区域内增加抓取模式切换，让用户可以在“标准抓取”和“Agent Crawl”之间选择；为 `agent_crawl` 提供单源触发、多阶段状态展示、同源并发保护，以及与当前首页查询轮询机制兼容的前端交互。

**架构：** 复用当前首页承载抓取控制，不新增独立 Agent 页面作为第一入口。后端以 `/sources/agent` 作为配置与运行入口，新增“立即触发”端点，并把 `AgentCrawlRun` 从粗粒度 `running/completed/failed` 扩展为“终态 + 当前阶段”双层状态模型。前端在 `HomePage` 中新增模式切换，在 Agent 模式下展示单源列表、阶段进度、最近运行结果，并对运行中的源做禁用与轮询刷新。

**技术栈：** FastAPI, SQLAlchemy 2.0, React 19, TypeScript, TanStack Query, pytest, vitest

**设计锚点：**
- 入口统一，但阶段模型允许和现有“新闻处理控制”不同
- Agent Crawl 是“单源触发”，不是全局抓取
- 阶段内允许并行，阶段间仍按顺序推进，所以可以稳定展示多阶段进度
- 必须防止同一 source 被重复触发，避免并发运行互相污染状态

---

## 方案摘要

### 首页交互形态

- 在现有 `NewsRunControl` 顶部增加模式切换：
  - `标准抓取`
  - `Agent Crawl`
- 选择 `标准抓取` 时，保留现有手动新闻处理控制与日志面板
- 选择 `Agent Crawl` 时，切换为：
  - Agent 源列表
  - 每个源的“立即抓取”按钮
  - 每个源最近一次运行的多阶段状态
  - 最近若干次运行历史摘要

### Agent 阶段模型

Agent Crawl 不复用 `collecting / processing / stopping`，而是使用自己的阶段：

1. `planning`
2. `crawling`
3. `quality`
4. `summarizing`
5. `completed`
6. `failed`

其中：
- `completed / failed` 是终态
- `planning / crawling / quality / summarizing` 是运行中阶段

### 并发策略

- 允许不同 source 并行运行
- 禁止同一 source 同时运行多个 agent crawl
- 后端触发接口若发现该 source 已处于运行态，返回 `409 Conflict`
- 前端据此禁用对应按钮，并展示“运行中”

---

## 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/app/api/agent_routes.py` | 修改 | 新增 `POST /sources/agent/{id}/run`，补充运行态校验 |
| `backend/app/fetchers/agent_crawl.py` | 修改 | 细化阶段状态写入，统一完成/失败收口 |
| `backend/app/models.py` | 修改 | 扩展 `AgentCrawlRun` 阶段字段 |
| `backend/alembic/versions/*.py` | 新建 | 为 `agent_crawl_runs` 增加阶段相关列 |
| `backend/app/scheduler.py` | 视情况修改 | 复用 `run_source_job()` 或抽取单源运行入口 |
| `backend/tests/integration/test_agent_api.py` | 修改/新增 | 覆盖 run 端点、409、防重入 |
| `backend/tests/unit/test_agent_crawl_fetcher.py` | 修改/新增 | 覆盖阶段推进与失败收口 |
| `frontend/src/api/client.ts` | 修改 | 新增 agent source / runs / trigger API |
| `frontend/src/types.ts` | 修改 | 新增 Agent source / run / stage 类型 |
| `frontend/src/components/NewsRunControl.tsx` | 修改 | 增加模式切换壳层，保留标准抓取 UI |
| `frontend/src/components/AgentRunControl.tsx` | 新建 | Agent 模式主控件 |
| `frontend/src/components/AgentSourceRunCard.tsx` | 新建 | 单源卡片：配置摘要、阶段状态、立即抓取 |
| `frontend/src/pages/HomePage.tsx` | 修改 | 接入模式切换、查询轮询、Agent 区块 |
| `frontend/src/pages/newsRunControl.test.ts` | 修改 | 覆盖模式切换行为 |
| `frontend/src/components/*.test.tsx` | 新建 | 覆盖 Agent 卡片与阶段展示 |

---

## 数据模型调整

### `agent_crawl_runs` 扩展建议

保留现有计数字段：
- `plan_urls_count`
- `fetched_count`
- `quality_passed`
- `items_created`

新增字段：
- `current_stage: str`  
  值域：`planning | crawling | quality | summarizing | completed | failed`
- `stage_message: str | null`  
  用于记录当前阶段的人类可读说明，如“已规划 18 个 URL”
- `triggered_by: str | null`  
  预留给手动触发者标记

状态约定：
- `status` 继续作为总状态：`running | completed | failed`
- `current_stage` 用于 UI 呈现细粒度阶段

这样能避免前端把 `status` 和阶段语义混在一起，兼容性也更好。

---

## 任务 1：补齐后端运行端点

**目标：** 新增单源“立即抓取”端点，返回运行记录，并复用现有调度逻辑。

**文件：**
- 修改：`backend/app/api/agent_routes.py`
- 视情况修改：`backend/app/scheduler.py`
- 修改/新增：`backend/tests/integration/test_agent_api.py`

- [ ] **步骤 1：先写失败测试**

覆盖至少以下场景：
- `POST /sources/agent/{id}/run` 成功触发
- 非 `agent_crawl` source 返回 `404`
- source 正在运行时返回 `409`

```bash
cd backend && ENABLE_SCHEDULER=0 .venv/bin/pytest tests/integration/test_agent_api.py -q
```

预期：先因缺少 run 端点或断言不满足而失败

- [ ] **步骤 2：新增 `POST /sources/agent/{id}/run`**

实现要求：
- 校验 source 存在且类型为 `agent_crawl`
- 校验该 source 是否已有活跃运行
- 使用后台线程触发 `run_source_job(source_id)`
- 返回：
  - `message`
  - `source_id`
  - `accepted`

- [ ] **步骤 3：跑通集成测试**

```bash
cd backend && ENABLE_SCHEDULER=0 .venv/bin/pytest tests/integration/test_agent_api.py -q
```

---

## 任务 2：扩展 Agent 运行阶段模型

**目标：** 把 Agent Crawl 的阶段推进显式写入数据库，供前端稳定轮询展示。

**文件：**
- 修改：`backend/app/models.py`
- 新建：`backend/alembic/versions/<timestamp>_expand_agent_run_stage_fields.py`
- 修改：`backend/app/fetchers/agent_crawl.py`
- 修改/新增：`backend/tests/unit/test_agent_crawl_fetcher.py`

- [ ] **步骤 1：先写失败测试**

覆盖至少以下行为：
- 创建 run 后初始为 `status=running`, `current_stage=planning`
- 进入 CrawlDAG 后 `current_stage=crawling`
- Quality / Summary 阶段依次推进
- 异常时 `status=failed`, `current_stage=failed`, `completed_at` 写入

```bash
cd backend && ENABLE_SCHEDULER=0 .venv/bin/pytest tests/unit/test_agent_crawl_fetcher.py -q
```

预期：先因字段缺失或状态不匹配而失败

- [ ] **步骤 2：补模型和迁移**

迁移内容：
- 为 `agent_crawl_runs` 增加 `current_stage`
- 增加 `stage_message`
- 可选增加 `triggered_by`

- [ ] **步骤 3：在 `AgentCrawlFetcher` 中按阶段落库**

阶段推进建议：
- 创建 run：`planning`
- Plan 完成：写 `stage_message`
- Crawl 中：`crawling`
- Quality 中：`quality`
- Summary 中：`summarizing`
- 成功：`status=completed`, `current_stage=completed`
- 失败：`status=failed`, `current_stage=failed`

- [ ] **步骤 4：验证单测**

```bash
cd backend && ENABLE_SCHEDULER=0 .venv/bin/pytest tests/unit/test_agent_crawl_fetcher.py -q
```

---

## 任务 3：增加同源防重入保护

**目标：** 保证同一 Agent source 在任意时刻只有一个活跃运行。

**文件：**
- 修改：`backend/app/api/agent_routes.py`
- 可选修改：`backend/app/scheduler.py`
- 修改/新增：`backend/tests/integration/test_agent_api.py`

- [ ] **步骤 1：明确活跃运行判定**

判定规则建议：
- `status == "running"` 视为活跃
- 或 `current_stage in {"planning","crawling","quality","summarizing"}` 视为活跃

- [ ] **步骤 2：实现 409 保护**

要求：
- API 层先查 DB 活跃记录
- 若命中，返回 `HTTP 409`
- 返回体包含：
  - `detail`
  - `run_id`
  - `current_stage`

- [ ] **步骤 3：验证“不同 source 可并行、同 source 不可重入”**

```bash
cd backend && ENABLE_SCHEDULER=0 .venv/bin/pytest tests/integration/test_agent_api.py -q
```

---

## 任务 4：前端补齐 Agent 类型与 API

**目标：** 让首页可以获取 Agent 源、运行历史，并触发单源抓取。

**文件：**
- 修改：`frontend/src/types.ts`
- 修改：`frontend/src/api/client.ts`

- [ ] **步骤 1：先补类型**

新增类型建议：
- `AgentRunStage`
- `AgentRunStatus`
- `AgentSourceConfig`
- `AgentSource`
- `AgentRunRecord`

- [ ] **步骤 2：新增 API 函数**

新增：
- `fetchAgentSources()`
- `fetchAgentRuns(sourceId: number)`
- `triggerAgentRun(sourceId: number)`

说明：
- 先不把 Agent 模式和 SSE 绑死，首页轮询即可
- `triggerAgentRun()` 要把 `409` 作为可识别业务错误返回

- [ ] **步骤 3：跑前端类型检查**

```bash
cd frontend && npm run build
```

预期：先因组件尚未接入可能仍失败，但类型文件和 API 文件本身应无报错

---

## 任务 5：首页增加抓取模式切换

**目标：** 让现有 `NewsRunControl` 成为统一入口容器，而不是只服务标准抓取。

**文件：**
- 修改：`frontend/src/components/NewsRunControl.tsx`
- 修改：`frontend/src/pages/HomePage.tsx`
- 修改：`frontend/src/pages/newsRunControl.test.ts`

- [ ] **步骤 1：定义模式切换状态**

建议模式：
- `standard`
- `agent`

切换位置：
- 放在当前控制卡片头部
- 采用紧凑 segmented control，而不是新建二级页面

- [ ] **步骤 2：保持标准抓取原交互不回归**

要求：
- 现有 `onStart/onStop`
- 现有展开/收起
- 现有错误提示
- 现有状态文案

Agent 模式只是在同一位置切换显示，不应破坏标准抓取逻辑。

- [ ] **步骤 3：补测试**

至少覆盖：
- 默认模式
- 切到 agent 后渲染 agent 区域
- 切回 standard 后渲染原控制区

```bash
cd frontend && npx vitest run src/pages/newsRunControl.test.ts
```

---

## 任务 6：实现 Agent 模式主视图与单源卡片

**目标：** 在首页 Agent 模式下，清楚展示单源状态、阶段、计数和立即抓取入口。

**文件：**
- 新建：`frontend/src/components/AgentRunControl.tsx`
- 新建：`frontend/src/components/AgentSourceRunCard.tsx`
- 修改：`frontend/src/pages/HomePage.tsx`
- 新建：相关 vitest 测试文件

- [ ] **步骤 1：设计单源卡片信息结构**

每张卡片至少展示：
- source 名称
- root_url
- focus/topic 摘要
- 当前阶段或最近一次终态
- `plan_urls_count / fetched_count / quality_passed / items_created`
- `立即抓取` 按钮
- 最近一次错误摘要

- [ ] **步骤 2：定义前端阶段展示规则**

建议映射：
- `planning` -> “规划 URL”
- `crawling` -> “并行抓取”
- `quality` -> “质量筛选”
- `summarizing` -> “生成摘要”
- `completed` -> “已完成”
- `failed` -> “失败”

可附加一条进度副文案，例如：
- `已规划 18 个 URL`
- `已抓取 12 / 18`
- `质量通过 7 / 12`
- `已生成 5 条候选`

- [ ] **步骤 3：接入首页查询和轮询**

轮询建议：
- 只在存在活跃 Agent run 时对 `agent-sources` / `agent-runs` 做 2 秒轮询
- 没有活跃 run 时停止轮询

- [ ] **步骤 4：覆盖测试**

至少覆盖：
- 运行中阶段展示
- 完成态展示
- 失败态展示
- 运行中按钮禁用
- 409 错误展示

```bash
cd frontend && npx vitest run
```

---

## 任务 7：统一首页刷新策略

**目标：** 避免 Agent 模式与标准抓取模式互相干扰，同时保证运行结束后列表能自动刷新。

**文件：**
- 修改：`frontend/src/pages/HomePage.tsx`

- [ ] **步骤 1：拆分两套 active run 判定**

需要分别维护：
- `isManualNewsRunActive(...)`
- `isAgentSourceRunning(...)`

- [ ] **步骤 2：运行结束后的刷新策略**

建议：
- Agent run 从活跃切到终态后：
  - `invalidateQueries(["items"])`
  - 如有 Agent 源列表查询，也刷新 `["agent-sources"]`

- [ ] **步骤 3：验证不会误触发标准抓取日志区轮询**

说明：
- Agent 模式先不强依赖现有 `NewsRunLogPanel`
- 后续若接 Agent 专属日志，再走独立 query key

---

## 任务 8：联调与验收

**目标：** 做一次最小可用闭环，确认首页上的 Agent 模式确实能跑通。

**验证清单：**

- [ ] 首页能在 `标准抓取 / Agent Crawl` 间切换
- [ ] Agent 模式能列出 source
- [ ] 点击“立即抓取”后，卡片进入 `planning`
- [ ] 阶段能推进到 `crawling / quality / summarizing`
- [ ] 完成后显示计数结果
- [ ] 同一 source 重复点击得到禁用或 409 提示
- [ ] 不同 source 可以同时触发
- [ ] 运行结束后 items 自动刷新

**建议命令：**

```bash
cd backend && ENABLE_SCHEDULER=0 .venv/bin/pytest tests/integration/test_agent_api.py tests/unit/test_agent_crawl_fetcher.py -q
cd frontend && npx vitest run
cd frontend && npm run build
```

---

## 阶段划分建议

为了降低一次性改动面，建议按下面 4 个阶段执行：

### Phase A：后端触发能力
- run 端点
- 409 防重入
- 基本 API 测试

### Phase B：后端阶段可视化
- migration
- `current_stage` / `stage_message`
- fetcher 阶段落库

### Phase C：首页 Agent 模式
- 模式切换
- Agent source 列表
- 单源卡片 + 立即抓取

### Phase D：交互收口
- 轮询优化
- 终态刷新
- 错误态展示
- 文案打磨

---

## 设计取舍说明

1. **为什么不先做独立 `AgentSourcesPage`？**  
   因为你已经明确希望和现有抓取功能做选择，而不是把 Agent 藏到另一页；首页切换更符合当前使用路径。

2. **为什么阶段内并行不会影响展示？**  
   因为 UI 展示的是“当前总阶段”，不是每个 worker 的线程态。阶段内并行只影响计数推进，不影响阶段顺序。

3. **为什么还要加后端防重入？**  
   前端禁用只能挡住正常点击，挡不住重复请求、多个标签页或未来 API 调用；后端必须兜底。

4. **为什么 `status` 和 `current_stage` 分开？**  
   `status` 适合表达终态，`current_stage` 适合表达过程态。拆开后，前后端都更容易写清楚。

