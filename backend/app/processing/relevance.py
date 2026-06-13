from typing import Protocol


class _Completer(Protocol):
    def complete(self, prompt: str) -> str: ...


_PROMPT = (
    "你是操作系统维护团队的情报筛选员。判断以下内容是否值得 OS maintainer 关注。\n"
    "收录标准：版本发布、安全公告、软件包更新、新技术/工具发布、性能数据、AI agent/LLM 工具链进展。\n"
    "排除标准：纯社区活动通知、招聘、用户入门教程、市场营销、非技术公告。\n"
    "关键词：{keywords}\n"
    "标题：{title}\n"
    "正文片段：{snippet}\n"
    "只回答 true 或 false。"
)


def llm_relevance(title: str, content: str, keywords: str, client: _Completer | None = None) -> bool:
    if client is None:
        from app.llm.client import LlmClient

        client = LlmClient()
    prompt = _PROMPT.format(keywords=keywords, title=title or "", snippet=content[:800])
    answer = client.complete(prompt).strip().lower()
    return answer.startswith("true")
