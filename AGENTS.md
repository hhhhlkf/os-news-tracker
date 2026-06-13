# OS News Tracker — AGENTS.md

## Project Overview

技术新闻追踪 Agent（OS News Tracker）— 自动从多类来源采集技术情报，用 LLM 整理归纳，汇总到可查询的 React 网页。支持分面筛选、时间范围过滤、排序切换和手动新闻采集运行控制。

**Two data streams:**
1. **News stream** — RSS + page monitor + keyword search → LLM enrichment (category, sub-tags, entities, summary)
2. **Structured stream** — Security advisories/CVE, lifecycle/EOL, image compatibility → direct parse to typed tables (no LLM). Adapters planned, not yet implemented.

**Pipeline:** `fetch → normalize → [relevance-filter] → dedup → enrich → store`

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0 + Alembic, PostgreSQL, APScheduler |
| Frontend | React 18, Vite, TypeScript, TanStack Query |
| Ingestion | feedparser, Scrapling, httpx |
| AI/LLM | Configurable OpenAI-compatible client (internal LLM gateway) |
| Testing | pytest, pytest-asyncio, respx, vitest |
| Deployment | Docker Compose |

## Project Structure

```
os-news-tracker/
├── CLAUDE.md
├── AGENTS.md                         # This file
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/versions/
│   ├── app/
│   │   ├── config.py                 # pydantic-settings (env-driven)
│   │   ├── db.py                     # SQLAlchemy engine + session factory
│   │   ├── models.py                 # ORM models (Base, Source, Item, Tag, Entity, SecurityAdvisory, ProductLifecycle, etc.)
│   │   ├── schemas.py                # Pydantic contracts
│   │   ├── enums.py                  # InfoType, Importance, SourceType, ItemStatus, EntityType, TagKind
│   │   ├── entry.py                  # App entrypoint: create_app() + startup hooks (create tables, seed, scheduler)
│   │   ├── pipeline.py               # News-stream orchestration
│   │   ├── manual_news_run.py        # Target-driven manual news run (thread-safe controller + runner)
│   │   ├── scheduler.py              # APScheduler: per-source cron jobs
│   │   ├── repository.py             # DB queries (idempotent writes)
│   │   ├── api/
│   │   │   ├── main.py               # FastAPI app factory (CORS middleware + router)
│   │   │   ├── routes.py             # GET /items (sort+filter), GET /items/{id}, GET /facets, GET/POST /news-run
│   │   │   └── deps.py               # get_session dependency
│   │   ├── sources/
│   │   │   ├── registry.py           # seed_sources_from_yaml() — idempotent YAML→DB seeding
│   │   │   └── seed_sources.yaml     # 84 source definitions (23 RSS, 46 page_monitor, 15 api)
│   │   ├── fetchers/
│   │   │   ├── base.py               # Fetcher Protocol
│   │   │   ├── rss.py
│   │   │   ├── page_monitor.py
│   │   │   └── search.py
│   │   ├── extract/
│   │   │   ├── base.py               # ContentExtractor Protocol
│   │   │   └── scrapling_extractor.py
│   │   ├── search/
│   │   │   ├── base.py               # SearchProvider Protocol
│   │   │   └── internal_gateway.py
│   │   ├── processing/
│   │   │   ├── normalizer.py         # RawItem → NormalizedItem (pure function)
│   │   │   ├── dedup.py              # url_hash + simhash + near-duplicate match
│   │   │   ├── relevance.py          # LLM relevance gate
│   │   │   └── enricher.py           # LLM enrich → EnrichedFields
│   │   └── llm/
│   │       └── client.py             # OpenAI-compatible client + cache
│   └── tests/
│       ├── conftest.py
│       ├── fixtures/
│       ├── unit/                     # 15 test files (config, dedup, enricher, extract, llm, models, normalizer, fetchers, etc.)
│       │   ├── test_manual_news_run.py
│       │   └── test_source_link_dedup.py
│       └── integration/
│           ├── test_api.py           # CRUD + sorting + time filtering (89 tests total)
│           ├── test_api_source_dedup.py
│           ├── test_pipeline.py
│           ├── test_registry.py
│           └── test_repository.py
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── types.ts                  # ItemSummary, ItemDetail, Facets, ManualNewsRunStatus, etc.
│       ├── demoData.ts               # Demo/offline fallback (4 items, supports sort + time filter)
│       ├── api/client.ts             # fetchItems, fetchItemDetail, fetchFacets, news run APIs, ApiError
│       ├── components/
│       │   ├── ItemList.tsx          # Paginated list (loading/empty/error states) + sortBy prop
│       │   ├── ItemCard.tsx          # Clickable row: badges + title + date (or "入库" label)
│       │   ├── ItemDetail.tsx        # Slide-out detail panel (7 sections, deduped source links)
│       │   ├── FacetSidebar.tsx      # 4-group filter: category/type/importance + time presets
│       │   ├── ImportanceBadge.tsx   # Color-coded: 高=red, 中=yellow, 低=gray
│       │   ├── InfoTypeBadge.tsx     # Neutral outlined badge
│       │   ├── NewsRunControl.tsx    # Manual news run: start/stop, time range, live stats
│       │   └── TimeRangePicker.tsx   # Relative vs absolute time range selector
│       └── pages/
│           ├── HomePage.tsx          # Search + sort dropdown + sidebar + list + overlay + run control
│           ├── homeData.ts           # Demo/live mode resolution + filterDemoItems (sort + time range)
│           ├── homeData.test.ts
│           └── newsRunControl.test.ts
└── docs/
    └── superpowers/
        ├── specs/2026-06-09-os-news-tracker-design.md
        ├── specs/2026-06-11-manual-news-run-control-design.md
        └── plans/
```

## Common Commands

### Backend (working directory: `backend/`)

```bash
# Run all tests (scheduler disabled for tests)
ENABLE_SCHEDULER=0 python -m pytest tests/ -v

# Run unit tests only
ENABLE_SCHEDULER=0 python -m pytest tests/unit/ -v

# Run a single test
ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_entry_import.py -v

# Dev server
uvicorn app.entry:app --reload --port 8000

# Create DB migration
alembic revision --autogenerate -m "description"
```

### Frontend (working directory: `frontend/`)

```bash
npm ci              # Install dependencies
npm run dev         # Dev server
npx vitest run      # Run tests
npm run build       # Type-check + production build
```

## Development Workflow

This project follows **Subagent-Driven Development (SDD)** via the Superpowers methodology:

1. **Implement** — Dispatch a fresh implementer subagent per task with full spec and context
2. **Spec Review** — Independent reviewer verifies implementation matches plan line-by-line
3. **Code Quality Review** — Reviewer checks architecture, error handling, type safety, tests
4. **Fix Loop** — Issues found → implementer fixes → re-review until approved

**Key rules:**
- One commit per task; amend commits for fixes (clean history)
- Spec compliance review MUST pass before code quality review
- TDD: red (failing test) → green (minimal implementation) → refactor
- Pure logic (normalizer, dedup) is isolated and unit-tested without I/O
- Fetchers, extractors, and search are behind Protocols — testable with fixtures, swappable

## Code Conventions

### Python (Backend)

- **Type annotations** on all function signatures
- **Pydantic v2** for data contracts; **pydantic-settings** for config
- **SQLAlchemy 2.0**: `Mapped` + `mapped_column` declarative style
- **Protocols** for pluggable components: `Fetcher`, `ContentExtractor`, `SearchProvider`
- **Pure functions** for processing: normalizer, dedup
- **Resource cleanup**: `try/finally` around DB sessions
- **Logging**: `logging.getLogger(__name__)` module-level loggers
- **Env vars**: `ENABLE_SCHEDULER` gates scheduler startup (default: `"1"`)
- **UTC datetime**: All datetimes are UTC-aware. `published_after`/`published_before` use `date.fromisoformat()` + `datetime(…, tzinfo=timezone.utc)`. Invalid dates return 422.
- **Sorting**: `SortBy = Literal["published_at", "fetched_at"]`, `SortDir = Literal["desc", "asc"]`. `published_at` sort uses `nullslast()` — items without publish date always appear last.
- **Time filtering**: `published_before` is inclusive (adds 1 day as upper bound). Both params nullable — omitted means no bound.

### TypeScript/React (Frontend)

- **Inline styles**: All components use `style` props (no CSS modules or Tailwind)
- **TanStack Query**: `useQuery` with `queryKey` + `queryFn` for all data fetching; 2s polling during active run states
- **ApiError pattern**: Custom error class from `api/client.ts`; handle with `instanceof ApiError`
- **Conditional rendering**: Null guards (`&&`), empty array guards (`.length > 0`), truthy checks for nullable fields
- **Type-safe facets**: `keyof Facets` for iterating facet groups (no `as any`)
- **Slide-out overlay**: Fixed-position panel, backdrop click-to-close, `stopPropagation` on panel
- **Sort dropdown**: `<select>` with `value:key` format in HomePage; `sortBy` prop through ItemList → ItemCard; "入库" label shown when sorting by `fetched_at`
- **Time filter presets**: 5 buttons (全部/24h/7d/30d/自定义); `useMemo` computes active preset; custom expands two `<input type="date">` fields; `daysAgo(n)` helper for offset computation
- **Demo data sync**: `filterDemoItems` mirrors backend filtering (time range, sort, null exclusion)

## Architecture Decisions

| Decision | Resolution |
|----------|-----------|
| Orchestration | **Deterministic pipeline + local LLM** (NOT autonomous agent loop) |
| Source types | 3 Fetcher types implemented: `rss`, `page_monitor`, `search`. `api` type (structured stream) planned |
| Source count | 84 sources in seed_sources.yaml: 23 RSS, 46 page_monitor, 15 api. 15 adapter names referenced |
| Extraction engine | Scrapling default + pluggable interface |
| Dedup strategy | url_hash + simhash + near-duplicate matching |
| Search | Internal search capability preferred; interface is pluggable |
| Manual news run | Target-driven, two-phase loop; thread-safe singleton; up to 3 expansion rounds on search sources |
| Time semantics | All UTC. `nullslast()` for null published_at. Inclusive `published_before`; invalid dates → 422 |
| Sorting | `published_at` (default) or `fetched_at`, asc/desc. Null published_at always last |
| V1 scope | Ingestion + two-stream processing + queryable web UI + manual news run. NO subscriptions, push, admin panel |
| Deployment | Internal network, with normal external internet access |

## Reference URLs

### Distribution Sources
| Distribution | URL |
|-------------|-----|
| Fedora | https://src.fedoraproject.org/ |
| SUSE | https://build.opensuse.org/project/show/openSUSE:Factory |
| Debian | https://salsa.debian.org/public · https://www.debian.org/distrib/packages |
| OpenEuler | https://gitee.com/src-openeuler |
| CentOS C9S | https://gitlab.com/redhat/centos-stream/rpms |
| Rocky Linux | https://git.rockylinux.org/staging/rpms |
| OpenCloudOS | https://git.opencloudos.tech/sources-stream/ |

### Security
- Red Hat CVE: https://access.redhat.com/security/cve/
- NVD: https://nvd.nist.gov/vuln/detail/

### Build Systems (Koji)
- Fedora: https://koji.fedoraproject.org/koji/
- CentOS: https://koji.mbox.centos.org/koji/
- CentOS Stream: https://kojihub.stream.centos.org/koji/
- OpenCloudOS: https://build.opencloudos.tech/koji/index

### Lifecycle & Compatibility
- Red Hat Lifecycle: https://access.redhat.com/support/policy/updates/errata/
- RHEL9 ABI Compatibility: https://access.redhat.com/articles/rhel9-abi-compatibility
