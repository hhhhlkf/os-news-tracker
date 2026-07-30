from typing import Protocol

from app.discovery.prompts import render_prompt


class _Completer(Protocol):
    def complete(self, prompt: str) -> str: ...


_PROMPT = (
    "你是操作系统维护团队的情报筛选员。判断以下内容是否值得 OS maintainer 关注。\n"
    "收录标准：与 OS 维护高度相关且具有明确技术或决策价值的关键版本发布、重大兼容性变化、重要基础设施/工具链演进、事实扎实的新技术方向、显著性能变化。\n"
    "内核实现分析、驱动机制、子系统原理和教程式文章，除非明确包含主线发布、重大兼容性变化、显著性能影响或维护动作价值，否则排除。\n"
    "漏洞/CVE/安全公告默认从严：仅当正文证明跨社区、跨发行版、上游广泛影响、供应链风险或显著维护决策价值时收录；普通单产品漏洞、例行安全公告和 CVE 罗列排除。\n"
    "排除标准：纯社区活动通知、招聘、用户入门教程、市场营销、非技术公告、一般性实现解读或局部优化。不要只凭标题中的发布、更新、CVE、AI 或性能词判断。\n"
    "关键词：{keywords}\n"
    "标题：{title}\n"
    "正文片段：{snippet}\n"
    "只回答 true 或 false。"
)


def llm_relevance(title: str, content: str, keywords: str, client: _Completer | None = None) -> bool:
    if client is None:
        from app.llm.client import LlmClient

        client = LlmClient()
    prompt = render_prompt(
        "relevance_filter",
        _PROMPT,
        {
            "{keywords}": keywords,
            "{title}": title or "",
            "{snippet}": content[:800],
        },
    )
    answer = client.complete(prompt).strip().lower()
    return answer.startswith("true")
