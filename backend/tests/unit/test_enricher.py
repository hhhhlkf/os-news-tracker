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
            "should_store": True,
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
    assert "should_store" in prompt
    assert "reject_reason" in prompt
    assert "信息不足" in prompt
    assert "why_it_matters" not in prompt
    assert "entities" not in prompt


def test_enricher_prompt_requires_aggregated_tagging_rules():
    prompts_sent: list[str] = []

    class _CaptureLlm:
        def complete(self, prompt, **kw):
            prompts_sent.append(prompt)
            return _valid_payload()

    n = NormalizedItem(
        source_id=1,
        title="openEuler kernel update",
        url="https://x/a",
        canonical_url="https://x/a",
        clean_content="body",
    )
    Enricher(llm=_CaptureLlm()).enrich(n)
    prompt = prompts_sent[0]
    assert "标签聚合规则" in prompt
    assert "canonical" in prompt
    assert "sub_tags 控制在 2-4 个" in prompt
    assert "最多保留 5 个标签" in prompt
    assert "不要把完整版本号" in prompt
    assert "OpenEuler/openEuler/欧拉" in prompt
    assert "同义写法" in prompt


def test_enricher_prompt_includes_existing_tags_and_merge_suggestions():
    prompts_sent: list[str] = []

    class _CaptureLlm:
        def complete(self, prompt, **kw):
            prompts_sent.append(prompt)
            return _valid_payload()

    n = NormalizedItem(
        source_id=1,
        title="openEuler kernel update",
        url="https://x/a",
        canonical_url="https://x/a",
        clean_content="body",
    )
    Enricher(llm=_CaptureLlm()).enrich(
        n,
        existing_tags=[
            {"id": 1, "name": "kernel", "usage_count": 12},
            {"id": 2, "name": "Linux Kernel", "usage_count": 4},
        ],
    )
    prompt = prompts_sent[0]
    assert '"id": 1' in prompt
    assert '"name": "kernel"' in prompt
    assert "优先复用 existing_tags" in prompt
    assert "merge_suggestions" in prompt
    assert "child_tag_id" in prompt
    assert "parent_tag_name" in prompt
    assert "默认 approved" in prompt


def test_enricher_prompt_requires_key_technology_news_filtering():
    prompts_sent: list[str] = []

    class _CaptureLlm:
        def complete(self, prompt, **kw):
            prompts_sent.append(prompt)
            return _valid_payload()

    n = NormalizedItem(
        source_id=1,
        title="Making sure you're not a bot",
        url="https://x/a",
        canonical_url="https://x/a",
        clean_content="Anubis uses Proof-of-Work to protect the website.",
    )
    Enricher(llm=_CaptureLlm()).enrich(n)
    prompt = prompts_sent[0]
    assert "关键技术新闻" in prompt
    assert "新兴技术" in prompt
    assert "跨社区影响" in prompt
    assert "厂商自身的小范围漏洞" in prompt
    assert "反爬挑战页" in prompt
    assert "Anubis" in prompt


def test_enricher_allows_rejection_payload():
    n = NormalizedItem(
        source_id=1,
        title="Anolis Developer Docs",
        url="https://gitee.com/anolis/docs",
        canonical_url="https://gitee.com/anolis/docs",
        clean_content="",
    )
    payload = json.dumps(
        {
            "title_zh": "Anolis 开发者文档",
            "summary": "页面缺少可供提炼的新闻或技术更新正文。",
            "tech_highlights": [],
            "info_type": "其他",
            "importance": "低",
            "main_category": "友商产品信息",
            "sub_tags": [],
            "keywords": [],
            "confidence": 0.12,
            "should_store": False,
            "reject_reason": "docs landing page without article-level technical update",
        },
        ensure_ascii=False,
    )
    fields = Enricher(llm=_StubLlm(payload)).enrich(n)
    assert fields.should_store is False
    assert "docs landing page" in (fields.reject_reason or "")
