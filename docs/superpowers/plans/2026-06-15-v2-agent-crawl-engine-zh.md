# V2 Agent 爬取引擎 — 实施计划

> **面向实施 Agent 的说明：** 必须使用子技能：superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 来逐任务实施此计划。步骤使用 checkbox（`- [ ]`）语法进行跟踪。

**目标：** 实现 `agent_crawl` 来源类型：PlanAgent → CrawlDAG → QualityWorkerPool → SummaryWorkerPool → SiteMemory，全部通过 `AgentCrawlFetcher` 串联，该 Fetcher 对接现有的 Fetcher 协议。新增 `/sources/agent` REST API 用于管理 Agent 来源。

**架构：** 新增 `backend/app/agent/` 包，包含 5 个模块。`backend/app/fetchers/agent_crawl.py` 中的 `AgentCrawlFetcher` 通过 `asyncio.run()` 编排它们。同步 LLM 调用通过 `asyncio.to_thread()` 包装以实现并行执行。SiteMemory 读写 `agent_site_memory` 数据库表。Agent 爬取的内容绕过 Enricher，以 `status=agent_enriched` 进入管道。

**前置条件：** 计划 `2026-06-15-v2-db-and-user-system.md` 必须已完成（数据库表已存在）。

**技术栈：** Python 3.11, asyncio, Scrapling, 现有 `LlmClient`, SQLAlchemy 2.0

**设计文档：** `docs/superpowers/specs/2026-06-15-v2-complete-design.md` §4, §9, §10, §11

---

## 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/app/agent/__init__.py` | 新建 | 空包标记 |
| `backend/app/agent/schemas.py` | 新建 | CrawlPlan, PlanUrl, RawPage, QualifiedPage, AgentItem, AgentSourceConfig |
| `backend/app/agent/site_memory.py` | 新建 | SiteMemory 读写，7 天保留 TTL |
| `backend/app/agent/plan_agent.py` | 新建 | PlanAgent：LLM 规划 + 确定性校验 |
| `backend/app/agent/crawl_dag.py` | 新建 | 基于异步 Semaphore 的并行抓取 |
| `backend/app/agent/quality_pool.py` | 新建 | 并行 LLM 质量评估 |
| `backend/app/agent/summary_pool.py` | 新建 | 并行 LLM 自适应摘要生成 |
| `backend/app/fetchers/agent_crawl.py` | 新建 | AgentCrawlFetcher（Fetcher 协议实现） |
| `backend/app/api/agent_routes.py` | 新建 | /sources/agent CRUD + 运行端点 |
| `backend/app/api/main.py` | 修改 | 注册 agent_routes 路由 |
| `backend/app/scheduler.py` | 修改 | 在 build_fetcher() 中接入 agent_crawl |
| `backend/tests/unit/test_agent_*.py` | 新建 | 各模块单元测试 |
| `backend/tests/integration/test_agent_api.py` | 新建 | API 集成测试 |

---

### 任务 8：Agent 包数据模型

**文件：**
- 新建：`backend/app/agent/__init__.py`
- 新建：`backend/app/agent/schemas.py`

- [ ] **步骤 1：创建包目录**

```bash
mkdir -p backend/app/agent && touch backend/app/agent/__init__.py
```

- [ ] **步骤 2：创建 `backend/app/agent/schemas.py`**

```python
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class AgentSourceConfig:
    """Agent 来源的运行配置"""
    source_id: int
    focus_areas: list[str]          # 关注领域
    topic_groups: list[str]         # 主题分组
    crawl_depth: int = 1            # 爬取深度
    max_urls_per_run: int = 20      # 每次最多爬取 URL 数
    quality_threshold: int = 4      # 质量门槛（0-10，低于此分丢弃）
    crawl_workers: int = 5          # 爬取并发数
    quality_workers: int = 3        # 评估并发数
    summary_workers: int = 3        # 摘要并发数


@dataclass
class PlanUrl:
    """PlanAgent 规划的单个 URL"""
    url: str
    guessed_topic: str = ""         # AI 推测的主题


@dataclass
class CrawlPlan:
    """PlanAgent 输出的抓取计划"""
    source_id: int
    urls: list[PlanUrl]


@dataclass
class RawPage:
    """CrawlDAG 抓取的原始页面"""
    url: str
    guessed_topic: str
    title: str
    content: str                    # 清洗后的正文文本


@dataclass
class QualityResult:
    """QualityWorkerPool 的质量评估结果"""
    score: int
    reason: str
    relevant_topic: str
    verdict: str                    # keep | discard
    should_remember: bool           # 是否写入 SiteMemory


@dataclass
class QualifiedPage:
    """通过质量筛选的页面"""
    page: RawPage
    verdict: str
    score: int


@dataclass
class AgentItem:
    """SummaryWorkerPool 输出的最终条目"""
    source_id: int
    url: str
    title: str
    topic_group: str | None
    content_type: str               # article | release_note | benchmark | discussion | changelog
    importance: str                 # 高 | 中 | 低
    body: str
    key_facts: list[str] = field(default_factory=list)
```

- [ ] **步骤 3：验证导入**

```bash
cd backend && ENABLE_SCHEDULER=0 python -c "from app.agent.schemas import AgentSourceConfig, CrawlPlan, AgentItem; print('OK')"
```

预期输出：`OK`

---

### 任务 9：SiteMemory（站点记忆）

**文件：**
- 新建：`backend/app/agent/site_memory.py`
- 新建：`backend/tests/unit/test_agent_site_memory.py`

- [ ] **步骤 1：编写失败测试**

创建 `backend/tests/unit/test_agent_site_memory.py`：

```python
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.agent.site_memory import SiteMemory
from app.models import AgentSiteMemory


def _make_db():
    """构造 mock 数据库会话"""
    db = MagicMock()
    db.scalars.return_value.first.return_value = None
    return db


def test_get_returns_none_when_no_record():
    """无记录时返回 None"""
    mem = SiteMemory()
    assert mem.get(db=_make_db(), source_id=1, url="https://example.com") is None


def test_get_returns_record_within_keep_ttl():
    """TTL 内的 keep 记录正常返回"""
    record = AgentSiteMemory(
        source_id=1, url_pattern="https://example.com",
        verdict="keep", quality_score=8, quality_reason="优质内容",
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
    """过期的 keep 记录返回 None，允许重新评估"""
    record = AgentSiteMemory(
        source_id=1, url_pattern="https://example.com",
        verdict="keep", quality_score=8, quality_reason="优质内容",
        last_seen_at=datetime.now(timezone.utc) - timedelta(days=8),
        seen_count=3,
    )
    db = _make_db()
    db.scalars.return_value.first.return_value = record
    mem = SiteMemory()
    result = mem.get(db=db, source_id=1, url="https://example.com")
    assert result is None   # 过期 keep → 重新评估


def test_get_returns_discard_regardless_of_age():
    """discard 记录永久保留，不受 TTL 影响"""
    record = AgentSiteMemory(
        source_id=1, url_pattern="https://example.com",
        verdict="discard", quality_score=2, quality_reason="低质量",
        last_seen_at=datetime.now(timezone.utc) - timedelta(days=100),
        seen_count=5,
    )
    db = _make_db()
    db.scalars.return_value.first.return_value = record
    mem = SiteMemory()
    result = mem.get(db=db, source_id=1, url="https://example.com")
    assert result is not None   # discard 永久有效
    assert result.verdict == "discard"
```

- [ ] **步骤 2：运行测试验证失败**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_site_memory.py -v
```

预期：ImportError（模块尚未创建）

- [ ] **步骤 3：创建 `backend/app/agent/site_memory.py`**

```python
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.schemas import QualityResult
from app.models import AgentSiteMemory

logger = logging.getLogger(__name__)

_KEEP_TTL_DAYS = 7     # keep 记录保留天数


class SiteMemory:
    """agent_site_memory 表的读写接口。

    keep 记录 7 天后过期（内容可能已变更）。
    discard 记录永久保留，除非管理员手动删除。
    """

    def get(self, db: Session, source_id: int, url: str) -> AgentSiteMemory | None:
        """查询 URL 的记忆记录。过期 keep 返回 None，discard 永久有效。"""
        record = db.scalars(
            select(AgentSiteMemory)
            .where(AgentSiteMemory.source_id == source_id, AgentSiteMemory.url_pattern == url)
        ).first()
        if record is None:
            return None
        if record.verdict == "keep":
            age = datetime.now(timezone.utc) - record.last_seen_at.replace(tzinfo=timezone.utc)
            if age > timedelta(days=_KEEP_TTL_DAYS):
                logger.debug("site_memory: %s 的 keep 记录已过期，将重新评估", url)
                return None
        return record

    def upsert(self, db: Session, source_id: int, url: str, result: QualityResult) -> None:
        """插入或更新记忆记录"""
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
        logger.info("site_memory: upsert %s → %s (分数=%s)", url, result.verdict, result.score)

    def should_skip(self, db: Session, source_id: int, url: str) -> bool:
        """判断 PlanAgent 是否应跳过此 URL（已被标记 discard 且 seen_count >= 2）"""
        record = db.scalars(
            select(AgentSiteMemory)
            .where(AgentSiteMemory.source_id == source_id, AgentSiteMemory.url_pattern == url)
        ).first()
        if record is None:
            return False
        return record.verdict == "discard" and record.seen_count >= 2
```

- [ ] **步骤 4：运行测试验证通过**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_site_memory.py -v
```

预期：4 个测试全部 PASS。

- [ ] **步骤 5：提交**

```bash
cd backend && git add app/agent/ tests/unit/test_agent_site_memory.py && git commit -m "feat: 新增 agent 包数据模型和 SiteMemory"
```

---

### 任务 10：CrawlDAG（并行抓取）

**文件：**
- 新建：`backend/app/agent/crawl_dag.py`
- 新建：`backend/tests/unit/test_agent_crawl_dag.py`

- [ ] **步骤 1：编写失败测试**

创建 `backend/tests/unit/test_agent_crawl_dag.py`：

```python
import asyncio
import pytest
from app.agent.crawl_dag import CrawlDAG
from app.agent.schemas import AgentSourceConfig, CrawlPlan, PlanUrl


def _config(**kwargs):
    """构造默认配置，可覆盖指定字段"""
    defaults = dict(
        source_id=1, focus_areas=[], topic_groups=[],
        crawl_workers=3, quality_workers=3, summary_workers=3,
        quality_threshold=4, crawl_depth=1, max_urls_per_run=20,
    )
    defaults.update(kwargs)
    return AgentSourceConfig(**defaults)


async def _fake_fetch(url):
    """伪造的抓取函数"""
    return {"title": f"Title for {url}", "content": f"Content of {url}"}


@pytest.mark.asyncio
async def test_execute_returns_raw_pages():
    """正常抓取返回 RawPage 列表"""
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
    """失败的抓取被跳过，不影响其他页面"""
    async def fail_one(url):
        if "fail" in url:
            raise RuntimeError("网络错误")
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
    """并发数受 crawl_workers 限制"""
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

- [ ] **步骤 2：运行测试验证失败**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_crawl_dag.py -v
```

预期：ImportError

- [ ] **步骤 3：创建 `backend/app/agent/crawl_dag.py`**

```python
import asyncio
import logging
import random
from typing import Callable, Awaitable

from app.agent.schemas import AgentSourceConfig, CrawlPlan, PlanUrl, RawPage

logger = logging.getLogger(__name__)


async def _default_fetch(url: str) -> dict:
    """默认抓取函数：基于 Scrapling，通过线程池执行以保证异步安全"""
    from app.extract.scrapling_extractor import ScraplingExtractor
    extractor = ScraplingExtractor()
    doc = await asyncio.to_thread(extractor.extract, url)
    return {"title": doc.title or "", "content": doc.clean_content or ""}


class CrawlDAG:
    """基于 asyncio Semaphore 的并行 URL 抓取器。

    所有待抓取 URL 之间无依赖关系，可完全并行。
    纯确定性执行——不调用 LLM。
    """

    def __init__(self, fetch_fn: Callable[[str], Awaitable[dict]] | None = None):
        self._fetch = fetch_fn or _default_fetch

    async def execute(self, plan: CrawlPlan, config: AgentSourceConfig) -> list[RawPage]:
        sem = asyncio.Semaphore(config.crawl_workers)

        async def fetch_one(pu: PlanUrl) -> RawPage | None:
            async with sem:
                await asyncio.sleep(random.uniform(0.1, 0.5))  # 随机延迟防反爬
                try:
                    result = await self._fetch(pu.url)
                    return RawPage(
                        url=pu.url,
                        guessed_topic=pu.guessed_topic,
                        title=result.get("title", ""),
                        content=result.get("content", ""),
                    )
                except Exception as e:
                    logger.warning("crawl_dag: 抓取失败 %s: %s", pu.url, e)
                    return None

        results = await asyncio.gather(*[fetch_one(pu) for pu in plan.urls])
        pages = [r for r in results if r is not None]
        logger.info("crawl_dag: 抓取完成 %d/%d 页", len(pages), len(plan.urls))
        return pages
```

- [ ] **步骤 4：运行测试验证通过**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_crawl_dag.py -v
```

预期：3 个测试 PASS。

- [ ] **步骤 5：提交**

```bash
cd backend && git add app/agent/crawl_dag.py tests/unit/test_agent_crawl_dag.py && git commit -m "feat: 新增 CrawlDAG 异步 Semaphore 并行抓取"
```

---

### 任务 11：QualityWorkerPool（质量评估）

**文件：**
- 新建：`backend/app/agent/quality_pool.py`
- 新建：`backend/tests/unit/test_agent_quality_pool.py`

- [ ] **步骤 1：编写失败测试**

创建 `backend/tests/unit/test_agent_quality_pool.py`：

```python
import asyncio
import json
import pytest
from unittest.mock import MagicMock, AsyncMock
from app.agent.quality_pool import QualityWorkerPool
from app.agent.schemas import AgentSourceConfig, RawPage


def _config(**kwargs):
    d = dict(source_id=1, focus_areas=["kernel"], topic_groups=[], quality_workers=2,
             quality_threshold=4, crawl_workers=5, summary_workers=3, crawl_depth=1, max_urls_per_run=20)
    d.update(kwargs)
    return AgentSourceConfig(**d)


def _page(url="https://a.com/1"):
    return RawPage(url=url, guessed_topic="kernel", title="标题", content="关于 eBPF 调度器的内容")


def _make_llm(score=7):
    """构造 mock LLM，返回指定分数"""
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "score": score, "reason": "相关", "relevant_topic": "kernel",
        "verdict": "keep" if score >= 4 else "discard", "should_remember": True,
    })
    return llm


@pytest.mark.asyncio
async def test_high_score_page_kept():
    """高分页面被保留"""
    pool = QualityWorkerPool(llm=_make_llm(score=7))
    db = MagicMock()
    results = await pool.assess_all([_page()], _config(), db=db)
    assert len(results) == 1
    assert results[0].verdict == "keep"


@pytest.mark.asyncio
async def test_low_score_below_threshold_discarded():
    """低于门槛的页面被丢弃"""
    pool = QualityWorkerPool(llm=_make_llm(score=2))
    db = MagicMock()
    results = await pool.assess_all([_page()], _config(quality_threshold=4), db=db)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_site_memory_hit_skips_llm():
    """命中 SiteMemory 缓存时跳过 LLM 调用"""
    from app.models import AgentSiteMemory
    from datetime import datetime, timezone
    cached = AgentSiteMemory(
        source_id=1, url_pattern="https://a.com/1",
        verdict="keep", quality_score=9, quality_reason="已缓存",
        last_seen_at=datetime.now(timezone.utc), seen_count=1,
    )
    mock_memory = MagicMock()
    mock_memory.get.return_value = cached
    mock_llm = MagicMock()

    pool = QualityWorkerPool(llm=mock_llm, memory=mock_memory)
    db = MagicMock()
    results = await pool.assess_all([_page("https://a.com/1")], _config(), db=db)
    assert len(results) == 1
    mock_llm.complete.assert_not_called()  # 缓存命中，未调用 LLM
```

- [ ] **步骤 2：运行测试验证失败**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_quality_pool.py -v
```

预期：ImportError

- [ ] **步骤 3：创建 `backend/app/agent/quality_pool.py`**

```python
import asyncio
import json
import logging
import re

from sqlalchemy.orm import Session

from app.agent.schemas import AgentSourceConfig, QualityResult, QualifiedPage, RawPage
from app.agent.site_memory import SiteMemory

logger = logging.getLogger(__name__)

_PROMPT = """你是内容质量评估员。评估以下页面内容对用户的价值。

用户关注点：{focus_areas}
页面 URL：{url}
页面标题：{title}
页面正文（前1500字）：{content_preview}

评估维度：
1. 与用户关注点的相关性（0-5）
2. 信息密度（是否包含具体的事实/数据/版本号/技术细节，0-5）

输出 JSON（不要多余文字）：
{{"score": 7, "reason": "...", "relevant_topic": "...", "verdict": "keep", "should_remember": true}}
"""


def _parse_quality(text: str, threshold: int) -> QualityResult:
    """从 LLM 响应中解析质量评估结果"""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"质量评估响应中未找到 JSON：{text[:100]}")
    data = json.loads(m.group(0))
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
    """并行 LLM 质量评估器。

    每个页面独立评估，可完全并行。
    命中 SiteMemory 缓存的页面跳过 LLM 调用，节省费用。
    """

    def __init__(self, llm=None, memory: SiteMemory | None = None):
        from app.llm.client import LlmClient
        self._llm = llm or LlmClient()
        self._memory = memory or SiteMemory()

    async def assess_all(
        self, pages: list[RawPage], config: AgentSourceConfig, *, db: Session
    ) -> list[QualifiedPage]:
        sem = asyncio.Semaphore(config.quality_workers)

        async def assess_one(page: RawPage) -> QualifiedPage | None:
            async with sem:
                # 先查缓存
                cached = self._memory.get(db=db, source_id=config.source_id, url=page.url)
                if cached is not None:
                    if cached.verdict == "discard":
                        return None
                    return QualifiedPage(page=page, verdict=cached.verdict, score=cached.quality_score or 0)

                # 缓存未命中 → 调用 LLM 评估
                prompt = _PROMPT.format(
                    focus_areas=", ".join(config.focus_areas),
                    url=page.url,
                    title=page.title,
                    content_preview=page.content[:1500],
                )
                raw = await asyncio.to_thread(self._llm.complete, prompt)
                result = _parse_quality(raw, config.quality_threshold)
                if result.should_remember or result.verdict == "discard":
                    self._memory.upsert(db=db, source_id=config.source_id, url=page.url, result=result)
                if result.verdict == "discard":
                    logger.info("quality_pool: 丢弃 %s (分数=%d)", page.url, result.score)
                    return None
                return QualifiedPage(page=page, verdict=result.verdict, score=result.score)

        results = await asyncio.gather(*[assess_one(p) for p in pages])
        qualified = [r for r in results if r is not None]
        logger.info("quality_pool: %d/%d 页通过质量筛选", len(qualified), len(pages))
        return qualified
```

- [ ] **步骤 4：运行测试验证通过**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_quality_pool.py -v
```

预期：3 个测试 PASS。

- [ ] **步骤 5：提交**

```bash
cd backend && git add app/agent/quality_pool.py tests/unit/test_agent_quality_pool.py && git commit -m "feat: 新增 QualityWorkerPool 含 SiteMemory 缓存"
```

---

### 任务 12：SummaryWorkerPool（摘要生成）

**文件：**
- 新建：`backend/app/agent/summary_pool.py`
- 新建：`backend/tests/unit/test_agent_summary_pool.py`

- [ ] **步骤 1：编写失败测试**

创建 `backend/tests/unit/test_agent_summary_pool.py`：

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
    page = RawPage(url=url, guessed_topic="kernel", title="Linux 6.12 发布", content="内容正文")
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
    """摘要生成返回 AgentItem"""
    pool = SummaryWorkerPool(llm=_make_llm())
    items = await pool.summarize_all([_qpage()], _config())
    assert len(items) == 1
    item = items[0]
    assert item.title == "Linux 6.12 正式发布"
    assert item.importance == "高"
    assert item.content_type == "release_note"


@pytest.mark.asyncio
async def test_topic_group_assigned_when_provided():
    """配置了主题分组时正确分配"""
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "title": "t", "topic_group": "项目动态", "content_type": "article",
        "importance": "中", "body": "正文", "key_facts": [], "source_url": "https://a.com/1",
    }, ensure_ascii=False)
    pool = SummaryWorkerPool(llm=llm)
    items = await pool.summarize_all([_qpage()], _config(topic_groups=["项目动态", "技术迭代"]))
    assert items[0].topic_group == "项目动态"


@pytest.mark.asyncio
async def test_no_topic_group_when_not_configured():
    """未配置主题分组时 topic_group 为 None"""
    pool = SummaryWorkerPool(llm=_make_llm())
    items = await pool.summarize_all([_qpage()], _config(topic_groups=[]))
    assert items[0].topic_group is None
```

- [ ] **步骤 2：运行测试验证失败**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_summary_pool.py -v
```

预期：ImportError

- [ ] **步骤 3：创建 `backend/app/agent/summary_pool.py`**

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
    """从 LLM 响应中解析摘要"""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"摘要响应中未找到 JSON：{text[:100]}")
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
    """并行 LLM 摘要生成器。

    每页独立摘要，根据内容类型自适应调整输出格式。
    支持按用户配置的主题分组归类。
    """

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
                    logger.warning("summary_pool: 摘要生成失败 %s: %s", qp.page.url, e)
                    return None

        results = await asyncio.gather(*[summarize_one(p) for p in pages])
        items = [r for r in results if r is not None]
        logger.info("summary_pool: 已生成摘要 %d/%d 页", len(items), len(pages))
        return items
```

- [ ] **步骤 4：运行测试验证通过**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_summary_pool.py -v
```

预期：3 个测试 PASS。

- [ ] **步骤 5：提交**

```bash
cd backend && git add app/agent/summary_pool.py tests/unit/test_agent_summary_pool.py && git commit -m "feat: 新增 SummaryWorkerPool 自适应内容类型输出"
```

---

### 任务 13：PlanAgent（爬取规划）

**文件：**
- 新建：`backend/app/agent/plan_agent.py`
- 新建：`backend/tests/unit/test_agent_plan_agent.py`

- [ ] **步骤 1：编写失败测试**

创建 `backend/tests/unit/test_agent_plan_agent.py`：

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
    """过滤非同域 URL，防止爬到其他网站"""
    llm = _make_llm([
        "https://blog.example.com/post/1",
        "https://evil.com/phishing",           # 非同域 — 应被过滤
        "https://blog.example.com/post/2",
    ])
    agent = PlanAgent(llm=llm)
    db = MagicMock()
    with patch.object(agent, "_fetch_links", return_value=["https://blog.example.com/post/1", "https://blog.example.com/post/2"]):
        plan = agent.plan(_make_source(), _config(), db=db)
    assert all("evil.com" not in u.url for u in plan.urls)


def test_plan_respects_max_urls():
    """计划 URL 数不超过 max_urls_per_run 限制"""
    urls = [f"https://blog.example.com/post/{i}" for i in range(20)]
    llm = _make_llm(urls)
    agent = PlanAgent(llm=llm)
    db = MagicMock()
    with patch.object(agent, "_fetch_links", return_value=urls):
        plan = agent.plan(_make_source(), _config(), db=db)
    assert len(plan.urls) <= 5   # max_urls_per_run=5


def test_plan_skips_known_discard_urls():
    """跳过 SiteMemory 中已知的低质量 URL"""
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

- [ ] **步骤 2：运行测试验证失败**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_plan_agent.py -v
```

预期：ImportError

- [ ] **步骤 3：创建 `backend/app/agent/plan_agent.py`**

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
    """基于 LLM 的爬取规划器。

    输入：来源网站 URL + 用户关注配置
    输出：优先级排序的 URL 列表（CrawlPlan）

    关键安全措施：
    - 确定性校验：只允许同域 URL
    - SiteMemory 联动：跳过已知低质量 URL
    - 数量上限：不超 max_urls_per_run
    """

    def __init__(self, llm=None, memory: SiteMemory | None = None):
        from app.llm.client import LlmClient
        self._llm = llm or LlmClient()
        self._memory = memory or SiteMemory()

    def _fetch_links(self, url: str) -> list[str]:
        """从页面中提取所有链接（去重，上限 100）"""
        from app.extract.scrapling_extractor import ScraplingExtractor
        try:
            extractor = ScraplingExtractor()
            doc = extractor.extract(url)
            import httpx
            from bs4 import BeautifulSoup
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
            return list(dict.fromkeys(links))[:100]  # 去重，上限 100
        except Exception as e:
            logger.warning("plan_agent: 提取链接失败 %s: %s", url, e)
            return []

    def plan(self, source: Source, config: AgentSourceConfig, *, db: Session) -> CrawlPlan:
        root_url = source.url
        base_domain = urlparse(root_url).netloc

        links = self._fetch_links(root_url)
        if not links:
            logger.warning("plan_agent: %s 未找到链接，返回空计划", root_url)
            return CrawlPlan(source_id=config.source_id, urls=[])

        # 发送给 LLM 前先过滤已知 discard URL
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

        # === 确定性校验（不信任 LLM 输出） ===
        try:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {"urls": []}
            raw_urls = data.get("urls", [])
        except Exception:
            raw_urls = []

        validated: list[PlanUrl] = []
        for entry in raw_urls:
            url = entry.get("url", "")
            # 必须同域
            if urlparse(url).netloc != base_domain:
                continue
            # 必须合法 http/https
            if not url.startswith("http"):
                continue
            # 不能是已知 discard URL
            if self._memory.should_skip(db=db, source_id=config.source_id, url=url):
                continue
            validated.append(PlanUrl(url=url, guessed_topic=entry.get("guessed_topic", "")))
            if len(validated) >= config.max_urls_per_run:
                break

        logger.info("plan_agent: 为来源 %d 规划了 %d 个 URL", config.source_id, len(validated))
        return CrawlPlan(source_id=config.source_id, urls=validated)
```

- [ ] **步骤 4：运行测试验证通过**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_agent_plan_agent.py -v
```

预期：3 个测试 PASS。

- [ ] **步骤 5：提交**

```bash
cd backend && git add app/agent/plan_agent.py tests/unit/test_agent_plan_agent.py && git commit -m "feat: 新增 PlanAgent 含确定性 URL 校验和 SiteMemory 联动"
```

---

### 任务 14：AgentCrawlFetcher + API 路由

**文件：**
- 新建：`backend/app/fetchers/agent_crawl.py`
- 新建：`backend/app/api/agent_routes.py`
- 修改：`backend/app/api/main.py`
- 修改：`backend/app/scheduler.py`

- [ ] **步骤 1：创建 `backend/app/fetchers/agent_crawl.py`**

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
    """agent_crawl 来源类型的 Fetcher 协议实现。

    编排四阶段流水线：
    ① PlanAgent → ② CrawlDAG → ③ QualityWorkerPool → ④ SummaryWorkerPool
    """

    def __init__(self, db: Session):
        self._db = db
        self._memory = SiteMemory()
        self._plan_agent = PlanAgent(memory=self._memory)
        self._crawl_dag = CrawlDAG()
        self._quality_pool = QualityWorkerPool(memory=self._memory)
        self._summary_pool = SummaryWorkerPool()

    def fetch(self, source: Source) -> list[RawItem]:
        # 加载来源配置
        config_model: AgentSourceConfigModel | None = self._db.get(AgentSourceConfigModel, source.id)
        if config_model is None:
            logger.warning("agent_crawl: 来源 %d 无配置，跳过", source.id)
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

        # 创建运行记录
        run = AgentCrawlRun(source_id=source.id)
        self._db.add(run)
        self._db.flush()

        async def _pipeline() -> list[AgentItem]:
            # ① 规划
            plan = self._plan_agent.plan(source, config, db=self._db)
            run.plan_urls_count = len(plan.urls)
            self._db.commit()
            # ② 抓取
            pages = await self._crawl_dag.execute(plan, config)
            run.fetched_count = len(pages)
            self._db.commit()
            # ③ 评估
            qualified = await self._quality_pool.assess_all(pages, config, db=self._db)
            run.quality_passed = len(qualified)
            self._db.commit()
            # ④ 摘要
            return await self._summary_pool.summarize_all(qualified, config)

        try:
            agent_items = asyncio.run(_pipeline())
        except Exception as e:
            run.status = "failed"
            run.error_message = str(e)
            run.completed_at = datetime.now(timezone.utc)
            self._db.commit()
            logger.exception("agent_crawl: 来源 %d 流水线执行失败", source.id)
            return []

        raw_items = [_to_raw_item(item) for item in agent_items]
        run.items_created = len(raw_items)
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        self._db.commit()
        return raw_items


def _to_raw_item(item: AgentItem) -> RawItem:
    """将 AgentItem 转换为 RawItem，对接现有去重管道。

    content_type 编码在 key_points 前缀中：首元素为 "__type:release_note"
    """
    from app.schemas import RawItem
    key_points_prefix = [f"__type:{item.content_type}"] + item.key_facts
    return RawItem(
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        raw_content=item.body,
        published_at=None,
        # 通过 extra 字段传递元数据——存储为条目状态覆盖
        extra={"agent_item": True, "main_category": item.topic_group or "agent_crawl",
               "importance": item.importance, "key_points": key_points_prefix},
    )
```

- [ ] **步骤 2：更新 RawItem schema 以接受 `extra` 字段**

在 `backend/app/schemas.py` 的 `RawItem` 模型中添加：

```python
extra: dict | None = None
```

- [ ] **步骤 3：在 `app/scheduler.py` 中接入 agent_crawl**

在 `backend/app/scheduler.py` 中，找到 `build_fetcher()` 函数，添加 `agent_crawl` 分支：

```python
from app.enums import SourceType

def build_fetcher(source: Source, extractor, search, db=None):
    if source.type == SourceType.AGENT_CRAWL:
        from app.fetchers.agent_crawl import AgentCrawlFetcher
        return AgentCrawlFetcher(db=db)
    # ... 后续原有分支
```

- [ ] **步骤 4：创建 `backend/app/api/agent_routes.py`**

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
    """创建/更新 Agent 来源的请求体"""
    name: str
    root_url: str
    focus_areas: list[str] = []          # 关注领域
    topic_groups: list[str] = []         # 主题分组
    crawl_depth: int = 1                 # 爬取深度
    max_urls_per_run: int = 20           # 每次最大 URL 数
    quality_threshold: int = 4           # 质量门槛
    crawl_workers: int = 5               # 抓取并发
    quality_workers: int = 3             # 评估并发
    summary_workers: int = 3             # 摘要并发


def _source_response(source: Source, config: AgentSourceConfig | None) -> dict:
    """构造来源 + 配置的统一响应"""
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
    """列出当前用户的所有 Agent 爬取来源"""
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
    """创建新的 Agent 爬取来源"""
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
    """更新 Agent 爬取来源"""
    source = db.get(Source, source_id)
    if source is None or source.type != "agent_crawl":
        raise HTTPException(status_code=404, detail="Agent 来源不存在")
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
    """删除 Agent 爬取来源"""
    source = db.get(Source, source_id)
    if source is None or source.type != "agent_crawl":
        raise HTTPException(status_code=404, detail="Agent 来源不存在")
    db.delete(source)
    db.commit()


@router.get("/{source_id}/runs")
def list_runs(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查看来源的运行历史（最近 20 次）"""
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
    """查看 SiteMemory 记录（仅管理员）"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
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
    """删除单条 SiteMemory 记录（仅管理员）"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    record = db.scalars(
        select(AgentSiteMemory)
        .where(AgentSiteMemory.source_id == source_id, AgentSiteMemory.url_pattern == url_pattern)
    ).first()
    if record:
        db.delete(record)
        db.commit()
```

- [ ] **步骤 5：在 `app/api/main.py` 中注册路由**

```python
from app.api.agent_routes import router as agent_router
app.include_router(agent_router)
```

- [ ] **步骤 6：运行完整测试套件**

```bash
cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/ -v 2>&1 | tail -20
```

预期：所有测试 PASS。

- [ ] **步骤 7：提交**

```bash
cd backend && git add app/fetchers/agent_crawl.py app/agent/plan_agent.py app/api/agent_routes.py app/api/main.py app/scheduler.py app/schemas.py && git commit -m "feat: 新增 AgentCrawlFetcher 和 /sources/agent API"
```
