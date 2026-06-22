"""CrawlDAG 单元测试 — 验证并行抓取、失败隔离、并发控制。"""

import asyncio

import pytest

from app.agent.crawl_dag import CrawlDAG
from app.agent.schemas import AgentSourceConfig, CrawlPlan, PlanUrl


def _config(**kwargs):
    """构造 AgentSourceConfig，提供合理的默认值。"""
    defaults = dict(
        source_id=1, focus_areas=[], topic_groups=[],
        crawl_workers=3, quality_workers=3, summary_workers=3,
        quality_threshold=4, crawl_depth=1, max_urls_per_run=20,
    )
    defaults.update(kwargs)
    return AgentSourceConfig(**defaults)


async def _fake_fetch(url):
    """模拟成功的抓取函数。"""
    return {"title": f"Title for {url}", "content": f"Content of {url}"}


@pytest.mark.asyncio
async def test_execute_returns_raw_pages():
    """正常抓取：所有 URL 都应返回对应的 RawPage。"""
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
    """单个 URL 抓取失败不应影响其他 URL。"""

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

    # 失败的 URL 被跳过，成功的 URL 保留
    assert len(pages) == 1
    assert pages[0].url == "https://a.com/ok"


@pytest.mark.asyncio
async def test_respects_worker_concurrency():
    """并发数不应超过 crawl_workers 配置。"""
    active = [0]
    peak = [0]

    async def counting_fetch(url):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.01)
        active[0] -= 1
        return {"title": "t", "content": "c"}

    plan = CrawlPlan(
        source_id=1,
        urls=[PlanUrl(url=f"https://x/{i}") for i in range(10)],
    )
    dag = CrawlDAG(fetch_fn=counting_fetch)
    await dag.execute(plan, _config(crawl_workers=3))

    # 峰值并发不应超过配置的 3
    assert peak[0] <= 3
