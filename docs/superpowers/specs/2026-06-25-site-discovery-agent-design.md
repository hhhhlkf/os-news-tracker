# 站点链接发现 Agent 设计（第一子项目：JSON API 型）

**日期：** 2026-06-25
**状态：** 待用户评审
**项目：** `os-news-tracker` — 用 LLM 驱动的"链接发现 Agent"替换现有探测层

---

## 1. 背景与动机

### 1.1 现有探测的根本缺陷

现有 `app/sources/api_discovery.py` 的 `discover_api_source` 做"字段映射"式探测：Playwright 渲染页面 → 抓 XHR → 找 JSON 列表数组 → 用预置字段名候选（`_TITLE_KEYS`/`_URL_KEYS`/`_PUBLISHED_KEYS`）映射 title/url/date → 自检要求条目**同时有 title 和 url**。

这套在 OpenAnolis 这类网站上彻底失败，根因链：

1. OpenAnolis `blogByCategoryPage.json` 返回的 item 只有文章 ID 字段 `no`，**没有 url 字段**。
2. 探测靠 `url_template` 推断详情页 URL，但 `openanolis.cn/blog` 是 SPA（`<div id="root">` + JS bundle），HTML 里**没有任何文章链接**，`_infer_url_template_from_anchors` 从锚点反推这条路走不通。
3. `url_template` 推断失败 → 条目无 url → `_self_check_probe` 的 `valid = [i for i in raw_items if i.title and i.url]` 为空 → 探测判失败 → **probe 永不缓存进 DB**。
4. probe 不进 DB → 后续分页引擎（`_fetch_json_list_paginated`）从未被调用 → 永远只爬第一页 10 条，且因默认 7 天时间窗（文章已是 14 天前）连这 10 条也被过滤光。

**核心缺陷**：现有探测只会"item 有什么字段就用什么"，不会"推断规律"（item 没 url 时从 ID 推断详情页 URL 是 `/blog/detail/{no}` 这种千奇百怪的规律）。且它把策略写死在程序里，遇到不在预置候选里的结构就死。

### 1.2 新方向：LLM 主角的"链接发现 Agent"

把"链接发现"从"程序字段映射"升级为"**LLM 调工具推理 + 程序执行验证**"：

- 给 LLM 一个 prompt（目标：搞清楚这个网站怎么爬）+ 一组丰富的工具，**LLM 自己决定调哪些工具、怎么推理**（不预设策略）。
- LLM 边调工具边推理，搞明白爬取门道，产出一组**配置信息（Recipe）**。
- Recipe 交给**插件式通用接口**执行。首次发现烧 token，之后只跑通用接口，省 token。
- 配置搞不定的极端情况，**LLM 自己写一个脚本文件**，通过受限命令行执行并登记，同类网站复用，不重复烧 token。

### 1.3 本文档范围（第一子项目）

这是个大系统的**第一个子项目**。整体愿景涵盖三类网站（JSON API / 静态 HTML / 动态 JS），但一次做全会重蹈"什么都能表示但什么都不精"的覆辙。第一子项目聚焦 **JSON API 型**（OpenAnolis 类），把"发现→规则→验证→保存→复用"一条路彻底走通，形成能站住的 Recipe 结构和工作流；静态 HTML 型、动态 JS 型作为**同结构的后续扩展**。

---

## 2. 范围与边界

### 2.1 第一子项目做什么

- 全新的 **SiteDiscoveryAgent**：LLM 驱动的链接发现，首次分析网站时运行。
- **17 个工具**支撑 LLM 探查网站（见 §4）。
- **Recipe 配置结构**（JSON API 型）+ **插件式通用接口**（`RecipeExecutor`）。
- **脚本兜底机制**：LLM 写 .py 脚本 + 受限命令行执行（`ScriptExecutor`）+ 统一执行/停止接口。第一子项目实装到"能存能加载能跑"，**安全沙箱留 TODO**。
- **数据模型**：4 张表统一管理爬取方式与执行生命周期。
- **去重**：爬取方式去重（避免重复存）+ 产出链接去重（避免重复算）。
- **绿地重建**：删旧 `api_discovery.py` / `detector.py` 的 JSON 识别部分 / `agent_crawl.py` 旧 probe 逻辑；改写编排器阶段 1/2；阶段 3~5（CrawlDAG/Quality/Summary）复用。
- 基于当前 `backup/paginated-probe-impl-before-rollback-20260625` 分支（含上一轮分页实现）建。上一轮分页配置字段命名（`page_param`/`has_more_path`/`size_param` 等）被新 Recipe 的 `pagination` 段继承；`_fetch_json_list_paginated` 的翻页循环逻辑被新 `RecipeExecutor` 继承。

### 2.2 明确不做（第一子项目）

- 静态 HTML 选择器型、动态 JS 交互型（load_more 按钮）的 Recipe 执行——通用接口预留扩展位，不实装。
- 脚本安全沙箱的深度实装（容器隔离/firejail/网络文件白名单）——只做白名单解释器 + 超时 + 工作目录隔离，其余 TODO。
- "规则失效自动修复"（运行中检测异常 → 再调 LLM 修复）——属第二子项目。
- 前端管理 UI——先做后端能力。
- 脚本回调基建工具（工具 1-12）——脚本自包含，要什么库自己 import；可选的"站点探查辅助库"留作后续优化，不预设。
- 阶段 3~5（抓正文/质量/摘要/入库）重写——复用现有。

---

## 3. 整体架构

### 3.1 两条命：生成命 vs 运行命

```
生成命（首次，烧 token，LLM 主导）：
  通用接口遇新网站 / 配置搞不定
  → 触发 SiteDiscoveryAgent（LLM 调工具 1-17 探查 + 推理 + 验证）
  → 产出 Recipe（优先）或 脚本（兜底）
  → 去重查重 → 存 crawl_methods
  → 记 site_discovery_runs（审计 + token）

运行命（后续，省 token，不调 LLM）：
  通用接口遇网站 → 查 crawl_methods
  ├─ executor_type=recipe → RecipeExecutor 解析 recipe_json 执行 → 吐产出 JSON
  └─ executor_type=script → ScriptExecutor.start → 子进程跑脚本 → collect 产出 JSON
  → CrawlOutputIngester 读产出 → 转 RawItem → 现有 pipeline 阶段 3~5
```

### 3.2 调度层（统一分流，不关心是 recipe 还是 script）

```
discover_and_fetch(site_url):
  1. method = find_recipe(site_url)   # 查 crawl_method_domains → crawl_methods
     ├─ 无 → 触发 SiteDiscoveryAgent 生成 method
     └─ 有 ↓
  2. 按 method.executor_type 分流：
     ├─ "recipe" → RecipeExecutor.run(recipe_json, params) → 产出 JSON
     └─ "script" → ScriptExecutor.start(method_id, params) → collect → 产出 JSON
  3. CrawlOutputIngester.ingest(产出 JSON) → 转 RawItem → pipeline 阶段 3~5
```

### 3.3 与现有系统的关系（绿地重建边界）

| 现有组件 | 处置 |
|---------|------|
| `app/sources/api_discovery.py` | **删**（被 SiteDiscoveryAgent + 工具集取代）|
| `app/sources/detector.py` 的 JSON API 识别（`_detect_json_api`）| **删** |
| `app/fetchers/agent_crawl.py::_try_runtime_discovery` / `_build_plan_from_api` 的 probe 逻辑 | **改写**为调用 RecipeExecutor/ScriptExecutor |
| `app/fetchers/api_adapters.py::ConfigurableApiProbeAdapter` | **删**（被 RecipeExecutor 取代）；`_fetch_json_list_paginated` 翻页逻辑迁移进 RecipeExecutor |
| `app/fetchers/agent_crawl.py` 编排器阶段 1/2 | **改写**：阶段1 用 SiteDiscoveryAgent/读 Recipe，阶段2 用 RecipeExecutor/ScriptExecutor 抓链接 |
| `CrawlDAG` / `QualityWorkerPool` / `SummaryWorkerPool`（阶段 3~5）| **复用**不动 |
| `app/extract/scrapling_extractor.py` | **复用**（工具 10/11 用）|
| 上一轮 `pagination` 字段命名 | **继承**进 Recipe.pagination 段 |

---

## 4. 工具集（17 个，给 LLM 用 + 后端服务）

工具按 LLM 探查网站的认知阶段组织：看页面 → 找数据源 → 理解条目 → 验证规律 → 确认详情页 → 收尾。工具设计原则：**LLM 提候选、程序执行、结果回喂 LLM 修订**（不预设最终规则）；**传精简信息不传整 HTML**（控 token）。

### 4.1 看页面
- **`fetch_page(url, render_js=False)`** → `{url, status, final_url, title, html, text, links}`。`render_js=False` 走 httpx，`=True` 走 Playwright。复用现有 `_capture_and_render` 的 Playwright 部分。
- **`extract_links(url, render_js=False)`** → `[{text, url, context}]`。锚文本 + 链接 + 周围文字。`render_js=True` 拿 JS 渲染后的链接。

### 4.2 找数据源
- **`capture_network(url)`** → `[{api_url, method, status, post_data, body_preview, parsed_json}]`。抓页面加载时的所有 JSON XHR/Fetch。SPA 命脉。复用现有 `page.on("response")` 捕获逻辑。`body_preview` 截断防 token 爆炸。
- **`json_path_explore(obj, path)`** → 该路径下的值 + 类型 + 数组长度 + 前 2 个元素样例。让 LLM 按路径探查大 JSON，不用整 JSON 塞上下文。

### 4.3 理解条目
- **`inspect_item(api_url, method, json_body)`** → `{status, items: [{字段名: 值样例}], item_count, sample_item_full}`。看 API 返回的 item 结构，判断有无 url 字段、ID 字段叫什么。复用现有 `_find_list_arrays`。
- **`list_fields(sample_item)`** → `[{field_name, value_type, value_sample, looks_like}]`。程序启发式初判字段类型（id/title/date/url/content/unknown），LLM 可采纳或推翻。

### 4.4 验证规律（核心环节）
- **`test_url_template(template, id_field, sample_items)`** → `{generated_urls, results: [{url, status, is_article_page, title_found, has_content}]}`。★最关键工具。LLM 猜模板，程序拿真实 ID 填进去逐个请求验证。`is_article_page` 对 SPA 详情页用"HTTP 200 + `<title>` 非空且不像首页标题"（不依赖正文长度，因 SPA 正文 JS 渲染）。
- **`probe_url_patterns(base_url, id_value, patterns=None)`** → `[{pattern, generated_url, status, is_article_page}]`。LLM 没头绪时批量试常见 pattern（`/blog/{id}`、`/blog/detail/{id}`、`/post/{id}`…）。`patterns=None` 用内置候选。定位为"灵感来源"，最终选哪个由 LLM 看验证结果决定。
- **`test_recipe(recipe)`** → `{discovered_urls, valid_count, invalid_count, sample_details, stats}`。整份 Recipe 自检，按 §7 严格统计指标。**含产出链接去重**（层面乙）。对应状态机 `ExecuteRule → ValidateLinks`。

### 4.5 确认详情页
- **`inspect_detail_page(url, render_js=False)`** → `{status, title, has_publish_date, content_chars, is_spa, structure_signature}`。核实单页是文章页。`structure_signature`（DOM 摘要如 `h1.title+div.content+time.date`）供同构判定。`is_spa` 提示正文 JS 渲染、`content_chars` 不可靠。复用 `ContentExtractor` + Playwright 兜底。
- **`compare_pages(urls)`** → `{same_structure, common_signature, per_page}`。判断一组详情页（3~5 个）是否同构，对应自检"多页结构相似"。

### 4.6 收尾
- **`save_recipe(domain, recipe)`** → `{saved, recipe_id}`。登记前先调 `find_similar_recipe` 查重（层面甲）。仅 `test_recipe` 自检通过后 LLM 才调。
- **`find_similar_recipe(site_url, recipe)` / `find_similar_script(site_url, purpose)`** → `{found, similar_id, similarity, match_reason}`。去重查重，按签名判定。见 §6 去重。

### 4.7 文件 / 脚本（LLM 写脚本兜底用）
- **`write_file(path, content)`** → `{written, path}`。LLM 把脚本存成文件。
- **`run_script(path, args, timeout)`** → `{exit_code, stdout, stderr, truncated}`。★受限命令行执行脚本。白名单解释器（仅 `python3`）+ 超时 + 工作目录隔离 + stdout 截断。stdout 约定为 JSON。LLM 生成命里临时验证脚本用（一次性、临时路径）；**复用底层同 `ScriptExecutor` 的子进程执行逻辑**。**含产出链接去重**（层面乙）。

### 4.8 脚本登记复用
- **`register_script(domain_or_pattern, path, description)`** → `{registered, script_id}`。登记前先调 `find_similar_script` 查重。把验证通过的脚本登记为"该类网站爬取方式"。
- **`find_script_for(site_url)`** → `{found, script_id, match_reason}`。按 domain/特征查已存脚本。运行命里通用接口配置搞不定时用。

### 4.9 后端服务（非 LLM 工具，运行命用）
- **`RecipeExecutor`**：解析 `recipe_json` 执行抓取，吐约定 JSON 产出。继承 `_fetch_json_list_paginated` 翻页逻辑。
- **`ScriptExecutor`**：统一脚本执行/停止/状态/收集。`start(method_id, params)→run_handle`、`stop(run_id)`、`get_status(run_id)`、`collect(run_id)→产出`。子进程受限（同 `run_script` 限制档：白名单解释器+超时+工作目录隔离）。
- **`CrawlOutputIngester`**：读约定 JSON 产出 → 校验 schema → 转 `RawItem` → 走现有 pipeline 入库 + 阶段 3~5。

> 工具 14 `run_script`（LLM 临时验证）与 `ScriptExecutor`（运行命复用）**复用同一套子进程执行底层**，入口不同：工具14 一次性临时执行，ScriptExecutor 带 run_handle 可管控停止。

---

## 5. Recipe 配置结构（JSON API 型）

通用接口能加载执行的配置。按"执行一次抓取需要知道什么"定字段，每个字段解决一个具体痛点：

```json
{
  "schema_version": "v1",
  "site_url": "https://openanolis.cn/blog",
  "entry": {
    "fetch_mode": "playwright_capture",
    "api_url_template": "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=",
    "method": "GET",
    "headers": {},
    "json_body": null
  },
  "items": {
    "items_path": "data.items",
    "fields": {
      "title": "title",
      "published_at": "gmtCreate",
      "content": ["summary", "content"],
      "id_field": "no"
    }
  },
  "item_url": {
    "source": "template",
    "template": "https://openanolis.cn/blog/detail/{id}",
    "id_field": "no"
  },
  "pagination": {
    "page_param": "page",
    "size_param": "pageSize",
    "size": 10,
    "start_page": 1,
    "max_pages": 5,
    "has_more_path": "data.hasMore"
  },
  "validation": {
    "min_discovered": 10,
    "min_valid_detail_ratio": 0.8,
    "max_duplicate_ratio": 0.2,
    "sample_detail_count": 5
  }
}
```

| 字段 | 解决什么 |
|------|---------|
| `entry.fetch_mode` | `playwright_capture`(SPA 抓 API) / `http`(直请求 API) / `html`(静态页选择器，后续扩展) |
| `entry.api_url_template` | 干净模板 URL，**不带写死的 `page=1&pageSize=10`**（解决"定死第一页"）|
| `items.fields.id_field` | 文章 ID 字段名（千奇百怪：`no`/`id`/`blogId`），LLM 识别后填 |
| `item_url.source` | ★三态：`field`(item 自带 url) / `template`(用 ID 拼) / `script`(转脚本兜底)。显式建模"千奇百怪链接结构" |
| `item_url.template` | LLM 推断+验证过的 URL 模板（`/blog/detail/{no}` 是试出来的，非预设）|
| `pagination` | 翻页规则，继承上一轮字段命名 |
| `validation` | 自检阈值，对齐 §7 严格标准 |

**`item_url.source = "script"` 是 recipe→script 的跃迁点**：LLM 探查时发现模板推断也验证不过，将此爬取方式的 `executor_type` 标为 `script`，存脚本而非 recipe。`executor_type`（宏观执行方式）与 `item_url.source`（跃迁原因标记）并存。

---

## 6. 去重（两个层面）

### 6.1 层面乙：产出链接去重（不需新工具，嵌进现有工具）
`test_recipe` 和 `run_script` 产出处理时按 URL 规范化去重（去 fragment、统一大小写、去尾斜杠后比对），重复的不计入 `discovered_count`，stats 暴露 `duplicate_count` / `duplicate_ratio`。继承上一轮 `_fetch_json_list_paginated` 的 `seen_urls` 思路。

### 6.2 层面甲：爬取方式去重（需签名机制 + 查重工具）
`save_recipe` / `register_script` 登记前调 `find_similar_*` 查重，命中复用已存项、不新建（在 `crawl_method_domains` 加"domain X → 已有 method Y"映射）。

**签名机制（第一子项目，JSON API 型）**：
- Recipe 签名 = `hash(api_url_host + items_path + url_template_normalized + pagination.page_param + pagination.has_more_path)`
  - `url_template_normalized`：把模板里的 ID 占位符统一成 `{id}`（`/blog/detail/{no}` 与 `/blog/detail/{id}` 视为同形）
- 脚本签名 = `hash(用途描述 + 适用 domain 模式)`（脚本内容难比对，先用"用途+适用范围"粗筛，命中再用产出比对）

---

## 7. 自检标准（严格验证，对齐用户 spec 第七节）

`test_recipe` / `run_script` 验证产出是否真文章页，达标才存：

- 发现候选链接数 ≥ `min_discovered`（默认 10）
- 随机验证 `sample_detail_count`（默认 5）个链接，至少 4 个是有效详情页
- 有效详情页比例 ≥ `min_valid_detail_ratio`（默认 0.8）
- 详情页标题成功率 ≥ 0.8
- 正文获取成功率 ≥ 0.8（对 SPA 详情页，正文判定降级为"`<title>` 像文章标题"，不依赖正文长度）
- 重复链接比例 ≤ `max_duplicate_ratio`（默认 0.2）

`passed = (discovered_count >= min_discovered and valid_detail_ratio >= min_valid_detail_ratio and duplicate_ratio <= max_duplicate_ratio)`

失败时 Agent 尝试：更换入口页 → 更换 URL Pattern → 启用 Playwright → 检查 JSON API → 实在不行转脚本兜底。最多重试 3~5 轮，避免无限循环。

---

## 8. 数据模型（4 张表）

### 8.1 `crawl_methods`（统一爬取方式）
```
id, domain(适用域名/模式,支持通配), entry_url,
executor_type Enum("recipe"|"script"),    ★执行方式字段
recipe_json JSONB, script_path, script_command, output_schema("v1"),
signature(去重签名), status Enum("active"|"disabled"|"failed"),
created_at, updated_at, last_run_at, last_run_status
```

### 8.2 `script_runs`（脚本执行生命周期）
```
id, method_id FK→crawl_methods, pid, command,
status Enum("running"|"completed"|"stopped"|"failed"|"timeout"),
started_at, ended_at, exit_code, output_location(产出JSON路径), error_message
```

### 8.3 `crawl_method_domains`（去重映射）
```
id, domain, method_id FK→crawl_methods, created_at, unique(domain)
```
去重命中时不新建 `crawl_methods`，在此表加"domain X → 已有 method Y"映射，底层记录共享，注册表不膨胀。

### 8.4 `site_discovery_runs`（Agent 发现过程审计）
```
id, site_url, status Enum("running"|"completed"|"failed"),
tool_calls JSONB(LLM 调了哪些工具/顺序/结果摘要——复盘哪步卡住+算token花在哪),
resulting_method_id FK→crawl_methods, llm_token_usage,
started_at, ended_at, error_message, retry_count
```
`tool_calls` = LLM 探查网站的"操作录像"，只在生成命写（运行命不写、不膨胀）。对应"这些功能都没写好"的痛点——以前黑箱，现在留录像可复盘/算账/重跑。

**ORM 约定**：SQLAlchemy 2.0 `Mapped`+`mapped_column`；新增 Enum `ExecutorType`/`CrawlMethodStatus`/`ScriptRunStatus`/`DiscoveryRunStatus` 进 `enums.py`；JSONB 用现有类型；Alembic 迁移建表。

---

## 9. 脚本执行与数据存取（三段式）

### 9.1 统一执行/停止（`ScriptExecutor` 服务）
- `start(method_id, params)→run_handle`：读 `crawl_methods.script_command`+`script_path`，拼 `python3 /scripts/xxx.py --site_url=... --output=...`，`subprocess.Popen` 启动（受限：白名单解释器+超时+工作目录隔离），落 `script_runs`（含 pid、status=running）。
- `stop(run_id)`：取 pid，`terminate`→超时未退则 `kill`，更新 status=stopped。
- `get_status(run_id)`：查 `script_runs` + 读子进程当前 stdout 缓存。
- `collect(run_id)→产出`：进程结束后读约定位置产出。

### 9.2 产出存取解耦（三段式契约）
**第一段 输出契约**：脚本不直接写主库（否则每个脚本要懂 ORM/DB schema，太重且危险）。约定脚本把产出写成标准格式 JSON 文件：
```json
{
  "schema_version": "v1",
  "site_url": "...", "fetched_at": "ISO8601",
  "items": [{"url":"...", "title":"...", "published_at":"...", "summary":"...", "content":"...", "extra":{}}],
  "stats": {"discovered_count": 15, "duplicate_count": 0}
}
```
外层 schema 固定（schema_version/items/stats），内层 item 是约定超集（url 必填，title/published_at/summary/content 可选）。脚本套壳吐 JSON，通用接口就能接。与 Recipe 产出同构，下游不分流。

**第二段 中转文件**：脚本写 JSON 到 `--output` 指定路径（`/var/data/crawl_runs/<run_id>/output.json`），`script_runs.output_location` 记路径。脚本崩了产出文件还在，可取证/重放；脚本不需 DB 连接串/ORM，降复杂度和安全面。

**第三段 通用入库**（`CrawlOutputIngester`）：脚本结束→`collect` 读 output JSON→校验 schema→每条 item 转 `RawItem`（现有 `schemas.py`）→走现有 pipeline 去重/存储（`repository.py` 幂等写）→阶段 3~5 接管（content 为空则抓正文、质量、摘要、入库）。

| 痛点 | 三段式解法 |
|------|-----------|
| 脚本产出格式不固定 | 外层固定、内层超集，套壳即可 |
| 脚本不该懂 DB | 只写 JSON 文件 |
| 脚本崩了数据丢 | 产出文件持久化、可重放 |
| recipe/script 产出要统一 | 两者同构 JSON，下游不分流 |

---

## 10. 工作流（固定状态机，非自由 ReAct）

```
START → FetchHomepage → FindCandidateSections → InspectListingPage
  → GenerateDiscoveryRule → ExecuteRule → ValidateLinks
  ├── 成功 → SaveRule（去重查重后存）
  └── 失败 → ReviseRule → ValidateLinks（最多 3~5 轮）
              └── 仍失败 → 转脚本兜底（write_file → run_script 验证 → register_script）
```
第一子项目用 Python 固定状态机实现（不引入 LangGraph，避免过度工程；后续多 Agent 扩展时再考虑）。

---

## 11. 涉及文件

| 文件 | 改动 |
|------|------|
| `app/discovery/agent.py` | **新建** SiteDiscoveryAgent（状态机 + LLM 编排）|
| `app/discovery/tools.py` | **新建** 17 个工具实现 |
| `app/discovery/recipe.py` | **新建** Recipe Pydantic 模型 + 校验 |
| `app/discovery/executor.py` | **新建** RecipeExecutor / ScriptExecutor / CrawlOutputIngester |
| `app/discovery/signature.py` | **新建** 去重签名 |
| `app/models.py` | **加** 4 张表 ORM |
| `app/enums.py` | **加** 4 个 Enum |
| `alembic/versions/` | **新建** 迁移 |
| `app/fetchers/agent_crawl.py` | **改写** 阶段 1/2 调用新发现层 |
| `app/sources/api_discovery.py` | **删** |
| `app/sources/detector.py` | **删** `_detect_json_api` |
| `app/fetchers/api_adapters.py::ConfigurableApiProbeAdapter` | **删**；翻页逻辑迁入 RecipeExecutor |
| `app/api/source_routes.py` | **改** 探测端点指向新 Agent |
| `tests/unit/discovery/` | **新建** 工具/Recipe/执行器/签名单测 |
| `tests/integration/` | **新建** 端到端：OpenAnolis 走通发现→复用 |

---

## 12. 验证标准

- OpenAnolis `openanolis.cn/blog` 首次运行 SiteDiscoveryAgent，产出有效 Recipe（`item_url.source=template`，`template=https://openanolis.cn/blog/detail/{id}`，`id_field=no`，`pagination.has_more_path=data.hasMore`），`test_recipe` 自检通过，存入 `crawl_methods`。
- 第二次运行同站：不调 LLM（`site_discovery_runs` 无新记录），`RecipeExecutor` 按 Recipe 翻多页产出 > 单页条数，`CrawlOutputIngester` 入库成功。
- 一个配置搞不定的模拟站点：LLM 写脚本，`run_script` 验证通过，`register_script` 登记，后续复用 `ScriptExecutor` 跑、不烧 token。
- 去重：同类网站登记时 `find_similar_*` 命中，复用已存 method，`crawl_methods` 不膨胀。
- 全量后端测试通过（含删旧探测后的回归）。

---

## 13. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 脚本安全（任意代码执行）| 第一子项目白名单解释器+超时+工作目录隔离；深度沙箱列 TODO；预期 OpenAnolis 用配置搞定，不真触发脚本 |
| LLM 推理失败/烧 token 多 | `site_discovery_runs.tool_calls`+`llm_token_usage` 可观测；最多 3~5 轮重试上限；失败降级为脚本兜底或放弃 |
| URL 规律千奇百怪猜不中 | `test_url_template`+`probe_url_patterns` 程序验证兜底；三态 `item_url.source` 含 script 跃迁 |
| 绿地删除旧探测破坏现有功能 | 逐 task 删+改，每步跑回归；阶段 3~5 复用不动；保留 `agent_crawl` 编排骨架 |
| 脚本间重复劳动 | 层面甲去重 + 可选"站点探查辅助库"（后续优化，不预设）|
