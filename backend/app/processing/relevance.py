from typing import Protocol


class _Completer(Protocol):
    def complete(self, prompt: str) -> str: ...


_PROMPT = (
    "判断下面网页内容是否与关键词「{keywords}」相关的技术新闻。"
    "只回答 true 或 false。\n标题：{title}\n正文片段：{snippet}"
)


def llm_relevance(title: str, content: str, keywords: str, client: _Completer | None = None) -> bool:
    if client is None:
        from app.llm.client import LlmClient

        client = LlmClient()
    prompt = _PROMPT.format(keywords=keywords, title=title or "", snippet=content[:800])
    answer = client.complete(prompt).strip().lower()
    return answer.startswith("true")
