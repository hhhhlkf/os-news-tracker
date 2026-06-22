"""AgentCrawlFetcher — Agent Crawl 抓取器（Handoff Chain 编排器）。

实现 Fetcher Protocol，将 PlanAgent → CrawlDAG → QualityWorkerPool → SummaryWorkerPool
四个阶段串联为完整的抓取管线。通过 asyncio.run() 驱动异步管线，
对外暴露同步的 fetch() 接口。

Agent crawl 条目绕过 LLM Enricher，直接以 status=agent_enriched 存入数据库。
"""

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


def _to_raw_item(item: AgentItem) -> RawItem:
    """将 AgentItem 转换为 RawItem，富化数据编码在 extra 字段中。

    content_type 通过 key_points 的首元素 __type: 前缀传递，
    Pipeline 中的 agent 旁路会从 extra 读取所有元数据。
    """
    key_points_prefix = [f"__type:{item.content_type}"] + item.key_facts
    return RawItem(
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        raw_content=item.body,
        published_at=None,
        extra={
            "agent_item": True,
            "main_category": item.topic_group or "agent_crawl",
            "importance": item.importance,
            "info_type": "其他",
            "key_points": key_points_prefix,
        },
    )


class AgentCrawlFetcher:
    """实现 Fetcher Protocol 的 Agent Crawl 抓取器。

    通过 asyncio.run() 驱动完整的 Handoff Chain：
    PlanAgent → CrawlDAG → QualityWorkerPool → SummaryWorkerPool。

    每个 fetch() 调用会在 agent_crawl_runs 表中创建一条运行记录，
    跟踪各阶段的产出数量，便于监控和调试。
    """

    def __init__(
        self,
        db: Session,
        *,
        plan_agent=None,
        crawl_dag=None,
        quality_pool=None,
        summary_pool=None,
    ):
        """初始化 AgentCrawlFetcher。

        Args:
            db: 数据库会话（与调用方共享，生命周期由调用方管理）。
            plan_agent: 可选的 PlanAgent 实例（用于测试注入）。
            crawl_dag: 可选的 CrawlDAG 实例。
            quality_pool: 可选的 QualityWorkerPool 实例。
            summary_pool: 可选的 SummaryWorkerPool 实例。
        """
        self._db = db
        self._memory = SiteMemory()
        self._plan_agent = plan_agent or PlanAgent(memory=self._memory)
        self._crawl_dag = crawl_dag or CrawlDAG()
        self._quality_pool = quality_pool or QualityWorkerPool(memory=self._memory)
        self._summary_pool = summary_pool or SummaryWorkerPool()

    def fetch(self, source: Source) -> list[RawItem]:
        """执行一次完整的 Agent Crawl 管线。

        流程：
        1. 从 agent_source_configs 表读取配置
        2. 创建 AgentCrawlRun 运行记录
        3. PlanAgent 规划 URL 列表
        4. CrawlDAG 并行抓取页面
        5. QualityWorkerPool 质量评估和过滤
        6. SummaryWorkerPool 摘要生成
        7. 将 AgentItem 转换为 RawItem 返回（Pipeline 负责存储）

        Args:
            source: 源记录（ORM 对象），需包含 id 和 url 属性。

        Returns:
            RawItem 列表，每个条目的 extra 字段包含 agent 富化元数据。
            管线失败时返回空列表。
        """
        # ── 1. 读取配置 ──
        config_model = self._db.get(AgentSourceConfigModel, source.id)
        if config_model is None:
            logger.warning(
                "agent_crawl: 源 %d 无 AgentSourceConfig，跳过", source.id,
            )
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

        # ── 2. 创建运行记录 ──
        run = AgentCrawlRun(source_id=source.id)
        self._db.add(run)
        self._db.flush()

        # ── 3–6. 执行异步管线 ──
        async def _pipeline() -> list[AgentItem]:
            # ③ PlanAgent: URL 规划
            plan = self._plan_agent.plan(source, config, db=self._db)
            run.plan_urls_count = len(plan.urls)
            self._db.commit()

            # ④ CrawlDAG: 并行抓取
            pages = await self._crawl_dag.execute(plan, config)
            run.fetched_count = len(pages)
            self._db.commit()

            # ⑤ QualityWorkerPool: 质量评估
            qualified = await self._quality_pool.assess_all(
                pages, config, db=self._db,
            )
            run.quality_passed = len(qualified)
            self._db.commit()

            # ⑥ SummaryWorkerPool: 摘要生成
            items = await self._summary_pool.summarize_all(qualified, config)
            return items

        try:
            agent_items = asyncio.run(_pipeline())
        except Exception as e:
            run.status = "failed"
            run.error_message = str(e)
            run.completed_at = datetime.now(timezone.utc)
            self._db.commit()
            logger.exception(
                "agent_crawl: 管线失败 source=%d: %s", source.id, e,
            )
            return []

        # ── 7. 转换为 RawItem ──
        raw_items = [_to_raw_item(item) for item in agent_items]
        run.items_created = len(raw_items)
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        self._db.commit()

        logger.info(
            "agent_crawl: source=%d 完成 plan=%d fetch=%d quality=%d items=%d",
            source.id, run.plan_urls_count, run.fetched_count,
            run.quality_passed, run.items_created,
        )
        return raw_items
