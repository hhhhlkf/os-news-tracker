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
            "title_tldr": "Linux 6.9 发布",
            "summary": "内核 6.9 发布，带来调度器改进。",
            "key_points": ["调度器改进", "更好能效"],
            "info_type": "发布",
            "importance": "高",
            "why_it_matters": "影响服务器性能。",
            "main_category": "OS性能发展",
            "sub_tags": ["kernel", "scheduler"],
            "entities": [{"type": "os", "name": "Linux"}, {"type": "version", "name": "6.9"}],
            "confidence": 0.92,
        },
        ensure_ascii=False,
    )


def test_enricher_parses_structured_output():
    n = NormalizedItem(
        source_id=1,
        title="Linux 6.9",
        url="https://x/a",
        canonical_url="https://x/a",
        clean_content="body",
    )
    enricher = Enricher(llm=_StubLlm(_valid_payload()))
    fields = enricher.enrich(n)
    assert fields.title_tldr == "Linux 6.9 发布"
    assert fields.info_type == "发布"
    assert fields.main_category == "OS性能发展"
    assert {e.name for e in fields.entities} == {"Linux", "6.9"}


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
