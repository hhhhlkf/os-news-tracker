# V2 Agent Crawl Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the `agent_crawl` source type: PlanAgent → CrawlDAG → QualityWorkerPool → SummaryWorkerPool → SiteMemory, all wired through an `AgentCrawlFetcher` that slots into the existing Fetcher Protocol. Add the `/sources/agent` REST API for managing agent sources.

**Architecture:** New `backend/app/agent/` package contains 5 modules. `AgentCrawlFetcher` in `backend/app/fetchers/agent_crawl.py` orchestrates them via `asyncio.run()`. Sync LLM calls are wrapped with `asyncio.to_thread()` for parallel execution. SiteMemory reads/writes the `agent_site_memory` DB table. Agent-crawled items bypass the Enricher and enter the pipeline with `status=agent_enriched`.

**Prerequisite:** Plan `2026-06-15-v2-db-and-user-system.md` must be complete (DB tables exist).

**Tech Stack:** Python 3.11, asyncio, Scrapling, existing `LlmClient`, SQLAlchemy 2.0

**Spec:** `docs/superpowers/specs/2026-06-15-v2-complete-design.md` §4, §9, §10, §11

---

## File Change Map

| File | Action | Notes |
|------|--------|-------|
| `backend/app/agent/__init__.py` | Create | Empty package marker |
| `backend/app/agent/schemas.py` | Create | CrawlPlan, PlanUrl, RawPage, QualifiedPage, AgentItem, AgentSourceConfig |
| `backend/app/agent/site_memory.py` | Create | SiteMemory read/write with 7-day keep TTL |
| `backend/app/agent/plan_agent.py` | Create | PlanAgent with LLM + deterministic validation |
| `backend/app/agent/crawl_dag.py` | Create | Async Semaphore-based parallel fetch |
| `backend/app/agent/quality_pool.py` | Create | Parallel LLM quality assessment |
| `backend/app/agent/summary_pool.py` | Create | Parallel LLM adaptive summarization |
| `backend/app/fetchers/agent_crawl.py` | Create | AgentCrawlFetcher (Fetcher Protocol) |
| `backend/app/api/agent_routes.py` | Create | /sources/agent CRUD + run endpoints |
| `backend/app/api/main.py` | Modify | Register agent_routes router |
| `backend/app/scheduler.py` | Modify | Wire agent_crawl fetcher in build_fetcher() |
| `backend/tests/unit/test_agent_*.py` | Create | Unit tests per module |
| `backend/tests/integration/test_agent_api.py` | Create | API integration tests |

---

### Task 8: Agent Package Schemas

**Files:**
- Create: `backend/app/agent/__init__.py`
- Create: `backend/app/agent/schemas.py`

- [ ] **Step 1: Create the package**

```bash
mkdir -p backend/app/agent && touch backend/app/agent/__init__.py
```

- [ ] **Step 2: Create `backend/app/agent/schemas.py`**

```python
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class AgentSourceConfig:
    source_id: int
    focus_areas: list[str]
    topic_groups: list[str]
    crawl_depth: int = 1
    max_urls_per_run: int = 20
    quality_threshold: int = 4
    crawl_workers: int = 5
    quality_workers: int = 3
    summary_workers: int = 3


@dataclass
class PlanUrl:
    url: str
    guessed_topic: str = ""


@dataclass
class CrawlPlan:
    source_id: int
    urls: list[PlanUrl]


@dataclass
class RawPage:
    url: str
    guessed_topic: str
    title: str
    content: str   # cleaned text


@dataclass
class QualityResult:
    score: int
    reason: str
    relevant_topic: str
    verdict: str            # keep | discard
    should_remember: bool


@dataclass
class QualifiedPage:
    page: RawPage
    verdict: str
    score: int


@dataclass
class AgentItem:
    source_id: int
    url: str
    title: str
    topic_group: str | None
    content_type: str       # article | release_note | benchmark | discussion | changelog
    importance: str         # 高 | 中 | 低
    body: str
    key_facts: list[str] = field(default_factory=list)
```

- [ ] **Step 3: Verify import**

```bash
cd backend && ENABLE_SCHEDULER=0 python -c "from app.agent.schemas import AgentSourceConfig, CrawlPlan, AgentItem; print('OK')"
```

Expected: `OK`

---

### Task 9: SiteMemory

**Files:**
- Create: `backend/app/agent/site_memory.py`
- Create: `backend/tests/unit/test_agent_site_memory.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/unit/test_agent_site_memory.py`:

```python
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.agent.site_memory import SiteMemory
from app.models import AgentSiteMemory


def _make_db():
    db = MagicMock()
    db.scalars.return_value.first.return_value = None
    return db


def test_get_returns_none_when_no_record():
    mem = SiteMemory()
    assert mem.get(db=_make_db(), source_id=1, url="https://example.com") is None


def test_get_returns_record_within_keep_ttl():
    record = AgentSiteMemory(
        source_id=1, url_pattern="https://example.com",
        verdict="keep", quality_score=8, quality_reason="good",
        last_seen_at=datetime.now(timezone.utc),
        seen_count=1,
    )
    db = _make_db()
    db.scalars.return_value.first.return_value = record
    mem = SiteMemory()
    result = mem.get(db=db, source_id=1, url="https://example.com")
    assert result is not None
    assert result.verdict == "keep"


def test_get_returns_none_for_stale_keep_record():
    record = AgentSiteMemory(
        source_id=1, url_pattern="https://example.com",
        verdict="keep", quality_score=8, quality_reason="good",
        last_seen_at=datetime.now(timezone.utc) - timedelta(days=8),
        seen_count=3,
    )
    db = _make_db()
    db.scalars.return_value.first.return_value = record
    mem = SiteMemory()
    result = mem.get(db=db, source_id=1, url="https://example.com")
    assert result is None   # stale keep → re-evaluate


def test_get_returns_discard_regardless_of_age():
    record = AgentSiteMemory(
        source_id=1, url_pattern="https://example.com",
        verdict="discard", quality_score=2, quality_reason="bad",
        last_seen_at=datetime.now(timezone.utc) - timedelta(days=100),
        seen_count=5,
    )
    db = _make_db()
    db.scalars.return_value.first.return_value = record
    mem = SiteMemory()
    result = mem.get(db=db, source_id=1, url="https://example.com")
    assert result is not None   # discard is permanent
    assert result.verdict == "discard"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_site_memory.py -v
```

Expected: ImportError.

- [ ] **Step 3: Create `backend/app/agent/site_memory.py`**

```python
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.schemas import QualityResult
from app.models import AgentSiteMemory

logger = logging.getLogger(__name__)

_KEEP_TTL_DAYS = 7


class SiteMemory:
    """Read/write interface for agent_site_memory.

    Keep records expire after 7 days (content may have changed).
    Discard records are permanent unless manually deleted by admin.
    """

    def get(self, db: Session, source_id: int, url: str) -> AgentSiteMemory | None:
        record = db.scalars(
            select(AgentSiteMemory)
            .where(AgentSiteMemory.source_id == source_id, AgentSiteMemory.url_pattern == url)
        ).first()
        if record is None:
            return None
        if record.verdict == "keep":
            age = datetime.now(timezone.utc) - record.last_seen_at.replace(tzinfo=timezone.utc)
            if age > timedelta(days=_KEEP_TTL_DAYS):
                logger.debug("site_memory: stale keep for %s, will re-evaluate", url)
                return None
        return record

    def upsert(self, db: Session, source_id: int, url: str, result: QualityResult) -> None:
        record = db.scalars(
            select(AgentSiteMemory)
            .where(AgentSiteMemory.source_id == source_id, AgentSiteMemory.url_pattern == url)
        ).first()
        if record is None:
            record = AgentSiteMemory(
                source_id=source_id,
                url_pattern=url,
                quality_score=result.score,
                quality_reason=result.reason,
                verdict=result.verdict,
                relevant_topic=result.relevant_topic,
            )
            db.add(record)
        else:
            record.quality_score = result.score
            record.quality_reason = result.reason
            record.verdict = result.verdict
            record.relevant_topic = result.relevant_topic
            record.last_seen_at = datetime.now(timezone.utc)
            record.seen_count += 1
        db.commit()
        logger.info("site_memory: upsert %s → %s (score=%s)", url, result.verdict, result.score)

    def should_skip(self, db: Session, source_id: int, url: str) -> bool:
        """Return True if PlanAgent should skip this URL (known discard with seen_count >= 2)."""
        record = db.scalars(
            select(AgentSiteMemory)
            .where(AgentSiteMemory.source_id == source_id, AgentSiteMemory.url_pattern == url)
        ).first()
        if record is None:
            return False
        return record.verdict == "discard" and record.seen_count >= 2
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_site_memory.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/agent/ tests/unit/test_agent_site_memory.py && git commit -m "feat: add agent package schemas and SiteMemory"
```

---

### Task 10: CrawlDAG

**Files:**
- Create: `backend/app/agent/crawl_dag.py`
- Create: `backend/tests/unit/test_agent_crawl_dag.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/unit/test_agent_crawl_dag.py`:

```python
import asyncio
import pytest
from app.agent.crawl_dag import CrawlDAG
from app.agent.schemas import AgentSourceConfig, CrawlPlan, PlanUrl


def _config(**kwargs):
    defaults = dict(
        source_id=1, focus_areas=[], topic_groups=[],
        crawl_workers=3, quality_workers=3, summary_workers=3,
        quality_threshold=4, crawl_depth=1, max_urls_per_run=20,
    )
    defaults.update(kwargs)
    return AgentSourceConfig(**defaults)


async def _fake_fetch(url):
    return {"title": f"Title for {url}", "content": f"Content of {url}"}


@pytest.mark.asyncio
async def test_execute_returns_raw_pages():
    plan = CrawlPlan(source_id=1, urls=[
        PlanUrl(url="https://a.com/1", guessed_topic="kernel"),
        PlanUrl(url="https://a.com/2", guessed_topic="ebpf"),
    ])
    dag = CrawlDAG(fetch_fn=_fake_fetch)
    pages = await dag.execute(plan, _config())
    assert len(pages) == 2
    urls = {p.url for p in pages}
    assert urls == {"https://a.com/1", "https://a.com/2"}


@pytest.mark.asyncio
async def test_failed_fetch_is_skipped():
    async def fail_one(url):
        if "fail" in url:
            raise RuntimeError("network error")
        return {"title": "ok", "content": "ok"}

    plan = CrawlPlan(source_id=1, urls=[
        PlanUrl(url="https://a.com/ok"),
        PlanUrl(url="https://a.com/fail"),
    ])
    dag = CrawlDAG(fetch_fn=fail_one)
    pages = await dag.execute(plan, _config())
    assert len(pages) == 1
    assert pages[0].url == "https://a.com/ok"


@pytest.mark.asyncio
async def test_respects_worker_concurrency():
    call_log = []
    active = [0]
    peak = [0]

    async def counting_fetch(url):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.01)
        active[0] -= 1
        return {"title": "t", "content": "c"}

    plan = CrawlPlan(source_id=1, urls=[PlanUrl(url=f"https://x/{i}") for i in range(10)])
    dag = CrawlDAG(fetch_fn=counting_fetch)
    await dag.execute(plan, _config(crawl_workers=3))
    assert peak[0] <= 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_crawl_dag.py -v
```

Expected: ImportError.

- [ ] **Step 3: Create `backend/app/agent/crawl_dag.py`**

```python
import asyncio
import logging
import random
from typing import Callable, Awaitable

from app.agent.schemas import AgentSourceConfig, CrawlPlan, PlanUrl, RawPage

logger = logging.getLogger(__name__)


async def _default_fetch(url: str) -> dict:
    """Default Scrapling-based fetch, wrapped in a thread so it's async-safe."""
    from app.extract.scrapling_extractor import ScraplingExtractor
    extractor = ScraplingExtractor()
    doc = await asyncio.to_thread(extractor.extract, url)
    return {"title": doc.title or "", "content": doc.clean_content or ""}


class CrawlDAG:
    """Parallel URL fetcher using asyncio Semaphore."""

    def __init__(self, fetch_fn: Callable[[str], Awaitable[dict]] | None = None):
        self._fetch = fetch_fn or _default_fetch

    async def execute(self, plan: CrawlPlan, config: AgentSourceConfig) -> list[RawPage]:
        sem = asyncio.Semaphore(config.crawl_workers)

        async def fetch_one(pu: PlanUrl) -> RawPage | None:
            async with sem:
                await asyncio.sleep(random.uniform(0.1, 0.5))
                try:
                    result = await self._fetch(pu.url)
                    return RawPage(
                        url=pu.url,
                        guessed_topic=pu.guessed_topic,
                        title=result.get("title", ""),
                        content=result.get("content", ""),
                    )
                except Exception as e:
                    logger.warning("crawl_dag: fetch failed %s: %s", pu.url, e)
                    return None

        results = await asyncio.gather(*[fetch_one(pu) for pu in plan.urls])
        pages = [r for r in results if r is not None]
        logger.info("crawl_dag: fetched %d/%d pages", len(pages), len(plan.urls))
        return pages
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_crawl_dag.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/agent/crawl_dag.py tests/unit/test_agent_crawl_dag.py && git commit -m "feat: add CrawlDAG with async semaphore parallel fetching"
```

---

### Task 11: QualityWorkerPool

**Files:**
- Create: `backend/app/agent/quality_pool.py`
- Create: `backend/tests/unit/test_agent_quality_pool.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/unit/test_agent_quality_pool.py`:

```python
"""QualityWorkerPool 单元测试 — 验证 LLM 质量评估、阈值过滤、SiteMemory 缓存命中。"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.agent.quality_pool import QualityWorkerPool
from app.agent.schemas import AgentSourceConfig, RawPage


def _make_db():
    """构造一个 mock 数据库会话，默认不返回任何 SiteMemory 记录。"""
    db = MagicMock()
    db.scalars.return_value.first.return_value = None
    return db


def _config(**kwargs):
    """构造 AgentSourceConfig，设置合理的默认值。"""
    d = dict(
        source_id=1, focus_areas=["kernel"], topic_groups=[],
        quality_workers=2, quality_threshold=4,
        crawl_workers=5, summary_workers=3,
        crawl_depth=1, max_urls_per_run=20,
    )
    d.update(kwargs)
    return AgentSourceConfig(**d)


def _page(url="https://a.com/1"):
    """构造一个简单的测试用 RawPage。"""
    return RawPage(
        url=url, guessed_topic="kernel",
        title="Title", content="Content about eBPF scheduler",
    )


def _make_llm(score=7):
    """构造一个 mock LLM，返回指定分数的 JSON 响应。"""
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "score": score,
        "reason": "relevant",
        "relevant_topic": "kernel",
        "verdict": "keep" if score >= 4 else "discard",
        "should_remember": True,
    })
    return llm


@pytest.mark.asyncio
async def test_high_score_page_kept():
    """高分页面应通过质量阈值，被保留。"""
    pool = QualityWorkerPool(llm=_make_llm(score=7))
    db = _make_db()
    results = await pool.assess_all([_page()], _config(), db=db)
    assert len(results) == 1
    assert results[0].verdict == "keep"


@pytest.mark.asyncio
async def test_low_score_below_threshold_discarded():
    """低分页面未达到阈值，应被丢弃。"""
    pool = QualityWorkerPool(llm=_make_llm(score=2))
    db = _make_db()
    results = await pool.assess_all(
        [_page()], _config(quality_threshold=4), db=db,
    )
    assert len(results) == 0   # 全被过滤


@pytest.mark.asyncio
async def test_site_memory_hit_skips_llm():
    """SiteMemory 命中时不应调用 LLM，直接使用缓存结果。"""
    from app.models import AgentSiteMemory

    # 构造一条 keep 缓存记录
    cached = AgentSiteMemory(
        source_id=1, url_pattern="https://a.com/1",
        verdict="keep", quality_score=9, quality_reason="cached",
        last_seen_at=datetime.now(timezone.utc), seen_count=1,
    )
    mock_memory = MagicMock()
    mock_memory.get.return_value = cached
    mock_llm = MagicMock()

    pool = QualityWorkerPool(llm=mock_llm, memory=mock_memory)
    db = MagicMock()
    results = await pool.assess_all(
        [_page("https://a.com/1")], _config(), db=db,
    )

    # 缓存命中 → 结果保留
    assert len(results) == 1
    # 缓存命中 → LLM 不应被调用
    mock_llm.complete.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_quality_pool.py -v
```

Expected: ImportError.

- [ ] **Step 3: Create `backend/app/agent/quality_pool.py`**

```python
"""QualityWorkerPool — 并行内容质量评估（Critic 角色）。

对 CrawlDAG 抓取的页面进行 LLM 质量打分，过滤低质量内容。
集成 SiteMemory 缓存：已知的高质量页面直接通过，已知的低质量页面直接丢弃，
避免重复调用 LLM，逐步降低 API 成本。

这是 Handoff Chain 的第③阶段。
"""

import asyncio
import json
import logging
import re

from sqlalchemy.orm import Session

from app.agent.schemas import (
    AgentSourceConfig,
    QualityResult,
    QualifiedPage,
    RawPage,
)
from app.agent.site_memory import SiteMemory

logger = logging.getLogger(__name__)

# ── 质量评估 prompt ──────────────────────────────────────────────
# 参考 Enricher 的 prompt 风格：明确的角色定位、详细的收录/排除标准、
# 具体的反垃圾规则、分维度的打分指南。
_PROMPT = """你是操作系统维护团队的技术内容质量评估员。你的任务是给抓取到的网页打分，判断它是否值得进入后续的摘要提取流程。

你只关注对 OS maintainer 有实际价值的技术内容。以下是你的评估标准和打分指南。

## 收录标准（高分特征）

以下类型的内容值得高分：
- 操作系统、内核、发行版、编译器的版本发布与重大更新
- 软件包更新、兼容性变化、ABI/API 变更公告
- 性能基准测试报告、横向对比、架构分析
- 云原生基础设施、容器运行时、文件系统、网络栈的技术进展
- AI agent / LLM 工具链、ML 推理框架的重要发布或技术路线变化
- 安全漏洞分析（跨社区/跨发行版影响）、供应链安全事件
- 上游项目的技术讨论、设计文档、RFC

## 排除标准（低分特征）

以下类型应打低分，因为它们对 OS maintainer 没有实际价值：
- 社区活动通知、线下 meetup、会议征稿、直播预告
- 招聘信息、职位发布、HR 相关
- 用户入门教程、"Hello World"、基础配置指南
- 市场营销材料、产品宣传、合作伙伴新闻
- 非技术性公告、公司财报、人事变动
- 单纯文档首页、仓库 README、SIG 介绍页、目录索引页
- 列表页、搜索结果页、标签归档页、登录页
- 反爬挑战页 / 人机验证页 / "Making sure you're not a bot"

## 反爬/空页面识别

如果页面标题或正文出现以下特征，必须打 0-1 分，verdict=discard：
- "确保您不是机器人" / "Making sure you're not a bot"
- "Anubis" / "Proof-of-Work" / "Hashcash"
- "请启用 JavaScript" / "enable JavaScript" / "browser verification"
- 正文为空、只有站点导航、只有 footer 链接
- 整个页面只有一句话或无实质技术内容

## 打分维度（0–10 整数分）

从以下四个维度综合评估，给出 0–10 的总分：

1. **相关性** — 页面内容与用户关注领域的匹配程度。
   - 直接命中（内核版本发布、发行版公告、包管理变更）→ 高
   - 间接相关（通用云原生、AI 工具链，但未涉及 OS 层面）→ 中
   - 无关（招聘、活动、营销）→ 低

2. **信息密度** — 是否包含具体的事实、数据、版本号、技术参数。
   - 有明确的版本号、CVE 编号、性能数字、代码片段 → 高
   - 有概括性技术描述但缺乏具体数据 → 中
   - 纯观点、纯介绍、无实质技术内容 → 低

3. **时效价值** — 对当前决策和行动的参考价值。
   - 刚发布的新版本、新漏洞、新工具 → 高
   - 持续性跟踪内容（如性能数据更新、路线图推进）→ 中
   - 过时信息、历史回顾、基础概念介绍 → 低

4. **可操作性** — OS maintainer 读完后能做什么。
   - 可直接指导升级/修复/适配决策 → 高
   - 提供背景知识，辅助长期判断 → 中
   - 读了和没读差别不大 → 低

## 分数区间参考

| 分数 | 含义 | 典型场景 |
|------|------|---------|
| 9–10 | 必读 | 内核大版本发布、严重安全漏洞、关键兼容性变更 |
| 7–8 | 推荐 | 重要包更新、性能报告、技术路线变化 |
| 5–6 | 可读 | 一般技术讨论、小版本更新、观点分析 |
| 3–4 | 边缘 | 通用技术新闻、与 OS 关系不大的工具链 |
| 0–2 | 噪音 | 招聘、活动、营销、反爬页、空页 |

## should_remember 规则

- 特征明显的页面（高信息密度、明确的技术主题）→ should_remember=true，让系统记住这个结论
- 内容模糊、难以归类的页面 → should_remember=false，下次可能还需要重新评估

## 输出格式

严格输出 JSON，不要多余文字：
{{"score": 7, "reason": "包含 Linux 6.12 版本发布的具体变更列表和性能数据", "relevant_topic": "Linux Kernel", "verdict": "keep", "should_remember": true}}
"""


def _extract_json(text: str) -> dict:
    """从 LLM 原始响应中提取 JSON 对象。

    支持两种格式：
    1. ```json { ... } ``` 围栏代码块
    2. 裸 JSON { ... }
    """
    # 优先匹配围栏代码块
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    # 回退：匹配裸 JSON
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        return json.loads(brace.group(0))
    raise ValueError(f"No JSON found in quality response: {text[:200]}")


def _parse_quality(text: str, threshold: int) -> QualityResult:
    """从 LLM 原始响应中提取 JSON 并解析为 QualityResult。

    安全措施：即使 LLM 返回 verdict="keep"，如果分数低于阈值，
    也会强制改为 discard。阈值是系统决策的硬约束，LLM 不能覆盖。
    """
    data = _extract_json(text)

    # 硬阈值覆盖：分数不够 → 强制 discard
    verdict = data.get("verdict", "discard")
    if data.get("score", 0) < threshold:
        verdict = "discard"

    return QualityResult(
        score=data.get("score", 0),
        reason=data.get("reason", ""),
        relevant_topic=data.get("relevant_topic", ""),
        verdict=verdict,
        should_remember=data.get("should_remember", False),
    )


class QualityWorkerPool:
    """并行内容质量评估器。

    对每页调用 LLM 进行 0–10 质量打分，低于阈值的页面被丢弃。
    内置 SiteMemory 缓存：命中缓存的页面跳过 LLM 调用，直接使用历史评估结果。

    用法：
        pool = QualityWorkerPool(llm=my_llm, memory=my_memory)
        qualified = await pool.assess_all(raw_pages, config, db=db)
    """

    def __init__(self, llm=None, memory: SiteMemory | None = None):
        from app.llm.client import LlmClient

        self._llm = llm or LlmClient()
        self._memory = memory or SiteMemory()

    async def assess_all(
        self,
        pages: list[RawPage],
        config: AgentSourceConfig,
        *,
        db: Session,
    ) -> list[QualifiedPage]:
        """并行评估所有页面质量。

        评估流程（每页独立）：
        1. 查 SiteMemory 缓存 → 命中则直接使用，跳过 LLM
        2. 缓存未命中 → 调用 LLM 打分
        3. 根据 should_remember 决定是否写入缓存
        4. 分数 < 阈值 → 丢弃，不进入结果列表
        """
        sem = asyncio.Semaphore(config.quality_workers)

        async def assess_one(page: RawPage) -> QualifiedPage | None:
            async with sem:
                # ── 1. 查 SiteMemory 缓存 ──
                cached = self._memory.get(
                    db=db, source_id=config.source_id, url=page.url,
                )
                if cached is not None:
                    if cached.verdict == "discard":
                        return None
                    # keep 缓存命中 → 直接通过，不调 LLM
                    return QualifiedPage(
                        page=page,
                        verdict=cached.verdict,
                        score=cached.quality_score or 0,
                    )

                # ── 2. 缓存未命中 → 调用 LLM 评估 ──
                prompt = _PROMPT.format(
                    focus_areas=", ".join(config.focus_areas),
                    url=page.url,
                    title=page.title,
                    content_preview=page.content[:1500],
                )
                raw = await asyncio.to_thread(self._llm.complete, prompt)
                result = _parse_quality(raw, config.quality_threshold)

                # ── 3. 写入 SiteMemory（仅限值得记住的结果）──
                if result.should_remember or result.verdict == "discard":
                    self._memory.upsert(
                        db=db, source_id=config.source_id,
                        url=page.url, result=result,
                    )

                # ── 4. 根据 verdict 决定保留或丢弃 ──
                if result.verdict == "discard":
                    logger.info(
                        "quality_pool: 丢弃 %s (score=%d)", page.url, result.score,
                    )
                    return None

                return QualifiedPage(
                    page=page, verdict=result.verdict, score=result.score,
                )

        results = await asyncio.gather(
            *[assess_one(p) for p in pages],
        )
        qualified = [r for r in results if r is not None]

        logger.info(
            "quality_pool: %d/%d 页通过质量评估", len(qualified), len(pages),
        )
        return qualified
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_quality_pool.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/agent/quality_pool.py tests/unit/test_agent_quality_pool.py && git commit -m "feat: add QualityWorkerPool with SiteMemory caching"
```

---

### Task 12: SummaryWorkerPool

**Files:**
- Create: `backend/app/agent/summary_pool.py`
- Create: `backend/tests/unit/test_agent_summary_pool.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/unit/test_agent_summary_pool.py`:

```python
import asyncio
import json
import pytest
from unittest.mock import MagicMock
from app.agent.summary_pool import SummaryWorkerPool
from app.agent.schemas import AgentSourceConfig, QualifiedPage, RawPage


def _config(topic_groups=None):
    return AgentSourceConfig(
        source_id=1, focus_areas=["kernel"], topic_groups=topic_groups or [],
        summary_workers=2, quality_workers=2, crawl_workers=3,
        quality_threshold=4, crawl_depth=1, max_urls_per_run=20,
    )


def _qpage(url="https://a.com/1"):
    page = RawPage(url=url, guessed_topic="kernel", title="Linux 6.12 Released", content="Content here")
    return QualifiedPage(page=page, verdict="keep", score=8)


def _make_llm(content_type="release_note"):
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "title": "Linux 6.12 正式发布",
        "topic_group": None,
        "content_type": content_type,
        "importance": "高",
        "body": "内核 6.12 引入 sched_ext",
        "key_facts": ["sched_ext 合入主线"],
        "source_url": "https://a.com/1",
    }, ensure_ascii=False)
    return llm


@pytest.mark.asyncio
async def test_summarize_returns_agent_item():
    pool = SummaryWorkerPool(llm=_make_llm())
    items = await pool.summarize_all([_qpage()], _config())
    assert len(items) == 1
    item = items[0]
    assert item.title == "Linux 6.12 正式发布"
    assert item.importance == "高"
    assert item.content_type == "release_note"


@pytest.mark.asyncio
async def test_topic_group_assigned_when_provided():
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "title": "t", "topic_group": "项目动态", "content_type": "article",
        "importance": "中", "body": "body", "key_facts": [], "source_url": "https://a.com/1",
    }, ensure_ascii=False)
    pool = SummaryWorkerPool(llm=llm)
    items = await pool.summarize_all([_qpage()], _config(topic_groups=["项目动态", "技术迭代"]))
    assert items[0].topic_group == "项目动态"


@pytest.mark.asyncio
async def test_no_topic_group_when_not_configured():
    pool = SummaryWorkerPool(llm=_make_llm())
    items = await pool.summarize_all([_qpage()], _config(topic_groups=[]))
    assert items[0].topic_group is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_summary_pool.py -v
```

Expected: ImportError.

- [ ] **Step 3: Create `backend/app/agent/summary_pool.py`**

```python
import asyncio
import json
import logging
import re

from app.agent.schemas import AgentItem, AgentSourceConfig, QualifiedPage

logger = logging.getLogger(__name__)

_PROMPT = """你是技术内容整理助手。阅读以下页面，提取对 OS maintainer 有价值的信息。

用户关注点：{focus_areas}
用户主题分组（从中选一个最匹配的，若无则留空）：{topic_groups}
页面 URL：{url}
页面正文：{content}

根据页面实际内容类型，选择最合适的输出格式，输出 JSON（不要多余文字）：
{{
  "title": "...",
  "topic_group": null,
  "content_type": "article",
  "importance": "中",
  "body": "...",
  "key_facts": [],
  "source_url": "{url}"
}}

content_type 选项：article | release_note | benchmark | discussion | changelog
importance 选项：高 | 中 | 低
"""


def _parse_summary(text: str, source_url: str, source_id: int) -> AgentItem:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON in summary response: {text[:100]}")
    data = json.loads(m.group(0))
    return AgentItem(
        source_id=source_id,
        url=data.get("source_url", source_url),
        title=data.get("title", ""),
        topic_group=data.get("topic_group") or None,
        content_type=data.get("content_type", "article"),
        importance=data.get("importance", "低"),
        body=data.get("body", ""),
        key_facts=data.get("key_facts", []),
    )


class SummaryWorkerPool:
    def __init__(self, llm=None):
        from app.llm.client import LlmClient
        self._llm = llm or LlmClient()

    async def summarize_all(
        self, pages: list[QualifiedPage], config: AgentSourceConfig
    ) -> list[AgentItem]:
        sem = asyncio.Semaphore(config.summary_workers)

        async def summarize_one(qp: QualifiedPage) -> AgentItem | None:
            async with sem:
                prompt = _PROMPT.format(
                    focus_areas=", ".join(config.focus_areas),
                    topic_groups=", ".join(config.topic_groups) if config.topic_groups else "无",
                    url=qp.page.url,
                    content=qp.page.content[:4000],
                )
                try:
                    raw = await asyncio.to_thread(self._llm.complete, prompt)
                    return _parse_summary(raw, source_url=qp.page.url, source_id=config.source_id)
                except Exception as e:
                    logger.warning("summary_pool: summarize failed %s: %s", qp.page.url, e)
                    return None

        results = await asyncio.gather(*[summarize_one(p) for p in pages])
        items = [r for r in results if r is not None]
        logger.info("summary_pool: summarized %d/%d pages", len(items), len(pages))
        return items
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_summary_pool.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/agent/summary_pool.py tests/unit/test_agent_summary_pool.py && git commit -m "feat: add SummaryWorkerPool with adaptive content-type output"
```

---

### Task 13: PlanAgent

**Files:**
- Create: `backend/app/agent/plan_agent.py`
- Create: `backend/tests/unit/test_agent_plan_agent.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/unit/test_agent_plan_agent.py`:

```python
import json
import pytest
from unittest.mock import MagicMock, patch
from app.agent.plan_agent import PlanAgent
from app.agent.schemas import AgentSourceConfig


def _config():
    return AgentSourceConfig(
        source_id=1, focus_areas=["kernel", "eBPF"], topic_groups=[],
        crawl_workers=5, quality_workers=3, summary_workers=3,
        quality_threshold=4, crawl_depth=1, max_urls_per_run=5,
    )


def _make_source(url="https://blog.example.com"):
    s = MagicMock()
    s.id = 1
    s.url = url
    return s


def _make_llm(urls):
    llm = MagicMock()
    llm.complete.return_value = json.dumps({"urls": [{"url": u, "guessed_topic": "kernel"} for u in urls]})
    return llm


def test_plan_filters_off_domain_urls():
    llm = _make_llm([
        "https://blog.example.com/post/1",
        "https://evil.com/phishing",           # off-domain — should be filtered
        "https://blog.example.com/post/2",
    ])
    agent = PlanAgent(llm=llm)
    db = MagicMock()
    with patch.object(agent, "_fetch_links", return_value=["https://blog.example.com/post/1", "https://blog.example.com/post/2"]):
        plan = agent.plan(_make_source(), _config(), db=db)
    assert all("evil.com" not in u.url for u in plan.urls)


def test_plan_respects_max_urls():
    urls = [f"https://blog.example.com/post/{i}" for i in range(20)]
    llm = _make_llm(urls)
    agent = PlanAgent(llm=llm)
    db = MagicMock()
    with patch.object(agent, "_fetch_links", return_value=urls):
        plan = agent.plan(_make_source(), _config(), db=db)
    assert len(plan.urls) <= 5   # max_urls_per_run=5


def test_plan_skips_known_discard_urls():
    mock_memory = MagicMock()
    mock_memory.should_skip.side_effect = lambda db, source_id, url: "discard" in url

    agent = PlanAgent(llm=_make_llm([
        "https://blog.example.com/good",
        "https://blog.example.com/discard-me",
    ]), memory=mock_memory)
    db = MagicMock()
    with patch.object(agent, "_fetch_links", return_value=["https://blog.example.com/good", "https://blog.example.com/discard-me"]):
        plan = agent.plan(_make_source(), _config(), db=db)
    assert all("discard" not in u.url for u in plan.urls)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_plan_agent.py -v
```

Expected: ImportError.

- [ ] **Step 3: Create `backend/app/agent/plan_agent.py`**

```python
import json
import logging
import re
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.agent.schemas import AgentSourceConfig, CrawlPlan, PlanUrl
from app.agent.site_memory import SiteMemory
from app.models import Source

logger = logging.getLogger(__name__)

_PROMPT = """你是一个网页内容分析助手。给定以下网页的链接列表和用户的关注点，
识别哪些链接最可能包含用户感兴趣的内容，并按优先级排序。

用户关注点：{focus_areas}
页面中发现的链接：{links_json}
已知低质量 URL pattern（跳过）：{skip_patterns}
最多返回：{max_urls} 个

只输出 JSON：{{"urls": [{{"url": "...", "guessed_topic": "..."}}]}}
"""


class PlanAgent:
    def __init__(self, llm=None, memory: SiteMemory | None = None):
        from app.llm.client import LlmClient
        self._llm = llm or LlmClient()
        self._memory = memory or SiteMemory()

    def _fetch_links(self, url: str) -> list[str]:
        from app.extract.scrapling_extractor import ScraplingExtractor
        try:
            extractor = ScraplingExtractor()
            doc = extractor.extract(url)
            # Extract all href links from the page
            # ScraplingExtractor doesn't return raw links — we use httpx fallback
            import httpx
            from bs4 import BeautifulSoup  # scrapling already depends on bs4
            resp = httpx.get(url, timeout=15, follow_redirects=True)
            soup = BeautifulSoup(resp.text, "html.parser")
            base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/"):
                    href = base + href
                if href.startswith("http"):
                    links.append(href)
            return list(dict.fromkeys(links))[:100]  # dedup, cap at 100
        except Exception as e:
            logger.warning("plan_agent: fetch_links failed for %s: %s", url, e)
            return []

    def plan(self, source: Source, config: AgentSourceConfig, *, db: Session) -> CrawlPlan:
        root_url = source.url
        base_domain = urlparse(root_url).netloc

        links = self._fetch_links(root_url)
        if not links:
            logger.warning("plan_agent: no links found for %s, returning empty plan", root_url)
            return CrawlPlan(source_id=config.source_id, urls=[])

        # Filter known discard URLs before sending to LLM
        skip_patterns = [
            link for link in links
            if self._memory.should_skip(db=db, source_id=config.source_id, url=link)
        ]
        candidate_links = [l for l in links if l not in skip_patterns]

        prompt = _PROMPT.format(
            focus_areas=", ".join(config.focus_areas),
            links_json=json.dumps(candidate_links[:50]),
            skip_patterns=json.dumps(skip_patterns[:10]),
            max_urls=config.max_urls_per_run,
        )
        raw = self._llm.complete(prompt)

        # Deterministic validation
        try:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {"urls": []}
            raw_urls = data.get("urls", [])
        except Exception:
            raw_urls = []

        validated: list[PlanUrl] = []
        for entry in raw_urls:
            url = entry.get("url", "")
            # Must be same domain
            if urlparse(url).netloc != base_domain:
                continue
            # Must be valid http/https
            if not url.startswith("http"):
                continue
            # Must not be in skip list
            if self._memory.should_skip(db=db, source_id=config.source_id, url=url):
                continue
            validated.append(PlanUrl(url=url, guessed_topic=entry.get("guessed_topic", "")))
            if len(validated) >= config.max_urls_per_run:
                break

        logger.info("plan_agent: planned %d URLs for source %d", len(validated), config.source_id)
        return CrawlPlan(source_id=config.source_id, urls=validated)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_plan_agent.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/agent/plan_agent.py tests/unit/test_agent_plan_agent.py && git commit -m "feat: add PlanAgent with deterministic URL validation and SiteMemory"
```

---

### Task 14: AgentCrawlFetcher + API Routes

**Files:**
- Create: `backend/app/fetchers/agent_crawl.py`
- Create: `backend/app/api/agent_routes.py`
- Modify: `backend/app/api/main.py`
- Modify: `backend/app/scheduler.py`

- [ ] **Step 1: Create `backend/app/fetchers/agent_crawl.py`**

```python
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agent.crawl_dag import CrawlDAG
from app.agent.plan_agent import PlanAgent
from app.agent.quality_pool import QualityWorkerPool
from app.agent.schemas import AgentItem, AgentSourceConfig
from app.agent.site_memory import SiteMemory
from app.agent.summary_pool import SummaryWorkerPool
from app.enums import ItemStatus
from app.models import AgentCrawlRun, AgentSourceConfig as AgentSourceConfigModel, Source
from app.schemas import RawItem

logger = logging.getLogger(__name__)


class AgentCrawlFetcher:
    """Fetcher Protocol implementation for agent_crawl source type."""

    def __init__(self, db: Session):
        self._db = db
        self._memory = SiteMemory()
        self._plan_agent = PlanAgent(memory=self._memory)
        self._crawl_dag = CrawlDAG()
        self._quality_pool = QualityWorkerPool(memory=self._memory)
        self._summary_pool = SummaryWorkerPool()

    def fetch(self, source: Source) -> list[RawItem]:
        config_model: AgentSourceConfigModel | None = self._db.get(AgentSourceConfigModel, source.id)
        if config_model is None:
            logger.warning("agent_crawl: no config for source %d, skipping", source.id)
            return []

        config = AgentSourceConfig(
            source_id=source.id,
            focus_areas=config_model.focus_areas or [],
            topic_groups=config_model.topic_groups or [],
            crawl_depth=config_model.crawl_depth,
            max_urls_per_run=config_model.max_urls_per_run,
            quality_threshold=config_model.quality_threshold,
            crawl_workers=config_model.crawl_workers,
            quality_workers=config_model.quality_workers,
            summary_workers=config_model.summary_workers,
        )

        run = AgentCrawlRun(source_id=source.id)
        self._db.add(run)
        self._db.flush()

        async def _pipeline() -> list[AgentItem]:
            plan = self._plan_agent.plan(source, config, db=self._db)
            run.plan_urls_count = len(plan.urls)
            self._db.commit()
            pages = await self._crawl_dag.execute(plan, config)
            run.fetched_count = len(pages)
            self._db.commit()
            qualified = await self._quality_pool.assess_all(pages, config, db=self._db)
            run.quality_passed = len(qualified)
            self._db.commit()
            return await self._summary_pool.summarize_all(qualified, config)

        try:
            agent_items = asyncio.run(_pipeline())
        except Exception as e:
            run.status = "failed"
            run.error_message = str(e)
            run.completed_at = datetime.now(timezone.utc)
            self._db.commit()
            logger.exception("agent_crawl: pipeline failed for source %d", source.id)
            return []

        raw_items = [_to_raw_item(item) for item in agent_items]
        run.items_created = len(raw_items)
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        self._db.commit()
        return raw_items


def _to_raw_item(item: AgentItem) -> RawItem:
    """Convert AgentItem to RawItem for the existing deduplication pipeline."""
    from app.schemas import RawItem
    # Encode content_type in key_points prefix: first element is "__type:release_note"
    key_points_prefix = [f"__type:{item.content_type}"] + item.key_facts
    return RawItem(
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        raw_content=item.body,
        published_at=None,
        # Pass metadata via extra field — stored as item status override
        extra={"agent_item": True, "main_category": item.topic_group or "agent_crawl",
               "importance": item.importance, "key_points": key_points_prefix},
    )
```

- [ ] **Step 2: Update RawItem schema to accept `extra` field**

In `backend/app/schemas.py`, in the `RawItem` model, add:

```python
extra: dict | None = None
```

- [ ] **Step 3: Wire agent_crawl in `app/scheduler.py`**

In `backend/app/scheduler.py`, find the `build_fetcher()` function and add a branch for `agent_crawl`:

```python
from app.enums import SourceType

def build_fetcher(source: Source, extractor, search, db=None):
    if source.type == SourceType.AGENT_CRAWL:
        from app.fetchers.agent_crawl import AgentCrawlFetcher
        return AgentCrawlFetcher(db=db)
    # ... existing branches follow
```

- [ ] **Step 4: Create `backend/app/api/agent_routes.py`**

```python
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models import (
    AgentCrawlRun, AgentSiteMemory, AgentSourceConfig,
    Source, User,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sources/agent", tags=["agent-sources"])


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


def _source_response(source: Source, config: AgentSourceConfig | None) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "url": source.url,
        "enabled": source.enabled,
        "config": {
            "focus_areas": config.focus_areas if config else [],
            "topic_groups": config.topic_groups if config else [],
            "crawl_depth": config.crawl_depth if config else 1,
            "max_urls_per_run": config.max_urls_per_run if config else 20,
            "quality_threshold": config.quality_threshold if config else 4,
            "crawl_workers": config.crawl_workers if config else 5,
            "quality_workers": config.quality_workers if config else 3,
            "summary_workers": config.summary_workers if config else 3,
        } if config else None,
    }


@router.get("")
def list_agent_sources(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sources = db.scalars(select(Source).where(Source.type == "agent_crawl")).all()
    result = []
    for s in sources:
        cfg = db.get(AgentSourceConfig, s.id)
        result.append(_source_response(s, cfg))
    return result


@router.post("", status_code=201)
def create_agent_source(
    body: AgentSourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    source = Source(
        name=body.name,
        type="agent_crawl",
        url=body.root_url,
        stream="news",
        enabled=True,
    )
    db.add(source)
    db.flush()
    config = AgentSourceConfig(
        source_id=source.id,
        focus_areas=body.focus_areas,
        topic_groups=body.topic_groups,
        crawl_depth=body.crawl_depth,
        max_urls_per_run=body.max_urls_per_run,
        quality_threshold=body.quality_threshold,
        crawl_workers=body.crawl_workers,
        quality_workers=body.quality_workers,
        summary_workers=body.summary_workers,
    )
    db.add(config)
    db.commit()
    return _source_response(source, config)


@router.put("/{source_id}")
def update_agent_source(
    source_id: int,
    body: AgentSourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    source = db.get(Source, source_id)
    if source is None or source.type != "agent_crawl":
        raise HTTPException(status_code=404, detail="Agent source not found")
    source.name = body.name
    source.url = body.root_url
    config = db.get(AgentSourceConfig, source_id)
    if config is None:
        config = AgentSourceConfig(source_id=source_id)
        db.add(config)
    config.focus_areas = body.focus_areas
    config.topic_groups = body.topic_groups
    config.crawl_depth = body.crawl_depth
    config.max_urls_per_run = body.max_urls_per_run
    config.quality_threshold = body.quality_threshold
    config.crawl_workers = body.crawl_workers
    config.quality_workers = body.quality_workers
    config.summary_workers = body.summary_workers
    db.commit()
    return _source_response(source, config)


@router.delete("/{source_id}", status_code=204)
def delete_agent_source(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    source = db.get(Source, source_id)
    if source is None or source.type != "agent_crawl":
        raise HTTPException(status_code=404, detail="Agent source not found")
    db.delete(source)
    db.commit()


@router.get("/{source_id}/runs")
def list_runs(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    runs = db.scalars(
        select(AgentCrawlRun)
        .where(AgentCrawlRun.source_id == source_id)
        .order_by(AgentCrawlRun.started_at.desc())
        .limit(20)
    ).all()
    return [{"id": r.id, "status": r.status, "plan_urls_count": r.plan_urls_count,
             "fetched_count": r.fetched_count, "quality_passed": r.quality_passed,
             "items_created": r.items_created, "started_at": r.started_at.isoformat() if r.started_at else None,
             "completed_at": r.completed_at.isoformat() if r.completed_at else None,
             "error_message": r.error_message} for r in runs]


@router.get("/{source_id}/memory")
def view_memory(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    records = db.scalars(
        select(AgentSiteMemory)
        .where(AgentSiteMemory.source_id == source_id)
        .order_by(AgentSiteMemory.last_seen_at.desc())
        .limit(100)
    ).all()
    return [{"url_pattern": r.url_pattern, "verdict": r.verdict, "score": r.quality_score,
             "reason": r.quality_reason, "seen_count": r.seen_count,
             "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None} for r in records]


@router.delete("/{source_id}/memory/{url_pattern:path}", status_code=204)
def delete_memory_record(
    source_id: int,
    url_pattern: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    record = db.scalars(
        select(AgentSiteMemory)
        .where(AgentSiteMemory.source_id == source_id, AgentSiteMemory.url_pattern == url_pattern)
    ).first()
    if record:
        db.delete(record)
        db.commit()
```

- [ ] **Step 5: Register agent_routes in `app/api/main.py`**

```python
from app.api.agent_routes import router as agent_router
app.include_router(agent_router)
```

- [ ] **Step 6: Run full test suite**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
cd backend && git add app/fetchers/agent_crawl.py app/agent/plan_agent.py app/api/agent_routes.py app/api/main.py app/scheduler.py app/schemas.py && git commit -m "feat: add AgentCrawlFetcher and /sources/agent API"
```
