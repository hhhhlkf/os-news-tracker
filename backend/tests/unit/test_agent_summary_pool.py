"""SummaryWorkerPool 单元测试 — 验证自适应摘要生成、主题分组分配、错误隔离。"""

import json
from unittest.mock import MagicMock

import pytest

from app.agent.schemas import AgentSourceConfig, QualifiedPage, RawPage
from app.agent.summary_pool import SummaryWorkerPool


def _config(topic_groups=None, **kwargs):
    """构造 AgentSourceConfig，提供合理的默认值。"""
    defaults = dict(
        source_id=1, focus_areas=["kernel", "ebpf"], topic_groups=topic_groups or [],
        summary_workers=2, quality_workers=2, crawl_workers=3,
        quality_threshold=4, crawl_depth=1, max_urls_per_run=20,
    )
    defaults.update(kwargs)
    return AgentSourceConfig(**defaults)


def _qpage(url="https://a.com/1"):
    """构造一个通过质量评估的测试用 QualifiedPage。"""
    page = RawPage(
        url=url, guessed_topic="kernel",
        title="Linux 6.12 Released", content="Linux 6.12 brings sched_ext and many EEVDF improvements.",
    )
    return QualifiedPage(page=page, verdict="keep", score=8)


def _make_llm(content_type="release_note"):
    """构造一个 mock LLM，返回指定内容类型的 JSON 摘要响应。"""
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "title": "Linux 6.12 正式发布",
        "topic_group": None,
        "content_type": content_type,
        "importance": "高",
        "body": "内核 6.12 引入 sched_ext 可扩展调度器框架，并带来多项 EEVDF 调度器改进。",
        "key_facts": ["sched_ext 合入主线", "EEVDF 调度器多项优化"],
        "sub_tags": ["Linux Kernel", "调度器"],
        "merge_suggestions": [],
    }, ensure_ascii=False)
    return llm


@pytest.mark.asyncio
async def test_summarize_returns_agent_item():
    """正常摘要：QualifiedPage 应被转换为 AgentItem，字段完整。"""
    pool = SummaryWorkerPool(llm=_make_llm())
    items = await pool.summarize_all([_qpage()], _config())
    assert len(items) == 1
    item = items[0]
    assert item.title == "Linux 6.12 正式发布"
    assert item.importance == "高"
    assert item.content_type == "release_note"
    assert len(item.key_facts) == 2
    assert item.sub_tags == ["Linux Kernel", "调度器"]
    assert item.source_id == 1
    assert item.url == "https://a.com/1"


@pytest.mark.asyncio
async def test_topic_group_assigned_when_provided():
    """配置了 topic_groups 时，LLM 可从列表中选择最匹配的分组。"""
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "title": "Anolis OS 23.2 发布",
        "topic_group": "项目动态",
        "content_type": "release_note",
        "importance": "中",
        "body": "Anolis OS 23.2 正式发布，新增多项安全特性。",
        "key_facts": ["基于龙蜥 23", "新增安全特性"],
    }, ensure_ascii=False)
    pool = SummaryWorkerPool(llm=llm)
    items = await pool.summarize_all(
        [_qpage()], _config(topic_groups=["项目动态", "技术迭代", "安全公告"]),
    )
    assert items[0].topic_group == "项目动态"


@pytest.mark.asyncio
async def test_no_topic_group_when_not_configured():
    """未配置 topic_groups 时，topic_group 应为 None。"""
    pool = SummaryWorkerPool(llm=_make_llm())
    items = await pool.summarize_all([_qpage()], _config(topic_groups=[]))
    assert items[0].topic_group is None


@pytest.mark.asyncio
async def test_failed_summary_is_skipped():
    """单个页面摘要失败不应影响其他页面。"""
    # 第一个调用成功，第二个抛出异常
    call_count = [0]

    def fail_second(prompt):
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("LLM timeout")
        return json.dumps({
            "title": "ok", "topic_group": None, "content_type": "article",
            "importance": "低", "body": "ok", "key_facts": [],
        }, ensure_ascii=False)

    llm = MagicMock()
    llm.complete.side_effect = fail_second

    pool = SummaryWorkerPool(llm=llm)
    items = await pool.summarize_all(
        [_qpage("https://a.com/1"), _qpage("https://a.com/2")], _config(),
    )
    # 第二个失败被跳过，第一个成功保留
    assert len(items) == 1


@pytest.mark.asyncio
async def test_respects_summary_workers_concurrency():
    """并发数不应超过 summary_workers 配置。"""
    active = [0]
    peak = [0]

    def counting_complete(prompt):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        # 短暂休眠模拟 LLM 调用
        import time
        time.sleep(0.02)
        active[0] -= 1
        return json.dumps({
            "title": "t", "topic_group": None, "content_type": "article",
            "importance": "低", "body": "b", "key_facts": [],
        }, ensure_ascii=False)

    llm = MagicMock()
    llm.complete.side_effect = counting_complete

    pool = SummaryWorkerPool(llm=llm)
    pages = [_qpage(f"https://a.com/{i}") for i in range(8)]
    await pool.summarize_all(pages, _config(summary_workers=2))

    # 峰值并发不应超过配置的 2
    assert peak[0] <= 2
