# V2 Group Subscription System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 3-tier group subscription system: system_admin creates groups, group_admin manages sources/members/filters, subscribers join groups and see group-filtered + personally-scored content.

**Architecture:** 6 new DB tables (groups, group_members, group_sources, group_profiles, group_join_requests, group_digest_schedule). Role rename: user→subscriber, admin→system_admin. Feed query filtered to user's groups' sources then through each group's filter_criteria. Personal scoring applied on top. New permission helpers enforce group-level access. Frontend gets group management pages.

**Prerequisite:** Plan `2026-06-15-v2-db-and-user-system.md` must be complete (users table + JWT auth exists).

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, Alembic, React 19, TypeScript

**Spec:** `docs/superpowers/specs/2026-06-15-group-subscription-design.md`

---

## File Change Map

| File | Action | Notes |
|------|--------|-------|
| `backend/app/enums.py` | Modify | Add `UserRole`, `GroupRole` enums |
| `backend/app/models.py` | Modify | Add 6 new group-related ORM models |
| `backend/app/auth.py` | Modify | Use `UserRole` enum in token payload |
| `backend/app/api/deps.py` | Modify | Add `require_system_admin`, `require_group_admin` helpers |
| `backend/app/groups/permissions.py` | Create | `is_group_admin()`, `is_group_member()`, group access checks |
| `backend/app/api/admin_group_routes.py` | Create | `/admin/groups` CRUD |
| `backend/app/api/group_routes.py` | Create | `/groups`, `/groups/{id}/*` member/source/profile/join APIs |
| `backend/app/api/group_digest_routes.py` | Create | `/groups/{id}/digest/*` schedule + trigger |
| `backend/app/api/main.py` | Modify | Register new routers |
| `backend/app/api/routes.py` | Modify | `/items` + `/facets` filtered by user groups |
| `backend/app/groups/feed_filter.py` | Create | Group-level filter_criteria evaluation |
| `backend/alembic/versions/` | Create | Migration for 6 tables + role rename |
| `backend/tests/unit/test_feed_filter.py` | Create | Pure function tests for group filter |
| `backend/tests/integration/test_group_api.py` | Create | Group management integration tests |
| `frontend/src/pages/MyGroupsPage.tsx` | Create | User's groups list + public groups browse |
| `frontend/src/pages/GroupDetailPage.tsx` | Create | Group detail (sources, filter, members) |
| `frontend/src/pages/AdminGroupsPage.tsx` | Create | sysadmin group management |
| `frontend/src/api/client.ts` | Modify | Add group API calls |
| `frontend/src/App.tsx` | Modify | Add new routes |

---

### Task 1: Add Group Enums + ORM Models

**Files:**
- Modify: `backend/app/enums.py`
- Modify: `backend/app/models.py`

- [ ] **Step 1: Add `UserRole` and `GroupRole` enums to `backend/app/enums.py`**

Add these two new enums at the top of `backend/app/enums.py`, after the existing imports:

```python
class UserRole(StrEnum):
    SUBSCRIBER   = "subscriber"
    SYSTEM_ADMIN = "system_admin"


class GroupRole(StrEnum):
    MEMBER      = "member"
    GROUP_ADMIN = "group_admin"
```

- [ ] **Step 2: Add 6 new ORM models at the end of `backend/app/models.py`**

First add the `import uuid as _uuid` line if not already present (it should be from the V2 DB plan). Then append:

```python
class Group(Base):
    __tablename__ = "groups"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(_uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupMember(Base):
    __tablename__ = "group_members"
    group_id: Mapped[str] = mapped_column(String(36), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="member")   # member | group_admin
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupSource(Base):
    __tablename__ = "group_sources"
    group_id: Mapped[str] = mapped_column(String(36), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    added_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupProfile(Base):
    __tablename__ = "group_profiles"
    group_id: Mapped[str] = mapped_column(String(36), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    filter_criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GroupJoinRequest(Base):
    __tablename__ = "group_join_requests"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_join_request_group_user"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(_uuid.uuid4()))
    group_id: Mapped[str] = mapped_column(String(36), ForeignKey("groups.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="pending")   # pending | approved | rejected
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GroupDigestSchedule(Base):
    __tablename__ = "group_digest_schedules"
    group_id: Mapped[str] = mapped_column(String(36), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    frequency: Mapped[str] = mapped_column(String(20), default="weekly")   # daily | weekly
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 0=Mon, 6=Sun
    hour_utc: Mapped[int] = mapped_column(Integer, default=8)
    updated_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 3: Verify imports**

```bash
cd backend && ENABLE_SCHEDULER=0 python -c "from app.models import Group, GroupMember, GroupSource, GroupProfile, GroupJoinRequest, GroupDigestSchedule; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Update `AgentSourceConfig` model to add `group_id`**

The `AgentSourceConfig` model was added in the V2 agent crawl plan. Find it in `backend/app/models.py` and add the `group_id` field:

```python
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
    group_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("groups.id", ondelete="SET NULL"), nullable=True)   # ← add this
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

> Note: `groups` table must be created BEFORE this migration runs (they're in the same migration — Alembic handles ordering via `op.create_table` order).

- [ ] **Step 5: Update `POST /sources/agent` to require `group_id`**

In `backend/app/api/agent_routes.py`, update `AgentSourceCreate`:

```python
class AgentSourceCreate(BaseModel):
    name: str
    root_url: str
    focus_areas: list[str] = []
    topic_groups: list[str] = []
    crawl_depth: int = 1
    max_urls_per_run: int = 20
    quality_threshold: int = 4
    crawl_workers: int = 5
    quality_workers: int = 3
    summary_workers: int = 3
    group_id: str   # ← now required; agent sources must belong to a group
```

And in `create_agent_source()`, after creating the source, auto-add to `group_sources`:

```python
    # Auto-link the new source to the group
    from app.models import GroupSource
    from app.groups.permissions import check_group_access
    check_group_access(db, body.group_id, current_user, require_admin=True)
    gs = GroupSource(group_id=body.group_id, source_id=source.id, added_by=current_user.id)
    db.add(gs)
    # Also set group_id on agent config
    config.group_id = body.group_id
```

- [ ] **Step 6: Commit**

```bash
cd backend && git add app/enums.py app/models.py app/api/agent_routes.py && git commit -m "feat: add UserRole/GroupRole enums, 6 group ORM models, group_id on agent sources"
```

---

### Task 2: Alembic Migration

**Files:**
- Create: `backend/alembic/versions/` (new migration file)

- [ ] **Step 1: Auto-generate migration**

```bash
cd backend && ENABLE_SCHEDULER=0 DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/osnews alembic revision --autogenerate -m "add_group_subscription_tables"
```

Expected: creates a new migration file.

- [ ] **Step 2: Edit the migration to include role rename**

Open the generated migration file and add to the `upgrade()` function before the `op.create_table` calls:

```python
# Rename existing role values
op.execute("UPDATE users SET role = 'subscriber' WHERE role = 'user'")
op.execute("UPDATE users SET role = 'system_admin' WHERE role = 'admin'")
```

And add to `downgrade()`:
```python
op.execute("UPDATE users SET role = 'user' WHERE role = 'subscriber'")
op.execute("UPDATE users SET role = 'admin' WHERE role = 'system_admin'")
```

- [ ] **Step 3: Run migration**

```bash
cd backend && ENABLE_SCHEDULER=0 DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/osnews alembic upgrade head
```

Expected: migration runs without error.

- [ ] **Step 4: Verify tables exist**

```bash
psql $DATABASE_URL -c "\dt" | grep -E "group"
```

Expected: `groups`, `group_members`, `group_sources`, `group_profiles`, `group_join_requests`, `group_digest_schedules`.

- [ ] **Step 5: Commit**

```bash
cd backend && git add alembic/versions/ && git commit -m "feat: migrate group tables and rename user roles (user→subscriber, admin→system_admin)"
```

---

### Task 3: Permission Helpers

**Files:**
- Create: `backend/app/groups/__init__.py`
- Create: `backend/app/groups/permissions.py`
- Modify: `backend/app/api/deps.py`
- Create: `backend/tests/unit/test_group_permissions.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/unit/test_group_permissions.py`:

```python
from unittest.mock import MagicMock
import pytest
from app.groups.permissions import is_group_member, is_group_admin, check_group_access
from app.models import GroupMember, User


def _user(role="subscriber", uid="u1"):
    u = MagicMock()
    u.id = uid
    u.role = role
    return u


def _make_db(member_role: str | None = None):
    db = MagicMock()
    if member_role is None:
        db.scalars.return_value.first.return_value = None
    else:
        m = MagicMock()
        m.role = member_role
        db.scalars.return_value.first.return_value = m
    return db


def test_is_group_member_true_when_member_exists():
    db = _make_db("member")
    assert is_group_member(db, group_id="g1", user_id="u1") is True


def test_is_group_member_false_when_not_member():
    db = _make_db(None)
    assert is_group_member(db, group_id="g1", user_id="u1") is False


def test_is_group_admin_true_for_group_admin():
    db = _make_db("group_admin")
    assert is_group_admin(db, group_id="g1", user_id="u1") is True


def test_is_group_admin_false_for_plain_member():
    db = _make_db("member")
    assert is_group_admin(db, group_id="g1", user_id="u1") is False


def test_system_admin_passes_any_group_check():
    db = _make_db(None)  # not a group member at all
    user = _user(role="system_admin")
    # system_admin always passes group access
    from fastapi import HTTPException
    try:
        check_group_access(db, group_id="g1", current_user=user, require_admin=False)
    except HTTPException:
        pytest.fail("system_admin should never get 403")


def test_non_member_raises_403():
    db = _make_db(None)
    user = _user(role="subscriber")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        check_group_access(db, group_id="g1", current_user=user, require_admin=False)
    assert exc.value.status_code == 403


def test_member_cannot_pass_require_admin():
    db = _make_db("member")
    user = _user(role="subscriber")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        check_group_access(db, group_id="g1", current_user=user, require_admin=True)
    assert exc.value.status_code == 403


def test_group_admin_passes_require_admin():
    db = _make_db("group_admin")
    user = _user(role="subscriber")
    from fastapi import HTTPException
    try:
        check_group_access(db, group_id="g1", current_user=user, require_admin=True)
    except HTTPException:
        pytest.fail("group_admin should pass require_admin=True")
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_group_permissions.py -v
```

Expected: ImportError.

- [ ] **Step 3: Create package and `backend/app/groups/permissions.py`**

```bash
mkdir -p backend/app/groups && touch backend/app/groups/__init__.py
```

Create `backend/app/groups/permissions.py`:

```python
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GroupMember, User


def is_group_member(db: Session, group_id: str, user_id: str) -> bool:
    m = db.scalars(
        select(GroupMember).where(
            GroupMember.group_id == group_id, GroupMember.user_id == user_id
        )
    ).first()
    return m is not None


def is_group_admin(db: Session, group_id: str, user_id: str) -> bool:
    m = db.scalars(
        select(GroupMember).where(
            GroupMember.group_id == group_id, GroupMember.user_id == user_id
        )
    ).first()
    return m is not None and m.role == "group_admin"


def check_group_access(
    db: Session, group_id: str, current_user: User, require_admin: bool = False
) -> GroupMember | None:
    """Raise HTTP 403 if current_user cannot access the group.

    system_admin bypasses all group checks.
    If require_admin=True, the user must be group_admin (or system_admin).
    Returns the GroupMember record, or None for system_admin (not a member).
    """
    if current_user.role == "system_admin":
        return None   # system_admin always passes

    m = db.scalars(
        select(GroupMember).where(
            GroupMember.group_id == group_id, GroupMember.user_id == current_user.id
        )
    ).first()

    if m is None:
        raise HTTPException(status_code=403, detail="Not a member of this group")

    if require_admin and m.role != "group_admin":
        raise HTTPException(status_code=403, detail="Group admin access required")

    return m
```

- [ ] **Step 4: Update `backend/app/api/deps.py` — add `require_system_admin`**

Add to `backend/app/api/deps.py`:

```python
def require_system_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "system_admin":
        raise HTTPException(status_code=403, detail="System admin access required")
    return current_user
```

- [ ] **Step 5: Run tests**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_group_permissions.py -v
```

Expected: 7 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd backend && git add app/groups/ app/api/deps.py tests/unit/test_group_permissions.py && git commit -m "feat: add group permission helpers (is_group_member, check_group_access)"
```

---

### Task 4: Group Filter Function

**Files:**
- Create: `backend/app/groups/feed_filter.py`
- Create: `backend/tests/unit/test_feed_filter.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/unit/test_feed_filter.py`:

```python
from types import SimpleNamespace
import pytest
from app.groups.feed_filter import passes_group_filter, FilterCriteria


def _item(title="Linux 6.12 released", main_category="OS性能发展", importance="高", summary="kernel update"):
    return SimpleNamespace(title=title, main_category=main_category, importance=importance, summary=summary or "")


def _criteria(**kwargs) -> FilterCriteria:
    defaults = dict(keyword_whitelist=[], keyword_blacklist=[], category_whitelist=[], min_importance="低")
    defaults.update(kwargs)
    return FilterCriteria(**defaults)


def test_empty_criteria_passes_all():
    assert passes_group_filter(_item(), _criteria()) is True


def test_keyword_whitelist_blocks_unmatched():
    c = _criteria(keyword_whitelist=["kernel"])
    assert passes_group_filter(_item(title="Python tutorial"), c) is False


def test_keyword_whitelist_passes_matched():
    c = _criteria(keyword_whitelist=["kernel"])
    assert passes_group_filter(_item(title="kernel scheduler update"), c) is True


def test_keyword_blacklist_blocks_matched():
    c = _criteria(keyword_blacklist=["招聘", "入门"])
    assert passes_group_filter(_item(title="Python 入门教程"), c) is False


def test_keyword_blacklist_passes_unmatched():
    c = _criteria(keyword_blacklist=["招聘"])
    assert passes_group_filter(_item(title="kernel update"), c) is True


def test_category_whitelist_blocks_wrong_category():
    c = _criteria(category_whitelist=["OS性能发展"])
    assert passes_group_filter(_item(main_category="友商产品信息"), c) is False


def test_category_whitelist_passes_matching_category():
    c = _criteria(category_whitelist=["OS性能发展"])
    assert passes_group_filter(_item(main_category="OS性能发展"), c) is True


def test_min_importance_blocks_low_when_medium_required():
    c = _criteria(min_importance="中")
    assert passes_group_filter(_item(importance="低"), c) is False


def test_min_importance_passes_equal():
    c = _criteria(min_importance="中")
    assert passes_group_filter(_item(importance="中"), c) is True


def test_min_importance_passes_high_when_medium_required():
    c = _criteria(min_importance="中")
    assert passes_group_filter(_item(importance="高"), c) is True


def test_whitelist_checks_summary_too():
    c = _criteria(keyword_whitelist=["CVE"])
    # title has no CVE but summary does
    assert passes_group_filter(_item(title="Security update", summary="critical CVE patch"), c) is True
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_feed_filter.py -v
```

Expected: ImportError.

- [ ] **Step 3: Create `backend/app/groups/feed_filter.py`**

```python
from dataclasses import dataclass, field

_IMPORTANCE_RANK = {"高": 3, "中": 2, "低": 1}


@dataclass
class FilterCriteria:
    keyword_whitelist: list[str] = field(default_factory=list)
    keyword_blacklist: list[str] = field(default_factory=list)
    category_whitelist: list[str] = field(default_factory=list)
    min_importance: str = "低"   # 高 | 中 | 低


def parse_filter_criteria(raw: dict) -> FilterCriteria:
    return FilterCriteria(
        keyword_whitelist=raw.get("keyword_whitelist") or [],
        keyword_blacklist=raw.get("keyword_blacklist") or [],
        category_whitelist=raw.get("category_whitelist") or [],
        min_importance=raw.get("min_importance") or "低",
    )


def passes_group_filter(item, criteria: FilterCriteria) -> bool:
    """Return True if item passes all group-level filter rules.

    Evaluation order (first failure → discard):
    1. min_importance
    2. category_whitelist (if non-empty)
    3. keyword_blacklist
    4. keyword_whitelist (if non-empty)
    """
    # 1. importance threshold
    item_rank = _IMPORTANCE_RANK.get(getattr(item, "importance", "低"), 1)
    min_rank = _IMPORTANCE_RANK.get(criteria.min_importance, 1)
    if item_rank < min_rank:
        return False

    # 2. category whitelist
    if criteria.category_whitelist:
        if getattr(item, "main_category", "") not in criteria.category_whitelist:
            return False

    # build searchable text once
    text = f"{getattr(item, 'title', '')} {getattr(item, 'summary', '')}".lower()

    # 3. keyword blacklist
    for kw in criteria.keyword_blacklist:
        if kw.lower() in text:
            return False

    # 4. keyword whitelist
    if criteria.keyword_whitelist:
        if not any(kw.lower() in text for kw in criteria.keyword_whitelist):
            return False

    return True


def apply_group_filter_to_items(items: list, group_filter_map: dict[int, list[FilterCriteria]]) -> list:
    """Filter items using OR logic across multiple group criteria.

    group_filter_map: {source_id: [FilterCriteria, ...]} — each item's source maps to its group(s)' filters.
    An item passes if it passes ANY of the criteria for its source's groups.
    Items whose source has no group filter pass unconditionally.
    """
    result = []
    for item in items:
        source_id = getattr(item, "source_id", None)
        criteria_list = group_filter_map.get(source_id, [])
        if not criteria_list:
            result.append(item)
            continue
        if any(passes_group_filter(item, c) for c in criteria_list):
            result.append(item)
    return result
```

- [ ] **Step 4: Run tests**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_feed_filter.py -v
```

Expected: 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/groups/feed_filter.py tests/unit/test_feed_filter.py && git commit -m "feat: add group filter_criteria evaluation (whitelist/blacklist/importance/category)"
```

---

### Task 5: Admin Group Management API

**Files:**
- Create: `backend/app/api/admin_group_routes.py`
- Create: `backend/tests/integration/test_group_api.py`
- Modify: `backend/app/api/main.py`

- [ ] **Step 1: Write failing integration tests**

Create `backend/tests/integration/test_group_api.py`:

```python
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.main import create_app
from app.api.deps import get_db, get_current_user, require_system_admin
from app.models import Base, User


def _setup(role="system_admin"):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(id="admin-1", email="admin@test.com", password_hash="x", role=role)
    db.add(user)
    db.commit()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_system_admin] = lambda: user
    return TestClient(app), db, user


def test_create_group(monkeypatch):
    client, db, admin = _setup()
    r = client.post("/admin/groups", json={"name": "OS Team", "description": "OS maintainers", "admin_user_id": "admin-1"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "OS Team"
    assert "id" in data


def test_list_groups(monkeypatch):
    client, db, _ = _setup()
    client.post("/admin/groups", json={"name": "Group A", "description": "", "admin_user_id": "admin-1"})
    r = client.get("/admin/groups")
    assert r.status_code == 200
    assert len(r.json()) >= 1


def test_delete_group():
    client, db, _ = _setup()
    r = client.post("/admin/groups", json={"name": "Temp", "description": "", "admin_user_id": "admin-1"})
    gid = r.json()["id"]
    r2 = client.delete(f"/admin/groups/{gid}")
    assert r2.status_code == 204


def test_create_group_creates_group_admin_member():
    client, db, _ = _setup()
    r = client.post("/admin/groups", json={"name": "G2", "description": "", "admin_user_id": "admin-1"})
    gid = r.json()["id"]
    from app.models import GroupMember
    from sqlalchemy import select
    m = db.scalars(select(GroupMember).where(GroupMember.group_id == gid, GroupMember.user_id == "admin-1")).first()
    assert m is not None
    assert m.role == "group_admin"
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_group_api.py -v
```

Expected: ImportError.

- [ ] **Step 3: Create `backend/app/api/admin_group_routes.py`**

```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_system_admin
from app.models import Group, GroupDigestSchedule, GroupMember, GroupProfile, User

router = APIRouter(prefix="/admin/groups", tags=["admin-groups"])


class GroupCreate(BaseModel):
    name: str
    description: str = ""
    admin_user_id: str   # user to designate as first group_admin


class GroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None


def _group_response(g: Group) -> dict:
    return {
        "id": g.id,
        "name": g.name,
        "description": g.description,
        "is_active": g.is_active,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "created_by": g.created_by,
    }


@router.post("", status_code=201)
def create_group(
    body: GroupCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_system_admin),
):
    # Verify admin_user_id exists
    admin_user = db.get(User, body.admin_user_id)
    if admin_user is None:
        raise HTTPException(status_code=404, detail="admin_user_id not found")

    import uuid
    group = Group(id=str(uuid.uuid4()), name=body.name, description=body.description, created_by=admin.id)
    db.add(group)
    db.flush()

    # Designate first group_admin
    member = GroupMember(group_id=group.id, user_id=body.admin_user_id, role="group_admin")
    db.add(member)

    # Create empty group_profile and digest schedule
    db.add(GroupProfile(group_id=group.id, filter_criteria={}, updated_by=admin.id))
    db.add(GroupDigestSchedule(group_id=group.id, enabled=False, updated_by=admin.id))

    db.commit()
    db.refresh(group)
    return _group_response(group)


@router.get("")
def list_groups(db: Session = Depends(get_db), admin: User = Depends(require_system_admin)):
    groups = db.scalars(select(Group).order_by(Group.created_at.desc())).all()
    return [_group_response(g) for g in groups]


@router.get("/{group_id}")
def get_group(group_id: str, db: Session = Depends(get_db), admin: User = Depends(require_system_admin)):
    g = db.get(Group, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Group not found")
    member_count = db.scalar(
        select(GroupMember).where(GroupMember.group_id == group_id).with_only_columns(
            __import__("sqlalchemy").func.count()
        )
    ) or 0
    resp = _group_response(g)
    resp["member_count"] = member_count
    return resp


@router.put("/{group_id}")
def update_group(
    group_id: str,
    body: GroupUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_system_admin),
):
    g = db.get(Group, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Group not found")
    if body.name is not None:
        g.name = body.name
    if body.description is not None:
        g.description = body.description
    if body.is_active is not None:
        g.is_active = body.is_active
    db.commit()
    return _group_response(g)


@router.delete("/{group_id}", status_code=204)
def delete_group(group_id: str, db: Session = Depends(get_db), admin: User = Depends(require_system_admin)):
    g = db.get(Group, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Group not found")
    db.delete(g)
    db.commit()


@router.put("/{group_id}/admins/{user_id}")
def set_group_admin(
    group_id: str, user_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_system_admin),
):
    m = db.scalars(
        select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id)
    ).first()
    if m is None:
        raise HTTPException(status_code=404, detail="User is not a member of this group")
    m.role = "group_admin"
    db.commit()
    return {"user_id": user_id, "role": "group_admin"}


@router.delete("/{group_id}/admins/{user_id}", status_code=204)
def revoke_group_admin(
    group_id: str, user_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_system_admin),
):
    m = db.scalars(
        select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id)
    ).first()
    if m is None:
        raise HTTPException(status_code=404, detail="User is not a member of this group")
    m.role = "member"
    db.commit()
```

- [ ] **Step 4: Register router in `backend/app/api/main.py`**

```python
from app.api.admin_group_routes import router as admin_group_router
app.include_router(admin_group_router)
```

- [ ] **Step 5: Run tests**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_group_api.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd backend && git add app/api/admin_group_routes.py app/api/main.py tests/integration/test_group_api.py && git commit -m "feat: add /admin/groups CRUD API for system admin"
```

---

### Task 6: Group Member, Sources, Profile & Join API

**Files:**
- Create: `backend/app/api/group_routes.py`
- Modify: `backend/app/api/main.py`

- [ ] **Step 1: Create `backend/app/api/group_routes.py`**

```python
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.groups.permissions import check_group_access, is_group_member
from app.models import (
    Group, GroupJoinRequest, GroupMember, GroupProfile,
    GroupSource, Source, User,
)

router = APIRouter(prefix="/groups", tags=["groups"])


# ── Public group listing ──────────────────────────────────────────────

@router.get("")
def list_public_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All active groups (any logged-in user can see, to apply to join)."""
    groups = db.scalars(select(Group).where(Group.is_active == True).order_by(Group.name)).all()
    return [
        {
            "id": g.id, "name": g.name, "description": g.description,
            "is_member": is_group_member(db, g.id, current_user.id),
        }
        for g in groups
    ]


@router.get("/my")
def my_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    memberships = db.scalars(
        select(GroupMember).where(GroupMember.user_id == current_user.id)
    ).all()
    result = []
    for m in memberships:
        g = db.get(Group, m.group_id)
        if g:
            result.append({"id": g.id, "name": g.name, "description": g.description, "role": m.role})
    return result


@router.get("/{group_id}")
def get_group_info(
    group_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=False)
    g = db.get(Group, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return {"id": g.id, "name": g.name, "description": g.description, "is_active": g.is_active}


# ── Members ───────────────────────────────────────────────────────────

@router.get("/{group_id}/members")
def list_members(
    group_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=False)
    members = db.scalars(select(GroupMember).where(GroupMember.group_id == group_id)).all()
    result = []
    for m in members:
        u = db.get(User, m.user_id)
        result.append({
            "user_id": m.user_id,
            "email": u.email if u else "",
            "display_name": u.display_name if u else "",
            "role": m.role,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
        })
    return result


class InviteRequest(BaseModel):
    user_id: str


@router.post("/{group_id}/members", status_code=201)
def invite_member(
    group_id: str,
    body: InviteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=True)
    existing = db.scalars(
        select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == body.user_id)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="User is already a member")
    target = db.get(User, body.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    m = GroupMember(group_id=group_id, user_id=body.user_id, role="member")
    db.add(m)
    db.commit()
    return {"user_id": body.user_id, "role": "member"}


@router.delete("/{group_id}/members/{user_id}", status_code=204)
def remove_member(
    group_id: str, user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=True)
    m = db.scalars(
        select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id)
    ).first()
    if m is None:
        raise HTTPException(status_code=404, detail="Member not found")
    db.delete(m)
    db.commit()


# ── Join Requests ─────────────────────────────────────────────────────

class JoinRequestCreate(BaseModel):
    message: str | None = None


@router.post("/{group_id}/join-requests", status_code=201)
def submit_join_request(
    group_id: str,
    body: JoinRequestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if is_group_member(db, group_id, current_user.id):
        raise HTTPException(status_code=409, detail="Already a member")
    existing = db.scalars(
        select(GroupJoinRequest).where(
            GroupJoinRequest.group_id == group_id,
            GroupJoinRequest.user_id == current_user.id,
            GroupJoinRequest.status == "pending",
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Pending request already exists")
    req = GroupJoinRequest(
        id=str(_uuid.uuid4()),
        group_id=group_id,
        user_id=current_user.id,
        message=body.message,
    )
    db.add(req)
    db.commit()
    return {"id": req.id, "status": "pending"}


@router.get("/{group_id}/join-requests")
def list_join_requests(
    group_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=True)
    reqs = db.scalars(
        select(GroupJoinRequest)
        .where(GroupJoinRequest.group_id == group_id, GroupJoinRequest.status == "pending")
        .order_by(GroupJoinRequest.created_at)
    ).all()
    return [
        {"id": r.id, "user_id": r.user_id, "message": r.message,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in reqs
    ]


class ReviewRequest(BaseModel):
    status: str   # approved | rejected


@router.put("/{group_id}/join-requests/{request_id}")
def review_join_request(
    group_id: str, request_id: str,
    body: ReviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=True)
    req = db.get(GroupJoinRequest, request_id)
    if req is None or req.group_id != group_id:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status != "pending":
        raise HTTPException(status_code=409, detail="Request already reviewed")
    if body.status not in ("approved", "rejected"):
        raise HTTPException(status_code=422, detail="status must be approved or rejected")

    req.status = body.status
    req.reviewed_by = current_user.id

    if body.status == "approved":
        existing = db.scalars(
            select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == req.user_id)
        ).first()
        if existing is None:
            db.add(GroupMember(group_id=group_id, user_id=req.user_id, role="member"))

    db.commit()
    return {"id": request_id, "status": body.status}


# ── Sources ───────────────────────────────────────────────────────────

@router.get("/{group_id}/sources")
def list_group_sources(
    group_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=False)
    gss = db.scalars(select(GroupSource).where(GroupSource.group_id == group_id)).all()
    result = []
    for gs in gss:
        s = db.get(Source, gs.source_id)
        if s:
            result.append({"source_id": gs.source_id, "name": s.name, "type": s.type, "url": s.url,
                           "added_at": gs.added_at.isoformat() if gs.added_at else None})
    return result


@router.post("/{group_id}/sources/{source_id}", status_code=201)
def add_source_to_group(
    group_id: str, source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=True)
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    existing = db.scalars(
        select(GroupSource).where(GroupSource.group_id == group_id, GroupSource.source_id == source_id)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Source already in group")
    gs = GroupSource(group_id=group_id, source_id=source_id, added_by=current_user.id)
    db.add(gs)
    db.commit()
    return {"group_id": group_id, "source_id": source_id}


@router.delete("/{group_id}/sources/{source_id}", status_code=204)
def remove_source_from_group(
    group_id: str, source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=True)
    gs = db.scalars(
        select(GroupSource).where(GroupSource.group_id == group_id, GroupSource.source_id == source_id)
    ).first()
    if gs is None:
        raise HTTPException(status_code=404, detail="Source not in group")
    db.delete(gs)
    db.commit()


# ── Group Profile (filter_criteria) ──────────────────────────────────

@router.get("/{group_id}/profile")
def get_group_profile(
    group_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=False)
    p = db.get(GroupProfile, group_id)
    return {"group_id": group_id, "filter_criteria": p.filter_criteria if p else {}}


class GroupProfileUpdate(BaseModel):
    filter_criteria: dict


@router.put("/{group_id}/profile")
def update_group_profile(
    group_id: str,
    body: GroupProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=True)
    p = db.get(GroupProfile, group_id)
    if p is None:
        p = GroupProfile(group_id=group_id)
        db.add(p)
    p.filter_criteria = body.filter_criteria
    p.updated_by = current_user.id
    db.commit()
    return {"group_id": group_id, "filter_criteria": p.filter_criteria}
```

- [ ] **Step 2: Register router in `backend/app/api/main.py`**

```python
from app.api.group_routes import router as group_router
app.include_router(group_router)
```

- [ ] **Step 3: Also add global sources listing endpoint to admin routes**

In `backend/app/api/admin_group_routes.py`, add at the end:

```python
@router.get("/../../sources/global", include_in_schema=True)
def list_global_sources(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """List all non-agent_crawl sources available to subscribe to."""
    from app.models import Source as SourceModel
    sources = db.scalars(
        select(SourceModel).where(SourceModel.type != "agent_crawl", SourceModel.enabled == True)
        .order_by(SourceModel.name)
    ).all()
    return [{"id": s.id, "name": s.name, "type": s.type, "url": s.url} for s in sources]
```

Actually, this endpoint path is awkward. Instead add it to a separate router:

In `backend/app/api/main.py`, add after the group_router:

```python
from fastapi import APIRouter as _AR
_sources_router = _AR(prefix="/sources", tags=["sources"])

@_sources_router.get("/global")
def list_global_sources(db = __import__("fastapi").Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db),
                        _user = __import__("fastapi").Depends(__import__("app.api.deps", fromlist=["get_current_user"]).get_current_user)):
    from sqlalchemy import select
    from app.models import Source
    rows = db.scalars(select(Source).where(Source.type != "agent_crawl", Source.enabled == True).order_by(Source.name)).all()
    return [{"id": s.id, "name": s.name, "type": s.type, "url": s.url} for s in rows]

app.include_router(_sources_router)
```

Actually, let me simplify — just add the route directly in group_routes.py as a top-level route, separate from the /groups prefix:

Add at the end of `backend/app/api/group_routes.py`:

```python
# Separate router for global source listing
sources_router = APIRouter(prefix="/sources", tags=["sources"])

@sources_router.get("/global")
def list_global_sources(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all non-agent_crawl global sources available for groups to subscribe to."""
    sources = db.scalars(
        select(Source)
        .where(Source.type != "agent_crawl", Source.enabled == True)
        .order_by(Source.name)
    ).all()
    return [{"id": s.id, "name": s.name, "type": s.type, "url": s.url} for s in sources]
```

And in `backend/app/api/main.py` also register `sources_router`:

```python
from app.api.group_routes import router as group_router, sources_router
app.include_router(group_router)
app.include_router(sources_router)
```

- [ ] **Step 4: Run full test suite**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all existing tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/api/group_routes.py app/api/main.py && git commit -m "feat: add group member/source/profile/join-request API routes"
```

---

### Task 7: Group Digest Schedule API

**Files:**
- Create: `backend/app/api/group_digest_routes.py`
- Modify: `backend/app/api/main.py`

- [ ] **Step 1: Create `backend/app/api/group_digest_routes.py`**

```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.groups.permissions import check_group_access
from app.models import Digest, GroupDigestSchedule, User, UserProfile

router = APIRouter(prefix="/groups", tags=["group-digest"])


class ScheduleUpdate(BaseModel):
    enabled: bool = False
    frequency: str = "weekly"   # daily | weekly
    day_of_week: int | None = None
    hour_utc: int = 8


@router.get("/{group_id}/digest/schedule")
def get_schedule(
    group_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=False)
    s = db.get(GroupDigestSchedule, group_id)
    if s is None:
        return {"group_id": group_id, "enabled": False, "frequency": "weekly", "day_of_week": None, "hour_utc": 8}
    return {"group_id": group_id, "enabled": s.enabled, "frequency": s.frequency,
            "day_of_week": s.day_of_week, "hour_utc": s.hour_utc}


@router.put("/{group_id}/digest/schedule")
def update_schedule(
    group_id: str,
    body: ScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=True)
    s = db.get(GroupDigestSchedule, group_id)
    if s is None:
        s = GroupDigestSchedule(group_id=group_id)
        db.add(s)
    s.enabled = body.enabled
    s.frequency = body.frequency
    s.day_of_week = body.day_of_week
    s.hour_utc = body.hour_utc
    s.updated_by = current_user.id
    db.commit()
    return {"group_id": group_id, "enabled": s.enabled, "frequency": s.frequency,
            "day_of_week": s.day_of_week, "hour_utc": s.hour_utc}


@router.post("/{group_id}/digest", status_code=202)
def trigger_group_digest(
    group_id: str,
    body: dict = {},
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=True)
    from datetime import datetime, timedelta, timezone
    import uuid, threading
    from app.models import GroupSource

    now = datetime.now(timezone.utc)
    scope = body.get("scope", "group")
    days = int(body.get("days", 7))

    digest_id = str(uuid.uuid4())
    profile = db.get(UserProfile, current_user.id)
    criteria = profile.criteria if profile else []
    min_score = profile.min_score_threshold if profile else 0

    # Collect group's source IDs to filter items
    group_source_ids = [gs.source_id for gs in db.scalars(
        select(GroupSource).where(GroupSource.group_id == group_id)
    ).all()]

    from app.models import Digest as DigestModel
    d = DigestModel(
        id=digest_id,
        trigger_type="manual",
        created_by=current_user.id,
        time_range_start=now - timedelta(days=days),
        time_range_end=now,
        scope="personalized",
        status="generating",
    )
    db.add(d)
    db.commit()

    def _run_background():
        from app.db import SessionLocal
        from app.processing.digest_agent import DigestAgent
        bg_db = SessionLocal()
        try:
            agent = DigestAgent()
            result = agent.run(
                db=bg_db,
                time_range_start=now - timedelta(days=days),
                time_range_end=now,
                scope="group",
                criteria=criteria,
                min_score_threshold=min_score,
                source_id_filter=group_source_ids,
            )
            dig = bg_db.get(DigestModel, digest_id)
            if dig:
                dig.period_summary = result["period_summary"]
                dig.hotspots = result["hotspots"]
                dig.emerging_topics = result["emerging_topics"]
                dig.stats = result["stats"]
                dig.status = "ready"
                bg_db.commit()
        except Exception as e:
            dig = bg_db.get(DigestModel, digest_id)
            if dig:
                dig.status = "failed"
                dig.error_message = str(e)
                bg_db.commit()
        finally:
            bg_db.close()

    threading.Thread(target=_run_background, daemon=True).start()
    return {"id": digest_id, "status": "generating"}


@router.get("/{group_id}/digests")
def list_group_digests(
    group_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_group_access(db, group_id, current_user, require_admin=False)
    # Return digests created by members of this group
    from app.models import GroupMember as GM
    member_ids = [m.user_id for m in db.scalars(select(GM).where(GM.group_id == group_id)).all()]
    digests = db.scalars(
        select(Digest)
        .where(or_(Digest.created_by.in_(member_ids), Digest.trigger_type == "scheduled"))
        .order_by(Digest.created_at.desc())
        .limit(30)
    ).all()
    return [
        {"id": d.id, "status": d.status, "scope": d.scope, "trigger_type": d.trigger_type,
         "time_range_start": d.time_range_start.isoformat() if d.time_range_start else None,
         "time_range_end": d.time_range_end.isoformat() if d.time_range_end else None,
         "created_at": d.created_at.isoformat() if d.created_at else None}
        for d in digests
    ]
```

- [ ] **Step 2: Update `DigestAgent.run()` to accept `source_id_filter`**

In `backend/app/processing/digest_agent.py`, update the `run()` method signature and SQL query:

```python
def run(
    self,
    db: Session,
    time_range_start: datetime,
    time_range_end: datetime,
    scope: str,
    criteria: list,
    min_score_threshold: int,
    source_id_filter: list[int] | None = None,   # ← add this parameter
) -> dict:
    stmt = select(Item).where(
        Item.published_at >= time_range_start,
        Item.published_at <= time_range_end,
    )
    if source_id_filter:   # ← add this block
        stmt = stmt.where(Item.source_id.in_(source_id_filter))
    items = db.scalars(stmt).all()
    # ... rest unchanged
```

- [ ] **Step 3: Register router**

In `backend/app/api/main.py`:

```python
from app.api.group_digest_routes import router as group_digest_router
app.include_router(group_digest_router)
```

- [ ] **Step 4: Run full test suite**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/ -v 2>&1 | tail -10
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/api/group_digest_routes.py app/api/main.py app/processing/digest_agent.py && git commit -m "feat: add group digest schedule API + group-scoped digest generation"
```

---

### Task 8: Modify `/items` and `/facets` to Filter by Group

**Files:**
- Modify: `backend/app/api/routes.py`

- [ ] **Step 1: Update `list_items` to filter by user's groups**

In `backend/app/api/routes.py`, update the `list_items` function. Add after the existing imports:

```python
from app.groups.feed_filter import apply_group_filter_to_items, parse_filter_criteria
```

Add `authorization` header param to the function signature (it may already be there from V2 personalization plan):

```python
from fastapi import Header
```

Inside `list_items`, **before** the SQL execution, add the group source filtering logic:

```python
    # --- Group membership filter ---
    group_source_ids: list[int] | None = None
    group_filter_map: dict[int, list] = {}
    user_has_no_groups = False

    if authorization and authorization.startswith("Bearer "):
        from app.auth import verify_access_token
        from app.models import GroupMember, GroupProfile, GroupSource
        from sqlalchemy import select as _select
        token = authorization.removeprefix("Bearer ").strip()
        payload = verify_access_token(token)
        if payload:
            uid = payload["sub"]
            memberships = db.scalars(
                _select(GroupMember).where(GroupMember.user_id == uid)
            ).all()
            if not memberships:
                user_has_no_groups = True
            else:
                gids = [m.group_id for m in memberships]
                group_sources = db.scalars(
                    _select(GroupSource).where(GroupSource.group_id.in_(gids))
                ).all()
                group_source_ids = list({gs.source_id for gs in group_sources})
                # Build filter map: source_id → list of group FilterCriteria
                from collections import defaultdict
                source_to_groups = defaultdict(list)
                for gs in group_sources:
                    source_to_groups[gs.source_id].append(gs.group_id)
                for source_id, relevant_gids in source_to_groups.items():
                    for gid in relevant_gids:
                        profile = db.get(GroupProfile, gid)
                        if profile and profile.filter_criteria:
                            criteria_obj = parse_filter_criteria(profile.filter_criteria)
                            group_filter_map.setdefault(source_id, []).append(criteria_obj)

    if user_has_no_groups:
        return {"total": 0, "items": [], "no_groups": True}

    if group_source_ids is not None:
        stmt = stmt.where(Item.source_id.in_(group_source_ids))
```

Then after fetching `rows`, apply the group filter before scoring:

```python
    rows = db.scalars(stmt.order_by(order_clause).limit(limit).offset(offset)).all()

    if group_filter_map:
        rows = apply_group_filter_to_items(rows, group_filter_map)
```

- [ ] **Step 2: Update `facets` endpoint to filter by group**

In `backend/app/api/routes.py`, update the `facets` function similarly — add `authorization: str | None = Header(default=None)` param and filter the counts by group source IDs when the user is logged in.

Replace the existing `facets` function:

```python
@router.get("/facets")
def facets(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    source_filter: list[int] | None = None
    if authorization and authorization.startswith("Bearer "):
        from app.auth import verify_access_token
        from app.models import GroupMember, GroupSource
        token = authorization.removeprefix("Bearer ").strip()
        payload = verify_access_token(token)
        if payload:
            uid = payload["sub"]
            memberships = db.scalars(select(GroupMember).where(GroupMember.user_id == uid)).all()
            gids = [m.group_id for m in memberships]
            if gids:
                gss = db.scalars(select(GroupSource).where(GroupSource.group_id.in_(gids))).all()
                source_filter = list({gs.source_id for gs in gss})

    def _counts(column):
        stmt = select(column, func.count()).group_by(column)
        if source_filter is not None:
            stmt = stmt.where(Item.source_id.in_(source_filter))
        rows = db.execute(stmt).all()
        return [{"value": value, "count": count} for value, count in rows if value is not None]

    sub_tag_stmt = (
        select(Tag.name, func.count(func.distinct(ItemTag.item_id)))
        .join(ItemTag, Tag.id == ItemTag.tag_id)
        .where(Tag.kind == "sub_tag")
        .group_by(Tag.name)
        .order_by(func.count(func.distinct(ItemTag.item_id)).desc())
        .limit(30)
    )
    if source_filter is not None:
        sub_tag_stmt = sub_tag_stmt.join(Item, Item.id == ItemTag.item_id).where(Item.source_id.in_(source_filter))
    sub_tag_rows = db.execute(sub_tag_stmt).all()

    return {
        "main_category": _counts(Item.main_category),
        "info_type": _counts(Item.info_type),
        "importance": _counts(Item.importance),
        "sub_tags": [{"value": name, "count": count} for name, count in sub_tag_rows],
    }
```

- [ ] **Step 3: Run integration tests**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_api.py -v 2>&1 | tail -20
```

Expected: all existing tests PASS (unauthenticated requests still see all items).

- [ ] **Step 4: Commit**

```bash
cd backend && git add app/api/routes.py && git commit -m "feat: filter /items and /facets by user's group subscriptions"
```

---

### Task 9: Frontend Group Pages

**Files:**
- Create: `frontend/src/pages/MyGroupsPage.tsx`
- Create: `frontend/src/pages/GroupDetailPage.tsx`
- Create: `frontend/src/pages/AdminGroupsPage.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Add group API calls to `frontend/src/api/client.ts`**

```typescript
import type { Group } from "../types";

// Add to types.ts:
// export interface Group { id: string; name: string; description: string; is_active: boolean; is_member?: boolean; }

export async function fetchMyGroups() {
  const res = await fetch("/api/groups/my", { headers: authHeaders() });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json();
}

export async function fetchPublicGroups() {
  const res = await fetch("/api/groups", { headers: authHeaders() });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json();
}

export async function applyToJoinGroup(groupId: string, message?: string) {
  const res = await fetch(`/api/groups/${groupId}/join-requests`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ message: message ?? null }),
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json();
}

export async function fetchGroupSources(groupId: string) {
  const res = await fetch(`/api/groups/${groupId}/sources`, { headers: authHeaders() });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json();
}

export async function fetchGlobalSources() {
  const res = await fetch("/api/sources/global", { headers: authHeaders() });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json();
}

export async function addSourceToGroup(groupId: string, sourceId: number) {
  const res = await fetch(`/api/groups/${groupId}/sources/${sourceId}`, {
    method: "POST", headers: authHeaders(),
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json();
}

export async function removeSourceFromGroup(groupId: string, sourceId: number) {
  const res = await fetch(`/api/groups/${groupId}/sources/${sourceId}`, {
    method: "DELETE", headers: authHeaders(),
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
}

export async function fetchGroupProfile(groupId: string) {
  const res = await fetch(`/api/groups/${groupId}/profile`, { headers: authHeaders() });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json();
}

export async function updateGroupProfile(groupId: string, filterCriteria: object) {
  const res = await fetch(`/api/groups/${groupId}/profile`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ filter_criteria: filterCriteria }),
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json();
}

export async function adminCreateGroup(name: string, description: string, adminUserId: string) {
  const res = await fetch("/api/admin/groups", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ name, description, admin_user_id: adminUserId }),
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json();
}

export async function adminListGroups() {
  const res = await fetch("/api/admin/groups", { headers: authHeaders() });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json();
}

export async function adminDeleteGroup(groupId: string) {
  const res = await fetch(`/api/admin/groups/${groupId}`, { method: "DELETE", headers: authHeaders() });
  if (!res.ok) throw new ApiError(res.status, await res.text());
}

export async function fetchJoinRequests(groupId: string) {
  const res = await fetch(`/api/groups/${groupId}/join-requests`, { headers: authHeaders() });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json();
}

export async function reviewJoinRequest(groupId: string, requestId: string, status: "approved" | "rejected") {
  const res = await fetch(`/api/groups/${groupId}/join-requests/${requestId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ status }),
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json();
}
```

- [ ] **Step 2: Create `frontend/src/pages/MyGroupsPage.tsx`**

```tsx
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchMyGroups, fetchPublicGroups, applyToJoinGroup } from "../api/client";

export function MyGroupsPage() {
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useState<"mine" | "browse">("mine");
  const [applyMessage, setApplyMessage] = useState("");
  const [applyingTo, setApplyingTo] = useState<string | null>(null);

  const { data: myGroups } = useQuery({ queryKey: ["my-groups"], queryFn: fetchMyGroups });
  const { data: publicGroups } = useQuery({ queryKey: ["public-groups"], queryFn: fetchPublicGroups, enabled: activeTab === "browse" });

  const applyMutation = useMutation({
    mutationFn: ({ groupId, message }: { groupId: string; message: string }) =>
      applyToJoinGroup(groupId, message),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["public-groups"] }); setApplyingTo(null); },
  });

  const tabStyle = (tab: string) => ({
    padding: "6px 16px", borderRadius: 8, border: "none", cursor: "pointer",
    background: activeTab === tab ? "var(--accent)" : "var(--surface-tint)",
    color: activeTab === tab ? "#fff" : "var(--ink-secondary)", fontSize: 13, fontWeight: 600,
  });

  return (
    <div style={{ maxWidth: 800, margin: "0 auto", padding: 24 }}>
      <h2 style={{ color: "var(--ink)", marginBottom: 20 }}>群组</h2>
      <div style={{ display: "flex", gap: 8, marginBottom: 20 }}>
        <button style={tabStyle("mine")} onClick={() => setActiveTab("mine")}>我的群组</button>
        <button style={tabStyle("browse")} onClick={() => setActiveTab("browse")}>浏览加入</button>
      </div>

      {activeTab === "mine" && (
        <div>
          {(myGroups ?? []).length === 0 ? (
            <div style={{ color: "var(--ink-muted)", padding: "32px 0", textAlign: "center" }}>
              你还没有加入任何群组。切换到「浏览加入」申请加入现有群组。
            </div>
          ) : (
            (myGroups ?? []).map((g: { id: string; name: string; description: string; role: string }) => (
              <div key={g.id} style={{
                background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 10,
                padding: 16, marginBottom: 10, display: "flex", alignItems: "center", gap: 16,
              }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 600, color: "var(--ink)" }}>{g.name}</div>
                  <div style={{ fontSize: 12, color: "var(--ink-muted)" }}>{g.description}</div>
                </div>
                <span style={{
                  padding: "2px 10px", borderRadius: 6, fontSize: 11, fontWeight: 600,
                  background: g.role === "group_admin" ? "var(--accent-subtle)" : "var(--surface-tint)",
                  color: g.role === "group_admin" ? "var(--accent)" : "var(--ink-muted)",
                }}>{g.role === "group_admin" ? "管理员" : "成员"}</span>
              </div>
            ))
          )}
        </div>
      )}

      {activeTab === "browse" && (
        <div>
          {(publicGroups ?? []).map((g: { id: string; name: string; description: string; is_member: boolean }) => (
            <div key={g.id} style={{
              background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 10,
              padding: 16, marginBottom: 10,
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 600, color: "var(--ink)" }}>{g.name}</div>
                  <div style={{ fontSize: 12, color: "var(--ink-muted)" }}>{g.description}</div>
                </div>
                {g.is_member ? (
                  <span style={{ fontSize: 12, color: "var(--signal-high)" }}>已加入</span>
                ) : (
                  <button onClick={() => setApplyingTo(g.id)} style={{
                    padding: "6px 14px", background: "var(--accent)", color: "#fff",
                    border: "none", borderRadius: 8, fontSize: 12, cursor: "pointer",
                  }}>申请加入</button>
                )}
              </div>
              {applyingTo === g.id && (
                <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
                  <input
                    value={applyMessage}
                    onChange={e => setApplyMessage(e.target.value)}
                    placeholder="申请理由（可选）"
                    style={{ flex: 1, padding: "8px 12px", border: "1px solid var(--border)", borderRadius: 8, fontSize: 13, fontFamily: "var(--font-body)" }}
                  />
                  <button onClick={() => applyMutation.mutate({ groupId: g.id, message: applyMessage })}
                    style={{ padding: "8px 16px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: 8, fontSize: 13, cursor: "pointer" }}>
                    提交
                  </button>
                  <button onClick={() => setApplyingTo(null)}
                    style={{ padding: "8px 12px", background: "var(--surface-tint)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 13, cursor: "pointer", color: "var(--ink-muted)" }}>
                    取消
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Create `frontend/src/pages/AdminGroupsPage.tsx`**

```tsx
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { adminListGroups, adminCreateGroup, adminDeleteGroup, fetchJoinRequests, reviewJoinRequest } from "../api/client";
import { getUser } from "../auth";

export function AdminGroupsPage() {
  const qc = useQueryClient();
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [selectedGroup, setSelectedGroup] = useState<string | null>(null);
  const currentUser = getUser();

  const { data: groups } = useQuery({ queryKey: ["admin-groups"], queryFn: adminListGroups });
  const { data: requests } = useQuery({
    queryKey: ["join-requests", selectedGroup],
    queryFn: () => fetchJoinRequests(selectedGroup!),
    enabled: !!selectedGroup,
  });

  const createMutation = useMutation({
    mutationFn: () => adminCreateGroup(name, desc, currentUser?.id ?? ""),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["admin-groups"] }); setShowCreate(false); setName(""); setDesc(""); },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => adminDeleteGroup(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["admin-groups"] }),
  });

  const reviewMutation = useMutation({
    mutationFn: ({ gid, rid, status }: { gid: string; rid: string; status: "approved" | "rejected" }) =>
      reviewJoinRequest(gid, rid, status),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["join-requests", selectedGroup] }),
  });

  const inputStyle = { padding: "8px 12px", border: "1px solid var(--border)", borderRadius: 8, fontSize: 13, fontFamily: "var(--font-body)", width: "100%", color: "var(--ink)" };

  return (
    <div style={{ maxWidth: 1000, margin: "0 auto", padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <h2 style={{ margin: 0, color: "var(--ink)" }}>群组管理</h2>
        <button onClick={() => setShowCreate(!showCreate)} style={{
          padding: "8px 18px", background: "var(--accent)", color: "#fff",
          border: "none", borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: "pointer",
        }}>+ 创建群组</button>
      </div>

      {showCreate && (
        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 10, padding: 20, marginBottom: 24 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
            <div>
              <label style={{ fontSize: 12, color: "var(--ink-secondary)", display: "block", marginBottom: 4 }}>群组名称</label>
              <input value={name} onChange={e => setName(e.target.value)} style={inputStyle} />
            </div>
            <div>
              <label style={{ fontSize: 12, color: "var(--ink-secondary)", display: "block", marginBottom: 4 }}>描述</label>
              <input value={desc} onChange={e => setDesc(e.target.value)} style={inputStyle} />
            </div>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={() => createMutation.mutate()} disabled={!name} style={{
              padding: "8px 18px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: "pointer",
            }}>保存</button>
            <button onClick={() => setShowCreate(false)} style={{
              padding: "8px 14px", background: "var(--surface-tint)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 13, cursor: "pointer", color: "var(--ink-muted)",
            }}>取消</button>
          </div>
        </div>
      )}

      <div style={{ display: "flex", gap: 20 }}>
        <div style={{ flex: 1 }}>
          {(groups ?? []).map((g: { id: string; name: string; description: string; is_active: boolean }) => (
            <div key={g.id} onClick={() => setSelectedGroup(g.id)} style={{
              background: selectedGroup === g.id ? "var(--accent-subtle)" : "var(--surface)",
              border: `1px solid ${selectedGroup === g.id ? "var(--accent)" : "var(--border)"}`,
              borderRadius: 10, padding: 14, marginBottom: 8, cursor: "pointer",
              display: "flex", alignItems: "center", gap: 14,
            }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600, color: "var(--ink)" }}>{g.name}</div>
                <div style={{ fontSize: 12, color: "var(--ink-muted)" }}>{g.description}</div>
              </div>
              <button onClick={(e) => { e.stopPropagation(); deleteMutation.mutate(g.id); }} style={{
                padding: "3px 10px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 11,
                color: "var(--ink-muted)", cursor: "pointer", background: "var(--surface)",
              }}>删除</button>
            </div>
          ))}
        </div>

        {selectedGroup && (
          <div style={{ width: 320, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 10, padding: 16 }}>
            <div style={{ fontWeight: 600, marginBottom: 12, color: "var(--ink)" }}>待审批申请</div>
            {(requests ?? []).length === 0 ? (
              <div style={{ color: "var(--ink-muted)", fontSize: 13 }}>暂无待审批申请</div>
            ) : (
              (requests ?? []).map((r: { id: string; user_id: string; message: string; created_at: string }) => (
                <div key={r.id} style={{ borderBottom: "1px solid var(--border-subtle)", paddingBottom: 12, marginBottom: 12 }}>
                  <div style={{ fontSize: 13, color: "var(--ink)", marginBottom: 4 }}>{r.user_id}</div>
                  {r.message && <div style={{ fontSize: 12, color: "var(--ink-muted)", marginBottom: 8 }}>{r.message}</div>}
                  <div style={{ display: "flex", gap: 6 }}>
                    <button onClick={() => reviewMutation.mutate({ gid: selectedGroup, rid: r.id, status: "approved" })} style={{
                      padding: "4px 12px", background: "var(--signal-high)", color: "#fff", border: "none", borderRadius: 6, fontSize: 12, cursor: "pointer",
                    }}>通过</button>
                    <button onClick={() => reviewMutation.mutate({ gid: selectedGroup, rid: r.id, status: "rejected" })} style={{
                      padding: "4px 12px", background: "var(--surface-tint)", border: "1px solid var(--border)", borderRadius: 6, fontSize: 12, cursor: "pointer", color: "var(--ink-muted)",
                    }}>拒绝</button>
                  </div>
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Create `frontend/src/pages/GroupDetailPage.tsx`**

```tsx
import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  fetchGroupSources, fetchGlobalSources, addSourceToGroup, removeSourceFromGroup,
  fetchGroupProfile, updateGroupProfile,
} from "../api/client";

export function GroupDetailPage() {
  const { id: groupId } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const [editingFilter, setEditingFilter] = useState(false);
  const [filterJson, setFilterJson] = useState("");

  const { data: sources } = useQuery({ queryKey: ["group-sources", groupId], queryFn: () => fetchGroupSources(groupId!) });
  const { data: globalSources } = useQuery({ queryKey: ["global-sources"], queryFn: fetchGlobalSources });
  const { data: profile } = useQuery({ queryKey: ["group-profile", groupId], queryFn: () => fetchGroupProfile(groupId!) });

  const addMutation = useMutation({
    mutationFn: (sourceId: number) => addSourceToGroup(groupId!, sourceId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["group-sources", groupId] }),
  });
  const removeMutation = useMutation({
    mutationFn: (sourceId: number) => removeSourceFromGroup(groupId!, sourceId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["group-sources", groupId] }),
  });
  const updateFilterMutation = useMutation({
    mutationFn: (fc: object) => updateGroupProfile(groupId!, fc),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["group-profile", groupId] }); setEditingFilter(false); },
  });

  const groupSourceIds = new Set((sources ?? []).map((s: { source_id: number }) => s.source_id));

  return (
    <div style={{ maxWidth: 900, margin: "0 auto", padding: 24 }}>
      <h2 style={{ color: "var(--ink)", marginBottom: 24 }}>群组详情</h2>

      {/* Sources */}
      <section style={{ marginBottom: 32 }}>
        <div style={{ fontWeight: 600, color: "var(--ink-secondary)", fontSize: 13, textTransform: "uppercase", letterSpacing: 1, marginBottom: 12 }}>
          来源订阅
        </div>
        <div style={{ display: "flex", gap: 20 }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 12, color: "var(--ink-muted)", marginBottom: 8 }}>已订阅 ({(sources ?? []).length})</div>
            {(sources ?? []).map((s: { source_id: number; name: string; type: string }) => (
              <div key={s.source_id} style={{
                background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8,
                padding: "8px 12px", marginBottom: 6, display: "flex", alignItems: "center", gap: 10,
              }}>
                <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, background: "var(--surface-tint)", padding: "1px 6px", borderRadius: 4, color: "var(--ink-muted)" }}>{s.type}</span>
                <span style={{ flex: 1, fontSize: 13, color: "var(--ink)" }}>{s.name}</span>
                <button onClick={() => removeMutation.mutate(s.source_id)} style={{
                  padding: "2px 8px", border: "1px solid var(--border)", borderRadius: 5, fontSize: 11, cursor: "pointer", background: "var(--surface)", color: "var(--ink-muted)",
                }}>移除</button>
              </div>
            ))}
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 12, color: "var(--ink-muted)", marginBottom: 8 }}>全局来源（点击添加）</div>
            {(globalSources ?? []).filter((s: { id: number }) => !groupSourceIds.has(s.id)).map((s: { id: number; name: string; type: string }) => (
              <div key={s.id} onClick={() => addMutation.mutate(s.id)} style={{
                background: "var(--surface)", border: "1px solid var(--border-subtle)", borderRadius: 8,
                padding: "8px 12px", marginBottom: 6, cursor: "pointer", display: "flex", alignItems: "center", gap: 10,
                opacity: 0.8,
              }}>
                <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, background: "var(--surface-tint)", padding: "1px 6px", borderRadius: 4, color: "var(--ink-muted)" }}>{s.type}</span>
                <span style={{ flex: 1, fontSize: 13, color: "var(--ink-secondary)" }}>{s.name}</span>
                <span style={{ fontSize: 12, color: "var(--accent)" }}>+ 添加</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Group Filter */}
      <section>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <div style={{ fontWeight: 600, color: "var(--ink-secondary)", fontSize: 13, textTransform: "uppercase", letterSpacing: 1 }}>
            群级内容过滤
          </div>
          <button onClick={() => { setEditingFilter(!editingFilter); setFilterJson(JSON.stringify(profile?.filter_criteria ?? {}, null, 2)); }} style={{
            padding: "5px 14px", border: "1px solid var(--border)", borderRadius: 8, fontSize: 12, cursor: "pointer", background: "var(--surface)", color: "var(--ink-secondary)",
          }}>编辑</button>
        </div>
        {editingFilter ? (
          <div>
            <textarea
              value={filterJson}
              onChange={e => setFilterJson(e.target.value)}
              rows={12}
              style={{ width: "100%", padding: "10px 12px", border: "1px solid var(--border)", borderRadius: 8, fontFamily: "var(--font-mono)", fontSize: 13, color: "var(--ink)" }}
            />
            <div style={{ fontSize: 11, color: "var(--ink-muted)", marginBottom: 8 }}>
              字段：keyword_whitelist / keyword_blacklist / category_whitelist / min_importance (高|中|低)
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button onClick={() => { try { updateFilterMutation.mutate(JSON.parse(filterJson)); } catch { alert("JSON 格式错误"); } }} style={{
                padding: "7px 18px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: "pointer",
              }}>保存</button>
              <button onClick={() => setEditingFilter(false)} style={{
                padding: "7px 12px", background: "var(--surface-tint)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 13, cursor: "pointer", color: "var(--ink-muted)",
              }}>取消</button>
            </div>
          </div>
        ) : (
          <pre style={{
            background: "var(--surface)", border: "1px solid var(--border-subtle)", borderRadius: 8,
            padding: 16, fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--ink-secondary)",
            overflow: "auto", margin: 0,
          }}>
            {JSON.stringify(profile?.filter_criteria ?? {}, null, 2)}
          </pre>
        )}
      </section>
    </div>
  );
}
```

- [ ] **Step 5: Update `frontend/src/App.tsx` to add new routes**

Add to the `<Routes>` section:

```tsx
import { MyGroupsPage } from "./pages/MyGroupsPage";
import { GroupDetailPage } from "./pages/GroupDetailPage";
import { AdminGroupsPage } from "./pages/AdminGroupsPage";

// inside Routes:
<Route path="/groups" element={<RequireAuth><MyGroupsPage /></RequireAuth>} />
<Route path="/groups/:id" element={<RequireAuth><GroupDetailPage /></RequireAuth>} />
<Route path="/admin/groups" element={<RequireAuth><AdminGroupsPage /></RequireAuth>} />
```

Also add「我的群组」入口到 HomePage header nav:

```tsx
<a href="/groups" onClick={e => { e.preventDefault(); navigate("/groups"); }}
   style={{ fontSize: 12, color: "#60a5fa" }}>我的群组</a>
```

- [ ] **Step 6: Add `Group` type to `frontend/src/types.ts`**

```typescript
export interface Group {
  id: string;
  name: string;
  description: string;
  is_active: boolean;
  is_member?: boolean;
  role?: string;
}
```

- [ ] **Step 7: Build and test**

```bash
cd frontend && npm run build 2>&1 | tail -5 && npx vitest run
```

Expected: build succeeds, all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/ && git commit -m "feat: add group pages (MyGroups, GroupDetail, AdminGroups) and group API calls"
```

---

### Task 10: Final Verification

- [ ] **Step 1: Run full backend test suite**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests PASS.

- [ ] **Step 2: Run frontend build + tests**

```bash
cd frontend && npm run build 2>&1 | tail -5 && npx vitest run
```

Expected: build succeeds, all tests PASS.

- [ ] **Step 3: Final commit tag**

```bash
git tag -a v2-group-subscription -m "V2 group subscription system complete"
```
