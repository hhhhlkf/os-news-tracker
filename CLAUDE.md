# OS News Tracker — CLAUDE.md

## Project Overview

A group-based OS news intelligence platform that collects technical news from multiple source types, enriches them with LLM-structured summaries, and presents results in a personalized React web UI. V2 adds multi-user accounts, group subscriptions (shared crawl cost), AI-driven intelligent crawling, personalized scoring, trend digest reports, and real-time log streaming.

**Two data streams:**

1. **News stream** — RSS + page monitoring + keyword search + Discovery connectors → LLM structured summary (category, sub-tags, entities, summary)
2. **Structured facts stream** — Security advisories/CVE, lifecycle/EOL, image releases → parsed directly into DB, no LLM (adapters planned, not yet implemented)

**Pipeline:** `fetch → normalize → [relevance-filter] → dedup → enrich → store`

**Active platform capabilities:**
- User system (JWT auth, subscriber/system_admin roles)
- Group subscription (group_admin manages sources; subscribers share crawl results)
- Discovery is being migrated to a Single Agent Loop that produces reviewed versioned connectors; the legacy LangGraph/DSL implementation remains migration-only.
- Personalized scoring (user-defined Scoring Criteria JSONB → fast keyword score + optional LLM precision)
- Digest / trend analysis (DigestAgent multi-step chain → hotspots + emerging topics)
- Real-time discovery logs are being migrated from polling/ring-buffer state to persisted authenticated SSE events.

`backend/app/agent/` and `backend/app/fetchers/agent_crawl.py` are deprecated and frozen. They are not an active Discovery path and must not be reused.

## Tech Stack

| Layer | Technology |
|-------|-------------|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0 + Alembic, Postgres, APScheduler |
| Auth | python-jose[cryptography] (JWT), passlib[bcrypt] |
| Frontend | React 19, Vite, TypeScript, TanStack Query, react-router-dom |
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
├── docker-compose.dev.yml                 # Development Docker Compose (hot-reload)
├── docker-compose.production.yml          # Immutable formal-release Compose
├── backend/
│   ├── pyproject.toml                     # Python deps (FastAPI, SQLAlchemy, python-jose, passlib, etc.)
│   ├── alembic.ini                        # DB migrations config
│   ├── Dockerfile.dev                     # Dev container
│   ├── alembic/versions/                  # Migration scripts
│   ├── app/
│   │   ├── config.py                      # Settings (env-driven, pydantic-settings)
│   │   ├── db.py                          # SQLAlchemy engine + SessionLocal
│   │   ├── models.py                      # ORM models (V1 + V2: User, Group, Digest, Agent*, etc.)
│   │   ├── schemas.py                     # Pydantic contracts
│   │   ├── enums.py                       # InfoType, Importance, SourceType, ItemStatus, UserRole, GroupRole
│   │   ├── auth.py                        # JWT helpers: create_access_token, verify_access_token, hash_password
│   │   ├── log_stream.py                  # SSE log ring buffer + SseLogHandler (real-time log panel)
│   │   ├── entry.py                       # App entrypoint (create_app + startup hooks)
│   │   ├── pipeline.py                    # News-stream orchestration (fetch→store)
│   │   ├── manual_news_run.py             # Target-driven manual news run (thread-safe controller)
│   │   ├── scheduler.py                   # APScheduler wiring (per-source cron jobs)
│   │   ├── repository.py                  # DB read/write queries (idempotent writes)
│   │   ├── api/
│   │   │   ├── main.py                    # FastAPI app factory + CORS + router registration
│   │   │   ├── routes.py                  # /items, /facets, /news-run (group-filtered in V2)
│   │   │   ├── deps.py                    # get_db, get_current_user, require_system_admin
│   │   │   ├── auth_routes.py             # /auth/register, /auth/login, /auth/me
│   │   │   ├── profile_routes.py          # /users/me/profile, /users/me/interactions
│   │   │   ├── digest_routes.py           # /digest CRUD + async generation
│   │   │   ├── agent_routes.py            # /sources/agent CRUD + runs + site memory
│   │   │   ├── admin_group_routes.py      # /admin/groups CRUD + admin assignment
│   │   │   ├── group_routes.py            # /groups, /groups/{id}/members|sources|profile|join-requests
│   │   │   └── group_digest_routes.py     # /groups/{id}/digest/schedule + trigger
│   │   ├── sources/
│   │   │   ├── registry.py                # Source registry (seed YAML → DB, idempotent)
│   │   │   └── seed_sources.yaml          # 84 source definitions (RSS/page_monitor/api/search)
│   │   ├── fetchers/
│   │   │   ├── base.py                    # Fetcher protocol + FetchResult
│   │   │   ├── rss.py                     # RssFetcher
│   │   │   ├── page_monitor.py            # PageMonitorFetcher
│   │   │   ├── search.py                  # SearchFetcher (keyword-based)
│   │   │   └── agent_crawl.py             # AgentCrawlFetcher (orchestrates app/agent/ pipeline)
│   │   ├── agent/                         # V2: Agent crawl engine (Handoff Chain)
│   │   │   ├── schemas.py                 # CrawlPlan, RawPage, QualifiedPage, AgentItem, AgentSourceConfig
│   │   │   ├── plan_agent.py              # PlanAgent: Pre-Act + DFSDT, LLM URL planning
│   │   │   ├── crawl_dag.py               # CrawlDAG: asyncio Semaphore parallel fetch (no LLM)
│   │   │   ├── quality_pool.py            # QualityWorkerPool: Critic, parallel LLM quality scoring
│   │   │   ├── summary_pool.py            # SummaryWorkerPool: Generator, adaptive summarization
│   │   │   └── site_memory.py             # SiteMemory: DB-backed quality cache (7-day TTL for keep)
│   │   ├── groups/                        # V2: Group subscription helpers
│   │   │   ├── permissions.py             # is_group_member, is_group_admin, check_group_access
│   │   │   └── feed_filter.py             # passes_group_filter, apply_group_filter_to_items
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
│   │   │   ├── enricher.py                # LLM enrich → EnrichedFields
│   │   │   ├── scorer.py                  # V2: compute_fast_score + ScoringAgent (LLM precision)
│   │   │   ├── profile_advisor.py         # V2: ProfileAdvisor Agent (AI criteria suggestions)
│   │   │   └── digest_agent.py            # V2: DigestAgent 3-step chain (aggregate→hotspots→summary)
│   │   └── llm/
│   │       └── client.py                  # OpenAI-compatible client + cache
│   └── tests/
│       ├── conftest.py
│       ├── fixtures/
│       ├── unit/                          # Pure function + mock LLM tests
│       └── integration/                   # API + pipeline integration tests
├── frontend/
│   ├── package.json                       # react-router-dom added in V2
│   ├── index.html                         # Google Fonts: Inter + JetBrains Mono (V2)
│   └── src/
│       ├── App.tsx                        # V2: BrowserRouter + RequireAuth guards
│       ├── auth.ts                        # V2: getToken, setAuth, clearAuth, authHeaders
│       ├── index.css                      # V2: CSS design tokens (--ink, --signal-high/mid/low, etc.)
│       ├── types.ts                       # ItemSummary, Facets, User, Digest, AgentSource, LogEntry, etc.
│       ├── api/client.ts                  # All API calls + auth headers injection
│       ├── hooks/
│       │   └── useLogStream.ts            # V2: EventSource hook for SSE log panel
│       ├── components/
│       │   ├── ItemCard.tsx               # V2: left-border score badge (teal/amber/slate)
│       │   ├── ItemList.tsx
│       │   ├── ItemDetail.tsx
│       │   ├── FacetSidebar.tsx           # V2: relevance threshold slider
│       │   ├── ImportanceBadge.tsx
│       │   ├── InfoTypeBadge.tsx
│       │   ├── NewsRunControl.tsx
│       │   ├── TimeRangePicker.tsx
│       │   └── LogPanel.tsx               # V2: real-time log panel (JetBrains Mono, dark bg)
│       └── pages/
│           ├── HomePage.tsx               # V2: user nav, relevance sort, group-filtered feed
│           ├── LoginPage.tsx              # V2
│           ├── RegisterPage.tsx           # V2
│           ├── ProfileSettingsPage.tsx    # V2: Scoring Criteria management + AI suggestions
│           ├── AgentSourcesPage.tsx       # V2: agent_crawl source CRUD
│           ├── DigestPage.tsx             # V2: INTEL BRIEF header + hotspots + trends
│           ├── MyGroupsPage.tsx           # V2: my groups + browse public groups + join request
│           ├── GroupDetailPage.tsx        # V2: source management + group filter config
│           ├── AdminGroupsPage.tsx        # V2: sysadmin group CRUD + join request review
│           ├── homeData.ts
│           ├── homeData.test.ts
│           └── newsRunControl.test.ts
└── docs/
    ├── code-review-reference.md
    └── superpowers/
        ├── agent-structure-design.md      # Agent paradigm reference (ReAct, Handoff Chain, DAG, etc.)
        ├── specs/
        │   ├── 2026-06-09-os-news-tracker-design.md
        │   ├── 2026-06-11-manual-news-run-control-design.md
        │   ├── 2026-06-13-prompt-and-display-redesign.md
        │   ├── 2026-06-15-v2-complete-design.md          # Merged V2 spec (personalization + agent crawl)
        │   ├── 2026-06-15-group-subscription-design.md   # Group subscription + 3-tier roles
        │   └── 2026-06-15-realtime-log-panel-design.md   # SSE log panel
        └── plans/
            ├── 2026-06-15-v2-db-and-user-system.md       # Tasks 1-7: DB + JWT auth
            ├── 2026-06-15-v2-agent-crawl-engine.md       # Tasks 8-14: Agent engine
            ├── 2026-06-15-v2-personalization-digest.md   # Tasks 15-19: Scoring + Digest
            ├── 2026-06-15-v2-frontend.md                 # Tasks 20-26: Frontend
            ├── 2026-06-15-v2-group-subscription.md       # Tasks 1-10: Group system
            └── 2026-06-15-v2-implementation-schedule.md  # Management-facing schedule
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

## Deployment Environments

Never run a bare `docker compose` command. Use `docker compose -f docker-compose.dev.yml ...` for development and `docker compose -p <release-project> -f docker-compose.production.yml ...` for formal releases.

### Development

- Worktree: `/data/workspace/os-news-tracker-dev`.
- Compose services: PostgreSQL 16 (`db`), reload-enabled FastAPI (`backend`), and Vite (`frontend`). Backend maps host `8000`; frontend maps host `5174` to container `5173`.
- Application source is bind-mounted. All schedulers are forced to `0`; development runs must not process scheduled work.
- This Compose project declares external volume `os-news-tracker_pgdata`. Environment identity is determined by project + working directory, not a guessed volume name.

After changing backend `.env` settings, recreate the backend; after changing frontend build/runtime settings, recreate the frontend:

```bash
docker compose -f docker-compose.dev.yml up -d --force-recreate backend
docker compose -f docker-compose.dev.yml up -d --force-recreate frontend
```

### Formal release

- The currently running formal stack is launched from `/data/workspace/os-news-tracker` with a release-specific project name and exposes only Nginx on host port `80`; `backend-prod` has no host port.
- Production is image-based and has no source hot reload. It joins external network `os-news-tracker_default` and currently resolves database host `db` there. Its live database is therefore managed separately from the development stack.
- Production shares approved connector artifacts, Nginx configuration, and WeChat/attestation/checkpoint volumes. Do not remove or replace these as part of an application release.
- Commit and push first; build only from a clean exact-commit worktree. Always set absolute `PRODUCTION_WORKTREE`; the Compose default is a historical fallback, not the release selector.
- Confirm Discovery/manual fetch idle before handover. Stop but retain the old pair, start and health-check the new pair, then retain only the new running pair and immediately previous stopped pair.

The authoritative commands and rollback/data-safety details are in `docs/deployment/deployment-runbook.md`.

## Development Workflow

This project uses **Subagent-Driven Development (SDD)**:

1. **Implementation** — Each task gets an independent implementer subagent (full task spec + context)
2. **Spec Review** — Spec compliance reviewer verifies implementation matches the plan line-by-line
3. **Code Quality Review** — Code quality reviewer checks architecture, error handling, type safety, tests
4. **Fix Loop** — Issues found during review are fixed by the implementer and re-reviewed

**Key rules:**

- One commit per task (iterate via `git commit --amend`)
- After making changes, run `git commit` to record them unless explicitly told not to commit.
- Spec review must pass before code quality review begins
- Do **not** update Agent Crawl related logic. Treat `backend/app/agent/` and `backend/app/fetchers/agent_crawl.py` as frozen unless the user explicitly asks to modify Agent Crawl.
- When a specific site/link cannot be crawled or a generated discovery DSL fails, first check whether the relevant prompts under `backend/app/discovery/graph.py` failed to state the needed constraint clearly. Prefer clarifying prompt boundaries and output contracts before adding hard-coded logic. Only add deterministic hard-rule code after confirming the failure is structural and cannot be solved reliably through prompt clarification.
- Do **not** proactively create or modify tests. Only add or change tests when the user explicitly asks for tests.
- Do not follow TDD by default. Implement the requested code change directly, then run existing relevant checks when practical.
- Existing tests may be used for verification, but do not write new tests just to satisfy a change.
- Pure logic (normalizer, dedup) should stay isolated and easy to verify without I/O dependencies
- Fetchers/extractors/search are all Protocol-based, pluggable and independently testable

## Code Conventions

### Python (Backend)

- **Type annotations:** Full type annotations on all function signatures
- **Pydantic v2:** Data contracts use `pydantic.BaseModel`; config uses `pydantic-settings`
- **SQLAlchemy 2.0:** Use `Mapped` + `mapped_column` declarative mapping
- **Protocols:** Pluggable components (Fetcher, ContentExtractor, SearchProvider) defined as Protocol classes — no concrete dependencies
- **Pure functions:** normalizer, dedup are pure functions with no side effects, easy to test
- **Error handling:** Use `try/finally` to ensure resource release (e.g., DB sessions)
- **Logging:** Module-level logger via `logging.getLogger(__name__)`
- **Environment variables:** Configuration controlled via `ENABLE_SCHEDULER`, `DATABASE_URL`, etc.
- **UTC datetime:** All datetime values are UTC-aware. `_as_utc()` helper converts naive→UTC (assumed) and aware→UTC (converted). Frontend sends explicit `Z` suffix.
- **Published date filtering:** `published_after` is inclusive (≥), `published_before` is inclusive (< next day). Both use `date.fromisoformat()` with `datetime(…, tzinfo=timezone.utc)`. Invalid dates return 422.
- **Sorting:** `SortBy = Literal["published_at", "fetched_at"]`, `SortDir = Literal["desc", "asc"]`. Default: `published_at` DESC with `nullslast()` (null published_at always last).
- **Idempotent writes:** Junction-table inserts use `_add_source_link_if_new()` pattern — check existence before insert, return bool. All junction writes (`merge_source_link`, `save_enriched`) are safe to call repeatedly.
- **Thread safety:** `ManualNewsRunController` uses `threading.Lock` protecting `_RuntimeState`; cooperative shutdown via `should_stop()`.
- **JWT auth:** `app/auth.py` — `hash_password` / `verify_password` (passlib bcrypt), `create_access_token` / `verify_access_token` (python-jose HS256). Secret from `JWT_SECRET_KEY` env var.
- **User roles:** `UserRole.SUBSCRIBER` = `"subscriber"`, `UserRole.SYSTEM_ADMIN` = `"system_admin"`. Group roles in `GroupRole`: `MEMBER` / `GROUP_ADMIN`. Use `check_group_access(db, group_id, user, require_admin=False)` for group permission enforcement.
- **Agent async:** `AgentCrawlFetcher.fetch()` is synchronous (Fetcher Protocol). Uses `asyncio.run()` internally to drive the async Handoff Chain. Scrapling and LLM calls wrapped with `asyncio.to_thread()` for parallel execution.
- **SiteMemory TTL:** `verdict=keep` expires after 7 days; `verdict=discard` is permanent. PlanAgent skips URLs with `discard, seen_count >= 2`.
- **Group feed filter:** `apply_group_filter_to_items(items, group_filter_map)` uses OR logic — item passes if it passes ANY of its source's associated group filters.
- **SSE log stream:** `SseLogHandler.emit()` called from any thread; appends to global `deque` (thread-safe). SSE generator polls every 100ms. Use `id: {seq_no}` in SSE format for auto-reconnect.

### TypeScript / React (Frontend)

- **Inline styles:** All components use inline `style` props (no CSS modules, no Tailwind)
- **CSS variables:** Design tokens in `index.css` as `--ink`, `--ground`, `--signal-high/mid/low`, `--font-body`, `--font-mono`. Use `var(--token)` in inline styles.
- **@tanstack/react-query:** Data fetching uses `useQuery` (`queryKey` + `queryFn`); 2s polling during active run states
- **ApiError pattern:** `api/client.ts` exports `ApiError` class (with HTTP status); error handling uses `instanceof ApiError`
- **Auth:** `auth.ts` exports `getToken`, `setAuth`, `clearAuth`, `authHeaders()`. All API calls inject `authHeaders()`. Unauthenticated users redirect to `/login`.
- **Routing:** `react-router-dom` v7. `RequireAuth` wrapper checks `getToken()` before rendering protected pages.
- **Conditional rendering:** null guards (`&&`), empty array guards (`.length > 0`), truthy checks on nullable fields
- **Type safety:** `keyof Facets` for type-safe facet group iteration
- **Slide-out overlay:** Fixed-position detail panel, background click to close, `stopPropagation` prevents close on panel click
- **UTC time:** `toAbsoluteDateTime()` emits explicit UTC ISO-8601 strings (`${date}T${time}Z`) — never uses `new Date()` with local-time strings
- **Defense-in-depth:** Frontend `Map`-based dedup on `source_links` as fallback (primary fix is in backend)
- **Sort dropdown:** `<select>` in HomePage search bar with `value:key` format; `sortBy` prop threaded through ItemList → ItemCard
- **Time filter presets:** `useMemo` computes `activePreset` from `published_after`/`published_before`; presets use `daysAgo(n)` helper. Custom range shows two `<input type="date">` fields.
- **Score badge:** `ItemCard` shows 4px left-border color (teal ≥80 / amber 50-79 / slate <50) + JetBrains Mono score number. `personalized_score` is nullable — no badge when null.
- **SSE log:** `useLogStream(enabled)` opens `EventSource('/api/logs/stream')`. Browser handles reconnect via `Last-Event-ID`. `LogPanel` fixed bottom-right, dark bg `#0d1117`, JetBrains Mono 13px.

## Architecture Decisions

| Decision | Conclusion |
|----------|------------|
| Orchestration | **Deterministic pipeline + localized LLM** (not autonomous agent loops) for standard sources |
| Agent crawl engine | **Handoff Chain** (PlanAgent → CrawlDAG → QualityWorkerPool → SummaryWorkerPool). Pre-Act inside PlanAgent only. CrawlDAG is pure-deterministic (no LLM). Stochastic-deterministic boundary enforced. |
| Collection sources | 4 Fetcher types: `rss` / `page_monitor` / `search` / `agent_crawl`. `api` type planned for structured stream |
| Agent crawl items | Bypass Enricher entirely; `status=agent_enriched`. SummaryWorkerPool owns summarization. |
| Source registry | 84 sources in `seed_sources.yaml` (23 RSS, 46 page_monitor, 15 api). `agent_crawl` sources user-created per group. |
| Extraction engine | Scrapling default + pluggable interface (Firecrawl as future option) |
| Dedup strategy | url_hash + simhash + near-duplicate matching |
| Search | Internal search capability preferred; interface is pluggable |
| Manual news run | Target-driven, two-phase (collect→process) loop with up to 3 expansion rounds on search-type sources; thread-safe singleton controller |
| User roles | System-level: `subscriber` / `system_admin`. Group-level: `member` / `group_admin` (stored in `group_members.role`). Independent axes. |
| Group subscription | Groups own sources via `group_sources`. Shared crawl per group (cost-efficient). Feed = union of user's groups' sources → group filter_criteria (OR logic) → personal scoring. |
| Group filter | `filter_criteria` JSONB: keyword_whitelist, keyword_blacklist, category_whitelist, min_importance. Applied at query time (soft filter, not at write time). |
| Personalized scoring | User defines Scoring Criteria (JSONB list). Fast track: keyword/tag overlap, computed at query time (no LLM). Precision track: LLM ScoringAgent, stored in `user_item_scores`, opt-in. |
| Digest generation | DigestAgent multi-step: aggregate → hotspot identification (LLM #1) → narrative synthesis (LLM #2). Async background thread + 2s polling. Per-group scheduled digest supported. |
| Real-time logs | Custom `SseLogHandler` → global `deque(maxlen=2000)`. `GET /logs/stream` SSE endpoint with `id:` field for automatic Last-Event-ID reconnect. Frontend `useLogStream` hook + `LogPanel` component. |
| Time semantics | All times UTC. `_as_utc()` backend, explicit `Z` suffix frontend. Relative ranges are open-ended (no upper bound); absolute ranges are bounded both sides. `published_at` null items: sorted last (nullslast), excluded from time-filtered queries |
| Observability | `TimeFilterStats` (missing_published_at, before_start, after_end, matched) tallied per-source during collection, exposed in status, logs, and gap_reason |
| Source link dedup | `UniqueConstraint(item_id, source_id, url)` on `item_sources` + `_add_source_link_if_new()` idempotent writes + API-level dedup + frontend fallback |
| V1 scope | Collection + two-stream processing + searchable web UI + manual news run |
| V2 scope | User system + group subscriptions + agent crawl + personalized scoring + digest + real-time logs |
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
