# OS News Tracker

OS News Tracker 是面向操作系统、Linux 发行版、开源基础设施和相关 AI 工具链的技术新闻情报平台。系统从 RSS、页面列表、JSON API、搜索结果、微信公众号、站点发现 DSL 和 Agent Crawl 等来源采集候选内容，经标准化、去重、LLM 富化后入库，并在前端提供新闻流、站点发现、邮件任务中心和系统晨抓能力。

当前项目已经从早期的单一新闻抓取工具演进为一个多模块系统。本文档以当前代码为准，覆盖技术细节、文件组织、系统功能和部署方式。

## 当前能力边界

- 新闻流：浏览、搜索、分面筛选、时间筛选、详情查看、推荐理由生成。
- 站点发现：输入站点或多源入口，生成可复用的 CrawlMethod DSL，支持待审核、质量审计、手动运行、取消和日志查看。
- 爬取方式库：管理 discovery methods，按统一抓取限制执行，并把结果送入标准新闻流水线。
- 邮件任务中心：基于当前筛选生成 HTML 预览、立即发送、保存模板、创建定时邮件任务，支持 TOF4 API 和 SMTP。
- 系统晨抓：按配置定时批量运行 active discovery methods，并记录每个方法的执行结果。
- 管理权限：前端使用单密码登录解锁管理功能，后端对敏感接口做权限校验。
- Agent Crawl：`backend/app/agent/` 和 `backend/app/fetchers/agent_crawl.py` 仍保留，但按项目约定视为冻结模块，非明确需求不修改。

已删除的旧功能：独立“新闻处理控制”模块和 `/news-run*` 手动新闻运行接口已经移除。运行日志统一使用 `/run-logs`。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Python 3.11+, FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL, APScheduler |
| 数据契约 | Pydantic v2, pydantic-settings |
| 抓取与解析 | feedparser, httpx, Scrapling, Playwright |
| LLM | OpenAI-compatible chat completions client |
| 智能发现 | LangGraph, langchain-core, langchain-openai |
| 前端 | React 19, Vite 8, TypeScript 6, TanStack Query, react-router-dom |
| 邮件 | TOF4 HTTP API, SMTP |
| 测试 | pytest, pytest-asyncio, respx, vitest |
| 部署 | Docker Compose: PostgreSQL + FastAPI/Uvicorn + Nginx/React |

## 数据与处理流程

### 标准新闻流水线

```text
Source/Fetcher
  -> RawItem
  -> normalizer
  -> relevance filter
  -> dedup
  -> enricher
  -> repository/store
  -> Item + Tag + Entity
```

核心入口：

- `backend/app/pipeline.py`：标准流水线编排。
- `backend/app/fetchers/`：RSS、页面监控、搜索、JSON API、Agent Crawl fetcher。
- `backend/app/processing/`：标准化、去重、相关性过滤、LLM 富化、推荐理由。
- `backend/app/repository.py`：幂等写入与关联表维护。

### 站点发现与 CrawlMethod 流程

```text
用户输入站点/多源入口
  -> discovery graph / multi graph
  -> 生成 DSL recipe
  -> auditor 审计
  -> pending review
  -> 审核通过后进入 CrawlMethod 库
  -> 手动运行或晨抓运行
  -> discovery runner
  -> 标准新闻流水线入库
```

核心模块：

- `backend/app/discovery/graph.py`：单站点发现图。
- `backend/app/discovery/multi_graph.py`：多源发现入口。
- `backend/app/discovery/dsl.py`：受限 DSL 结构与校验。
- `backend/app/discovery/interpreter.py`：DSL 解释执行。
- `backend/app/discovery/multi_dsl.py`、`multi_interpreter.py`：多源 DSL 支持。
- `backend/app/discovery/runner.py`：运行已入库 CrawlMethod 并接入标准 pipeline。
- `backend/app/discovery/recipe_prepare.py`：抓取限制、微信跳过键、微信补抓 action 准备。
- `backend/app/discovery/wechat_tools.py`：微信公众号搜索/历史/正文补抓。
- `backend/app/discovery/review.py`：待审核方式审批、删除、邮件提醒。
- `backend/app/discovery/quality_audit.py`：信息质量、信息密度、综合评分与等级。

## 主要系统功能

### 1. 新闻流

前端入口：`/`

主要能力：

- 关键词搜索。
- 主分类、重要度、技术热点筛选。
- 发布时间预设：24h、7d、30d、自定义日期。
- 排序：发布时间或入库时间，正序/倒序。
- 条目详情侧滑面板。
- 推荐理由生成：`POST /items/{id}/reason`。
- 邮件任务中心和系统晨抓入口。

相关文件：

- `frontend/src/pages/HomePage.tsx`
- `frontend/src/components/FacetSidebar.tsx`
- `frontend/src/components/ItemList.tsx`
- `frontend/src/components/ItemCard.tsx`
- `frontend/src/components/ItemDetail.tsx`
- `backend/app/api/routes.py`

### 2. 站点发现

前端入口：`/discover`

主要能力：

- 单站点 AI 探查。
- 多源探查，包括普通网站、微信搜索、公众号历史等 recipe。
- 发现流程图和节点细节展示。
- CrawlMethod 待审核列表。
- CrawlMethod 正式库列表、详情、运行、取消、删除。
- Prompt 工作室。
- 主分类维护。
- 质量评分展示：综合评分、质量分、密度分、等级和审计原因。

相关文件：

- `frontend/src/pages/DiscoveryPage.tsx`
- `frontend/src/components/DiscoveryPanel.tsx`
- `frontend/src/components/DiscoveryFlowChart.tsx`
- `frontend/src/components/DiscoveryLogPanel.tsx`
- `frontend/src/components/CrawlMethodReviewList.tsx`
- `frontend/src/components/CrawlMethodList.tsx`
- `frontend/src/components/CrawlMethodDetail.tsx`
- `frontend/src/components/PromptStudioPanel.tsx`
- `frontend/src/components/MainCategoryPanel.tsx`
- `backend/app/api/discovery_routes.py`
- `backend/app/discovery/`

### 3. 邮件任务中心

邮件任务中心挂在首页卡片中，弹窗内分为：

- 立即发送：基于当前筛选快照生成 HTML 预览和发送请求。
- 模板列表：保存、预览、更新、删除模板，可从模板创建预定任务。
- 预定发送：每日/每周定时发送，支持暂停、恢复、立即发送和查看发送日志。

重要细节：

- 时间筛选会保存为快照。24h、7d、30d 使用相对窗口，发送时按当下时间重新计算。
- 邮件预览和正式邮件 HTML 都会显示每条新闻的重要性。
- 邮件发送通道由 `MAIL_PROVIDER` 控制，可选 `tof4` 或 `smtp`。

相关文件：

- `frontend/src/components/MailTaskCenter.tsx`
- `frontend/src/components/MailImmediateSendPanel.tsx`
- `frontend/src/components/MailTemplateListPanel.tsx`
- `frontend/src/components/MailScheduleListPanel.tsx`
- `frontend/src/mail/api.ts`
- `frontend/src/mail/types.ts`
- `backend/app/api/mail_routes.py`
- `backend/app/mail/service.py`
- `backend/app/mail/rendering.py`
- `backend/app/mail/tof4_provider.py`
- `backend/app/mail/smtp_provider.py`

### 4. 系统晨抓

系统晨抓用于按计划批量执行 active discovery methods，不再依赖已删除的手动新闻处理控制。

主要能力：

- 配置启用状态、执行时间、频率、回看窗口、巡检间隔。
- 立即执行和停止。
- 查看今日状态、运行记录、单个方法明细。
- 与 `/run-logs` 共享结构化运行日志。

相关文件：

- `frontend/src/components/MorningCrawlModal.tsx`
- `frontend/src/components/MorningCrawlStatusPanel.tsx`
- `frontend/src/components/MorningCrawlTimelinePanel.tsx`
- `frontend/src/morningCrawl/api.ts`
- `frontend/src/morningCrawl/types.ts`
- `backend/app/api/morning_crawl_routes.py`
- `backend/app/morning_crawl/service.py`

### 5. 管理权限

前端顶部导航提供“管理密码”登录。登录成功后解锁管理能力，状态持久化在前端。

后端配置项：

```ini
SYSTEM_ACCESS_PASSWORD=admin
```

受控能力包括：

- 系统晨抓配置、运行、停止。
- 站点发现待审核列表、审批、删除、提醒配置。
- CrawlMethod PATCH/DELETE。
- Prompt 工作室。
- 主分类维护。

相关文件：

- `frontend/src/auth.ts`
- `frontend/src/App.tsx`
- `backend/app/api/auth_routes.py`
- `backend/app/api/deps.py`

## 代码组织

```text
os-news-tracker/
├── README.md
├── AGENTS.md
├── CLAUDE.md
├── .env.example
├── docker-compose.yml
├── docker-compose.dev.yml
├── backend/
│   ├── Dockerfile
│   ├── Dockerfile.dev
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/versions/
│   ├── app/
│   │   ├── api/                 # FastAPI routers
│   │   ├── agent/               # Agent Crawl Handoff Chain, frozen
│   │   ├── discovery/           # 站点发现、DSL、审核、质量审计、方法运行
│   │   ├── extract/             # 内容提取协议和 Scrapling 实现
│   │   ├── fetchers/            # RSS/page/search/json/agent fetchers
│   │   ├── llm/                 # OpenAI-compatible LLM client
│   │   ├── mail/                # 邮件预览、渲染、发送
│   │   ├── morning_crawl/       # 系统晨抓
│   │   ├── processing/          # normalize/dedup/relevance/enrich/reason
│   │   ├── search/              # 搜索 Provider
│   │   ├── sources/             # 种子数据源和探测工具
│   │   ├── auth.py
│   │   ├── categories.py
│   │   ├── config.py
│   │   ├── db.py
│   │   ├── entry.py
│   │   ├── enums.py
│   │   ├── models.py
│   │   ├── pipeline.py
│   │   ├── repository.py
│   │   ├── run_logs.py
│   │   ├── scheduler.py
│   │   └── schemas.py
│   └── tests/
├── frontend/
│   ├── Dockerfile
│   ├── Dockerfile.dev
│   ├── nginx.conf
│   ├── package.json
│   └── src/
│       ├── api/
│       ├── components/
│       ├── discovery/
│       ├── hooks/
│       ├── mail/
│       ├── morningCrawl/
│       ├── pages/
│       ├── App.tsx
│       ├── auth.ts
│       └── types.ts
├── db/
└── docs/
```

## 后端核心文件说明

| 文件或目录 | 作用 |
| --- | --- |
| `backend/app/entry.py` | 应用启动入口；执行 Alembic 迁移，按开关启动新闻源调度、邮件调度、晨抓调度。 |
| `backend/app/api/main.py` | FastAPI app factory；注册全部 router。 |
| `backend/app/config.py` | 环境变量配置。 |
| `backend/app/models.py` | SQLAlchemy ORM；包括 Source、Item、Tag、CrawlMethod、Mail、MorningCrawl 等表。 |
| `backend/app/schemas.py` | Pydantic API 契约。 |
| `backend/app/pipeline.py` | 标准新闻流水线。 |
| `backend/app/run_logs.py` | 内存环形运行日志，供发现抓取、晨抓等模块共享。 |
| `backend/app/scheduler.py` | APScheduler 任务注册；新闻源、邮件、晨抓互相独立。 |
| `backend/app/discovery/runner.py` | 执行 CrawlMethod 并入库。 |
| `backend/app/mail/service.py` | 邮件模板、预览、发送、定时任务业务逻辑。 |
| `backend/app/morning_crawl/service.py` | 系统晨抓配置、执行和状态维护。 |

## 前端核心文件说明

| 文件或目录 | 作用 |
| --- | --- |
| `frontend/src/App.tsx` | 顶部导航、管理登录、路由。 |
| `frontend/src/pages/HomePage.tsx` | 新闻流页面，包含邮件任务中心和晨抓入口。 |
| `frontend/src/pages/DiscoveryPage.tsx` | 站点发现页面。 |
| `frontend/src/api/client.ts` | 通用后端 API 调用。 |
| `frontend/src/mail/api.ts` | 邮件任务中心 API 和筛选快照转换。 |
| `frontend/src/morningCrawl/api.ts` | 系统晨抓 API。 |
| `frontend/src/components/CrawlMethodList.tsx` | CrawlMethod 正式库。 |
| `frontend/src/components/CrawlMethodReviewList.tsx` | 待审核方式列表。 |
| `frontend/src/components/MailTaskCenter.tsx` | 邮件任务中心弹窗。 |
| `frontend/src/components/MorningCrawlModal.tsx` | 系统晨抓弹窗。 |

## API 速览

| 路由前缀 | 说明 |
| --- | --- |
| `/items`, `/facets`, `/items/{id}` | 新闻流查询、详情、推荐理由。 |
| `/run-logs` | 通用运行日志。 |
| `/auth` | 用户接口和系统管理密码登录。 |
| `/sources` | 传统 source 探测、创建、删除。 |
| `/sources/agent`, `/crawl-sources` | Agent Crawl source 和兼容入口。 |
| `/discovery` | 站点发现、CrawlMethod、审核、质量审计、Prompt、主分类。 |
| `/mail` | 邮件模板、预览、立即发送、定时任务、发送日志。 |
| `/system-morning-crawl` | 系统晨抓配置、立即执行、停止、运行记录。 |

关键 discovery 端点：

- `POST /discovery/run`
- `POST /discovery/multi-run`
- `GET /discovery/runs`
- `GET /discovery/methods`
- `GET /discovery/methods/review-pending`
- `POST /discovery/methods/review/approve`
- `POST /discovery/methods/review/delete`
- `POST /discovery/methods/{id}/fetch`
- `POST /discovery/methods/{id}/fetch/cancel`
- `GET/POST/PUT/DELETE /discovery/prompt-sets`
- `GET/POST/PUT/DELETE /discovery/main-categories`

## 配置

配置由 `pydantic-settings` 从 `.env` 加载。Docker Compose 会把根目录 `.env` 注入后端容器。

### 必要配置

```ini
POSTGRES_DB=osnews
POSTGRES_USER=osnews_app
POSTGRES_PASSWORD=change-me
DATABASE_URL=postgresql+psycopg://osnews_app:change-me@db:5432/osnews
LLM_BASE_URL=https://your-llm-gateway.example.com/v1
LLM_API_KEY=replace-me
LLM_MODEL=replace-me
SYSTEM_ACCESS_PASSWORD=admin
```

### LLM 与抓取

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_BASE_URL` | `http://llm.invalid/v1` | OpenAI-compatible `/v1` 网关。 |
| `LLM_API_KEY` | `test-key` | LLM API key。 |
| `LLM_MODEL` | `test-model` | Chat completion 模型名。 |
| `LLM_MAX_CONCURRENCY` | `4` | LLM 并发上限。 |
| `DISCOVERY_MAX_CONCURRENT_RUNS` | `3` | 智能探查全局并发上限，超过后返回 429 提醒等待。 |
| `SEARCH_PROVIDER` | `none` | 搜索 Provider，当前可接 `internal`。 |
| `FETCH_USER_AGENT` | `os-news-tracker/0.1 (+internal)` | 抓取 User-Agent。 |
| `FETCH_PER_HOST_DELAY_SECONDS` | `2.0` | 同 host 抓取间隔。 |
| `MANUAL_FETCH_MAX_WORKERS` | `4` | 手动方法抓取 worker 数。 |
| `WECHAT_MP_COOKIE` | 空 | 微信公众号历史抓取 cookie。 |
| `WECHAT_MP_TOKEN` | 空 | 微信公众号历史抓取 token。 |
| `WECHAT_MP_PROFILE_NAME` | `wechat_mp_default` | 微信档案名。 |

### 邮件

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MAIL_PROVIDER` | `tof4` | 默认邮件通道：`tof4` 或 `smtp`。 |
| `TOF4_PAASID` | 空 | TOF4 API paasid。 |
| `TOF4_TOKEN` | 空 | TOF4 API token。 |
| `TOF4_URL` | 空 | TOF4 API 地址。 |
| `TOF4_FROM_EMAIL` | 空 | TOF4 发件邮箱，空则回退 SMTP 发件邮箱。 |
| `TOF4_FROM_NAME` | 空 | TOF4 发件名称。 |
| `SMTP_HOST` | `localhost` | SMTP host。 |
| `SMTP_PORT` | `25` | SMTP port。 |
| `SMTP_USERNAME` | 空 | SMTP 用户名。 |
| `SMTP_PASSWORD` | 空 | SMTP 密码。 |
| `SMTP_FROM_EMAIL` | `no-reply@example.com` | SMTP 发件邮箱。 |
| `SMTP_FROM_NAME` | `OS News Tracker` | SMTP 发件名称。 |
| `SMTP_USE_TLS` | `0` | 是否 STARTTLS。 |
| `SMTP_USE_SSL` | `0` | 是否 SSL。 |

### 调度开关

这些开关在 `entry.py` 中读取：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ENABLE_SCHEDULER` | `1` | 传统新闻源 cron 调度。测试和本地开发通常设为 `0`。 |
| `RUN_SEED` | `0` | 启动时导入 `backend/app/sources/seed_sources.yaml`。 |
| `RUN_STARTUP_BACKFILL` | `0` | 启动时执行回填。 |
| `ENABLE_MAIL_SCHEDULER` | `0` | 邮件定时任务调度。 |
| `ENABLE_MORNING_CRAWL_SCHEDULER` | `0` | 系统晨抓调度。 |

开发 Compose 默认：

- `ENABLE_SCHEDULER=0`
- `ENABLE_MAIL_SCHEDULER=1`
- `ENABLE_MORNING_CRAWL_SCHEDULER=1`
- `RUN_STARTUP_BACKFILL=0`

### `.env` 修改后的生效方式

后端容器读取 `.env`。修改 `.env` 后需要重建或重启后端容器，推荐：

```bash
docker compose -f docker-compose.dev.yml up -d --force-recreate backend
```

生产 Compose：

```bash
docker compose up -d --force-recreate backend
```

## 部署

### 生产部署

生产 Compose 使用：

- PostgreSQL 16，数据卷 `pgdata`。
- FastAPI backend，端口 `8000`。
- React 静态文件经 Nginx 提供，宿主机端口 `8080`。

```bash
cp .env.example .env
# 编辑 .env
docker compose up --build -d
docker compose ps
```

访问：

- 前端：http://localhost:8080
- 后端 API：http://localhost:8000
- Swagger：http://localhost:8000/docs
- PostgreSQL：宿主机 `localhost:15432`

### 开发部署

开发 Compose 使用源码挂载和热更新：

- backend：`uvicorn app.entry:app --reload`，端口 `8000`。
- frontend：`vite --host 0.0.0.0 --port 5173`，端口 `5173`。
- frontend dev server 代理 `/items`、`/discovery`、`/mail`、`/auth` 等 API 到 backend 容器。

```bash
cp .env.example .env
# 编辑 .env
docker compose -f docker-compose.dev.yml up --build -d
docker compose -f docker-compose.dev.yml logs -f backend
```

访问：

- 前端：http://localhost:5173
- 后端 API：http://localhost:8000
- Swagger：http://localhost:8000/docs

### 本地非 Docker 开发

后端：

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ENABLE_SCHEDULER=0 uvicorn app.entry:app --reload --port 8000
```

前端：

```bash
cd frontend
npm ci
npm run dev
```

PostgreSQL 可继续使用 Compose 中的 `db` 服务：

```bash
docker compose -f docker-compose.dev.yml up -d db
```

## 数据库迁移

应用启动时会自动执行 Alembic `upgrade head`。如果需要手动生成或应用迁移：

```bash
cd backend
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

不要用 `Base.metadata.create_all()` 绕过迁移。当前 schema 依赖 Alembic 版本记录。

## 常用命令

### Docker

```bash
docker compose up --build -d
docker compose logs -f backend
docker compose restart backend
docker compose down
docker compose down -v
```

开发环境：

```bash
docker compose -f docker-compose.dev.yml up --build -d
docker compose -f docker-compose.dev.yml logs -f
docker compose -f docker-compose.dev.yml up -d --force-recreate backend
```

### 后端测试

```bash
cd backend
ENABLE_SCHEDULER=0 python -m pytest tests/ -v
ENABLE_SCHEDULER=0 python -m pytest tests/unit/ -v
```

### 前端测试与构建

```bash
cd frontend
npm run build
npm run test
npx vitest run src/pages/HomePage.test.tsx
```

容器中运行：

```bash
docker compose -f docker-compose.dev.yml exec -T frontend npm run build
docker compose -f docker-compose.dev.yml exec -T backend env ENABLE_SCHEDULER=0 python -m pytest tests/unit -q
```

## 运行日志

系统使用 `backend/app/run_logs.py` 维护内存环形日志，接口为：

```text
GET /run-logs?after_id=0&limit=200
```

当前使用方：

- 站点发现运行日志。
- CrawlMethod 抓取日志。
- 微信文章补抓请求/响应/解析耗时。
- 系统晨抓执行日志。

日志是运行期内存态，不是审计表。需要持久历史时应看对应业务表，例如 `crawl_method_runs`、`morning_crawl_runs`、`mail_deliveries`。

## 数据表概要

| 表 | 说明 |
| --- | --- |
| `sources` | 传统数据源和 discovery source。 |
| `items` | 富化后的新闻条目。 |
| `tags`, `tag_aliases`, `item_tags` | 技术热点、主分类标签与别名。 |
| `entities`, `item_entities` | 实体抽取结果。 |
| `crawl_methods` | 站点发现产出的 DSL 抓取方式。 |
| `crawl_method_runs` | 单个 CrawlMethod 运行记录。 |
| `site_discovery_runs` | 站点发现任务轨迹和结果。 |
| `crawl_method_review_reminder_configs` | 待审核方式邮件提醒配置。 |
| `mail_templates`, `mail_schedules`, `mail_deliveries` | 邮件模板、定时任务和发送记录。 |
| `morning_crawl_config`, `morning_crawl_runs`, `morning_crawl_run_methods` | 系统晨抓配置和执行明细。 |
| `main_categories` | 可管理主分类。 |
| `discovery_prompt_sets` | Prompt 工作室配置。 |
| `security_advisories`, `product_lifecycles`, `image_releases`, `compatibility_entries` | 结构化情报流预留/部分实现表。 |

## 注意事项

- `backend/app/agent/` 和 `backend/app/fetchers/agent_crawl.py` 是冻结模块，除非明确需求，不要修改。
- 微信公众号历史抓取依赖 cookie/token，且容易受微信侧限制；更新 `.env` 后要重建后端容器。
- 公众号正文补抓做了截断和解析优化，但仍应控制并发与补抓上限。
- 邮件 24h/7d/30d 筛选是相对窗口，发送或预览时按当前时刻计算。
- `SYSTEM_ACCESS_PASSWORD` 默认是 `admin`，部署时必须修改。
- 生产环境建议不要暴露 PostgreSQL `15432` 到公网。
- 如果修改 prompt、分类或审核策略，优先通过现有 Prompt 工作室和配置表验证，再改硬编码逻辑。

## 参考入口

- 前端主入口：[frontend/src/App.tsx](frontend/src/App.tsx)
- 后端应用入口：[backend/app/entry.py](backend/app/entry.py)
- API 注册：[backend/app/api/main.py](backend/app/api/main.py)
- ORM 模型：[backend/app/models.py](backend/app/models.py)
- 站点发现：[backend/app/discovery/](backend/app/discovery/)
- 邮件中心：[backend/app/mail/](backend/app/mail/)
- 系统晨抓：[backend/app/morning_crawl/](backend/app/morning_crawl/)
