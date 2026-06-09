# 技术新闻追踪 Agent —— 设计文档（V1 / MVP）

- 文档日期：2026-06-09
- 状态：待用户评审
- 项目代号：`os-news-tracker`

---

## 1. 背景与目标

为公司内部团队（maintainer、研发、决策者）做一个**技术新闻追踪 Agent**，自动从多类来源采集技术情报，用 LLM 整理归纳成统一的结构化总结，汇总到一个**可查询的网页**上，支持按分类、标签、实体、时间检索。

整体产品由多个相对独立的子系统组成，采用**迭代开发**，本文档只定义 **V1（MVP）** 的范围与设计。

### 关注的信息类型（初始 5 类主分类）

1. `OS跟踪来源` —— 主流 OS 的跟踪来源与更新
2. `友商产品信息` —— 友商产品发布地、发布信息
3. `软件包适配` —— 各类新兴软件包的适配情况
4. `OS性能发展` —— OS 性能发展情况
5. `司内AI工具` —— 公司内部 AI 工具相关信息

---

## 2. 关键前提（已与用户确认）

| 维度 | 结论 |
| --- | --- |
| 部署环境 | 公司**内网部署**，但**可正常访问外网**（抓取公开来源无需代理/白名单） |
| AI 能力 | 调用**司内统一 LLM 网关/API**；具体调用方式后续用 iWiki MCP 查文档确认。设计上抽象为「可配置的 OpenAI 兼容 / 自定义 endpoint」 |
| 规模与频率 | **几十个来源**，一天 1-2 次或一周 1-2 次拉取（采集侧无性能压力） |
| 订阅对象 | 用户自助订阅（轻量） + 管理员定向配置（→ V2 实现） |
| 登录/身份 | V1 **轻量化**：基于邮箱的订阅 + 管理员简单凭证，不引入复杂鉴权 |
| tag 体系 | 管理员维护**一级主分类** + AI 在每条信息上**自动抽取细粒度子标签** |
| 知识图谱 | V1 做**轻量结构化实体抽取 + 分面检索**，专用图数据库/语义检索留到后期 |
| 技术栈 | **Python 后端（FastAPI）+ 现代 JS 前端（React）** |
| 整体编排 | **方案 A：确定性流水线 + 局部轻量 LLM**（非自主 Agent 循环） |
| 采集/提取引擎 | **Scrapling 作默认引擎 + 可插拔接口**；Firecrawl 作后续可选项 |
| 搜索源 | 优先**司内联网能力**（待 iWiki MCP 确认）；否则 Firecrawl `/search` 或外部 API。接口可插拔 |

---

## 3. 迭代路线图（全局视角）

- **V1（本文档）**：采集（RSS + 固定页监控 + 关键词搜索）→ AI 处理（去重、归类、子标签、结构化实体、结构化摘要）→ 入库 → 可查询网页。
- **V2**：订阅体系（邮箱自助订阅 + 管理员定向配置）+ 邮件推送。
- **V3**：iWiki 联动（MCP 或写接口把摘要写到 iWiki）。
- **V4**：采集源管理后台、抓取健康度监控、AI 成本/质量看板。
- **V5**：语义检索、关系图谱、推送个性化、去重/聚类优化。

> V1 明确**不做**：邮件/iWiki 推送、订阅体系、复杂登录鉴权、采集源管理后台、语义搜索、关系图谱。

---

## 4. 总体架构（V1）

```
┌─────────────────────────────────────────────────────────────┐
│                      调度器 (APScheduler)                      │
│              每天/每周触发，按 source 配置的频率                 │
└───────────────────────────┬─────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  采集层 (可插拔 Fetcher)                                       │
│  ├─ RssFetcher          订阅 RSS/Atom                          │
│  ├─ PageMonitorFetcher  固定页抓取 + 变更检测                   │
│  └─ SearchFetcher       关键词搜索 → 候选链接 → 抓正文          │
│        ↑ 依赖可插拔的 ContentExtractor / SearchProvider        │
└───────────────────────────┬─────────────────────────────────┘
                            ↓  RawItem (统一结构)
┌─────────────────────────────────────────────────────────────┐
│  处理层                                                        │
│  ├─ Normalizer    清洗正文 / 提取标题、时间 / URL 规范化        │
│  ├─ Deduplicator  URL 指纹 + 内容 simhash 去重（含跨源合并）   │
│  └─ Enricher(LLM) 归类 + 子标签 + 结构化实体 + 结构化摘要       │
└───────────────────────────┬─────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  存储 (Postgres)  sources / items / tags / item_tags /        │
│                   entities / item_entities                    │
└───────────────────────────┬─────────────────────────────────┘
                            ↓
┌──────────────────────┐        ┌──────────────────────────────┐
│  FastAPI (查询/分面)  │ ←───→  │  React 前端 (可查询、分面筛选) │
└──────────────────────┘        └──────────────────────────────┘
```

---

## 5. 组件设计

每个组件单一职责、通过明确接口通信、可独立测试。

### 5.1 Source Registry（采集源注册）

- 维护所有采集源的配置：类型、URL、抓取频率、关键词、所属主分类候选、启用状态、健康状态。
- V1 用**配置文件 + 数据库表**双轨（配置文件做初始 seed，运行态读库）；可视化管理后台留到 V4。

### 5.2 Fetcher（可插拔采集器）

统一接口：

```python
class Fetcher(Protocol):
    def fetch(self, source: Source) -> list[RawItem]: ...
```

三种实现互不依赖：

- **RssFetcher**：解析 RSS/Atom（如 `feedparser`），产出条目。
- **PageMonitorFetcher**：抓取已知固定页，做变更检测（对比上次内容指纹），有新内容才产出。依赖 `ContentExtractor`。利用 Scrapling 的**自适应解析**应对页面改版。
- **SearchFetcher**：用关键词调 `SearchProvider` 拿候选链接 → 对每个链接用 `ContentExtractor` 抓正文 → 嵌入**轻量 LLM 判定相关性**后产出（这是 V1 唯一的局部 agentic 环节，仍受控）。

### 5.3 ContentExtractor（可插拔正文提取引擎）

```python
class ContentExtractor(Protocol):
    def extract(self, url: str) -> ExtractedDoc: ...   # 干净正文 + 元数据
```

- **V1 默认实现：`ScraplingExtractor`** —— 轻量库、零额外服务、自适应解析、强反爬（StealthyFetcher 可过 Cloudflare）。
- **可选实现：`FirecrawlExtractor`** —— 后续需要高质量 LLM-ready markdown 时启用。注意 Firecrawl 为 **AGPL-3.0**，且自部署需 Docker + Redis + Playwright，引入前需走法务确认。

### 5.4 SearchProvider（可插拔搜索源）

```python
class SearchProvider(Protocol):
    def search(self, query: str) -> list[SearchResult]: ...   # 链接 + 摘要
```

- **优先：`InternalGatewaySearch`** —— 若司内 LLM 网关自带联网搜索能力（待 iWiki MCP 确认），最优（合规、免费、走内部通道）。
- **备选：`FirecrawlSearch`**（自部署配 SearXNG）或外部 API（Tavily / Google Programmable Search）。

### 5.5 Normalizer

- 把各来源的杂乱字段统一成干净正文 + 标准元数据（标题、发布时间、规范化 URL）。
- URL 规范化（去 utm、统一协议/末尾斜杠）用于去重。

### 5.6 Deduplicator

- 一级：规范化 URL 指纹（`url_hash`）精确去重。
- 二级：内容 `simhash` 近似去重，识别同一新闻的多源命中。
- **跨源合并展示策略**：同一新闻多源命中时**合并为一条** item，详情页列出「多个来源链接」，避免列表刷屏。

### 5.7 Enricher（唯一的 LLM 环节）

- 一次 LLM 调用同时产出：主分类、细粒度子标签、结构化实体、**结构化摘要**（见第 7 节）。
- 输出带 `llm_confidence`；低置信度条目入库但标记待人工复核。
- 结果按 `content_hash` **缓存**，重跑不重复花钱。

### 5.8 API + 前端

- FastAPI 提供分面检索 + 全文搜索 + 详情接口。
- React 前端：列表页（快速扫读）+ 详情页（结构化展示）+ 分面筛选侧栏。

---

## 6. 数据模型（Postgres，关键表）

```sql
sources(
  id, name, type,            -- type: rss | page_monitor | search
  url, keywords,             -- search 类型用 keywords
  fetch_cron,                -- 抓取频率
  main_category,             -- 候选主分类
  enabled, last_run_at,
  health_status, fail_count,
  last_content_hash          -- page_monitor 变更检测用
)

items(
  id, source_id,
  title, url, url_hash, content_hash,
  raw_content, clean_content,
  published_at, fetched_at,
  main_category,
  -- 结构化摘要字段（见第 7 节）
  title_tldr, summary, key_points,   -- key_points: jsonb 数组
  info_type, importance, why_it_matters,
  status,                    -- new | enriched | enrich_failed | needs_review
  llm_confidence
)

item_sources(item_id, source_id, url)   -- 跨源合并：一条 item 对应多个来源链接

tags(id, name, kind)                     -- kind: main_category | sub_tag
item_tags(item_id, tag_id)

entities(id, type, name)                 -- type: vendor|product|os|package|version|topic
item_entities(item_id, entity_id, role)
```

- 全文检索：Postgres `tsvector` + GIN 索引（V1 足够；语义检索留 V5）。
- 分面字段（main_category、info_type、importance、published_at、entity）建索引。

---

## 7. 信息总结标准结构（已与用户确认）

每条信息由 Enricher 一次产出固定结构化字段，前端/未来推送统一复用：

| 字段 | 说明 |
| --- | --- |
| `title_tldr` | 一句话概括（≤30 字，列表页直接看） |
| `summary` | 核心摘要（2-4 句，说清发生了什么） |
| `key_points` | 关键点（3-5 条 bullet，存 jsonb 数组） |
| `info_type` | 信息类型：`发布` \| `更新` \| `性能数据` \| `适配` \| `观点/分析` \| `其他` |
| `importance` | 重要度：`高` \| `中` \| `低`（给 V2 推送排序/过滤用） |
| `why_it_matters` | 影响/意义（1-2 句，面向 maintainer 的"所以呢"） |
| `entities` | 结构化实体：厂商/产品/OS/软件包/版本/主题 |
| `main_category` | 主分类（5 类之一） |
| `sub_tags` | 细粒度子标签 |
| `source` / `url` / `published_at` / `llm_confidence` | 元数据 |

### 前端展示要求

- **列表页**：`title_tldr` + `info_type` + `importance` + 主分类，一行扫读。
- **详情页**：展开 `summary` + `key_points` + `why_it_matters` + 实体 + 多来源链接。
- **每个区块在视觉上必须做区分**：例如 `key_points` 用列表卡片、`importance` 用色彩标签（高=红/橙、中=黄、低=灰）、`why_it_matters` 用强调块、`info_type` 用 badge。

---

## 8. 数据流（一次采集，端到端）

1. 调度器按各 source 的 `fetch_cron` 触发。
2. 对应 Fetcher 拉取 → 产出 `RawItem`。
3. Normalizer 清洗 → 标准化元数据。
4. Deduplicator 判重：已存在 URL → 追加到 `item_sources`（跨源合并）；全新 → 进入下一步。
5. Enricher 调司内 LLM 网关，产出结构化摘要 + 分类 + 实体（命中缓存则跳过）。
6. 写入 `items` / `tags` / `entities` 等表。
7. 前端即可查询展示。

**幂等保证**：同一条目重复跑不产生重复记录或重复 LLM 调用（依赖 `url_hash` / `content_hash`）。

---

## 9. 错误处理与稳健性

- 单个源失败**隔离**，不影响其他源；累积 `fail_count`，更新 `health_status`。
- LLM 调用：超时/限流自动重试（指数退避）；多次失败标 `status=enrich_failed`，下轮补处理，不阻塞展示。
- 抓取**合规**：只抓公开信息、遵守 `robots`、每站限速 + 合理 UA；友商页面按天级低频抓取，不做激进爬取。
- 所有外部调用设超时；pipeline 有整体看护日志。

---

## 10. 高性能策略（贴合规模）

- 采集侧用 `asyncio` 并发抓取（几十个源秒级完成），**不引入** Celery/队列等重组件（YAGNI）。
- LLM 调用做**批处理 + 并发上限**控制成本与限流。
- 查询侧性能靠**索引设计**：分面字段索引、全文 GIN 索引、分页游标——这是「高性能」体感的关键。
- LLM 结果按 `content_hash` **缓存**，重跑不重复花钱。

---

## 11. 测试策略

- **Fetcher**：本地 fixture（保存的 RSS/HTML 样本）单测，不依赖外网。
- **Normalizer / Deduplicator**：纯函数，覆盖边界（URL 规范化、simhash 近似）。
- **Enricher**：mock LLM 网关，校验 prompt 构造与结构化输出解析。
- **API**：集成测试 + 一条端到端 smoke（fixture → 入库 → 查询）。

---

## 12. 部署形态（V1）

- 单机 **Docker Compose**：FastAPI + Postgres + 前端静态资源 + 调度进程。
- 数据保留：历史**永久留存**（追踪需要时间线），不自动清理；大字段（`raw_content`）可后续归档。
- 后续要上内部发布平台再迁移。

---

## 13. 待办/待确认（不阻塞 V1 设计，落地时处理）

- [ ] 用 iWiki MCP 查清司内 LLM 网关的调用方式（endpoint、鉴权、是否带联网搜索）。
- [ ] 确认外部搜索 API（若内部无联网能力）的可用性与合规。
- [ ] 确认 Firecrawl AGPL-3.0 在公司内部自用的法务结论（仅在引入 Firecrawl 时需要）。
- [ ] 初始采集源清单（各主分类下具体抓哪些站点/RSS）。
