# Multi-Source Discovery Graph 实现计划

**日期：** 2026-07-06
**状态：** 待执行
**对应设计：** `docs/superpowers/specs/2026-07-06-multi-source-discovery-graph-design.md`

---

## 总原则

- 先搭整体框架，再填分支能力。
- 不修改 Agent Crawl 相关逻辑。
- 不直接重写现有 `dsl_writer()` / `auditor()`；新增 `multi_dsl_writer()` / `multi_auditor()` 聚合。
- 不主动写无意义测试。每完成一个真实可用子流程后，再写一个从入口跑通整条子图的真实测试。

---

## Phase 1：多来源图骨架

### Task 1：新增输入与路由模型

文件：

- `backend/app/discovery/multi_schema.py`
- `backend/app/discovery/multi_graph.py`

实现：

- `MultiDiscoveryState`
- `SourceRoute`
- `BranchKind`
- `normalize_input(state)`
- `source_router(state, llm=None)`

验收：

- `[KM]` / `[iWiki]` 输入能路由到 `internal_mcp`。
- `mp.weixin.qq.com` 输入能路由到 `wechat`。
- 普通 URL/RSS 能路由到 `website`。
- 纯关键词无标记不默认进入网站流程。

不写测试，先用直接函数调用和日志确认。

### Task 2：搭建 multi graph 编排

文件：

- `backend/app/discovery/multi_graph.py`

实现：

- `branch_supervisor_route(state)`
- `website_branch_entry(state)`
- `wechat_explorer(state)` 占位但返回明确 unsupported/needs_implementation artifact。
- `internal_mcp_explorer(state)` 占位但返回明确 unsupported/needs_mcp artifact。
- `multi_dsl_writer(state, llm=None)`
- `multi_auditor(state, llm=None)`
- `build_multi_graph(checkpointer=None)`

验收：

- 三类输入都能走到 `multi_dsl_writer`。
- website 分支能委托旧流程节点，不破坏旧 `build_graph()`。
- 未实现分支应以结构化失败结束，而不是抛异常。

不写测试。

### Task 3：新增 multi-run API

文件：

- `backend/app/api/discovery_routes.py`

实现：

- `MultiDiscoverRequest(input: str, force: bool = False, name: str | None = None, hints: dict | None = None)`
- `POST /discovery/multi-run`
- `start_multi_discovery_run(...)`

验收：

- 旧 `/discovery/run` 不变。
- 新 `/discovery/multi-run` 可以接受 URL 和关键词。
- `site_discovery_runs.site_url` 可暂存 raw input 或 normalized input，但 node_trace 必须保留 route 信息。

不写测试。

---

## Phase 2：Multi DSL 与运行命

### Task 4：新增 multi DSL schema

文件：

- `backend/app/discovery/multi_dsl.py`

实现：

- `MultiDslRecipe(recipe_type="multi_dsl")`
- `McpCallAction`
- `WechatFetchArticleAction`
- `WechatSearchAction`
- 复用或重新声明 `ExtractAction` / `DedupByAction`
- `validate_multi_semantics(recipe)`

验收：

- 不修改旧 `DslRecipe(recipe_type="dsl")` 的语义。
- `multi_dsl` 可以表达 MCP 调用和公众号抓取动作。

不写测试。

### Task 5：新增 multi interpreter

文件：

- `backend/app/discovery/multi_interpreter.py`

实现：

- `MultiDslInterpreter.run(recipe)`
- `mcp_call` 执行适配点。
- `wechat_*` 执行适配点。
- `extract/dedup_by` 对齐旧解释器输出 `{items, stats}`。

验收：

- `multi_dsl` 运行命输出与旧 DSL 一样的 items/stats 结构。
- MCP/公众号适配点未配置时返回明确错误，不静默成功。

不写测试。

### Task 6：运行命 dispatch

文件：

- `backend/app/api/discovery_routes.py`

实现：

- 新增 `run_any_method(recipe_dict)`。
- `recipe_type == "dsl"` 走旧 `DslInterpreter`。
- `recipe_type == "multi_dsl"` 走 `MultiDslInterpreter`。
- 新增或复用 `/discovery/methods/{id}/multi-fetch`。

验收：

- 旧方法继续可抓。
- 新 multi 方法可按 recipe_type 分派。

不写测试。

---

## Phase 3：司内 MCP 分支

### Task 7：MCP 工具发现与调用适配

文件：

- `backend/app/discovery/internal_mcp_tools.py`
- `backend/app/discovery/multi_graph.py`
- `backend/app/discovery/multi_interpreter.py`

实现：

- MCP server/tool registry。
- KM/iWiki marker 到 server/tool 候选映射。
- `internal_mcp_explorer` 真实调用 MCP list/search 类工具。
- branch artifact 包含工具名、参数模板、字段映射、样本 items。

验收：

- 输入 `关键词 [KM]` 能真实调用 KM MCP 并返回样本。
- 输入 `关键词 [iWiki]` 能真实调用 iWiki MCP 并返回样本。
- 失败时说明是权限、网络、工具不存在还是无结果。

### Task 8：MCP writer/auditor

文件：

- `backend/app/discovery/multi_graph.py`

实现：

- `multi_dsl_writer` 将 internal branch artifact 写成 `multi_dsl`。
- `multi_auditor` 真实执行 `MultiDslInterpreter`，并用 LLM 对结果质量做判断。

验收：

- API 调 `POST /discovery/multi-run` 后可生成并保存 KM/iWiki 方法。
- 方法 fetch 可真实返回 items。

测试：

- 写一个 live 测试，环境变量开启，输入一个真实 `[KM]` 或 `[iWiki]` 查询，从 API 或 graph 入口跑完整子流程，真实调用 MCP。

---

## Phase 4：微信公众号分支

### Task 9：公众号 explorer

文件：

- `backend/app/discovery/wechat_tools.py`
- `backend/app/discovery/multi_graph.py`

实现：

- 识别公众号文章 URL。
- 抓单篇文章 title/author/published_at/content。
- 探查公众号主页或历史列表可用性。
- branch artifact 记录登录态/频控/不可访问原因。

验收：

- 单篇公众号文章链接可生成抓单篇方法。
- 如果历史列表无法访问，必须明确失败原因，不生成虚假可用方法。

### Task 10：公众号 writer/auditor

文件：

- `backend/app/discovery/multi_graph.py`
- `backend/app/discovery/multi_interpreter.py`

实现：

- 写 `wechat_fetch_article` / `wechat_search` multi DSL。
- auditor 真实执行并校验 item 内容质量。

验收：

- 公众号文章 URL 可以生成方法并 fetch 出 item。

测试：

- 写一个 live 测试，输入真实公众号文章 URL，从入口跑完整公众号子流程。

---

## Phase 5：前端接入

待后端三分支至少一个完整跑通后再做。

范围：

- Discovery 输入框支持 URL/关键词。
- 显示 route kind。
- 节点图展示 `source_router`、branch explorer、multi writer/auditor。
- 方法详情展示 `recipe_type/source_kind`。

