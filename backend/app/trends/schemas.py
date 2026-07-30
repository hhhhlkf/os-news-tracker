from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TrendTriggerMode = Literal["manual", "scheduled"]
TrendWindowMode = Literal["date_range", "relative"]
TrendRelativeWindowUnit = Literal["week", "month"]
TrendEmbeddingProvider = Literal["local_qwen", "openai_compatible"]
TrendEmbeddingStatusValue = Literal[
    "not_installed",
    "downloading",
    "ready",
    "loading",
    "processing",
    "failed",
]
TrendCardListStatus = Literal["pending", "ready", "skipped", "failed"]
StorylineMembership = Literal["core", "supporting", "duplicate"]
StorylineReviewDecision = Literal["accept", "split", "reject"]
StorylineReviewStatus = Literal["pending", "running", "accepted", "split", "rejected", "failed"]


class TrendIdentityTemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    identity_text: str = Field(min_length=1, max_length=300)

    @field_validator("name", "identity_text")
    @classmethod
    def validate_non_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class TrendIdentityTemplateResponse(BaseModel):
    template_id: str
    name: str
    identity_text: str
    created_at: date


class TrendCarouselTemplateResponse(BaseModel):
    """Public template metadata used to choose a carousel."""

    template_id: str
    name: str


class TrendSettingsUpdateRequest(BaseModel):
    window_mode: TrendWindowMode = "relative"
    window_start_date: date | None = None
    window_end_date: date | None = None
    relative_window_unit: TrendRelativeWindowUnit = "week"
    relative_window_value: int = Field(default=4, ge=1, le=104)
    trend_count: int = Field(default=5, ge=1, le=100)
    storyline_candidate_goal: int = Field(default=20, ge=1, le=100)
    trigger_mode: TrendTriggerMode = "manual"
    schedule_rule: str | None = Field(default=None, max_length=100)
    scheduled_template_id: str | None = Field(default=None, max_length=36)

    @field_validator("schedule_rule")
    @classmethod
    def normalize_schedule_rule(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def validate_date_window(self) -> "TrendSettingsUpdateRequest":
        if self.window_mode == "date_range":
            if self.window_start_date is None or self.window_end_date is None:
                raise ValueError("window_start_date and window_end_date are required for date_range mode")
            if self.window_start_date > self.window_end_date:
                raise ValueError("window_start_date must be on or before window_end_date")
        else:
            self.window_start_date = None
            self.window_end_date = None
        if self.trigger_mode == "scheduled":
            if not self.schedule_rule:
                raise ValueError("schedule_rule is required for scheduled trigger mode")
            if not self.scheduled_template_id:
                raise ValueError("scheduled_template_id is required for scheduled trigger mode")
        return self


class TrendSettingsResponse(TrendSettingsUpdateRequest):
    pass


class TrendEmbeddingStatusResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    provider: TrendEmbeddingProvider
    model_id: str
    model_revision: str
    model_version: str | None
    embedding_version: str
    dimension: int
    normalization: str
    cache_dir: str
    cache_installed: bool
    status: TrendEmbeddingStatusValue
    error_message: str | None
    remedy: str | None
    worker_base_url: str
    worker_reachable: bool
    worker_active: bool
    worker_error: str | None
    status_updated_at: datetime


class NewsExplanationContent(BaseModel):
    """Strict LLM output contract for the card stage."""

    model_config = ConfigDict(extra="forbid")

    news_actor: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=100)
    result: str = Field(min_length=1, max_length=100)
    potential_impact: str = Field(min_length=1, max_length=100)
    cause: str = Field(min_length=1, max_length=100)

    @field_validator("news_actor", "action", "result", "potential_impact", "cause")
    @classmethod
    def validate_card_field(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空或只包含空白字符")
        return value


class TrendCardBackfillRequest(BaseModel):
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def validate_date_range(self) -> "TrendCardBackfillRequest":
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date 和 end_date 必须同时提供")
        if self.start_date is not None and self.end_date is not None and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self


class TrendVectorBackfillRequest(TrendCardBackfillRequest):
    """Uses the identical business-date window contract as news-card backfill."""


class TrendVectorStageStatusResponse(BaseModel):
    is_running: bool
    start_date: date
    end_date: date
    embedding_version: str
    pending_count: int
    generated_count: int
    failed_count: int
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    retry_guidance: str | None
    message: str | None = None


class TrendClusterRunRequest(TrendCardBackfillRequest):
    """The same resolved window is used for vector readiness and clustering."""


class TrendClusterStageStatusResponse(BaseModel):
    is_running: bool
    start_date: date
    end_date: date
    embedding_version: str
    candidate_goal: int
    max_cluster_size: int
    pending_vector_count: int
    generated_vector_count: int
    vector_failed_count: int
    candidate_cluster_count: int
    pending_match_count: int
    used_threshold: float | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    retry_guidance: str | None
    message: str | None = None


class TrendCandidateClusterListItemResponse(BaseModel):
    candidate_cluster_id: str
    member_count: int
    cohesion_score: float
    threshold: float


class TrendCandidateClusterListResponse(BaseModel):
    start_date: date
    end_date: date
    total: int
    offset: int
    limit: int
    items: list[TrendCandidateClusterListItemResponse]


class StorylineReviewMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    card_id: str = Field(min_length=1, max_length=36)
    membership: StorylineMembership


class StorylineReviewStoryline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    members: list[StorylineReviewMember] = Field(min_length=1, max_length=12)
    agent_review: str = Field(min_length=1, max_length=1000)

    @field_validator("title", "agent_review")
    @classmethod
    def strip_review_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空或只包含空白字符")
        return value


class StorylineReviewOutput(BaseModel):
    """The strict contract returned by the storyline-review Agent."""

    model_config = ConfigDict(extra="forbid")

    decision: StorylineReviewDecision
    storylines: list[StorylineReviewStoryline] = Field(max_length=12)
    removed_card_ids: list[str] = Field(default_factory=list, max_length=12)
    agent_review: str = Field(min_length=1, max_length=1500)

    @field_validator("removed_card_ids")
    @classmethod
    def validate_removed_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("removed_card_ids 不能重复")
        if any(not card_id.strip() for card_id in value):
            raise ValueError("removed_card_ids 不能包含空值")
        return value

    @field_validator("agent_review")
    @classmethod
    def strip_overall_review(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("agent_review 不能为空")
        return value

    @model_validator(mode="after")
    def validate_decision_shape(self) -> "StorylineReviewOutput":
        if self.decision == "reject" and self.storylines:
            raise ValueError("reject 时 storylines 必须为空")
        if self.decision == "accept" and len(self.storylines) != 1:
            raise ValueError("accept 时必须且只能输出一条 storyline")
        if self.decision == "split" and len(self.storylines) < 2:
            raise ValueError("split 时必须输出至少两条 storyline")
        return self


class TrendStorylineReviewRequest(TrendCardBackfillRequest):
    """Triggers review for the current versioned candidate set."""


class TrendStorylineStageStatusResponse(BaseModel):
    is_running: bool
    start_date: date
    end_date: date
    embedding_version: str
    pending_count: int
    accepted_count: int
    split_count: int
    rejected_count: int
    failed_count: int
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    retry_guidance: str | None
    message: str | None = None


class TrendStorylineReviewListItemResponse(BaseModel):
    review_id: str
    candidate_cluster_id: str | None
    status: StorylineReviewStatus
    decision: StorylineReviewDecision | None
    member_card_ids: list[str]
    removed_card_ids: list[str]
    agent_review: str | None
    error_message: str | None
    attempt_count: int
    created_at: datetime
    completed_at: datetime | None


class TrendStorylineReviewListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[TrendStorylineReviewListItemResponse]


class TrendStorylineMemberResponse(BaseModel):
    card_id: str
    at: date
    membership: StorylineMembership


class TrendStorylineListItemResponse(BaseModel):
    storyline_id: str
    title: str
    overall_start_date: date
    overall_end_date: date
    overall_influence_score: float
    cohesion_score: float
    decision: Literal["accept", "split"]
    status: Literal["active", "archived"]
    agent_review: str
    members: list[TrendStorylineMemberResponse]


class TrendStorylineListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[TrendStorylineListItemResponse]


class TrendCardStageStatusResponse(BaseModel):
    is_running: bool
    start_date: date
    end_date: date
    pending_count: int
    generated_count: int
    skipped_count: int
    failed_count: int
    card_prompt_version: str
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    retry_guidance: str | None
    message: str | None = None


class TrendCardListItemResponse(BaseModel):
    item_id: int
    title: str
    published_at: datetime | None
    fetched_at: datetime
    status: TrendCardListStatus
    news_actor: str | None
    action: str | None
    result: str | None
    potential_impact: str | None
    cause: str | None
    skip_reason: str | None
    error_message: str | None
    attempt_count: int
    card_updated_at: datetime | None


class TrendCardListResponse(BaseModel):
    start_date: date
    end_date: date
    total: int
    offset: int
    limit: int
    items: list[TrendCardListItemResponse]


TrendCategory = Literal[
    "emerging_trend",
    "hot_event",
    "periodic_activity",
    "attention_declining",
    "unverified_change",
]
TrendRunStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]

TREND_CATEGORY_LABELS: dict[str, str] = {
    "emerging_trend": "新兴趋势",
    "hot_event": "热点事件",
    "periodic_activity": "周期性活动",
    "attention_declining": "注意力持续下降",
    "unverified_change": "无法形成可验证的共同变化",
}


class TrendEvaluationOutput(BaseModel):
    """Strict contract returned by the template trend-evaluation Agent."""

    model_config = ConfigDict(extra="forbid")

    category: TrendCategory
    topic: str | None = Field(default=None, max_length=300)
    trend_summary: str | None = None
    template_relevance_score: float = Field(ge=0, le=100)
    agent_review: str = Field(min_length=1, max_length=2000)

    @field_validator("topic", "trend_summary", mode="before")
    @classmethod
    def strip_nullable_text(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("agent_review")
    @classmethod
    def validate_agent_review(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("agent_review 不能为空")
        return value

    @model_validator(mode="after")
    def validate_category_shape(self) -> "TrendEvaluationOutput":
        if self.category == "unverified_change":
            if self.topic is not None or self.trend_summary is not None:
                raise ValueError("unverified_change 时 topic 与 trend_summary 必须为 null")
        else:
            if not self.topic:
                raise ValueError(f"{self.category} 必须提供中文 topic")
            if not self.trend_summary:
                raise ValueError(f"{self.category} 必须提供中文 trend_summary")
        return self


class TrendRunTriggerRequest(BaseModel):
    template_id: str = Field(min_length=1, max_length=36)


class TrendRunStatusResponse(BaseModel):
    run_id: str | None
    template_id: str | None
    is_running: bool
    status: TrendRunStatus | None
    window_start_date: date | None
    window_end_date: date | None
    trend_count: int | None
    storyline_candidate_goal: int | None
    candidate_count: int
    completed_candidate_count: int
    result_count: int
    unverified_count: int
    reusable: bool = False
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    retry_guidance: str | None
    message: str | None = None


class TrendRunListItemResponse(BaseModel):
    run_id: str
    template_id: str
    status: TrendRunStatus
    window_start_date: date
    window_end_date: date
    trend_count: int
    candidate_count: int
    completed_candidate_count: int
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class TrendRunListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[TrendRunListItemResponse]


class TrendResultItemResponse(BaseModel):
    """One exact news reference; ``title`` is display-only, filtering uses ``item_id``."""

    item_id: int
    title: str


class TrendResultResponse(BaseModel):
    result_id: str
    run_id: str
    storyline_id: str
    overall_start_date: date
    overall_end_date: date
    window_start_date: date
    window_end_date: date
    overall_score: float
    window_score: float
    template_relevance_score: float
    trend_rank_score: float
    category: TrendCategory
    topic: str | None
    trend_summary: str | None
    agent_review: str
    item_ids: list[int]
    sources: list[TrendResultItemResponse]


class TrendLatestResultsResponse(BaseModel):
    template_id: str
    run_id: str | None
    window_start_date: date | None
    window_end_date: date | None
    trend_count: int | None
    status: TrendRunStatus | None
    finished_at: datetime | None
    items: list[TrendResultResponse]
    message: str | None = None


class TrendCarouselItemResponse(BaseModel):
    """One verified trend card shown in the news-page carousel."""

    result_id: str
    storyline_id: str
    category: TrendCategory
    category_label: str
    topic: str
    trend_summary: str
    trend_rank_score: float
    window_start_date: date
    window_end_date: date
    window_item_count: int
    item_ids: list[int]
    sources: list[TrendResultItemResponse]


class TrendCarouselResponse(BaseModel):
    template_id: str
    run_id: str | None
    window_start_date: date | None
    window_end_date: date | None
    trend_count: int
    finished_at: datetime | None
    items: list[TrendCarouselItemResponse]
    message: str | None = None


TrendScheduleRunStatus = Literal["running", "succeeded", "failed", "skipped", "reused"]


class TrendScheduleRunResponse(BaseModel):
    schedule_run_id: str
    schedule_rule: str
    scheduled_for: datetime
    template_id: str | None
    template_name: str | None
    trend_run_id: str | None
    status: TrendScheduleRunStatus
    stage: str | None
    detail: str | None
    window_start_date: date | None
    window_end_date: date | None
    trend_count: int | None
    storyline_candidate_goal: int | None
    started_at: datetime | None
    finished_at: datetime | None


class TrendScheduleStatusResponse(BaseModel):
    """Workbench view of the single global schedule rule and its last outcome."""

    enabled: bool
    trigger_mode: TrendTriggerMode
    schedule_rule: str | None
    schedule_rule_label: str | None
    scheduled_template_id: str | None
    scheduled_template_name: str | None
    is_running: bool
    current_stage: str | None
    next_run_at: datetime | None
    skip_reason: str | None
    last_run: TrendScheduleRunResponse | None
    message: str | None = None
