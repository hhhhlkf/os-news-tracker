# Multi-Source Discovery Graph 设计

**日期：** 2026-07-06
**状态：** 待用户评审
**项目：** `os-news-tracker` — Site Discovery Graph 扩展为普通网站、微信公众号、司内 KM/iWiki 三分支

---

## 1. 背景

现有 `backend/app/discovery/graph.py` 的图逻辑主要服务普通网站/订阅链接探查：抓首页、抓网络请求、explorer 判断网页/API/RSS 结构、validator 验 URL 规律、dsl_writer 写 DSL、auditor 审计并实跑。

现在需要把“发现爬取方法”的入口扩展成三类来源：

1. 普通网站或订阅链接：继续沿用现有网站探查流程。
2. 微信公众号：支持公众号名称/ID、公众号历史页 URL，并生成可保存、可审计、可运行的公众号抓取方法。
3. 公司内部知识来源：输入关键词、作者等文本并带 `[KM]` 或 `[iWiki]` 标记时，路由到司内 MCP 分支。本设计只定义接口契约，完整实现另行设计。

本设计采用方案 B：新增独立 multi graph 与 `multi_dsl`，旧网站分支委托旧流程，不重写现有 `dsl_writer()` / `auditor()`。

---

## 2. 范围与非目标

### 2.1 本次范围

- 新增多来源入口和 source router。
- 新增 multi graph：`normalize_input -> source_router -> branch explorer -> multi_dsl_writer -> multi_auditor -> save_method`。
- 普通网站分支作为旧流程的委托分支，不改内部逻辑。
- 公众号分支作为首个完整落地分支：
  - 支持公众号名称/ID。
  - 支持公众号历史页 URL。
  - 支持关键词搜索公众号文章。
  - 参考 `wechatarticles` 的公众号历史文章能力，并拆成系统内部工具。
  - 参考 `weixin_search_mcp` 的搜狗微信搜索能力，并拆成系统内部工具。
  - 支持默认认证 profile：`wechat_mp_default`。
- 新增 `multi_dsl` schema 和运行命 dispatch。
- 司内 KM/iWiki 分支只定义路由、artifact、`mcp_call` DSL 契约。

### 2.2 非目标

- 不修改 `backend/app/agent/` 和 `backend/app/fetchers/agent_crawl.py`。
- 不直接重写旧 `dsl_writer()` / `auditor()`。
- 不把第三方微信公众号项目作为黑盒依赖直接塞进 graph。
- 不在 DSL 中保存 cookie、token、header 等敏感认证信息。
- 不在本 spec 完整实现 KM/iWiki MCP 分支。
- 不为骨架写只检查格式的测试。

---

## 3. 设计原则

### 3.1 旧逻辑稳定优先

- 旧 `build_graph()`、旧 `/discovery/run`、旧 `DslRecipe(recipe_type="dsl")` 保持可用。
- 新增 `multi_dsl_writer()` 和 `multi_auditor()` 作为聚合节点。
- `branch_kind=website` 时，`multi_dsl_writer()` 委托旧 `dsl_writer()`，`multi_auditor()` 委托旧 `auditor()`。

### 3.2 先生成方法，再审计执行

三类来源都遵循同一思想：

1. agent/tool 先探查可行路线。
2. writer 将路线写成可保存 DSL。
3. auditor 真实执行 DSL 或真实调用工具验证。
4. 只有 auditor 通过或进入明确 pending 状态，才保存为方法。

### 3.3 Prompt-first，不先写特殊硬规则

特殊链接或 DSL 失败时，优先检查 prompt 是否没有讲清楚限制、字段边界、输出契约或工具能力。只有确认是结构性缺陷，且无法通过 prompt 稳定约束时，才加入确定性硬规则代码。

### 3.4 测试必须真实走通

一个子流程功能完整搭建后再写测试。测试必须从 API 或 graph 入口直接跑完整子流程，真实调用 LLM/工具/抓取能力；不写 fake、不写仿真、不写只检查格式的测试。需要外部网络或认证的测试用环境变量显式开启。

---

## 4. 输入与路由

### 4.1 API 输入

旧入口保留：

```text
POST /discovery/run
```

新增多来源入口：

```text
POST /discovery/multi-run
```

请求示例：

```json
{
  "input": "腾讯技术工程",
  "force": false,
  "name": "腾讯技术工程",
  "hints": {
    "source_kind": "wechat",
    "limit": 20,
    "fetch_content": false
  }
}
```

`hints.source_kind` 可选。没有 hint 时由 `source_router` 判断；用户明确指定 `wechat` 时，router 只做校验，不强行改成 website。

### 4.2 路由结果

`source_router` 输出结构化结果：

```json
{
  "kind": "website | wechat | internal_mcp | unsupported",
  "confidence": 0.0,
  "normalized_input": "...",
  "input_type": "url | feed | wechat_account | wechat_history_url | keyword | internal_query",
  "markers": ["KM"],
  "reason": "...",
  "suggested_branch": "website | wechat | internal_mcp"
}
```

### 4.3 路由规则

优先确定性判断，模糊输入再交给 LLM：

- 包含 `[KM]` 或 `[iWiki]`：`internal_mcp`。
- 明确 `mp.weixin.qq.com` 历史页 URL 或公众号相关 URL：`wechat`。
- 用户通过 `hints.source_kind=wechat` 指定：`wechat`。
- 普通 `http://` / `https://` / RSS feed URL：`website`。
- 纯关键词且无 hint、无 `[KM]`/`[iWiki]`：默认 `unsupported`，不自动走外网搜索。

---

## 5. Graph 架构

### 5.1 新增 agent/节点

新增节点放在 `backend/app/discovery/` 体系内，不放进 `backend/app/agent/`：

```text
source_router
wechat_explorer
internal_mcp_explorer
multi_dsl_writer
multi_auditor
```

旧网站分支继续使用：

```text
fetch_homepage
capture_network
explorer
validator
dsl_writer
auditor
save_method
```

### 5.2 MultiDiscoveryState

新增 `MultiDiscoveryState`，不要把非网站字段硬塞进 `homepage/network_captures/url_rule`。

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

普通网站分支可以继续写入旧字段；公众号和司内分支主要写入 `branch_artifact`。

### 5.3 图流转

```text
START
  -> normalize_input
  -> source_router
  -> branch_supervisor
      -> website branch:
           fetch_homepage -> capture_network -> explorer -> validator
      -> wechat branch:
           wechat_explorer
      -> internal_mcp branch:
           internal_mcp_explorer
  -> multi_dsl_writer
  -> multi_auditor
      -> pass -> save_method -> END
      -> pending_auth -> save_method -> END
      -> retry_later -> END
      -> rewrite -> multi_dsl_writer
      -> reexplore -> selected branch explorer
      -> failed -> END
```

---

## 6. 公众号分支设计

### 6.1 支持的输入

公众号分支支持三类输入：

1. 公众号名称/ID，例如 `腾讯技术工程`。
2. 公众号历史页 URL。
3. 关键词搜索公众号文章。

单篇公众号文章 URL 不是首个可交付版本主路径，可以后续作为辅助能力加入。

### 6.2 工具拆解

参考 `wechatarticles` 和 `weixin_search_mcp`，但拆成系统内部工具，位置建议：

```text
backend/app/discovery/wechat_tools.py
```

工具能力：

```text
wechat_search_articles(query, limit)
```

参考 `weixin_search_mcp`，通过搜狗微信搜索获取文章候选。该工具适合关键词搜索，不保证某公众号完整历史，不依赖 `auth_ref`。

```text
wechat_resolve_account(nickname_or_account_id, auth_ref)
```

参考 `wechatarticles` 类能力，尝试将公众号名称/ID 解析为 `fakeid`、`__biz`、标准 nickname。需要默认认证 profile。

```text
wechat_fetch_account_history(nickname, account_id, fakeid, biz, limit, fetch_content, auth_ref)
```

核心历史抓取工具。DSL 存 `limit`；解释器内部转换为 `begin/count` 分页，直到达到 `limit` 或无更多文章。运行时优先用 `fakeid/__biz`，失效后再用原始 nickname/account_id 重新解析。

```text
wechat_probe_history_url(history_url, auth_ref)
```

处理用户直接提供历史页 URL 的情况，判断公开可访问、需要登录、验证码、频控或不支持。

```text
wechat_fetch_article_content(url, auth_ref=None)
```

当 `fetch_content=true` 时逐篇抓正文。默认 `false`，避免首个可交付版本过慢或触发频控。

### 6.3 认证 profile

首个可交付版本只支持一个默认 profile：

```text
wechat_mp_default
```

真实敏感信息来自后端 env/config，例如：

```text
WECHAT_MP_COOKIE
WECHAT_MP_TOKEN
```

DSL 只保存引用：

```json
"auth_ref": "wechat_mp_default"
```

不在 DSL 或 node trace 中保存 cookie/token/header。配置缺失或失效时，工具返回 `pending_auth` 或 `auth_invalid`。

### 6.4 公众号 branch artifact

`wechat_explorer` 输出：

```json
{
  "source_kind": "wechat",
  "input_type": "account | history_url | keyword",
  "nickname": "腾讯技术工程",
  "account_id": null,
  "fakeid": "...",
  "__biz": "...",
  "history_url": null,
  "auth_ref": "wechat_mp_default",
  "limit": 20,
  "fetch_content": false,
  "sample_items": [],
  "status": "ok | pending_auth | auth_invalid | captcha_required | rate_limited | needs_resolver | unsupported | empty",
  "notes": []
}
```

---

## 7. Multi DSL 设计

### 7.1 旧 DSL 不变

旧配方继续使用：

```json
{
  "recipe_type": "dsl",
  "entry_url": "...",
  "actions": []
}
```

新配方使用：

```json
{
  "recipe_type": "multi_dsl",
  "source_kind": "wechat",
  "entry": "腾讯技术工程",
  "auth_ref": "wechat_mp_default",
  "requires_auth": true,
  "actions": []
}
```

`multi_dsl` 可以复用/继承旧 DSL 的 `extract`、`dedup_by`、变量渲染、语义校验思路，但新增公众号和 MCP 原语。旧 `dsl.py` 和 `interpreter.py` 尽量不动，新 schema 和解释器放在 `multi_dsl.py` / `multi_interpreter.py`。

### 7.2 公众号配方示例

```json
{
  "recipe_type": "multi_dsl",
  "source_kind": "wechat",
  "entry": "腾讯技术工程",
  "auth_ref": "wechat_mp_default",
  "requires_auth": true,
  "actions": [
    {
      "op": "wechat_fetch_account_history",
      "nickname": "腾讯技术工程",
      "account_id": null,
      "fakeid": "...",
      "__biz": "...",
      "limit": 20,
      "fetch_content": false,
      "auth_ref": "wechat_mp_default",
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

### 7.3 公众号原语

- `wechat_search_articles`
- `wechat_resolve_account`
- `wechat_fetch_account_history`
- `wechat_probe_history_url`
- `wechat_fetch_article_content`

### 7.4 MCP 原语契约

司内 KM/iWiki 分支在本设计中只定义契约：

```json
{
  "op": "mcp_call",
  "server": "km | iwiki",
  "tool": "search_articles",
  "args": {
    "query": "...",
    "author": null
  },
  "as": "last_fetch"
}
```

完整 MCP 工具发现、鉴权和审计在后续 spec 中展开。

---

## 8. Multi Auditor 设计

`multi_auditor` 按 `recipe_type/source_kind` 分派：

- `recipe_type == "dsl"`：委托旧 `auditor()`。
- `source_kind == "wechat"`：真实执行 `MultiDslInterpreter` 和公众号工具。
- `source_kind == "internal_mcp"`：按接口契约返回 `needs_implementation` 或 pending 状态，后续 spec 完整实现。

### 8.1 公众号状态语义

公众号工具统一返回结构化状态：

- `ok`
- `pending_auth`
- `auth_invalid`
- `captcha_required`
- `rate_limited`
- `needs_resolver`
- `unsupported`
- `empty`

### 8.2 公众号审计判定

`ok` 且返回文章数量达到动态阈值时，通过：

```text
required_count = min(5, max(1, floor(limit * 0.25)))
```

字段要求：

- 必须有 `title`。
- 必须有 `url`。
- `published_at` 尽量要求；如果底层工具明确不能返回，auditor 降级但记录 warning。
- `content` 只在 `fetch_content=true` 时要求。

其他状态：

- `pending_auth`：保存方法，状态不可运行，等待默认 profile 配置或恢复。
- `auth_invalid`：认证失效，方法不可运行。
- `captcha_required` / `rate_limited`：不重写 DSL，返回 `retry_later`。
- `needs_resolver`：回 `wechat_explorer` 或保存 pending resolver。
- 字段质量不足：回 `multi_dsl_writer` rewrite。

---

## 9. API 与存储

### 9.1 API

新增：

```text
POST /discovery/multi-run
```

保留：

```text
POST /discovery/run
POST /discovery/methods/{id}/fetch
```

方法运行入口建议不分裂：`/discovery/methods/{id}/fetch` 内部按 `recipe_type` dispatch。

```text
recipe_type=dsl       -> DslInterpreter
recipe_type=multi_dsl -> MultiDslInterpreter
```

### 9.2 存储

`crawl_methods.dsl_recipe` 是 JSON，可保存 `multi_dsl`。

方法状态需要表达更细：

- `active`：auditor passed，可运行。
- `pending_auth`：DSL 已生成，但需要默认认证 profile 配置或认证恢复。
- `auth_invalid`
- `retry_later`
- `failed`
- `disabled`

`signature` 建议包含：

```text
source_kind + normalized_input + auth_ref + fetch_content + limit
```

避免公众号名称、普通网站域名、不同抓取参数之间冲突。

---

## 10. 真实测试边界

不为 multi graph 骨架写只检查格式的测试。

公众号分支完整落地后，写 live 测试：

```text
POST /discovery/multi-run
input = 一个真实公众号名称或历史页 URL
hints.source_kind = wechat
hints.limit = 5
```

测试等待 run 完成，然后用生成的 method 调：

```text
POST /discovery/methods/{id}/fetch
```

并验证真实返回 items。测试需要环境变量显式开启：

```text
RUN_LIVE_WECHAT_DISCOVERY=1
WECHAT_MP_COOKIE=...
WECHAT_MP_TOKEN=...
```

如果没有认证配置，测试 skip，而不是 mock。

---

## 11. 风险与处理

1. 公众号平台接口、搜狗搜索接口、历史文章接口都可能变化。处理方式：工具返回结构化状态，不静默成功。
2. 微信认证信息会过期。处理方式：DSL 只存 `auth_ref`，认证失效返回 `auth_invalid`。
3. 公众号 ID 到历史文章列表不一定能稳定解析。处理方式：优先用 `fakeid/__biz`，失效后用 nickname/account_id 重新解析；仍失败则 `needs_resolver`。
4. 旧网站发现流程稳定性不能被影响。处理方式：旧入口、旧 DSL、旧 writer/auditor 保持委托，不重写。
5. KM/iWiki 真实 MCP 工具名和权限未知。处理方式：本 spec 只定义接口契约，不承诺可运行。

