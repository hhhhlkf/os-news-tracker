"""QualityWorkerPool 单元测试 — 验证 LLM 质量评估、阈值过滤、SiteMemory 缓存命中。"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.agent.quality_pool import QualityWorkerPool
from app.agent.schemas import AgentSourceConfig, RawPage


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


def _make_db():
    """构造一个 mock 数据库会话，默认不返回任何 SiteMemory 记录。"""
    db = MagicMock()
    db.scalars.return_value.first.return_value = None
    return db


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
async def test_empty_llm_response_skips_page_not_abort_run():
    """单个页面 LLM 返回空/非 JSON 时，该页被跳过，不拖垮整条评估。

    复现 run 失败：LLM 偶发返回空字符串，_parse_quality 抛 ValueError，
    assess_one 没有 try/except，异常传到 asyncio.gather 导致整条 run failed。
    """
    llm = MagicMock()
    # 第一个页面返回空（触发 ValueError），第二个返回正常 JSON
    llm.complete.side_effect = ["", _make_llm(score=7).complete.return_value]
    pool = QualityWorkerPool(llm=llm)
    db = _make_db()
    results = await pool.assess_all(
        [_page("https://a.com/bad"), _page("https://a.com/good")],
        _config(), db=db,
    )
    # 坏页面被跳过，好页面保留，assess_all 没抛异常
    assert len(results) == 1
    assert results[0].page.url == "https://a.com/good"


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
