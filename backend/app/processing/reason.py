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

_OS_INSIGHT_PROMPT = """你是面向操作系统（OS）研发与维护工程师的技术情报分析师。
根据下面的文章信息，写一段「OS 启发」，提炼这条信息对操作系统设计、实现、运维或生态协作可带来的启发。

要求：
- 用中文，只写 1 句话，不超过 50 字，简洁有力。
- 聚焦 OS 本体：内核/发行版/包管理/兼容性/安全更新/构建与发布等可迁移的做法或警示。
- 不要重复「为什么值得读」；要写「对 OS 工作能学到什么 / 可怎么用」。
- 直接输出启发正文，不要加「OS启发：」或「OS 启发：」前缀，不要使用引号或 markdown。

标题：{title}
主分类：{category}
摘要：{summary}
技术要点：
{key_points}

只输出 OS 启发正文：
"""


def _item_prompt_fields(item: Item) -> dict[str, str]:
    key_points = "\n".join(item.key_points or []) or "（无）"
    return {
        "title": item.title_tldr or item.title,
        "category": item.main_category or "（未分类）",
        "summary": item.summary or "（无摘要）",
        "key_points": key_points,
    }


def _strip_label_prefixes(text: str, prefixes: tuple[str, ...]) -> str:
    for prefix in prefixes:
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def generate_recommendation_reason(item: Item, llm: LlmClient | None = None) -> str:
    """Generate a generic, maintainer-facing recommendation reason for an item."""
    llm = llm or LlmClient()
    text = llm.complete(_REASON_PROMPT.format(**_item_prompt_fields(item))).strip()
    return _strip_label_prefixes(text, ("推荐理由：", "推荐理由:"))


def generate_os_insight(item: Item, llm: LlmClient | None = None) -> str:
    """Generate an OS-focused insight sentence for an item (same workflow as reason)."""
    llm = llm or LlmClient()
    text = llm.complete(_OS_INSIGHT_PROMPT.format(**_item_prompt_fields(item))).strip()
    return _strip_label_prefixes(text, ("OS启发：", "OS启发:", "OS 启发：", "OS 启发:"))
