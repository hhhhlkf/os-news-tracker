# OS News Tracker — CLAUDE.md

## Project Overview

技术新闻追踪 Agent（OS News Tracker），自动从多类来源采集技术情报，用 LLM 整理归纳成统一的结构化总结，汇总到可查询的 React 网页上，支持按分类、标签、实体、时间检索。

**两条数据流：**
1. **新闻动态流** — RSS + 结构化 API + 固定页面监控 + 关键词搜索 → LLM 结构化摘要（分类、子标签、实体、摘要）
2. **结构化事实流** — 安全公告/CVE、生命周期/EOL、镜像适配 → 直接解析入库，不走 LLM

**流水线：** `fetch → normalize → [relevance-filter] → dedup → enrich → store`

## Tech Stack

| 层 | 技术 |
|---|------|
| 后端 | Python 3.11+, FastAPI, SQLAlchemy 2.0 + Alembic, Postgres, APScheduler |
| 前端 | React 18, Vite, TypeScript, TanStack Query |
| 采集 | feedparser, Scrapling, httpx |
| AI/LLM | 可配置的 OpenAI 兼容 client（司内 LLM 网关） |
| 测试 | pytest, pytest-asyncio, respx |
| 部署 | Docker Compose |

## Project Structure

```
os-news-tracker/
├── CLAUDE.md                         # This file
├── backend/
│   ├── pyproject.toml                # Python deps (FastAPI, SQLAlchemy, etc.)
│   ├── alembic.ini                   # DB migrations config
│   ├── alembic/versions/             # Migration scripts
│   ├── app/
│   │   ├── __init__.py
│   │   ├── config.py                 # Settings (env-driven, pydantic-settings)
│   │   ├── db.py                     # SQLAlchemy engine + SessionLocal
│   │   ├── models.py                 # ORM models (Source, Item, Tag, Entity, etc.)
│   │   ├── schemas.py                # Pydantic contracts
│   │   ├── enums.py                  # InfoType, Importance, SourceType, ItemStatus, etc.
│   │   ├── entry.py                  # App entrypoint (create_app + startup hooks)
│   │   ├── pipeline.py               # News-stream orchestration
│   │   ├── scheduler.py              # APScheduler wiring (per-source cron jobs)
│   │   ├── repository.py             # DB read/write queries
│   │   ├── api/
│   │   │   ├── main.py               # FastAPI app factory + CORS
│   │   │   ├── routes.py             # /items, /items/{id}, /facets
│   │   │   └── deps.py               # DB session dependency
│   │   ├── sources/
│   │   │   ├── registry.py           # Source registry (seed YAML → DB, idempotent)
│   │   │   └── seed_sources.yaml     # Initial source definitions
│   │   ├── fetchers/
│   │   │   ├── base.py               # Fetcher protocol + FetchResult
│   │   │   ├── rss.py                # RssFetcher
│   │   │   ├── page_monitor.py       # PageMonitorFetcher
│   │   │   ├── search.py             # SearchFetcher (keyword-based)
│   │   │   └── api.py                # ApiFetcher (structured stream)
│   │   ├── extract/
│   │   │   ├── base.py               # ContentExtractor protocol
│   │   │   └── scrapling_extractor.py
│   │   ├── search/
│   │   │   ├── base.py               # SearchProvider protocol
│   │   │   └── internal_gateway.py   # Placeholder impl
│   │   ├── processing/
│   │   │   ├── normalizer.py         # RawItem → NormalizedItem (pure)
│   │   │   ├── dedup.py              # url_hash + simhash + near-dup
│   │   │   ├── relevance.py          # LLM relevance gate for search hits
│   │   │   └── enricher.py           # LLM enrich → EnrichedFields
│   │   ├── structured/
│   │   │   ├── schemas.py            # AdvisoryRecord, LifecycleRecord, etc.
│   │   │   ├── repository.py         # Idempotent upsert for structured data
│   │   │   ├── pipeline.py           # Structured stream run path
│   │   │   └── adapters/
│   │   │       ├── base.py           # SourceAdapter protocol + registry
│   │   │       └── ubuntu_security.py
│   │   └── llm/
│   │       └── client.py             # OpenAI-compatible client + cache
│   └── tests/
│       ├── conftest.py
│       ├── fixtures/                 # Saved RSS/HTML samples + LLM responses
│       ├── unit/                     # Unit tests (pure logic, fetchers, etc.)
│       └── integration/              # Integration tests
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── types.ts                  # ItemSummary, ItemDetail, Facets, etc.
│       ├── api/
│       │   └── client.ts             # fetchItems, fetchItemDetail, fetchFacets + ApiError
│       ├── components/
│       │   ├── ItemList.tsx          # Paginated list with loading/empty/error states
│       │   ├── ItemCard.tsx          # Clickable list row (badges + title + date)
│       │   ├── ItemDetail.tsx        # Slide-out detail panel (7 sections)
│       │   ├── FacetSidebar.tsx      # Facet filter sidebar (3 groups: category/type/importance)
│       │   ├── ImportanceBadge.tsx   # Color-coded badge: 高=red, 中=yellow, 低=gray
│       │   └── InfoTypeBadge.tsx     # Neutral outlined badge
│       └── pages/
│           └── HomePage.tsx          # Top-level layout (search + sidebar + list + overlay)
└── docs/
    └── superpowers/
        ├── specs/
        │   └── 2026-06-09-os-news-tracker-design.md  # Design doc (V1/MVP scope)
        └── plans/
            └── 2026-06-09-os-news-tracker-v1.md      # Implementation plan (all tasks)
```

## Common Commands

### Backend

```bash
cd backend

# Run all tests
ENABLE_SCHEDULER=0 python -m pytest tests/ -v

# Run unit tests only
ENABLE_SCHEDULER=0 python -m pytest tests/unit/ -v

# Run a single test file
ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_entry_import.py -v

# Run the dev server
uvicorn app.entry:app --reload --port 8000

# Generate a new Alembic migration
alembic revision --autogenerate -m "description"
```

### Frontend

```bash
cd frontend

# Install deps
npm ci

# Dev server
npm run dev

# Type-check + build
npm run build
```

## Development Workflow

本项目使用 **Subagent-Driven Development (SDD)** 流程：

1. **实现阶段** — 为每个 Task 派发独立 implementer subagent（完整 task spec + 上下文）
2. **规范审查** — Spec compliance reviewer 逐行验证实现与计划一致
3. **代码质量审查** — Code quality reviewer 检查架构、错误处理、类型安全、测试
4. **修复循环** — 审查发现的问题由 implementer 修复后重新审查

**关键规则：**
- 每个 Task 一个 commit（通过 `git commit --amend` 修复后迭代）
- 规范审查必须通过后才进行代码质量审查
- 纯逻辑（normalizer, dedup）隔离单元测试，无 I/O 依赖
- Fetchers/extractors/search 均基于 Protocol，可插拔、可独立测试
- 遵循 TDD：先写失败测试 → 最小实现 → 验证通过

## Code Conventions

### Python（后端）
- **类型注解：** 所有函数签名使用完整类型注解
- **Pydantic v2：** 数据契约使用 `pydantic.BaseModel`，配置使用 `pydantic-settings`
- **SQLAlchemy 2.0：** 使用 `Mapped` + `mapped_column` 声明式映射
- **Protocols：** 可插拔组件（Fetcher, ContentExtractor, SearchProvider, SourceAdapter）均定义为 Protocol 类，不依赖具体实现
- **纯函数：** normalizer、dedup 为纯函数，无副作用，易测试
- **错误处理：** 使用 `try/finally` 确保资源释放（如 DB session）
- **日志：** 使用 `logging.getLogger(__name__)` 模块级 logger
- **环境变量：** 配置通过 `ENABLE_SCHEDULER` 等环境变量控制行为

### TypeScript / React（前端）
- **内联样式：** 所有组件使用 inline `style` props（无 CSS modules，无 Tailwind）
- **@tanstack/react-query：** 数据获取使用 `useQuery`（`queryKey` + `queryFn`）
- **ApiError 模式：** `api/client.ts` 导出 `ApiError` 类（含 HTTP status）；错误处理使用 `instanceof ApiError`
- **条件渲染：** null 守卫（`&&`），空数组守卫（`.length > 0`），nullable 字段的 truthy 检查
- **类型安全：** 使用 `keyof Facets` 进行类型安全的分面组迭代
- **Slide-out overlay：** 固定定位详情面板，背景点击关闭，`stopPropagation` 防止面板内点击关闭

## Architecture Decisions

| 决策 | 结论 |
|------|------|
| 编排方式 | **确定性流水线 + 局部 LLM**（非自主 Agent 循环） |
| 采集源 | 4 类 Fetcher：`rss` / `api` / `page_monitor` / `search` |
| 提取引擎 | Scrapling 默认 + 可插拔接口（Firecrawl 后续可选） |
| 去重策略 | url_hash + simhash + 近重复匹配 |
| 搜索 | 优先司内联网能力，接口可插拔 |
| V1 范围 | 采集 + 两条流处理 + 可查询网页；不含订阅、推送、管理后台 |
| 部署 | 公司内网，可正常访问外网 |

## Reference URLs

### 发行版参考
| 发行版 | 链接 |
|--------|------|
| Fedora | https://src.fedoraproject.org/ |
| SUSE | https://build.opensuse.org/project/show/openSUSE:Factory |
| Debian | https://salsa.debian.org/public · https://www.debian.org/distrib/packages |
| OpenEuler | https://gitee.com/src-openeuler |
| CentOS C9S | https://gitlab.com/redhat/centos-stream/rpms |
| Rocky Linux | https://git.rockylinux.org/staging/rpms |
| OpenCloudOS | https://git.opencloudos.tech/sources-stream/ |

### 安全相关
- Red Hat CVE: https://access.redhat.com/security/cve/
- NVD 漏洞库: https://nvd.nist.gov/vuln/detail/

### Koji / 构建系统
- Fedora Koji: https://koji.fedoraproject.org/koji/
- CentOS Koji: https://koji.mbox.centos.org/koji/
- CentOS Stream Koji: https://kojihub.stream.centos.org/koji/
- OpenCloudOS Koji: https://build.opencloudos.tech/koji/index

### 生命周期
- Red Hat 生命周期: https://access.redhat.com/support/policy/updates/errata/
- RHEL9 ABI 兼容性: https://access.redhat.com/articles/rhel9-abi-compatibility
