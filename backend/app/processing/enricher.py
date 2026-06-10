import json
import re

from app.enums import MAIN_CATEGORIES
from app.llm.client import LlmClient
from app.schemas import EnrichedFields, NormalizedItem

_PROMPT_TEMPLATE = """你是技术新闻整理助手。阅读下面的文章，输出严格的 JSON（不要多余文字）。

可选主分类（必须从中选一个最贴切的）：{categories}

字段要求：
- title_tldr: 一句话概括，<=30字
- summary: 2-4句核心摘要
- key_points: 3-5条关键点（字符串数组）
- info_type: 从 [发布, 更新, 性能数据, 适配, 观点/分析, 其他] 选一个
- importance: 从 [高, 中, 低] 选一个
- why_it_matters: 1-2句，面向maintainer的影响说明
- main_category: 从可选主分类里选一个
- sub_tags: 细粒度子标签（字符串数组，如厂商名/产品名/技术名）
- entities: 数组，每项 {{"type": one of [vendor,product,os,package,version,topic], "name": "..."}}
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
