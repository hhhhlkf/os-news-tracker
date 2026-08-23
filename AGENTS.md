# OS News Tracker — AGENTS.md

## Project Overview

群组化技术新闻情报平台（OS News Tracker）— 支持多用户、群组订阅、智能探查、确定性采集、个性化推荐评分和趋势总结的 OS 技术情报系统。

**Three-tier role model:** `system_admin` → `group_admin`（群内）→ `subscriber`（群内 `member`）

**Two data streams:**
1. **News stream** — RSS + page monitor + keyword search + Discovery connectors → shared news Pipeline
2. **Structured stream** — CVE/EOL/image → direct parse (planned)

**Pipeline:** `fetch → normalize → [relevance-filter] → dedup → enrich → store`

`agent_crawl` and its Handoff Chain are deprecated legacy code. They are not an active product path, must not be reused for Discovery, and remain frozen until explicitly removed in a separately scoped task.

## Active Discovery Refactor — Source of Truth

The approved target architecture is defined in:

`docs/superpowers/plans/2026-08-19-discovery-local-plugin-refactor.md`

The current repository still contains the legacy LangGraph/DSL Discovery implementation while migration is in progress. Do not describe target components as already implemented until their code lands. All new Discovery work must move toward the approved plan:

- Scope: ordinary websites and WeChat public-account discovery. Internal forum keeps only its existing interface and routing seam.
- One in-process **Single Agent Loop Engine**; no Agent Graph, Graph Engineering, LangGraph workflow, or multi-Agent handoff for the new path.
- Ordinary websites produce one versioned Python connector per site. WeChat uses one shared anonymous Sogou connector with per-account configuration.
- Agent Bash/browser/file tools run only inside ephemeral Docker + gVisor (`runsc`) sandboxes. Never silently fall back to plain Docker for untrusted generated code.
- Discovery RAG stores only approved connectors and confirmed repair experience. Formal connector execution never calls Agent, RAG, or LLM.
- Connector output remains `{items, stats}` and must continue through `CrawlOutputIngester` and the existing news Pipeline.
- Preserve external Discovery HTTP contracts, review flow, cancellation, ingestion, and formal fetch result shape; replace the internal website/WeChat implementation.
- Legacy website/WeChat DSLs are migration-only: dual-run per site, retain rollback for at least 7 days and 3 successful formal runs, then remove them. Protect the internal-forum compatibility seam before deleting shared legacy code.
- Discovery loop limit: 5 minutes and at most 5 write/execute/repair rounds.
- Formal failures: after 3 consecutive failures, start one bounded repair run; every repaired version returns to pending review and is never auto-published.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0 + Alembic, PostgreSQL, APScheduler |
| Auth | python-jose[cryptography], passlib[bcrypt] |
| Frontend | React 19, Vite, TypeScript, TanStack Query, react-router-dom |
| Ingestion | feedparser, Scrapling, httpx |
| AI/LLM | Configurable OpenAI-compatible client (internal LLM gateway) |
| Testing | pytest, pytest-asyncio, respx, vitest |
| Deployment | Docker Compose; gVisor `runsc`/`systrap` is the approved Discovery sandbox target |

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
│   │   ├── run_logs.py               # Current process-local run-log buffer (Discovery target: persisted SSE events)
│   │   ├── discovery/                 # Current legacy Discovery implementation; migrate via approved refactor plan
│   │   │   ├── website_workflow.py   # Legacy LangGraph website workflow
│   │   │   ├── graph.py              # Legacy DSL-oriented discovery graph
│   │   │   ├── runner.py             # Existing formal method execution + Pipeline handoff
│   │   │   ├── fetch_jobs.py         # Killable fetch-job controller
│   │   │   ├── audit.py, quality_audit.py
│   │   │   └── wechat_tools.py       # Legacy WeChat search/history tools
│   │   ├── morning_crawl/             # Scheduled execution of approved CrawlMethods
│   │   ├── fetchers/
│   │   │   ├── base.py, rss.py, page_monitor.py, search.py
│   │   │   └── agent_crawl.py        # DEPRECATED/FROZEN — never use for Discovery refactor
│   │   ├── agent/                    # DEPRECATED/FROZEN legacy Handoff Chain
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
│   ├── hooks/useDiscoveryLogs.ts     # Current polling hook; target is authenticated fetch-based SSE
│   ├── components/
│   │   ├── ItemCard.tsx              # Left-border score badge (teal/amber/slate)
│   │   ├── DiscoveryFlowChart.tsx     # Current legacy graph; replace with Single Agent Loop visualization
│   │   ├── DiscoveryLogPanel.tsx      # Discovery/method run log panel
│   │   └── ... (existing V1 components)
│   └── pages/
│       ├── LoginPage, RegisterPage
│       ├── ProfileSettingsPage       # Scoring Criteria + AI suggestions
│       ├── DiscoveryPage             # Technical discovery and CrawlMethod management
│       ├── DigestPage                # INTEL BRIEF + hotspots + trends
│       ├── MyGroupsPage, GroupDetailPage, AdminGroupsPage
│       └── ... (existing V1 pages)
└── docs/superpowers/
    ├── agent-structure-design.md
    ├── specs/  (8 design docs)
    └── plans/
        └── 2026-08-19-discovery-local-plugin-refactor.md  # Approved Discovery implementation source of truth
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
- The approved Discovery refactor plan is binding. Check implementation decisions against it before changing Discovery boundaries, persistence, APIs, sandboxing, or migration behavior.
- Do **not** update or reuse Agent Crawl logic. Treat `backend/app/agent/` and `backend/app/fetchers/agent_crawl.py` as deprecated and frozen.
- Do not extend the legacy LangGraph/DSL website path for new features. New ordinary-site and WeChat work belongs in the Single Agent Loop + connector path.
- For a generated connector failure, first inspect tool evidence, the Loop prompt constraints, output contracts, and deterministic evaluator feedback. Site-specific behavior belongs in that site's connector, not as a shared-runtime hard-coded fallback.
- **No site-mechanism hard-coding in shared Discovery code.** Shared runtime, prompts, schemas, and validators must not enumerate website-specific pagination/extraction kinds, endpoint patterns, parameter names, response field names, selectors, framework signatures, or other currently known crawling techniques as a closed set. Agent-produced strategy/evidence objects must remain open and bounded so unfamiliar mechanisms can be represented without a code change. Fixed validation is allowed only for stable safety/resource limits and mechanism-independent outcome invariants (for example: real sandbox execution occurred, a follow-up operation produced new item identities, and a bounded stop condition exists).
- Never certify Agent-generated connector behavior by searching its source for exact endpoint, field-path, parameter, or selector string literals. Carry the open strategy into Build/Repair, then verify behavior through gVisor execution, host-owned evidence, connector contracts, and deterministic output comparison. Site-specific implementation details belong in the versioned connector and approved RAG experience, not in the shared Loop Engine.
- Keep Agent decisions separate from deterministic execution and evaluation. The model must never self-certify that its connector ran successfully.
- Untrusted generated code must execute through the gVisor sandbox abstraction. If `runsc` is unavailable, report the environment blocker; do not silently execute it with ordinary `runc`/Docker or import it into the backend process.
- Only the dev database may be migrated during implementation unless the user explicitly authorizes a production rollout. Never infer authorization to touch the production database or `os-news-tracker_pgdata` volume.
- Preserve unrelated worktree changes and untracked files. In particular, do not add `backend/uv.lock` unless the task explicitly requires it.
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
- **Connector contract**: Versioned connectors expose `async crawl(request, context) -> dict` and return exactly `{items, stats}`. stdout is result JSON; stderr is log/event output.
- **Connector isolation**: Connectors cannot receive a DB session or backend secrets. Validate Manifest path, entrypoint, checksum, runtime version, and allowed domains before execution.
- **Runtime dependencies**: Discovery may trial an exact pinned dependency in its sandbox; formal execution uses an immutable versioned Runtime image and never installs from PyPI at run time.

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
- **Discovery flow target**: Render the approved Context → Explore → Build → Execute → Evaluate/Repair → Package lifecycle from backend `phase` and `round`; do not infer the current stage from log history.
- **Discovery logs target**: Use authenticated `fetch`-based SSE, append chronologically, batch UI updates, virtualize long lists, and pause auto-scroll while the user reads history. Do not expose private chain-of-thought; show redacted auditable action summaries and tool evidence.

## Architecture Decisions

| Decision | Resolution |
|----------|-----------|
| Discovery orchestration | One in-process Single Agent Loop Engine with deterministic phase control, evaluator, limits, checkpoints, and review gate. No new LangGraph or multi-Agent handoff. |
| Agent crawl | Deprecated and frozen. It is excluded from Discovery and must not be reused. |
| Connector artifacts | One versioned Python connector per ordinary site; one shared anonymous Sogou connector for WeChat account configurations. |
| Connector execution | Ephemeral Docker + gVisor containers, maximum 4 globally. Formal scheduled > manual > Discovery > repair priority. |
| Discovery RAG | Hybrid metadata/keyword/Embedding retrieval over approved connectors and confirmed repairs only. Never used during formal crawling. |
| WeChat | Anonymous Sogou discovery + public article fetch. Authenticated MP-history path is deprecated and dormant. |
| Internal forum | Preserve interface and routing seam only; no implementation work in this refactor. |
| Legacy DSL | Migration-only. Dual-run and cut over per site; keep 7 days plus 3 successful formal runs for rollback, then delete website/WeChat DSL logic. |
| Source types | Runtime may still contain legacy `agent_crawl` records/types during cleanup; do not create new ones. Structured `api` stream remains planned. |
| User roles | System: `subscriber` / `system_admin`. Group-level: `member` / `group_admin` (independent axis). |
| Group feed | Items scoped to user's groups' sources → group filter_criteria (OR logic) → personal scoring. |
| Personalization | User Scoring Criteria (JSONB). Fast: keyword overlap at query time. LLM: `user_item_scores` table, opt-in. |
| Digest | DigestAgent 3-step chain. Per-group scheduled digest supported. Async background thread + 2s poll. |
| Discovery logs | Target is persisted per-run SSE events with redaction and replay. Current polling/ring-buffer code is legacy until migrated. |
| Time semantics | All UTC. `nullslast()` for null published_at. Inclusive `published_before`; invalid dates → 422 |
| Sorting | `published_at` / `fetched_at` / `relevance` (V2, login required), asc/desc. Null published_at always last. |
| Active platform scope | User system + group subscriptions + Discovery connectors + personalized scoring + digest + real-time logs |
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
