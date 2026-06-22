"""PlanAgent 单元测试 — 验证 URL 规划、同域过滤、SiteMemory 跳过、上限截断。"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.agent.plan_agent import PlanAgent
from app.agent.schemas import AgentSourceConfig


def _config(**kwargs):
    """构造 AgentSourceConfig，提供合理的默认值。"""
    defaults = dict(
        source_id=1, focus_areas=["kernel", "eBPF"], topic_groups=[],
        crawl_workers=5, quality_workers=3, summary_workers=3,
        quality_threshold=4, crawl_depth=1, max_urls_per_run=5,
    )
    defaults.update(kwargs)
    return AgentSourceConfig(**defaults)


def _make_source(url="https://blog.example.com"):
    """构造一个 mock Source 对象。"""
    s = MagicMock()
    s.id = 1
    s.url = url
    return s


def _make_llm(urls):
    """构造一个 mock LLM，返回指定的 URL 列表及其 guessed_topic。"""
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "urls": [{"url": u, "guessed_topic": "kernel"} for u in urls],
    })
    return llm


def test_plan_filters_off_domain_urls():
    """LLM 可能返回跨域 URL，确定性验证层应将其过滤掉。"""
    llm = _make_llm([
        "https://blog.example.com/post/1",
        "https://evil.com/phishing",           # 跨域 — 应被过滤
        "https://blog.example.com/post/2",
    ])
    agent = PlanAgent(llm=llm)
    db = MagicMock()
    with patch.object(agent, "_fetch_links", return_value=[
        "https://blog.example.com/post/1",
        "https://blog.example.com/post/2",
    ]):
        plan = agent.plan(_make_source(), _config(), db=db)
    assert all("evil.com" not in u.url for u in plan.urls)
    assert len(plan.urls) == 2


def test_plan_respects_max_urls():
    """即使 LLM 返回超过 max_urls_per_run 的 URL，也应被截断。"""
    urls = [f"https://blog.example.com/post/{i}" for i in range(20)]
    llm = _make_llm(urls)
    agent = PlanAgent(llm=llm)
    db = MagicMock()
    with patch.object(agent, "_fetch_links", return_value=urls):
        plan = agent.plan(_make_source(), _config(), db=db)
    assert len(plan.urls) <= 5   # max_urls_per_run=5


def test_plan_skips_known_discard_urls():
    """SiteMemory 标记为 discard 的 URL 应在规划阶段就被排除。"""
    mock_memory = MagicMock()
    mock_memory.should_skip.side_effect = lambda db, source_id, url: "discard" in url

    agent = PlanAgent(llm=_make_llm([
        "https://blog.example.com/good",
        "https://blog.example.com/discard-me",
    ]), memory=mock_memory)
    db = MagicMock()
    with patch.object(agent, "_fetch_links", return_value=[
        "https://blog.example.com/good",
        "https://blog.example.com/discard-me",
    ]):
        plan = agent.plan(_make_source(), _config(), db=db)
    assert all("discard" not in u.url for u in plan.urls)
    assert len(plan.urls) == 1


def test_plan_returns_empty_when_no_links():
    """当页面没有可提取的链接时，应返回空计划。"""
    agent = PlanAgent(llm=MagicMock())
    db = MagicMock()
    with patch.object(agent, "_fetch_links", return_value=[]):
        plan = agent.plan(_make_source(), _config(), db=db)
    assert len(plan.urls) == 0


def test_plan_includes_source_id():
    """返回的 CrawlPlan 应正确携带 source_id。"""
    llm = _make_llm(["https://blog.example.com/post/1"])
    agent = PlanAgent(llm=llm)
    db = MagicMock()
    with patch.object(agent, "_fetch_links", return_value=["https://blog.example.com/post/1"]):
        plan = agent.plan(_make_source(), _config(source_id=42), db=db)
    assert plan.source_id == 42
