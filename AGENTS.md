# OS News Tracker — AGENTS.md

## Project Overview

群组化技术新闻情报平台（OS News Tracker）— 支持多用户、群组订阅、AI 智能爬取、个性化推荐评分、趋势总结的 OS 技术情报系统。

**Three-tier role model:** `system_admin` → `group_admin`（群内）→ `subscriber`（群内 `member`）

**Two data streams:**
1. **News stream** — RSS + page monitor + keyword search + agent_crawl → LLM enrichment
2. **Structured stream** — CVE/EOL/image → direct parse (planned)

**Pipeline:** `fetch → normalize → [relevance-filter] → dedup → enrich → store`
**Agent crawl pipeline (Handoff Chain):** `PlanAgent → CrawlDAG → QualityWorkerPool → SummaryWorkerPool`

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0 + Alembic, PostgreSQL, APScheduler |
| Auth | python-jose[cryptography], passlib[bcrypt] |
| Frontend | React 19, Vite, TypeScript, TanStack Query, react-router-dom |
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
│   │   ├── auth.py                   # JWT helpers: create_access_token, verify_access_token, hash_password
│   │   ├── log_stream.py             # SSE ring buffer + SseLogHandler
│   │   ├── fetchers/
│   │   │   ├── base.py, rss.py, page_monitor.py, search.py
│   │   │   └── agent_crawl.py        # AgentCrawlFetcher — Handoff Chain orchestrator
│   │   ├── agent/                    # V2: Handoff Chain stages
│   │   │   ├── plan_agent.py         # PlanAgent (Pre-Act + DFSDT)
│   │   │   ├── crawl_dag.py          # CrawlDAG (async parallel, no LLM)
│   │   │   ├── quality_pool.py       # QualityWorkerPool (Critic)
│   │   │   ├── summary_pool.py       # SummaryWorkerPool (Generator)
│   │   │   ├── site_memory.py        # SiteMemory (keep 7d TTL, discard permanent)
│   │   │   └── schemas.py            # CrawlPlan, RawPage, QualifiedPage, AgentItem
│   │   ├── groups/                   # V2: group permission + feed filter
│   │   │   ├── permissions.py        # check_group_access, is_group_admin
│   │   │   └── feed_filter.py        # passes_group_filter (OR logic across groups)
│   │   ├── extract/, search/         # ContentExtractor + SearchProvider protocols
│   │   ├── processing/
│   │   │   ├── normalizer.py, dedup.py, relevance.py, enricher.py
│   │   │   ├── scorer.py             # V2: compute_fast_score + ScoringAgent
│   │   │   ├── profile_advisor.py    # V2: ProfileAdvisor Agent
│   │   │   └── digest_agent.py       # V2: DigestAgent 3-step chain
│   │   ├── api/
│   │   │   ├── main.py, routes.py, deps.py
│   │   │   ├── auth_routes.py        # /auth/*
│   │   │   ├── profile_routes.py     # /users/me/profile
│   │   │   ├── digest_routes.py      # /digest
│   │   │   ├── agent_routes.py       # /sources/agent
│   │   │   ├── admin_group_routes.py # /admin/groups
│   │   │   ├── group_routes.py       # /groups, /groups/{id}/*
│   │   │   └── group_digest_routes.py # /groups/{id}/digest/*
│   │   └── llm/client.py
│   └── tests/unit/ + integration/
├── frontend/src/
│   ├── App.tsx                       # BrowserRouter + RequireAuth routes
│   ├── auth.ts                       # Token storage + authHeaders()
│   ├── index.css                     # CSS design tokens
│   ├── api/client.ts                 # All API calls
│   ├── hooks/useLogStream.ts         # EventSource SSE hook
│   ├── components/
│   │   ├── ItemCard.tsx              # Left-border score badge (teal/amber/slate)
│   │   ├── LogPanel.tsx              # Real-time log panel (JetBrains Mono, dark)
│   │   └── ... (existing V1 components)
│   └── pages/
│       ├── LoginPage, RegisterPage
│       ├── ProfileSettingsPage       # Scoring Criteria + AI suggestions
│       ├── AgentSourcesPage          # agent_crawl source management
│       ├── DigestPage                # INTEL BRIEF + hotspots + trends
│       ├── MyGroupsPage, GroupDetailPage, AdminGroupsPage
│       └── ... (existing V1 pages)
└── docs/superpowers/
    ├── agent-structure-design.md
    ├── specs/  (8 design docs)
    └── plans/  (6 implementation plans + schedule)
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
- After making changes, run `git commit` to record them unless explicitly told not to commit.
- Spec compliance review MUST pass before code quality review
- Do **not** update Agent Crawl related logic. Treat `backend/app/agent/` and `backend/app/fetchers/agent_crawl.py` as frozen unless the user explicitly asks to modify Agent Crawl.
- When a specific site/link cannot be crawled or a generated discovery DSL fails, first check whether the relevant prompts under `backend/app/discovery/graph.py` failed to state the needed constraint clearly. Prefer clarifying prompt boundaries and output contracts before adding hard-coded logic. Only add deterministic hard-rule code after confirming the failure is structural and cannot be solved reliably through prompt clarification.
- Do **not** proactively create or modify tests. Only add or change tests when the user explicitly asks for tests.
- Do not follow TDD by default. Implement the requested code change directly, then run existing relevant checks when practical.
- Existing tests may be used for verification, but do not write new tests just to satisfy a change.
- Pure logic (normalizer, dedup) should stay isolated and easy to verify without I/O
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
| Orchestration | **Deterministic pipeline + local LLM** for standard sources. **Handoff Chain** for agent crawl. |
| Agent crawl | PlanAgent (Pre-Act+DFSDT) → CrawlDAG (DAG parallel, no LLM) → QualityWorkerPool (Critic) → SummaryWorkerPool (Generator). Agent items bypass Enricher (`status=agent_enriched`). |
| Source types | 4 types: `rss`, `page_monitor`, `search`, `agent_crawl`. `api` type (structured stream) planned. |
| User roles | System: `subscriber` / `system_admin`. Group-level: `member` / `group_admin` (independent axis). |
| Group feed | Items scoped to user's groups' sources → group filter_criteria (OR logic) → personal scoring. |
| Personalization | User Scoring Criteria (JSONB). Fast: keyword overlap at query time. LLM: `user_item_scores` table, opt-in. |
| Digest | DigestAgent 3-step chain. Per-group scheduled digest supported. Async background thread + 2s poll. |
| Real-time logs | SSE + deque ring buffer. `id:` field for auto-reconnect. All backend processes covered. |
| Time semantics | All UTC. `nullslast()` for null published_at. Inclusive `published_before`; invalid dates → 422 |
| Sorting | `published_at` / `fetched_at` / `relevance` (V2, login required), asc/desc. Null published_at always last. |
| V2 scope | User system + group subscriptions + agent crawl + personalized scoring + digest + real-time logs |
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
