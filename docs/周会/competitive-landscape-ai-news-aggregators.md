# AI/LLM 新闻聚合开源项目对标分析

> 调研日期：2026-06-14
> 目的：对标 os-news-tracker，借鉴架构与功能设计

## 一、与 os-news-tracker 最相似的对标项目

os-news-tracker 架构：**RSS + 网页监控 + 搜索 → LLM 结构化摘要（分类/子标签/实体/重要性）→ React Web UI（分面过滤+时间过滤）**，技术栈 **Python FastAPI + React + PostgreSQL**。

以下按相似度排序：

---

### 🥇 Tier 1：高度对标（架构相似度最高）

#### 1. [CondenseIt](https://github.com/wildlifechorus/condenseit) — 68 ⭐

| 维度 | 内容 |
|------|------|
| **核心用途** | 自托管 AI 新闻摘要，RSS/YouTube/Reddit/HN/GitHub → LLM → 浏览器阅读 |
| **技术栈** | Python + TypeScript, Ollama/OpenRouter/OpenAI, SQLite |
| **采集方式** | RSS、YouTube、Reddit、Hacker News、GitHub Releases、Google News、网站 diff、播客 |
| **LLM 用法** | 摘要 + 评分排序 + 偏好学习（用户打分反馈） |
| **前端** | Web UI（每日摘要阅读器 + 管理面板） |
| **部署** | Docker Compose, GHCR 镜像 |
| **最近更新** | 2026-06-04 (v2.8.0) |

**对标亮点**：多信号排序引擎（TF-IDF + embeddings + 同义词）、用户偏好学习、偏好反馈闭环——可借鉴用于个性化推荐方向。

---

#### 2. [glean](https://github.com/jaypetez/glean) — 2 ⭐（功能最完整的新项目）

| 维度 | 内容 |
|------|------|
| **核心用途** | 可插拔个人 Agent，RSS+爬虫+搜索+API → 任意 LLM → 多通道推送 |
| **技术栈** | Python, Ollama/Anthropic/OpenAI, Docker |
| **采集方式** | RSS/Atom、网页抓取、Hacker News、Reddit、SearXNG/Brave/Tavily 搜索 |
| **LLM 用法** | 摘要 + 去重 + 排序 + **结构化 Skill 提取**（JSON Schema，如 CVE 提取、交易发现） |
| **前端** | Web UI（SSE 实时 dashboard） |
| **部署** | Docker `docker compose up` |
| **最近更新** | 2026-05-19 (v1.4.0) |

**对标亮点**：
- **4 层插件系统**：Source → Enrich → Filter → Sink，每个环节可插拔
- **Per-source LLM dispatch**：不同源可以用不同模型
- **Structured Skill**：用 JSON Schema 定义提取模板（类似 os-news-tracker 的 EnrichedFields 设计）
- **SSE 实时推送 dashboard**：前端实时展示处理进度

---

#### 3. [Horizon](https://github.com/Thysrael/Horizon) — 6.4k ⭐

| 维度 | 内容 |
|------|------|
| **核心用途** | AI 新闻雷达，多源聚合 → LLM 评分过滤 → 中英双语日报 |
| **技术栈** | Python 99.7%, Claude/GPT-4/Gemini, GitHub Actions |
| **采集方式** | Hacker News、RSS、Reddit、Telegram、GitHub、Twitter/X |
| **LLM 用法** | 0-10 评分 + 内容过滤 + 双语摘要 + Web 上下文富化 |
| **前端** | 静态站点（GitHub Pages）+ 邮件订阅 |
| **部署** | GitHub Actions 自动化 |
| **最近更新** | 活跃维护中（179 commits） |

**对标亮点**：
- **评分+过滤 pipeline**：先评分再决定是否保留（类似 os-news-tracker 计划的 relevance filter）
- **Web 上下文富化**：对每条新闻搜索背景信息补充
- **多源去重**：跨源 dedup

---

#### 4. [ClueArk](https://github.com/lqomg/ClueArk) — 41 ⭐

| 维度 | 内容 |
|------|------|
| **核心用途** | 个人 AI 情报助手，关键词驱动多信源监控 |
| **技术栈** | TypeScript, NestJS + React + MongoDB, Qdrant 向量搜索, Docker Compose |
| **采集方式** | RSS + 可选 Web 爬虫，关键词驱动 |
| **LLM 用法** | 关键词/实体提取 + 语义匹配 + 话题监控 |
| **前端** | React Web UI |
| **部署** | Docker Compose 一键部署 |
| **最近更新** | 活跃开发中（76 commits） |

**对标亮点**：
- **关键词驱动采集**：类似 os-news-tracker 的 search 类型 source
- **Qdrant 向量搜索**：语义级别的内容匹配
- **NestJS + React + MongoDB**：与 FastAPI + React + PostgreSQL 属同类全栈架构

---

### 🥈 Tier 2：部分对标（有亮点可借鉴）

#### 5. [TrendRadar](https://github.com/sansan0/TrendRadar) — 59.4k ⭐

| 维度 | 内容 |
|------|------|
| **核心用途** | AI 舆情监控，35+ 平台热点聚合 + AI 分析 |
| **技术栈** | Python, Docker |
| **采集方式** | 微博/知乎/B站/抖音/财联社等 35+ 平台 + RSS |
| **LLM 用法** | AI 筛选 + 翻译 + 分析简报 + 情感分析 + 趋势预测 + MCP 自然语言查询 |
| **前端** | Web UI + 微信/飞书/钉钉/Telegram/邮件/ntfy/Bark/Slack 推送 |
| **部署** | Docker, 30 秒 GitHub Actions 部署 |
| **最近更新** | 2026-06-02 (v6.9.0) |

**对标亮点**：
- **MCP 集成**：支持 Claude 等 AI 助手通过自然语言查询趋势——这是前沿方向
- **多通道推送**：微信/飞书/钉钉/Telegram 全覆盖
- **13 种分析工具**：情感分析、趋势追踪、相似检索等

#### 6. [ZenFeed](https://github.com/glidea/zenfeed) — 1.7k ⭐

| 维度 | 内容 |
|------|------|
| **核心用途** | AI 信息中枢，智能 RSS 阅读器 + 实时事件监控 |
| **技术栈** | Go 99.8%, MCP Server, Prometheus 监控 |
| **采集方式** | RSS + RSSHub |
| **LLM 用法** | 摘要 + 分析报告 + 邮件摘要 |
| **前端** | Web UI（RSS 阅读器） |
| **部署** | Docker |
| **最近更新** | 2025-11-07 (v0.7.0) |

**对标亮点**：
- **MCP Server**：可作为 AI 助手的数据源
- **Prometheus 监控**：运维级别的可观测性
- **事件监控模式**：不仅是摘要，还追踪事件演变

#### 7. [auto-news](https://github.com/finaldie/auto-news) — 885 ⭐

| 维度 | 内容 |
|------|------|
| **核心用途** | 个人新闻聚合器，多源 → LLM → 高效阅读 |
| **技术栈** | Python 97.2%, LangChain (ChatGPT/Gemini/Ollama) |
| **采集方式** | Twitter/X、RSS、YouTube、Web 文章、Reddit、个人笔记 |
| **LLM 用法** | 摘要 + 多 Agent 深度分析 + 周回顾 |
| **前端** | CLI + Helm Chart (K8s) |
| **部署** | Helm / K8s |
| **最近更新** | 2024-11-10 (v0.9.15) |

---

### 🥉 Tier 3：特定领域参考

#### 8. [Alt](https://github.com/Kaikei-e/Alt) — 8 ⭐

- **亮点**：20+ 微服务（Go/Python/Rust/SvelteKit/F#），事件溯源架构，RAG Q&A，3D 标签可视化，GPU 批处理
- **借鉴**：微服务拆分思路、RAG 问答、标签可视化

#### 9. [CyberPulse](https://github.com/Ali-Jabbar-CS/CyberPulse) — 新项目

- **亮点**：网络安全威胁情报专用，CVE 正则+LLM 双重提取，Claude API 分析
- **借鉴**：结构化事实流（CVE）的解析模式——对应 os-news-tracker 计划的 structured facts stream

#### 10. [The Daily Crosswire](https://github.com/yamijuan/the-daily-crosswire) — 6 ⭐

- **亮点**：LangGraph Agent 编排，FastAPI + React + PostgreSQL + pgvector，多语言，PDF 输出
- **借鉴**：**技术栈与 os-news-tracker 几乎一致**（FastAPI + React + PG + pgvector）

#### 11. [AI-News-Subscription-Agent](https://github.com/akatsukikyouko/AI-News-Subscription-Agent)（智闻订阅）

- **亮点**：Flask + APScheduler + DrissionPage（反爬），多线程 AI 分析，关键词定制订阅
- **借鉴**：反爬虫方案、定时任务调度

#### 12. [AI Daily News](https://github.com/Dajucoder/ai_daily_news)

- **亮点**：Django + React 18 + Ant Design + PostgreSQL，流式对话，JWT 认证
- **借鉴**：**全栈架构与 os-news-tracker 高度一致**，前端组件库选择参考

#### 13. [FactuAI](https://github.com/zayedrmdn/FactuAI)

- **亮点**：React + FastAPI + PostgreSQL + pgvector，4 阶段分析 pipeline，新闻真实性验证
- **借鉴**：技术栈完全一致，pipeline 阶段划分参考

---

## 二、横向对比总表

| 项目 | Stars | 采集方式 | LLM 用法 | 前端 | 技术栈 | 最近更新 |
|------|-------|---------|---------|------|--------|---------|
| **TrendRadar** | 59.4k | 35+平台+RSS | 筛选/翻译/分析/情感/MCP | Web+多通道推送 | Python, Docker | 2026-06 |
| **Horizon** | 6.4k | RSS/HN/Reddit/TG/GH/X | 评分/过滤/双语摘要/富化 | 静态站点+邮件 | Python, Claude/GPT/Gemini | 2026 活跃 |
| **ZenFeed** | 1.7k | RSS+RSSHub | 摘要/分析/事件监控 | Web UI | Go, MCP, Prometheus | 2025-11 |
| **auto-news** | 885 | RSS/X/YT/Reddit/Web | 摘要/多Agent深度分析 | CLI | Python, LangChain | 2024-11 |
| **GitHubSentinel** | 364 | GitHub API+HN | 摘要/信息挖掘 | Gradio Web UI | Python, OpenAI/Ollama | 2024-09 |
| **CondenseIt** | 68 | RSS/YT/Reddit/HN/GH/播客 | 摘要/偏好学习/多信号排序 | Web 阅读器 | Python+TS, Ollama/OpenAI | 2026-06 |
| **OpenTrends** | 59 | RSS/RSSHub/HN/GH/知乎 | 翻译/摘要 | Web | TS, Hono/Bun, PG | 2026 活跃 |
| **newscope** | 42 | RSS | 兴趣评分/话题提取 | Web UI | Go, Docker | 活跃 |
| **ClueArk** | 41 | RSS+爬虫(关键词驱动) | 实体提取/语义匹配 | React | TS, NestJS, Mongo, Qdrant | 活跃 |
| **News Llama** | 11 | RSS/X/Reddit/DuckDuckGo | 摘要/去重/关键词提取 | HTML/RSS/JSON | Python, Local LLM | 活跃 |
| **Alt** | 8 | RSS+Inoreader | 摘要/标签提取/RAG QA | SvelteKit 3D | Go+Python+Rust 多服务 | 活跃 |
| **Curio** | 7 | RSS | 文章策展 | 报纸风格 Web | Python+TS, OpenAI | 活跃 |
| **Daily Crosswire** | 6 | RSS+Web Search | LangGraph Agent 编排 | FastAPI+React | Python, PG+pgvector | 活跃 |
| **glean** | 2 | RSS+爬虫+搜索+API | 摘要/去重/结构化提取 | SSE Dashboard | Python, Docker | 2026-05 |

---

## 三、关键发现与建议

### os-news-tracker 相比对标项目的差异化优势

| 维度 | os-news-tracker | 对标项目普遍情况 |
|------|-----------------|-----------------|
| **结构化富化** | 分类+子标签+实体+重要性 四维标注 | 多数只做摘要，不做结构化字段 |
| **分面检索** | 前端 Facet 过滤 + 时间范围过滤 | 多数只有时间排序或简单搜索 |
| **手动新闻采集** | ManualNewsRun 目标驱动采集+时间过滤统计 | 几乎没有对标实现 |
| **双流架构** | News + Structured Facts 并行 | 无对标（CyberPulse 部分接近） |
| **源管理** | 84 sources YAML 种子 + 注册表模式 | 多数只支持 RSS OPML 导入 |

### 建议借鉴的功能方向

1. **偏好学习反馈闭环**（参考 CondenseIt）：用户对条目的打分/互动信号反馈到排序模型
2. **MCP Server 集成**（参考 TrendRadar/ZenFeed）：让外部 AI 助手能查询新闻库
3. **多通道推送**（参考 TrendRadar/glean）：Telegram/飞书/邮件摘要推送
4. **RAG Q&A**（参考 Alt/Daily Crosswire）：基于新闻库的语义搜索问答
5. **SSE/WebSocket 实时进度**（参考 glean）：ManualNewsRun 的前端实时进度展示
6. **Per-source LLM dispatch**（参考 glean）：不同源用不同 LLM 配置
7. **pgvector 语义去重**（参考 Daily Crosswire）：在现有 url_hash 之上增加语义级去重

---

## 四、项目链接汇总

| 项目 | GitHub |
|------|--------|
| TrendRadar | <https://github.com/sansan0/TrendRadar> |
| Horizon | <https://github.com/Thysrael/Horizon> |
| ZenFeed | <https://github.com/glidea/zenfeed> |
| auto-news | <https://github.com/finaldie/auto-news> |
| GitHubSentinel | <https://github.com/DjangoPeng/GitHubSentinel> |
| CondenseIt | <https://github.com/wildlifechorus/condenseit> |
| OpenTrends | <https://github.com/nexmoe/opentrends> |
| newscope | <https://github.com/umputun/newscope> |
| ClueArk | <https://github.com/lqomg/ClueArk> |
| News Llama | <https://github.com/slb350/news-llama> |
| Alt | <https://github.com/Kaikei-e/Alt> |
| Curio | <https://github.com/CyberDNS/Curio> |
| The Daily Crosswire | <https://github.com/yamijuan/the-daily-crosswire> |
| glean | <https://github.com/jaypetez/glean> |
| CyberPulse | <https://github.com/Ali-Jabbar-CS/CyberPulse> |
| AI-News-Subscription-Agent | <https://github.com/akatsukikyouko/AI-News-Subscription-Agent> |
| AI Daily News | <https://github.com/Dajucoder/ai_daily_news> |
| FactuAI | <https://github.com/zayedrmdn/FactuAI> |
