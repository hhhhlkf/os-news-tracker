from app.llm.client import LlmClient
from app.models import Item

_REASON_PROMPT = """你是面向操作系统维护工程师（OS maintainer）的技术情报分析师。
根据下面的文章信息，写一段「推荐理由」，说明这条信息为什么值得 OS maintainer 阅读、对其工作有什么价值。

要求：
- 用中文，只写 1 句话，不超过 50 字，简洁有力。
- 面向 maintainer 的"所以呢"：点明实际影响、可借鉴之处或需要关注的风险。
- 直接输出理由正文，不要加「推荐理由：」前缀，不要使用引号或 markdown。

标题：{title}
主分类：{category}
摘要：{summary}
技术要点：
{key_points}

只输出推荐理由正文：
"""


def generate_recommendation_reason(item: Item, llm: LlmClient | None = None) -> str:
    """Generate a generic, maintainer-facing recommendation reason for an item."""
    llm = llm or LlmClient()
    key_points = "\n".join(item.key_points or []) or "（无）"
    prompt = _REASON_PROMPT.format(
        title=item.title_tldr or item.title,
        category=item.main_category or "（未分类）",
        summary=item.summary or "（无摘要）",
        key_points=key_points,
    )
    text = llm.complete(prompt).strip()
    # Strip an accidental leading label if the model adds one.
    for prefix in ("推荐理由：", "推荐理由:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text
