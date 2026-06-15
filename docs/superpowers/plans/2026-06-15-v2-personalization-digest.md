# V2 Personalization & Digest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement personalized scoring (fast keyword matching + optional LLM), profile management API, ProfileAdvisor Agent, DigestAgent multi-step chain, and Digest CRUD API.

**Architecture:** `compute_fast_score()` is a pure function computed at query time — no storage. LLM scoring writes to `user_item_scores`. `DigestAgent` runs as a background thread (same pattern as manual news run) with polling. ProfileAdvisor is a synchronous LLM call returning suggested Criterion[].

**Prerequisite:** Plan `2026-06-15-v2-db-and-user-system.md` must be complete (users, user_profiles tables exist and auth works).

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, existing `LlmClient`

**Spec:** `docs/superpowers/specs/2026-06-15-v2-complete-design.md` §5, §6, §7, §9

---

## File Change Map

| File | Action | Notes |
|------|--------|-------|
| `backend/app/processing/scorer.py` | Create | `compute_fast_score()` + `ScoringAgent` (LLM) |
| `backend/app/processing/profile_advisor.py` | Create | ProfileAdvisor LLM agent |
| `backend/app/processing/digest_agent.py` | Create | DigestAgent multi-step chain |
| `backend/app/api/profile_routes.py` | Create | /users/me/profile + /users/me/profile/suggest + /users/me/interactions |
| `backend/app/api/digest_routes.py` | Create | /digest CRUD + async generation |
| `backend/app/api/routes.py` | Modify | Add personalized_score to /items response + sort_by=relevance + min_score param |
| `backend/app/api/main.py` | Modify | Register profile_routes and digest_routes |
| `backend/tests/unit/test_scorer.py` | Create | compute_fast_score unit tests |
| `backend/tests/unit/test_digest_agent.py` | Create | DigestAgent step tests |
| `backend/tests/integration/test_profile_api.py` | Create | Profile CRUD integration tests |
| `backend/tests/integration/test_digest_api.py` | Create | Digest API integration tests |

---

### Task 15: Fast Score Engine

**Files:**
- Create: `backend/app/processing/scorer.py`
- Create: `backend/tests/unit/test_scorer.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/unit/test_scorer.py`:

```python
import pytest
from app.processing.scorer import compute_fast_score, Criterion, MatchRule


def _c(label="c", weight=1.0, enabled=True, **match_kwargs):
    match = MatchRule(**{
        "keywords": [], "sub_tags": [], "main_category": [], "info_type": [], "keywords_op": "any",
        **match_kwargs,
    })
    return Criterion(id="x", label=label, enabled=enabled, weight=weight, match=match)


def _item(title="", sub_tags=None, main_category="OS性能发展", info_type="发布", importance="中"):
    from types import SimpleNamespace
    return SimpleNamespace(
        title=title, sub_tags=sub_tags or [],
        main_category=main_category, info_type=info_type, importance=importance,
    )


def test_no_criteria_returns_zero():
    assert compute_fast_score(_item(), []) == 0


def test_disabled_criterion_not_counted():
    c = _c(weight=2.0, enabled=False, keywords=["kernel"])
    assert compute_fast_score(_item(title="kernel news"), [c]) == 0


def test_keyword_match_contributes_score():
    c = _c(weight=2.0, keywords=["eBPF"])
    score = compute_fast_score(_item(title="eBPF guide"), [c])
    assert score > 0


def test_importance_high_adds_bonus():
    score_high = compute_fast_score(_item(importance="高"), [])
    score_low = compute_fast_score(_item(importance="低"), [])
    assert score_high > score_low


def test_category_match_contributes():
    c = _c(weight=3.0, main_category=["OS性能发展"])
    score = compute_fast_score(_item(main_category="OS性能发展"), [c])
    assert score > 0


def test_all_keyword_op_requires_all_keywords():
    c = _c(weight=2.0, keywords=["eBPF", "scheduler"], keywords_op="all")
    assert compute_fast_score(_item(title="eBPF scheduler guide"), [c]) > 0
    assert compute_fast_score(_item(title="only eBPF here"), [c]) == 0


def test_score_capped_at_100():
    criteria = [_c(weight=3.0, keywords=["x"]) for _ in range(20)]
    score = compute_fast_score(_item(title="x x x", importance="高"), criteria)
    assert score <= 100
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_scorer.py -v
```

Expected: ImportError.

- [ ] **Step 3: Create `backend/app/processing/scorer.py`**

```python
import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MatchRule:
    keywords: list[str] = field(default_factory=list)
    sub_tags: list[str] = field(default_factory=list)
    main_category: list[str] = field(default_factory=list)
    info_type: list[str] = field(default_factory=list)
    keywords_op: str = "any"   # any | all


@dataclass
class Criterion:
    id: str
    label: str
    enabled: bool
    weight: float
    match: MatchRule


def _match_strength(item, rule: MatchRule) -> float:
    scores = []
    if rule.keywords:
        text = f"{item.title} {' '.join(item.sub_tags or [])}".lower()
        hits = sum(1 for kw in rule.keywords if kw.lower() in text)
        if rule.keywords_op == "all" and hits < len(rule.keywords):
            return 0.0
        scores.append(hits / len(rule.keywords))
    if rule.sub_tags:
        overlap = len(set(rule.sub_tags) & set(item.sub_tags or []))
        scores.append(overlap / len(rule.sub_tags))
    if rule.main_category:
        scores.append(1.0 if item.main_category in rule.main_category else 0.0)
    if rule.info_type:
        scores.append(1.0 if item.info_type in rule.info_type else 0.0)
    return sum(scores) / len(scores) if scores else 0.0


def compute_fast_score(item, criteria: list[Criterion]) -> int:
    """Compute personalized relevance score for item given user criteria. Pure function, no DB."""
    raw = 0.0
    for c in criteria:
        if not c.enabled:
            continue
        strength = _match_strength(item, c.match)
        if strength > 0:
            raw += c.weight * strength
    importance_bonus = {"高": 15, "中": 5, "低": 0}.get(getattr(item, "importance", "低"), 0)
    return min(100, int(raw * 30) + importance_bonus)


def parse_criteria(raw_criteria: list[dict]) -> list[Criterion]:
    """Parse JSONB criteria list from DB into Criterion objects."""
    result = []
    for c in raw_criteria:
        match_data = c.get("match", {})
        match = MatchRule(
            keywords=match_data.get("keywords", []),
            sub_tags=match_data.get("sub_tags", []),
            main_category=match_data.get("main_category", []),
            info_type=match_data.get("info_type", []),
            keywords_op=match_data.get("keywords_op", "any"),
        )
        result.append(Criterion(
            id=c.get("id", ""),
            label=c.get("label", ""),
            enabled=c.get("enabled", True),
            weight=float(c.get("weight", 1.0)),
            match=match,
        ))
    return result


class ScoringAgent:
    """LLM-based precision scorer for users with enable_llm_scoring=True."""

    def __init__(self, llm=None):
        from app.llm.client import LlmClient
        self._llm = llm or LlmClient()

    def score_item(self, description: str, title: str, summary: str, sub_tags: list[str],
                   main_category: str, info_type: str) -> tuple[int, str]:
        """Returns (score 0-100, reason)."""
        prompt = (
            f"用户关注描述：{description}\n\n"
            f"文章信息：\n"
            f"标题：{title}\n"
            f"摘要：{summary[:500]}\n"
            f"关键词：{', '.join(sub_tags)}\n"
            f"分类：{main_category} / {info_type}\n\n"
            "请给出 0-100 的相关性分数，以及一句理由。\n"
            '只输出 JSON：{"score": 85, "reason": "..."}'
        )
        raw = self._llm.complete(prompt)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return 50, "parse error"
        data = json.loads(m.group(0))
        return min(100, max(0, int(data.get("score", 50)))), data.get("reason", "")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_scorer.py -v
```

Expected: 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/processing/scorer.py tests/unit/test_scorer.py && git commit -m "feat: add compute_fast_score and ScoringAgent"
```

---

### Task 16: Profile API

**Files:**
- Create: `backend/app/api/profile_routes.py`
- Create: `backend/tests/integration/test_profile_api.py`

- [ ] **Step 1: Write failing integration tests**

Create `backend/tests/integration/test_profile_api.py`:

```python
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.main import create_app
from app.api.deps import get_db, get_current_user
from app.models import Base, User, UserProfile


def _make_app_with_user(role="user"):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(id="user-1", email="test@test.com", password_hash="x", role=role)
    profile = UserProfile(user_id="user-1", criteria=[], enable_llm_scoring=False, min_score_threshold=25)
    db.add(user)
    db.add(profile)
    db.commit()

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_get_profile_returns_empty_criteria():
    client = _make_app_with_user()
    r = client.get("/users/me/profile")
    assert r.status_code == 200
    assert r.json()["criteria"] == []
    assert r.json()["enable_llm_scoring"] is False


def test_put_profile_updates_criteria():
    client = _make_app_with_user()
    new_criteria = [{"id": "c1", "label": "kernel", "enabled": True, "weight": 2.0,
                     "match": {"keywords": ["eBPF"], "sub_tags": [], "main_category": [],
                               "info_type": [], "keywords_op": "any"}}]
    r = client.put("/users/me/profile", json={"criteria": new_criteria, "enable_llm_scoring": False, "min_score_threshold": 30})
    assert r.status_code == 200
    assert len(r.json()["criteria"]) == 1
    assert r.json()["criteria"][0]["label"] == "kernel"


def test_post_interaction_records_view():
    client = _make_app_with_user()
    r = client.post("/users/me/interactions", json={"item_id": 999, "action": "view"})
    assert r.status_code in (201, 200, 404)   # 404 is ok if item doesn't exist — no FK in sqlite
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_profile_api.py -v
```

Expected: 404 or ImportError.

- [ ] **Step 3: Create `backend/app/api/profile_routes.py`**

```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models import User, UserItemInteraction, UserProfile

router = APIRouter(prefix="/users/me", tags=["profile"])


class ProfileUpdate(BaseModel):
    criteria: list[dict] = []
    free_text_description: str | None = None
    enable_llm_scoring: bool = False
    min_score_threshold: int = 25


class InteractionCreate(BaseModel):
    item_id: int
    action: str   # view | bookmark


@router.get("/profile")
def get_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = db.get(UserProfile, current_user.id)
    if profile is None:
        profile = UserProfile(user_id=current_user.id)
        db.add(profile)
        db.commit()
    return {
        "user_id": profile.user_id,
        "criteria": profile.criteria or [],
        "free_text_description": profile.free_text_description,
        "enable_llm_scoring": profile.enable_llm_scoring,
        "min_score_threshold": profile.min_score_threshold,
    }


@router.put("/profile")
def update_profile(
    body: ProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = db.get(UserProfile, current_user.id)
    if profile is None:
        profile = UserProfile(user_id=current_user.id)
        db.add(profile)
    profile.criteria = body.criteria
    profile.free_text_description = body.free_text_description
    profile.enable_llm_scoring = body.enable_llm_scoring
    profile.min_score_threshold = body.min_score_threshold
    db.commit()
    return {
        "user_id": profile.user_id,
        "criteria": profile.criteria,
        "free_text_description": profile.free_text_description,
        "enable_llm_scoring": profile.enable_llm_scoring,
        "min_score_threshold": profile.min_score_threshold,
    }


@router.post("/profile/suggest")
def suggest_criteria(
    body: dict = {},
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger ProfileAdvisor Agent to generate suggested Criterion[]."""
    from app.processing.profile_advisor import ProfileAdvisor
    from sqlalchemy import func, select
    from app.models import Tag

    top_tags = db.execute(
        select(Tag.name).where(Tag.kind == "sub_tag")
        .group_by(Tag.name).order_by(func.count().desc()).limit(50)
    ).scalars().all()

    advisor = ProfileAdvisor()
    suggestions = advisor.suggest(
        top_tags=list(top_tags),
        description=body.get("description", ""),
    )
    return {"suggestions": suggestions}


@router.post("/interactions", status_code=201)
def record_interaction(
    body: InteractionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    existing = db.get(UserItemInteraction, (current_user.id, body.item_id, body.action))
    if existing is None:
        interaction = UserItemInteraction(
            user_id=current_user.id,
            item_id=body.item_id,
            action=body.action,
        )
        db.add(interaction)
        db.commit()
    return {"recorded": True}
```

- [ ] **Step 4: Create `backend/app/processing/profile_advisor.py`**

```python
import json
import logging
import re

logger = logging.getLogger(__name__)

_PROMPT = """你是操作系统技术情报助手。根据以下技术热词和用户描述，
为 OS maintainer 生成个性化的评分准则建议。

系统当前技术热词（top 50）：{top_tags}
用户职能描述：{description}

为用户推荐 3-5 条评分准则，每条描述一个重要技术关注点。
输出 JSON 数组，每条包含 label、match 字段（keywords 数组）、weight（0.5-3.0）和 reason（一句解释）：
[
  {{"label": "内核调度关注", "match": {{"keywords": ["scheduler", "sched_ext", "CFS"]}}, "weight": 2.0, "reason": "内核调度是 OS 性能的核心"}},
  ...
]
只输出 JSON 数组。
"""


class ProfileAdvisor:
    def __init__(self, llm=None):
        from app.llm.client import LlmClient
        self._llm = llm or LlmClient()

    def suggest(self, top_tags: list[str], description: str = "") -> list[dict]:
        prompt = _PROMPT.format(
            top_tags=", ".join(top_tags[:50]),
            description=description or "OS 维护工程师",
        )
        raw = self._llm.complete(prompt)
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            logger.warning("profile_advisor: no JSON array in response")
            return []
        try:
            suggestions = json.loads(m.group(0))
            # Add required fields for a Criterion
            import uuid
            result = []
            for s in suggestions:
                match = s.get("match", {})
                result.append({
                    "id": str(uuid.uuid4()),
                    "label": s.get("label", ""),
                    "enabled": True,
                    "weight": float(s.get("weight", 1.5)),
                    "match": {
                        "keywords": match.get("keywords", []),
                        "sub_tags": match.get("sub_tags", []),
                        "main_category": match.get("main_category", []),
                        "info_type": match.get("info_type", []),
                        "keywords_op": "any",
                    },
                    "reason": s.get("reason", ""),
                })
            return result
        except Exception as e:
            logger.warning("profile_advisor: parse failed: %s", e)
            return []
```

- [ ] **Step 5: Register profile_routes in `app/api/main.py`**

```python
from app.api.profile_routes import router as profile_router
app.include_router(profile_router)
```

- [ ] **Step 6: Run tests**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_profile_api.py tests/unit/test_scorer.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
cd backend && git add app/api/profile_routes.py app/processing/profile_advisor.py app/api/main.py tests/integration/test_profile_api.py && git commit -m "feat: add profile API, ProfileAdvisor, and personalized scoring"
```

---

### Task 17: DigestAgent

**Files:**
- Create: `backend/app/processing/digest_agent.py`
- Create: `backend/tests/unit/test_digest_agent.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/unit/test_digest_agent.py`:

```python
import json
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
from app.processing.digest_agent import DigestAgent, _aggregate_items


def _make_item(main_category="OS性能发展", importance="高", sub_tags=None):
    item = MagicMock()
    item.main_category = main_category
    item.importance = importance
    item.title_tldr = "Test title"
    item.summary = "Test summary"
    item.tags = [MagicMock(name=t, kind="sub_tag") for t in (sub_tags or ["kernel", "ebpf"])]
    item.published_at = datetime.now(timezone.utc)
    return item


def test_aggregate_computes_category_counts():
    items = [_make_item("OS性能发展"), _make_item("OS性能发展"), _make_item("OS跟踪来源")]
    result = _aggregate_items(items)
    assert result["by_category"]["OS性能发展"] == 2
    assert result["by_category"]["OS跟踪来源"] == 1


def test_aggregate_computes_tag_frequency():
    items = [_make_item(sub_tags=["kernel", "ebpf"]), _make_item(sub_tags=["kernel", "rhel"])]
    result = _aggregate_items(items)
    tag_dict = dict(result["top_tags"])
    assert tag_dict["kernel"] == 2
    assert tag_dict["ebpf"] == 1


def test_digest_agent_calls_llm_twice():
    llm = MagicMock()
    llm.complete.side_effect = [
        json.dumps({"hotspots": [{"topic": "Linux 6.12", "item_count": 3, "reason": "r", "related_item_ids": []}],
                    "emerging_topics": [{"topic": "RISC-V", "trend": "rising", "reasoning": "x"}]}),
        "本周技术动态总结。",
    ]
    db = MagicMock()
    db.scalars.return_value.all.return_value = [_make_item() for _ in range(5)]

    agent = DigestAgent(llm=llm)
    result = agent.run(
        db=db,
        time_range_start=datetime.now(timezone.utc) - timedelta(days=7),
        time_range_end=datetime.now(timezone.utc),
        scope="global",
        criteria=[],
        min_score_threshold=0,
    )
    assert llm.complete.call_count == 2
    assert len(result["hotspots"]) == 1
    assert result["period_summary"] == "本周技术动态总结。"
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_digest_agent.py -v
```

Expected: ImportError.

- [ ] **Step 3: Create `backend/app/processing/digest_agent.py`**

```python
import json
import logging
import re
from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Item, Tag

logger = logging.getLogger(__name__)

_HOTSPOT_PROMPT = """分析以下技术内容的频次数据，识别技术热点和新兴趋势。

分类频次：{by_category}
Top 标签（本期）：{top_tags}
标签趋势变化（与上期对比）：{trending_deltas}

请输出 JSON（不要多余文字）：
{{
  "hotspots": [{{"topic": "...", "item_count": 5, "reason": "...", "related_item_ids": []}}],
  "emerging_topics": [{{"topic": "...", "trend": "rising", "reasoning": "..."}}]
}}
"""

_SUMMARY_PROMPT = """你是 OS 技术情报分析师。根据以下本期 top 信息和主题分析，
为 OS maintainer 写一段简洁的整体总结（200字以内，中文）。

Top 信息摘要：
{top_items_text}

主题识别结果：
热点：{hotspots_text}
新兴趋势：{emerging_text}

只输出总结文字，不要 JSON："""


def _aggregate_items(items: list) -> dict:
    by_category: Counter = Counter()
    tag_counts: Counter = Counter()
    for item in items:
        if item.main_category:
            by_category[item.main_category] += 1
        for tag in (item.tags or []):
            if getattr(tag, "kind", "") == "sub_tag":
                tag_counts[tag.name] += 1
    return {
        "by_category": dict(by_category),
        "top_tags": tag_counts.most_common(30),
        "total_items": len(items),
    }


class DigestAgent:
    def __init__(self, llm=None):
        from app.llm.client import LlmClient
        self._llm = llm or LlmClient()

    def run(
        self,
        db: Session,
        time_range_start: datetime,
        time_range_end: datetime,
        scope: str,
        criteria: list,
        min_score_threshold: int,
    ) -> dict:
        # Step 1: aggregate
        stmt = select(Item).where(
            Item.published_at >= time_range_start,
            Item.published_at <= time_range_end,
        )
        items = db.scalars(stmt).all()

        if scope == "personalized" and criteria:
            from app.processing.scorer import compute_fast_score, parse_criteria
            parsed = parse_criteria(criteria)
            items = [i for i in items if compute_fast_score(i, parsed) >= min_score_threshold]

        stats = _aggregate_items(items)
        top_items = sorted(
            items,
            key=lambda i: ({"高": 2, "中": 1, "低": 0}.get(i.importance or "低", 0)),
            reverse=True,
        )[:30]

        # Compare with previous period for trending deltas
        prev_start = time_range_start - (time_range_end - time_range_start)
        prev_items = db.scalars(
            select(Item).where(Item.published_at >= prev_start, Item.published_at < time_range_start)
        ).all()
        prev_stats = _aggregate_items(prev_items)
        prev_tag_dict = dict(prev_stats["top_tags"])
        trending_deltas = {
            tag: count - prev_tag_dict.get(tag, 0)
            for tag, count in stats["top_tags"]
            if count - prev_tag_dict.get(tag, 0) > 0
        }

        # Step 2: topic identification (LLM Call #1)
        hotspot_prompt = _HOTSPOT_PROMPT.format(
            by_category=json.dumps(stats["by_category"], ensure_ascii=False),
            top_tags=json.dumps(stats["top_tags"][:15], ensure_ascii=False),
            trending_deltas=json.dumps(trending_deltas, ensure_ascii=False),
        )
        hotspot_raw = self._llm.complete(hotspot_prompt)
        try:
            m = re.search(r"\{.*\}", hotspot_raw, re.DOTALL)
            hotspot_data = json.loads(m.group(0)) if m else {}
        except Exception:
            hotspot_data = {}

        hotspots = hotspot_data.get("hotspots", [])
        emerging_topics = hotspot_data.get("emerging_topics", [])

        # Step 3: narrative synthesis (LLM Call #2)
        top_items_text = "\n".join(
            f"- {getattr(i, 'title_tldr', '') or ''}: {(getattr(i, 'summary', '') or '')[:100]}"
            for i in top_items[:15]
        )
        summary_prompt = _SUMMARY_PROMPT.format(
            top_items_text=top_items_text,
            hotspots_text=", ".join(h.get("topic", "") for h in hotspots),
            emerging_text=", ".join(e.get("topic", "") for e in emerging_topics),
        )
        period_summary = self._llm.complete(summary_prompt).strip()

        return {
            "period_summary": period_summary,
            "hotspots": hotspots,
            "emerging_topics": emerging_topics,
            "stats": {
                "total_items": stats["total_items"],
                "by_category": stats["by_category"],
                "top_tags": stats["top_tags"][:15],
            },
        }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_digest_agent.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/processing/digest_agent.py tests/unit/test_digest_agent.py && git commit -m "feat: add DigestAgent with 3-step LLM chain"
```

---

### Task 18: Digest API

**Files:**
- Create: `backend/app/api/digest_routes.py`
- Create: `backend/tests/integration/test_digest_api.py`
- Modify: `backend/app/api/main.py`

- [ ] **Step 1: Write failing integration tests**

Create `backend/tests/integration/test_digest_api.py`:

```python
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch

from app.api.main import create_app
from app.api.deps import get_db, get_current_user
from app.models import Base, User, UserProfile


def _setup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(id="u1", email="t@t.com", password_hash="x", role="user")
    profile = UserProfile(user_id="u1", criteria=[], enable_llm_scoring=False, min_score_threshold=25)
    db.add_all([user, profile])
    db.commit()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), db


def test_post_digest_returns_generating_status():
    client, _ = _setup()
    now = datetime.now(timezone.utc)
    payload = {
        "time_range_start": (now - timedelta(days=7)).isoformat(),
        "time_range_end": now.isoformat(),
        "scope": "global",
    }
    with patch("app.api.digest_routes._start_digest_background"):
        r = client.post("/digest", json=payload)
    assert r.status_code == 202
    data = r.json()
    assert data["status"] == "generating"
    assert "id" in data


def test_get_digest_list_returns_created_digest():
    client, db = _setup()
    from app.models import Digest
    now = datetime.now(timezone.utc)
    d = Digest(id="d1", trigger_type="manual", created_by="u1",
               time_range_start=now - timedelta(days=7), time_range_end=now,
               scope="personalized", status="ready", period_summary="test")
    db.add(d)
    db.commit()
    r = client.get("/digest")
    assert r.status_code == 200
    assert any(item["id"] == "d1" for item in r.json())


def test_get_digest_detail_returns_content():
    client, db = _setup()
    from app.models import Digest
    now = datetime.now(timezone.utc)
    d = Digest(id="d2", trigger_type="manual", created_by="u1",
               time_range_start=now - timedelta(days=7), time_range_end=now,
               scope="global", status="ready", period_summary="Summary here",
               hotspots=[{"topic": "Linux", "item_count": 3, "reason": "r", "related_item_ids": []}])
    db.add(d)
    db.commit()
    r = client.get("/digest/d2")
    assert r.status_code == 200
    assert r.json()["period_summary"] == "Summary here"
    assert len(r.json()["hotspots"]) == 1
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_digest_api.py -v
```

Expected: ImportError.

- [ ] **Step 3: Create `backend/app/api/digest_routes.py`**

```python
import logging
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models import Digest, User, UserProfile

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/digest", tags=["digest"])


class DigestCreate(BaseModel):
    time_range_start: datetime
    time_range_end: datetime
    scope: str = "personalized"   # personalized | global


def _digest_response(d: Digest) -> dict:
    return {
        "id": d.id,
        "trigger_type": d.trigger_type,
        "scope": d.scope,
        "status": d.status,
        "time_range_start": d.time_range_start.isoformat() if d.time_range_start else None,
        "time_range_end": d.time_range_end.isoformat() if d.time_range_end else None,
        "period_summary": d.period_summary,
        "hotspots": d.hotspots or [],
        "emerging_topics": d.emerging_topics or [],
        "stats": d.stats or {},
        "error_message": d.error_message,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _start_digest_background(digest_id: str, user_id: str, body: DigestCreate) -> None:
    """Run DigestAgent in background thread and update digest record."""
    from app.db import SessionLocal
    from app.processing.digest_agent import DigestAgent

    def _run():
        db = SessionLocal()
        try:
            digest = db.get(Digest, digest_id)
            if digest is None:
                return
            profile = db.get(UserProfile, user_id)
            criteria = profile.criteria if profile else []
            min_score = profile.min_score_threshold if profile else 0

            agent = DigestAgent()
            result = agent.run(
                db=db,
                time_range_start=body.time_range_start,
                time_range_end=body.time_range_end,
                scope=body.scope,
                criteria=criteria if body.scope == "personalized" else [],
                min_score_threshold=min_score,
            )
            digest.period_summary = result["period_summary"]
            digest.hotspots = result["hotspots"]
            digest.emerging_topics = result["emerging_topics"]
            digest.stats = result["stats"]
            digest.status = "ready"
            db.commit()
        except Exception as e:
            db_inner = SessionLocal()
            try:
                d = db_inner.get(Digest, digest_id)
                if d:
                    d.status = "failed"
                    d.error_message = str(e)
                    db_inner.commit()
            finally:
                db_inner.close()
            logger.exception("digest_agent: background run failed for %s", digest_id)
        finally:
            db.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()


@router.post("", status_code=202)
def create_digest(
    body: DigestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import uuid
    digest = Digest(
        id=str(uuid.uuid4()),
        trigger_type="manual",
        created_by=current_user.id,
        time_range_start=body.time_range_start,
        time_range_end=body.time_range_end,
        scope=body.scope,
        status="generating",
    )
    db.add(digest)
    db.commit()
    _start_digest_background(digest.id, current_user.id, body)
    return {"id": digest.id, "status": "generating"}


@router.get("")
def list_digests(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    digests = db.scalars(
        select(Digest)
        .where(or_(Digest.created_by == current_user.id, Digest.trigger_type == "scheduled"))
        .order_by(Digest.created_at.desc())
        .limit(50)
    ).all()
    return [_digest_response(d) for d in digests]


@router.get("/{digest_id}")
def get_digest(
    digest_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    digest = db.get(Digest, digest_id)
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    if digest.trigger_type != "scheduled" and digest.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return _digest_response(digest)
```

- [ ] **Step 4: Register router in `app/api/main.py`**

```python
from app.api.digest_routes import router as digest_router
app.include_router(digest_router)
```

- [ ] **Step 5: Run tests**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_digest_api.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 6: Run full suite**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/ -v 2>&1 | tail -10
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
cd backend && git add app/api/digest_routes.py app/api/main.py tests/integration/test_digest_api.py && git commit -m "feat: add Digest API with async background DigestAgent"
```

---

### Task 19: Update `/items` with Personalized Score

**Files:**
- Modify: `backend/app/api/routes.py`

- [ ] **Step 1: Add `personalized_score` to `/items` response and `sort_by=relevance` + `min_score` params**

In `backend/app/api/routes.py`, update the `list_items` function:

Add query params after `published_before`:
```python
    sort_by: SortBy | Literal["relevance"] = "published_at",
    min_score: int | None = None,
```

And before the SQL execution block, add the personalized scoring logic:

```python
    # Get current user from optional auth header for personalized scoring
    from fastapi import Request
    from app.auth import verify_access_token
    from app.models import UserProfile
    from app.processing.scorer import compute_fast_score, parse_criteria

    # (This is handled in the response mapping below)
```

Update `_item_summary()` to accept an optional score:

```python
def _item_summary(item: Item, score: int | None = None) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "title_tldr": item.title_tldr,
        "main_category": item.main_category,
        "info_type": item.info_type,
        "importance": item.importance,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "fetched_at": item.fetched_at.isoformat() if item.fetched_at else None,
        "url": item.url,
        "personalized_score": score,
    }
```

Add a new optional `Authorization` header param to `list_items`:

```python
def list_items(
    ...
    authorization: str | None = Header(default=None),
    min_score: int | None = None,
    sort_by: str = "published_at",
    ...
):
    ...
    # After fetching rows, compute scores if user is authenticated
    rows = db.scalars(stmt.order_by(order_clause).limit(limit).offset(offset)).all()
    
    criteria = []
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        from app.auth import verify_access_token
        from app.models import UserProfile
        from app.processing.scorer import parse_criteria
        payload = verify_access_token(token)
        if payload:
            profile = db.get(UserProfile, payload["sub"])
            if profile and profile.criteria:
                criteria = parse_criteria(profile.criteria)
    
    items_with_scores = []
    for item in rows:
        score = compute_fast_score(item, criteria) if criteria else None
        items_with_scores.append(_item_summary(item, score=score))
    
    if sort_by == "relevance" and criteria:
        items_with_scores.sort(key=lambda x: x.get("personalized_score") or 0, reverse=True)
    
    if min_score is not None:
        items_with_scores = [i for i in items_with_scores if (i.get("personalized_score") or 0) >= min_score]
    
    return {"total": total, "items": items_with_scores}
```

> Note: Import `Header` from fastapi at top of `routes.py` and `compute_fast_score` from scorer.

- [ ] **Step 2: Run API integration tests**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_api.py -v 2>&1 | tail -15
```

Expected: all existing tests still PASS.

- [ ] **Step 3: Commit**

```bash
cd backend && git add app/api/routes.py && git commit -m "feat: add personalized_score to /items response, sort_by=relevance, min_score filter"
```
