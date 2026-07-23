from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from app.enums import InfoType, Importance
from app.mail.validation import normalize_recipients


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
    child_tag_id: int | None = None
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


class ManualNewsRunRequest(BaseModel):
    time_mode: TimeMode
    relative_range: RelativeRange | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    target_count: int = Field(gt=0, le=500)
    # Token accounting: manual = multi-method batch UI; manual_method = single-method run.
    trigger_type: Literal["manual", "manual_method"] | None = None
    # Shared id for one「抓取选中」batch so token charts can stack methods together.
    batch_id: str | None = Field(default=None, max_length=64)

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


MailRelativeRange = Literal["24h", "7d", "30d"]
MailSortBy = Literal["published_at", "fetched_at"]
MailSortDir = Literal["desc", "asc"]
MailBoundaryMode = Literal["none", "absolute", "relative"]
MailProviderKind = Literal["tof4", "smtp"]


class MailFilterSnapshot(BaseModel):
    q: str | None = None
    main_category: str | None = None
    info_type: str | None = None
    importance: str | None = None
    sub_tag: str | None = None
    source_id: str | None = None
    sort_by: MailSortBy = "published_at"
    sort_dir: MailSortDir = "desc"
    published_after_mode: MailBoundaryMode = "none"
    published_after_value: MailRelativeRange | None = None
    published_after: str | None = None
    published_before_mode: MailBoundaryMode = "none"
    published_before_value: MailRelativeRange | None = None
    published_before: str | None = None
    fetched_after_mode: MailBoundaryMode = "none"
    fetched_after_value: MailRelativeRange | None = None
    fetched_after: str | None = None
    fetched_before_mode: MailBoundaryMode = "none"
    fetched_before_value: MailRelativeRange | None = None
    fetched_before: str | None = None


class MailNoticeBlock(BaseModel):
    doc_text: str = ""
    website_url: str = ""


class MailNoticeConfigResponse(BaseModel):
    doc_text: str = ""
    website_url: str = ""
    include_on_send: bool = False
    include_on_template: bool = False


class MailNoticeConfigUpdateRequest(BaseModel):
    doc_text: str | None = None
    website_url: str | None = None
    include_on_send: bool | None = None
    include_on_template: bool | None = None


class MailTemplateCreateRequest(BaseModel):
    name: str
    subject: str
    recipients: list[str] = Field(default_factory=list)
    filter_snapshot: MailFilterSnapshot = Field(default_factory=MailFilterSnapshot)
    is_active: bool = True

    @field_validator("recipients", mode="before")
    @classmethod
    def _validate_recipients(cls, value: object) -> list[str]:
        return normalize_recipients(value if isinstance(value, list) else [])


class MailTemplateResponse(BaseModel):
    id: int
    name: str
    subject: str
    recipients: list[str] = Field(default_factory=list)
    filter_snapshot: MailFilterSnapshot
    notice: MailNoticeBlock | None = None
    is_active: bool
    last_send_at: datetime | None = None
    last_send_status: str | None = None
    last_send_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MailTemplateUpdateRequest(BaseModel):
    name: str | None = None
    subject: str | None = None
    recipients: list[str] | None = None
    filter_snapshot: MailFilterSnapshot | None = None
    is_active: bool | None = None

    @field_validator("recipients", mode="before")
    @classmethod
    def _validate_recipients(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        return normalize_recipients(value if isinstance(value, list) else [])


class MailTemplateActionRequest(BaseModel):
    provider: MailProviderKind | None = None


class MailImmediatePreviewRequest(BaseModel):
    subject: str
    recipients: list[str] = Field(default_factory=list)
    filter_snapshot: MailFilterSnapshot = Field(default_factory=MailFilterSnapshot)
    provider: MailProviderKind | None = None

    @field_validator("recipients", mode="before")
    @classmethod
    def _validate_recipients(cls, value: object) -> list[str]:
        return normalize_recipients(value if isinstance(value, list) else [])


class MailPreviewItem(BaseModel):
    title: str
    reason: str | None = None
    summary: str | None = None
    importance: str | None = None
    key_points: list[str] = Field(default_factory=list)
    hotspots: list[str] = Field(default_factory=list)
    source_url: str
    published_at: str | None = None
    source_name: str | None = None
    source_quality_score: int | None = None
    source_quality_grade: str | None = None
    source_quality_status: str | None = None


class MailPreviewResponse(BaseModel):
    subject: str
    filter_snapshot: MailFilterSnapshot
    recipients: list[str] = Field(default_factory=list)
    provider: MailProviderKind
    item_count: int
    items: list[MailPreviewItem] = Field(default_factory=list)
    rendered_html: str
    notice: MailNoticeBlock | None = None


class MailImmediateSendResponse(BaseModel):
    delivery_id: int
    provider: MailProviderKind
    status: str
    item_count: int
    error_message: str | None = None


MailFrequency = Literal["daily", "weekly"]


class MailScheduleCreateRequest(BaseModel):
    name: str
    subject: str
    recipients: list[str] = Field(default_factory=list)
    filter_snapshot: MailFilterSnapshot = Field(default_factory=MailFilterSnapshot)
    frequency: MailFrequency = "daily"
    send_time: str = "09:00"
    enabled: bool = True
    template_id: int | None = None

    @field_validator("recipients", mode="before")
    @classmethod
    def _validate_recipients(cls, value: object) -> list[str]:
        return normalize_recipients(value if isinstance(value, list) else [])


class MailScheduleUpdateRequest(BaseModel):
    name: str | None = None
    subject: str | None = None
    recipients: list[str] | None = None
    filter_snapshot: MailFilterSnapshot | None = None
    frequency: MailFrequency | None = None
    send_time: str | None = None
    enabled: bool | None = None

    @field_validator("recipients", mode="before")
    @classmethod
    def _validate_recipients(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        return normalize_recipients(value if isinstance(value, list) else [])


class MailScheduleResponse(BaseModel):
    id: int
    template_id: int | None = None
    template_name: str | None = None
    name: str
    subject: str
    recipients: list[str] = Field(default_factory=list)
    filter_snapshot: MailFilterSnapshot
    frequency: MailFrequency
    send_time: str
    enabled: bool
    last_sent_at: datetime | None = None
    last_result_status: str | None = None
    last_result_count: int | None = None
    last_sent_marker_date: str | None = None
    next_run_at: datetime | None = None
    patrol_status: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MailDeliveryLog(BaseModel):
    id: int
    trigger_type: str
    status: str
    item_count: int
    subject: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None


# --- System morning crawl ---

MorningCrawlFrequency = Literal["daily", "weekly"]
MorningCrawlLookback = Literal["24h", "7d", "30d", "all"]


class MorningCrawlConfigResponse(BaseModel):
    enabled: bool
    run_time: str
    frequency: MorningCrawlFrequency
    lookback_window: MorningCrawlLookback
    patrol_interval_hours: int
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    last_success_date: str | None = None
    next_run_at: datetime | None = None


class MorningCrawlConfigUpdateRequest(BaseModel):
    enabled: bool | None = None
    run_time: str | None = None
    frequency: MorningCrawlFrequency | None = None
    lookback_window: MorningCrawlLookback | None = None
    patrol_interval_hours: int | None = Field(default=None, ge=1, le=24)


class MorningCrawlRunSummary(BaseModel):
    id: int
    trigger_type: str
    status: str
    run_date: str | None = None
    total_methods: int
    success_methods: int
    failed_methods: int
    stored_count: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None


class MorningCrawlDashboardResponse(BaseModel):
    config: MorningCrawlConfigResponse
    active_method_count: int
    today_status: str          # not_run | running | success | partial | failed
    today_run: MorningCrawlRunSummary | None = None
    recent_runs: list[MorningCrawlRunSummary] = Field(default_factory=list)
    is_running: bool = False


class MorningCrawlRunMethodDetail(BaseModel):
    id: int
    method_id: int | None = None
    domain: str | None = None
    status: str
    discovered_count: int
    stored_count: int
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class MorningCrawlRunDetailResponse(BaseModel):
    run: MorningCrawlRunSummary
    methods: list[MorningCrawlRunMethodDetail] = Field(default_factory=list)
