"""SummaryWorkerPool — 并行自适应摘要生成（Generator 角色）。

对 QualityWorkerPool 筛选通过的页面进行 LLM 摘要提取，根据页面实际内容类型
自动选择最合适的输出格式（发行说明/基准测试/技术讨论/更新日志/通用文章）。

这是 Handoff Chain 的第④阶段，产出最终进入新闻流的 AgentItem。
"""

import asyncio
import json
import logging
import re

from app.agent.schemas import AgentItem, AgentSourceConfig, QualifiedPage

logger = logging.getLogger(__name__)

# ── 摘要生成 prompt ──────────────────────────────────────────────
# 参考 Enricher 的 prompt 风格：明确的角色定位、内容类型识别、
# 字段级别的输出规范、反例说明。
_PROMPT = """你是操作系统维护团队的技术内容分析师。你的任务是将通过质量筛选的网页内容，
提炼为结构化的技术情报摘要，供 OS maintainer 快速了解要点并决定是否需要深入阅读原文。

## 角色定位

你面对的是专业 OS maintainer（内核开发者、发行版维护者、安全工程师），
他们对操作系统、内核、编译器、包管理、云原生基础设施有深入理解。
你需要提取他们关心的技术事实，而不是复述入门级内容。

## 内容类型识别

根据页面实际内容，选择最合适的 content_type：

| content_type | 适用场景 | 示例 |
|-------------|---------|------|
| release_note | 版本发布、发行版更新、重要软件包新版本 | "Linux 6.12 正式发布"、"Anolis OS 23.2 发布" |
| benchmark | 性能基准测试、横向对比、架构性能分析 | "LLVM 19 vs GCC 15 编译性能对比" |
| changelog | 更新日志、变更列表、补丁说明 | "systemd 257 变更摘要" |
| discussion | 技术讨论、RFC、设计文档、社区争议 | "LKML 讨论是否默认启用 PREEMPT_RT" |
| article | 通用技术文章（不属于以上类型的默认值） | 技术分析、架构介绍、观点评论 |

## 字段要求

- **title**: 中文标题，准确反映页面核心内容，20字以内。不是翻译原标题，而是概括技术要点。
- **topic_group**: 从用户提供的分组列表中选择一个最匹配的。如果列表为空或无匹配项，设为 null。
- **content_type**: 从上述类型中选择一个最贴切的。
- **importance**: 对 OS maintainer 的重要程度。
  - **高**: 内核大版本发布、严重安全漏洞、关键兼容性变更、重大新工具
  - **中**: 重要软件包更新、技术路线变化、性能报告
  - **低**: 一般性技术讨论、小版本更新、观点分析
- **body**: 2–5 句核心摘要，包含具体的技术事实（版本号、特性名、性能数据），不泛泛而谈。
- **key_facts**: 3–5 条关键事实（字符串数组），每条为一个独立的技术要点。格式为「[关键词] 具体说明」，如：
  - 「[内核] Linux 6.12 引入 sched_ext 可扩展调度器框架」
  - 「[性能] EEVDF 调度器延迟降低 15%」
  - 「[兼容性] 移除对 ia64 架构的支持」

## 注意事项

- 不要编造页面中不存在的版本号、数据或技术细节。
- 如果页面信息不足以形成有效摘要（标题空洞、正文无实质内容），body 可以简短但必须诚实。
- body 中不要包含 URL 链接。
- key_facts 应去重，避免同一事实换说法重复。

## 输出格式

严格输出 JSON，不要多余文字：
```json
{{
  "title": "Linux 6.12 内核正式发布",
  "topic_group": "项目动态",
  "content_type": "release_note",
  "importance": "高",
  "body": "Linux 6.12 内核正式发布，引入 sched_ext 可扩展调度器框架……",
  "key_facts": ["[内核] sched_ext 合入主线", "[调度器] EEVDF 多项优化"]
}}
```

用户关注领域：{focus_areas}
用户主题分组：{topic_groups}
页面 URL：{url}
页面标题：{title}
页面正文：
{content}
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
    raise ValueError(f"No JSON found in summary response: {text[:200]}")


def _parse_summary(text: str, source_url: str, source_id: int) -> AgentItem:
    """从 LLM 原始响应中提取 JSON 并解析为 AgentItem。

    source_url 从输入的 QualifiedPage.url 传入，不信任 LLM 返回的 URL
    （防止 LLM 幻觉生成错误 URL）。

    Args:
        text: LLM 原始响应文本。
        source_url: 页面原始 URL（来自 QualifiedPage）。
        source_id: 源 ID。

    Returns:
        解析后的 AgentItem。

    Raises:
        ValueError: 响应中找不到有效 JSON。
    """
    data = _extract_json(text)

    # 验证 importance 取值
    importance = data.get("importance", "低")
    if importance not in ("高", "中", "低"):
        importance = "低"

    # 验证 content_type 取值
    valid_types = {"article", "release_note", "benchmark", "discussion", "changelog"}
    content_type = data.get("content_type", "article")
    if content_type not in valid_types:
        content_type = "article"

    # topic_group 可为 None
    topic_group = data.get("topic_group") or None

    return AgentItem(
        source_id=source_id,
        url=source_url,  # 使用传入的 URL，不信任 LLM 输出
        title=data.get("title", ""),
        topic_group=topic_group,
        content_type=content_type,
        importance=importance,
        body=data.get("body", ""),
        key_facts=data.get("key_facts", []),
    )


class SummaryWorkerPool:
    """并行自适应摘要生成器。

    对每页调用 LLM 进行内容摘要，根据 content_type 自动选择输出格式。
    使用 asyncio.Semaphore 控制并发数。

    用法：
        pool = SummaryWorkerPool(llm=my_llm)
        items = await pool.summarize_all(qualified_pages, config)
    """

    def __init__(self, llm=None):
        """初始化 SummaryWorkerPool。

        Args:
            llm: 可选的自定义 LLM 客户端，默认使用 LlmClient。
        """
        from app.llm.client import LlmClient

        self._llm = llm or LlmClient()

    async def summarize_all(
        self, pages: list[QualifiedPage], config: AgentSourceConfig
    ) -> list[AgentItem]:
        """并行摘要所有通过质量评估的页面。

        单个页面摘要失败时记录日志并跳过，不影响其他页面的处理。

        Args:
            pages: QualityWorkerPool 产出的合格页面列表。
            config: Agent 源配置，summary_workers 控制并发上限。

        Returns:
            成功摘要的 AgentItem 列表。失败的页面被静默跳过。
        """
        sem = asyncio.Semaphore(config.summary_workers)

        async def summarize_one(qp: QualifiedPage) -> AgentItem | None:
            async with sem:
                try:
                    prompt = _PROMPT.format(
                        focus_areas=", ".join(config.focus_areas),
                        topic_groups=", ".join(config.topic_groups) if config.topic_groups else "无",
                        url=qp.page.url,
                        title=qp.page.title,
                        content=qp.page.content[:4000],
                    )
                    raw = await asyncio.to_thread(self._llm.complete, prompt)
                    return _parse_summary(
                        raw, source_url=qp.page.url, source_id=config.source_id,
                    )
                except Exception as e:
                    logger.warning(
                        "summary_pool: 摘要失败 %s: %s", qp.page.url, e,
                    )
                    return None

        # 所有页面并发摘要，Semaphore 自动排队
        results = await asyncio.gather(*[summarize_one(p) for p in pages])
        items = [r for r in results if r is not None]

        logger.info(
            "summary_pool: 摘要完成 %d/%d 页", len(items), len(pages),
        )
        return items
