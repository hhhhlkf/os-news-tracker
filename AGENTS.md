# OS News Tracker — AGENTS.md

## Project Overview

技术新闻追踪 Agent（OS News Tracker）— 自动从多类来源采集技术情报，用 LLM 整理归纳，汇总到可查询的 React 网页。

**Two data streams:**
1. **News stream** — RSS + structured APIs + page monitor + keyword search → LLM enrichment (category, sub-tags, entities, summary)
2. **Structured stream** — Security advisories/CVE, lifecycle/EOL, image compatibility → direct parse to typed tables (no LLM)

**Pipeline:** `fetch → normalize → [relevance-filter] → dedup → enrich → store`

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0 + Alembic, PostgreSQL, APScheduler |
| Frontend | React 18, Vite, TypeScript, TanStack Query |
| Ingestion | feedparser, Scrapling, httpx |
| AI/LLM | Configurable OpenAI-compatible client (internal LLM gateway) |
| Testing | pytest, pytest-asyncio, respx |
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
│   │   ├── models.py                 # ORM models (Base, Source, Item, Tag, Entity, etc.)
│   │   ├── schemas.py                # Pydantic contracts
│   │   ├── enums.py                  # InfoType, Importance, SourceType, ItemStatus, EntityType, TagKind
│   │   ├── entry.py                  # App entrypoint: create_app() + startup hooks (create tables, seed, scheduler)
│   │   ├── pipeline.py               # News-stream orchestration
│   │   ├── scheduler.py              # APScheduler: per-source cron jobs
│   │   ├── repository.py             # DB queries
│   │   ├── api/
│   │   │   ├── main.py               # FastAPI app factory (CORS middleware + router)
│   │   │   ├── routes.py             # GET /items, GET /items/{id}, GET /facets
│   │   │   └── deps.py               # get_session dependency
│   │   ├── sources/
│   │   │   ├── registry.py           # seed_sources_from_yaml() — idempotent YAML→DB seeding
│   │   │   └── seed_sources.yaml     # Initial source config
│   │   ├── fetchers/
│   │   │   ├── base.py               # Fetcher Protocol
│   │   │   ├── rss.py
│   │   │   ├── page_monitor.py
│   │   │   ├── search.py
│   │   │   └── api.py
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
│   │   ├── structured/
│   │   │   ├── schemas.py            # AdvisoryRecord, LifecycleRecord, ImageRecord, CompatibilityRecord
│   │   │   ├── repository.py         # Idempotent upsert
│   │   │   ├── pipeline.py
│   │   │   └── adapters/
│   │   │       ├── base.py           # SourceAdapter Protocol + registry
│   │   │       └── ubuntu_security.py
│   │   └── llm/
│   │       └── client.py             # OpenAI-compatible client + cache
│   └── tests/
│       ├── conftest.py
│       ├── fixtures/
│       ├── unit/                     # 14+ test files
│       └── integration/
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── types.ts
│       ├── api/client.ts             # fetchItems, fetchItemDetail, fetchFacets, ApiError
│       ├── components/
│       │   ├── ItemList.tsx          # Paginated list (loading/empty/error states)
│       │   ├── ItemCard.tsx          # Clickable row: badges + title + date
│       │   ├── ItemDetail.tsx        # Slide-out detail panel (7 sections)
│       │   ├── FacetSidebar.tsx      # 3-group facet filter (category/type/importance)
│       │   ├── ImportanceBadge.tsx   # Color-coded: 高=red, 中=yellow, 低=gray
│       │   └── InfoTypeBadge.tsx     # Neutral outlined badge
│       └── pages/
│           └── HomePage.tsx          # Search + sidebar + list + slide-out overlay
└── docs/
    └── superpowers/
        ├── specs/2026-06-09-os-news-tracker-design.md
        └── plans/2026-06-09-os-news-tracker-v1.md
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
- **Protocols** for pluggable components: `Fetcher`, `ContentExtractor`, `SearchProvider`, `SourceAdapter`
- **Pure functions** for processing: normalizer, dedup
- **Resource cleanup**: `try/finally` around DB sessions
- **Logging**: `logging.getLogger(__name__)` module-level loggers
- **Env vars**: `ENABLE_SCHEDULER` gates scheduler startup (default: `"1"`)

### TypeScript/React (Frontend)

- **Inline styles**: All components use `style` props (no CSS modules or Tailwind)
- **TanStack Query**: `useQuery` with `queryKey` + `queryFn` for all data fetching
- **ApiError pattern**: Custom error class from `api/client.ts`; handle with `instanceof ApiError`
- **Conditional rendering**: Null guards (`&&`), empty array guards (`.length > 0`), truthy checks for nullable fields
- **Type-safe facets**: `keyof Facets` for iterating facet groups (no `as any`)
- **Slide-out overlay**: Fixed-position panel, backdrop click-to-close, `stopPropagation` on panel

## Architecture Decisions

| Decision | Resolution |
|----------|-----------|
| Orchestration | **Deterministic pipeline + local LLM** (NOT autonomous agent loop) |
| Source types | 4 Fetcher types: `rss`, `api`, `page_monitor`, `search` |
| Extraction engine | Scrapling default + pluggable interface |
| Dedup strategy | url_hash + simhash + near-duplicate matching |
| Search | Internal search capability preferred; interface is pluggable |
| V1 scope | Ingestion + two-stream processing + queryable web UI. NO subscriptions, push, admin panel |
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
| TencentOS | http://qa.mirrors.tlinux.woa.com/tlinux/3.3/isos/x86_64/ |

### Security
- Red Hat CVE: https://access.redhat.com/security/cve/
- NVD: https://nvd.nist.gov/vuln/detail/

### Build Systems (Koji)
- Fedora: https://koji.fedoraproject.org/koji/
- CentOS: https://koji.mbox.centos.org/koji/
- CentOS Stream: https://kojihub.stream.centos.org/koji/
- OpenCloudOS: https://build.opencloudos.tech/koji/index
- CCLinux: https://koji.cclinux.org/koji/index

### Lifecycle & Compatibility
- Red Hat Lifecycle: https://access.redhat.com/support/policy/updates/errata/
- RHEL9 ABI Compatibility: https://access.redhat.com/articles/rhel9-abi-compatibility

### Package Lookup
- Fedora Lookaside: https://src.fedoraproject.org/lookaside/pkgs/
- CentOS Lookaside: https://git.centos.org/sources/
- EPEL8: https://rpmfind.net/linux/RPM/epel/8/x86_64/Packages/
- Tsinghua Mirror (CentOS): https://mirrors.tuna.tsinghua.edu.cn/centos-vault/

### RPM Building
- Fedora Wiki (How to build RPM): https://fedoraproject.org/wiki/How_to_create_an_RPM_package/zh-cn

### CentOS Infrastructure
- CentOS Wiki Sources: https://wiki.centos.org/zh/Sources
- CentOS Git: https://git.centos.org/
- C9S Mirror Manager: https://admin.fedoraproject.org/mirrormanager/mirrors/CentOS/9-stream
- MQTT Updates: https://wiki.centos.org/zh/Sources

### OpenCloudOS Stream
- Build Stream: https://build.stream.opencloudos.tech/
- Gitee: https://gitee.com/organizations/opencloudos-stream/projects
- ISO Downloads: https://testing.mirrors.opencloudos.tech/opencloudos/9.0/isos/x86_64/
- Images: https://testing.mirrors.opencloudos.tech/opencloudos/9.0/images/x86_64/
