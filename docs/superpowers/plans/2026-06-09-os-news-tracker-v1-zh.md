# OS News Tracker V1 实现计划（中文版）

> **致执行者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务逐步实现本计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 构建技术新闻追踪 Agent 的 V1/MVP，含**两条流**：①*新闻流*——从 RSS + 结构化 API + 固定页 + 关键词搜索采集，用 LLM 富化（归类、子标签、结构化实体、结构化摘要）；②*结构化数据流*——把安全公告/生命周期/镜像/兼容性直接解析进类型化表（不走 LLM）。两者均存 Postgres，并通过带分面检索的 React 前端展示。

**架构：** 新闻流为 APScheduler 驱动的确定性流水线（`采集 → 归一化 →[相关性过滤]→ 去重 → 富化 → 入库`），结构化流为并行的 `采集 → 适配器解析 → upsert` 路径，二者按 `source.stream` 路由。采集器、正文提取、搜索、每源 API 适配器均通过协议（`Fetcher`、`ContentExtractor`、`SearchProvider`、`SourceAdapter`）做成可插拔。唯一的 LLM 环节是 `Enricher` 和一个可选的相关性判定。FastAPI 向 React（Vite + TypeScript）前端提供新闻 + 结构化接口。**不使用 agent / 自主循环**——见设计文档第 14 节。

**技术栈：** Python 3.11、FastAPI、SQLAlchemy 2.0 + Alembic、Postgres、APScheduler、feedparser、Scrapling、httpx、pydantic v2、pytest；React 18 + Vite + TypeScript + TanStack Query；Docker Compose。

---

## 文件结构

```
os-news-tracker/
  backend/
    pyproject.toml
    alembic.ini
    alembic/                      # 迁移
      env.py
      versions/
    app/
      __init__.py
      config.py                   # Settings（环境变量驱动）
      db.py                       # engine + session 工厂
      models.py                   # SQLAlchemy ORM 模型
      schemas.py                  # pydantic 契约（RawItem、ExtractedDoc、EnrichedFields...）
      enums.py                    # InfoType、Importance、SourceType、ItemStatus、EntityType、TagKind
      sources/
        registry.py               # 从 seed 配置 + DB 加载源
        seed_sources.yaml         # 初始源清单
      fetchers/
        base.py                   # Fetcher 协议 + FetchResult
        rss.py                    # RssFetcher
        page_monitor.py           # PageMonitorFetcher
        search.py                 # SearchFetcher
        api.py                    # ApiFetcher（结构化流）
      structured/                 # 结构化数据流
        schemas.py                # AdvisoryRecord/LifecycleRecord/ImageRecord/CompatibilityRecord/StructuredBatch
        repository.py             # 幂等 upsert
        pipeline.py               # 结构化运行路径
        adapters/
          base.py                 # SourceAdapter 协议 + 注册表
          ubuntu_security.py      # 示例适配器
      extract/
        base.py                   # ContentExtractor 协议 + ExtractedDoc
        scrapling_extractor.py    # 默认引擎
      search/
        base.py                   # SearchProvider 协议 + SearchResult
        internal_gateway.py       # 占位实现（受配置开关控制）
      processing/
        normalizer.py             # 归一化 RawItem -> NormalizedItem（纯函数）
        dedup.py                  # url_hash、simhash、近似去重匹配（纯函数）
        relevance.py              # 对搜索结果做轻量 LLM 相关性判断
        enricher.py               # LLM 富化 -> EnrichedFields
      llm/
        client.py                 # 可配置的 OpenAI 兼容客户端 + 缓存
      pipeline.py                 # 编排
      scheduler.py                # APScheduler 接线
      repository.py               # DB 读写查询
      api/
        __init__.py
        main.py                   # FastAPI app 工厂
        routes.py                 # /items、/items/{id}、/facets
        deps.py                   # DB session 依赖
    tests/
      conftest.py
      fixtures/                   # 保存的 RSS/HTML 样本 + LLM 响应
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

**边界划分：** 纯逻辑（`normalizer`、`dedup`）独立无 I/O，可单测。采集器/提取器/搜索都在协议背后，各自可用 fixture 测试且可替换。流水线只依赖协议，不依赖具体实现。

---

## 阶段 0 —— 项目脚手架

### Task 0：后端项目骨架 + 工具链

**文件：**

- 创建：`backend/pyproject.toml`
- 创建：`backend/app/__init__.py`
- 创建：`backend/tests/conftest.py`
- 创建：`.env.example`

- [x] **步骤 1：创建 `backend/pyproject.toml`**

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

- [x] **步骤 2：创建空的 `backend/app/__init__.py`**（空文件）。

- [ ] **步骤 3：创建 `backend/tests/conftest.py`**

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

- [ ] **步骤 4：创建 `.env.example`**

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

- [ ] **步骤 5：安装并验证**

运行：`cd backend && pip install -e ".[dev]"`
预期：安装无错误。

- [ ] **步骤 6：提交**

```bash
git add backend/pyproject.toml backend/app/__init__.py backend/tests/conftest.py .env.example
git commit -m "chore: scaffold backend project and tooling"
```

---

### Task 1：配置 + 枚举

**文件：**

- 创建：`backend/app/config.py`
- 创建：`backend/app/enums.py`
- 测试：`backend/tests/unit/test_config.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_config.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_config.py -v`
预期：失败，报 `ModuleNotFoundError: No module named 'app.config'`

- [ ] **步骤 3：创建 `backend/app/enums.py`**

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

- [ ] **步骤 4：创建 `backend/app/config.py`**

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

- [ ] **步骤 5：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_config.py -v`
预期：通过

- [ ] **步骤 6：提交**

```bash
git add backend/app/config.py backend/app/enums.py backend/tests/unit/test_config.py
git commit -m "feat: add settings and domain enums"
```

---

## 阶段 1 —— 数据模型 + 迁移

### Task 2：SQLAlchemy 模型

**文件：**

- 创建：`backend/app/db.py`
- 创建：`backend/app/models.py`
- 测试：`backend/tests/unit/test_models.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_models.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_models.py -v`
预期：失败，报 `ModuleNotFoundError: No module named 'app.models'`

- [ ] **步骤 3：创建 `backend/app/db.py`**

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

- [ ] **步骤 4：创建 `backend/app/models.py`**

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
    adapter: Mapped[str | None] = mapped_column(String(100), nullable=True)   # api 适配器名
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


# --- 结构化数据流的表（不走 LLM，由 API 直接解析）---

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

- [ ] **步骤 5：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_models.py -v`
预期：通过

- [ ] **步骤 6：提交**

```bash
git add backend/app/db.py backend/app/models.py backend/tests/unit/test_models.py
git commit -m "feat: add SQLAlchemy models and session factory"
```

---

### Task 3：Alembic 迁移（初始 schema）

**文件：**

- 创建：`backend/alembic.ini`
- 创建：`backend/alembic/env.py`
- 创建：`backend/alembic/versions/0001_initial.py`（自动生成后人工复核）

- [ ] **步骤 1：初始化 alembic**

运行：`cd backend && alembic init alembic`
然后编辑 `alembic/env.py`，设置 `target_metadata`：

```python
from app.models import Base
from app.config import get_settings

target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", get_settings().database_url)
```

- [ ] **步骤 2：针对 Postgres 开发库自动生成迁移**

运行：`cd backend && DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/osnews alembic revision --autogenerate -m "initial"`
预期：在 `alembic/versions/` 下生成创建所有表的文件。

- [ ] **步骤 3：应用并验证**

运行：`cd backend && DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/osnews alembic upgrade head`
预期：表已创建；`alembic current` 显示该 revision。

- [ ] **步骤 4：提交**

```bash
git add backend/alembic.ini backend/alembic/
git commit -m "feat: add initial alembic migration"
```

---

## 阶段 2 —— 契约（pydantic schemas）

### Task 4：流水线数据契约

**文件：**

- 创建：`backend/app/schemas.py`
- 测试：`backend/tests/unit/test_schemas.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_schemas.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_schemas.py -v`
预期：失败，报 `ModuleNotFoundError: No module named 'app.schemas'`

- [ ] **步骤 3：创建 `backend/app/schemas.py`**

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

- [ ] **步骤 4：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_schemas.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add backend/app/schemas.py backend/tests/unit/test_schemas.py
git commit -m "feat: add pydantic pipeline contracts"
```

---

## 阶段 3 —— 纯逻辑：归一化器

### Task 5：URL 规范化 + 归一化器

**文件：**

- 创建：`backend/app/processing/normalizer.py`
- 测试：`backend/tests/unit/test_normalizer.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_normalizer.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_normalizer.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/processing/normalizer.py`**

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

- [ ] **步骤 4：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_normalizer.py -v`
预期：通过（3 passed）

- [ ] **步骤 5：提交**

```bash
git add backend/app/processing/normalizer.py backend/tests/unit/test_normalizer.py
git commit -m "feat: add URL canonicalization and normalizer"
```

---

## 阶段 4 —— 纯逻辑：去重器

### Task 6：哈希 + simhash 近似去重检测

**文件：**

- 创建：`backend/app/processing/dedup.py`
- 测试：`backend/tests/unit/test_dedup.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_dedup.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_dedup.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/processing/dedup.py`**

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

- [ ] **步骤 4：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_dedup.py -v`
预期：通过（4 passed）

- [ ] **步骤 5：提交**

```bash
git add backend/app/processing/dedup.py backend/tests/unit/test_dedup.py
git commit -m "feat: add url/content hashing and simhash near-dup detection"
```

---

## 阶段 5 —— 提取 + 搜索协议

### Task 7：ContentExtractor 协议 + Scrapling 提取器

**文件：**

- 创建：`backend/app/extract/base.py`
- 创建：`backend/app/extract/scrapling_extractor.py`
- 测试：`backend/tests/unit/test_extract.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_extract.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_extract.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/extract/base.py`**

```python
from typing import Protocol, runtime_checkable
from app.schemas import ExtractedDoc


@runtime_checkable
class ContentExtractor(Protocol):
    def extract(self, url: str) -> ExtractedDoc: ...
```

- [ ] **步骤 4：创建 `backend/app/extract/scrapling_extractor.py`**

默认引擎封装 Scrapling 的 fetcher。fetcher 通过注入传入，便于测试用 fake。文本提取通过解析页面内容做简单的 HTML 转文本。

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

- [ ] **步骤 5：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_extract.py -v`
预期：通过（2 passed）

- [ ] **步骤 6：提交**

```bash
git add backend/app/extract/ backend/tests/unit/test_extract.py
git commit -m "feat: add ContentExtractor protocol and Scrapling extractor"
```

---

### Task 8：SearchProvider 协议 + 受配置控制的实现

**文件：**

- 创建：`backend/app/search/base.py`
- 创建：`backend/app/search/internal_gateway.py`
- 测试：`backend/tests/unit/test_search_provider.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_search_provider.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_search_provider.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/search/base.py`**

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

- [ ] **步骤 4：创建 `backend/app/search/internal_gateway.py`**（占位实现，待用 iWiki MCP 确认真实端点）

```python
import httpx
from app.config import get_settings
from app.search.base import SearchProvider, SearchResult


class InternalGatewaySearch(SearchProvider):
    """司内 LLM 网关联网搜索的占位实现。

    真实端点/鉴权需先用 iWiki MCP 确认后才启用（SEARCH_PROVIDER=internal）。
    在此之前 SEARCH_PROVIDER 保持为 'none'。
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

- [ ] **步骤 5：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_search_provider.py -v`
预期：通过（2 passed）

- [ ] **步骤 6：提交**

```bash
git add backend/app/search/ backend/tests/unit/test_search_provider.py
git commit -m "feat: add SearchProvider protocol with null + internal-gateway impls"
```

---

## 阶段 6 —— 采集器

### Task 9：Fetcher 协议 + RssFetcher

**文件：**

- 创建：`backend/app/fetchers/base.py`
- 创建：`backend/app/fetchers/rss.py`
- 创建：`backend/tests/fixtures/sample_feed.xml`
- 测试：`backend/tests/unit/test_rss_fetcher.py`

- [ ] **步骤 1：创建 fixture** —— `backend/tests/fixtures/sample_feed.xml`

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

- [ ] **步骤 2：编写失败测试** —— `backend/tests/unit/test_rss_fetcher.py`

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

- [ ] **步骤 3：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_rss_fetcher.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 4：创建 `backend/app/fetchers/base.py`**

```python
from typing import Protocol, runtime_checkable
from app.models import Source
from app.schemas import RawItem


@runtime_checkable
class Fetcher(Protocol):
    def fetch(self, source: Source) -> list[RawItem]: ...
```

- [ ] **步骤 5：创建 `backend/app/fetchers/rss.py`**

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

- [ ] **步骤 6：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_rss_fetcher.py -v`
预期：通过

- [ ] **步骤 7：提交**

```bash
git add backend/app/fetchers/base.py backend/app/fetchers/rss.py backend/tests/fixtures/sample_feed.xml backend/tests/unit/test_rss_fetcher.py
git commit -m "feat: add Fetcher protocol and RSS fetcher"
```

---

### Task 10：PageMonitorFetcher（固定页 + 变更检测）

**文件：**

- 创建：`backend/app/fetchers/page_monitor.py`
- 测试：`backend/tests/unit/test_page_monitor.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_page_monitor.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_page_monitor.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/fetchers/page_monitor.py`**

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

- [ ] **步骤 4：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_page_monitor.py -v`
预期：通过（2 passed）

- [ ] **步骤 5：提交**

```bash
git add backend/app/fetchers/page_monitor.py backend/tests/unit/test_page_monitor.py
git commit -m "feat: add page-monitor fetcher with change detection"
```

---

### Task 11：SearchFetcher（搜索 → 提取 → 相关性过滤）

**文件：**

- 创建：`backend/app/processing/relevance.py`
- 创建：`backend/app/fetchers/search.py`
- 测试：`backend/tests/unit/test_search_fetcher.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_search_fetcher.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_search_fetcher.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/processing/relevance.py`**

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

- [ ] **步骤 4：创建 `backend/app/fetchers/search.py`**

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

- [ ] **步骤 5：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_search_fetcher.py -v`
预期：通过

- [ ] **步骤 6：提交**

```bash
git add backend/app/processing/relevance.py backend/app/fetchers/search.py backend/tests/unit/test_search_fetcher.py
git commit -m "feat: add search fetcher with LLM relevance gate"
```

---

## 阶段 7 —— LLM 客户端 + 富化器

### Task 12：带缓存的可配置 LLM 客户端

**文件：**

- 创建：`backend/app/llm/client.py`
- 测试：`backend/tests/unit/test_llm_client.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_llm_client.py`

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
    assert transport.calls == 1  # 命中缓存
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_llm_client.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/llm/client.py`**

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

- [ ] **步骤 4：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_llm_client.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add backend/app/llm/client.py backend/tests/unit/test_llm_client.py
git commit -m "feat: add configurable OpenAI-compatible LLM client with cache"
```

---

### Task 13：Enricher（结构化摘要输出）

**文件：**

- 创建：`backend/app/processing/enricher.py`
- 测试：`backend/tests/unit/test_enricher.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_enricher.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_enricher.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/processing/enricher.py`**

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

- [ ] **步骤 4：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_enricher.py -v`
预期：通过（2 passed）

- [ ] **步骤 5：提交**

```bash
git add backend/app/processing/enricher.py backend/tests/unit/test_enricher.py
git commit -m "feat: add LLM enricher producing structured summary schema"
```

---

## 阶段 8 —— 仓储 + 流水线

### Task 14：仓储（幂等入库 + 跨源合并）

**文件：**

- 创建：`backend/app/repository.py`
- 测试：`backend/tests/integration/test_repository.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/integration/test_repository.py`

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
    assert repo.count_items() == before  # 不新增 item
    assert merged is True
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/integration/test_repository.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/repository.py`**

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

- [ ] **步骤 4：运行测试确认通过**

运行：`cd backend && pytest tests/integration/test_repository.py -v`
预期：通过（2 passed）

- [ ] **步骤 5：提交**

```bash
git add backend/app/repository.py backend/tests/integration/test_repository.py
git commit -m "feat: add repository with idempotent save and cross-source merge"
```

---

### Task 15：流水线编排

**文件：**

- 创建：`backend/app/pipeline.py`
- 测试：`backend/tests/integration/test_pipeline.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/integration/test_pipeline.py`

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
    assert n1 == 1     # 一条新 item
    assert n2 == 0     # 重跑被去重
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/integration/test_pipeline.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/pipeline.py`**

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
            # RSS 已带摘要；仅在内容过薄时才抓全文
            if raw.raw_content and len(raw.raw_content) > 200:
                return ExtractedDoc(url=raw.url, title=raw.title,
                                    clean_content=raw.raw_content,
                                    published_at=raw.published_at)
            return self._extractor.extract(raw.url)
        return ExtractedDoc(url=raw.url, title=raw.title,
                            clean_content=raw.raw_content or "",
                            published_at=raw.published_at)
```

- [ ] **步骤 4：运行测试确认通过**

运行：`cd backend && pytest tests/integration/test_pipeline.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add backend/app/pipeline.py backend/tests/integration/test_pipeline.py
git commit -m "feat: add pipeline orchestration with per-source isolation"
```

---

## 阶段 9 —— API

### Task 16：FastAPI 查询/分面/详情接口

**文件：**

- 创建：`backend/app/api/__init__.py`
- 创建：`backend/app/api/deps.py`
- 创建：`backend/app/api/routes.py`
- 创建：`backend/app/api/main.py`
- 测试：`backend/tests/integration/test_api.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/integration/test_api.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/integration/test_api.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/api/__init__.py`**（空文件）。

- [ ] **步骤 4：创建 `backend/app/api/deps.py`**

```python
from app.db import SessionLocal


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **步骤 5：创建 `backend/app/api/routes.py`**

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

- [ ] **步骤 6：创建 `backend/app/api/main.py`**

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

- [ ] **步骤 7：运行测试确认通过**

运行：`cd backend && pytest tests/integration/test_api.py -v`
预期：通过（4 passed）

- [ ] **步骤 8：提交**

```bash
git add backend/app/api/ backend/tests/integration/test_api.py
git commit -m "feat: add FastAPI query/facet/detail endpoints"
```

---

## 阶段 10 —— 源管理 + 调度

### Task 17：源注册 + seed 加载器

**文件：**

- 创建：`backend/app/sources/seed_sources.yaml`
- 创建：`backend/app/sources/registry.py`
- 测试：`backend/tests/integration/test_registry.py`

- [ ] **步骤 1：创建 `backend/app/sources/seed_sources.yaml`**

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

- [ ] **步骤 2：编写失败测试** —— `backend/tests/integration/test_registry.py`

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
    seed_sources_from_yaml(session, str(f))  # 第二次不得重复
    count = len(session.scalars(select(Source)).all())
    assert count == 1
```

- [ ] **步骤 3：运行测试确认失败**

运行：`cd backend && pytest tests/integration/test_registry.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 4：创建 `backend/app/sources/registry.py`**

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

- [ ] **步骤 5：运行测试确认通过**

运行：`cd backend && pytest tests/integration/test_registry.py -v`
预期：通过

- [ ] **步骤 6：提交**

```bash
git add backend/app/sources/ backend/tests/integration/test_registry.py
git commit -m "feat: add source registry with idempotent yaml seeding"
```

---

### Task 18：调度器接线 + 采集器工厂

**文件：**

- 创建：`backend/app/scheduler.py`
- 测试：`backend/tests/unit/test_scheduler.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_scheduler.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_scheduler.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/scheduler.py`**

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

- [ ] **步骤 4：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_scheduler.py -v`
预期：通过

- [ ] **步骤 5：运行后端完整测试套件**

运行：`cd backend && pytest -v`
预期：全部通过。

- [ ] **步骤 6：提交**

```bash
git add backend/app/scheduler.py backend/tests/unit/test_scheduler.py
git commit -m "feat: add scheduler wiring and fetcher factory"
```

---

## 阶段 11 —— 前端

### Task 19：前端脚手架 + API 客户端 + 类型

**文件：**

- 创建：`frontend/package.json`
- 创建：`frontend/vite.config.ts`
- 创建：`frontend/index.html`
- 创建：`frontend/src/main.tsx`
- 创建：`frontend/src/types.ts`
- 创建：`frontend/src/api/client.ts`

- [ ] **步骤 1：用 Vite React TS 脚手架**

运行：`cd frontend && npm create vite@latest . -- --template react-ts && npm install && npm install @tanstack/react-query`
预期：项目脚手架完成，依赖已安装。

- [ ] **步骤 2：创建 `frontend/src/types.ts`**

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

- [ ] **步骤 3：创建 `frontend/src/api/client.ts`**

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

- [ ] **步骤 4：替换 `frontend/src/main.tsx`**

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

- [ ] **步骤 5：验证构建**

运行：`cd frontend && npm run build`
预期：无类型错误，构建成功。

- [ ] **步骤 6：提交**

```bash
git add frontend/package.json frontend/vite.config.ts frontend/index.html frontend/src/main.tsx frontend/src/types.ts frontend/src/api/client.ts
git commit -m "feat: scaffold frontend with API client and types"
```

---

### Task 20：带视觉区分的徽标组件

**文件：**

- 创建：`frontend/src/components/ImportanceBadge.tsx`
- 创建：`frontend/src/components/InfoTypeBadge.tsx`

- [ ] **步骤 1：创建 `frontend/src/components/ImportanceBadge.tsx`**

重要度按 spec 用颜色区分（高=红/橙，中=黄，低=灰）。

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

- [ ] **步骤 2：创建 `frontend/src/components/InfoTypeBadge.tsx`**

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

- [ ] **步骤 3：验证构建**

运行：`cd frontend && npm run build`
预期：无错误构建成功。

- [ ] **步骤 4：提交**

```bash
git add frontend/src/components/ImportanceBadge.tsx frontend/src/components/InfoTypeBadge.tsx
git commit -m "feat: add importance and info-type badges"
```

---

### Task 21：列表、卡片、详情、分面侧栏、页面接线

**文件：**

- 创建：`frontend/src/components/ItemCard.tsx`
- 创建：`frontend/src/components/ItemList.tsx`
- 创建：`frontend/src/components/ItemDetail.tsx`
- 创建：`frontend/src/components/FacetSidebar.tsx`
- 创建：`frontend/src/pages/HomePage.tsx`
- 修改：`frontend/src/App.tsx`

- [ ] **步骤 1：创建 `frontend/src/components/ItemCard.tsx`**

列表行按 spec 展示 `title_tldr + info_type + importance + main_category`。

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

- [ ] **步骤 2：创建 `frontend/src/components/ItemDetail.tsx`**

详情页按 spec 让每个区块视觉区分（摘要 / 关键点列表卡片 / 影响意义强调块 / 实体）。

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

- [ ] **步骤 3：创建 `frontend/src/components/FacetSidebar.tsx`**

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

- [ ] **步骤 4：创建 `frontend/src/components/ItemList.tsx`**

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

- [ ] **步骤 5：创建 `frontend/src/pages/HomePage.tsx`**

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

- [ ] **步骤 6：替换 `frontend/src/App.tsx`**

```tsx
import { HomePage } from "./pages/HomePage";

export default function App() {
  return <HomePage />;
}
```

- [ ] **步骤 7：验证构建**

运行：`cd frontend && npm run build`
预期：无类型错误，构建成功。

- [ ] **步骤 8：提交**

```bash
git add frontend/src/
git commit -m "feat: add list, card, detail, facet sidebar and home page"
```

---

## 阶段 12 —— 部署

### Task 22：应用入口（API + 调度 + 初始化 seed）

**文件：**

- 创建：`backend/app/entry.py`
- 测试：`backend/tests/unit/test_entry_import.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_entry_import.py`

```python
def test_entry_exposes_app():
    from app.entry import app
    assert app is not None
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_entry_import.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/entry.py`**

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

- [ ] **步骤 4：运行测试确认通过**

运行：`cd backend && ENABLE_SCHEDULER=0 pytest tests/unit/test_entry_import.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add backend/app/entry.py backend/tests/unit/test_entry_import.py
git commit -m "feat: add app entrypoint with seeding and scheduler bootstrap"
```

---

### Task 23：Docker Compose + Dockerfile

**文件：**

- 创建：`backend/Dockerfile`
- 创建：`frontend/Dockerfile`
- 创建：`frontend/nginx.conf`
- 创建：`docker-compose.yml`

- [ ] **步骤 1：创建 `backend/Dockerfile`**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY . .
RUN python -m playwright install --with-deps chromium || true
CMD ["uvicorn", "app.entry:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **步骤 2：创建 `frontend/Dockerfile`**

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

- [ ] **步骤 3：创建 `frontend/nginx.conf`**

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

- [ ] **步骤 4：创建 `docker-compose.yml`**

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

- [ ] **步骤 5：验证 compose 配置**

运行：`docker compose config`
预期：打印合并后的有效配置，无错误。

- [ ] **步骤 6：提交**

```bash
git add backend/Dockerfile frontend/Dockerfile frontend/nginx.conf docker-compose.yml
git commit -m "feat: add Docker Compose deployment for db, backend, frontend"
```

---

### Task 24：项目 README + 最终完整套件验证

**文件：**

- 创建：`README.md`

- [ ] **步骤 1：创建 `README.md`**，内容包含：项目概述、V1 范围、引用 spec 的架构图、本地开发步骤（`pip install -e ".[dev]"`、`pytest`、`npm run dev`）、Docker Compose 运行（`docker compose up --build`）、环境变量表（对应 `.env.example`）、以及待办事项（司内 LLM 网关端点、搜索 provider、Firecrawl AGPL 说明）。
- [ ] **步骤 2：运行后端完整测试套件**

运行：`cd backend && ENABLE_SCHEDULER=0 pytest -v`
预期：全部通过。

- [ ] **步骤 3：构建前端**

运行：`cd frontend && npm run build`
预期：干净构建。

- [ ] **步骤 4：提交**

```bash
git add README.md
git commit -m "docs: add project README and finalize V1"
```

---

## 阶段 13 —— 结构化数据流与 ApiFetcher（设计 v2 新增）

> 这些任务实现更新设计里的第二条（结构化）流（设计文档 §5.9、§6）。基础部分（`enums.py`、`models.py`、seed yaml、registry）已在 Task 1、2、17 更新。本阶段建议在 Task 18（调度器）之后、Task 24 最终全量测试之前实现。前端任务（S6）归到前端阶段。

### Task 25 (S1)：结构化契约 + ApiFetcher + 适配器注册表

**文件：**

- 创建：`backend/app/structured/__init__.py`
- 创建：`backend/app/structured/schemas.py`
- 创建：`backend/app/structured/adapters/__init__.py`
- 创建：`backend/app/structured/adapters/base.py`
- 创建：`backend/app/fetchers/api.py`
- 测试：`backend/tests/unit/test_api_fetcher.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/unit/test_api_fetcher.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_api_fetcher.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/structured/__init__.py` 和 `backend/app/structured/adapters/__init__.py`**（均为空文件）。

- [ ] **步骤 4：创建 `backend/app/structured/schemas.py`**

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

- [ ] **步骤 5：创建 `backend/app/structured/adapters/base.py`**

```python
from typing import Protocol, runtime_checkable
from app.structured.schemas import StructuredBatch


@runtime_checkable
class SourceAdapter(Protocol):
    def parse(self, payload) -> StructuredBatch: ...


def get_adapters() -> dict[str, SourceAdapter]:
    """适配器名 -> 实例 的注册表。随源确认逐步扩展。"""
    from app.structured.adapters.ubuntu_security import UbuntuSecurityAdapter
    return {
        "ubuntu_security": UbuntuSecurityAdapter(),
    }
```

- [ ] **步骤 6：创建 `backend/app/fetchers/api.py`**

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

- [ ] **步骤 7：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_api_fetcher.py -v`
预期：通过

- [ ] **步骤 8：提交**

```bash
git add backend/app/structured/ backend/app/fetchers/api.py backend/tests/unit/test_api_fetcher.py
git commit -m "feat: add structured contracts, ApiFetcher and adapter registry"
```

---

### Task 26 (S2)：Ubuntu 安全适配器（示例）

> 以 `notices.json`（公开、稳定 schema）为完整示例适配器。其余适配器（`redhat_securitydata`、`redhat_lifecycle`、镜像、兼容性）遵循同一 `SourceAdapter` 契约，待各源响应 schema 确认后各自成任务（设计文档 §13 待办），并在 `get_adapters()` 注册。

**文件：**

- 创建：`backend/app/structured/adapters/ubuntu_security.py`
- 创建：`backend/tests/fixtures/ubuntu_notices.json`
- 测试：`backend/tests/unit/test_ubuntu_security_adapter.py`

- [ ] **步骤 1：创建 fixture** —— `backend/tests/fixtures/ubuntu_notices.json`

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

- [ ] **步骤 2：编写失败测试** —— `backend/tests/unit/test_ubuntu_security_adapter.py`

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

- [ ] **步骤 3：运行测试确认失败**

运行：`cd backend && pytest tests/unit/test_ubuntu_security_adapter.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 4：创建 `backend/app/structured/adapters/ubuntu_security.py`**

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

- [ ] **步骤 5：运行测试确认通过**

运行：`cd backend && pytest tests/unit/test_ubuntu_security_adapter.py -v`
预期：通过

- [ ] **步骤 6：提交**

```bash
git add backend/app/structured/adapters/ubuntu_security.py backend/tests/fixtures/ubuntu_notices.json backend/tests/unit/test_ubuntu_security_adapter.py
git commit -m "feat: add Ubuntu security source adapter"
```

---

### Task 27 (S3)：StructuredRepository（幂等 upsert）

**文件：**

- 创建：`backend/app/structured/repository.py`
- 测试：`backend/tests/integration/test_structured_repository.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/integration/test_structured_repository.py`

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
    n2 = repo.upsert_batch(source_id=1, batch=_batch("important"))  # 同主键，字段变化
    assert n1["advisories"] == 1
    assert session.scalar(select(func.count(SecurityAdvisory.id))) == 1  # 不重复
    row = session.scalar(select(SecurityAdvisory))
    assert row.severity == "important"  # 原地更新
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/integration/test_structured_repository.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/structured/repository.py`**

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

- [ ] **步骤 4：运行测试确认通过**

运行：`cd backend && pytest tests/integration/test_structured_repository.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add backend/app/structured/repository.py backend/tests/integration/test_structured_repository.py
git commit -m "feat: add structured repository with idempotent upsert"
```

---

### Task 28 (S4)：高量新闻源的相关性预过滤

> 修改 Task 15 的新闻 `Pipeline`，使 `relevance_filter=true` 的源（如 arXiv）在 LLM 富化前丢弃不相关条目。

**文件：**

- 修改：`backend/app/pipeline.py`
- 测试：`backend/tests/integration/test_pipeline_relevance.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/integration/test_pipeline_relevance.py`

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
    assert n == 1  # 只保留 scheduler 那条
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/integration/test_pipeline_relevance.py -v`
预期：失败（Pipeline 无 `relevance_fn` 参数）

- [ ] **步骤 3：修改 `backend/app/pipeline.py`**

更新构造函数与逐条循环，把类体替换为：

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

- [ ] **步骤 4：运行两个流水线测试确认通过**

运行：`cd backend && pytest tests/integration/test_pipeline.py tests/integration/test_pipeline_relevance.py -v`
预期：通过（原去重测试仍过；相关性测试通过）

- [ ] **步骤 5：提交**

```bash
git add backend/app/pipeline.py backend/tests/integration/test_pipeline_relevance.py
git commit -m "feat: add relevance pre-filter for high-volume news sources"
```

---

### Task 29 (S5)：结构化流水线 + 调度器按 stream 路由

**文件：**

- 创建：`backend/app/structured/pipeline.py`
- 修改：`backend/app/scheduler.py`
- 测试：`backend/tests/integration/test_structured_pipeline.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/integration/test_structured_pipeline.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/integration/test_structured_pipeline.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/structured/pipeline.py`**

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

- [ ] **步骤 4：修改 `backend/app/scheduler.py`** —— 按 stream 路由并支持 `api` 类型。把 `run_source_job` 与 `build_fetcher` 替换为：

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

并在 `scheduler.py` 顶部加导入：`from app.enums import SourceType, Stream`。

- [ ] **步骤 5：运行测试**

运行：`cd backend && pytest tests/integration/test_structured_pipeline.py tests/unit/test_scheduler.py -v`
预期：通过

- [ ] **步骤 6：提交**

```bash
git add backend/app/structured/pipeline.py backend/app/scheduler.py backend/tests/integration/test_structured_pipeline.py
git commit -m "feat: add structured pipeline and route scheduler by stream"
```

---

### Task 30 (S6)：结构化数据 API 接口

**文件：**

- 创建：`backend/app/api/structured_routes.py`
- 修改：`backend/app/api/main.py`（挂载新 router）
- 测试：`backend/tests/integration/test_structured_api.py`

- [ ] **步骤 1：编写失败测试** —— `backend/tests/integration/test_structured_api.py`

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

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && pytest tests/integration/test_structured_api.py -v`
预期：失败，报 `ModuleNotFoundError`

- [ ] **步骤 3：创建 `backend/app/api/structured_routes.py`**

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

- [ ] **步骤 4：修改 `backend/app/api/main.py`** —— 挂载结构化 router。更新 `create_app`：

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

- [ ] **步骤 5：运行测试确认通过**

运行：`cd backend && pytest tests/integration/test_structured_api.py -v`
预期：通过

- [ ] **步骤 6：提交**

```bash
git add backend/app/api/structured_routes.py backend/app/api/main.py backend/tests/integration/test_structured_api.py
git commit -m "feat: add structured-data API endpoints"
```

---

### Task 31 (S7)：前端结构化数据表视图

> 归到前端阶段（Task 21 之后）。加一个 tab，在新闻列表与结构化数据表（安全公告/生命周期/兼容性）之间切换。

**文件：**

- 创建：`frontend/src/api/structured.ts`
- 创建：`frontend/src/components/StructuredTable.tsx`
- 修改：`frontend/src/pages/HomePage.tsx`（加简单 tab 切换）

- [ ] **步骤 1：创建 `frontend/src/api/structured.ts`**

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

- [ ] **步骤 2：创建 `frontend/src/components/StructuredTable.tsx`**

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

- [ ] **步骤 3：修改 `frontend/src/pages/HomePage.tsx`** —— 在内容上方加 tab 切换。在 `<h1>` 之后插入，按 `view` 状态渲染新闻视图或结构化表：

```tsx
// 与其他 useState 一起加：
const [view, setView] = useState<"news" | "advisories" | "lifecycles" | "compatibility">("news");

// 在 <h1>技术新闻追踪</h1> 之后加这段 tab 栏：
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

// 把原有的搜索框 + 分面/列表 flex 容器包成只在 view === "news" 时渲染，
// 否则渲染 <StructuredTable resource={view} />（顶部 import 它）。
```

- [ ] **步骤 4：验证构建**

运行：`cd frontend && npm run build`
预期：无类型错误，构建成功。

- [ ] **步骤 5：提交**

```bash
git add frontend/src/api/structured.ts frontend/src/components/StructuredTable.tsx frontend/src/pages/HomePage.tsx
git commit -m "feat: add structured-data table view with tab switch"
```

---

## 自查记录（由计划作者完成）

- **Spec 覆盖：** RSS/固定页/搜索三类采集器（Task 9-11）、Scrapling 默认且可插拔提取器（Task 7）、内部优先的可插拔搜索 provider（Task 8）、归一化 + URL 规范化（Task 5）、去重 + 跨源合并（Task 6、14）、严格按摘要 schema 字段的 LLM 富化（Task 13）、结构化实体分面查询（Task 14、16）、视觉区块区分 + 重要度配色（Task 20-21）、单源错误隔离 + 富化失败不阻塞（Task 15）、url_hash/content_hash 幂等（Task 6、14、15）、LLM 缓存（Task 12）、单机 Docker Compose + 永久保留（Task 23，无清理任务）、5 个初始主分类（`enums.py`、seed yaml）。全部覆盖。
- **遵守不做项：** 无邮件/iWiki/订阅/鉴权/语义检索/图数据库相关任务。
- **类型一致性：** `EnrichedFields`、`NormalizedItem`、`ExtractedDoc`、`RawItem`、`SearchResult` 跨任务复用一致；`save_enriched`/`merge_source_link`/`exists_by_canonical` 在仓储、流水线、API 任务中命名一致。
- **待办（在 spec §13 跟踪）：** 确认司内 LLM 网关端点及是否带联网搜索（确认后把 `SEARCH_PROVIDER=internal` 打开）；仅在采用 Firecrawl 时做 AGPL 法务确认；敲定真实的初始采集源清单。

### 阶段 13 新增（结构化流，设计 v2）

- **覆盖：** `api` SourceType + `Stream` 枚举（Task 1）；`sources` 新字段 + 4 张结构化表（Task 2）；seed yaml + registry 传新字段（Task 17）；`ApiFetcher` + `SourceAdapter` 注册表 + 结构化契约（Task 25）；Ubuntu 安全适配器示例（Task 26）；幂等 `StructuredRepository`（Task 27）；高量新闻源相关性预过滤（Task 28）；`StructuredPipeline` + 调度按 `stream` 路由（Task 29）；结构化 API 接口（Task 30）；前端结构化表 + tab（Task 31）。对应设计文档 §5.2/§5.9/§6/§7（`论文/研究`）/§14。
- **推迟（设计文档）：** repo 包元数据、邮件列表归档、兼容性 *diff* 追踪（V1 仅快照）；其余适配器（`redhat_securitydata`、`redhat_lifecycle`、镜像、兼容性）待各源响应 schema 确认后各自成任务（§13）。
- **类型一致性：** `StructuredBatch`、`AdvisoryRecord`、`LifecycleRecord`、`ImageRecord`、`CompatibilityRecord`、`SourceAdapter` 跨 fetcher/适配器/仓储/流水线复用；`fetch_structured`/`upsert_batch`/`parse` 命名一致。`Pipeline` 构造函数新增 `relevance_fn`（默认 `llm_relevance`）。

