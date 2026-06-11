# OS News Tracker — CLAUDE.md

## Project Overview

An automated OS news intelligence tracker that collects technical news from multiple source types, enriches them with LLM-structured summaries (category, sub-tags, entities, impact), and presents results in a searchable React web UI with facet filtering and time-range retrieval.

**Two data streams:**

1. **News stream** — RSS + structured APIs + page monitoring + keyword search → LLM structured summary (category, sub-tags, entities, summary)
2. **Structured facts stream** — Security advisories/CVE, lifecycle/EOL, image releases → parsed directly into DB, no LLM

**Pipeline:** `fetch → normalize → [relevance-filter] → dedup → enrich → store`

## Tech Stack

| Layer | Technology |
|-------|-------------|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0 + Alembic, Postgres, APScheduler |
| Frontend | React 18, Vite, TypeScript, TanStack Query |
| Collection | feedparser, Scrapling, httpx |
| AI/LLM | Configurable OpenAI-compatible client (internal LLM gateway) |
| Testing | pytest, pytest-asyncio, respx, vitest |
| Deployment | Docker Compose |

## Project Structure

```
os-news-tracker/
├── CLAUDE.md                              # This file
├── AGENTS.md                              # Subagent config
├── README.md
├── docker-compose.yml                     # Production Docker Compose
├── docker-compose.dev.yml                 # Development Docker Compose (hot-reload)
├── backend/
│   ├── pyproject.toml                     # Python deps (FastAPI, SQLAlchemy, etc.)
│   ├── alembic.ini                        # DB migrations config
│   ├── Dockerfile.dev                     # Dev container
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/                      # Migration scripts
│   │       ├── ca363b702936_initial.py
│   │       └── b8f1a2c3d4e5_add_item_source_unique_constraint.py
│   ├── app/
│   │   ├── __init__.py
│   │   ├── config.py                      # Settings (env-driven, pydantic-settings)
│   │   ├── db.py                          # SQLAlchemy engine + SessionLocal
│   │   ├── models.py                      # ORM models (Source, Item, Tag, Entity, etc.)
│   │   ├── schemas.py                     # Pydantic contracts (requests, responses, internal)
│   │   ├── enums.py                       # InfoType, Importance, SourceType, ItemStatus, etc.
│   │   ├── entry.py                       # App entrypoint (create_app + startup hooks)
│   │   ├── pipeline.py                    # News-stream orchestration (fetch→store)
│   │   ├── manual_news_run.py             # Target-driven manual news run (controller + runner)
│   │   ├── scheduler.py                   # APScheduler wiring (per-source cron jobs)
│   │   ├── repository.py                  # DB read/write queries (idempotent writes)
│   │   ├── api/
│   │   │   ├── main.py                    # FastAPI app factory + CORS
│   │   │   ├── routes.py                  # /items, /items/{id}, /facets, /news-run
│   │   │   └── deps.py                    # DB session dependency
│   │   ├── sources/
│   │   │   ├── registry.py                # Source registry (seed YAML → DB, idempotent)
│   │   │   └── seed_sources.yaml          # Initial source definitions
│   │   ├── fetchers/
│   │   │   ├── base.py                    # Fetcher protocol + FetchResult
│   │   │   ├── rss.py                     # RssFetcher
│   │   │   ├── page_monitor.py            # PageMonitorFetcher
│   │   │   ├── search.py                  # SearchFetcher (keyword-based)
│   │   │   └── api.py                     # ApiFetcher (structured stream)
│   │   ├── extract/
│   │   │   ├── base.py                    # ContentExtractor protocol
│   │   │   └── scrapling_extractor.py     # Scrapling-based extractor (default)
│   │   ├── search/
│   │   │   ├── base.py                    # SearchProvider protocol
│   │   │   └── internal_gateway.py        # Placeholder impl
│   │   ├── processing/
│   │   │   ├── normalizer.py              # RawItem → NormalizedItem (pure)
│   │   │   ├── dedup.py                   # url_hash + simhash + near-dup
│   │   │   ├── relevance.py               # LLM relevance gate for search hits
│   │   │   └── enricher.py                # LLM enrich → EnrichedFields
│   │   ├── structured/
│   │   │   ├── schemas.py                 # AdvisoryRecord, LifecycleRecord, etc.
│   │   │   ├── repository.py              # Idempotent upsert for structured data
│   │   │   ├── pipeline.py                # Structured stream run path
│   │   │   └── adapters/
│   │   │       ├── base.py                # SourceAdapter protocol + registry
│   │   │       └── ubuntu_security.py
│   │   └── llm/
│   │       └── client.py                  # OpenAI-compatible client + cache
│   └── tests/
│       ├── conftest.py
│       ├── fixtures/                      # Saved RSS/HTML samples + LLM responses
│       ├── unit/
│       │   ├── test_config.py
│       │   ├── test_dedup.py
│       │   ├── test_enricher.py
│       │   ├── test_entry_import.py
│       │   ├── test_extract.py
│       │   ├── test_llm_client.py
│       │   ├── test_manual_news_run.py    # Time filter stats, consistency, timezone safety
│       │   ├── test_models.py
│       │   ├── test_normalizer.py
│       │   ├── test_page_monitor.py
│       │   ├── test_rss_fetcher.py
│       │   ├── test_scheduler.py
│       │   ├── test_schemas.py
│       │   ├── test_search_fetcher.py
│       │   ├── test_search_provider.py
│       │   └── test_source_link_dedup.py  # Idempotent merge_source_link, save_enriched
│       └── integration/
│           ├── test_api.py
│           ├── test_api_source_dedup.py   # API-level source link dedup + order stability
│           ├── test_pipeline.py
│           ├── test_registry.py
│           └── test_repository.py
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json / tsconfig.app.json / tsconfig.node.json
│   ├── index.html
│   ├── Dockerfile.dev                     # Dev container
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── App.css
│       ├── index.css
│       ├── types.ts                       # ItemSummary, ItemDetail, Facets, TimeFilterStats, etc.
│       ├── vite-env.d.ts
│       ├── demoData.ts                    # Demo/offline fallback data
│       ├── api/
│       │   └── client.ts                  # fetchItems, fetchItemDetail, fetchFacets + ApiError
│       ├── components/
│       │   ├── ItemList.tsx               # Paginated list with loading/empty/error states
│       │   ├── ItemCard.tsx               # Clickable list row (badges + title + date)
│       │   ├── ItemDetail.tsx             # Slide-out detail panel (7 sections + deduped source links)
│       │   ├── FacetSidebar.tsx           # Facet filter sidebar (3 groups: category/type/importance)
│       │   ├── ImportanceBadge.tsx        # Color-coded badge: high=red, medium=yellow, low=gray
│       │   ├── InfoTypeBadge.tsx          # Neutral outlined badge
│       │   ├── NewsRunControl.tsx         # Manual news run control panel (start/stop, time range, stats)
│       │   └── TimeRangePicker.tsx        # Relative vs absolute time range picker
│       └── pages/
│           ├── HomePage.tsx               # Top-level layout (search + sidebar + list + overlay + run control)
│           ├── homeData.ts                # Demo/live data mode resolution + filtering
│           ├── homeData.test.ts           # Tests for demo data mode
│           └── newsRunControl.test.ts     # Tests for NewsRunControl helpers
└── docs/
    ├── code-review-reference.md
    └── superpowers/
        ├── README.md
        ├── specs/
        │   ├── 2026-06-09-os-news-tracker-design.md          # Original V1 design doc
        │   └── 2026-06-11-manual-news-run-control-design.md  # Manual news run control design
        └── plans/
            ├── 2026-06-09-os-news-tracker-v1.md              # Implementation plan
            ├── 2026-06-09-os-news-tracker-v1-zh.md           # Implementation plan (Chinese)
            └── 2026-06-11-manual-news-run-control.md         # Manual news run control plan
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

# Run tests
npx vitest run

# Type-check + build
npm run build
```

## Development Workflow

This project uses **Subagent-Driven Development (SDD)**:

1. **Implementation** — Each task gets an independent implementer subagent (full task spec + context)
2. **Spec Review** — Spec compliance reviewer verifies implementation matches the plan line-by-line
3. **Code Quality Review** — Code quality reviewer checks architecture, error handling, type safety, tests
4. **Fix Loop** — Issues found during review are fixed by the implementer and re-reviewed

**Key rules:**

- One commit per task (iterate via `git commit --amend`)
- Spec review must pass before code quality review begins
- Pure logic (normalizer, dedup) uses isolated unit tests with no I/O dependencies
- Fetchers/extractors/search are all Protocol-based, pluggable and independently testable
- Follow TDD: write failing tests first → minimal implementation → verify pass

## Code Conventions

### Python (Backend)

- **Type annotations:** Full type annotations on all function signatures
- **Pydantic v2:** Data contracts use `pydantic.BaseModel`; config uses `pydantic-settings`
- **SQLAlchemy 2.0:** Use `Mapped` + `mapped_column` declarative mapping
- **Protocols:** Pluggable components (Fetcher, ContentExtractor, SearchProvider, SourceAdapter) defined as Protocol classes — no concrete dependencies
- **Pure functions:** normalizer, dedup are pure functions with no side effects, easy to test
- **Error handling:** Use `try/finally` to ensure resource release (e.g., DB sessions)
- **Logging:** Module-level logger via `logging.getLogger(__name__)`
- **Environment variables:** Configuration controlled via `ENABLE_SCHEDULER`, `DATABASE_URL`, etc.
- **UTC datetime:** All datetime values are UTC-aware. `_as_utc()` helper converts naive→UTC (assumed) and aware→UTC (converted). Frontend sends explicit `Z` suffix.
- **Idempotent writes:** Junction-table inserts use `_add_source_link_if_new()` pattern — check existence before insert, return bool. All junction writes (`merge_source_link`, `save_enriched`) are safe to call repeatedly.
- **Thread safety:** `ManualNewsRunController` uses `threading.Lock` protecting `_RuntimeState`; cooperative shutdown via `should_stop()`.

### TypeScript / React (Frontend)

- **Inline styles:** All components use inline `style` props (no CSS modules, no Tailwind)
- **@tanstack/react-query:** Data fetching uses `useQuery` (`queryKey` + `queryFn`); 2s polling during active run states
- **ApiError pattern:** `api/client.ts` exports `ApiError` class (with HTTP status); error handling uses `instanceof ApiError`
- **Conditional rendering:** null guards (`&&`), empty array guards (`.length > 0`), truthy checks on nullable fields
- **Type safety:** `keyof Facets` for type-safe facet group iteration
- **Slide-out overlay:** Fixed-position detail panel, background click to close, `stopPropagation` prevents close on panel click
- **UTC time:** `toAbsoluteDateTime()` emits explicit UTC ISO-8601 strings (`${date}T${time}Z`) — never uses `new Date()` with local-time strings
- **Defense-in-depth:** Frontend `Map`-based dedup on `source_links` as fallback (primary fix is in backend)

## Architecture Decisions

| Decision | Conclusion |
|----------|------------|
| Orchestration | **Deterministic pipeline + localized LLM** (not autonomous agent loops) |
| Collection sources | 4 Fetcher types: `rss` / `api` / `page_monitor` / `search` |
| Extraction engine | Scrapling default + pluggable interface (Firecrawl as future option) |
| Dedup strategy | url_hash + simhash + near-duplicate matching |
| Search | Internal search capability preferred; interface is pluggable |
| Manual news run | Target-driven, two-phase (collect→process) loop with up to 3 expansion rounds on search-type sources; thread-safe singleton controller |
| Time semantics | All times UTC. `_as_utc()` backend, explicit `Z` suffix frontend. Relative ranges are open-ended (no upper bound); absolute ranges are bounded both sides |
| Observability | `TimeFilterStats` (missing_published_at, before_start, after_end, matched) tallied per-source during collection, exposed in status, logs, and gap_reason |
| Source link dedup | `UniqueConstraint(item_id, source_id, url)` on `item_sources` + `_add_source_link_if_new()` idempotent writes + API-level dedup + frontend fallback |
| V1 scope | Collection + two-stream processing + searchable web UI; no subscriptions, push notifications, or admin backend |
| Deployment | Internal network, normal external internet access |

## Reference URLs

### Distribution References

| Distribution | Link |
|-------------|------|
| Fedora | https://src.fedoraproject.org/ |
| SUSE | https://build.opensuse.org/project/show/openSUSE:Factory |
| Debian | https://salsa.debian.org/public · https://www.debian.org/distrib/packages |
| OpenEuler | https://gitee.com/src-openeuler |
| CentOS C9S | https://gitlab.com/redhat/centos-stream/rpms |
| Rocky Linux | https://git.rockylinux.org/staging/rpms |
| OpenCloudOS | https://git.opencloudos.tech/sources-stream/ |

### Security

- Red Hat CVE: <https://access.redhat.com/security/cve/>
- NVD: <https://nvd.nist.gov/vuln/detail/>

### Koji / Build Systems

- Fedora Koji: <https://koji.fedoraproject.org/koji/>
- CentOS Koji: <https://koji.mbox.centos.org/koji/>
- CentOS Stream Koji: <https://kojihub.stream.centos.org/koji/>
- OpenCloudOS Koji: <https://build.opencloudos.tech/koji/index>

### Lifecycle

- Red Hat Lifecycle: <https://access.redhat.com/support/policy/updates/errata/>
- RHEL9 ABI Compatibility: <https://access.redhat.com/articles/rhel9-abi-compatibility>
