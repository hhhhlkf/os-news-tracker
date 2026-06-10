# OS News Tracker V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the V1/MVP of a technical-news tracking agent with **two streams**: (1) a *news stream* ingesting RSS + structured APIs + fixed pages + keyword search, enriching each item with an LLM (categorize, sub-tags, structured entities, structured summary); (2) a *structured-data stream* parsing security advisories / lifecycle / images / compatibility straight into typed tables (no LLM). Both stored in Postgres and served to a queryable React frontend with faceted search.

**Architecture:** A deterministic pipeline (`fetch → normalize → [relevance-filter] → dedup → enrich → store`) for the news stream, plus a parallel `fetch → adapter.parse → upsert` path for the structured stream, both driven by APScheduler and routed by `source.stream`. Fetchers, content extraction, search, and per-source API adapters are pluggable behind protocols (`Fetcher`, `ContentExtractor`, `SearchProvider`, `SourceAdapter`). The only LLM steps are the `Enricher` and an optional relevance gate. FastAPI serves news + structured endpoints to a React (Vite + TypeScript) frontend. **No agent / autonomous loop** — see spec §14.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0 + Alembic, Postgres, APScheduler, feedparser, Scrapling, httpx, pydantic v2, pytest; React 18 + Vite + TypeScript + TanStack Query; Docker Compose.

---

## File Structure

```
os-news-tracker/
  backend/
    pyproject.toml
    alembic.ini
    alembic/                      # migrations
      env.py
      versions/
    app/
      __init__.py
      config.py                   # Settings (env-driven)
      db.py                       # engine + session factory
      models.py                   # SQLAlchemy ORM models
      schemas.py                  # pydantic contracts (RawItem, ExtractedDoc, EnrichedFields, ...)
      enums.py                    # InfoType, Importance, SourceType, ItemStatus, EntityType, TagKind
      sources/
        registry.py               # load sources from seed config + DB
        seed_sources.yaml         # initial source list
      fetchers/
        base.py                   # Fetcher protocol + FetchResult
        rss.py                    # RssFetcher
        page_monitor.py           # PageMonitorFetcher
        search.py                 # SearchFetcher
        api.py                    # ApiFetcher (structured stream)
      structured/                 # structured-data stream
        schemas.py                # AdvisoryRecord / LifecycleRecord / ImageRecord / CompatibilityRecord / StructuredBatch
        repository.py             # idempotent upsert
        pipeline.py               # structured run path
        adapters/
          base.py                 # SourceAdapter protocol + registry
          ubuntu_security.py      # worked example adapter
      extract/
        base.py                   # ContentExtractor protocol + ExtractedDoc
        scrapling_extractor.py    # default engine
      search/
        base.py                   # SearchProvider protocol + SearchResult
        internal_gateway.py       # placeholder impl (config-gated)
      processing/
        normalizer.py             # normalize RawItem -> NormalizedItem (pure)
        dedup.py                  # url_hash, simhash, near-dup match (pure)
        relevance.py              # lightweight LLM relevance check for search results
        enricher.py               # LLM enrich -> EnrichedFields
      llm/
        client.py                 # configurable OpenAI-compatible client + cache
      pipeline.py                 # orchestration
      scheduler.py                # APScheduler wiring
      repository.py               # DB read/write queries
      api/
        __init__.py
        main.py                   # FastAPI app factory
        routes.py                 # /items, /items/{id}, /facets
        deps.py                   # DB session dependency
    tests/
      conftest.py
      fixtures/                   # saved RSS/HTML samples + LLM responses
      unit/
      integration/
  frontend/
    package.json
    vite.config.ts
    index.html
    src/
      main.tsx
      api/client.ts
      types.ts
      components/
        ItemList.tsx
        ItemCard.tsx
        ItemDetail.tsx
        FacetSidebar.tsx
        ImportanceBadge.tsx
        InfoTypeBadge.tsx
      pages/
        HomePage.tsx
      App.tsx
  docker-compose.yml
  .env.example
```

**Boundaries:** pure logic (`normalizer`, `dedup`) is isolated and unit-tested without I/O. Fetchers/extractors/search are behind protocols so each is testable with fixtures and swappable. The pipeline depends only on protocols, not concrete impls.

---

## Phase P — Prerequisites: Toolchain & Environment Setup

> Do this ONCE before Task 0, on a blank machine. The rest of the plan assumes a working Python 3.11+ virtualenv with deps installed, run via `backend/.venv/bin/...`.

### Task P0: Provision the toolchain

**Goal:** a Python **3.11+** interpreter, a project virtualenv, and (for the frontend phase) Node **20+**.

- [ ] **Step 1: Check what's already available**

```bash
python3 --version            # need >= 3.11
command -v uv || echo "no uv"
node --version || echo "no node"   # need >= 20 for the frontend phase
```

- [ ] **Step 2: If Python 3.11+ is missing, install `uv` and let it provide Python**

`uv` is a single static binary that can provision a standalone CPython without touching the system Python. Install it one of these ways (pick whichever your environment allows):

```bash
# Option A: official installer (network: astral.sh)
curl -LsSf https://astral.sh/uv/install.sh | sh
# Option B: via existing pip (user install)
python3 -m pip install --user uv
```

Ensure `uv` is on PATH (user installs commonly land in `~/.local/bin` or `~/Library/Python/<ver>/bin`):

```bash
export PATH="$HOME/.local/bin:$HOME/Library/Python/3.9/bin:$PATH"
uv --version
```

- [ ] **Step 3: Note the env for the rest of the plan**

The virtualenv itself is created in **Task 0 Step 5** (it needs `backend/pyproject.toml` to exist first). After that, EVERY backend command in this plan runs through the venv:

```bash
cd backend && .venv/bin/pytest ...      # tests
cd backend && .venv/bin/python ...       # scripts
cd backend && .venv/bin/alembic ...      # migrations
```

Do **not** use the system `python3` for project work if it is < 3.11.

- [ ] **Step 4: Optional services (only needed for deploy/integration, NOT for the TDD unit/integration tests)**

- **Node 20+**: required for the frontend phase (Tasks 19-21, 31). Install via your platform (e.g. `brew install node`, `nvm install 20`, or distro package).
- **Postgres / Docker**: NOT required to run the test suite (tests use in-memory SQLite). Only needed for Task 3's Postgres re-verification and Task 23's `docker compose`. If unavailable, those steps fall back to SQLite / are deferred — see the notes in those tasks.

> No commit in this task — it only provisions tooling. Versions used during initial build: `uv` 0.11.x provisioning CPython 3.12.

---

## Phase 0 — Project Scaffolding

### Task 0: Backend project skeleton + tooling

**Files:**

- Create: `backend/pyproject.toml`
- Create: `backend/app/__init__.py`
- Create: `backend/tests/conftest.py`
- Create: `.env.example`

- [ ] **Step 1: Create `backend/pyproject.toml`**

```toml
[project]
name = "os-news-tracker"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "sqlalchemy>=2.0",
    "alembic>=1.13",
    "psycopg[binary]>=3.1",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "feedparser>=6.0",
    "httpx>=0.27",
    "apscheduler>=3.10",
    "scrapling>=0.4",
    "python-dateutil>=2.9",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "respx>=0.21"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Create empty `backend/app/__init__.py`** (empty file).

- [ ] **Step 3: Create `backend/tests/conftest.py`**

```python
import os
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("LLM_BASE_URL", "http://llm.invalid/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")


@pytest.fixture
def fixtures_dir():
    return os.path.join(os.path.dirname(__file__), "fixtures")
```

- [ ] **Step 4: Create `.env.example`**

```bash
DATABASE_URL=postgresql+psycopg://postgres:postgres@db:5432/osnews
LLM_BASE_URL=https://internal-gateway.example.com/v1
LLM_API_KEY=changeme
LLM_MODEL=internal-default
LLM_MAX_CONCURRENCY=4
SEARCH_PROVIDER=internal     # internal | none
FETCH_USER_AGENT=os-news-tracker/0.1 (+internal)
FETCH_PER_HOST_DELAY_SECONDS=2
```

- [ ] **Step 5: Create the virtualenv and install (uv, Python 3.11+)**

Requires the toolchain from Phase P. Create the venv with a 3.11+ interpreter and install the project editable with dev extras:

```bash
cd backend
uv venv --python 3.12          # creates backend/.venv with a standalone CPython 3.12
uv pip install -e ".[dev]"     # installs fastapi, sqlalchemy, scrapling, pytest, etc.
```

Verify the interpreter and that pytest runs (no tests yet → exit code 5 / "no tests ran" is expected):

```bash
.venv/bin/python --version     # Python 3.12.x
.venv/bin/pytest -q            # "no tests ran" at this stage is fine
```

Expected: install completes without error; `.venv` exists. (`backend/.venv/` is gitignored.)

> Fallback if you must use system pip instead of uv: only works if `python3` is >= 3.11 with pip >= 21.3 — `cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`.

- [ ] **Step 6: Commit**

```bash
git add backend/pyproject.toml backend/app/__init__.py backend/tests/conftest.py .env.example
git commit -m "chore: scaffold backend project and tooling"
```

(Ensure `backend/.venv/` is ignored — add it to `.gitignore` if not already.)

---

### Task 1: Config + enums

**Files:**

- Create: `backend/app/config.py`
- Create: `backend/app/enums.py`
- Test: `backend/tests/unit/test_config.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_config.py`

```python
from app.config import get_settings


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/db")
    monkeypatch.setenv("LLM_MODEL", "m1")
    get_settings.cache_clear()
    s = get_settings()
    assert s.database_url.endswith("/db")
    assert s.llm_model == "m1"
    assert s.llm_max_concurrency >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 3: Create `backend/app/enums.py`**

```python
from enum import StrEnum


class SourceType(StrEnum):
    RSS = "rss"
    API = "api"
    PAGE_MONITOR = "page_monitor"
    SEARCH = "search"


class Stream(StrEnum):
    NEWS = "news"
    STRUCTURED = "structured"


class ItemStatus(StrEnum):
    NEW = "new"
    ENRICHED = "enriched"
    ENRICH_FAILED = "enrich_failed"
    NEEDS_REVIEW = "needs_review"


class InfoType(StrEnum):
    RELEASE = "发布"
    UPDATE = "更新"
    PERFORMANCE = "性能数据"
    ADAPTATION = "适配"
    PAPER = "论文/研究"
    ANALYSIS = "观点/分析"
    OTHER = "其他"


class AdvisorySeverity(StrEnum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    MODERATE = "moderate"
    LOW = "low"
    UNKNOWN = "unknown"


class CompatibilityKind(StrEnum):
    HARDWARE = "hardware"
    SOFTWARE = "software"
    PACKAGE = "package"
    IMAGE = "image"
    OSV = "osv"


class Importance(StrEnum):
    HIGH = "高"
    MEDIUM = "中"
    LOW = "低"


class EntityType(StrEnum):
    VENDOR = "vendor"
    PRODUCT = "product"
    OS = "os"
    PACKAGE = "package"
    VERSION = "version"
    TOPIC = "topic"


class TagKind(StrEnum):
    MAIN_CATEGORY = "main_category"
    SUB_TAG = "sub_tag"


MAIN_CATEGORIES = [
    "OS跟踪来源",
    "友商产品信息",
    "软件包适配",
    "OS性能发展",
    "司内AI工具",
]
```

- [ ] **Step 4: Create `backend/app/config.py`**

```python
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+pysqlite:///:memory:"
    llm_base_url: str = "http://llm.invalid/v1"
    llm_api_key: str = "test-key"
    llm_model: str = "test-model"
    llm_max_concurrency: int = 4
    search_provider: str = "none"
    fetch_user_agent: str = "os-news-tracker/0.1 (+internal)"
    fetch_per_host_delay_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_config.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/config.py backend/app/enums.py backend/tests/unit/test_config.py
git commit -m "feat: add settings and domain enums"
```

---

## Phase 1 — Data Models + Migrations

### Task 2: SQLAlchemy models

**Files:**

- Create: `backend/app/db.py`
- Create: `backend/app/models.py`
- Test: `backend/tests/unit/test_models.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_models.py`

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models import Base, Source, Item, Tag, Entity
from app.enums import SourceType, ItemStatus


def test_models_create_and_relate():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        src = Source(name="Phoronix", type=SourceType.RSS, url="https://x/feed",
                     main_category="OS性能发展")
        item = Item(source=src, title="t", url="https://x/a", url_hash="h1",
                    content_hash="c1", status=ItemStatus.NEW)
        item.tags.append(Tag(name="kernel", kind="sub_tag"))
        item.entities.append(Entity(type="os", name="Linux"))
        s.add(item)
        s.commit()
        assert item.id is not None
        assert item.source.name == "Phoronix"
        assert item.tags[0].name == "kernel"
        assert item.entities[0].name == "Linux"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models'`

- [ ] **Step 3: Create `backend/app/db.py`**

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import get_settings

_settings = get_settings()
engine = create_engine(_settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 4: Create `backend/app/models.py`**

```python
from datetime import datetime
from sqlalchemy import (
    String, Text, Integer, DateTime, ForeignKey, Float, JSON, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(20))           # rss | api | page_monitor | search
    url: Mapped[str] = mapped_column(String(1000))
    keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    adapter: Mapped[str | None] = mapped_column(String(100), nullable=True)   # api adapter name
    stream: Mapped[str] = mapped_column(String(20), default="news")           # news | structured
    vendor: Mapped[str | None] = mapped_column(String(50), nullable=True)
    fetch_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    main_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    relevance_filter: Mapped[bool] = mapped_column(default=False)
    relevance_keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    health_status: Mapped[str] = mapped_column(String(20), default="ok")
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    items: Mapped[list["Item"]] = relationship(back_populates="source")


item_tags = None  # see association below


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("name", "kind", name="uq_tag_name_kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (UniqueConstraint("type", "name", name="uq_entity_type_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(300))


class ItemTag(Base):
    __tablename__ = "item_tags"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"), primary_key=True)


class ItemEntity(Base):
    __tablename__ = "item_entities"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), primary_key=True)
    role: Mapped[str | None] = mapped_column(String(50), nullable=True)


class ItemSource(Base):
    __tablename__ = "item_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    url: Mapped[str] = mapped_column(String(1000))


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    title: Mapped[str] = mapped_column(String(1000))
    url: Mapped[str] = mapped_column(String(1000))
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    clean_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    main_category: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    title_tldr: Mapped[str | None] = mapped_column(String(200), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_points: Mapped[list | None] = mapped_column(JSON, nullable=True)
    info_type: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    importance: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    why_it_matters: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    llm_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    source: Mapped["Source"] = relationship(back_populates="items")
    tags: Mapped[list["Tag"]] = relationship(secondary="item_tags")
    entities: Mapped[list["Entity"]] = relationship(secondary="item_entities")


# --- Structured-data stream tables (no LLM; parsed straight from APIs) ---

class SecurityAdvisory(Base):
    __tablename__ = "security_advisories"
    __table_args__ = (UniqueConstraint("vendor", "advisory_id", name="uq_adv_vendor_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    vendor: Mapped[str] = mapped_column(String(50), index=True)
    advisory_id: Mapped[str] = mapped_column(String(100))
    cve_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    title: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_products: Mapped[list | None] = mapped_column(JSON, nullable=True)
    fixed_versions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class ProductLifecycle(Base):
    __tablename__ = "product_lifecycles"
    __table_args__ = (UniqueConstraint("vendor", "product", "version", name="uq_lc_vpv"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    vendor: Mapped[str] = mapped_column(String(50), index=True)
    product: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(100))
    release_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ga_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    eol_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    eus_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    phase: Mapped[str | None] = mapped_column(String(50), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class ImageRelease(Base):
    __tablename__ = "image_releases"
    __table_args__ = (
        UniqueConstraint("vendor", "product", "image_tag", "arch", "cloud", name="uq_img"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    vendor: Mapped[str] = mapped_column(String(50), index=True)
    product: Mapped[str] = mapped_column(String(200))
    image_tag: Mapped[str] = mapped_column(String(200))
    arch: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cloud: Mapped[str | None] = mapped_column(String(50), nullable=True)
    image_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(200), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CompatibilityEntry(Base):
    __tablename__ = "compatibility_entries"
    __table_args__ = (
        UniqueConstraint("vendor", "kind", "name", "product", "version", "arch", name="uq_compat"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    vendor: Mapped[str] = mapped_column(String(50), index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)   # hardware|software|package|image|osv
    name: Mapped[str] = mapped_column(String(300))
    product: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    arch: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_models.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/db.py backend/app/models.py backend/tests/unit/test_models.py
git commit -m "feat: add SQLAlchemy models and session factory"
```

---

### Task 3: Alembic migration (initial schema)

**Files:**

- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/versions/0001_initial.py` (autogenerated, then reviewed)

- [ ] **Step 1: Init alembic**

Run: `cd backend && alembic init alembic`
Then edit `alembic/env.py` to set `target_metadata`:

```python
from app.models import Base
from app.config import get_settings

target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", get_settings().database_url)
```

- [ ] **Step 2: Autogenerate migration against a Postgres dev DB**

Run: `cd backend && DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/osnews alembic revision --autogenerate -m "initial"`
Expected: a file under `alembic/versions/` creating all tables.

- [ ] **Step 3: Apply and verify**

Run: `cd backend && DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/osnews alembic upgrade head`
Expected: tables created; `alembic current` shows the revision.

- [ ] **Step 4: Commit**

```bash
git add backend/alembic.ini backend/alembic/
git commit -m "feat: add initial alembic migration"
```

---

## Phase 2 — Contracts (pydantic schemas)

### Task 4: Pipeline data contracts

**Files:**

- Create: `backend/app/schemas.py`
- Test: `backend/tests/unit/test_schemas.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_schemas.py`

```python
from datetime import datetime
from app.schemas import RawItem, ExtractedDoc, EnrichedFields, EntityRef
from app.enums import InfoType, Importance


def test_raw_item_minimal():
    r = RawItem(source_id=1, title="t", url="https://x/a")
    assert r.raw_content is None


def test_enriched_fields_validation():
    e = EnrichedFields(
        title_tldr="x",
        summary="s",
        key_points=["a", "b"],
        info_type=InfoType.RELEASE,
        importance=Importance.HIGH,
        why_it_matters="w",
        main_category="OS性能发展",
        sub_tags=["kernel"],
        entities=[EntityRef(type="os", name="Linux")],
        confidence=0.9,
    )
    assert e.info_type == InfoType.RELEASE
    assert e.entities[0].name == "Linux"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.schemas'`

- [ ] **Step 3: Create `backend/app/schemas.py`**

```python
from datetime import datetime
from pydantic import BaseModel, Field
from app.enums import InfoType, Importance, EntityType


class RawItem(BaseModel):
    source_id: int
    title: str
    url: str
    raw_content: str | None = None
    published_at: datetime | None = None


class ExtractedDoc(BaseModel):
    url: str
    title: str | None = None
    clean_content: str
    published_at: datetime | None = None


class EntityRef(BaseModel):
    type: EntityType
    name: str
    role: str | None = None


class EnrichedFields(BaseModel):
    title_tldr: str
    summary: str
    key_points: list[str] = Field(default_factory=list)
    info_type: InfoType
    importance: Importance
    why_it_matters: str
    main_category: str
    sub_tags: list[str] = Field(default_factory=list)
    entities: list[EntityRef] = Field(default_factory=list)
    confidence: float = 0.0


class NormalizedItem(BaseModel):
    source_id: int
    title: str
    url: str
    canonical_url: str
    clean_content: str
    published_at: datetime | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_schemas.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas.py backend/tests/unit/test_schemas.py
git commit -m "feat: add pydantic pipeline contracts"
```

---

## Phase 3 — Pure Logic: Normalizer

### Task 5: URL canonicalization + normalizer

**Files:**

- Create: `backend/app/processing/normalizer.py`
- Test: `backend/tests/unit/test_normalizer.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_normalizer.py`

```python
from app.processing.normalizer import canonicalize_url, normalize
from app.schemas import RawItem, ExtractedDoc


def test_canonicalize_strips_tracking_and_trailing_slash():
    u = "https://Example.com/Path/?utm_source=x&id=5#frag"
    assert canonicalize_url(u) == "https://example.com/Path?id=5"


def test_canonicalize_removes_trailing_slash():
    assert canonicalize_url("https://x.com/a/") == "https://x.com/a"


def test_normalize_builds_normalized_item():
    raw = RawItem(source_id=1, title="  Hello  ", url="https://x.com/a/?utm_medium=rss")
    doc = ExtractedDoc(url="https://x.com/a", clean_content="body text")
    n = normalize(raw, doc)
    assert n.title == "Hello"
    assert n.canonical_url == "https://x.com/a"
    assert n.clean_content == "body text"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_normalizer.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/processing/normalizer.py`**

```python
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from app.schemas import RawItem, ExtractedDoc, NormalizedItem

_TRACKING_PREFIXES = ("utm_", "ref", "fbclid", "gclid")


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ]
    query = urlencode(query_pairs)
    path = parts.path.rstrip("/") or ""
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize(raw: RawItem, doc: ExtractedDoc) -> NormalizedItem:
    return NormalizedItem(
        source_id=raw.source_id,
        title=(doc.title or raw.title).strip(),
        url=raw.url,
        canonical_url=canonicalize_url(raw.url),
        clean_content=doc.clean_content.strip(),
        published_at=doc.published_at or raw.published_at,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_normalizer.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/processing/normalizer.py backend/tests/unit/test_normalizer.py
git commit -m "feat: add URL canonicalization and normalizer"
```

---

## Phase 4 — Pure Logic: Deduplicator

### Task 6: Hashing + simhash near-duplicate detection

**Files:**

- Create: `backend/app/processing/dedup.py`
- Test: `backend/tests/unit/test_dedup.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_dedup.py`

```python
from app.processing.dedup import url_hash, content_hash, simhash, hamming, is_near_duplicate


def test_url_hash_stable_and_distinct():
    assert url_hash("https://x.com/a") == url_hash("https://x.com/a")
    assert url_hash("https://x.com/a") != url_hash("https://x.com/b")


def test_content_hash_stable():
    assert content_hash("hello world") == content_hash("hello world")


def test_simhash_near_duplicate_detected():
    a = "Linux kernel 6.9 released with new scheduler improvements and fixes"
    b = "Linux kernel 6.9 released with new scheduler improvements and bugfixes"
    assert is_near_duplicate(simhash(a), simhash(b), threshold=5)


def test_simhash_distinct_not_duplicate():
    a = "Linux kernel 6.9 released with scheduler improvements"
    b = "Apple announces new MacBook with M5 chip and display upgrade"
    assert not is_near_duplicate(simhash(a), simhash(b), threshold=5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_dedup.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/processing/dedup.py`**

```python
import hashlib
import re


def url_hash(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def simhash(text: str, bits: int = 64) -> int:
    vector = [0] * bits
    for token in _tokens(text):
        h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    result = 0
    for i in range(bits):
        if vector[i] > 0:
            result |= 1 << i
    return result


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_near_duplicate(a: int, b: int, threshold: int = 4) -> bool:
    return hamming(a, b) <= threshold
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_dedup.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/processing/dedup.py backend/tests/unit/test_dedup.py
git commit -m "feat: add url/content hashing and simhash near-dup detection"
```

---

## Phase 5 — Extraction + Search Protocols

### Task 7: ContentExtractor protocol + Scrapling extractor

**Files:**

- Create: `backend/app/extract/base.py`
- Create: `backend/app/extract/scrapling_extractor.py`
- Test: `backend/tests/unit/test_extract.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_extract.py`

```python
from app.extract.base import ContentExtractor
from app.extract.scrapling_extractor import ScraplingExtractor


class _FakeFetcher:
    def __init__(self, html):
        self._html = html

    def fetch(self, url, **kw):
        class _Page:
            def __init__(self, html):
                self.html_content = html
        return _Page(self._html)


def test_scrapling_extractor_extracts_text(monkeypatch):
    html = "<html><head><title>Hi</title></head><body><article><p>Hello world body</p></article></body></html>"
    ext = ScraplingExtractor(fetcher=_FakeFetcher(html))
    doc = ext.extract("https://x.com/a")
    assert doc.url == "https://x.com/a"
    assert "Hello world body" in doc.clean_content
    assert doc.title == "Hi"


def test_extractor_is_protocol_instance():
    assert isinstance(ScraplingExtractor(fetcher=_FakeFetcher("<html></html>")), ContentExtractor)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_extract.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/extract/base.py`**

```python
from typing import Protocol, runtime_checkable
from app.schemas import ExtractedDoc


@runtime_checkable
class ContentExtractor(Protocol):
    def extract(self, url: str) -> ExtractedDoc: ...
```

- [ ] **Step 4: Create `backend/app/extract/scrapling_extractor.py`**

The default engine wraps Scrapling's fetcher. The fetcher is injected so tests use a fake. Text extraction uses a simple HTML-to-text via the page's parsed content.

```python
import re
from html.parser import HTMLParser
from app.schemas import ExtractedDoc
from app.extract.base import ContentExtractor


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = None
        self._in_title = False
        self._in_skip = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in ("script", "style"):
            self._in_skip = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in ("script", "style"):
            self._in_skip = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title = text
        elif not self._in_skip:
            self.chunks.append(text)


def _default_fetcher():
    from scrapling.fetchers import Fetcher
    return Fetcher


class ScraplingExtractor(ContentExtractor):
    def __init__(self, fetcher=None):
        self._fetcher = fetcher or _default_fetcher()

    def extract(self, url: str) -> ExtractedDoc:
        page = self._fetcher.fetch(url)
        html = getattr(page, "html_content", None) or getattr(page, "body", "") or ""
        parser = _TextExtractor()
        parser.feed(html)
        clean = re.sub(r"\s+", " ", " ".join(parser.chunks)).strip()
        return ExtractedDoc(url=url, title=parser.title, clean_content=clean)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_extract.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add backend/app/extract/ backend/tests/unit/test_extract.py
git commit -m "feat: add ContentExtractor protocol and Scrapling extractor"
```

---

### Task 8: SearchProvider protocol + config-gated impl

**Files:**

- Create: `backend/app/search/base.py`
- Create: `backend/app/search/internal_gateway.py`
- Test: `backend/tests/unit/test_search_provider.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_search_provider.py`

```python
from app.search.base import SearchProvider, SearchResult, NullSearchProvider, get_search_provider


def test_null_provider_returns_empty():
    p = NullSearchProvider()
    assert p.search("anything") == []
    assert isinstance(p, SearchProvider)


def test_get_search_provider_defaults_to_null(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER", "none")
    from app.config import get_settings
    get_settings.cache_clear()
    p = get_search_provider()
    assert isinstance(p, NullSearchProvider)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_search_provider.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/search/base.py`**

```python
from typing import Protocol, runtime_checkable
from pydantic import BaseModel
from app.config import get_settings


class SearchResult(BaseModel):
    url: str
    title: str | None = None
    snippet: str | None = None


@runtime_checkable
class SearchProvider(Protocol):
    def search(self, query: str) -> list[SearchResult]: ...


class NullSearchProvider(SearchProvider):
    def search(self, query: str) -> list[SearchResult]:
        return []


def get_search_provider() -> SearchProvider:
    name = get_settings().search_provider
    if name == "internal":
        from app.search.internal_gateway import InternalGatewaySearch
        return InternalGatewaySearch()
    return NullSearchProvider()
```

- [ ] **Step 4: Create `backend/app/search/internal_gateway.py`** (stub pending iWiki MCP confirmation of the real endpoint)

```python
import httpx
from app.config import get_settings
from app.search.base import SearchProvider, SearchResult


class InternalGatewaySearch(SearchProvider):
    """Placeholder for the internal LLM-gateway web search.

    The exact endpoint/auth must be confirmed via iWiki MCP before enabling
    (SEARCH_PROVIDER=internal). Until then SEARCH_PROVIDER stays 'none'.
    """

    def __init__(self, client: httpx.Client | None = None):
        self._settings = get_settings()
        self._client = client or httpx.Client(timeout=15.0)

    def search(self, query: str) -> list[SearchResult]:
        resp = self._client.post(
            f"{self._settings.llm_base_url}/web_search",
            headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
            json={"query": query},
        )
        resp.raise_for_status()
        data = resp.json()
        return [SearchResult(**r) for r in data.get("results", [])]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_search_provider.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add backend/app/search/ backend/tests/unit/test_search_provider.py
git commit -m "feat: add SearchProvider protocol with null + internal-gateway impls"
```

---

## Phase 6 — Fetchers

### Task 9: Fetcher protocol + RssFetcher

**Files:**

- Create: `backend/app/fetchers/base.py`
- Create: `backend/app/fetchers/rss.py`
- Create: `backend/tests/fixtures/sample_feed.xml`
- Test: `backend/tests/unit/test_rss_fetcher.py`

- [ ] **Step 1: Create fixture** — `backend/tests/fixtures/sample_feed.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Phoronix</title>
  <item>
    <title>Linux 6.9 Released</title>
    <link>https://phoronix.com/linux-6-9?utm_source=rss</link>
    <pubDate>Mon, 01 Jun 2026 10:00:00 GMT</pubDate>
    <description>New kernel with scheduler improvements.</description>
  </item>
  <item>
    <title>Mesa 25.1 Released</title>
    <link>https://phoronix.com/mesa-25-1</link>
    <pubDate>Tue, 02 Jun 2026 10:00:00 GMT</pubDate>
    <description>New Mesa graphics stack.</description>
  </item>
</channel></rss>
```

- [ ] **Step 2: Write the failing test** — `backend/tests/unit/test_rss_fetcher.py`

```python
import os
from app.fetchers.base import Fetcher
from app.fetchers.rss import RssFetcher
from app.models import Source
from app.enums import SourceType


def test_rss_fetcher_parses_items(fixtures_dir):
    path = os.path.join(fixtures_dir, "sample_feed.xml")
    src = Source(id=1, name="Phoronix", type=SourceType.RSS, url=f"file://{path}")
    fetcher = RssFetcher()
    items = fetcher.fetch(src)
    assert len(items) == 2
    assert items[0].title == "Linux 6.9 Released"
    assert items[0].url.startswith("https://phoronix.com/linux-6-9")
    assert items[0].published_at is not None
    assert isinstance(fetcher, Fetcher)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_rss_fetcher.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Create `backend/app/fetchers/base.py`**

```python
from typing import Protocol, runtime_checkable
from app.models import Source
from app.schemas import RawItem


@runtime_checkable
class Fetcher(Protocol):
    def fetch(self, source: Source) -> list[RawItem]: ...
```

- [ ] **Step 5: Create `backend/app/fetchers/rss.py`**

```python
from datetime import datetime, timezone
import feedparser
from dateutil import parser as dateparser
from app.fetchers.base import Fetcher
from app.models import Source
from app.schemas import RawItem


class RssFetcher(Fetcher):
    def fetch(self, source: Source) -> list[RawItem]:
        parsed = feedparser.parse(source.url)
        items: list[RawItem] = []
        for entry in parsed.entries:
            published = None
            if entry.get("published"):
                try:
                    published = dateparser.parse(entry["published"])
                    if published.tzinfo:
                        published = published.astimezone(timezone.utc).replace(tzinfo=None)
                except (ValueError, TypeError):
                    published = None
            items.append(RawItem(
                source_id=source.id,
                title=entry.get("title", "").strip(),
                url=entry.get("link", ""),
                raw_content=entry.get("summary", "") or entry.get("description", ""),
                published_at=published,
            ))
        return items
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_rss_fetcher.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/app/fetchers/base.py backend/app/fetchers/rss.py backend/tests/fixtures/sample_feed.xml backend/tests/unit/test_rss_fetcher.py
git commit -m "feat: add Fetcher protocol and RSS fetcher"
```

---

### Task 10: PageMonitorFetcher (fixed page + change detection)

**Files:**

- Create: `backend/app/fetchers/page_monitor.py`
- Test: `backend/tests/unit/test_page_monitor.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_page_monitor.py`

```python
from app.fetchers.page_monitor import PageMonitorFetcher
from app.models import Source
from app.enums import SourceType
from app.schemas import ExtractedDoc


class _StubExtractor:
    def __init__(self, content):
        self._content = content

    def extract(self, url):
        return ExtractedDoc(url=url, title="Page", clean_content=self._content)


def test_page_monitor_emits_when_content_changed():
    src = Source(id=2, name="Vendor", type=SourceType.PAGE_MONITOR,
                 url="https://vendor.com/news", last_content_hash=None)
    fetcher = PageMonitorFetcher(extractor=_StubExtractor("first version"))
    items = fetcher.fetch(src)
    assert len(items) == 1
    assert src.last_content_hash is not None


def test_page_monitor_skips_when_unchanged():
    src = Source(id=2, name="Vendor", type=SourceType.PAGE_MONITOR,
                 url="https://vendor.com/news")
    fetcher = PageMonitorFetcher(extractor=_StubExtractor("same"))
    first = fetcher.fetch(src)
    second = fetcher.fetch(src)
    assert len(first) == 1
    assert len(second) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_page_monitor.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/fetchers/page_monitor.py`**

```python
from app.fetchers.base import Fetcher
from app.models import Source
from app.schemas import RawItem
from app.processing.dedup import content_hash


class PageMonitorFetcher(Fetcher):
    def __init__(self, extractor):
        self._extractor = extractor

    def fetch(self, source: Source) -> list[RawItem]:
        doc = self._extractor.extract(source.url)
        new_hash = content_hash(doc.clean_content)
        if source.last_content_hash == new_hash:
            return []
        source.last_content_hash = new_hash
        return [RawItem(
            source_id=source.id,
            title=doc.title or source.name,
            url=source.url,
            raw_content=doc.clean_content,
            published_at=doc.published_at,
        )]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_page_monitor.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/fetchers/page_monitor.py backend/tests/unit/test_page_monitor.py
git commit -m "feat: add page-monitor fetcher with change detection"
```

---

### Task 11: SearchFetcher (search → extract → relevance gate)

**Files:**

- Create: `backend/app/processing/relevance.py`
- Create: `backend/app/fetchers/search.py`
- Test: `backend/tests/unit/test_search_fetcher.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_search_fetcher.py`

```python
from app.fetchers.search import SearchFetcher
from app.models import Source
from app.enums import SourceType
from app.schemas import ExtractedDoc
from app.search.base import SearchResult


class _StubSearch:
    def search(self, query):
        return [SearchResult(url="https://a.com/1", title="A"),
                SearchResult(url="https://b.com/2", title="B")]


class _StubExtractor:
    def extract(self, url):
        return ExtractedDoc(url=url, title="T", clean_content=f"content of {url}")


def _relevance_only_a(title, content, keywords):
    return "a.com" in title or "a.com" in content


def test_search_fetcher_filters_by_relevance():
    src = Source(id=3, name="kw", type=SourceType.SEARCH,
                 url="", keywords="linux kernel")
    fetcher = SearchFetcher(search=_StubSearch(), extractor=_StubExtractor(),
                            relevance_fn=_relevance_only_a)
    items = fetcher.fetch(src)
    assert len(items) == 1
    assert items[0].url == "https://a.com/1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_search_fetcher.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/processing/relevance.py`**

```python
from app.llm.client import LlmClient

_PROMPT = (
    "判断下面网页内容是否与关键词「{keywords}」相关的技术新闻。"
    "只回答 true 或 false。\n标题：{title}\n正文片段：{snippet}"
)


def llm_relevance(title: str, content: str, keywords: str, client: LlmClient | None = None) -> bool:
    client = client or LlmClient()
    prompt = _PROMPT.format(keywords=keywords, title=title or "", snippet=content[:800])
    answer = client.complete(prompt).strip().lower()
    return answer.startswith("true")
```

- [ ] **Step 4: Create `backend/app/fetchers/search.py`**

```python
from app.fetchers.base import Fetcher
from app.models import Source
from app.schemas import RawItem
from app.processing.relevance import llm_relevance


class SearchFetcher(Fetcher):
    def __init__(self, search, extractor, relevance_fn=llm_relevance):
        self._search = search
        self._extractor = extractor
        self._relevance_fn = relevance_fn

    def fetch(self, source: Source) -> list[RawItem]:
        keywords = source.keywords or ""
        results = self._search.search(keywords)
        items: list[RawItem] = []
        for r in results:
            doc = self._extractor.extract(r.url)
            if not self._relevance_fn(doc.title or r.title or "", doc.clean_content, keywords):
                continue
            items.append(RawItem(
                source_id=source.id,
                title=doc.title or r.title or r.url,
                url=r.url,
                raw_content=doc.clean_content,
                published_at=doc.published_at,
            ))
        return items
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_search_fetcher.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/processing/relevance.py backend/app/fetchers/search.py backend/tests/unit/test_search_fetcher.py
git commit -m "feat: add search fetcher with LLM relevance gate"
```

---

## Phase 7 — LLM Client + Enricher

### Task 12: Configurable LLM client with cache

**Files:**

- Create: `backend/app/llm/client.py`
- Test: `backend/tests/unit/test_llm_client.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_llm_client.py`

```python
from app.llm.client import LlmClient


class _StubTransport:
    def __init__(self):
        self.calls = 0

    def post(self, url, headers=None, json=None):
        self.calls += 1

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "hello"}}]}
        return _Resp()


def test_llm_client_completes_and_caches():
    transport = _StubTransport()
    client = LlmClient(http=transport)
    out1 = client.complete("prompt-1")
    out2 = client.complete("prompt-1")
    assert out1 == "hello"
    assert out2 == "hello"
    assert transport.calls == 1  # cached
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_llm_client.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/llm/client.py`**

```python
import hashlib
from app.config import get_settings


class LlmClient:
    def __init__(self, http=None, model: str | None = None):
        self._settings = get_settings()
        self._model = model or self._settings.llm_model
        self._http = http or self._make_http()
        self._cache: dict[str, str] = {}

    def _make_http(self):
        import httpx
        return httpx.Client(timeout=60.0)

    def _key(self, prompt: str) -> str:
        return hashlib.sha256(f"{self._model}:{prompt}".encode("utf-8")).hexdigest()

    def complete(self, prompt: str, *, temperature: float = 0.2) -> str:
        key = self._key(prompt)
        if key in self._cache:
            return self._cache[key]
        resp = self._http.post(
            f"{self._settings.llm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
            json={
                "model": self._model,
                "temperature": temperature,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        self._cache[key] = content
        return content
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_llm_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/llm/client.py backend/tests/unit/test_llm_client.py
git commit -m "feat: add configurable OpenAI-compatible LLM client with cache"
```

---

### Task 13: Enricher (structured summary output)

**Files:**

- Create: `backend/app/processing/enricher.py`
- Test: `backend/tests/unit/test_enricher.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_enricher.py`

```python
import json
from app.processing.enricher import Enricher
from app.schemas import NormalizedItem


class _StubLlm:
    def __init__(self, payload):
        self._payload = payload

    def complete(self, prompt, **kw):
        return self._payload


def _valid_payload():
    return json.dumps({
        "title_tldr": "Linux 6.9 发布",
        "summary": "内核 6.9 发布，带来调度器改进。",
        "key_points": ["调度器改进", "更好能效"],
        "info_type": "发布",
        "importance": "高",
        "why_it_matters": "影响服务器性能。",
        "main_category": "OS性能发展",
        "sub_tags": ["kernel", "scheduler"],
        "entities": [{"type": "os", "name": "Linux"}, {"type": "version", "name": "6.9"}],
        "confidence": 0.92,
    }, ensure_ascii=False)


def test_enricher_parses_structured_output():
    n = NormalizedItem(source_id=1, title="Linux 6.9", url="https://x/a",
                       canonical_url="https://x/a", clean_content="body")
    enricher = Enricher(llm=_StubLlm(_valid_payload()))
    fields = enricher.enrich(n)
    assert fields.title_tldr == "Linux 6.9 发布"
    assert fields.info_type == "发布"
    assert fields.main_category == "OS性能发展"
    assert {e.name for e in fields.entities} == {"Linux", "6.9"}


def test_enricher_handles_markdown_fenced_json():
    n = NormalizedItem(source_id=1, title="t", url="https://x/a",
                       canonical_url="https://x/a", clean_content="body")
    fenced = "```json\n" + _valid_payload() + "\n```"
    fields = Enricher(llm=_StubLlm(fenced)).enrich(n)
    assert fields.confidence == 0.92
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_enricher.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/processing/enricher.py`**

```python
import json
import re
from app.schemas import NormalizedItem, EnrichedFields
from app.enums import MAIN_CATEGORIES
from app.llm.client import LlmClient

_PROMPT_TEMPLATE = """你是技术新闻整理助手。阅读下面的文章，输出严格的 JSON（不要多余文字）。

可选主分类（必须从中选一个最贴切的）：{categories}

字段要求：
- title_tldr: 一句话概括，≤30字
- summary: 2-4句核心摘要
- key_points: 3-5条关键点（字符串数组）
- info_type: 从 [发布, 更新, 性能数据, 适配, 观点/分析, 其他] 选一个
- importance: 从 [高, 中, 低] 选一个
- why_it_matters: 1-2句，面向maintainer的影响说明
- main_category: 从可选主分类里选一个
- sub_tags: 细粒度子标签（字符串数组，如厂商名/产品名/技术名）
- entities: 数组，每项 {{"type": one of [vendor,product,os,package,version,topic], "name": "..."}}
- confidence: 0~1 的浮点，表示你对归类与摘要的把握

标题：{title}
正文：
{content}

只输出 JSON：
"""


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        return json.loads(brace.group(0))
    raise ValueError(f"No JSON found in LLM output: {text[:200]}")


class Enricher:
    def __init__(self, llm: LlmClient | None = None):
        self._llm = llm or LlmClient()

    def enrich(self, item: NormalizedItem) -> EnrichedFields:
        prompt = _PROMPT_TEMPLATE.format(
            categories=", ".join(MAIN_CATEGORIES),
            title=item.title,
            content=item.clean_content[:6000],
        )
        raw = self._llm.complete(prompt)
        data = _extract_json(raw)
        if data.get("main_category") not in MAIN_CATEGORIES:
            data["main_category"] = MAIN_CATEGORIES[-1]
        return EnrichedFields(**data)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_enricher.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/processing/enricher.py backend/tests/unit/test_enricher.py
git commit -m "feat: add LLM enricher producing structured summary schema"
```

---

## Phase 8 — Repository + Pipeline

### Task 14: Repository (idempotent upsert + cross-source merge)

**Files:**

- Create: `backend/app/repository.py`
- Test: `backend/tests/integration/test_repository.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/integration/test_repository.py`

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Source
from app.enums import SourceType, Importance, InfoType
from app.schemas import NormalizedItem, EnrichedFields, EntityRef
from app.repository import Repository


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Source(id=1, name="Phoronix", type=SourceType.RSS, url="u"))
    s.commit()
    yield s
    s.close()


def _norm():
    return NormalizedItem(source_id=1, title="t", url="https://x/a",
                          canonical_url="https://x/a", clean_content="body text")


def _fields():
    return EnrichedFields(title_tldr="x", summary="s", key_points=["a"],
                          info_type=InfoType.RELEASE, importance=Importance.HIGH,
                          why_it_matters="w", main_category="OS性能发展",
                          sub_tags=["kernel"], entities=[EntityRef(type="os", name="Linux")],
                          confidence=0.9)


def test_save_new_item_persists_tags_entities(session):
    repo = Repository(session)
    item = repo.save_enriched(_norm(), _fields())
    assert item.id is not None
    assert item.tags[0].name == "kernel"
    assert item.entities[0].name == "Linux"
    assert repo.exists_by_canonical("https://x/a") is True


def test_duplicate_canonical_url_merges_source_not_duplicate(session):
    repo = Repository(session)
    repo.save_enriched(_norm(), _fields())
    before = repo.count_items()
    merged = repo.merge_source_link("https://x/a", source_id=1, url="https://mirror/a")
    assert repo.count_items() == before  # no new item
    assert merged is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/integration/test_repository.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/repository.py`**

```python
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.models import Item, Tag, Entity, ItemSource
from app.schemas import NormalizedItem, EnrichedFields
from app.enums import ItemStatus, TagKind
from app.processing.dedup import url_hash, content_hash


class Repository:
    def __init__(self, session: Session):
        self._s = session

    def exists_by_canonical(self, canonical_url: str) -> bool:
        h = url_hash(canonical_url)
        return self._s.scalar(select(Item.id).where(Item.url_hash == h)) is not None

    def count_items(self) -> int:
        return self._s.scalar(select(func.count(Item.id))) or 0

    def _get_or_create_tag(self, name: str, kind: str) -> Tag:
        tag = self._s.scalar(select(Tag).where(Tag.name == name, Tag.kind == kind))
        if tag is None:
            tag = Tag(name=name, kind=kind)
            self._s.add(tag)
        return tag

    def _get_or_create_entity(self, type_: str, name: str) -> Entity:
        ent = self._s.scalar(select(Entity).where(Entity.type == type_, Entity.name == name))
        if ent is None:
            ent = Entity(type=type_, name=name)
            self._s.add(ent)
        return ent

    def save_enriched(self, n: NormalizedItem, f: EnrichedFields) -> Item:
        item = Item(
            source_id=n.source_id,
            title=n.title,
            url=n.canonical_url,
            url_hash=url_hash(n.canonical_url),
            content_hash=content_hash(n.clean_content),
            clean_content=n.clean_content,
            published_at=n.published_at,
            main_category=f.main_category,
            title_tldr=f.title_tldr,
            summary=f.summary,
            key_points=f.key_points,
            info_type=f.info_type,
            importance=f.importance,
            why_it_matters=f.why_it_matters,
            status=ItemStatus.ENRICHED,
            llm_confidence=f.confidence,
        )
        item.tags.append(self._get_or_create_tag(f.main_category, TagKind.MAIN_CATEGORY))
        for t in f.sub_tags:
            item.tags.append(self._get_or_create_tag(t, TagKind.SUB_TAG))
        for e in f.entities:
            item.entities.append(self._get_or_create_entity(e.type, e.name))
        self._s.add(item)
        self._s.flush()
        self._s.add(ItemSource(item_id=item.id, source_id=n.source_id, url=n.canonical_url))
        self._s.commit()
        return item

    def merge_source_link(self, canonical_url: str, source_id: int, url: str) -> bool:
        h = url_hash(canonical_url)
        item_id = self._s.scalar(select(Item.id).where(Item.url_hash == h))
        if item_id is None:
            return False
        self._s.add(ItemSource(item_id=item_id, source_id=source_id, url=url))
        self._s.commit()
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/integration/test_repository.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/repository.py backend/tests/integration/test_repository.py
git commit -m "feat: add repository with idempotent save and cross-source merge"
```

---

### Task 15: Pipeline orchestration

**Files:**

- Create: `backend/app/pipeline.py`
- Test: `backend/tests/integration/test_pipeline.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/integration/test_pipeline.py`

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Source
from app.enums import SourceType, Importance, InfoType
from app.schemas import RawItem, ExtractedDoc, EnrichedFields, EntityRef
from app.pipeline import Pipeline


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Source(id=1, name="Phoronix", type=SourceType.RSS, url="u"))
    s.commit()
    yield s
    s.close()


class _StubFetcher:
    def fetch(self, source):
        return [RawItem(source_id=1, title="Linux 6.9", url="https://x/a?utm_source=rss",
                        raw_content="body")]


class _StubExtractor:
    def extract(self, url):
        return ExtractedDoc(url=url, title="Linux 6.9", clean_content="kernel body text")


class _StubEnricher:
    def enrich(self, n):
        return EnrichedFields(title_tldr="Linux 6.9", summary="s", key_points=["a"],
                              info_type=InfoType.RELEASE, importance=Importance.HIGH,
                              why_it_matters="w", main_category="OS性能发展",
                              sub_tags=["kernel"], entities=[EntityRef(type="os", name="Linux")],
                              confidence=0.9)


def test_pipeline_end_to_end_dedups_on_rerun(session):
    src = session.get(Source, 1)
    pipeline = Pipeline(session=session, extractor=_StubExtractor(), enricher=_StubEnricher())
    n1 = pipeline.run_source(src, fetcher=_StubFetcher())
    n2 = pipeline.run_source(src, fetcher=_StubFetcher())
    assert n1 == 1     # one new item
    assert n2 == 0     # deduped on rerun
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/integration/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/pipeline.py`**

```python
import logging
from sqlalchemy.orm import Session
from app.models import Source
from app.enums import SourceType
from app.processing.normalizer import normalize
from app.repository import Repository

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, session: Session, extractor, enricher):
        self._session = session
        self._extractor = extractor
        self._enricher = enricher
        self._repo = Repository(session)

    def run_source(self, source: Source, fetcher) -> int:
        try:
            raw_items = fetcher.fetch(source)
        except Exception:
            logger.exception("fetch failed for source %s", source.name)
            source.fail_count += 1
            source.health_status = "error"
            self._session.commit()
            return 0

        new_count = 0
        for raw in raw_items:
            doc = self._extract_for(raw, source)
            n = normalize(raw, doc)
            if self._repo.exists_by_canonical(n.canonical_url):
                self._repo.merge_source_link(n.canonical_url, source.id, raw.url)
                continue
            try:
                fields = self._enricher.enrich(n)
            except Exception:
                logger.exception("enrich failed for %s", n.canonical_url)
                continue
            self._repo.save_enriched(n, fields)
            new_count += 1

        source.health_status = "ok"
        self._session.commit()
        return new_count

    def _extract_for(self, raw, source: Source):
        from app.schemas import ExtractedDoc
        if source.type == SourceType.RSS:
            # RSS already carries a summary; only fetch full text if content is thin
            if raw.raw_content and len(raw.raw_content) > 200:
                return ExtractedDoc(url=raw.url, title=raw.title,
                                    clean_content=raw.raw_content,
                                    published_at=raw.published_at)
            return self._extractor.extract(raw.url)
        return ExtractedDoc(url=raw.url, title=raw.title,
                            clean_content=raw.raw_content or "",
                            published_at=raw.published_at)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/integration/test_pipeline.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipeline.py backend/tests/integration/test_pipeline.py
git commit -m "feat: add pipeline orchestration with per-source isolation"
```

---

## Phase 9 — API

### Task 16: FastAPI query/facet/detail endpoints

**Files:**

- Create: `backend/app/api/__init__.py`
- Create: `backend/app/api/deps.py`
- Create: `backend/app/api/routes.py`
- Create: `backend/app/api/main.py`
- Test: `backend/tests/integration/test_api.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/integration/test_api.py`

```python
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Source
from app.enums import SourceType, Importance, InfoType
from app.schemas import NormalizedItem, EnrichedFields, EntityRef
from app.repository import Repository
from app.api.main import create_app
from app.api.deps import get_db


@pytest.fixture
def client():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    seed = TestSession()
    seed.add(Source(id=1, name="Phoronix", type=SourceType.RSS, url="u"))
    seed.commit()
    repo = Repository(seed)
    repo.save_enriched(
        NormalizedItem(source_id=1, title="Linux 6.9", url="https://x/a",
                       canonical_url="https://x/a", clean_content="body"),
        EnrichedFields(title_tldr="Linux 6.9 发布", summary="s", key_points=["a"],
                       info_type=InfoType.RELEASE, importance=Importance.HIGH,
                       why_it_matters="w", main_category="OS性能发展",
                       sub_tags=["kernel"], entities=[EntityRef(type="os", name="Linux")],
                       confidence=0.9))
    seed.close()

    app = create_app()

    def _override():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def test_list_items(client):
    resp = client.get("/items")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["title_tldr"] == "Linux 6.9 发布"


def test_filter_by_main_category(client):
    assert client.get("/items?main_category=OS性能发展").json()["total"] == 1
    assert client.get("/items?main_category=司内AI工具").json()["total"] == 0


def test_facets_endpoint(client):
    facets = client.get("/facets").json()
    assert "OS性能发展" in [f["value"] for f in facets["main_category"]]


def test_item_detail(client):
    item_id = client.get("/items").json()["items"][0]["id"]
    detail = client.get(f"/items/{item_id}").json()
    assert detail["summary"] == "s"
    assert detail["entities"][0]["name"] == "Linux"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/integration/test_api.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/api/__init__.py`** (empty file).

- [ ] **Step 4: Create `backend/app/api/deps.py`**

```python
from app.db import SessionLocal


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 5: Create `backend/app/api/routes.py`**

```python
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.models import Item, Tag, ItemSource

router = APIRouter()


def _item_summary(item: Item) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "title_tldr": item.title_tldr,
        "main_category": item.main_category,
        "info_type": item.info_type,
        "importance": item.importance,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "url": item.url,
    }


@router.get("/items")
def list_items(
    db: Session = Depends(get_db),
    main_category: str | None = None,
    info_type: str | None = None,
    importance: str | None = None,
    q: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    stmt = select(Item)
    if main_category:
        stmt = stmt.where(Item.main_category == main_category)
    if info_type:
        stmt = stmt.where(Item.info_type == info_type)
    if importance:
        stmt = stmt.where(Item.importance == importance)
    if q:
        like = f"%{q}%"
        stmt = stmt.where((Item.title.ilike(like)) | (Item.summary.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Item.published_at.desc().nullslast())
                      .limit(limit).offset(offset)).all()
    return {"total": total, "items": [_item_summary(i) for i in rows]}


@router.get("/facets")
def facets(db: Session = Depends(get_db)):
    def _counts(column):
        rows = db.execute(select(column, func.count()).group_by(column)).all()
        return [{"value": v, "count": c} for v, c in rows if v is not None]
    return {
        "main_category": _counts(Item.main_category),
        "info_type": _counts(Item.info_type),
        "importance": _counts(Item.importance),
    }


@router.get("/items/{item_id}")
def item_detail(item_id: int, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    sources = db.scalars(select(ItemSource).where(ItemSource.item_id == item_id)).all()
    return {
        **_item_summary(item),
        "summary": item.summary,
        "key_points": item.key_points or [],
        "why_it_matters": item.why_it_matters,
        "llm_confidence": item.llm_confidence,
        "sub_tags": [t.name for t in item.tags if t.kind == "sub_tag"],
        "entities": [{"type": e.type, "name": e.name} for e in item.entities],
        "source_links": [{"source_id": s.source_id, "url": s.url} for s in sources],
    }
```

- [ ] **Step 6: Create `backend/app/api/main.py`**

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="OS News Tracker")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd backend && pytest tests/integration/test_api.py -v`
Expected: PASS (4 passed)

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/ backend/tests/integration/test_api.py
git commit -m "feat: add FastAPI query/facet/detail endpoints"
```

---

## Phase 10 — Sources + Scheduler

### Task 17: Source registry + seed loader

**Files:**

- Create: `backend/app/sources/seed_sources.yaml`
- Create: `backend/app/sources/registry.py`
- Test: `backend/tests/integration/test_registry.py`

- [ ] **Step 1: Create `backend/app/sources/seed_sources.yaml`**

```yaml
# --- 新闻流：RSS ---
- name: Red Hat Blog
  type: rss
  url: https://www.redhat.com/en/rss/blog
  vendor: redhat
  stream: news
  main_category: OS跟踪来源
  fetch_cron: "0 8 * * *"
- name: Ubuntu Blog
  type: rss
  url: https://ubuntu.com/blog/feed
  vendor: ubuntu
  stream: news
  main_category: OS跟踪来源
  fetch_cron: "0 8 * * *"
- name: Phoronix (news)
  type: rss
  url: https://www.phoronix.com/rss.php
  vendor: phoronix
  stream: news
  main_category: OS性能发展
  fetch_cron: "0 */12 * * *"
- name: LWN headlines
  type: rss
  url: https://lwn.net/headlines/rss
  stream: news
  main_category: OS性能发展
  fetch_cron: "0 8 * * *"
# 高量论文源：开启相关性预过滤
- name: arXiv OS/PF/DC/AR
  type: rss
  url: https://rss.arxiv.org/rss/cs.OS+cs.PF+cs.DC+cs.AR
  stream: news
  main_category: OS性能发展
  relevance_filter: true
  relevance_keywords: "operating system, scheduler, kernel, IO, performance, benchmark, virtualization"
  fetch_cron: "0 1 * * *"
# --- 新闻流：固定页监控 ---
- name: RHEL Release Notes
  type: page_monitor
  url: https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/
  vendor: redhat
  stream: news
  main_category: OS跟踪来源
  fetch_cron: "0 9 * * *"
# --- 结构化流：API ---
- name: Red Hat Security Data
  type: api
  url: https://access.redhat.com/hydra/rest/securitydata
  adapter: redhat_securitydata
  vendor: redhat
  stream: structured
  main_category: OS跟踪来源
  fetch_cron: "0 7 * * *"
- name: Ubuntu Security Notices
  type: api
  url: https://ubuntu.com/security/notices.json
  adapter: ubuntu_security
  vendor: ubuntu
  stream: structured
  main_category: OS跟踪来源
  fetch_cron: "0 7 * * *"
- name: Red Hat Product Life Cycle
  type: api
  url: https://access.redhat.com/product-life-cycles/api/v1/
  adapter: redhat_lifecycle
  vendor: redhat
  stream: structured
  main_category: OS跟踪来源
  fetch_cron: "0 6 * * 1"
```

> 说明：这是初始 seed，覆盖各 `type`/`stream` 各一例；完整源清单见设计文档附录 A/B/C，落地时按需补全。`api` 源的 `adapter` 名要与 Task S2 注册的适配器一致。

- [ ] **Step 2: Write the failing test** — `backend/tests/integration/test_registry.py`

```python
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.models import Base, Source
from app.sources.registry import seed_sources_from_yaml


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_seed_is_idempotent(session, tmp_path):
    yaml_text = (
        "- name: Phoronix\n"
        "  type: rss\n"
        "  url: https://x/feed\n"
        "  main_category: OS性能发展\n"
    )
    f = tmp_path / "seed.yaml"
    f.write_text(yaml_text, encoding="utf-8")
    seed_sources_from_yaml(session, str(f))
    seed_sources_from_yaml(session, str(f))  # second run must not duplicate
    count = len(session.scalars(select(Source)).all())
    assert count == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && pytest tests/integration/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Create `backend/app/sources/registry.py`**

```python
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Source


def seed_sources_from_yaml(session: Session, path: str) -> int:
    with open(path, encoding="utf-8") as fh:
        entries = yaml.safe_load(fh) or []
    added = 0
    for e in entries:
        existing = session.scalar(select(Source).where(Source.name == e["name"]))
        if existing:
            continue
        session.add(Source(
            name=e["name"], type=e["type"], url=e.get("url", ""),
            keywords=e.get("keywords"), fetch_cron=e.get("fetch_cron"),
            main_category=e.get("main_category"),
            adapter=e.get("adapter"), stream=e.get("stream", "news"),
            vendor=e.get("vendor"),
            relevance_filter=e.get("relevance_filter", False),
            relevance_keywords=e.get("relevance_keywords"),
        ))
        added += 1
    session.commit()
    return added
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/integration/test_registry.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/sources/ backend/tests/integration/test_registry.py
git commit -m "feat: add source registry with idempotent yaml seeding"
```

---

### Task 18: Scheduler wiring + fetcher factory

**Files:**

- Create: `backend/app/scheduler.py`
- Test: `backend/tests/unit/test_scheduler.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_scheduler.py`

```python
from app.scheduler import build_fetcher
from app.models import Source
from app.enums import SourceType
from app.fetchers.rss import RssFetcher
from app.fetchers.page_monitor import PageMonitorFetcher
from app.fetchers.search import SearchFetcher


class _Ext:
    def extract(self, url):
        from app.schemas import ExtractedDoc
        return ExtractedDoc(url=url, clean_content="x")


class _Search:
    def search(self, q):
        return []


def test_build_fetcher_by_type():
    ext, search = _Ext(), _Search()
    assert isinstance(build_fetcher(Source(type=SourceType.RSS), ext, search), RssFetcher)
    assert isinstance(build_fetcher(Source(type=SourceType.PAGE_MONITOR), ext, search), PageMonitorFetcher)
    assert isinstance(build_fetcher(Source(type=SourceType.SEARCH), ext, search), SearchFetcher)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/scheduler.py`**

```python
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Source
from app.enums import SourceType
from app.fetchers.rss import RssFetcher
from app.fetchers.page_monitor import PageMonitorFetcher
from app.fetchers.search import SearchFetcher
from app.extract.scrapling_extractor import ScraplingExtractor
from app.search.base import get_search_provider
from app.processing.enricher import Enricher
from app.pipeline import Pipeline

logger = logging.getLogger(__name__)


def build_fetcher(source: Source, extractor, search):
    if source.type == SourceType.RSS:
        return RssFetcher()
    if source.type == SourceType.PAGE_MONITOR:
        return PageMonitorFetcher(extractor=extractor)
    if source.type == SourceType.SEARCH:
        return SearchFetcher(search=search, extractor=extractor)
    raise ValueError(f"unknown source type {source.type}")


def run_source_job(source_id: int):
    session = SessionLocal()
    try:
        source = session.get(Source, source_id)
        if not source or not source.enabled:
            return
        extractor = ScraplingExtractor()
        search = get_search_provider()
        fetcher = build_fetcher(source, extractor, search)
        pipeline = Pipeline(session=session, extractor=extractor, enricher=Enricher())
        n = pipeline.run_source(source, fetcher=fetcher)
        logger.info("source %s produced %d new items", source.name, n)
    finally:
        session.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    session = SessionLocal()
    try:
        for source in session.scalars(select(Source).where(Source.enabled.is_(True))):
            cron = source.fetch_cron or "0 8 * * *"
            scheduler.add_job(run_source_job, CronTrigger.from_crontab(cron),
                              args=[source.id], id=f"source-{source.id}")
    finally:
        session.close()
    scheduler.start()
    return scheduler
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Run the full backend suite**

Run: `cd backend && pytest -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/scheduler.py backend/tests/unit/test_scheduler.py
git commit -m "feat: add scheduler wiring and fetcher factory"
```

---

## Phase 11 — Frontend

### Task 19: Frontend scaffold + API client + types

**Files:**

- Create: `frontend/package.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/types.ts`
- Create: `frontend/src/api/client.ts`

- [ ] **Step 1: Scaffold Vite React TS**

Run: `cd frontend && npm create vite@latest . -- --template react-ts && npm install && npm install @tanstack/react-query`
Expected: project scaffolded, deps installed.

- [ ] **Step 2: Create `frontend/src/types.ts`**

```typescript
export interface ItemSummary {
  id: number;
  title: string;
  title_tldr: string | null;
  main_category: string | null;
  info_type: string | null;
  importance: string | null;
  published_at: string | null;
  url: string;
}

export interface ItemDetail extends ItemSummary {
  summary: string | null;
  key_points: string[];
  why_it_matters: string | null;
  llm_confidence: number | null;
  sub_tags: string[];
  entities: { type: string; name: string }[];
  source_links: { source_id: number; url: string }[];
}

export interface FacetValue { value: string; count: number; }
export interface Facets {
  main_category: FacetValue[];
  info_type: FacetValue[];
  importance: FacetValue[];
}
export interface ItemListResponse { total: number; items: ItemSummary[]; }
```

- [ ] **Step 3: Create `frontend/src/api/client.ts`**

```typescript
import type { ItemListResponse, ItemDetail, Facets } from "../types";

const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export async function fetchItems(params: Record<string, string>): Promise<ItemListResponse> {
  const qs = new URLSearchParams(params).toString();
  const r = await fetch(`${BASE}/items?${qs}`);
  if (!r.ok) throw new Error("failed to load items");
  return r.json();
}

export async function fetchItemDetail(id: number): Promise<ItemDetail> {
  const r = await fetch(`${BASE}/items/${id}`);
  if (!r.ok) throw new Error("failed to load item");
  return r.json();
}

export async function fetchFacets(): Promise<Facets> {
  const r = await fetch(`${BASE}/facets`);
  if (!r.ok) throw new Error("failed to load facets");
  return r.json();
}
```

- [ ] **Step 4: Replace `frontend/src/main.tsx`**

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "./index.css";

const queryClient = new QueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>
);
```

- [ ] **Step 5: Verify build**

Run: `cd frontend && npm run build`
Expected: builds without type errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/package.json frontend/vite.config.ts frontend/index.html frontend/src/main.tsx frontend/src/types.ts frontend/src/api/client.ts
git commit -m "feat: scaffold frontend with API client and types"
```

---

### Task 20: Badges with visual distinction

**Files:**

- Create: `frontend/src/components/ImportanceBadge.tsx`
- Create: `frontend/src/components/InfoTypeBadge.tsx`

- [ ] **Step 1: Create `frontend/src/components/ImportanceBadge.tsx`**

Importance uses color per the spec (高=红/橙, 中=黄, 低=灰).

```tsx
const COLORS: Record<string, { bg: string; fg: string }> = {
  "高": { bg: "#fde2e1", fg: "#b42318" },
  "中": { bg: "#fef3c7", fg: "#92400e" },
  "低": { bg: "#eceef1", fg: "#475467" },
};

export function ImportanceBadge({ value }: { value: string | null }) {
  if (!value) return null;
  const c = COLORS[value] ?? COLORS["低"];
  return (
    <span style={{
      background: c.bg, color: c.fg, padding: "2px 8px",
      borderRadius: 12, fontSize: 12, fontWeight: 600,
    }}>{value}</span>
  );
}
```

- [ ] **Step 2: Create `frontend/src/components/InfoTypeBadge.tsx`**

```tsx
export function InfoTypeBadge({ value }: { value: string | null }) {
  if (!value) return null;
  return (
    <span style={{
      border: "1px solid #d0d5dd", color: "#344054",
      padding: "2px 8px", borderRadius: 6, fontSize: 12,
    }}>{value}</span>
  );
}
```

- [ ] **Step 3: Verify build**

Run: `cd frontend && npm run build`
Expected: builds without errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ImportanceBadge.tsx frontend/src/components/InfoTypeBadge.tsx
git commit -m "feat: add importance and info-type badges"
```

---

### Task 21: List, card, detail, facet sidebar, page wiring

**Files:**

- Create: `frontend/src/components/ItemCard.tsx`
- Create: `frontend/src/components/ItemList.tsx`
- Create: `frontend/src/components/ItemDetail.tsx`
- Create: `frontend/src/components/FacetSidebar.tsx`
- Create: `frontend/src/pages/HomePage.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Create `frontend/src/components/ItemCard.tsx`**

List row shows `title_tldr + info_type + importance + main_category` per spec.

```tsx
import type { ItemSummary } from "../types";
import { ImportanceBadge } from "./ImportanceBadge";
import { InfoTypeBadge } from "./InfoTypeBadge";

export function ItemCard({ item, onClick }: { item: ItemSummary; onClick: () => void }) {
  return (
    <button onClick={onClick} style={{
      display: "block", width: "100%", textAlign: "left",
      border: "1px solid #eaecf0", borderRadius: 10, padding: 14,
      marginBottom: 10, background: "#fff", cursor: "pointer",
    }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
        <ImportanceBadge value={item.importance} />
        <InfoTypeBadge value={item.info_type} />
        {item.main_category && (
          <span style={{ fontSize: 12, color: "#667085" }}>{item.main_category}</span>
        )}
        {item.published_at && (
          <span style={{ fontSize: 12, color: "#98a2b3", marginLeft: "auto" }}>
            {item.published_at.slice(0, 10)}
          </span>
        )}
      </div>
      <div style={{ fontWeight: 600 }}>{item.title_tldr ?? item.title}</div>
    </button>
  );
}
```

- [ ] **Step 2: Create `frontend/src/components/ItemDetail.tsx`**

Detail visually separates each block (summary / key_points list cards / why_it_matters emphasis block / entities) per spec.

```tsx
import { useQuery } from "@tanstack/react-query";
import { fetchItemDetail } from "../api/client";
import { ImportanceBadge } from "./ImportanceBadge";
import { InfoTypeBadge } from "./InfoTypeBadge";

export function ItemDetail({ id }: { id: number }) {
  const { data, isLoading } = useQuery({
    queryKey: ["item", id],
    queryFn: () => fetchItemDetail(id),
  });
  if (isLoading || !data) return <div>加载中…</div>;
  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
        <ImportanceBadge value={data.importance} />
        <InfoTypeBadge value={data.info_type} />
        <span style={{ fontSize: 12, color: "#667085" }}>{data.main_category}</span>
      </div>
      <h2 style={{ margin: "4px 0 12px" }}>{data.title}</h2>

      <section style={{ marginBottom: 16 }}>
        <h4 style={{ color: "#475467" }}>摘要</h4>
        <p>{data.summary}</p>
      </section>

      <section style={{ marginBottom: 16 }}>
        <h4 style={{ color: "#475467" }}>关键点</h4>
        <div>
          {data.key_points.map((kp, i) => (
            <div key={i} style={{
              border: "1px solid #eaecf0", borderRadius: 8,
              padding: "8px 12px", marginBottom: 6, background: "#fafafa",
            }}>{kp}</div>
          ))}
        </div>
      </section>

      <section style={{
        marginBottom: 16, background: "#eff8ff",
        borderLeft: "4px solid #2e90fa", padding: "10px 14px", borderRadius: 6,
      }}>
        <h4 style={{ color: "#175cd3", marginTop: 0 }}>影响 / 意义</h4>
        <p style={{ marginBottom: 0 }}>{data.why_it_matters}</p>
      </section>

      <section style={{ marginBottom: 16 }}>
        <h4 style={{ color: "#475467" }}>实体</h4>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {data.entities.map((e, i) => (
            <span key={i} style={{
              background: "#f2f4f7", borderRadius: 6, padding: "2px 8px", fontSize: 12,
            }}>{e.type}: {e.name}</span>
          ))}
        </div>
      </section>

      <section>
        <h4 style={{ color: "#475467" }}>来源链接</h4>
        <ul>
          {data.source_links.map((s, i) => (
            <li key={i}><a href={s.url} target="_blank" rel="noreferrer">{s.url}</a></li>
          ))}
        </ul>
      </section>
    </div>
  );
}
```

- [ ] **Step 3: Create `frontend/src/components/FacetSidebar.tsx`**

```tsx
import { useQuery } from "@tanstack/react-query";
import { fetchFacets } from "../api/client";

interface Props {
  selected: Record<string, string>;
  onSelect: (key: string, value: string) => void;
}

export function FacetSidebar({ selected, onSelect }: Props) {
  const { data } = useQuery({ queryKey: ["facets"], queryFn: fetchFacets });
  if (!data) return null;
  const groups: [string, string][] = [
    ["main_category", "主分类"],
    ["info_type", "信息类型"],
    ["importance", "重要度"],
  ];
  return (
    <aside style={{ width: 220, paddingRight: 16 }}>
      {groups.map(([key, label]) => (
        <div key={key} style={{ marginBottom: 16 }}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>{label}</div>
          {(data as any)[key].map((f: { value: string; count: number }) => (
            <div key={f.value}
                 onClick={() => onSelect(key, selected[key] === f.value ? "" : f.value)}
                 style={{
                   cursor: "pointer", padding: "4px 6px", borderRadius: 6,
                   background: selected[key] === f.value ? "#eff8ff" : "transparent",
                   fontSize: 13,
                 }}>
              {f.value} <span style={{ color: "#98a2b3" }}>({f.count})</span>
            </div>
          ))}
        </div>
      ))}
    </aside>
  );
}
```

- [ ] **Step 4: Create `frontend/src/components/ItemList.tsx`**

```tsx
import { useQuery } from "@tanstack/react-query";
import { fetchItems } from "../api/client";
import { ItemCard } from "./ItemCard";

interface Props {
  filters: Record<string, string>;
  onOpen: (id: number) => void;
}

export function ItemList({ filters, onOpen }: Props) {
  const params = Object.fromEntries(Object.entries(filters).filter(([, v]) => v));
  const { data, isLoading } = useQuery({
    queryKey: ["items", params],
    queryFn: () => fetchItems(params),
  });
  if (isLoading) return <div>加载中…</div>;
  if (!data || data.total === 0) return <div>暂无结果</div>;
  return (
    <div style={{ flex: 1 }}>
      <div style={{ color: "#667085", marginBottom: 10 }}>共 {data.total} 条</div>
      {data.items.map((item) => (
        <ItemCard key={item.id} item={item} onClick={() => onOpen(item.id)} />
      ))}
    </div>
  );
}
```

- [ ] **Step 5: Create `frontend/src/pages/HomePage.tsx`**

```tsx
import { useState } from "react";
import { FacetSidebar } from "../components/FacetSidebar";
import { ItemList } from "../components/ItemList";
import { ItemDetail } from "../components/ItemDetail";

export function HomePage() {
  const [filters, setFilters] = useState<Record<string, string>>({ q: "" });
  const [openId, setOpenId] = useState<number | null>(null);

  const setFilter = (key: string, value: string) =>
    setFilters((f) => ({ ...f, [key]: value }));

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: 24 }}>
      <h1>技术新闻追踪</h1>
      <input
        placeholder="搜索标题/摘要…"
        value={filters.q ?? ""}
        onChange={(e) => setFilter("q", e.target.value)}
        style={{ width: "100%", padding: 10, marginBottom: 16,
                 border: "1px solid #d0d5dd", borderRadius: 8 }}
      />
      <div style={{ display: "flex" }}>
        <FacetSidebar selected={filters} onSelect={setFilter} />
        <ItemList filters={filters} onOpen={setOpenId} />
      </div>
      {openId !== null && (
        <div onClick={() => setOpenId(null)} style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.35)",
          display: "flex", justifyContent: "flex-end",
        }}>
          <div onClick={(e) => e.stopPropagation()} style={{
            width: 560, maxWidth: "90vw", background: "#fff",
            height: "100%", overflowY: "auto",
          }}>
            <ItemDetail id={openId} />
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 6: Replace `frontend/src/App.tsx`**

```tsx
import { HomePage } from "./pages/HomePage";

export default function App() {
  return <HomePage />;
}
```

- [ ] **Step 7: Verify build**

Run: `cd frontend && npm run build`
Expected: builds without type errors.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/
git commit -m "feat: add list, card, detail, facet sidebar and home page"
```

---

## Phase 12 — Deployment

### Task 22: App entrypoint (API + scheduler + seeding)

**Files:**

- Create: `backend/app/entry.py`
- Test: `backend/tests/unit/test_entry_import.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_entry_import.py`

```python
def test_entry_exposes_app():
    from app.entry import app
    assert app is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_entry_import.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/entry.py`**

```python
import logging
import os
from app.api.main import create_app
from app.db import engine, SessionLocal
from app.models import Base
from app.sources.registry import seed_sources_from_yaml

logging.basicConfig(level=logging.INFO)
app = create_app()


@app.on_event("startup")
def _startup():
    Base.metadata.create_all(engine)
    seed_path = os.path.join(os.path.dirname(__file__), "sources", "seed_sources.yaml")
    session = SessionLocal()
    try:
        seed_sources_from_yaml(session, seed_path)
    finally:
        session.close()
    if os.environ.get("ENABLE_SCHEDULER", "1") == "1":
        from app.scheduler import start_scheduler
        app.state.scheduler = start_scheduler()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ENABLE_SCHEDULER=0 pytest tests/unit/test_entry_import.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/entry.py backend/tests/unit/test_entry_import.py
git commit -m "feat: add app entrypoint with seeding and scheduler bootstrap"
```

---

### Task 23: Docker Compose + Dockerfiles

**Files:**

- Create: `backend/Dockerfile`
- Create: `frontend/Dockerfile`
- Create: `frontend/nginx.conf`
- Create: `docker-compose.yml`

- [ ] **Step 1: Create `backend/Dockerfile`**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY . .
RUN python -m playwright install --with-deps chromium || true
CMD ["uvicorn", "app.entry:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Create `frontend/Dockerfile`**

```dockerfile
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
```

- [ ] **Step 3: Create `frontend/nginx.conf`**

```nginx
server {
  listen 80;
  location / {
    root /usr/share/nginx/html;
    try_files $uri /index.html;
  }
  location /api/ {
    proxy_pass http://backend:8000/;
  }
}
```

- [ ] **Step 4: Create `docker-compose.yml`**

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_DB: osnews
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"

  backend:
    build: ./backend
    env_file: .env
    environment:
      DATABASE_URL: postgresql+psycopg://postgres:postgres@db:5432/osnews
    depends_on:
      - db
    ports:
      - "8000:8000"

  frontend:
    build: ./frontend
    depends_on:
      - backend
    ports:
      - "8080:80"

volumes:
  pgdata:
```

- [ ] **Step 5: Verify compose config**

Run: `docker compose config`
Expected: valid merged config printed, no errors.

- [ ] **Step 6: Commit**

```bash
git add backend/Dockerfile frontend/Dockerfile frontend/nginx.conf docker-compose.yml
git commit -m "feat: add Docker Compose deployment for db, backend, frontend"
```

---

### Task 24: Project README + final full-suite verification

**Files:**

- Create: `README.md`

- [ ] **Step 1: Create `README.md`** with: project overview, V1 scope, architecture diagram reference to the spec, local dev setup (`pip install -e ".[dev]"`, `pytest`, `npm run dev`), Docker Compose run (`docker compose up --build`), env vars table (mirror `.env.example`), and the open follow-ups (internal LLM gateway endpoint, search provider, Firecrawl AGPL note).
- [ ] **Step 2: Run the full backend suite**

Run: `cd backend && ENABLE_SCHEDULER=0 pytest -v`
Expected: all tests PASS.

- [ ] **Step 3: Build frontend**

Run: `cd frontend && npm run build`
Expected: builds clean.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: add project README and finalize V1"
```

---

## Phase 13 — Structured Data Stream & ApiFetcher (design v2 additions)

> These tasks add the second (structured) stream from the updated design (spec §5.9, §6). Foundations (`enums.py`, `models.py`, seed yaml, registry) were already updated in Tasks 1, 2, 17. Implement this phase after Task 18 (scheduler) and before Task 24's final full-suite run. The frontend task (S6) belongs with the frontend phase.

### Task 25 (S1): Structured contracts + ApiFetcher + adapter registry

**Files:**

- Create: `backend/app/structured/__init__.py`
- Create: `backend/app/structured/schemas.py`
- Create: `backend/app/structured/adapters/__init__.py`
- Create: `backend/app/structured/adapters/base.py`
- Create: `backend/app/fetchers/api.py`
- Test: `backend/tests/unit/test_api_fetcher.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/unit/test_api_fetcher.py`

```python
from app.fetchers.api import ApiFetcher
from app.structured.schemas import StructuredBatch, AdvisoryRecord
from app.models import Source
from app.enums import SourceType


class _StubAdapter:
    def parse(self, payload) -> StructuredBatch:
        return StructuredBatch(advisories=[AdvisoryRecord(
            advisory_id=payload["id"], vendor="ubuntu", severity="low")])


class _StubHttp:
    def get(self, url, headers=None):
        class _R:
            def raise_for_status(self): pass
            def json(self): return {"id": "USN-1-1"}
        return _R()


def test_api_fetcher_uses_adapter_and_returns_batch():
    src = Source(id=9, name="u", type=SourceType.API,
                 url="https://x/notices.json", adapter="stub")
    fetcher = ApiFetcher(http=_StubHttp(), adapters={"stub": _StubAdapter()})
    batch = fetcher.fetch_structured(src)
    assert len(batch.advisories) == 1
    assert batch.advisories[0].advisory_id == "USN-1-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_api_fetcher.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/structured/__init__.py`** and `backend/app/structured/adapters/__init__.py` (both empty files).

- [ ] **Step 4: Create `backend/app/structured/schemas.py`**

```python
from datetime import datetime
from pydantic import BaseModel, Field


class AdvisoryRecord(BaseModel):
    vendor: str
    advisory_id: str
    cve_ids: list[str] = Field(default_factory=list)
    severity: str = "unknown"
    title: str | None = None
    summary_text: str | None = None
    affected_products: list = Field(default_factory=list)
    fixed_versions: list = Field(default_factory=list)
    published_at: datetime | None = None
    updated_at: datetime | None = None
    url: str | None = None


class LifecycleRecord(BaseModel):
    vendor: str
    product: str
    version: str
    release_date: datetime | None = None
    ga_date: datetime | None = None
    eol_date: datetime | None = None
    eus_date: datetime | None = None
    phase: str | None = None
    url: str | None = None


class ImageRecord(BaseModel):
    vendor: str
    product: str
    image_tag: str
    arch: str | None = None
    cloud: str | None = None
    image_id: str | None = None
    checksum: str | None = None
    released_at: datetime | None = None


class CompatibilityRecord(BaseModel):
    vendor: str
    kind: str
    name: str
    product: str | None = None
    version: str | None = None
    arch: str | None = None
    status: str | None = None
    url: str | None = None


class StructuredBatch(BaseModel):
    advisories: list[AdvisoryRecord] = Field(default_factory=list)
    lifecycles: list[LifecycleRecord] = Field(default_factory=list)
    images: list[ImageRecord] = Field(default_factory=list)
    compatibilities: list[CompatibilityRecord] = Field(default_factory=list)
```

- [ ] **Step 5: Create `backend/app/structured/adapters/base.py`**

```python
from typing import Protocol, runtime_checkable
from app.structured.schemas import StructuredBatch


@runtime_checkable
class SourceAdapter(Protocol):
    def parse(self, payload) -> StructuredBatch: ...


def get_adapters() -> dict[str, SourceAdapter]:
    """Registry of adapter-name -> instance. Extend as sources are confirmed."""
    from app.structured.adapters.ubuntu_security import UbuntuSecurityAdapter
    return {
        "ubuntu_security": UbuntuSecurityAdapter(),
    }
```

- [ ] **Step 6: Create `backend/app/fetchers/api.py`**

```python
import httpx
from app.config import get_settings
from app.models import Source
from app.structured.schemas import StructuredBatch
from app.structured.adapters.base import get_adapters


class ApiFetcher:
    def __init__(self, http=None, adapters=None):
        self._http = http or httpx.Client(timeout=30.0,
                                          headers={"User-Agent": get_settings().fetch_user_agent})
        self._adapters = adapters if adapters is not None else get_adapters()

    def fetch_structured(self, source: Source) -> StructuredBatch:
        adapter = self._adapters[source.adapter]
        resp = self._http.get(source.url)
        resp.raise_for_status()
        return adapter.parse(resp.json())
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_api_fetcher.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add backend/app/structured/ backend/app/fetchers/api.py backend/tests/unit/test_api_fetcher.py
git commit -m "feat: add structured contracts, ApiFetcher and adapter registry"
```

---

### Task 26 (S2): Ubuntu security adapter (worked example)

> Worked, fully-specified adapter for `notices.json` (public, stable schema). Other adapters (`redhat_securitydata`, `redhat_lifecycle`, image, compatibility) follow the same `SourceAdapter` contract and each get their own task once that source's response schema is confirmed (spec §13 follow-up). Register each in `get_adapters()`.

**Files:**

- Create: `backend/app/structured/adapters/ubuntu_security.py`
- Create: `backend/tests/fixtures/ubuntu_notices.json`
- Test: `backend/tests/unit/test_ubuntu_security_adapter.py`

- [ ] **Step 1: Create fixture** — `backend/tests/fixtures/ubuntu_notices.json`

```json
{
  "notices": [
    {
      "id": "USN-6789-1",
      "title": "Linux kernel vulnerabilities",
      "summary": "Several security issues were fixed in the Linux kernel.",
      "published": "2026-05-20T10:00:00Z",
      "cves": ["CVE-2026-1111", "CVE-2026-2222"],
      "references": ["https://ubuntu.com/security/notices/USN-6789-1"]
    }
  ]
}
```

- [ ] **Step 2: Write the failing test** — `backend/tests/unit/test_ubuntu_security_adapter.py`

```python
import json
import os
from app.structured.adapters.ubuntu_security import UbuntuSecurityAdapter
from app.structured.adapters.base import SourceAdapter


def test_ubuntu_adapter_parses_notices(fixtures_dir):
    with open(os.path.join(fixtures_dir, "ubuntu_notices.json")) as fh:
        payload = json.load(fh)
    adapter = UbuntuSecurityAdapter()
    batch = adapter.parse(payload)
    assert isinstance(adapter, SourceAdapter)
    assert len(batch.advisories) == 1
    adv = batch.advisories[0]
    assert adv.advisory_id == "USN-6789-1"
    assert adv.vendor == "ubuntu"
    assert adv.cve_ids == ["CVE-2026-1111", "CVE-2026-2222"]
    assert adv.published_at is not None
    assert adv.url == "https://ubuntu.com/security/notices/USN-6789-1"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && pytest tests/unit/test_ubuntu_security_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Create `backend/app/structured/adapters/ubuntu_security.py`**

```python
from dateutil import parser as dateparser
from app.structured.schemas import StructuredBatch, AdvisoryRecord
from app.structured.adapters.base import SourceAdapter


class UbuntuSecurityAdapter(SourceAdapter):
    def parse(self, payload) -> StructuredBatch:
        advisories: list[AdvisoryRecord] = []
        for n in payload.get("notices", []):
            published = None
            if n.get("published"):
                try:
                    published = dateparser.parse(n["published"]).replace(tzinfo=None)
                except (ValueError, TypeError):
                    published = None
            refs = n.get("references") or []
            advisories.append(AdvisoryRecord(
                vendor="ubuntu",
                advisory_id=n["id"],
                cve_ids=list(n.get("cves", [])),
                severity="unknown",
                title=n.get("title"),
                summary_text=n.get("summary"),
                published_at=published,
                url=refs[0] if refs else None,
            ))
        return StructuredBatch(advisories=advisories)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/unit/test_ubuntu_security_adapter.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/structured/adapters/ubuntu_security.py backend/tests/fixtures/ubuntu_notices.json backend/tests/unit/test_ubuntu_security_adapter.py
git commit -m "feat: add Ubuntu security source adapter"
```

---

### Task 27 (S3): StructuredRepository (idempotent upsert)

**Files:**

- Create: `backend/app/structured/repository.py`
- Test: `backend/tests/integration/test_structured_repository.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/integration/test_structured_repository.py`

```python
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from app.models import Base, Source, SecurityAdvisory
from app.enums import SourceType, Stream
from app.structured.schemas import StructuredBatch, AdvisoryRecord
from app.structured.repository import StructuredRepository


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Source(id=1, name="u", type=SourceType.API, url="x", stream=Stream.STRUCTURED))
    s.commit()
    yield s
    s.close()


def _batch(sev="low"):
    return StructuredBatch(advisories=[AdvisoryRecord(
        vendor="ubuntu", advisory_id="USN-1-1", cve_ids=["CVE-1"], severity=sev)])


def test_upsert_inserts_then_updates_not_duplicates(session):
    repo = StructuredRepository(session)
    n1 = repo.upsert_batch(source_id=1, batch=_batch("low"))
    n2 = repo.upsert_batch(source_id=1, batch=_batch("important"))  # same key, changed field
    assert n1["advisories"] == 1
    assert session.scalar(select(func.count(SecurityAdvisory.id))) == 1  # no dup
    row = session.scalar(select(SecurityAdvisory))
    assert row.severity == "important"  # updated in place
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/integration/test_structured_repository.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/structured/repository.py`**

```python
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import SecurityAdvisory, ProductLifecycle, ImageRelease, CompatibilityEntry
from app.structured.schemas import StructuredBatch


class StructuredRepository:
    def __init__(self, session: Session):
        self._s = session

    def _upsert(self, model, key_filter: dict, values: dict) -> bool:
        existing = self._s.scalar(select(model).filter_by(**key_filter))
        if existing is None:
            self._s.add(model(**key_filter, **values))
            return True
        for k, v in values.items():
            setattr(existing, k, v)
        return True

    def upsert_batch(self, source_id: int, batch: StructuredBatch) -> dict:
        counts = {"advisories": 0, "lifecycles": 0, "images": 0, "compatibilities": 0}
        for a in batch.advisories:
            self._upsert(SecurityAdvisory,
                         {"vendor": a.vendor, "advisory_id": a.advisory_id},
                         {"source_id": source_id, "cve_ids": a.cve_ids, "severity": a.severity,
                          "title": a.title, "summary_text": a.summary_text,
                          "affected_products": a.affected_products, "fixed_versions": a.fixed_versions,
                          "published_at": a.published_at, "updated_at": a.updated_at, "url": a.url})
            counts["advisories"] += 1
        for lc in batch.lifecycles:
            self._upsert(ProductLifecycle,
                         {"vendor": lc.vendor, "product": lc.product, "version": lc.version},
                         {"source_id": source_id, "release_date": lc.release_date,
                          "ga_date": lc.ga_date, "eol_date": lc.eol_date, "eus_date": lc.eus_date,
                          "phase": lc.phase, "url": lc.url})
            counts["lifecycles"] += 1
        for im in batch.images:
            self._upsert(ImageRelease,
                         {"vendor": im.vendor, "product": im.product, "image_tag": im.image_tag,
                          "arch": im.arch, "cloud": im.cloud},
                         {"source_id": source_id, "image_id": im.image_id,
                          "checksum": im.checksum, "released_at": im.released_at})
            counts["images"] += 1
        for c in batch.compatibilities:
            self._upsert(CompatibilityEntry,
                         {"vendor": c.vendor, "kind": c.kind, "name": c.name,
                          "product": c.product, "version": c.version, "arch": c.arch},
                         {"source_id": source_id, "status": c.status,
                          "snapshot_at": datetime.utcnow(), "url": c.url})
            counts["compatibilities"] += 1
        self._s.commit()
        return counts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/integration/test_structured_repository.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/structured/repository.py backend/tests/integration/test_structured_repository.py
git commit -m "feat: add structured repository with idempotent upsert"
```

---

### Task 28 (S4): Relevance pre-filter for high-volume news sources

> Modifies the news `Pipeline` from Task 15 so sources with `relevance_filter=true` (e.g. arXiv) drop irrelevant items before the LLM enrich step.

**Files:**

- Modify: `backend/app/pipeline.py`
- Test: `backend/tests/integration/test_pipeline_relevance.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/integration/test_pipeline_relevance.py`

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Source
from app.enums import SourceType, Importance, InfoType
from app.schemas import RawItem, ExtractedDoc, EnrichedFields, EntityRef
from app.pipeline import Pipeline


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Source(id=1, name="arXiv", type=SourceType.RSS, url="u",
                 relevance_filter=True, relevance_keywords="scheduler, kernel"))
    s.commit()
    yield s
    s.close()


class _Fetcher:
    def fetch(self, source):
        return [
            RawItem(source_id=1, title="A new CPU scheduler design", url="https://x/a", raw_content="about kernel scheduler"),
            RawItem(source_id=1, title="A study of medieval poetry", url="https://x/b", raw_content="poetry"),
        ]


class _Extractor:
    def extract(self, url):
        return ExtractedDoc(url=url, title="t", clean_content="body")


class _Enricher:
    def enrich(self, n):
        return EnrichedFields(title_tldr="x", summary="s", key_points=["a"],
                              info_type=InfoType.PAPER, importance=Importance.MEDIUM,
                              why_it_matters="w", main_category="OS性能发展",
                              sub_tags=[], entities=[EntityRef(type="topic", name="scheduler")],
                              confidence=0.8)


def _relevance(title, content, keywords):
    return "scheduler" in (title + content).lower()


def test_relevance_filter_drops_irrelevant(session):
    src = session.get(Source, 1)
    pipeline = Pipeline(session=session, extractor=_Extractor(), enricher=_Enricher(),
                        relevance_fn=_relevance)
    n = pipeline.run_source(src, fetcher=_Fetcher())
    assert n == 1  # only the scheduler item kept
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/integration/test_pipeline_relevance.py -v`
Expected: FAIL (Pipeline has no `relevance_fn` param)

- [ ] **Step 3: Modify `backend/app/pipeline.py`**

Update the constructor and the per-item loop. Replace the class body with:

```python
import logging
from sqlalchemy.orm import Session
from app.models import Source
from app.enums import SourceType
from app.processing.normalizer import normalize
from app.processing.relevance import llm_relevance
from app.repository import Repository

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, session: Session, extractor, enricher, relevance_fn=llm_relevance):
        self._session = session
        self._extractor = extractor
        self._enricher = enricher
        self._relevance_fn = relevance_fn
        self._repo = Repository(session)

    def run_source(self, source: Source, fetcher) -> int:
        try:
            raw_items = fetcher.fetch(source)
        except Exception:
            logger.exception("fetch failed for source %s", source.name)
            source.fail_count += 1
            source.health_status = "error"
            self._session.commit()
            return 0

        new_count = 0
        for raw in raw_items:
            doc = self._extract_for(raw, source)
            n = normalize(raw, doc)
            if source.relevance_filter and not self._relevance_fn(
                n.title, n.clean_content, source.relevance_keywords or ""
            ):
                continue
            if self._repo.exists_by_canonical(n.canonical_url):
                self._repo.merge_source_link(n.canonical_url, source.id, raw.url)
                continue
            try:
                fields = self._enricher.enrich(n)
            except Exception:
                logger.exception("enrich failed for %s", n.canonical_url)
                continue
            self._repo.save_enriched(n, fields)
            new_count += 1

        source.health_status = "ok"
        self._session.commit()
        return new_count

    def _extract_for(self, raw, source: Source):
        from app.schemas import ExtractedDoc
        if source.type == SourceType.RSS:
            if raw.raw_content and len(raw.raw_content) > 200:
                return ExtractedDoc(url=raw.url, title=raw.title,
                                    clean_content=raw.raw_content,
                                    published_at=raw.published_at)
            return self._extractor.extract(raw.url)
        return ExtractedDoc(url=raw.url, title=raw.title,
                            clean_content=raw.raw_content or "",
                            published_at=raw.published_at)
```

- [ ] **Step 4: Run both pipeline tests to verify they pass**

Run: `cd backend && pytest tests/integration/test_pipeline.py tests/integration/test_pipeline_relevance.py -v`
Expected: PASS (the original dedup test still passes; relevance test passes)

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipeline.py backend/tests/integration/test_pipeline_relevance.py
git commit -m "feat: add relevance pre-filter for high-volume news sources"
```

---

### Task 29 (S5): Structured pipeline + scheduler routing by stream

**Files:**

- Create: `backend/app/structured/pipeline.py`
- Modify: `backend/app/scheduler.py`
- Test: `backend/tests/integration/test_structured_pipeline.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/integration/test_structured_pipeline.py`

```python
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from app.models import Base, Source, SecurityAdvisory
from app.enums import SourceType, Stream
from app.structured.schemas import StructuredBatch, AdvisoryRecord
from app.structured.pipeline import StructuredPipeline


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Source(id=1, name="u", type=SourceType.API, url="x",
                 adapter="stub", stream=Stream.STRUCTURED))
    s.commit()
    yield s
    s.close()


class _StubApiFetcher:
    def fetch_structured(self, source):
        return StructuredBatch(advisories=[AdvisoryRecord(
            vendor="ubuntu", advisory_id="USN-9-1", severity="critical")])


def test_structured_pipeline_upserts(session):
    src = session.get(Source, 1)
    pipeline = StructuredPipeline(session=session)
    counts = pipeline.run_source(src, fetcher=_StubApiFetcher())
    assert counts["advisories"] == 1
    assert session.scalar(select(func.count(SecurityAdvisory.id))) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/integration/test_structured_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/structured/pipeline.py`**

```python
import logging
from sqlalchemy.orm import Session
from app.models import Source
from app.structured.repository import StructuredRepository

logger = logging.getLogger(__name__)


class StructuredPipeline:
    def __init__(self, session: Session):
        self._session = session
        self._repo = StructuredRepository(session)

    def run_source(self, source: Source, fetcher) -> dict:
        try:
            batch = fetcher.fetch_structured(source)
        except Exception:
            logger.exception("structured fetch failed for %s", source.name)
            source.fail_count += 1
            source.health_status = "error"
            self._session.commit()
            return {"advisories": 0, "lifecycles": 0, "images": 0, "compatibilities": 0}
        counts = self._repo.upsert_batch(source.id, batch)
        source.health_status = "ok"
        self._session.commit()
        return counts
```

- [ ] **Step 4: Modify `backend/app/scheduler.py`** — route by stream and support `api` type. Replace `run_source_job` and `build_fetcher` with:

```python
def build_fetcher(source: Source, extractor, search):
    if source.type == SourceType.RSS:
        return RssFetcher()
    if source.type == SourceType.PAGE_MONITOR:
        return PageMonitorFetcher(extractor=extractor)
    if source.type == SourceType.SEARCH:
        return SearchFetcher(search=search, extractor=extractor)
    if source.type == SourceType.API:
        from app.fetchers.api import ApiFetcher
        return ApiFetcher()
    raise ValueError(f"unknown source type {source.type}")


def run_source_job(source_id: int):
    session = SessionLocal()
    try:
        source = session.get(Source, source_id)
        if not source or not source.enabled:
            return
        if source.stream == Stream.STRUCTURED:
            from app.fetchers.api import ApiFetcher
            from app.structured.pipeline import StructuredPipeline
            counts = StructuredPipeline(session=session).run_source(source, fetcher=ApiFetcher())
            logger.info("structured source %s -> %s", source.name, counts)
            return
        extractor = ScraplingExtractor()
        search = get_search_provider()
        fetcher = build_fetcher(source, extractor, search)
        pipeline = Pipeline(session=session, extractor=extractor, enricher=Enricher())
        n = pipeline.run_source(source, fetcher=fetcher)
        logger.info("source %s produced %d new items", source.name, n)
    finally:
        session.close()
```

Add the import at the top of `scheduler.py`: `from app.enums import SourceType, Stream`.

- [ ] **Step 5: Run tests**

Run: `cd backend && pytest tests/integration/test_structured_pipeline.py tests/unit/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/structured/pipeline.py backend/app/scheduler.py backend/tests/integration/test_structured_pipeline.py
git commit -m "feat: add structured pipeline and route scheduler by stream"
```

---

### Task 30 (S6): Structured API endpoints

**Files:**

- Create: `backend/app/api/structured_routes.py`
- Modify: `backend/app/api/main.py` (include the new router)
- Test: `backend/tests/integration/test_structured_api.py`

- [ ] **Step 1: Write the failing test** — `backend/tests/integration/test_structured_api.py`

```python
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Source, SecurityAdvisory, ProductLifecycle
from app.enums import SourceType, Stream
from app.api.main import create_app
from app.api.deps import get_db


@pytest.fixture
def client():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    seed = TestSession()
    seed.add(Source(id=1, name="u", type=SourceType.API, url="x", stream=Stream.STRUCTURED))
    seed.add(SecurityAdvisory(source_id=1, vendor="ubuntu", advisory_id="USN-1-1",
                              severity="critical", title="t"))
    seed.add(ProductLifecycle(source_id=1, vendor="redhat", product="RHEL", version="9"))
    seed.commit()
    seed.close()
    app = create_app()

    def _override():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def test_list_advisories_and_filter(client):
    assert client.get("/advisories").json()["total"] == 1
    assert client.get("/advisories?vendor=ubuntu&severity=critical").json()["total"] == 1
    assert client.get("/advisories?severity=low").json()["total"] == 0


def test_list_lifecycles(client):
    data = client.get("/lifecycles?vendor=redhat").json()
    assert data["total"] == 1
    assert data["items"][0]["product"] == "RHEL"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/integration/test_structured_api.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `backend/app/api/structured_routes.py`**

```python
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.models import SecurityAdvisory, ProductLifecycle, ImageRelease, CompatibilityEntry

router = APIRouter()


def _page(db, stmt, limit, offset, to_dict):
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return {"total": total, "items": [to_dict(r) for r in rows]}


@router.get("/advisories")
def advisories(db: Session = Depends(get_db), vendor: str | None = None,
               severity: str | None = None, q: str | None = None,
               limit: int = Query(50, le=200), offset: int = 0):
    stmt = select(SecurityAdvisory)
    if vendor:
        stmt = stmt.where(SecurityAdvisory.vendor == vendor)
    if severity:
        stmt = stmt.where(SecurityAdvisory.severity == severity)
    if q:
        stmt = stmt.where(SecurityAdvisory.title.ilike(f"%{q}%"))
    stmt = stmt.order_by(SecurityAdvisory.published_at.desc().nullslast())
    return _page(db, stmt, limit, offset, lambda r: {
        "id": r.id, "vendor": r.vendor, "advisory_id": r.advisory_id,
        "cve_ids": r.cve_ids or [], "severity": r.severity, "title": r.title,
        "published_at": r.published_at.isoformat() if r.published_at else None, "url": r.url,
    })


@router.get("/lifecycles")
def lifecycles(db: Session = Depends(get_db), vendor: str | None = None,
               product: str | None = None, limit: int = Query(50, le=200), offset: int = 0):
    stmt = select(ProductLifecycle)
    if vendor:
        stmt = stmt.where(ProductLifecycle.vendor == vendor)
    if product:
        stmt = stmt.where(ProductLifecycle.product == product)
    stmt = stmt.order_by(ProductLifecycle.eol_date.asc().nullslast())
    return _page(db, stmt, limit, offset, lambda r: {
        "id": r.id, "vendor": r.vendor, "product": r.product, "version": r.version,
        "eol_date": r.eol_date.isoformat() if r.eol_date else None, "phase": r.phase, "url": r.url,
    })


@router.get("/compatibility")
def compatibility(db: Session = Depends(get_db), vendor: str | None = None,
                  kind: str | None = None, q: str | None = None,
                  limit: int = Query(50, le=200), offset: int = 0):
    stmt = select(CompatibilityEntry)
    if vendor:
        stmt = stmt.where(CompatibilityEntry.vendor == vendor)
    if kind:
        stmt = stmt.where(CompatibilityEntry.kind == kind)
    if q:
        stmt = stmt.where(CompatibilityEntry.name.ilike(f"%{q}%"))
    return _page(db, stmt, limit, offset, lambda r: {
        "id": r.id, "vendor": r.vendor, "kind": r.kind, "name": r.name,
        "product": r.product, "version": r.version, "arch": r.arch,
        "status": r.status, "url": r.url,
    })
```

- [ ] **Step 4: Modify `backend/app/api/main.py`** — include the structured router. Update `create_app`:

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import router
from app.api.structured_routes import router as structured_router


def create_app() -> FastAPI:
    app = FastAPI(title="OS News Tracker")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(structured_router)
    return app


app = create_app()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/integration/test_structured_api.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/structured_routes.py backend/app/api/main.py backend/tests/integration/test_structured_api.py
git commit -m "feat: add structured-data API endpoints"
```

---

### Task 31 (S7): Frontend structured-data table view

> Belongs with the frontend phase (after Task 21). Adds a tab to switch between the news list and a structured data table (advisories / lifecycles / compatibility).

**Files:**

- Create: `frontend/src/api/structured.ts`
- Create: `frontend/src/components/StructuredTable.tsx`
- Modify: `frontend/src/pages/HomePage.tsx` (add a simple tab switch)

- [ ] **Step 1: Create `frontend/src/api/structured.ts`**

```typescript
const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export interface Paged<T> { total: number; items: T[]; }

export async function fetchStructured<T>(path: string, params: Record<string, string>): Promise<Paged<T>> {
  const qs = new URLSearchParams(Object.fromEntries(Object.entries(params).filter(([, v]) => v))).toString();
  const r = await fetch(`${BASE}/${path}?${qs}`);
  if (!r.ok) throw new Error(`failed to load ${path}`);
  return r.json();
}
```

- [ ] **Step 2: Create `frontend/src/components/StructuredTable.tsx`**

```tsx
import { useQuery } from "@tanstack/react-query";
import { fetchStructured, type Paged } from "../api/structured";

const COLUMNS: Record<string, { key: string; label: string }[]> = {
  advisories: [
    { key: "vendor", label: "厂商" }, { key: "advisory_id", label: "公告号" },
    { key: "severity", label: "等级" }, { key: "title", label: "标题" },
    { key: "published_at", label: "发布" },
  ],
  lifecycles: [
    { key: "vendor", label: "厂商" }, { key: "product", label: "产品" },
    { key: "version", label: "版本" }, { key: "eol_date", label: "EOL" },
    { key: "phase", label: "阶段" },
  ],
  compatibility: [
    { key: "vendor", label: "厂商" }, { key: "kind", label: "类型" },
    { key: "name", label: "名称" }, { key: "product", label: "产品" },
    { key: "status", label: "状态" },
  ],
};

export function StructuredTable({ resource }: { resource: "advisories" | "lifecycles" | "compatibility" }) {
  const cols = COLUMNS[resource];
  const { data, isLoading } = useQuery({
    queryKey: ["structured", resource],
    queryFn: () => fetchStructured<Record<string, unknown>>(resource, {}),
  });
  if (isLoading || !data) return <div>加载中…</div>;
  return (
    <div>
      <div style={{ color: "#667085", marginBottom: 10 }}>共 {data.total} 条</div>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
        <thead>
          <tr>{cols.map((c) => (
            <th key={c.key} style={{ textAlign: "left", borderBottom: "2px solid #eaecf0", padding: 8 }}>{c.label}</th>
          ))}</tr>
        </thead>
        <tbody>
          {data.items.map((row, i) => (
            <tr key={i}>{cols.map((c) => (
              <td key={c.key} style={{ borderBottom: "1px solid #f2f4f7", padding: 8 }}>
                {String((row as Record<string, unknown>)[c.key] ?? "")}
              </td>
            ))}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 3: Modify `frontend/src/pages/HomePage.tsx`** — add a tab switch above the content. Insert near the top of the returned JSX (after the `<h1>`), and render either the existing news view or the structured table based on a `view` state:

```tsx
// add near other useState hooks:
const [view, setView] = useState<"news" | "advisories" | "lifecycles" | "compatibility">("news");

// add this tab bar right after <h1>技术新闻追踪</h1>:
<div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
  {([["news", "新闻动态"], ["advisories", "安全公告"], ["lifecycles", "生命周期"], ["compatibility", "兼容性"]] as const).map(
    ([v, label]) => (
      <button key={v} onClick={() => setView(v)} style={{
        padding: "6px 12px", borderRadius: 8, cursor: "pointer",
        border: view === v ? "1px solid #2e90fa" : "1px solid #d0d5dd",
        background: view === v ? "#eff8ff" : "#fff",
      }}>{label}</button>
    )
  )}
</div>

// wrap the existing search input + facet/list flex container so it only renders when view === "news",
// and render <StructuredTable resource={view} /> otherwise (import it at top).
```

- [ ] **Step 4: Verify build**

Run: `cd frontend && npm run build`
Expected: builds without type errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/structured.ts frontend/src/components/StructuredTable.tsx frontend/src/pages/HomePage.tsx
git commit -m "feat: add structured-data table view with tab switch"
```

---

## Self-Review Notes (completed by plan author)

- **Spec coverage:** RSS/page-monitor/search fetchers (Tasks 9-11), Scrapling default + pluggable extractor (Task 7), pluggable search provider w/ internal-first (Task 8), normalizer + URL canonicalization (Task 5), dedup + cross-source merge (Tasks 6, 14), LLM enrich with the exact summary schema fields (Task 13), structured-entity faceted query (Tasks 14, 16), visual block distinction + importance colors (Tasks 20-21), per-source error isolation + enrich-failure non-blocking (Task 15), idempotency via url_hash/content_hash (Tasks 6, 14, 15), LLM cache (Task 12), Docker Compose single-host + permanent retention (Task 23, no purge job), 5 initial main categories (`enums.py`, seed yaml). All covered.
- **Out-of-scope honored:** no email/iWiki/subscription/auth/semantic-search/graph-db tasks.
- **Type consistency:** `EnrichedFields`, `NormalizedItem`, `ExtractedDoc`, `RawItem`, `SearchResult` reused consistently; `save_enriched`/`merge_source_link`/`exists_by_canonical` names match across repository, pipeline, and API tasks.
- **Open follow-ups (tracked in spec §13):** confirm internal LLM gateway endpoint + whether it offers web search (then flip `SEARCH_PROVIDER=internal`); Firecrawl AGPL legal check only if adopted; finalize the real seed source list.

### Phase 13 additions (structured stream, design v2)

- **Coverage:** `api` SourceType + `Stream` enum (Task 1); `sources` new fields + 4 structured tables (Task 2); seed yaml + registry pass new fields (Task 17); `ApiFetcher` + `SourceAdapter` registry + structured contracts (Task 25); worked Ubuntu security adapter (Task 26); idempotent `StructuredRepository` upsert (Task 27); relevance pre-filter for high-volume news sources (Task 28); `StructuredPipeline` + scheduler routing by `stream` (Task 29); structured API endpoints (Task 30); frontend structured table + tab switch (Task 31). Maps to spec §5.2/§5.9/§6/§7 (`论文/研究`)/§14.
- **Deferred (spec):** repo package metadata, mailing-list archives, compatibility *diff* tracking (V1 = snapshot only); remaining adapters (`redhat_securitydata`, `redhat_lifecycle`, image, compatibility) are per-source tasks pending each API's confirmed response schema (spec §13).
- **Type consistency:** `StructuredBatch`, `AdvisoryRecord`, `LifecycleRecord`, `ImageRecord`, `CompatibilityRecord`, `SourceAdapter` reused across fetcher/adapter/repository/pipeline; `fetch_structured` / `upsert_batch` / `parse` names consistent. `Pipeline` constructor gains `relevance_fn` (default `llm_relevance`).

