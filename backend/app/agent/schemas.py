from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class AgentSourceConfig:
    source_id: int
    focus_areas: list[str]
    topic_groups: list[str]
    crawl_depth: int = 1
    max_urls_per_run: int = 20
    quality_threshold: int = 4
    crawl_workers: int = 5
    quality_workers: int = 3
    summary_workers: int = 3


@dataclass
class PlanUrl:
    url: str
    guessed_topic: str = ""


@dataclass
class CrawlPlan:
    source_id: int
    urls: list[PlanUrl]


@dataclass
class RawPage:
    url: str
    guessed_topic: str
    title: str
    content: str   # cleaned text
    published_at: datetime | None = None   # 文章发布时间，由 extractor 提取


@dataclass
class QualityResult:
    score: int
    reason: str
    relevant_topic: str
    verdict: str            # keep | discard
    should_remember: bool


@dataclass
class QualifiedPage:
    page: RawPage
    verdict: str
    score: int


@dataclass
class AgentItem:
    source_id: int
    url: str
    title: str
    topic_group: str | None
    content_type: str       # article | release_note | benchmark | discussion | changelog
    importance: str         # 高 | 中 | 低
    body: str
    published_at: datetime | None = None   # 从抓取阶段透传，非 LLM 产出
    key_facts: list[str] = field(default_factory=list)
    sub_tags: list[str] = field(default_factory=list)
    merge_suggestions: list[dict[str, Any]] = field(default_factory=list)
