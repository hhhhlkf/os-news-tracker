# OS News Tracker — 代码审阅参考文档

> 面向代码审阅者的快速导航文档。以文件组织为线索，逐文件概括用途、关键逻辑和审阅关注点。

---

## 目录

- [1. 项目总览](#1-项目总览)
- [2. 后端 — 配置与入口层](#2-后端--配置与入口层)
- [3. 后端 — API 层](#3-后端--api-层)
- [4. 后端 — 数据模型与契约](#4-后端--数据模型与契约)
- [5. 后端 — 数据处理流水线](#5-后端--数据处理流水线)
- [6. 后端 — 采集器（Fetchers）](#6-后端--采集器fetchers)
- [7. 后端 — 内容提取（Extract）](#7-后端--内容提取extract)
- [8. 后端 — 搜索（Search）](#8-后端--搜索search)
- [9. 后端 — LLM 客户端](#9-后端--llm-客户端)
- [10. 后端 — 调度器（Scheduler）](#10-后端--调度器scheduler)
- [11. 后端 — 数据源注册](#11-后端--数据源注册)
- [12. 后端 — 数据库仓库层](#12-后端--数据库仓库层)
- [13. 后端 — 结构化数据流](#13-后端--结构化数据流)
- [14. 后端 — 数据库迁移](#14-后端--数据库迁移)
- [15. 后端 — 测试](#15-后端--测试)
- [16. 前端 — 入口与配置](#16-前端--入口与配置)
- [17. 前端 — 类型定义与 API 客户端](#17-前端--类型定义与-api-客户端)
- [18. 前端 — 组件](#18-前端--组件)
- [19. 前端 — 页面与辅助逻辑](#19-前端--页面与辅助逻辑)
- [20. 部署配置文件](#20-部署配置文件)
- [21. 数据流全景图](#21-数据流全景图)

---

## 1. 项目总览

OS News Tracker 是一个**技术新闻自动追踪系统**。它从 RSS、API、搜索引擎和固定页面四类来源抓取数据，经过归一化、去重、LLM 摘要后存入 Postgres，再通过 React 前端提供可搜索、可筛选的新闻阅读界面。

**核心架构：两条数据流**

| 数据流 | 路径 | 说明 |
|--------|------|------|
| 新闻动态流 | fetch → normalize → dedup → enrich → store | RSS/搜索/页面监控来的非结构化内容，走 LLM 做摘要 |
| 结构化事实流 | fetch → parse → upsert | 安全公告/CVE/生命周期等结构化数据，直接解析入库 |

**技术栈速览：** Python 3.11+ / FastAPI / SQLAlchemy 2.0 / Postgres / APScheduler / React 18 / Vite / TypeScript / TanStack Query

---

## 2. 后端 — 配置与入口层

### [backend/app/config.py](backend/app/config.py)

**用途：** 全局配置中心，所有环境相关参数的统一入口。

**关键点：**
- 使用 `pydantic-settings` 的 `BaseSettings`，自动从 `.env` 文件和环境变量加载配置
- `get_settings()` 用 `@lru_cache` 缓存，整个进程只实例化一次
- 包含的配置项：数据库 URL、LLM 网关地址/密钥/模型、搜索提供者、抓取 UA、请求间隔等

**审阅关注点：** 新加配置项是否提供了合理的默认值；敏感信息（API key）是否通过环境变量注入而非硬编码。

### [backend/app/entry.py](backend/app/entry.py)

**用途：** 应用启动入口，负责 FastAPI 应用创建和启动初始化。

**关键逻辑：**
1. 导入 `create_app()` 创建 FastAPI 实例
2. `on_event("startup")` 钩子中依次执行：
   - `Base.metadata.create_all(engine)` — 自动建表（开发/演示用，生产建议用 Alembic）
   - `seed_sources_from_yaml()` — 从 YAML 读取数据源定义，幂等写入 `sources` 表
   - 若 `ENABLE_SCHEDULER=1`：启动 APScheduler 后台调度器
   - 若 `RUN_STARTUP_BACKFILL=1`：在后台线程立即执行一轮全量抓取

**审阅关注点：** 环境变量控制的两层开关（`ENABLE_SCHEDULER` + `RUN_STARTUP_BACKFILL`）的默认值是否合理；`create_all` 在生产环境的行为。

### [backend/app/db.py](backend/app/db.py)

**用途：** 数据库连接管理。

**关键点：**
- 创建 SQLAlchemy `engine` 和 `SessionLocal`（sessionmaker）
- `get_session()` 是一个生成器函数，用作 FastAPI 的 `Depends` 依赖项，确保请求结束后 session 被关闭
- `expire_on_commit=False`：防止 commit 后访问属性触发额外查询

**审阅关注点：** engine 配置是否适合目标数据库（连接池大小等）；session 生命周期管理。

---

## 3. 后端 — API 层

### [backend/app/api/main.py](backend/app/api/main.py)

**用途：** FastAPI 应用工厂。

**关键点：**
- `create_app()` 创建 FastAPI 实例，配置 CORS 中间件（允许所有来源，内网部署场景）
- 注册 `routes.py` 中的 router
- 模块底部暴露 `app = create_app()` 供 uvicorn 直接引用

### [backend/app/api/routes.py](backend/app/api/routes.py)

**用途：** 所有 REST API 端点定义。这是前后端之间的**唯一数据合同**。

**端点一览：**

| 方法 | 路径 | 用途 | 关键参数 |
|------|------|------|---------|
| GET | `/items` | 分页列表查询 | `main_category`, `info_type`, `importance`, `q`（搜索）, `limit`, `offset` |
| GET | `/items/{id}` | 单条详情 | `item_id`（路径参数） |
| GET | `/facets` | 分面聚合统计 | 无参数，返回三个维度的 value+count |

**关键逻辑：**
- `/items` 支持多条件 AND 筛选 + 关键词模糊搜索（ILIKE 标题和摘要）
- 结果按 `published_at DESC NULLS LAST` 排序
- `/items/{id}` 返回完整字段，包括 tags、entities、source_links 的关联查询
- `/facets` 对 `main_category`、`info_type`、`importance` 三个列做 `GROUP BY` + `COUNT`

**审阅关注点：** SQL 注入风险（已使用参数化查询，安全）；ILIKE 在大表上的性能；分页上限 `le=200`；详情接口对不存在的 ID 返回 404。

### [backend/app/api/deps.py](backend/app/api/deps.py)

**用途：** FastAPI 依赖注入定义。

**关键点：**
- `get_db()` 从 `SessionLocal` 创建 session，用 `try/finally` 确保关闭
- 被 `routes.py` 中所有端点的 `db: Session = Depends(get_db)` 引用

---

## 4. 后端 — 数据模型与契约

### [backend/app/models.py](backend/app/models.py)

**用途：** SQLAlchemy ORM 模型定义，是整个系统的**数据骨架**。共 10 个表。

**新闻动态流相关（3 个实体表 + 3 个关联表）：**

| 表名 | 用途 | 关键字段 |
|------|------|---------|
| `sources` | 数据源配置 | name, type(rss/api/page_monitor/search), url, stream(news/structured), fetch_cron, enabled |
| `items` | 新闻条目（核心表） | url_hash(唯一索引), title, summary, key_points(JSON), info_type, importance, status |
| `tags` | 标签字典 | name, kind(main_category/sub_tag)，联合唯一约束 |
| `entities` | 实体字典 | type(vendor/product/os/...), name，联合唯一约束 |
| `item_tags` | 条目-标签关联 | item_id → items, tag_id → tags |
| `item_entities` | 条目-实体关联 | item_id → items, entity_id → entities, role |
| `item_sources` | 条目-来源关联 | 一条新闻可能被多个源报道，这个表记录了所有引用关系 |

**结构化数据流相关（4 个表）：**

| 表名 | 用途 |
|------|------|
| `security_advisories` | 安全公告（CVE 编号、严重等级、受影响产品、修复版本） |
| `product_lifecycles` | 产品生命周期（发布日期、EOL 日期、当前阶段） |
| `image_releases` | 镜像发布记录（tag、架构、云平台、checksum） |
| `compatibility_entries` | 兼容性条目（硬件/软件/包/镜像/OSV 兼容状态） |

**审阅关注点：** JSON 列的使用（`key_points`, `cve_ids` 等）——Postgres 支持 JSON 查询但不是所有操作都高效；各表的唯一约束设计是否覆盖了去重需求；`url_hash` 的唯一索引是去重的核心防线。

### [backend/app/schemas.py](backend/app/schemas.py)

**用途：** Pydantic 数据契约，定义流水线各阶段之间传递的数据结构。

| 类名 | 阶段 | 用途 |
|------|------|------|
| `RawItem` | fetch 输出 | 采集器产出的原始条目 |
| `ExtractedDoc` | extract 输出 | 从网页中提取的清洗后内容 |
| `NormalizedItem` | normalize 输出 | 统一格式的中间产物（含 canonical_url） |
| `EnrichedFields` | enrich 输出 | LLM 摘要后的结构化字段 |
| `EntityRef` | 嵌套在 EnrichedFields 中 | 实体引用（类型+名称+角色） |

**审阅关注点：** 字段的可选性（`| None`）是否与实际数据流一致；`canonical_url` 的生成逻辑（在 normalizer 中）是否正确处理了边界情况。

### [backend/app/enums.py](backend/app/enums.py)

**用途：** 所有枚举常量的统一定义。

| 枚举 | 取值 | 用途 |
|------|------|------|
| `SourceType` | rss, api, page_monitor, search | 数据源类型 |
| `Stream` | news, structured | 数据流类型 |
| `ItemStatus` | new, enriched, enrich_failed, needs_review | 条目处理状态 |
| `InfoType` | 发布, 更新, 性能数据, 适配, 观点/分析, 其他 | 信息类型（中文值） |
| `Importance` | 高, 中, 低 | 重要程度（中文值） |
| `EntityType` | vendor, product, os, package, version, topic | 实体类型 |
| `TagKind` | main_category, sub_tag | 标签种类 |
| `AdvisorySeverity` | critical, important, moderate, low, unknown | 安全公告严重等级 |
| `CompatibilityKind` | hardware, software, package, image, osv | 兼容性种类 |

**还有常量 `MAIN_CATEGORIES`**：`["OS跟踪来源", "友商产品信息", "软件包适配", "OS性能发展", "司内AI工具"]` — 这是 LLM 做分类时的候选集。

---

## 5. 后端 — 数据处理流水线

### [backend/app/pipeline.py](backend/app/pipeline.py)

**用途：** 新闻动态流的总编排器。对单个数据源执行完整的 fetch → extract → normalize → dedup → enrich → store 流程。

**核心方法 `run_source(source, fetcher) -> int`：**
1. 调用 `fetcher.fetch(source)` 拉取原始条目列表
2. 对每个原始条目：
   - `_extract_for()` — RSS 中有足够内容的直接用，否则调 Scrapling 提取网页正文
   - `normalize()` — 统一格式，生成 canonical URL
   - `exists_by_canonical()` — URL 去重检查
     - 已存在 → 调用 `merge_source_link()` 追加来源引用
     - 不存在 → 调 `enricher.enrich()` 做 LLM 摘要 → `save_enriched()` 入库
3. 更新 source 的健康状态
4. 返回新增条目数

**`_extract_for()` 的分支逻辑：**
- RSS 源 + 已有 >200 字符内容 → 直接使用，省一次 HTTP 请求
- RSS 源但内容不足 → 调 Scrapling 抓取完整网页
- 其他类型 → 直接使用 raw 中的内容

**审阅关注点：** 异常处理——fetch 失败和 enrich 失败分别处理，但 extract 和 normalize 的异常会向上传播；`_extract_for` 中 200 字符的阈值是否合理。

### [backend/app/processing/normalizer.py](backend/app/processing/normalizer.py)

**用途：** 将 RawItem + ExtractedDoc 合并为统一的 NormalizedItem。纯函数，无副作用。

**关键逻辑：**
- `canonicalize_url(url)` — 去掉 UTM 追踪参数（`utm_*`, `ref`, `fbclid`, `gclid`），统一 scheme 和 netloc 为小写，去掉路径末尾斜杠
- `normalize(raw, doc)` — 合并两个输入，优先用 doc 中的 title 和 content

**审阅关注点：** URL 规范化是否覆盖了所有需要去重的场景（比如 www vs non-www 未处理）；追踪参数列表是否完整。

### [backend/app/processing/dedup.py](backend/app/processing/dedup.py)

**用途：** 去重相关的纯函数集合。

| 函数 | 用途 |
|------|------|
| `url_hash(url)` | SHA256 哈希，用于精确 URL 去重 |
| `content_hash(text)` | SHA256 哈希，用于检测内容变化（PageMonitor 用） |
| `simhash(text, bits=64)` | SimHash 指纹，用于相似内容检测 |
| `hamming(a, b)` | 汉明距离计算 |
| `is_near_duplicate(a, b, threshold=4)` | 判断两个 simhash 是否在阈值内（默认 ≤4 位差异） |

**审阅关注点：** SimHash 的 token 归一化（`bugfix` → `fix`）只处理了一个特例，可能不足以覆盖所有变体；汉明距离阈值 4 对于 64 位 simhash 可能偏宽松。

### [backend/app/processing/relevance.py](backend/app/processing/relevance.py)

**用途：** 搜索结果的 LLM 相关性过滤。只用于 `SearchFetcher`（搜索关键词可能返回不相关的结果）。

**关键逻辑：**
- 构造 prompt：`"判断下面网页内容是否与关键词「{keywords}」相关的技术新闻。只回答 true 或 false。"`
- 传入标题 + 正文前 800 字符
- 解析 LLM 返回，检查是否以 `"true"` 开头

**审阅关注点：** LLM 返回格式不稳定时（如返回 "True" 大写或带标点）可能误判为不相关；800 字符截断可能丢失关键上下文。

### [backend/app/processing/enricher.py](backend/app/processing/enricher.py)

**用途：** LLM 驱动的内容摘要与结构化。是整个系统**最核心的智能环节**。

**关键逻辑：**
1. 构造详细 prompt，要求 LLM 以 JSON 格式输出 `title_tldr`、`summary`、`key_points`、`info_type`、`importance`、`why_it_matters`、`main_category`、`sub_tags`、`entities`、`confidence`
2. 传入标题 + 正文前 6000 字符
3. `_extract_json()` — 从 LLM 返回值中提取 JSON（支持 markdown 代码块和裸 JSON 两种格式）
4. 如果 LLM 返回的 `main_category` 不在预定义列表中，兜底为 `MAIN_CATEGORIES[-1]`（"司内AI工具"）

**审阅关注点：** JSON 提取的正则是防御性代码，但 corner case 很多（LLM 可能返回多段 JSON、注释等）；6000 字符截断对长文可能丢失信息；prompt 设计直接影响输出质量。

---

## 6. 后端 — 采集器（Fetchers）

所有采集器遵循统一接口（Protocol），可插拔、可独立测试。

### [backend/app/fetchers/base.py](backend/app/fetchers/base.py)

**用途：** 定义 `Fetcher` Protocol —— 任何采集器只需实现 `fetch(source) -> list[RawItem]`。

### [backend/app/fetchers/rss.py](backend/app/fetchers/rss.py)

**用途：** RSS/Atom 订阅源采集器。

**关键逻辑：**
- 使用 `feedparser` 解析 RSS/Atom feed
- 提取 title、link、summary/description、published 时间
- 时间解析使用 `python-dateutil`，自动处理时区转换为 UTC

**审阅关注点：** 时间解析失败时静默设为 None（不阻塞流程）；feedparser 对非标准 feed 的兼容性；没有处理分页/历史条目限制。

### [backend/app/fetchers/page_monitor.py](backend/app/fetchers/page_monitor.py)

**用途：** 固定页面变更监控器。只返回**内容发生变化**的页面。

**关键逻辑：**
1. 用 extractor 抓取页面内容
2. 计算 `content_hash`
3. 与上次抓取的 `last_content_hash` 比较
4. 相同 → 返回空列表（无变化）；不同 → 更新 hash 并返回一条 RawItem

**审阅关注点：** 这是幂等设计——同一内容不会重复入库；但如果页面有动态内容（时间戳、广告），每次 hash 都不同会导致频繁入库。

### [backend/app/fetchers/search.py](backend/app/fetchers/search.py)

**用途：** 关键词搜索采集器。先搜索再对结果做相关性过滤。

**关键逻辑：**
1. 调用 SearchProvider 搜索关键词
2. 对每个搜索结果用 extractor 抓取网页内容
3. 调用 `llm_relevance()` 判断是否与技术新闻相关
4. 只保留通过相关性检查的结果

**审阅关注点：** 最重的采集器——每个结果都要抓网页 + 调 LLM；搜索关键词来自 source 配置，关键词质量直接影响结果；相关性过滤依赖 LLM 调用，有成本和延迟。

### ~~backend/app/fetchers/api.py~~（不存在）

**说明：** API 类型的采集器在 V1 中尚未实现独立文件。结构化数据流的 API 源（如 Ubuntu Security Notices）目前通过 `structured/adapters/` 目录下的适配器处理，但该目录的文件也尚未创建。这是 V1 范围中预留的扩展点。

---

## 7. 后端 — 内容提取（Extract）

### [backend/app/extract/base.py](backend/app/extract/base.py)

**用途：** 定义 `ContentExtractor` Protocol —— `extract(url) -> ExtractedDoc`。

### [backend/app/extract/scrapling_extractor.py](backend/app/extract/scrapling_extractor.py)

**用途：** 基于 Scrapling 库的网页正文提取器（默认实现）。

**关键逻辑：**
1. 使用 Scrapling 的 `Fetcher` 抓取页面（支持 JS 渲染）
2. 用自定义的 `_TextExtractor`（HTMLParser 子类）提取纯文本：
   - 提取 `<title>` 标签内容
   - 跳过 `<script>` 和 `<style>` 标签
   - 合并文本块，压缩多余空白
3. 返回 `ExtractedDoc(url, title, clean_content)`

**审阅关注点：** 自实现的 HTMLParser 比较简单，对复杂页面（动态加载、非标准结构）的提取效果可能有限；Scrapling Fetcher 通过延迟导入避免未安装时的 import 错误；`_default_fetcher` 的惰性加载模式值得注意。

---

## 8. 后端 — 搜索（Search）

### [backend/app/search/base.py](backend/app/search/base.py)

**用途：** 搜索抽象层。

**关键组件：**
- `SearchResult` — 搜索结果的数据模型（url, title, snippet）
- `SearchProvider` Protocol — `search(query) -> list[SearchResult]`
- `NullSearchProvider` — 空实现（配置为 `"none"` 时使用），返回空列表
- `get_search_provider()` — 工厂函数，根据配置返回对应实现

**审阅关注点：** 工厂函数目前只支持 `"internal"` 一种非空实现，扩展新搜索提供者需要修改此函数。

### [backend/app/search/internal_gateway.py](backend/app/search/internal_gateway.py)

**用途：** 司内 LLM 网关的 web search 接口适配器（占位实现）。

**关键逻辑：** 向 `{llm_base_url}/web_search` 发 POST 请求，将返回结果映射为 `SearchResult` 列表。

**审阅关注点：** 这是占位实现，实际接口格式可能不同；依赖 LLM 网关的 web_search 能力；超时 15 秒。

---

## 9. 后端 — LLM 客户端

### [backend/app/llm/client.py](backend/app/llm/client.py)

**用途：** OpenAI 兼容的 LLM 客户端封装。系统中所有 LLM 调用的**唯一出口**。

**关键逻辑：**
- 使用 httpx 同步客户端（`timeout=60.0`）
- **内置缓存**：以 `sha256(model:prompt)` 为 key 缓存响应，同一 prompt 在进程生命周期内只请求一次
- `complete(prompt, temperature=0.2)` — 发送 chat completion 请求，返回文本内容
- 配置（base_url, api_key, model）均从 `Settings` 读取

**审阅关注点：** 缓存是无大小限制的内存字典，长时间运行可能占用大量内存；缓存不考虑 prompt 以外的因素（temperature 变化不会导致 cache miss）；同步 HTTP 调用会阻塞事件循环（但 scheduler 在独立线程中运行）。

---

## 10. 后端 — 调度器（Scheduler）

### [backend/app/scheduler.py](backend/app/scheduler.py)

**用途：** 基于 APScheduler 的定时任务管理。这是系统**自动运转的心脏**。

**核心函数：**

| 函数 | 用途 |
|------|------|
| `build_fetcher(source, extractor, search)` | 根据 source.type 构建对应的 Fetcher 实例 |
| `run_source_job(source_id)` | 单次执行：从 DB 加载 source → 构建 fetcher → 创建 Pipeline → 运行 → 记录日志 |
| `run_startup_backfill()` | 启动时立即对所有启用的 news 源执行一轮抓取 |
| `start_scheduler()` | 遍历所有启用的 source，为每个注册 cron 定时任务，启动 BackgroundScheduler |

**关键设计：**
- 每个 cron job 独立 session，job 之间不共享状态
- 默认 cron 为 `"0 8 * * *"`（每天早上 8 点）
- startup backfill 在后台线程（daemon）运行，不阻塞 FastAPI 启动
- `SUPPORTED_NEWS_SOURCE_TYPES` 常量排除了 `api` 类型（API 类型走结构化流）

**审阅关注点：** cron job 之间没有并发控制——如果上一个 job 没跑完就到了下一次触发时间，会同时运行两个实例；backfill 是顺序执行而非并行，源多时可能很慢；API 类型源被排除在新闻流之外。

---

## 11. 后端 — 数据源注册

### [backend/app/sources/registry.py](backend/app/sources/registry.py)

**用途：** 从 YAML 文件读取数据源定义，幂等写入数据库。

**关键逻辑：**
- `seed_sources_from_yaml(session, path)` — 读取 YAML → 遍历条目 → 按 `name` 查重 → 不存在的才 INSERT → commit
- 幂等性依赖 `name` 字段的唯一性（但数据库层面 `name` 没有唯一约束！）

**审阅关注点：** 数据库 `sources.name` 列没有 UNIQUE 约束，幂等性仅靠应用层 SELECT 保证，并发场景可能重复插入；不支持更新已有 source（只新增不修改）。

### [backend/app/sources/seed_sources.yaml](backend/app/sources/seed_sources.yaml)

**用途：** 初始数据源配置文件。共 9 个源。

| 类型 | 数量 | 示例 |
|------|------|------|
| RSS | 5 | Red Hat Blog, Ubuntu Blog, Phoronix, LWN, arXiv |
| page_monitor | 1 | RHEL Release Notes |
| API (structured) | 3 | Red Hat Security Data, Ubuntu Security Notices, Red Hat Product Life Cycle |

**审阅关注点：** arXiv 源启用了 `relevance_filter: true`，意味着每次抓取都会对每篇文章调 LLM 做相关性判断——成本较高；API 源的 adapter 配置引用了尚未实现的适配器（`redhat_securitydata`, `ubuntu_security`, `redhat_lifecycle`）。

---

## 12. 后端 — 数据库仓库层

### [backend/app/repository.py](backend/app/repository.py)

**用途：** 封装所有数据库读写操作，是流水线和 API 之间的**数据访问边界**。

| 方法 | 用途 |
|------|------|
| `exists_by_canonical(url)` | URL 去重检查 |
| `count_items()` | 条目总数 |
| `save_enriched(item, fields)` | 保存一条完整的新闻条目（含 tags、entities、source_link） |
| `merge_source_link(url, source_id, new_url)` | 为已有条目追加新的来源链接 |
| `_get_or_create_tag(name, kind)` | 标签的 get-or-create |
| `_get_or_create_entity(type, name)` | 实体的 get-or-create |

**审阅关注点：** `_get_or_create_*` 方法在并发下存在竞态条件（SELECT + INSERT 不是原子的）；`save_enriched` 在一个方法中做了多表写入，事务边界合理但缺少显式的 rollback 处理；`merge_source_link` 允许同一个 source_id+url 重复插入（没有去重检查）。

---

## 13. 后端 — 结构化数据流

**当前状态：** `structured/` 目录下的文件（schemas.py、repository.py、pipeline.py、adapters/）**均不存在**。这是 V1 设计文档中预留但尚未实现的模块。

**设计意图（来自设计文档）：**
- `schemas.py` — 定义 `AdvisoryRecord`、`LifecycleRecord` 等结构化数据模型
- `repository.py` — 结构化数据的幂等 upsert 逻辑
- `pipeline.py` — 结构化流的运行路径（不走 LLM，直接解析入库）
- `adapters/base.py` — `SourceAdapter` Protocol + 注册表
- `adapters/ubuntu_security.py` — Ubuntu Security Notices API 适配器示例

**审阅关注点：** 这是 V1 最大的未完成模块；seed_sources.yaml 中已经配置了 3 个 structured 类型的源，但对应的 adapter 和 pipeline 尚未实现。

---

## 14. 后端 — 数据库迁移

### [backend/alembic/env.py](backend/alembic/env.py)

**用途：** Alembic 迁移引擎配置。从 `app.models.Base.metadata` 读取表结构，从 `Settings` 读取数据库 URL。

### [backend/alembic/versions/ca363b702936_initial.py](backend/alembic/versions/ca363b702936_initial.py)

**用途：** 初始迁移脚本。创建全部 10 个表、索引和约束。由 Alembic autogenerate 生成。

### [backend/pyproject.toml](backend/pyproject.toml)

**用途：** Python 项目配置。定义依赖、测试配置和包发现规则。

**关键依赖：** FastAPI、SQLAlchemy 2.0、Alembic、psycopg（Postgres 驱动）、feedparser、httpx、APScheduler、Scrapling、Pydantic v2

**审阅关注点：** Scrapling 的 `[fetchers]` extra 可能需要额外系统依赖（Playwright）；psycopg 使用 binary 版本。

---

## 15. 后端 — 测试

### [backend/tests/conftest.py](backend/tests/conftest.py)

**用途：** pytest 全局配置。强制设置测试环境变量（SQLite 内存数据库 + 假 LLM 端点），防止测试意外连接外部服务。提供 `fixtures_dir` fixture。

### 单元测试（`tests/unit/`）

| 文件 | 测试对象 | 测试类型 |
|------|---------|---------|
| `test_config.py` | Settings 配置加载 | 纯逻辑 |
| `test_dedup.py` | url_hash, content_hash, simhash, 汉明距离 | 纯逻辑 |
| `test_normalizer.py` | URL 规范化 | 纯逻辑 |
| `test_schemas.py` | Pydantic 模型验证 | 纯逻辑 |
| `test_models.py` | ORM 模型创建和关系 | 需 DB（SQLite 内存） |
| `test_rss_fetcher.py` | RssFetcher（mock feedparser） | 单元 + mock |
| `test_page_monitor.py` | PageMonitorFetcher | 单元 + mock |
| `test_search_fetcher.py` | SearchFetcher | 单元 + mock |
| `test_extract.py` | ScraplingExtractor | 单元 + mock |
| `test_llm_client.py` | LlmClient（mock HTTP） | 单元 + mock |
| `test_enricher.py` | Enricher（mock LLM） | 单元 + mock |
| `test_search_provider.py` | SearchProvider 实现 | 单元 + mock |
| `test_entry_import.py` | entry 模块导入 | 冒烟测试 |
| `test_scheduler.py` | scheduler 函数 | 单元 + mock |

### 集成测试（`tests/integration/`）

| 文件 | 测试场景 |
|------|---------|
| `test_api.py` | FastAPI 端点（含筛选、搜索、404） |
| `test_pipeline.py` | Pipeline.run_source 端到端流程 |
| `test_registry.py` | seed_sources_from_yaml 幂等性 |
| `test_repository.py` | Repository 的 CRUD + 去重 |

**审阅关注点：** 集成测试依赖 SQLite（与生产 Postgres 行为可能不同，特别是 JSON 查询和并发行为）。

---

## 16. 前端 — 入口与配置

### [frontend/index.html](frontend/index.html)

**用途：** SPA 入口 HTML。`<div id="root">` 挂载 React，`<script type="module" src="/src/main.tsx">` 加载应用。

### [frontend/src/main.tsx](frontend/src/main.tsx)

**用途：** React 应用启动入口。

**关键逻辑：** 创建 `QueryClient`（TanStack Query 的全局缓存管理器）→ 包裹 `QueryClientProvider` + `React.StrictMode` → 渲染 `<App />`。

### [frontend/src/App.tsx](frontend/src/App.tsx)

**用途：** 根组件。目前只渲染 `<HomePage />`，单页应用无需路由。

### [frontend/src/index.css](frontend/src/index.css)

**用途：** 全局基础样式。设置 `:root` 的字体、颜色、抗锯齿，`body` 去除 margin，全局 `box-sizing: border-box`。

### [frontend/vite.config.ts](frontend/vite.config.ts)

**用途：** Vite 构建配置。

**关键配置：**
- `host: "0.0.0.0"` — Docker 容器内可访问
- `usePolling: true` — 文件监听使用轮询模式（Docker 兼容）
- proxy：`/items` 和 `/facets` 代理到 `http://backend:8000`（开发时绕过 CORS）

### [frontend/package.json](frontend/package.json)

**用途：** 前端依赖和脚本定义。

**关键依赖：** React 19、TanStack Query 5、Vite 8、TypeScript 6、Vitest 4

### [frontend/Dockerfile](frontend/Dockerfile)

**用途：** 前端生产镜像。多阶段构建：Node 编译 → nginx 托管静态文件。

---

## 17. 前端 — 类型定义与 API 客户端

### [frontend/src/types.ts](frontend/src/types.ts)

**用途：** 前端所有的 TypeScript 类型定义，与后端 API 响应结构一一对应。

| 类型 | 对应后端 |
|------|---------|
| `ItemSummary` | `/items` 响应中的单个 item |
| `ItemDetail` | `/items/{id}` 响应（继承 ItemSummary） |
| `Facets` / `FacetValue` | `/facets` 响应 |
| `ItemListResponse` | `/items` 响应的顶层结构 |

### [frontend/src/api/client.ts](frontend/src/api/client.ts)

**用途：** API 调用封装层。前端与后端通信的**唯一出口**。

**关键设计：**
- `ApiError` 类 — 自定义错误类型（含 HTTP status），组件可做类型安全的错误处理
- `BASE` — API 地址，默认 `http://localhost:8000`，可通过 `VITE_API_BASE` 环境变量覆盖
- `fetchItems(params)` — 将参数对象转为 URLSearchParams，GET 请求
- `fetchItemDetail(id)` — 单条详情
- `fetchFacets()` — 分面聚合数据

**审阅关注点：** 没有请求超时设置；没有重试逻辑；`fetchItems` 把所有参数都序列化到 query string（包括空值）。

---

## 18. 前端 — 组件

### [frontend/src/components/ImportanceBadge.tsx](frontend/src/components/ImportanceBadge.tsx)

**用途：** 重要度标签组件。纯展示组件。

**视觉效果：**
- 高（红色）：`#fde2e1` 背景 + `#b42318` 文字
- 中（黄色）：`#fef3c7` 背景 + `#92400e` 文字
- 低（灰色）：`#eceef1` 背景 + `#475467` 文字
- `null` → 不渲染

### [frontend/src/components/InfoTypeBadge.tsx](frontend/src/components/InfoTypeBadge.tsx)

**用途：** 信息类型标签组件。中性灰色边框样式。`null` → 不渲染。

### [frontend/src/components/ItemCard.tsx](frontend/src/components/ItemCard.tsx)

**用途：** 列表中的单条新闻卡片。

**显示内容：** 重要度标签 + 信息类型标签 + 分类名 + 发布日期 + 标题（优先显示 TLDR 版本）

**交互：** 整个卡片是一个 `<button>`，点击触发 `onClick` 回调（打开详情面板）。

### [frontend/src/components/ItemList.tsx](frontend/src/components/ItemList.tsx)

**用途：** 新闻列表容器。处理三种状态：

| 状态 | 渲染 |
|------|------|
| 加载中 | "正在加载条目…" |
| 空结果 | 虚线边框的空白状态（显示 `emptyMessage`） |
| 有数据 | "共 N 条" + ItemCard 列表 |

### [frontend/src/components/ItemDetail.tsx](frontend/src/components/ItemDetail.tsx)

**用途：** 新闻详情展示组件。支持两种数据来源：

1. **直接传入 `item`**（演示模式）→ 直接渲染
2. **传入 `id`**（实时模式）→ 通过 TanStack Query 的 `useQuery` 获取数据

**展示的 7 个区块：**
1. 标签行（重要度 + 信息类型 + 分类）
2. 标题
3. 摘要（有条件渲染）
4. 关键点列表（有条件渲染）
5. 影响/意义（蓝色高亮框，有条件渲染）
6. 实体列表（有条件渲染）
7. 来源链接（有条件渲染）

**错误处理：** 加载中 / 加载失败（区分 ApiError 和普通错误） / 无数据 / 未选择条目

### [frontend/src/components/FacetSidebar.tsx](frontend/src/components/FacetSidebar.tsx)

**用途：** 分面筛选侧边栏。

**三个筛选组：** 主分类 → 信息类型 → 重要度

**交互：** 点击选中（高亮蓝色），再次点击取消选中。选中状态通过 `selected` map 和 `onSelect` 回调管理。

**加载态：** "正在加载筛选项…"

---

## 19. 前端 — 页面与辅助逻辑

### [frontend/src/pages/HomePage.tsx](frontend/src/pages/HomePage.tsx)

**用途：** 唯一的页面组件，承载所有 UI 布局和状态管理。这是前端的**核心组件**。

**状态管理：**
- `filters` — 当前筛选条件（含搜索关键词 `q`）
- `openId` — 当前打开的详情条目 ID
- 两个 TanStack Query：`itemsQuery`（列表数据）和 `facetsQuery`（分面数据）

**核心逻辑 — 双模式切换：**
- 通过 `resolveHomeDataMode()` 判断：API 请求失败 → 自动切换到 "demo" 模式，使用内置演示数据
- "demo" 模式显示蓝色提示条："当前未连接到后端 API，页面自动切换为演示数据"

**页面布局：**
```
┌─────────────────────────────────────────────┐
│  Header（标题 + 数据源状态 + 条目数 + 筛选数） │
├─────────────────────────────────────────────┤
│  搜索框 + 清空按钮                           │
├────────────┬────────────────────────────────┤
│  Facet     │  ItemList                      │
│  Sidebar   │  （或空状态提示）                │
│            │                                │
└────────────┴────────────────────────────────┘
                    ┌──────────┐
  详情面板（overlay） │ ItemDetail│
                    └──────────┘
```

**空状态处理（三层）：**
1. 实时模式 + 无数据 → "数据库里还没有已入库条目…"
2. 筛选后无匹配 → "没有匹配的条目，试试放宽搜索词…"
3. 演示模式提示条

### [frontend/src/pages/homeData.ts](frontend/src/pages/homeData.ts)

**用途：** HomePage 的数据处理辅助函数。纯逻辑，与 UI 无关（因此可独立测试）。

| 函数 | 用途 |
|------|------|
| `resolveHomeDataMode({itemsFailed, facetsFailed})` | 任一 API 失败 → `"demo"`；都成功 → `"live"` |
| `filterDemoItems(items, filters)` | 演示数据的筛选逻辑（搜索 + 分类 + 信息类型 + 重要度） |
| `buildDemoFacets(items)` | 从演示数据动态计算分面计数 |
| `makeListResponse(items)` | 将完整 ItemDetail 裁剪为 ItemSummary 列表 |

**审阅关注点：** 演示数据筛选逻辑和后端 SQL 筛选逻辑是独立实现的，行为可能不一致；分面按 count 降序 + 中文拼音排序。

### [frontend/src/demoData.ts](frontend/src/demoData.ts)

**用途：** 4 条静态演示数据。覆盖 4 个主分类各一条，用于开发时预览 UI 和后端不可用时的降级展示。

### [frontend/src/pages/homeData.test.ts](frontend/src/pages/homeData.test.ts)

**用途：** `homeData.ts` 的单元测试。覆盖模式判断、筛选逻辑、分面构建。

---

## 20. 部署配置文件

### [docker-compose.yml](docker-compose.yml)（项目根目录）

**用途：** 生产部署编排。定义 3 个服务：db（Postgres 16）、backend（FastAPI）、frontend（nginx）。

**关键细节：**
- db 有 healthcheck（`pg_isready`），backend 依赖 db healthy 后才启动
- backend 通过 `DATABASE_URL` 环境变量连接 db 服务
- 数据持久化通过 named volume `pgdata`

### [backend/Dockerfile](backend/Dockerfile)

**用途：** 后端容器镜像。基于 `python:3.11-slim`，pip 安装依赖，尝试安装 Playwright（Chromium，用于 Scrapling 的 JS 渲染），失败不阻塞。

### [frontend/Dockerfile](frontend/Dockerfile)

**用途：** 前端容器镜像。多阶段构建：`node:20-alpine` 编译 → `nginx:alpine` 托管。

---

## 21. 数据流全景图

### 新闻动态流（News Stream）

```
                      ┌──────────────┐
                      │ seed_sources  │  YAML 定义 → sources 表
                      │   .yaml       │
                      └──────┬───────┘
                             │
                      ┌──────▼───────┐
                      │  Scheduler   │  APScheduler 定时触发
                      │  (可选)       │  或 startup backfill
                      └──────┬───────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
      ┌───────▼──────┐ ┌────▼─────┐ ┌──────▼──────┐
      │  RssFetcher  │ │PageMonit │ │SearchFetcher│
      │  (feedparser)│ │orFetcher │ │(search+LLM) │
      └───────┬──────┘ └────┬─────┘ └──────┬──────┘
              │              │              │
              └──────────────┼──────────────┘
                             │
                    ┌────────▼────────┐
                    │  Scrapling      │  提取网页正文
                    │  Extractor      │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │   Normalizer    │  去追踪参数 + 统一格式
                    │  (纯函数)        │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │   Dedup Check   │  url_hash → 已存在?
                    │  (Repository)   │
                    └───┬────────┬────┘
                        │        │
                   已存在     不存在
                        │        │
              ┌─────────▼──┐ ┌──▼──────────┐
              │ merge link │ │  Enricher    │  LLM 摘要 + 分类
              │  (追加引用) │ │  (LLM JSON)  │
              └────────────┘ └──┬──────────┘
                                │
                         ┌──────▼──────┐
                         │  Repository │  入库 (items + tags
                         │  .save       │  + entities + links)
                         │  _enriched   │
                         └─────────────┘
```

### 结构化事实流（Structured Stream）— V1 未实现

```
API Source → Adapter (parse) → Structured Repository (upsert) → DB
                                  ↑
                            不走 LLM，直接解析入库
```

### 前端数据流

```
User Action (搜索/筛选/翻页)
        │
        ▼
  HomePage (状态管理: filters + openId)
        │
        ├──▶ fetchItems(params)   ──▶ GET /items?category=...&q=...
        │         │                        │
        │         ▼                        ▼
        │    ItemList ◀─────── { total, items[] }
        │
        ├──▶ fetchFacets()      ──▶ GET /facets
        │         │                        │
        │         ▼                        ▼
        │    FacetSidebar ◀──── { main_category[], info_type[], importance[] }
        │
        └──▶ fetchItemDetail(id) ──▶ GET /items/{id}
                  │                        │
                  ▼                        ▼
             ItemDetail (overlay) ◀─── { ...完整字段 }
```

---

## 附录：审阅检查清单

### 后端审阅重点

- [ ] 环境变量和配置的默认值是否合理（[config.py](backend/app/config.py)）
- [ ] LLM 调用的缓存策略是否够用（[client.py](backend/app/llm/client.py)）
- [ ] JSON 提取正则的健壮性（[enricher.py](backend/app/processing/enricher.py)）
- [ ] URL 规范化的边界情况覆盖（[normalizer.py](backend/app/processing/normalizer.py)）
- [ ] 并发抓取时的资源竞争（[scheduler.py](backend/app/scheduler.py)）
- [ ] 数据库唯一约束与代码层面去重的双重保障（[models.py](backend/app/models.py) + [repository.py](backend/app/repository.py)）
- [ ] API 端点返回字段是否包含不应暴露的内部数据（[routes.py](backend/app/api/routes.py)）
- [ ] 结构化数据流模块的缺失（`structured/` 目录为空）

### 前端审阅重点

- [ ] API 错误时的降级策略是否合理（demo 模式切换）
- [ ] 演示数据与实时模式的行为一致性
- [ ] 组件 null/empty 状态覆盖是否完整
- [ ] TanStack Query 的 queryKey 设计是否会导致缓存冲突
- [ ] 详情面板的 overlay 交互（背景点击关闭、stopPropagation）
- [ ] 搜索输入是否会导致过度频繁的 API 请求（目前是 onChange 即时触发，无 debounce）
