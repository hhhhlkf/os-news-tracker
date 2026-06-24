from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from app.enums import InfoType, Importance


class RawItem(BaseModel):
    source_id: int
    title: str
    url: str
    raw_content: str | None = None
    published_at: datetime | None = None
    extra: dict | None = None


class ExtractedDoc(BaseModel):
    url: str
    title: str | None = None
    clean_content: str
    published_at: datetime | None = None


class TagMergeSuggestion(BaseModel):
    child_tag_id: int
    parent_tag_id: int | None = None
    parent_tag_name: str
    reason: str | None = None
    confidence: float = 0.0


class EnrichedFields(BaseModel):
    title_zh: str
    summary: str
    tech_highlights: list[str] = Field(default_factory=list)
    info_type: InfoType
    importance: Importance
    main_category: str
    sub_tags: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    merge_suggestions: list[TagMergeSuggestion] = Field(default_factory=list)
    confidence: float = 0.0
    should_store: bool = True
    reject_reason: str | None = None



class NormalizedItem(BaseModel):
    source_id: int
    title: str
    url: str
    canonical_url: str
    clean_content: str
    published_at: datetime | None = None


RelativeRange = Literal["24h", "7d", "30d"]
TimeMode = Literal["relative", "absolute"]
RunState = Literal[
    "idle", "collecting", "processing", "stopping",
    "completed", "failed", "stopped",
]


class ManualNewsRunRequest(BaseModel):
    time_mode: TimeMode
    relative_range: RelativeRange | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    target_count: int = Field(gt=0, le=500)

    @model_validator(mode="after")
    def validate_mode(self):
        if self.time_mode == "relative" and self.relative_range is None:
            raise ValueError("relative_range is required for relative mode")
        if self.time_mode == "absolute":
            self.relative_range = None
            if self.start_at is None or self.end_at is None:
                raise ValueError("start_at and end_at are required for absolute mode")
            if self.start_at > self.end_at:
                raise ValueError("start_at must be before end_at")
            if self.start_at.tzinfo is None:
                raise ValueError("start_at must be timezone-aware (UTC)")
            if self.end_at.tzinfo is None:
                raise ValueError("end_at must be timezone-aware (UTC)")
        return self


class AgentCrawlRunRequest(BaseModel):
    time_mode: TimeMode = "relative"
    relative_range: RelativeRange | None = "7d"
    start_at: datetime | None = None
    end_at: datetime | None = None
    target_count: int | None = Field(default=None, gt=0, le=500)

    @model_validator(mode="after")
    def validate_mode(self):
        if self.time_mode == "relative" and self.relative_range is None:
            raise ValueError("relative_range is required for relative mode")
        if self.time_mode == "absolute":
            self.relative_range = None
            if self.start_at is None or self.end_at is None:
                raise ValueError("start_at and end_at are required for absolute mode")
            if self.start_at > self.end_at:
                raise ValueError("start_at must be before end_at")
            if self.start_at.tzinfo is None:
                raise ValueError("start_at must be timezone-aware (UTC)")
            if self.end_at.tzinfo is None:
                raise ValueError("end_at must be timezone-aware (UTC)")
        return self


class TimeFilterStats(BaseModel):
    """Per-category counts from time-window filtering.

    These are tallied across all sources during candidate collection so
    developers can quickly diagnose *why* a manual run produced too few
    (or zero) candidates.
    """

    missing_published_at: int = 0
    before_start: int = 0
    after_end: int = 0
    matched: int = 0
    included_without_date: int = 0


class ManualNewsRunStatus(BaseModel):
    state: RunState
    time_mode: TimeMode | None = None
    relative_range: RelativeRange | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    target_count: int | None = None
    discovered_count: int = 0
    queued_count: int = 0
    processed_count: int = 0
    saved_count: int = 0
    fulfilled: bool = False
    gap_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    time_filter_stats: TimeFilterStats | None = None
