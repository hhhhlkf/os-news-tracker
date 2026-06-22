from dataclasses import dataclass, field
from datetime import datetime


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
    key_facts: list[str] = field(default_factory=list)
