"""AgentCrawlFetcher 单元测试 — 验证 Handoff Chain 编排、配置缺失处理、异常隔离。"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.schemas import AgentItem, CrawlPlan, PlanUrl, QualifiedPage, RawPage
from app.fetchers.agent_crawl import AgentCrawlFetcher, _to_raw_item
from app.models import AgentSourceConfig as AgentSourceConfigModel
from app.schemas import RawItem


def _make_config_model(**kwargs):
    """构造一个数据库 AgentSourceConfig 记录。"""
    defaults = dict(
        source_id=1, focus_areas=["kernel", "eBPF"], topic_groups=["项目动态"],
        crawl_depth=1, max_urls_per_run=5, quality_threshold=4,
        crawl_workers=3, quality_workers=2, summary_workers=2,
    )
    defaults.update(kwargs)
    return AgentSourceConfigModel(**defaults)


def _make_agent_item(url="https://blog.example.com/post/1", **kwargs):
    """构造一个 AgentItem。"""
    defaults = dict(
        source_id=1, url=url, title="Linux 6.12 发布",
        topic_group="项目动态", content_type="release_note",
        importance="高", body="内核 6.12 引入 sched_ext。",
        key_facts=["[内核] sched_ext 合入主线"],
    )
    defaults.update(kwargs)
    return AgentItem(**defaults)


class TestToRawItem:
    """测试 _to_raw_item 将 AgentItem 转换为 RawItem。"""

    def test_converts_basic_fields(self):
        """基本字段应正确映射。"""
        item = _make_agent_item()
        raw = _to_raw_item(item)
        assert raw.source_id == 1
        assert raw.url == "https://blog.example.com/post/1"
        assert raw.title == "Linux 6.12 发布"
        assert raw.raw_content == "内核 6.12 引入 sched_ext。"
        assert raw.published_at is None

    def test_extra_contains_agent_metadata(self):
        """extra 字段应包含 agent 元数据和编码后的 key_points。"""
        item = _make_agent_item(sub_tags=["Linux Kernel", "调度器"])
        raw = _to_raw_item(item)
        assert raw.extra is not None
        assert raw.extra["agent_item"] is True
        assert raw.extra["main_category"] == "项目动态"
        assert raw.extra["importance"] == "高"
        assert "__type:release_note" in raw.extra["key_points"]
        assert raw.extra["sub_tags"] == ["Linux Kernel", "调度器"]

    def test_topic_group_none_falls_back_to_default_main_category(self):
        """topic_group 为空时，main_category 应继承来源默认主分类。"""
        item = _make_agent_item(topic_group=None)
        raw = _to_raw_item(item, default_main_category="友商产品信息")
        assert raw.extra["main_category"] == "友商产品信息"

    def test_key_points_include_content_type_prefix(self):
        """content_type 通过 __type: 前缀编码到 key_points 首元素。"""
        item = _make_agent_item(
            content_type="benchmark",
            key_facts=["[性能] SPEC CPU 2017 提升 5%"],
        )
        raw = _to_raw_item(item)
        assert raw.extra["key_points"][0] == "__type:benchmark"
        assert raw.extra["key_points"][1] == "[性能] SPEC CPU 2017 提升 5%"


class TestAgentCrawlFetcherFetch:
    """测试 AgentCrawlFetcher.fetch() 编排逻辑。"""

    def test_missing_config_returns_empty(self):
        """当 AgentSourceConfig 不存在时，应返回空列表。"""
        db = MagicMock()
        db.get.return_value = None  # AgentSourceConfig 查询返回 None
        fetcher = AgentCrawlFetcher(db=db)
        source = MagicMock()
        source.id = 1
        result = fetcher.fetch(source)
        assert result == []

    def test_successful_pipeline_returns_raw_items(self):
        """完整 pipeline 成功时应返回 RawItem 列表。"""
        db = MagicMock()
        db.get.return_value = _make_config_model()
        stage_snapshots: list[tuple[str | None, str | None, int, int, int, int, str | None]] = []

        def record_stage():
            run = db.add.call_args_list[0].args[0]
            stage_snapshots.append(
                (
                    getattr(run, "current_stage", None),
                    getattr(run, "stage_message", None),
                    run.plan_urls_count,
                    run.fetched_count,
                    run.quality_passed,
                    run.items_created,
                    run.status,
                )
            )

        db.commit.side_effect = record_stage

        # Mock 四个阶段
        mock_plan = CrawlPlan(source_id=1, urls=[
            PlanUrl(url="https://blog.example.com/post/1", guessed_topic="kernel"),
        ])
        mock_page = RawPage(
            url="https://blog.example.com/post/1", guessed_topic="kernel",
            title="Linux 6.12", content="内核内容...",
        )
        mock_qualified = QualifiedPage(page=mock_page, verdict="keep", score=8)
        mock_agent_item = _make_agent_item()

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=MagicMock(plan=MagicMock(return_value=mock_plan)),
            crawl_dag=MagicMock(execute=AsyncMock(return_value=[mock_page])),
            quality_pool=MagicMock(assess_all=AsyncMock(return_value=[mock_qualified])),
            summary_pool=MagicMock(summarize_all=AsyncMock(return_value=[mock_agent_item])),
        )
        source = MagicMock()
        source.id = 1
        result = fetcher.fetch(source)
        assert len(result) == 1
        assert isinstance(result[0], RawItem)
        assert result[0].title == "Linux 6.12 发布"
        assert stage_snapshots == [
            ("planning", "已规划 1 个 URL", 1, 0, 0, 0, "running"),
            ("crawling", "并行抓取 1 个 URL", 1, 1, 0, 0, "running"),
            ("quality", "质量通过 1 / 1", 1, 1, 1, 0, "running"),
            ("summarizing", "正在生成 1 条摘要", 1, 1, 1, 0, "running"),
            ("completed", "已生成 1 条候选", 1, 1, 1, 1, "completed"),
        ]

    def test_rss_seed_source_uses_feed_entries_as_plan_urls(self):
        """RSS-backed agent sources should seed CrawlDAG from feed entry links."""
        db = MagicMock()
        db.get.return_value = _make_config_model(max_urls_per_run=2)
        plan_agent = MagicMock()
        crawl_dag = MagicMock(execute=AsyncMock(return_value=[]))
        rss_fetcher = MagicMock(fetch=MagicMock(return_value=[
            RawItem(
                source_id=1,
                title="Kernel fix",
                url="https://example.com/post-1",
                published_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
            ),
            RawItem(
                source_id=1,
                title="Release notes",
                url="https://example.com/post-2",
                published_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
            ),
            RawItem(
                source_id=1,
                title="Overflow",
                url="https://example.com/post-3",
                published_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
            ),
        ]))

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=plan_agent,
            crawl_dag=crawl_dag,
            quality_pool=MagicMock(assess_all=AsyncMock(return_value=[])),
            summary_pool=MagicMock(summarize_all=AsyncMock(return_value=[])),
            rss_fetcher=rss_fetcher,
            time_window={
                "time_mode": "absolute",
                "start_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "end_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
            },
        )
        source = MagicMock()
        source.id = 1
        source.url = "https://example.com/feed.xml"
        source.api_config = {
            "seed_type": "rss",
            "seed_url": "https://example.com/feed.xml",
        }

        result = fetcher.fetch(source)

        assert result == []
        plan_agent.plan.assert_not_called()
        plan = crawl_dag.execute.call_args.args[0]
        assert [url.url for url in plan.urls] == [
            "https://example.com/post-1",
            "https://example.com/post-2",
        ]

    def test_legacy_agent_source_with_feed_url_uses_rss_seed(self):
        """Existing agent sources created from RSS URLs should work without api_config."""
        db = MagicMock()
        db.get.return_value = _make_config_model(max_urls_per_run=1)
        plan_agent = MagicMock()
        crawl_dag = MagicMock(execute=AsyncMock(return_value=[]))
        rss_fetcher = MagicMock(fetch=MagicMock(return_value=[
            RawItem(
                source_id=1,
                title="Blog article",
                url="https://example.com/blog/article",
                published_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
            ),
        ]))

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=plan_agent,
            crawl_dag=crawl_dag,
            quality_pool=MagicMock(assess_all=AsyncMock(return_value=[])),
            summary_pool=MagicMock(summarize_all=AsyncMock(return_value=[])),
            rss_fetcher=rss_fetcher,
            time_window={
                "time_mode": "absolute",
                "start_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "end_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
            },
        )
        source = MagicMock()
        source.id = 1
        source.url = "https://example.com/blog/index.xml"
        source.api_config = None

        fetcher.fetch(source)

        plan_agent.plan.assert_not_called()
        plan = crawl_dag.execute.call_args.args[0]
        assert [url.url for url in plan.urls] == ["https://example.com/blog/article"]

    def test_rss_seed_filters_entries_by_absolute_time_window(self):
        """RSS-backed agent sources should only plan URLs inside the run time window."""
        db = MagicMock()
        db.get.return_value = _make_config_model(max_urls_per_run=5)
        crawl_dag = MagicMock(execute=AsyncMock(return_value=[]))
        rss_fetcher = MagicMock(fetch=MagicMock(return_value=[
            RawItem(
                source_id=1,
                title="Old",
                url="https://example.com/old",
                published_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            ),
            RawItem(
                source_id=1,
                title="Inside",
                url="https://example.com/inside",
                published_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
            ),
            RawItem(
                source_id=1,
                title="Future",
                url="https://example.com/future",
                published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            ),
        ]))

        fetcher = AgentCrawlFetcher(
            db=db,
            crawl_dag=crawl_dag,
            quality_pool=MagicMock(assess_all=AsyncMock(return_value=[])),
            summary_pool=MagicMock(summarize_all=AsyncMock(return_value=[])),
            rss_fetcher=rss_fetcher,
            time_window={
                "time_mode": "absolute",
                "start_at": datetime(2026, 6, 10, tzinfo=timezone.utc),
                "end_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
            },
        )
        source = MagicMock()
        source.id = 1
        source.url = "https://example.com/feed.xml"
        source.api_config = None

        fetcher.fetch(source)

        plan = crawl_dag.execute.call_args.args[0]
        assert [url.url for url in plan.urls] == ["https://example.com/inside"]

    def test_pipeline_failure_returns_empty(self):
        """Pipeline 中任何阶段抛出异常时应返回空列表并记录失败。"""
        db = MagicMock()
        db.get.return_value = _make_config_model()
        stage_snapshots: list[tuple[str | None, str | None, str, str | None]] = []

        def record_stage():
            run = db.add.call_args_list[0].args[0]
            stage_snapshots.append(
                (
                    getattr(run, "current_stage", None),
                    getattr(run, "stage_message", None),
                    run.status,
                    run.error_message,
                )
            )

        db.commit.side_effect = record_stage

        fetcher = AgentCrawlFetcher(
            db=db,
            plan_agent=MagicMock(plan=MagicMock(side_effect=RuntimeError("LLM 不可用"))),
        )
        source = MagicMock()
        source.id = 1
        result = fetcher.fetch(source)
        assert result == []
        # 验证 run 被标记为 failed
        calls = db.add.call_args_list
        assert len(calls) >= 1  # 至少创建了 AgentCrawlRun
        assert stage_snapshots[-1] == ("failed", "LLM 不可用", "failed", "LLM 不可用")
