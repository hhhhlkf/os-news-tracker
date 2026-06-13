import json

from app.processing.enricher import Enricher
from app.schemas import NormalizedItem


class _StubLlm:
    def __init__(self, payload):
        self._payload = payload

    def complete(self, prompt, **kw):
        return self._payload


def _valid_payload():
    return json.dumps(
        {
            "title_zh": "Linux 6.9 正式发布",
            "summary": "内核 6.9 发布，带来调度器改进。",
            "tech_highlights": [
                "[调度器] sched_ext 框架正式合入主线",
                "[能效] 改善大小核调度策略",
            ],
            "info_type": "发布",
            "importance": "高",
            "main_category": "OS性能发展",
            "sub_tags": ["kernel", "scheduler"],
            "keywords": ["Linux 6.9", "sched_ext", "EAS"],
            "confidence": 0.92,
        },
        ensure_ascii=False,
    )


def test_enricher_parses_new_schema():
    n = NormalizedItem(
        source_id=1,
        title="Linux 6.9",
        url="https://x/a",
        canonical_url="https://x/a",
        clean_content="body",
    )
    enricher = Enricher(llm=_StubLlm(_valid_payload()))
    fields = enricher.enrich(n)
    assert fields.title_zh == "Linux 6.9 正式发布"
    assert fields.info_type == "发布"
    assert fields.main_category == "OS性能发展"
    assert len(fields.tech_highlights) == 2
    assert "[调度器]" in fields.tech_highlights[0]
    assert fields.keywords == ["Linux 6.9", "sched_ext", "EAS"]


def test_enricher_handles_markdown_fenced_json():
    n = NormalizedItem(
        source_id=1,
        title="t",
        url="https://x/a",
        canonical_url="https://x/a",
        clean_content="body",
    )
    fenced = "```json\n" + _valid_payload() + "\n```"
    fields = Enricher(llm=_StubLlm(fenced)).enrich(n)
    assert fields.confidence == 0.92


def test_enricher_prompt_contains_role_and_exclusions():
    prompts_sent: list[str] = []

    class _CaptureLlm:
        def complete(self, prompt, **kw):
            prompts_sent.append(prompt)
            return _valid_payload()

    n = NormalizedItem(
        source_id=1,
        title="t",
        url="https://x/a",
        canonical_url="https://x/a",
        clean_content="body",
    )
    Enricher(llm=_CaptureLlm()).enrich(n)
    prompt = prompts_sent[0]
    assert "技术情报分析师" in prompt
    assert "社区活动通知" in prompt
    assert "title_zh" in prompt
    assert "tech_highlights" in prompt
    assert "keywords" in prompt
    assert "why_it_matters" not in prompt
    assert "entities" not in prompt
