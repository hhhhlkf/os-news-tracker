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
from app.run_logs import append_run_log

logger = logging.getLogger(__name__)

# ── 质量评估 prompt ──────────────────────────────────────────────
# 风格对齐 Enricher prompt：段落式收录/排除标准 + 明确内容插入区。
# 修复：补充 {focus_areas}/{url}/{title}/{content_preview} 占位符，
# 确保 LLM 能看到待评估的实际页面内容。
_PROMPT = """你是操作系统维护工程师的技术内容质量评估员。阅读以下页面内容，给出 0–10 的综合质量分，判断它是否值得进入后续摘要提取流程。

只收录对 OS maintainer 有实际价值的技术内容：操作系统、内核、发行版、编译器、包管理器、云原生基础设施的版本发布与重大更新；性能基准测试、架构分析、ABI/API 变更公告；AI agent / LLM 工具链、ML 推理框架的重要发布；跨社区/跨发行版的严重安全漏洞与供应链安全事件；上游项目的技术讨论、设计文档、RFC。
不收录：社区活动通知、线下 meetup、会议征稿、直播预告；招聘信息；用户入门教程、基础配置指南；市场营销材料、产品宣传；非技术性公告、公司财报、人事变动；单纯文档首页、仓库 README、SIG 介绍页、目录索引页；列表页、搜索结果页、登录页；反爬挑战页（"Making sure you're not a bot" / "Anubis" / "Proof-of-Work" / "enable JavaScript"）或正文为空、只有站点导航的页面——这些必须打 0 分，verdict=discard。

## 打分维度（0–10 整数分）

从以下四个维度综合评估，给出 0–10 的总分：

1. **相关性** — 与用户关注领域（见下方）的匹配程度
   - 直接命中（内核版本发布、发行版公告、包管理变更）→ 高
   - 间接相关（通用云原生、AI 工具链，但未涉及 OS 层面）→ 中
   - 无关（招聘、活动、营销）→ 低

2. **信息密度** — 是否包含具体的事实、数据、版本号、技术参数
   - 有明确的版本号、CVE 编号、性能数字、代码片段 → 高
   - 有概括性技术描述但缺乏具体数据 → 中
   - 纯观点、纯介绍、无实质技术内容 → 低

3. **时效价值** — 对当前决策和行动的参考价值
   - 刚发布的新版本、新漏洞、新工具 → 高
   - 持续性跟踪内容（性能数据更新、路线图推进）→ 中
   - 过时信息、历史回顾、基础概念介绍 → 低

4. **可操作性** — OS maintainer 读完后能做什么
   - 可直接指导升级/修复/适配决策 → 高
   - 提供背景知识，辅助长期判断 → 中
   - 读了和没读差别不大 → 低

## 分数区间参考

| 分数 | 含义 | 典型场景 |
|------|------|---------|
| 9–10 | 必读 | 内核大版本发布、严重安全漏洞、关键兼容性变更 |
| 7–8  | 推荐 | 重要包更新、性能报告、技术路线变化 |
| 5–6  | 可读 | 一般技术讨论、小版本更新、观点分析 |
| 3–4  | 边缘 | 通用技术新闻、与 OS 关系不大的工具链 |
| 0–2  | 噪音 | 招聘、活动、营销、反爬页、空页 |

should_remember：特征明显（高信息密度、明确技术主题）→ true；内容模糊难以归类 → false。

用户关注点：{focus_areas}

URL：{url}
标题：{title}
正文片段（前1500字）：
{content_preview}

只输出 JSON，不要多余文字：
{{"score": 7, "reason": "包含 Linux 6.12 版本发布的具体变更列表和性能数据", "relevant_topic": "Linux Kernel", "verdict": "keep", "should_remember": true}}
"""


def _extract_json(text: str) -> dict:
    """从 LLM 原始响应中提取 JSON 对象。

    支持两种格式：
    1. ```json { ... } ``` 围栏代码块
    2. 裸 JSON { ... }

    Args:
        text: LLM 原始响应文本。

    Returns:
        解析后的 dict。

    Raises:
        ValueError: 响应中找不到有效 JSON。
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

    Args:
        text: LLM 原始响应文本。
        threshold: 质量阈值，低于此分数的页面一律丢弃。

    Returns:
        解析后的 QualityResult。

    Raises:
        ValueError: 响应中找不到有效 JSON。
    """
    data = _extract_json(text)

    # 硬阈值覆盖：分数不够 → 强制 discard
    # LLM 可能因"内容本身是真实的"而给高分，但我们的阈值是系统级约束
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
        source_name: str | None = None,
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

        append_run_log(
            "quality",
            "开始质量评估",
            source=source_name,
            pages=len(pages),
            threshold=config.quality_threshold,
        )

        async def assess_one(page: RawPage) -> QualifiedPage | None:
            async with sem:
                # ── 1. 查 SiteMemory 缓存 ──
                cached = self._memory.get(
                    db=db, source_id=config.source_id, url=page.url,
                )
                if cached is not None:
                    if cached.verdict == "discard":
                        append_run_log(
                            "quality",
                            "丢弃（缓存命中 discard）",
                            source=source_name,
                            url=page.url,
                            score=cached.quality_score or 0,
                            reason=cached.quality_reason or "SiteMemory 历史判定为低质量",
                        )
                        return None
                    # keep 缓存命中 → 直接通过，不调 LLM
                    append_run_log(
                        "quality",
                        "通过（缓存命中 keep）",
                        source=source_name,
                        url=page.url,
                        score=cached.quality_score or 0,
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
                try:
                    raw = await asyncio.to_thread(self._llm.complete, prompt)
                    result = _parse_quality(raw, config.quality_threshold)
                except Exception as e:
                    # LLM 偶发空响应/非 JSON/超时等：跳过该页，不当做 keep，
                    # 也不让单个页面的失败拖垮整条 run（与 summary_pool 一致）。
                    append_run_log(
                        "quality",
                        "评估失败（跳过）",
                        source=source_name,
                        url=page.url,
                        level="error",
                        reason=str(e)[:200],
                    )
                    logger.warning(
                        "quality_pool: 评估失败 %s: %s", page.url, e,
                    )
                    return None

                # ── 3. 写入 SiteMemory（仅限值得记住的结果）──
                if result.should_remember or result.verdict == "discard":
                    self._memory.upsert(
                        db=db, source_id=config.source_id,
                        url=page.url, result=result,
                    )

                # ── 4. 根据 verdict 决定保留或丢弃 ──
                if result.verdict == "discard":
                    append_run_log(
                        "quality",
                        "丢弃（低于阈值）",
                        source=source_name,
                        url=page.url,
                        score=result.score,
                        threshold=config.quality_threshold,
                        reason=result.reason or "质量分低于阈值",
                    )
                    logger.info(
                        "quality_pool: 丢弃 %s (score=%d)", page.url, result.score,
                    )
                    return None

                append_run_log(
                    "quality",
                    "通过质量评估",
                    source=source_name,
                    url=page.url,
                    score=result.score,
                    topic=result.relevant_topic or None,
                    reason=result.reason or None,
                )
                return QualifiedPage(
                    page=page, verdict=result.verdict, score=result.score,
                )

        results = await asyncio.gather(
            *[assess_one(p) for p in pages],
        )
        qualified = [r for r in results if r is not None]

        append_run_log(
            "quality",
            "质量评估完成",
            source=source_name,
            passed=len(qualified),
            pages=len(pages),
            rejected=len(pages) - len(qualified),
        )
        logger.info(
            "quality_pool: %d/%d 页通过质量评估", len(qualified), len(pages),
        )
        return qualified
