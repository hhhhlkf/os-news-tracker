# OS News Tracker V2 综合设计文档

**日期：** 2026-06-15
**状态：** 待用户评审
**项目：** `os-news-tracker` — 采集层升级 + 用户个性化 + 趋势总结

> 本文档合并了以下两份设计：
> - `2026-06-15-personalization-scoring-digest-design.md`（个性化打分 / 用户系统 / Digest）
> - `2026-06-15-agent-crawl-engine-design.md`（Agent 驱动采集引擎）
>
> 两者冲突时以 **agent-crawl** 文档为准。

---

## 1. 背景与目标

在 V1 系统（新闻采集 + LLM 结构化入库 + 可检索 React 前端）的基础上，本版本新增四个相互联动的子系统：

| 子系统 | 核心功能 |
|--------|---------|
| Agent 采集引擎 | 新增 `agent_crawl` 源类型：AI 驱动的 URL 规划、全流水线并行抓取、质量评估 + SiteMemory |
| 用户系统 | 注册/登录（JWT）、角色（user/admin） |
| 个性化引擎 | 自定义评分准则 + AI 打分（快速/LLM 双通道） |
| 趋势总结（Digest）| Digest 生成（按需 + 定期）+ 热点 / 新兴趋势分析 |

### 明确不做（保持聚焦）

- 邮件推送（V3）
- iWiki 联动（V3）
- 用户之间共享/协作偏好
- 复杂 RBAC（只有 `user` / `admin` 两级）
- 实时 WebSocket 推送
- 日志持久化入库（见独立的实时日志面板设计文档）

---

## 2. 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                           V1 现有系统                            │
│   RSS/API/PageMonitor/Search Fetcher → Pipeline → items 入库    │
└───────────────────┬─────────────────────────────────────────────┘
                    │
         ┌──────────▼──────────┐
         │   Agent 采集引擎    │  新增 agent_crawl 源类型
         │  PlanAgent          │  Pre-Act + DFSDT
         │  CrawlDAG           │  并行 I/O（5 workers）
         │  QualityWorkerPool  │  并行 LLM Critic（3 workers）
         │  SummaryWorkerPool  │  并行 LLM Generator（3 workers）
         │  SiteMemory         │  Reflexion 记忆机制
         └──────────┬──────────┘
                    │  agent_enriched items
         ┌──────────▼──────────┐
         │    User 子系统      │  注册/登录，JWT，角色
         └──────────┬──────────┘
                    │
         ┌──────────▼──────────────────────┐
         │       个性化打分引擎            │
         │  用户 Scoring Criteria (JSONB)  │  自定义准则列表
         │  快速通道：规则匹配算分          │  查询时实时，无 LLM
         │  精打通道：LLM ScoringAgent     │  后台批量，可选开启
         └──────────┬──────────────────────┘
                    │
         ┌──────────▼──────────────────────┐
         │        Digest 子系统            │
         │  DigestAgent（多步 Chain）      │  聚合 → 主题识别 → 综合
         │  按需（个性化）+ 定期（全局）   │
         └─────────────────────────────────┘
```

---

## 3. 用户系统

### 3.1 认证机制

- **方案**：邮箱 + 密码，`bcrypt` 存密码哈希，**JWT（无状态 token）**
- **Token 有效期**：7 天，前端存 `localStorage`
- **角色**：`user`（默认）/ `admin`（可管理采集源、查看所有用户、配置定时 Digest、查看 SiteMemory）
- **注册方式**：开放自助注册，无审批流程

### 3.2 数据模型

```sql
users(
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email        VARCHAR UNIQUE NOT NULL,
  password_hash VARCHAR NOT NULL,
  display_name VARCHAR,
  role         VARCHAR DEFAULT 'user',    -- user | admin
  is_active    BOOLEAN DEFAULT TRUE,
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  last_login_at TIMESTAMPTZ
)
```

### 3.3 用户 Profile 初始化

用户成功注册后，系统自动创建一条空的 `user_profiles` 行（`criteria=[]`，`enable_llm_scoring=false`，`min_score_threshold=25`）。

### 3.4 API 端点

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| POST | `/auth/register` | 注册（邮箱+密码），同时创建空 user_profiles | 无 |
| POST | `/auth/login` | 登录，返回 JWT | 无 |
| GET | `/auth/me` | 当前用户信息 | 需登录 |
| PUT | `/auth/me` | 更新 display_name 等 | 需登录 |

### 3.5 鉴权中间件

- 所有 `/items`、`/facets`、`/digest`、`/users/me/*`、`/sources/agent` 端点需要有效 JWT
- `Authorization: Bearer <token>` header
- 无效/过期 token → 401，前端重定向 `/login`
- 管理员专属路由 `/admin/*` → 403 for non-admin

---

## 4. Agent 驱动采集引擎

现有 RSS/API/page_monitor 采集器保持不变，新增 `agent_crawl` 源类型作为智能补充，处理用户自由添加的任意网站。

### 4.1 Agent 范式设计（参考 `agent-structure-design.md`）

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

### 4.2 数据流图（全流水线并行）

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
                │  AgentItem[]（status=agent_enriched，不走 Enricher）
         进入现有 Deduplicator → items 表
```

> **关键**：agent 产出的 item 标记 `status=agent_enriched`，**不经过现有 Enricher**，由 SummaryWorkerPool 直接生成摘要字段。

### 4.3 PlanAgent

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

**失败策略（DFSDT）**：
- 若 `root_url` 返回 403/Cloudflare，尝试切换 `StealthyFetcher`
- 若抓到链接数为 0，重试一次并扩大链接选择策略
- 两次失败后标记 run 为 `plan_failed`，跳过本轮

### 4.4 CrawlDAG（并行抓取 Worker Pool）

**职责**：按 CrawlPlan 并发抓取所有 URL，返回原始页面内容。纯 I/O，无 LLM。

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

### 4.5 QualityWorkerPool（并行 Critic）

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
            cached = memory.get(config.source_id, page.url)
            if cached:
                return QualifiedPage(page=page, verdict=cached.verdict, score=cached.quality_score)
            result = await llm_assess(page, config)
            memory.upsert(config.source_id, page.url, result)
            return QualifiedPage(page=page, verdict=result.verdict, score=result.score) if result.verdict == "keep" else None
    results = await asyncio.gather(*[assess_one(p) for p in pages])
    return [r for r in results if r is not None]
```

**后处理（确定性规则）：**
- `score < quality_threshold`（用户配置，默认 4）→ 强制 `verdict=discard`
- `should_remember=true` 或 `verdict=discard, seen_count≥1` → 写入 `agent_site_memory`
- SiteMemory `verdict=keep` 命中 → 直接复用分数，跳过 LLM 调用（有效期 7 天）
- SiteMemory `verdict=discard` → 永久有效，管理员可手动删除

### 4.6 SummaryWorkerPool（并行 Generator，自适应摘要）

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
  "importance": "高",
  "body": "...",               // 格式依 content_type 自适应
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

**映射到现有 DB 字段（零 schema 变更）：**

| SummaryWorkerPool 字段 | → DB 字段 | 说明 |
|------------------------|----------|------|
| `title` | `items.title_tldr` | 中文或英文标题 |
| `body` | `items.summary` | 自适应正文 |
| `key_facts` | `items.key_points` | JSONB 数组 |
| `importance` | `items.importance` | 高/中/低 |
| `topic_group` | `items.main_category` | 用户分组名，无分组时用 `agent_crawl` |
| `content_type` | 存入 `items.key_points` 第一个元素的元数据前缀 | 前端据此选择渲染模板 |

### 4.7 SiteMemory

```sql
agent_site_memory(
  id              SERIAL PRIMARY KEY,
  source_id       INT REFERENCES sources(id) ON DELETE CASCADE,
  url_pattern     VARCHAR NOT NULL,
  quality_score   INT,
  quality_reason  TEXT,
  verdict         VARCHAR NOT NULL,    -- keep | discard
  relevant_topic  VARCHAR,
  last_seen_at    TIMESTAMPTZ DEFAULT NOW(),
  seen_count      INT DEFAULT 1,
  UNIQUE(source_id, url_pattern)
)
```

### 4.8 AgentCrawlFetcher 编排

```python
class AgentCrawlFetcher:
    def fetch(self, source: Source) -> list[RawItem]:
        config = self.repo.get_agent_config(source.id)

        async def _pipeline() -> list[RawItem]:
            plan = await self.plan_agent.plan_async(source, config)
            pages = await self.crawl_dag.execute(plan, config)
            qualified = await self.quality_pool.assess_all(pages, config)
            return await self.summary_pool.summarize_all(qualified, config)

        return asyncio.run(_pipeline())
```

> **Async 边界**：整个 pipeline 在一次 `asyncio.run()` 内完成，三个并行阶段共享同一事件循环。若将来 Pipeline 整体升级为 async，直接改为 `await` 即可。

---

## 5. 个性化偏好 Profile

### 5.1 核心概念：Scoring Criteria

用户可自由定义任意数量的「评分准则」，每条描述「什么样的内容对我重要」。准则彼此独立、可单独开关，每条有自己的权重。

### 5.2 数据模型

```sql
user_profiles(
  user_id      UUID PRIMARY KEY REFERENCES users(id),
  criteria     JSONB NOT NULL DEFAULT '[]',
  free_text_description TEXT,
  enable_llm_scoring BOOLEAN DEFAULT FALSE,
  min_score_threshold INT DEFAULT 25,
  updated_at   TIMESTAMPTZ DEFAULT NOW()
)
```

#### Criterion 结构（JSONB 数组元素）

```json
{
  "id":      "c-uuid-001",
  "label":   "内核性能关注",
  "enabled": true,
  "weight":  2.0,
  "match": {
    "keywords":      ["eBPF", "scheduler", "PREEMPT_RT"],
    "sub_tags":      ["kernel", "performance"],
    "main_category": ["OS性能发展"],
    "info_type":     [],
    "keywords_op":   "any"
  }
}
```

- `weight` 范围：0.5–3.0（UI 以步进 0.5 的滑块呈现）
- `keywords_op`：`any`= 命中任一即算，`all`= 全部命中才算

### 5.3 AI 辅助生成准则（ProfileAdvisor Agent）

触发时机：用户点击「AI 帮我生成偏好建议」。

- **输入**：系统当前 top-50 技术热词（来自 sub_tags 聚合）+ 用户历史点击记录 + 可选职能描述
- **输出**：建议的 `Criterion[]` 列表，每条附一句理由
- **UI**：建议结果以可折叠卡片列表展示，用户逐条「接受」或「忽略」

### 5.4 用户行为记录

```sql
user_item_interactions(
  user_id     UUID REFERENCES users(id),
  item_id     INT REFERENCES items(id),
  action      VARCHAR,   -- view | bookmark
  interacted_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (user_id, item_id, action)
)
```

### 5.5 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/users/me/profile` | 获取当前用户 profile（含 criteria） |
| PUT | `/users/me/profile` | 全量更新 profile |
| POST | `/users/me/profile/suggest` | 触发 ProfileAdvisor Agent，返回建议准则列表 |
| POST | `/users/me/interactions` | 记录用户行为（view/bookmark） |

---

## 6. 个性化打分机制

### 6.1 快速通道（无 LLM，查询时实时计算）

对每条 item，遍历用户所有 `enabled=true` 的准则：

```python
def compute_fast_score(item: ItemSummary, criteria: list[Criterion]) -> int:
    raw = 0.0
    for c in criteria:
        if not c.enabled:
            continue
        strength = _match_strength(item, c.match, c.match.keywords_op)
        if strength > 0:
            raw += c.weight * strength
    importance_bonus = {"高": 15, "中": 5, "低": 0}.get(item.importance, 0)
    score = min(100, int(raw * 30) + importance_bonus)
    return score
```

> 快速通道分数在查询时计算，**不持久化存储**。

### 6.2 精打通道（LLM，后台批量，可选）

当 `enable_llm_scoring=true` 时，后台 `ScoringAgent` 定期运行：

```sql
user_item_scores(
  user_id    UUID REFERENCES users(id),
  item_id    INT REFERENCES items(id),
  score      INT NOT NULL,
  scoring_method VARCHAR,      -- fast | llm
  score_reason TEXT,
  scored_at  TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (user_id, item_id)
)
```

查询时：有 LLM 分数则优先使用，否则用快速通道分数。LLM 分数 72 小时后对新 items 重新评估。

### 6.3 个性化 Feed

登录用户访问 `/items` 时：
- 默认排序：按个性化分数（高→低）
- 可切换回原有排序（`published_at` / `fetched_at`）
- 侧边栏新增「最低相关度」滑块（对应 `min_score_threshold`）
- 每条 item 显示分数徽章：**左侧 4px 颜色竖条 + 右上角 JetBrains Mono 分数数字**（详见第 11 节设计系统）

**API 变化（`GET /items`）：**
- 登录状态下 response 增加 `personalized_score: int | null`
- 新增 query param：`sort_by=relevance`（登录后可用）、`min_score=N`

---

## 7. 新闻总结与趋势分析（Digest）

### 7.1 两种模式

| 模式 | 触发方式 | 内容范围 | 存储 |
|------|---------|---------|------|
| 个性化按需总结 | 用户主动生成 | **按用户 scoring criteria 筛选后的 items**（≥ min_score_threshold） | 存入 `digests`，该用户可查 |
| 全局定时总结 | 系统自动（每日/每周） | 全量 items | 存入 `digests`，所有用户可查 |

按需生成时提供「扩展到全部内容」开关，允许用户切换至全局视角。

### 7.2 DigestAgent 多步 Chain

```
Step 1: 数据聚合
  - 查询/过滤 items（个性化模式先按 fast score 过滤）
  - 统计 sub_tag / main_category 频次分布
  - 对比「上一个同等时长区间」频次（识别趋势）
  - 取 top-30 items（按 importance + score 排序）
  输出：频次分布 JSON + trending deltas + top-30 item 摘要列表

Step 2: 主题识别 Agent（LLM Call #1）
  输入：频次分布 + trending deltas
  输出：hotspots[] + emerging_topics[]

Step 3: 内容综合 Agent（LLM Call #2）
  输入：top-30 item 摘要 + Step 2 主题识别结果
  输出：period_summary（300 字以内）

Step 4: 格式化与存储
  拼装最终 Digest JSON → 写入 digests 表（status: ready）
```

### 7.3 Digest 输出结构

```json
{
  "period_summary": "本周共收录 127 条信息。内核调度与 eBPF 工具链持续活跃...",
  "hotspots": [
    {
      "topic": "Linux 6.12 内核发布",
      "item_count": 8,
      "reason": "sched_ext 合入主线，多家评测对比发布",
      "related_item_ids": [12, 45, 67]
    }
  ],
  "emerging_topics": [
    {
      "topic": "RISC-V 服务器生态",
      "trend": "rising",
      "reasoning": "本周提及 4 次，较上周增加 3 次"
    }
  ],
  "stats": {
    "total_items": 127,
    "by_category": {"OS性能发展": 45, "OS跟踪来源": 38},
    "top_tags": [["kernel", 23], ["RHEL", 18], ["eBPF", 12]]
  }
}
```

### 7.4 数据模型

```sql
digests(
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trigger_type     VARCHAR NOT NULL,   -- manual | scheduled
  created_by       UUID REFERENCES users(id),
  time_range_start TIMESTAMPTZ NOT NULL,
  time_range_end   TIMESTAMPTZ NOT NULL,
  scope            VARCHAR DEFAULT 'personalized',  -- personalized | global
  period_summary   TEXT,
  hotspots         JSONB DEFAULT '[]',
  emerging_topics  JSONB DEFAULT '[]',
  stats            JSONB DEFAULT '{}',
  status           VARCHAR DEFAULT 'generating',  -- generating | ready | failed
  error_message    TEXT,
  created_at       TIMESTAMPTZ DEFAULT NOW()
)
```

### 7.5 异步生成流程

DigestAgent 涉及 2 次 LLM 调用（约 15-40 秒），采用轮询模式：
1. POST `/digest` → 立即返回 `{id, status: "generating"}`，后台启动 DigestAgent
2. 前端每 2 秒轮询 GET `/digest/{id}`，直到 `status == "ready"` 或 `"failed"`
3. 前端展示「生成中…」三步进度指示器（Step 1 / 2 / 3）

### 7.6 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/digest` | 触发生成（time_range_start, time_range_end, scope）→ 立即返回 `{id, status}` |
| GET | `/digest` | 列出所有可查看的 Digest |
| GET | `/digest/{id}` | 获取 Digest 详情（含状态） |
| GET | `/admin/digest/schedule` | 查看/配置定时任务（admin） |
| PUT | `/admin/digest/schedule` | 设置每日/每周定时 Digest（admin） |

---

## 8. 数据模型变更汇总

### 新增表（完整列表）

```sql
-- ── 用户系统 ──────────────────────────────────────────────────
users(id, email, password_hash, display_name, role, is_active, created_at, last_login_at)

-- ── 个性化 ────────────────────────────────────────────────────
user_profiles(user_id, criteria JSONB, free_text_description, enable_llm_scoring, min_score_threshold, updated_at)
user_item_scores(user_id, item_id, score, scoring_method, score_reason, scored_at)
user_item_interactions(user_id, item_id, action, interacted_at)

-- ── Digest ────────────────────────────────────────────────────
digests(id, trigger_type, created_by, time_range_start, time_range_end, scope,
        period_summary, hotspots, emerging_topics, stats, status, error_message, created_at)

-- ── Agent 采集引擎 ────────────────────────────────────────────
agent_source_configs(
  source_id         INT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
  focus_areas       JSONB NOT NULL DEFAULT '[]',
  topic_groups      JSONB NOT NULL DEFAULT '[]',
  crawl_depth       INT NOT NULL DEFAULT 1,
  max_urls_per_run  INT NOT NULL DEFAULT 20,
  quality_threshold INT NOT NULL DEFAULT 4,
  crawl_workers     INT NOT NULL DEFAULT 5,
  quality_workers   INT NOT NULL DEFAULT 3,
  summary_workers   INT NOT NULL DEFAULT 3,
  updated_at        TIMESTAMPTZ DEFAULT NOW()
)

agent_site_memory(
  id, source_id, url_pattern, quality_score, quality_reason,
  verdict, relevant_topic, last_seen_at, seen_count,
  UNIQUE(source_id, url_pattern)
)

agent_crawl_runs(
  id, source_id, started_at, completed_at,
  plan_urls_count, fetched_count, quality_passed, items_created,
  status,       -- running | completed | failed | plan_failed
  error_message
)
```

### 现有表最小化变更

```sql
-- sources.type 枚举新增 'agent_crawl'
-- items.status 新增 'agent_enriched'（agent 产出的 item 不走 Enricher，直接标此状态）
-- 无其他破坏性变更
```

---

## 9. LLM Agent 边界

| Agent | 模块路径 | 调用时机 | 输入 | 输出 |
|-------|---------|---------|------|------|
| Enricher（现有）| `app/processing/enricher.py` | 入库时，每条一次 | item 正文 | 结构化字段（title_zh, summary 等） |
| PlanAgent（新）| `app/agent/plan_agent.py` | agent 采集时，每 source 一次 | root_url + 链接列表 + 关注点 | 确定性 URL 计划 |
| QualityWorkerPool（新）| `app/agent/quality_pool.py` | agent 采集，每页并发 | 页面内容 + 关注点 | 0-10 分 + verdict |
| SummaryWorkerPool（新）| `app/agent/summary_pool.py` | agent 采集，每页并发 | 页面内容 + 主题分组 | 自适应摘要 JSON |
| ScoringAgent（新）| `app/processing/scorer.py` | 后台批量，用户开启 LLM 打分时 | user profile + item 摘要 | 0-100 分 + 理由 |
| ProfileAdvisor（新）| `app/processing/profile_advisor.py` | 用户主动触发 | top-50 热词 + 行为历史 | 建议 Criterion[] |
| DigestAgent（新）| `app/processing/digest_agent.py` | 按需/定期 | 聚合数据 + top items | Digest JSON（两次 LLM Call）|

> **agent_crawl 路径不走 Enricher**：PlanAgent → CrawlDAG → QualityWorkerPool → SummaryWorkerPool 是独立流水线，产出直接入库，`status=agent_enriched`。

所有 Agent 使用现有 `LlmClient`（OpenAI 兼容接口），兼容未来替换为 LangChain/LangGraph。

---

## 10. 后端文件结构

```
backend/app/
├── fetchers/
│   ├── base.py              （现有，不变）
│   ├── rss.py               （不变）
│   ├── page_monitor.py      （不变）
│   ├── search.py            （不变）
│   └── agent_crawl.py       ← 新增：AgentCrawlFetcher（编排 app/agent/ 四个子模块）
├── agent/                   ← 新增目录
│   ├── __init__.py
│   ├── plan_agent.py        ← PlanAgent（Pre-Act + DFSDT）
│   ├── crawl_dag.py         ← CrawlDAG（asyncio Semaphore，crawl_workers 并发）
│   ├── quality_pool.py      ← QualityWorkerPool（Critic，quality_workers 并发 LLM）
│   ├── summary_pool.py      ← SummaryWorkerPool（Generator，summary_workers 并发 LLM）
│   └── site_memory.py       ← SiteMemory 读写接口（DB + 内存缓存）
├── processing/
│   ├── enricher.py          （现有，不变）
│   ├── relevance.py         （现有，不变）
│   ├── normalizer.py        （现有，不变）
│   ├── dedup.py             （现有，不变）
│   ├── scorer.py            ← 新增：ScoringAgent（LLM 精打分）
│   ├── profile_advisor.py   ← 新增：ProfileAdvisor Agent
│   └── digest_agent.py      ← 新增：DigestAgent（多步 Chain）
└── models.py                （新增所有 V2 ORM 模型）
```

---

## 11. API 端点完整列表（V2 新增）

### 用户认证
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/auth/register` | 注册 |
| POST | `/auth/login` | 登录，返回 JWT |
| GET | `/auth/me` | 当前用户信息 |
| PUT | `/auth/me` | 更新 display_name |

### 个性化 Profile
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/users/me/profile` | 获取 profile + criteria |
| PUT | `/users/me/profile` | 全量更新 profile |
| POST | `/users/me/profile/suggest` | AI 生成准则建议 |
| POST | `/users/me/interactions` | 记录 view/bookmark 行为 |

### Digest
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/digest` | 触发生成（异步，返回 id） |
| GET | `/digest` | 列出可查看的 Digest |
| GET | `/digest/{id}` | 获取详情 |
| GET/PUT | `/admin/digest/schedule` | 定时任务配置（admin） |

### Agent 采集源
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/sources/agent` | 列出当前用户的 agent sources |
| POST | `/sources/agent` | 新增 agent source |
| PUT | `/sources/agent/{id}` | 更新配置 |
| DELETE | `/sources/agent/{id}` | 删除（含关联 site_memory） |
| POST | `/sources/agent/{id}/run` | 立即触发一次采集 |
| GET | `/sources/agent/{id}/runs` | 查看运行历史 |
| GET | `/sources/agent/{id}/memory` | 查看 SiteMemory（admin） |
| DELETE | `/sources/agent/{id}/memory/{url_pattern}` | 手动删除 memory 记录（admin） |

---

## 12. 前端设计系统与页面变更

### 12.1 设计 Token 系统

#### 颜色

```css
--ink:           #0d1117;
--ink-secondary: #424a57;
--ink-muted:     #7d8b9a;
--ground:        #f0f2f7;
--surface:       #ffffff;
--surface-tint:  #f8f9fc;
--border:        #d4dae3;
--border-subtle: #eaecf2;

/* 个性化分数信号色 */
--signal-high:    #0d9488;   /* teal-600，高相关度 ≥ 80 */
--signal-high-bg: #f0fdfa;
--signal-mid:     #d97706;   /* amber-600，中相关度 50–79 */
--signal-mid-bg:  #fffbeb;
--signal-low:     #94a3b8;   /* slate-400，低相关度 < 50 */
--signal-low-bg:  #f8fafc;

--accent:         #2563eb;
--accent-hover:   #1d4ed8;
--accent-subtle:  #eff6ff;
```

#### 字体

```css
/* Google Fonts: Inter + JetBrains Mono */
--font-body: "Inter", system-ui, sans-serif;
--font-mono: "JetBrains Mono", "Fira Code", monospace;
```

`--font-mono` 专用场景：分数数字、日期、版本号、技术标签、Digest 页面时间戳。

### 12.2 标志性元素：个性化分数徽章

左侧 4px 颜色竖条 + 右上角 JetBrains Mono 分数数字：

```
┌─┬──────────────────────────────────────────────┐
│█│  高   发布   OS性能发展                  82   │  ← teal 82
│█│  Linux 6.12 内核正式发布                      │
└─┴──────────────────────────────────────────────┘
  ↑ 4px signal-high 左边框
```

| 分数 | 左边框色 | 分数字色 |
|------|---------|---------|
| ≥ 80 | `--signal-high` | `--signal-high` |
| 50–79 | `--signal-mid` | `--signal-mid` |
| < 50 | `--border` | `--ink-muted` |

### 12.3 新增前端页面

| 路由 | 说明 |
|------|------|
| `/login` | 登录页（单列居中，440px 卡片） |
| `/register` | 注册页 |
| `/settings/profile` | 偏好设置（Scoring Criteria 管理 + LLM 打分开关 + AI 建议区） |
| `/settings/agent-sources` | Agent 采集源管理（添加/编辑/删除/触发运行） |
| `/digest` | Digest 历史 + 生成入口（INTEL BRIEF 风格头部，JetBrains Mono 日期） |

### 12.4 Feed 页面增强

- **分数徽章**：每条 ItemCard 左侧 4px 竖条 + 右上角 Mono 分数
- **相关度滑块**：侧边栏新增「最低相关度」滑块
- **排序新增**：`相关度优先`（登录后可见）
- **Agent 来源标记**：`agent_crawl` 来源显示「🤖 智能采集」+ 域名
- **主题分组标题**：`topic_group` 不为空时，列表中相同分组的 items 显示分组分隔标题
- **Benchmark 渲染**：`content_type=benchmark` 时 key_facts 用 `<table>` 渲染

### 12.5 动效规范

只在两处使用动效：
1. Digest 生成进度（Step 1 → 2 → 3，圆点从灰 → accent 蓝，0.3s transition）
2. 分数徽章首次出现（`opacity: 0 → 1` + `translateY(4px) → 0`，0.25s）

`@media (prefers-reduced-motion: reduce)` 时所有 transition 设为 0s。

---

## 13. 测试策略

| 类型 | 覆盖范围 |
|------|---------|
| **Agent 采集** | PlanAgent（mock Scrapling + mock LLM，验证确定性校验）；QualityWorkerPool（threshold 过滤、SiteMemory 缓存复用）；SummaryWorkerPool（各 content_type 到 DB 字段映射）；CrawlDAG（并发上限、单页失败隔离）；AgentCrawlFetcher 集成（fixture HTML + mock LLM）|
| **用户系统** | 注册/登录 API；JWT 签发与验证；profile 自动初始化 |
| **个性化** | `compute_fast_score()` 纯函数；ScoringAgent mock LLM；ProfileAdvisor mock LLM |
| **Digest** | DigestAgent 各 Step 独立 mock；异步生成轮询状态机 |
| **集成** | `/items` 个性化排序；`/digest` 生成与查询；`/sources/agent` CRUD |
| **前端** | ProfileCriteria 组件渲染；分数徽章显示；Digest 状态切换；useLogStream hook |

---

## 14. 迭代路线图

| 阶段 | 内容 |
|------|------|
| V2（本文档）| 用户系统 + 个性化打分 + Digest 总结 + Agent 采集引擎 + 实时日志面板 |
| V3 | 邮件推送（订阅 digest 到邮箱）+ iWiki 联动 |
| V4 | 采集源管理后台、抓取健康度监控、AI 成本看板 |
| V5 | 语义检索、关系图谱、协作偏好 |

---

## 15. 待确认项

- [ ] Agent 采集：`agent_crawl` 来源是否纳入 APScheduler cron 体系（建议纳入，默认 `"0 6 * * *"` 每日一次）
- [ ] Agent 采集：冷启动 LLM 调用成本估算（20 URLs ≈ 41 次 LLM 调用，需确认内网网关速率限制）
- [ ] Agent 采集：`crawl_workers` 默认值 5 是否对所有目标网站足够保守
- [ ] 个性化：LLM 精打分的批次大小和调用限速（依赖内部 LLM 网关实际限制）
- [ ] Digest：定时总结的默认频率是每日还是每周？（建议每周，admin 可改）
- [ ] 用户系统：注册是否需要邮箱格式校验（建议：仅格式校验，无需发验证邮件）
- [ ] 实时日志：`/logs/stream` 端点 V2 上线后是否需要 JWT 鉴权（建议：仅限已登录用户）
