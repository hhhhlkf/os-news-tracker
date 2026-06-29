# 站点链接发现 Agent 设计

**日期：** 2026-06-29
**状态：** 待用户评审
**项目：** `os-news-tracker` — 用 LangChain + LangGraph 实现的站点链接发现 Agent

---

## 1. 背景与动机

### 1.1 问题背景：现有探测的缺陷

现有 `app/sources/api_discovery.py` 做"字段映射"式探测：Playwright 渲染 → 抓 XHR → 找 JSON 列表数组 → 用预置字段名候选映射 title/url/date → 自检要求条目同时有 title 和 url。遇到 OpenAnolis/openEuler 这类 item 无 url 字段、需从 ID 推断详情页 URL 的站点彻底失败，且策略写死在程序里，遇到不在预置候选里的结构就死。

### 1.2 设计方向：LLM 主角的"链接发现 Agent"

把"链接发现"设计为"LLM 调工具推理 + 程序执行验证"：

- 给 LLM 一组工具 + 一个目标（搞清楚这个网站怎么爬），LLM 自主决定调哪些工具、怎么推理。
- LLM 边调工具边推理，搞明白爬取门道，产出一组**配置信息（DSL Recipe）**。
- DSL Recipe 交给**确定性解释器**执行。首次发现烧 token，之后只跑解释器，省 token。
- 配置搞不定的极端情况，判 failed 人工介入，不自动写脚本（理由见 §1.4）。

技术体系：LangChain `ChatOpenAI`（指向 DeepSeek，已验证网关支持原生 tool calling）+ `with_structured_output` 做结构化产出；LangGraph `StateGraph` + supervisor + worker agents 做编排；LangChain `@tool` + `bind_tools` 做工具。外层状态机控进度/审计，节点内 ReAct 子图给 LLM 灵活调工具空间。

### 1.3 范围

- **只新建 SiteDiscoveryAgent**（`app/discovery/`），现有 Handoff Chain（`app/agent/` 的 PlanAgent/CrawlDAG/QualityWorkerPool/SummaryWorkerPool）**保持不动**。
- **纯增量**：不删 `api_discovery.py`/`detector._detect_json_api`/`ConfigurableApiProbeAdapter`，不改写 `agent_crawl` 阶段 1/2。新 discovery 层走**新端点**独立提供能力，旧探测/旧抓取流程零影响。接入主流程是后续迁移事，不在本 spec。
- **第一子项目覆盖站点类型**：JSON API / RSS / 静态 HTML 列表（声明式 DSL 即可）+ 动态 SPA（Playwright 交互 DSL，基础动作）。三类以上网站统一用一种 DSL 表达。

---

## 2. 整体架构：两条命

### 2.1 生成命（首次/配置失效时，烧 token，LangGraph 多 Agent 协作）

```
SiteDiscoveryGraph (LangGraph StateGraph)
├─ 确定性程序节点（不调 LLM）：fetch_homepage / capture_network / save_method
├─ Supervisor 节点：路由决策、token 预算检查、终止判定
└─ Worker Agents（ReAct，LLM 自主调工具）：
     explorer    — 探查站点结构、找数据源、理解 item
     validator   — 推断 + 验证 URL 规律（技术真伪）
     dsl_writer  — 产出 DSL Recipe（with_structured_output）
     auditor     — 审计复核 DSL Recipe（结构合理性 + 独立测试达标判定）
→ 产出 DSL Recipe 存 crawl_methods + 审计 site_discovery_runs
```

### 2.2 运行命（后续，省 token，完全不调 LLM/agent）

```
discover_and_fetch(site_url)
→ find_method(site_url) 查 crawl_method_domains → crawl_methods
→ DslInterpreter.run(method.dsl_recipe) 顺序执行 actions → 吐约定 JSON
→ CrawlOutputIngester → RawItem → 现有 pipeline 阶段 3~5（复用不动）
```

**关键边界**：运行命根本不进 LangGraph。`DslInterpreter` 是纯确定性执行器，解释 DSL 动作序列，零 LLM。这是"规律总结后不使用 agent 的快速爬取"。

### 2.3 增量集成边界


| 现有组件                                                        | 处置                          |
| ----------------------------------------------------------- | --------------------------- |
| `app/sources/api_discovery.py`                              | **不动**，旧探测继续给现有 source 用    |
| `app/sources/detector.py::_detect_json_api`                 | **不动**                      |
| `app/fetchers/api_adapters.py::ConfigurableApiProbeAdapter` | **不动**，现有 api source 仍用它    |
| `app/fetchers/agent_crawl.py`                               | **不动**，现有 Handoff Chain 原样跑 |
| `app/agent/`（现有 Handoff Chain）                              | **不动**                      |
| `app/discovery/`                                            | **全新增**，与旧探测并存              |


**接入方式**：新端点 `POST /discovery/run`（触发生成命）、`POST /discovery/methods/{id}/fetch`（触发运行命）。不替换旧 `/sources/discover`，不接入 `agent_crawl` 主流程。

---

## 3. LangGraph 图结构

### 3.1 State（TypedDict，跨节点流转）

```python
class DiscoveryState(TypedDict):
    site_url: str
    homepage: dict          # fetch_homepage 产出：html/links/title
    network_captures: list  # capture_network 产出：XHR/JSON 响应
    exploration: dict       # explorer 产出：候选 API、items_path、id_field、字段映射
    url_rule: dict | None   # validator 产出：template + 验证证据
    dsl_recipe: dict | None # dsl_writer 产出
    audit_result: dict | None  # auditor 产出：通过/不通过 + 原因 + 测试统计
    attempt: int            # 重试轮数
    verdict: str | None     # "dsl" | "failed"
    method_id: int | None   # 最终存入的 crawl_methods.id
    token_used: int         # 累计 token（硬中止用）
    error: str | None
```

### 3.2 节点与边

```
START → fetch_homepage → capture_network → supervisor
                                         │
         ┌───────────┬───────────┬───────┴───────┬──────────┐
         ▼           ▼           ▼               ▼          ▼
      explorer   validator   dsl_writer      auditor    save_method
   (ReAct)     (ReAct)     (ReAct)         (ReAct)     (确定性)
         │           │           │               │          │
         └───────────┴───────────┴───────────────┘          ▼
                     ▼                                       END
                 supervisor ◄─────────────────────────
```

- **确定性程序节点**（零 LLM）：`fetch_homepage`、`capture_network`、`save_method`
- **Supervisor 节点**（轻量 LLM 路由）：看 State 决定下一个 worker / 终止 / 转兜底，并检查 token 预算
- **Worker Agents**（ReAct，LLM 自主调工具）：`explorer`、`validator`、`dsl_writer`、`auditor`

### 3.3 Supervisor 路由逻辑

- `capture_network` 后 → `explorer`（先探查站点结构）
- `explorer` 后 → `validator`（有候选 API，去验证 URL 规律）
- `validator` 后 → `dsl_writer`（有 URL 规律，组装 DSL Recipe）
- `dsl_writer` 后 → `auditor`（审计复核）
- `auditor` 通过 → `save_method` → END（verdict=dsl）
- `auditor` 不通过 且 `attempt < MAX_ATTEMPTS` → 回 `dsl_writer`（按 audit 反馈修 Recipe）或 `validator`（规律本身有问题）
- `attempt` 用尽 → END（verdict=failed，记 error）
- **token 硬中止**：supervisor 每轮检查 `token_used`，超 `TOKEN_BUDGET`（默认 50000，可配）→ END（verdict=failed，error="token_budget_exceeded"）

### 3.4 Checkpointing + 审计

- **PostgresSaver**：LangGraph checkpoint 持久化到 Postgres（复用现有 `DATABASE_URL`，checkpoint 表由 PostgresSaver 自动建，**不属于业务 3 表**）。支持**跨进程断点续跑**——进程崩了重启，从最后 checkpoint 续跑，不重烧 token。
- **业务审计**走 `site_discovery_runs.node_trace`：每个节点进出落一条 State diff 摘要 + token 消耗，可查阅的"操作录像"。与 checkpoint 职责分清：checkpoint 管运行时断点，`node_trace` 管可审计的节点级历史。

---

## 4. DSL 规约

### 4.1 执行模型

一个 DSL Recipe = `actions` 顺序数组 + 元数据。执行时维护上下文 context：

```python
{
  "vars": {"entry_url": "...", "page": 1, ...},   # 变量
  "items": [],                                      # 累积提取的条目
  "last_fetch": <json/feed/html 对象>,             # 最近一次 fetch 结果
  "browser": <Playwright 句柄>                     # 动态站用
}
```

`DslInterpreter` 顺序执行 actions，每个 action 读/写 context，最后吐 `context["items"]` 为约定 JSON。

### 4.2 原语参数 schema（8 个基础 + 扩展位）

`**fetch` — HTTP 获取（静态站主力）**

```json
{"op": "fetch",
 "mode": "json | feed | html",
 "url": "...",
 "method": "GET | POST",
 "headers": {}, "query": {}, "json_body": {},
 "as": "last_fetch"}
```

`**goto` / `wait_for` / `click` — Playwright 交互（动态站）**

```json
{"op": "goto",     "url": "...", "wait_until": "networkidle"}
{"op": "wait_for", "selector": "...", "timeout_ms": 5000}
{"op": "click",    "selector": "...", "after_wait_ms": 500}
```

`**extract` — 提取（fields 内嵌声明式）**

```json
{"op": "extract",
 "from": "obj.records | selector:article.item",
 "fields": {
   "title": "title",
   "url": "template:https://x/{item.path}.html",
   "published_at": "attr:datetime",
   "content": ["summary", "content"]
 },
 "into": "items",
 "merge": false}
```

`fields` 值四种形式：


| 形式             | 含义                             |
| -------------- | ------------------------------ |
| 裸字符串           | 字段名（json path 或 selector text） |
| `template:...` | 模板替换，`{item.field}` 引用当前条目字段   |
| `attr:...`     | 取元素属性                          |
| 数组             | 多个候选，第一个非空者用                   |


`**loop` — 循环**

```json
{"op": "loop",
 "until": <condition>,
 "max_iters": 5,
 "body": [<action>...],
 "on_each": [{"op": "set", "var": "page", "expr": "{{page}} + 1"}]}
```

`**set` — 设置变量**

```json
{"op": "set", "var": "page", "value": 2, "expr": "{{page}} + 1"}
```

`**dedup_by` — 去重**

```json
{"op": "dedup_by", "field": "url"}
```

**扩展位（定义 op 名，第一子项目不实装）**：`scroll_to`、`if`、`type`、`retry_with_stealth`、`solve_captcha`。

### 4.3 变量替换

**语法**：`{{var_name}}`，支持一层点号 `{{last_fetch.hasMore}}`。


| 变量                             | 来源            | 作用域                             |
| ------------------------------ | ------------- | ------------------------------- |
| `{{entry_url}}` `{{site_url}}` | Recipe 元数据    | 全程                              |
| `{{page}}` `{{iter}}`          | loop 计数器      | loop 体内                         |
| `{{last_fetch.xxx}}`           | 最近 fetch 结果字段 | fetch 之后                        |
| `{{item.xxx}}`                 | 当前条目字段        | 仅 `extract.fields` 的 template 内 |


**支持替换的位置**：`fetch.url`/`fetch.json_body`/`fetch.query`/`goto.url`/`set.expr`/`extract.fields` 的 template 值。
**不支持**：CSS selector（保持静态，避免注入面）。

### 4.4 loop 终止条件 DSL

`until` 五种取值 + 操作符：

```json
{"count_of": "items", ">=": 20}
{"var": "page", ">": 10}
{"path": "obj.hasMore", "==": false}
{"exists": "selector:button.load-more"}
{"not_exists": "selector:button.load-more"}
```

操作符：`>= > <= < == != exists not_exists`。
**终止 = `until` 为真 或 `max_iters` 用尽，先到先停**。`max_iters` 必填且 1 ≤ ≤ 20（校验强制），防死循环。

### 4.5 校验

**结构校验**（Pydantic discriminated union by `op`）：

```python
class DslRecipe(BaseModel):
    recipe_type: Literal["dsl"] = "dsl"
    entry_url: str
    actions: list[Annotated[Fetch|Goto|WaitFor|Click|Extract|Loop|Set|DedupBy, Field(discriminator="op")]]
```

**语义校验**（自定义 validator）：

1. `loop.max_iters` 必填且 1 ≤ ≤ 20
2. `extract.from` 须与最近 `fetch.mode` 匹配：mode=json → json path（如 `obj.records`）；mode=html → `selector:` 前缀；mode=feed → `from` 省略（feedparser 已解析为标准 entries，`fields` 直接映射 `title`/`link`/`published`/`summary` 等标准字段）
3. `extract.fields` 至少有一个能产出 `url`（裸字段名含 url/link/href，或有 `template:` 含 `{item.`）
4. `{{var}}` 引用的变量必须已定义（`{{page}}` 只在 loop 体内合法）
5. `goto/click/wait_for` 要求前面有 `goto` 打开浏览器上下文

**两道校验时机**：

- **生成命**：`dsl_writer` 用 LangChain `with_structured_output(DslRecipe)`，LLM 产出即过 Pydantic 校验，不合法让 LLM 重产（`max_attempts`）。
- **运行命**：`DslInterpreter` 加载 Recipe 时再过一遍校验，拦截存库后被改坏/历史不合法的 Recipe。

### 4.6 四类站的 DSL 示例

```
JSON API:  fetch(json) → extract(path, fields) → loop(set page+1, fetch, extract merge, until count>=N or hasMore=false)
RSS:       fetch(feed) → extract(标准字段)
静态 HTML: fetch(html) → extract(selector, fields) → loop(fetch next_page, extract merge, until no next)
动态 SPA:  goto → wait_for → loop(click load-more, wait_for, until not_exists button or count>=N) → extract(selector)
```

---

## 5. 数据模型与集成

### 5.1 数据模型（3 张表）

`**crawl_methods` — 统一爬取方式（存 DSL Recipe）**

```
id, domain, entry_url,
dsl_recipe JSONB,
signature,
status Enum("active"|"disabled"|"failed"),
created_at, updated_at, last_run_at, last_run_status
```

`**crawl_method_domains` — 去重映射**

```
id, domain, method_id FK→crawl_methods, created_at, unique(domain)
```

`**site_discovery_runs` — Agent 发现过程审计**

```
id, site_url, status Enum("running"|"completed"|"failed"),
node_trace JSONB,
resulting_method_id FK→crawl_methods, llm_token_usage,
started_at, ended_at, error_message, retry_count
```

ORM 约定：SQLAlchemy 2.0 `Mapped`+`mapped_column`；新增 Enum `CrawlMethodStatus`/`DiscoveryRunStatus` 进 `enums.py`；Alembic 迁移建表。

### 5.2 去重签名（DSL 化后重定义）

`signature = hash(entry_url + fetch_url_host + extract_from + has_loop)`

- `fetch_url_host`：第一个 `fetch`/`goto` 的 URL host
- `extract_from`：`extract.from` 值
- `has_loop`：是否含 loop
命中后用产出链接比对兜底。

### 5.3 新建文件结构

```
app/discovery/
├── graph.py          # SiteDiscoveryGraph（StateGraph + supervisor + 4 worker agents）
├── tools.py          # LangChain @tool 工具定义（fetch_page/capture_network/inspect/validate/test_dsl...）
├── dsl.py            # DSL Pydantic 模型 + 校验
├── interpreter.py    # DslInterpreter（运行命执行 DSL，httpx + Playwright）
├── ingester.py       # CrawlOutputIngester（DSL 产出 JSON → RawItem → pipeline）
└── signature.py      # 去重签名
```

### 5.4 依赖新增（`pyproject.toml`）

```
langchain-core>=0.3
langgraph[postgres]>=0.2
langchain-openai>=0.2
```

LLM 接入：`ChatOpenAI(base_url="https://api.deepseek.com", model="deepseek-v4-pro", api_key=...)`，复用现有 `LLM_API_KEY`/`LLM_BASE_URL` 配置。

### 5.5 完整数据流

```
生成命：discover_and_fetch(site_url) 无 method
  → SiteDiscoveryGraph.run(site_url)
     fetch_homepage → capture_network → supervisor
       → explorer(ReAct) → supervisor → validator(ReAct) → supervisor
       → dsl_writer(structured output DslRecipe) → auditor(ReAct 复核) → supervisor
       ├─ audit 通过 → save_method(去重+存 crawl_methods) → 记 site_discovery_runs → 返回 method
       └─ audit 不通过 & attempt<MAX → 回 dsl_writer/validator；用尽 → failed
  → 拿到 method，进运行命

运行命：discover_and_fetch(site_url) 有 method
  → DslInterpreter.run(method.dsl_recipe)
     顺序执行 actions（fetch/extract/loop/click...）→ 吐约定 JSON
  → CrawlOutputIngester.ingest(JSON) → RawItem → 现有 pipeline 阶段 3~5
```

---

## 6. 错误处理与失败语义

### 6.1 错误处理矩阵


| 失败点                  | 处理                                                   | 决策者               |
| -------------------- | ---------------------------------------------------- | ----------------- |
| LLM 超时/空响应/非 JSON    | ChatModel `max_retries`；worker 节点 try/except 吞单轮失败   | supervisor 重试或换路径 |
| dsl_writer 产出不合法 DSL | `with_structured_output(DslRecipe)` 强制重产；仍不合法 → 该轮失败 | supervisor        |
| auditor 不通过          | 按 audit 反馈路由：回 dsl_writer 或 validator                | supervisor        |
| validator 规律验证失败     | 回 explorer 换数据源/入口，或 validator 试别的 pattern           | supervisor        |
| 运行命 fetch 4xx/5xx    | 该 action 报错终止，吐空产出，`last_run_status=failed`          | DslInterpreter    |
| 运行命 selector 超时/找不到  | `timeout_ms` 到即报错                                    | DslInterpreter    |
| 运行命 loop 失控          | `max_iters` 硬上限 ≤ 20 到即停                             | DslInterpreter    |
| 生成命中途进程崩             | PostgresSaver checkpoint，跨进程从最后 checkpoint 续跑        | LangGraph         |


### 6.2 重试与失败语义

- `**MAX_ATTEMPTS = 3**`（可配）：图级重试上限。supervisor 决定回退到哪个 worker，`attempt+1`。
- **token 硬中止**：supervisor 每轮检查 `token_used`，超 `TOKEN_BUDGET`（默认 50000，可配）→ END，`status=failed`，`error="token_budget_exceeded"`。第一子项目实装。
- **单 worker 失败不拖垮整图**：节点级 try/except + supervisor 路由，单个 worker 失败只影响该轮，不传播到整图。
- **用尽 → `site_discovery_runs.status=failed`**：记 `error_message` + `node_trace`。failed 不阻塞同站未来重试。
- **运行命不达标产出不入库**：ingester 校验条目数/字段，不达标丢弃并记 `last_run_status=failed`，不写半截数据。

### 6.3 token 保护

- LangGraph `recursion_limit` 限制图总步数
- 每个 worker 的 ReAct 子图有独立 `max_iterations`
- `site_discovery_runs.llm_token_usage` 累计，supervisor 每轮检查，超 `TOKEN_BUDGET` 硬中止

---

## 7. 测试策略（分层，TDD）


| 层           | 对象                                                             | 依赖                     | 工具                                       |
| ----------- | -------------------------------------------------------------- | ---------------------- | ---------------------------------------- |
| 纯单测         | DSL 规约/校验（`dsl.py`）、签名（`signature.py`）                         | 无 I/O                  | pytest                                   |
| mock I/O 单测 | `interpreter.py`（四类站执行）、`tools.py`                             | mock HTTP + Playwright | respx、mock Playwright                    |
| mock LLM 单测 | `graph.py`（supervisor 路由、4 worker、重试/兜底/failed/token 硬中止）      | mock ChatModel         | LangGraph 测试模式 + mock checkpointer       |
| 集成测试        | 端到端：mock 站点 → 生成命 → 存 method → 运行命 → 入库                        | mock 全栈                | respx + 本地 HTML fixture + mock ChatModel |
| 回归保护        | 旧 `api_discovery`/`detector`/`api_adapters`/`agent_crawl` 现有测试 | —                      | 保持不动通过                                   |


关键测试用例：

- DSL：每原语合法/非法校验、变量替换、loop `until` 求值、`max_iters` 上限触发
- interpreter：四类站各跑通、loop 翻页到尾终止、dedup 去重、action 异常终止
- graph：supervisor 路由接力顺序、auditor 不通过→回退、`MAX_ATTEMPTS` 用尽→failed、token 超预算→硬中止、单 worker 失败不拖垮整图
- 集成：OpenAnolis/openEuler mock 站点走通生成命→运行命→入库

不打真实 DeepSeek/公网（除 `@pytest.mark.live` 手动验证），全 mock。

---

## 8. 涉及文件


| 文件                             | 改动                                                         |
| ------------------------------ | ---------------------------------------------------------- |
| `app/discovery/graph.py`       | **新建** SiteDiscoveryGraph                                  |
| `app/discovery/tools.py`       | **新建** LangChain @tool 工具集                                 |
| `app/discovery/dsl.py`         | **新建** DSL Pydantic 模型 + 校验                                |
| `app/discovery/interpreter.py` | **新建** DslInterpreter                                      |
| `app/discovery/ingester.py`    | **新建** CrawlOutputIngester                                 |
| `app/discovery/signature.py`   | **新建** 去重签名                                                |
| `app/api/discovery_routes.py`  | **新建** `/discovery/run`、`/discovery/methods/{id}/fetch` 端点 |
| `app/models.py`                | **加** 3 张表 ORM                                             |
| `app/enums.py`                 | **加** `CrawlMethodStatus`/`DiscoveryRunStatus`             |
| `alembic/versions/`            | **新建** 迁移                                                  |
| `pyproject.toml`               | **加** langchain-core/langgraph[postgres]/langchain-openai  |
| `tests/unit/discovery/`        | **新建** dsl/interpreter/tools/graph/signature 单测            |
| `tests/integration/`           | **新建** 端到端                                                 |


---

## 9. 验证标准

- OpenAnolis `openanolis.cn/blog` 首次运行 SiteDiscoveryGraph，产出合法 DSL Recipe（含 `fetch(json)` + `extract` fields 用 `template:.../{item.no}` + `loop` 翻页），auditor 自检通过，存入 `crawl_methods`。
- openEuler `www.openeuler.org/zh/interaction/blog-list/` 同样走通（POST body 分页由 `fetch` 的 `json_body` + `loop` 表达）。
- 第二次运行同站：不调 LLM（`site_discovery_runs` 无新记录），`DslInterpreter` 按 DSL 翻多页产出 > 单页条数，`CrawlOutputIngester` 入库成功。
- 一个动态 SPA mock 站点（load_more 按钮）：`goto + wait_for + loop(click + wait_for) + extract` 走通，产出达标。
- token 硬中止：mock LLM 累计 token 超 `TOKEN_BUDGET`，图中止、`status=failed`、`error=token_budget_exceeded`。
- 跨进程续跑：生成命中途 kill 进程，重启后从 PostgresSaver checkpoint 续跑成功。
- 去重：同类网站登记时签名命中，复用已存 method，`crawl_methods` 不膨胀。
- 旧功能回归：`api_discovery`/`detector`/`api_adapters`/`agent_crawl` 现有测试全通过。
- 全量后端测试通过。

---

## 10. 风险与缓解


| 风险                 | 缓解                                                                                              |
| ------------------ | ----------------------------------------------------------------------------------------------- |
| DSL 膨胀成迷你编程语言      | 原语小而正交，站类型是原语组合而非专用原语；新增原语需评审                                                                   |
| LLM 产出非法 DSL       | `with_structured_output(DslRecipe)` + `max_attempts` 重产 + 运行命加载时再校验                             |
| LLM 推理失败/烧 token 多 | `site_discovery_runs.node_trace` + `llm_token_usage` 可观测；`MAX_ATTEMPTS=3` 上限；`TOKEN_BUDGET` 硬中止 |
| URL 规律千奇百怪猜不中      | validator 的 `test_url_template`/`probe_url_patterns` 程序验证兜底                                     |
| 单 worker 失败拖垮整图    | 节点级 try/except + supervisor 路由，失败只影响该轮                                                          |
| 跨进程续跑丢中间态          | PostgresSaver checkpoint 持久化到 Postgres                                                          |
| 增量并存导致两套探测技术债      | 本 spec 明确纯增量；迁移接入主流程作为后续独立 spec，不在本 spec                                                        |
| DeepSeek 网关偶发空响应   | ChatModel `max_retries` + worker try/except（根治 LLM 空响应拖垮整图）                                     |

---

## 11. 扩展性：其他渠道

本设计聚焦 web 四类站（JSON API / RSS / 静态 HTML / 动态 SPA），但为后续扩展到其他渠道预留路径。本节是前瞻性说明，不在第一子项目实装。

### 11.1 渠道覆盖度

| 渠道 | 取数方式 | 覆盖度 |
|------|---------|-------|
| 公司内部文档（Confluence/MediaWiki）| REST API，JSON，内网 | 直接覆盖：`fetch(json)` + `extract` + `loop`，headers 带 token |
| 论坛（Discourse 等）| 官方 JSON API / HTML | 直接覆盖：API 走 `fetch(json)`，HTML 走 `fetch(html)` + `extract(selector)` |
| 社交平台关注（Twitter/微博）| 官方 API，cursor 分页，OAuth | 结构覆盖，需 cursor 分页增强 + 鉴权外置 |
| 微信公众号 | 封闭生态，无公开 API | 第三方 RSS 服务 → 直接覆盖；登录爬 → 需登录态原语；直接爬 → 不适合 DSL |

### 11.2 共痛点与处理

**鉴权/登录态**：外置到 `crawl_methods.auth_config` JSONB（后续扩展字段），DSL 的 `fetch.headers` 用 `{{auth.token}}` 引用。DSL 内不做 OAuth/登录流程——那是通用编程语言的事，会让 DSL 膨胀。OAuth/cookie 过期由外层服务刷新，DSL 只用带入的凭据。

**cursor 分页**（社交 API 通用）：增强现有原语能力，不新增专用原语——`extract` 支持 `into: "vars"`（把响应里的 `next_cursor` 提到变量），`loop` 用 `{{cursor}}` 喂给下次 `fetch`，`until` 用 `{"var": "cursor", "==": null}`。这是 `extract`/`loop`/`set` 的能力增强，守住"小而正交"。

**反爬**：优先用官方 API 绕开；强反爬 + 无 API 的渠道不走 DSL。`retry_with_stealth`/`solve_captcha` 原语已在扩展位，第一子项目不实装。

### 11.3 扩展策略

1. 保持 DSL 受限：不为每渠道加专用原语（如 `crawl_wechat`），用通用原语组合 + headers 鉴权
2. 鉴权外置：`crawl_methods.auth_config`，DSL 用 `{{auth.*}}` 引用
3. cursor 分页：`extract.into="vars"` + `loop` 变量传递（现有原语增强）
4. 封闭生态用专用 Fetcher 适配器：现有架构有 Fetcher Protocol，不走 DSL
5. 新原语按"小而正交 + 评审"加（如 `type` 做登录交互）

### 11.4 边界

本设计覆盖"能 HTTP 取内容 + 字段提取 + 翻页"的渠道（含鉴权 headers、cursor 分页增强后），即内部文档、论坛、社交 API、第三方 RSS。封闭生态 + 强反爬 + 无 API（如直接爬微信）超出 DSL 范畴，应走专用 Fetcher 适配器——这是合理边界，因为这类渠道本不该靠通用爬取解决。


