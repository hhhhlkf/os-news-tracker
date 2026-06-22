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

# 质量评估 prompt 模板
# 要求 LLM 从相关性、信息密度两个维度打分，输出结构化 JSON
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
    """从 LLM 原始响应中提取 JSON 并解析为 QualityResult。

    安全措施：即使 LLM 返回 verdict="keep"，如果分数低于阈值，
    也会强制改为 discard。阈值是系统决策的硬约束，LLM 不能覆盖。

    Args:
        text: LLM 原始响应文本（可能包含非 JSON 的前后文）。
        threshold: 质量阈值，低于此分数的页面一律丢弃。

    Returns:
        解析后的 QualityResult。

    Raises:
        ValueError: 响应中找不到有效 JSON。
    """
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON in quality response: {text[:100]}")
    data = json.loads(m.group(0))

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
        """初始化 QualityWorkerPool。

        Args:
            llm: 可选的自定义 LLM 客户端，默认使用 LlmClient。
            memory: 可选的 SiteMemory 实例，默认创建新实例。
        """
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

        Args:
            pages: CrawlDAG 抓取的原始页面列表。
            config: Agent 源配置，quality_threshold 和 quality_workers 在此读取。
            db: 数据库会话（传递给 SiteMemory）。

        Returns:
            通过质量评估的 QualifiedPage 列表。低于阈值的页面不出现在结果中。
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
                        logger.debug(
                            "quality_pool: 缓存命中 discard → 跳过 %s", page.url,
                        )
                        return None
                    # keep 缓存命中 → 直接通过，不调 LLM
                    logger.debug(
                        "quality_pool: 缓存命中 keep → 直接通过 %s", page.url,
                    )
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
