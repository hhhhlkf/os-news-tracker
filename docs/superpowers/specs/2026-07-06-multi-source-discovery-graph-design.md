# Multi-Source Discovery Graph 设计

**日期：** 2026-07-06
**状态：** 待用户评审
**项目：** `os-news-tracker` — Site Discovery Graph 扩展为普通网站、公众号、司内 KM/iWiki 三分支

---

## 1. 背景

现有 `backend/app/discovery/graph.py` 的图逻辑主要服务普通网站/订阅链接探查：抓首页、抓网络请求、explorer 判断网页/API/RSS 结构、validator 验 URL 规律、dsl_writer 写 DSL、auditor 审计并实跑。

现在需要把“发现爬取方法”的入口扩展成三类来源：

1. 普通网址或订阅链接：沿用现有网站探查流程。
2. 微信公众号链接：进入公众号爬取分支，生成公众号抓取方法。
3. 公司内部知识来源：输入关键词/作者等文本并带 `[KM]` 或 `[iWiki]` 标记时，进入司内 MCP 检索分支，生成 MCP 工具组合形成的抓取方法。

核心目标不是立即把所有分支细节写满，而是先把完整 graph 框架搭出来，再逐个补充分支能力。

---

## 2. 设计原则

### 2.1 保持旧逻辑稳定

- 不直接重写现有 `dsl_writer()` 和 `auditor()`。
- 新增 `multi_dsl_writer()` 和 `multi_auditor()` 作为多来源聚合别名。
- 普通网站分支在 `multi_dsl_writer()`/`multi_auditor()` 内部继续委托旧 `dsl_writer()`/`auditor()`。
- 公众号和司内分支使用新的 branch writer/auditor，但遵循旧 writer/auditor 的思想：先生成可保存的动作配方，再由审计节点真实执行或真实调用工具验证。

### 2.2 路由先确定，能力后填充

第一阶段只搭整体框架：

- 输入归一化。
- source router agent 识别来源类型。
- 三个 branch explorer 产出统一 branch artifact。
- `multi_dsl_writer` 汇聚生成 DSL。
- `multi_auditor` 汇聚审计。
- save_method 复用或薄封装。

第二阶段开始逐个补充分支真实能力。

### 2.3 Prompt-first，不先写特殊硬规则

当某类特殊链接或 DSL 失败时，优先判断对应 prompt 是否没有讲清楚限制、字段边界、输出契约或工具能力。只有确认是结构性缺陷，且无法通过 prompt 稳定约束时，才写确定性硬规则代码。

### 2.4 测试约束

- 不为“骨架”写只检查格式的测试。
- 一个子流程功能完整搭建后，再写一个端到端真实功能测试。
- 测试应从 API 或 graph 入口直接走完整子流程，真实调用 LLM/工具/MCP/抓取能力；不允许 fake、仿真、只 mock 中间判断。
- 测试可以通过环境变量显式开启，避免默认 CI 误跑外部网络或司内 MCP。

---

## 3. 输入与路由

### 3.1 新入口请求

现有 `POST /discovery/run` 只接受 `url: HttpUrl`。多来源入口需要支持文本输入：

```json
{
  "input": "Linux 内核 热补丁 [KM]",
  "force": false,
  "name": "KM Linux hotpatch",
  "hints": {
    "author": "optional",
    "time_range": "optional"
  }
}
```

建议新增 `POST /discovery/multi-run`，先不破坏旧 `/discovery/run`。

### 3.2 路由结果

`source_router` 产出：

```json
{
  "kind": "website | wechat | internal_mcp | unsupported",
  "confidence": 0.0,
  "normalized_input": "...",
  "markers": ["KM"],
  "reason": "...",
  "suggested_branch": "website_explorer | wechat_explorer | internal_mcp_explorer"
}
```

### 3.3 路由规则

优先使用确定性预判，LLM 只处理模糊输入：

- 包含 `[KM]` 或 `[iWiki]`：`internal_mcp`。
- URL host 为 `mp.weixin.qq.com` 或明确微信公众号文章/主页链接：`wechat`。
- 其他 `http://` / `https://` / RSS feed URL：`website`。
- 纯关键词且无 `[KM]`/`[iWiki]`：先标记 `unsupported` 或要求用户选择来源，不默认走外网搜索。

---

## 4. Graph 结构

### 4.1 MultiDiscoveryState

在现有 `DiscoveryState` 旁新增 `MultiDiscoveryState`，不要把非网站字段硬塞进 `homepage/network_captures/url_rule`。

```python
class MultiDiscoveryState(DiscoveryState, total=False):
    raw_input: str
    normalized_input: str
    source_route: dict
    branch_kind: str
    branch_artifact: dict
    branch_trace_logs: list[dict]
    multi_dsl_recipe: dict | None
    multi_audit_result: dict | None
```

普通网站分支可以继续写入旧字段，公众号/司内分支主要写入 `branch_artifact`。

### 4.2 图草图

```text
START
  -> normalize_input
  -> source_router
  -> branch_supervisor
      -> website_preflight -> fetch_homepage -> capture_network -> explorer -> validator
      -> wechat_explorer
      -> internal_mcp_explorer
  -> multi_dsl_writer
  -> multi_auditor
      -> pass -> save_method -> END
      -> rewrite -> multi_dsl_writer
      -> reexplore -> selected branch explorer
      -> failed -> END
```

### 4.3 分支职责

**website branch**

- 复用现有 `fetch_homepage`、`capture_network`、`explorer`、`validator`。
- `multi_dsl_writer` 委托 `dsl_writer`。
- `multi_auditor` 委托 `auditor`。

**wechat branch**

- 识别文章 URL、公众号主页 URL、可能的文章列表入口。
- 探查可用抓取方式：页面抓取、历史文章接口、搜索入口、必要登录态说明。
- 产出 `branch_artifact`，描述可执行动作和限制。

**internal_mcp branch**

- 识别 `[KM]` / `[iWiki]` 标记并剥离查询文本。
- 调用 MCP 工具发现可用能力，形成检索路线。
- 产出 `branch_artifact`：MCP server/tool 名称、参数模板、分页/时间范围、字段映射。

---

## 5. Multi DSL 设计

### 5.1 不破坏旧 DSL

旧 `DslRecipe(recipe_type="dsl")` 继续只描述网页/HTTP/Playwright 动作。

新增多来源配方：

```json
{
  "recipe_type": "multi_dsl",
  "source_kind": "internal_mcp",
  "entry": "Linux 内核 热补丁 [KM]",
  "actions": [
    {
      "op": "mcp_call",
      "server": "km",
      "tool": "search_articles",
      "args": {
        "query": "Linux 内核 热补丁"
      },
      "as": "last_fetch"
    },
    {
      "op": "extract",
      "from": "last_fetch.items",
      "fields": {
        "title": "title",
        "url": "url",
        "published_at": "published_at",
        "summary": "summary",
        "content": "content"
      },
      "into": "items",
      "merge": false
    },
    {
      "op": "dedup_by",
      "field": "url"
    }
  ],
  "notes": []
}
```

### 5.2 新原语建议

- `mcp_list_tools`：列出 MCP server 工具能力，主要用于发现阶段，不一定存入最终配方。
- `mcp_call`：运行命执行 MCP 工具。
- `wechat_fetch_article`：抓取单篇公众号文章。
- `wechat_search`：按公众号/关键词检索文章。
- `normalize_items`：将分支返回统一成新闻 item 字段。

这些原语应放在新的 `multi_dsl.py` / `multi_interpreter.py`，旧 `dsl.py` 和 `interpreter.py` 尽量不动。

---

## 6. Auditor 设计

`multi_auditor` 按 `recipe_type/source_kind` 分派：

- `recipe_type == "dsl"`：调用旧 `auditor`。
- `source_kind == "internal_mcp"`：真实执行 MCP 调用，检查返回 items 是否有 title/url/content 或 summary。
- `source_kind == "wechat"`：真实执行公众号抓取动作，检查可用 item 数和字段质量。

审计结果仍保持旧思想：

```json
{
  "passed": true,
  "decision": "pass | rewrite | reexplore | fail",
  "errors": [],
  "test": {
    "items": [],
    "stats": {}
  },
  "llm_verdict": {}
}
```

---

## 7. API 与存储

### 7.1 API

新增：

- `POST /discovery/multi-run`
- `POST /discovery/methods/{id}/multi-fetch`

保留：

- `POST /discovery/run`
- `POST /discovery/methods/{id}/fetch`

### 7.2 存储

`crawl_methods.dsl_recipe` 是 JSON，可保存 `multi_dsl`。但运行命需要按 `recipe_type` dispatch：

- `dsl` -> `DslInterpreter`
- `multi_dsl` -> `MultiDslInterpreter`

`signature` 应包含 source_kind，避免同一文本/域名下不同来源冲突。

---

## 8. 风险

1. 司内 MCP 的真实工具名和权限未知：先设计抽象接口，等 MCP 可用后绑定具体 server/tool。
2. 公众号抓取可能受登录态、频控、反爬影响：writer 必须把限制写进 notes，auditor 必须真实执行验证。
3. 旧前端只理解 URL：multi-run 初期可只提供后端 API，前端后续再接。
4. 旧 `DiscoverRequest.url: HttpUrl` 不适合关键词：必须新增接口，避免破坏旧契约。

