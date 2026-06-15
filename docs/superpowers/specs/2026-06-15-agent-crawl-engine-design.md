# Agent 驱动采集引擎设计文档

**日期：** 2026-06-15
**状态：** 待用户评审
**项目：** `os-news-tracker` V1 采集层升级

---

## 1. 背景与目标

V1 系统的采集层依赖预先配置的固定来源（RSS feeds、已知页面 URL、结构化 API），对于：

- 用户临时想关注的任意网站（结构未知、子页面动态变化）
- 需要深入子页面才能获取完整内容的场景
- 内容质量参差不齐需要动态筛选的来源

现有 Fetcher 类型（rss/page_monitor/search）能力有限。本设计引入基于 Agent 范式的智能采集引擎，作为新的 `agent_crawl` 源类型，**与现有 RSS/API fetcher 并列运行，不取代已有来源**。

### 关键特性

1. **AI 驱动的 URL 规划** — 自动分析网站结构，制定抓取计划
2. **配置化爬取深度** — 默认一层深度（列表页 → 文章详情），可设为 2-3 层
3. **质量评估 + 记忆** — 每页质量打分，网站/页面级缓存，下次直接跳过低质量内容
4. **用户主题分组** — 可选设置大类标签（如「项目动态」「技术迭代」），agent 自动归类；不设置则逐条列出
5. **自适应摘要** — 根据页面实际内容类型（发布说明/基准测试/文章/讨论）灵活组织输出，不套固定模板

---

## 2. Agent 范式设计

### 设计原则对照（参考 `agent-structure-design.md`）

实际生产推荐组合：**Pre-Act（规划）+ DAG（并行执行）+ Generator-Critic（质量循环）+ 随机-确定性边界（安全护栏）**

| 阶段 | 所用范式 | 并行能力 | 说明 |
|------|---------|---------|------|
| PlanAgent | **Pre-Act + DFSDT** | 单次 | 先一次性生成完整抓取计划（URL 列表）；若主页抓取失败可回溯换入口策略 |
| CrawlDAG | **DAG 并行** | ✅ 多 worker | URL 列表转为 DAG 并发任务，独立页面互不依赖，默认 5 workers |
| QualityWorkerPool | **DAG 并行 + Critic** | ✅ 多 worker | 每页独立打分，并发执行 LLM 调用，默认 3 workers；输出写入 SiteMemory |
| SummaryWorkerPool | **DAG 并行 + Generator** | ✅ 多 worker | 对通过质量门的页面并发生成自适应摘要，默认 3 workers |
| SiteMemory | **Reflexion** | — | 历史评估结果存入记忆，指导下次 PlanAgent 跳过低质量 URL pattern |
| 整体边界 | **随机-确定性边界** | — | CrawlDAG 是纯确定性护栏；LLM 只影响 Plan 与 Assess 两个离散阶段 |

> **并行设计原则**：每个 item（URL/页面）在 CrawlDAG、QualityWorkerPool、SummaryWorkerPool 三个阶段均可独立并发处理。Worker 数量通过 `agent_source_configs` 独立配置，慢的阶段可设置更多 workers。

### 数据流图（全流水线并行）

```
用户配置 AgentSource
（root_url + focus_areas + topic_groups + depth + quality_threshold + worker counts）
                │
         ┌──────▼───────┐
         │  PlanAgent   │  Pre-Act: 分析网站结构 → 输出确定性 URL 列表
         │  (LLM × 1)   │  参考 SiteMemory 跳过已知低质 URL
         └──────┬───────┘
                │  CrawlPlan（URL[] + 推测主题）
         ┌──────▼──────────────────────────────────────────┐
         │              CrawlDAG（并行 × N workers）         │
         │  w1: fetch(url_1) ─┐                             │
         │  w2: fetch(url_2) ─┤ asyncio.gather → RawPage[] │  无 LLM，纯 I/O
         │  w3: fetch(url_3) ─┤                             │  默认 5 workers
         │  ...               ─┘                             │
         └──────┬──────────────────────────────────────────┘
                │  RawPage[]
         ┌──────▼──────────────────────────────────────────┐
         │        QualityWorkerPool（并行 × M workers）      │
         │  w1: assess(page_1) ─┐                           │
         │  w2: assess(page_2) ─┤  → QualifiedPage[]        │  LLM × M 并发
         │  w3: assess(page_3) ─┘  + 写入 SiteMemory        │  默认 3 workers
         └──────┬──────────────────────────────────────────┘
                │  QualifiedPage[]（通过质量筛选）
         ┌──────▼──────────────────────────────────────────┐
         │        SummaryWorkerPool（并行 × K workers）      │
         │  w1: summarize(page_a) ─┐                        │
         │  w2: summarize(page_b) ─┤  → AgentItem[]         │  LLM × K 并发
         │  w3: summarize(page_c) ─┘  按 topic_groups 归类  │  默认 3 workers
         └──────┬──────────────────────────────────────────┘
                │  AgentItem[]
         进入现有 Deduplicator → items 表
```

每个 worker 池的并发数独立配置，针对实际瓶颈调整（网络 I/O 密集 → 提高 CrawlDAG workers；LLM 吞吐有限 → 降低 Quality/Summary workers）。

---

## 3. 组件详细设计

### 3.1 PlanAgent

**职责**：给定网站根 URL 和用户关注点，生成一份确定性的抓取计划。

**执行步骤：**

1. 用 Scrapling 抓取 `root_url` 的 HTML（纯确定性，无 LLM）
2. 查询 `agent_site_memory` 获取该 source 的历史质量记录
3. 发一次 LLM 调用（CoT 推理）：

```
你是一个网页内容分析助手。给定以下网页的链接列表和用户的关注点，
识别哪些链接最可能包含用户感兴趣的内容，并按优先级排序。

用户关注点：{focus_areas}
页面中发现的链接：{links_json}
已知低质量 URL pattern（跳过）：{skip_patterns}
最多返回：{max_urls} 个

只输出 JSON：{"urls": [{"url": "...", "guessed_topic": "..."}]}
```

4. 对 LLM 输出做**确定性校验**（随机-确定性边界）：
   - 有效 URL 格式
   - 同域名或已知子域名（防止 LLM 幻觉跨站链接）
   - 数量不超过 `max_urls`
   - 去掉 `agent_site_memory` 中 `verdict=discard, seen_count≥2` 的 URL

**输出：** `CrawlPlan(urls: list[PlanUrl], source_id: int)`

**失败策略（DFSDT）**：
- 若 `root_url` 返回 403/Cloudflare，尝试切换 `StealthyFetcher`
- 若抓到链接数为 0，重试一次并扩大链接选择策略
- 两次失败后标记 run 为 `plan_failed`，跳过本轮

---

### 3.2 CrawlDAG（并行抓取 Worker Pool）

**职责**：按 CrawlPlan 并发抓取所有 URL，返回原始页面内容。纯 I/O，无 LLM。

**Worker Pool 实现：**

```python
async def execute_crawl(plan: CrawlPlan, config: AgentSourceConfig) -> list[RawPage]:
    sem = asyncio.Semaphore(config.crawl_workers)   # 默认 5，可配置
    async def fetch_one(pu: PlanUrl) -> RawPage | None:
        async with sem:
            await asyncio.sleep(random.uniform(0.5, 2.0))  # 礼貌延迟
            try:
                content = await scrapling_fetch(pu.url)
                return RawPage(url=pu.url, guessed_topic=pu.guessed_topic, content=content)
            except Exception as e:
                logger.warning(f"fetch failed: {pu.url}: {e}")
                return None
    results = await asyncio.gather(*[fetch_one(pu) for pu in plan.urls])
    return [r for r in results if r is not None]
```

- 单页失败 → 记录日志，继续其他页面（不中止整批）
- `crawl_workers`（默认 5）：网络 I/O 密集可设到 10-20，反爬敏感网站降到 2-3

---

### 3.3 QualityWorkerPool（并行 Critic）

**职责**：对每个抓取到的页面独立评估内容质量与相关性，多 worker 并发执行 LLM 调用。

**LLM Prompt：**

```
你是内容质量评估员。评估以下页面内容对用户的价值。

用户关注点：{focus_areas}
页面 URL：{url}
页面标题：{title}
页面正文（前1500字）：{content_preview}

评估维度：
1. 与用户关注点的相关性（0-5）
2. 信息密度（是否包含具体的事实/数据/版本号/技术细节，0-5）

输出 JSON：
{
  "score": 7,
  "reason": "...",
  "relevant_topic": "内核性能",
  "verdict": "keep",   // keep | discard
  "should_remember": true
}
```

**Worker Pool 并发实现：**

```python
async def assess_all(pages: list[RawPage], config: AgentSourceConfig) -> list[QualifiedPage]:
    sem = asyncio.Semaphore(config.quality_workers)   # 默认 3，可配置
    async def assess_one(page: RawPage) -> QualifiedPage | None:
        async with sem:
            # SiteMemory 命中 → 跳过 LLM
            cached = memory.get(config.source_id, page.url)
            if cached:
                return QualifiedPage(page=page, verdict=cached.verdict, score=cached.quality_score)
            result = await llm_assess(page, config)    # LLM 调用
            memory.upsert(config.source_id, page.url, result)
            return QualifiedPage(page=page, verdict=result.verdict, score=result.score) if result.verdict == "keep" else None
    results = await asyncio.gather(*[assess_one(p) for p in pages])
    return [r for r in results if r is not None]
```

**后处理（确定性规则）：**

- `score < quality_threshold`（用户配置，默认 4）→ 强制 `verdict=discard`
- `should_remember=true` 或 `verdict=discard, seen_count≥1` → 写入 `agent_site_memory`
- SiteMemory 命中 → 直接复用历史 `verdict`，跳过 LLM 调用
- `quality_workers`（默认 3）：受 LLM 网关并发限制约束，按实际限速调整

---

### 3.4 SummaryWorkerPool（并行 Generator，自适应摘要）

**职责**：对通过质量筛选的页面并发生成摘要，格式根据内容类型自适应，支持主题分组归类。

**LLM Prompt：**

```
你是技术内容整理助手。阅读以下页面，提取对 OS maintainer 有价值的信息。

用户关注点：{focus_areas}
用户主题分组（从中选一个最匹配的，若无则留空）：{topic_groups}
页面 URL：{url}
页面正文：{content}

根据页面实际内容类型，选择最合适的输出格式，输出 JSON：

{
  "title": "...",
  "topic_group": "项目动态",   // 若 topic_groups 为空则 null
  "content_type": "release_note",  // article | release_note | benchmark | discussion | changelog
  "importance": "高",         // 高 | 中 | 低
  "body": "...",               // 正文摘要，格式依 content_type：
                               //   article → 2-4句核心摘要
                               //   release_note → 关键变更列表（markdown bullet）
                               //   benchmark → 主要数据点（如"比上版本快 12%"）
                               //   discussion → 核心观点 + 争议点
                               //   changelog → 精简变更记录
  "key_facts": ["..."],        // 1-3条最重要事实（可选）
  "source_url": "{url}"
}
```

**Worker Pool 并发实现：**

```python
async def summarize_all(pages: list[QualifiedPage], config: AgentSourceConfig) -> list[AgentItem]:
    sem = asyncio.Semaphore(config.summary_workers)   # 默认 3，可配置
    async def summarize_one(qp: QualifiedPage) -> AgentItem:
        async with sem:
            return await llm_summarize(qp, config)
    results = await asyncio.gather(*[summarize_one(p) for p in pages])
    return [r for r in results if r is not None]
```

`summary_workers`（默认 3）：可独立于 `quality_workers` 配置，汇总阶段 prompt 更长，适当减少并发。

**映射到现有 DB 字段（零 schema 变更）：**

| SummaryAgent 字段 | → DB 字段 | 说明 |
|-------------------|----------|------|
| `title` | `items.title_tldr` | 用中文或英文标题 |
| `body` | `items.summary` | 自适应正文 |
| `key_facts` | `items.key_points` | JSONB 数组 |
| `importance` | `items.importance` | 高/中/低 |
| `topic_group` | `items.main_category` | 用用户的分组名，无分组时用 `agent_crawl` |
| `content_type` | 存入 `items.key_points` 第一个元素的元数据前缀 | 前端据此选择渲染模板 |

---

### 3.5 SiteMemory 数据模型

```sql
agent_site_memory(
  id              SERIAL PRIMARY KEY,
  source_id       INT REFERENCES sources(id) ON DELETE CASCADE,
  url_pattern     VARCHAR NOT NULL,    -- 具体 URL 或 URL 前缀
  quality_score   INT,                 -- 0-10
  quality_reason  TEXT,
  verdict         VARCHAR NOT NULL,    -- keep | discard
  relevant_topic  VARCHAR,
  last_seen_at    TIMESTAMPTZ DEFAULT NOW(),
  seen_count      INT DEFAULT 1,
  UNIQUE(source_id, url_pattern)
)
```

**使用规则：**
- `verdict=discard, seen_count >= 2` → PlanAgent 直接跳过，不进入 CrawlDAG
- `verdict=keep` → QualityAgent 直接复用分数，跳过 LLM 评估调用（有效期 7 天；超过 7 天后重新评估，内容可能已更新）
- `verdict=discard` → 永久有效（低质量站点极少逆转），管理员可在 SiteMemory 管理页手动删除
- 每次见到同一 URL → `seen_count++, last_seen_at=NOW()`

---

## 4. 数据模型变更汇总

### 新增表

```sql
-- Agent 源配置（扩展现有 sources 表的配置细节）
agent_source_configs(
  source_id         INT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
  focus_areas       JSONB NOT NULL DEFAULT '[]',    -- ["内核性能", "调度优化"]
  topic_groups      JSONB NOT NULL DEFAULT '[]',    -- ["项目动态", "技术迭代"]，空=不分组
  crawl_depth       INT NOT NULL DEFAULT 1,
  max_urls_per_run  INT NOT NULL DEFAULT 20,
  quality_threshold INT NOT NULL DEFAULT 4,         -- 0-10，低于此丢弃
  -- 各阶段 Worker 数量（独立可调）
  crawl_workers     INT NOT NULL DEFAULT 5,         -- I/O 密集，可适当调高
  quality_workers   INT NOT NULL DEFAULT 3,         -- LLM 并发，受网关速率限制
  summary_workers   INT NOT NULL DEFAULT 3,         -- LLM 并发，prompt 更长酌情调低
  updated_at        TIMESTAMPTZ DEFAULT NOW()
)

-- Site Memory
agent_site_memory(...)  -- 见 3.5 节

-- 运行日志
agent_crawl_runs(
  id                SERIAL PRIMARY KEY,
  source_id         INT REFERENCES sources(id),
  started_at        TIMESTAMPTZ DEFAULT NOW(),
  completed_at      TIMESTAMPTZ,
  plan_urls_count   INT DEFAULT 0,
  fetched_count     INT DEFAULT 0,
  quality_passed    INT DEFAULT 0,
  items_created     INT DEFAULT 0,
  status            VARCHAR DEFAULT 'running',    -- running | completed | failed | plan_failed
  error_message     TEXT
)
```

### 现有表变更（最小化）

```sql
-- sources.type 枚举新增 'agent_crawl'
-- items.status 新增 'agent_enriched'（agent 产出的 item 不走 Enricher，直接标此状态）
-- 无其他 migration 破坏性变更
```

---

## 5. 模块文件结构

```
backend/app/
├── fetchers/
│   ├── base.py              （现有，不变）
│   ├── rss.py               （不变）
│   ├── page_monitor.py      （不变）
│   ├── search.py            （不变）
│   └── agent_crawl.py       ← 新增：AgentCrawlFetcher（实现 Fetcher Protocol，编排 4 个子模块）
├── agent/                   ← 新增目录
│   ├── __init__.py
│   ├── plan_agent.py        ← PlanAgent（Pre-Act + DFSDT）
│   ├── crawl_dag.py         ← CrawlDAG（asyncio Semaphore，crawl_workers 并发）
│   ├── quality_pool.py      ← QualityWorkerPool（Critic，quality_workers 并发 LLM）
│   ├── summary_pool.py      ← SummaryWorkerPool（Generator，summary_workers 并发 LLM）
│   └── site_memory.py       ← SiteMemory 读写接口（DB + 内存缓存）
└── models.py                （新增 AgentSourceConfig, AgentSiteMemory, AgentCrawlRun ORM）
```

`AgentCrawlFetcher` 实现 `Fetcher` Protocol，内部编排上述四个 Agent，外部调用方式与其他 Fetcher 完全一致：

```python
class AgentCrawlFetcher:
    def fetch(self, source: Source) -> list[RawItem]:
        # 同步入口，内部用 asyncio.run() 驱动全流水线（三阶段均并行）
        config = self.repo.get_agent_config(source.id)

        async def _pipeline() -> list[RawItem]:
            # Step 1: PlanAgent（单次 LLM，生成确定性 URL 列表）
            plan = await self.plan_agent.plan_async(source, config)
            # Step 2: CrawlDAG（并行 I/O，crawl_workers 并发）
            pages = await self.crawl_dag.execute(plan, config)
            # Step 3: QualityWorkerPool（并行 LLM，quality_workers 并发）
            qualified = await self.quality_pool.assess_all(pages, config)
            # Step 4: SummaryWorkerPool（并行 LLM，summary_workers 并发）
            return await self.summary_pool.summarize_all(qualified, config)

        return asyncio.run(_pipeline())
```

> **Async 边界说明**：整个 pipeline 在一次 `asyncio.run()` 内完成，三个并行阶段共享同一事件循环，无嵌套 event loop 问题。若未来 Pipeline 整体升级为 async，直接改为 `await self.agent_crawler.fetch_async(source)` 即可。

> **注意（Async 边界）**：现有 `Fetcher` Protocol 是同步的。`AgentCrawlFetcher` 在同步上下文中用 `asyncio.run()` 包装异步 CrawlDAG。若将来 Pipeline 整体迁移为 async，可同步升级 `AgentCrawlFetcher.fetch()` 为 `async def fetch_async()`。

---

## 6. API 端点（新增）

用户通过 UI 添加/管理 `agent_crawl` 类型来源（不通过 seed_sources.yaml，后者只用于系统预设来源）：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/sources/agent` | 列出当前用户添加的所有 agent sources |
| POST | `/sources/agent` | 新增 agent source（body: root_url + focus_areas + topic_groups + depth 等配置） |
| PUT | `/sources/agent/{id}` | 更新配置 |
| DELETE | `/sources/agent/{id}` | 删除 source + 关联的 site_memory |
| POST | `/sources/agent/{id}/run` | 立即触发一次采集（返回 run_id，前端轮询）|
| GET | `/sources/agent/{id}/runs` | 查看运行历史 |
| GET | `/sources/agent/{id}/memory` | 查看 SiteMemory 记录（管理员）|
| DELETE | `/sources/agent/{id}/memory/{url_pattern}` | 手动删除某条 memory 记录（管理员）|

---

## 7. 前端变更

### 7.1 Agent 源配置（新增/扩展源管理页面）

用户可在来源管理处添加 `agent_crawl` 类型的新来源，填写：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| 网站 URL | 入口页（列表页/博客首页） | — |
| 关注点标签 | 多标签输入，告诉 agent 关注什么（如 "eBPF, 调度器"） | — |
| 主题分组 | 可选，结果展示的分组标签（如 "项目动态, 技术迭代"） | 空（逐条列出） |
| 爬取深度 | 1 / 2 / 3 | 1 |
| 每次最多 URL | 5 / 10 / 20 / 50 | 20 |
| 质量筛选阈值 | 0-10 滑块 | 4 |
| ▼ 高级设置（可折叠）| | |
| 抓取并发数 | CrawlDAG worker 数（I/O 密集，可设较高） | 5 |
| 质量评估并发数 | QualityPool LLM worker 数 | 3 |
| 摘要生成并发数 | SummaryPool LLM worker 数 | 3 |

### 7.2 Feed 展示调整

- `topic_group` 不为空时：列表中相同分组的 items 在时间线旁显示分组标题标签
- `content_type = benchmark` 时：ItemDetail 的 `key_facts` 以 `<table>` 而非 `<ul>` 渲染
- 来源徽章：RSS 来源显示来源名；`agent_crawl` 来源显示「🤖 智能采集」标签 + 网站域名

### 7.3 SiteMemory 查看（可选，管理员页面）

管理员可查看某个 agent source 的 memory 记录，了解哪些 URL 被持续丢弃（可手动覆盖 verdict）。

---

## 8. 测试策略

| 类型 | 覆盖 |
|------|------|
| PlanAgent 单元测试 | mock Scrapling + mock LLM，验证确定性校验逻辑（域名检查、数量上限、skip_patterns 过滤）|
| QualityAgent 单元测试 | mock LLM，验证 threshold 过滤、SiteMemory 写入、缓存复用逻辑 |
| SummaryAgent 单元测试 | mock LLM，验证各 content_type 到 DB 字段的映射 |
| CrawlDAG 单元测试 | mock Scrapling，验证并发上限、单页失败不影响其他页面 |
| AgentCrawlFetcher 集成测试 | fixture 页面（本地 HTML 样本） + mock LLM，端到端验证 `fetch()` 输出格式与现有 NormalizedItem 兼容 |

---

## 9. 待确认项

- [ ] LLM 调用成本估算：一次 agent_crawl 运行（20 URLs）≈ PlanAgent 1次 + QualityAgent ≤20次（SiteMemory 命中则跳过）+ SummaryAgent ≤20次，共约 41 次 LLM 调用（冷启动）。随着 SiteMemory 积累，实际调用次数会下降。需确认内网 LLM 网关的并发/速率限制。
- [ ] CrawlDAG 的 `concurrent_limit` 默认值 5 是否足够保守（取决于目标网站的反爬策略，Scrapling StealthyFetcher 可应对大多数 Cloudflare 场景）。
- [ ] `agent_crawl` 来源是否纳入现有定时调度器（APScheduler）的 cron 体系。**建议纳入**，以 `fetch_cron: "0 6 * * *"`（每日一次）为默认频率，用户可在前端配置中修改。
