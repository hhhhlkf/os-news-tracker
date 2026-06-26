"""CrawlDAG — 并行 URL 抓取引擎（纯确定性，不涉及 LLM）。

使用 asyncio.Semaphore 控制并发数，对 PlanAgent 产出的 URL 列表
并行抓取页面内容。单个 URL 失败不影响其他 URL 的执行。

这是 Handoff Chain 中唯一的纯确定性阶段，不调用任何 LLM。
"""

import asyncio
import logging
import random
from typing import Awaitable, Callable

from app.agent.schemas import AgentSourceConfig, CrawlPlan, PlanUrl, RawPage
from app.run_logs import append_run_log

logger = logging.getLogger(__name__)


async def _default_fetch(url: str) -> dict:
    """默认抓取实现：使用 ScraplingExtractor 提取页面正文。

    通过 asyncio.to_thread 在线程池中执行，避免阻塞事件循环。
    """
    from app.extract.scrapling_extractor import ScraplingExtractor

    extractor = ScraplingExtractor()
    doc = await asyncio.to_thread(extractor.extract, url)
    return {
        "title": doc.title or "",
        "content": doc.clean_content or "",
        "published_at": doc.published_at,
    }


class CrawlDAG:
    """并行 URL 抓取器。

    使用 asyncio.Semaphore 限制并发数，按 config.crawl_workers 控制。
    每个 URL 抓取前加入随机延迟（0.1–0.5 秒），避免触发目标站点的反爬机制。

    用法：
        dag = CrawlDAG(fetch_fn=custom_fetch)  # fetch_fn 可选，默认用 Scrapling
        pages = await dag.execute(plan, config)
    """

    def __init__(
        self, fetch_fn: Callable[[str], Awaitable[dict]] | None = None
    ):
        """初始化 CrawlDAG。

        Args:
            fetch_fn: 可选的自定义抓取函数，签名为 async (url: str) -> dict。
                      返回 dict 需包含 "title" 和 "content" 键。
                      默认使用 ScraplingExtractor。
        """
        self._fetch = fetch_fn or _default_fetch

    async def execute(
        self,
        plan: CrawlPlan,
        config: AgentSourceConfig,
        *,
        source_name: str | None = None,
    ) -> list[RawPage]:
        """执行并行抓取。

        Args:
            plan: PlanAgent 产出的抓取计划，包含待抓取 URL 列表。
            config: Agent 源配置，crawl_workers 控制并发上限。
            source_name: 可选的源名称，用于运行日志展示。

        Returns:
            成功抓取的 RawPage 列表。失败的 URL 被静默跳过并记录日志。
        """
        sem = asyncio.Semaphore(config.crawl_workers)

        append_run_log(
            "fetch",
            "开始并行抓取页面",
            source=source_name,
            plan_urls=len(plan.urls),
            workers=config.crawl_workers,
        )

        async def fetch_one(pu: PlanUrl) -> RawPage | None:
            async with sem:
                # 随机延迟，减轻目标服务器压力
                await asyncio.sleep(random.uniform(0.1, 0.5))
                try:
                    result = await self._fetch(pu.url)
                    page = RawPage(
                        url=pu.url,
                        guessed_topic=pu.guessed_topic,
                        title=result.get("title", ""),
                        content=result.get("content", ""),
                        published_at=result.get("published_at"),
                    )
                    append_run_log(
                        "fetch",
                        "页面抓取成功",
                        source=source_name,
                        url=pu.url,
                        title=page.title or "(无标题)",
                        chars=len(page.content),
                    )
                    return page
                except Exception as e:
                    append_run_log(
                        "fetch",
                        "页面抓取失败",
                        source=source_name,
                        level="error",
                        url=pu.url,
                        reason=str(e),
                    )
                    logger.warning(
                        "crawl_dag: 抓取失败 %s: %s", pu.url, e
                    )
                    return None

        # 所有 URL 并发抓取，Semaphore 自动排队
        results = await asyncio.gather(
            *[fetch_one(pu) for pu in plan.urls]
        )
        pages = [r for r in results if r is not None]

        append_run_log(
            "fetch",
            "页面抓取完成",
            source=source_name,
            fetched=len(pages),
            plan_urls=len(plan.urls),
            failed=len(plan.urls) - len(pages),
        )
        logger.info(
            "crawl_dag: 抓取完成 %d/%d 页", len(pages), len(plan.urls)
        )
        return pages
