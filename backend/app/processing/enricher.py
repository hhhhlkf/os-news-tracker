import json
import re

from app.enums import MAIN_CATEGORIES
from app.llm.client import LlmClient
from app.schemas import EnrichedFields, NormalizedItem

_PROMPT_TEMPLATE = """你是操作系统维护工程师的技术情报分析师。阅读下面的技术文章，为 OS maintainer 提取结构化情报。

只收录有专业价值的技术内容：版本发布、安全公告、新技术/工具发布、AI agent/LLM 工具链进展、性能基准测试、技术架构分析。
不收录：社区活动通知、招聘信息、用户入门教程、市场营销材料、非技术性公告。

输出严格 JSON（不要多余文字）。

可选主分类（必须选一个最贴切的）：{categories}

字段要求：
- title_zh: 中文翻译标题（准确翻译原标题，不是概括），20字以内
- summary: 2-4句核心摘要
- tech_highlights: 3-5条技术要点（字符串数组），每条格式为「[关键词] 具体说明」，如「[内核版本] Linux 6.12 引入了 sched_ext 调度器框架」
- info_type: 从 [发布, 更新, 性能数据, 适配, 观点/分析, 其他] 选一个
- importance: 从 [高, 中, 低] 选一个。高=版本发布/安全公告/重大新工具；中=技术更新/AI工具链；低=性能测试/架构分析
- main_category: 从可选主分类里选一个
- sub_tags: 细粒度子标签（字符串数组，如厂商名/产品名/技术名）
- keywords: 扁平关键词数组，列出文中的关键技术术语（如 ["Linux 6.12", "RHEL 10", "systemd 256", "eBPF"]）
- confidence: 0~1 的浮点，表示你对归类与摘要的把握

标题：{title}
正文：
{content}

只输出 JSON：
"""


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        return json.loads(brace.group(0))
    raise ValueError(f"No JSON found in LLM output: {text[:200]}")


class Enricher:
    def __init__(self, llm: LlmClient | None = None):
        self._llm = llm or LlmClient()

    def enrich(self, item: NormalizedItem) -> EnrichedFields:
        prompt = _PROMPT_TEMPLATE.format(
            categories=", ".join(MAIN_CATEGORIES),
            title=item.title,
            content=item.clean_content[:6000],
        )
        raw = self._llm.complete(prompt)
        data = _extract_json(raw)
        if data.get("main_category") not in MAIN_CATEGORIES:
            data["main_category"] = MAIN_CATEGORIES[-1]
        return EnrichedFields(**data)
