# V2 数据库迁移与用户系统 — 实施计划

> **面向 agent 工作器：** 必需的子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐步实施本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 通过单个 Alembic 迁移添加所有 V2 数据库表，然后实现基于 JWT 的认证（注册/登录/我），并自动初始化 user_profiles。

**架构：** 新的 ORM 模型添加到 `app/models.py`。新建 `app/auth.py` 用于 JWT + 密码哈希。新建 `app/api/auth_routes.py` 用于认证端点。更新 `app/api/deps.py` 添加 `get_current_user` 依赖。所有现有表和代码保持不变。

**技术栈：** Python 3.11、FastAPI、SQLAlchemy 2.0、Alembic、`python-jose[cryptography]`、`passlib[bcrypt]`

**规范：** `docs/superpowers/specs/2026-06-15-v2-complete-design.md` §3、§8

---

## 文件变更地图

| 文件 | 操作 | 备注 |
|------|--------|-------|
| `backend/pyproject.toml` | 修改 | 添加 `python-jose[cryptography]`、`passlib[bcrypt]` |
| `backend/app/models.py` | 修改 | 添加 8 个新 ORM 模型 |
| `backend/app/enums.py` | 修改 | 向 SourceType 添加 `agent_crawl`，向 ItemStatus 添加 `agent_enriched` |
| `backend/app/auth.py` | 创建 | JWT 工具函数 + 密码哈希 |
| `backend/app/api/auth_routes.py` | 创建 | /auth/register、/auth/login、/auth/me、/auth/me (PUT) |
| `backend/app/api/deps.py` | 修改 | 添加 `get_current_user` 依赖 |
| `backend/app/api/main.py` | 修改 | 注册 auth_routes 路由 |
| `backend/alembic/versions/v2_add_all_tables.py` | 创建 | 所有 V2 表的单次迁移 |
| `backend/tests/unit/test_auth.py` | 创建 | JWT + 密码单元测试 |
| `backend/tests/integration/test_auth_api.py` | 创建 | 注册/登录/我的信息集成测试 |

---

### 任务 1：添加依赖

**文件：**
- 修改：`backend/pyproject.toml`

- [ ] **步骤 1：添加新的后端依赖**

替换 `backend/pyproject.toml` 中的 `dependencies` 列表：

```toml
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
    "scrapling[fetchers]>=0.4",
    "python-dateutil>=2.9",
    "pyyaml>=6.0",
    "python-jose[cryptography]>=3.3",
    "passlib[bcrypt]>=1.7",
]
```

- [ ] **步骤 2：安装新依赖**

```bash
cd backend && pip install "python-jose[cryptography]>=3.3" "passlib[bcrypt]>=1.7"
```

预期：包安装无错误。

---

### 任务 2：更新枚举

**文件：**
- 修改：`backend/app/enums.py`

- [ ] **步骤 1：阅读当前枚举**

```bash
cd backend && grep -n "SourceType\|ItemStatus" app/enums.py
```

- [ ] **步骤 2：添加新枚举值**

在 `backend/app/enums.py` 中，向 `SourceType` 添加 `agent_crawl`，向 `ItemStatus` 添加 `agent_enriched`。具体修改取决于当前内容——在现有值旁边添加：

```python
class SourceType(str, Enum):
    RSS = "rss"
    API = "api"
    PAGE_MONITOR = "page_monitor"
    SEARCH = "search"
    AGENT_CRAWL = "agent_crawl"   # ← 添加此项

class ItemStatus(str, Enum):
    NEW = "new"
    ENRICHED = "enriched"
    ENRICH_FAILED = "enrich_failed"
    NEEDS_REVIEW = "needs_review"
    AGENT_ENRICHED = "agent_enriched"   # ← 添加此项
```

- [ ] **步骤 3：验证导入**

```bash
cd backend && ENABLE_SCHEDULER=0 python -c "from app.enums import SourceType, ItemStatus; print(SourceType.AGENT_CRAWL, ItemStatus.AGENT_ENRICHED)"
```

预期：`agent_crawl agent_enriched`

---

### 任务 3：添加 V2 ORM 模型

**文件：**
- 修改：`backend/app/models.py`

- [ ] **步骤 1：在 `backend/app/models.py` 末尾添加所有 V2 模型**

```python
import uuid as _uuid


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(_uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="user")   # user | admin
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    profile: Mapped["UserProfile | None"] = relationship(back_populates="user", uselist=False)


class UserProfile(Base):
    __tablename__ = "user_profiles"
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    criteria: Mapped[list] = mapped_column(JSON, default=list)
    free_text_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enable_llm_scoring: Mapped[bool] = mapped_column(default=False)
    min_score_threshold: Mapped[int] = mapped_column(Integer, default=25)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    user: Mapped["User"] = relationship(back_populates="profile")


class UserItemScore(Base):
    __tablename__ = "user_item_scores"
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), primary_key=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    scoring_method: Mapped[str] = mapped_column(String(20), default="fast")   # fast | llm
    score_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserItemInteraction(Base):
    __tablename__ = "user_item_interactions"
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), primary_key=True)
    action: Mapped[str] = mapped_column(String(20), primary_key=True)   # view | bookmark
    interacted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Digest(Base):
    __tablename__ = "digests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(_uuid.uuid4()))
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)   # manual | scheduled
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    time_range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    time_range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), default="personalized")   # personalized | global
    period_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    hotspots: Mapped[list] = mapped_column(JSON, default=list)
    emerging_topics: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="generating")   # generating | ready | failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentSourceConfig(Base):
    __tablename__ = "agent_source_configs"
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    focus_areas: Mapped[list] = mapped_column(JSON, default=list)
    topic_groups: Mapped[list] = mapped_column(JSON, default=list)
    crawl_depth: Mapped[int] = mapped_column(Integer, default=1)
    max_urls_per_run: Mapped[int] = mapped_column(Integer, default=20)
    quality_threshold: Mapped[int] = mapped_column(Integer, default=4)
    crawl_workers: Mapped[int] = mapped_column(Integer, default=5)
    quality_workers: Mapped[int] = mapped_column(Integer, default=3)
    summary_workers: Mapped[int] = mapped_column(Integer, default=3)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentSiteMemory(Base):
    __tablename__ = "agent_site_memory"
    __table_args__ = (UniqueConstraint("source_id", "url_pattern", name="uq_agent_memory_source_url"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    url_pattern: Mapped[str] = mapped_column(String(2000), nullable=False)
    quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)   # keep | discard
    relevant_topic: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    seen_count: Mapped[int] = mapped_column(Integer, default=1)


class AgentCrawlRun(Base):
    __tablename__ = "agent_crawl_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    plan_urls_count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    quality_passed: Mapped[int] = mapped_column(Integer, default=0)
    items_created: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="running")   # running | completed | failed | plan_failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **步骤 2：验证模型导入**

```bash
cd backend && ENABLE_SCHEDULER=0 python -c "from app.models import User, UserProfile, Digest, AgentSourceConfig, AgentSiteMemory; print('OK')"
```

预期：`OK`

---

### 任务 4：Alembic 迁移

**文件：**
- 创建：`backend/alembic/versions/v2_add_all_v2_tables.py`

- [ ] **步骤 1：自动生成迁移**

```bash
cd backend && ENABLE_SCHEDULER=0 DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/osnews alembic revision --autogenerate -m "add_v2_tables"
```

预期：在 `alembic/versions/` 下创建一个新文件。

- [ ] **步骤 2：检查并运行迁移**

```bash
cd backend && ENABLE_SCHEDULER=0 DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/osnews alembic upgrade head
```

预期：迁移无错误运行。验证新表：

```bash
psql $DATABASE_URL -c "\dt" | grep -E "users|user_profiles|digests|agent_"
```

预期：列出 `users`、`user_profiles`、`user_item_scores`、`user_item_interactions`、`digests`、`agent_source_configs`、`agent_site_memory`、`agent_crawl_runs`。

- [ ] **步骤 3：提交**

```bash
cd backend && git add app/models.py app/enums.py alembic/versions/ && git commit -m "feat: add V2 ORM models and database migration"
```

---

### 任务 5：认证工具函数(`app/auth.py`)

**文件：**
- 创建：`backend/app/auth.py`
- 创建：`backend/tests/unit/test_auth.py`

- [ ] **步骤 1：先写会失败的测试**

创建 `backend/tests/unit/test_auth.py`：

```python
import pytest
from app.auth import create_access_token, verify_access_token, hash_password, verify_password


def test_hash_and_verify_password():
    h = hash_password("secret123")
    assert verify_password("secret123", h)
    assert not verify_password("wrong", h)


def test_hash_is_not_plaintext():
    h = hash_password("secret123")
    assert h != "secret123"
    assert h.startswith("$2b$")


def test_create_and_verify_token():
    token = create_access_token(user_id="abc-123", role="user")
    payload = verify_access_token(token)
    assert payload["sub"] == "abc-123"
    assert payload["role"] == "user"


def test_verify_invalid_token_returns_none():
    assert verify_access_token("not.a.token") is None


def test_verify_tampered_token_returns_none():
    token = create_access_token(user_id="abc", role="user")
    tampered = token[:-4] + "xxxx"
    assert verify_access_token(tampered) is None
```

- [ ] **步骤 2：运行测试确认失败**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_auth.py -v
```

预期：ImportError（模块尚未创建）。

- [ ] **步骤 3：创建 `backend/app/auth.py`**

```python
import os
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-me-in-production-use-32+-chars")
_ALGORITHM = "HS256"
_ACCESS_TOKEN_EXPIRE_DAYS = 7

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


def create_access_token(user_id: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=_ACCESS_TOKEN_EXPIRE_DAYS)
    payload = {"sub": user_id, "role": role, "exp": expire}
    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


def verify_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
    except JWTError:
        return None
```

- [ ] **步骤 4：运行测试确认通过**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_auth.py -v
```

预期：5 个测试全部通过（PASS）。

- [ ] **步骤 5：提交**

```bash
cd backend && git add app/auth.py tests/unit/test_auth.py && git commit -m "feat: add JWT auth helpers (hash_password, create_access_token)"
```

---

### 任务 6：更新 `deps.py` 添加 `get_current_user`

**文件：**
- 修改：`backend/app/api/deps.py`

- [ ] **步骤 1：阅读当前 deps.py**

```bash
cd backend && cat app/api/deps.py
```

- [ ] **步骤 2：添加 `get_current_user` 依赖**

替换整个 `backend/app/api/deps.py` 为：

```python
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.auth import verify_access_token
from app.db import SessionLocal
from app.models import User


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.removeprefix("Bearer ").strip()
    payload = verify_access_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user
```

- [ ] **步骤 3：验证导入**

```bash
cd backend && ENABLE_SCHEDULER=0 python -c "from app.api.deps import get_current_user, require_admin; print('OK')"
```

预期：`OK`

---

### 任务 7：认证 API 路由

**文件：**
- 创建：`backend/app/api/auth_routes.py`
- 创建：`backend/tests/integration/test_auth_api.py`

- [ ] **步骤 1：写会失败的集成测试**

创建 `backend/tests/integration/test_auth_api.py`：

```python
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.main import create_app
from app.api.deps import get_db
from app.models import Base

TEST_DB = "sqlite://"

@pytest.fixture
def client():
    engine = create_engine(TEST_DB, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    SessionTest = sessionmaker(bind=engine)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: SessionTest()
    yield TestClient(app)
    Base.metadata.drop_all(engine)


def test_register_creates_user_and_profile(client):
    r = client.post("/auth/register", json={"email": "a@b.com", "password": "pass1234"})
    assert r.status_code == 201
    data = r.json()
    assert data["email"] == "a@b.com"
    assert data["role"] == "user"


def test_register_duplicate_email_fails(client):
    client.post("/auth/register", json={"email": "dup@b.com", "password": "pass1234"})
    r = client.post("/auth/register", json={"email": "dup@b.com", "password": "pass1234"})
    assert r.status_code == 409


def test_login_returns_token(client):
    client.post("/auth/register", json={"email": "u@b.com", "password": "mypassword"})
    r = client.post("/auth/login", json={"email": "u@b.com", "password": "mypassword"})
    assert r.status_code == 200
    assert "access_token" in r.json()


def test_login_wrong_password_fails(client):
    client.post("/auth/register", json={"email": "u2@b.com", "password": "correct"})
    r = client.post("/auth/login", json={"email": "u2@b.com", "password": "wrong"})
    assert r.status_code == 401


def test_me_returns_current_user(client):
    client.post("/auth/register", json={"email": "me@b.com", "password": "pass1234"})
    login = client.post("/auth/login", json={"email": "me@b.com", "password": "pass1234"})
    token = login.json()["access_token"]
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["email"] == "me@b.com"


def test_me_without_token_returns_401(client):
    r = client.get("/auth/me")
    assert r.status_code == 401
```

- [ ] **步骤 2：运行测试确认失败**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_auth_api.py -v
```

预期：FAIL（路由尚未创建）。

- [ ] **步骤 3：创建 `backend/app/api/auth_routes.py`**

```python
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.auth import create_access_token, hash_password, verify_password
from app.models import User, UserProfile

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


def _user_response(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.post("/register", status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    if len(body.password) < 6:
        raise HTTPException(status_code=422, detail="Password must be at least 6 characters")
    user = User(
        email=body.email.lower().strip(),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered")
    # 自动创建空的用户配置
    profile = UserProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    db.refresh(user)
    return _user_response(user)


@router.post("/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    from sqlalchemy import select
    user = db.scalars(select(User).where(User.email == body.email.lower().strip())).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    token = create_access_token(user_id=user.id, role=user.role)
    return {"access_token": token, "token_type": "bearer", "user": _user_response(user)}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return _user_response(current_user)


@router.put("/me")
def update_me(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if "display_name" in body:
        current_user.display_name = body["display_name"]
    db.commit()
    db.refresh(current_user)
    return _user_response(current_user)
```

- [ ] **步骤 4：在 `app/api/main.py` 中注册路由**

在 `backend/app/api/main.py` 中，导入并包含新路由：

```python
from app.api.auth_routes import router as auth_router
# 在现有 router include 之后添加：
app.include_router(auth_router)
```

- [ ] **步骤 5：运行测试确认通过**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_auth_api.py -v
```

预期：6 个测试全部通过（PASS）。

- [ ] **步骤 6：运行完整测试套件确认无回归**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/ -v 2>&1 | tail -20
```

预期：所有现有测试仍然通过（PASS）。

- [ ] **步骤 7：提交**

```bash
cd backend && git add app/api/auth_routes.py app/api/deps.py app/api/main.py tests/integration/test_auth_api.py && git commit -m "feat: add JWT auth API (register, login, me)"
```
