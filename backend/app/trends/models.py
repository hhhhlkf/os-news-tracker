from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class TrendIdentityTemplate(Base):
    """An immutable perspective used by the trend opinion layer."""

    __tablename__ = "trend_identity_templates"

    template_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    identity_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())


class TrendSettings(Base):
    """Singleton global controls for future trend runs and the workbench."""

    __tablename__ = "trend_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_trend_settings_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    window_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="relative")
    window_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    relative_window_unit: Mapped[str] = mapped_column(String(10), nullable=False, default="week")
    relative_window_value: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    trend_count: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    storyline_candidate_goal: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    trigger_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    schedule_rule: Mapped[str | None] = mapped_column(String(100), nullable=True)
    scheduled_template_id: Mapped[str | None] = mapped_column(
        ForeignKey("trend_identity_templates.template_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())
    updated_at: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        server_default=func.current_date(),
        onupdate=func.current_date(),
    )


class TrendEmbeddingModelState(Base):
    """Singleton readiness state of the embedding model behind the fact layer."""

    __tablename__ = "trend_embedding_model_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_trend_embedding_model_state_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(100), nullable=False)
    # Resolved snapshot revision of the cached weights; unknown before download.
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_version: Mapped[str] = mapped_column(String(200), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    normalization: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_installed")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    remedy: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class NewsExplanationCard(Base):
    """One versioned fact card for one stored news item."""

    __tablename__ = "news_explanation_cards"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ready', 'failed', 'skipped')",
            name="ck_news_explanation_cards_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_news_explanation_cards_attempt_count"),
    )

    card_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    news_actor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result: Mapped[str | None] = mapped_column(String(100), nullable=True)
    potential_impact: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cause: Mapped[str | None] = mapped_column(String(100), nullable=True)
    at: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    card_prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class NewsCardEmbedding(Base):
    """One portable, versioned 1024-dim vector for an explanation card."""

    __tablename__ = "news_card_embeddings"

    card_id: Mapped[str] = mapped_column(
        ForeignKey("news_explanation_cards.card_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # JSON is intentional: SQLite tests receive a native list[float], while
    # PostgreSQL stores the same portable value as jsonb without pgvector.
    embedding: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_version: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    normalized: Mapped[bool] = mapped_column(Boolean, nullable=False)
    generated_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TrendCandidateCluster(Base):
    """Latest deterministic candidate set awaiting a future Agent review stage."""

    __tablename__ = "trend_candidate_clusters"

    candidate_cluster_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    embedding_version: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    cohesion_score: Mapped[float] = mapped_column(Float, nullable=False)
    max_cluster_size: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    window_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    generated_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TrendCandidateClusterMember(Base):
    __tablename__ = "trend_candidate_cluster_members"

    candidate_cluster_id: Mapped[str] = mapped_column(
        ForeignKey("trend_candidate_clusters.candidate_cluster_id", ondelete="CASCADE"),
        primary_key=True,
    )
    card_id: Mapped[str] = mapped_column(
        ForeignKey("news_explanation_cards.card_id", ondelete="CASCADE"),
        primary_key=True,
    )
    member_order: Mapped[int] = mapped_column(Integer, nullable=False)


class Storyline(Base):
    """A durable fact-layer storyline, intentionally independent of candidate clusters."""

    __tablename__ = "storylines"
    __table_args__ = (
        CheckConstraint("decision IN ('accept', 'split')", name="ck_storylines_decision"),
        CheckConstraint("status IN ('active', 'archived')", name="ck_storylines_status"),
    )

    storyline_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    overall_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    overall_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    window_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    cluster_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    cohesion_score: Mapped[float] = mapped_column(Float, nullable=False)
    overall_influence_score: Mapped[float] = mapped_column(Float, nullable=False)
    window_influence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    agent_review: Mapped[str] = mapped_column(Text, nullable=False)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("trend_storyline_reviews.review_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    last_member_at: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class StorylineMember(Base):
    __tablename__ = "storyline_members"
    __table_args__ = (
        CheckConstraint("membership IN ('core', 'supporting', 'duplicate')", name="ck_storyline_members_membership"),
    )

    storyline_id: Mapped[str] = mapped_column(
        ForeignKey("storylines.storyline_id", ondelete="CASCADE"), primary_key=True
    )
    card_id: Mapped[str] = mapped_column(
        ForeignKey("news_explanation_cards.card_id", ondelete="CASCADE"), primary_key=True
    )
    at: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    membership: Mapped[str] = mapped_column(String(20), nullable=False)


class StorylineSnapshot(Base):
    __tablename__ = "storyline_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    storyline_id: Mapped[str] = mapped_column(
        ForeignKey("storylines.storyline_id", ondelete="CASCADE"), nullable=False, index=True
    )
    review_id: Mapped[str] = mapped_column(
        ForeignKey("trend_storyline_reviews.review_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    at: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())
    time_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    time_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    card_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    memberships: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    influence_score: Mapped[float] = mapped_column(Float, nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)


class TrendStorylineReview(Base):
    """Audit record keyed by a stable member-set hash, not an ephemeral cluster FK."""

    __tablename__ = "trend_storyline_reviews"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'accepted', 'split', 'rejected', 'failed')",
            name="ck_trend_storyline_reviews_status",
        ),
        CheckConstraint("decision IS NULL OR decision IN ('accept', 'split', 'reject')", name="ck_trend_storyline_reviews_decision"),
    )

    review_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    review_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # Deliberately not a foreign key: candidate clusters are replaced wholesale.
    candidate_cluster_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    embedding_version: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    window_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    window_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    member_card_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    removed_card_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    agent_review: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TrendRun(Base):
    """One opinion-layer evaluation attempt for a single identity template."""

    __tablename__ = "trend_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_trend_runs_status",
        ),
        CheckConstraint("trend_count >= 1", name="ck_trend_runs_trend_count"),
        CheckConstraint("storyline_candidate_goal >= 1", name="ck_trend_runs_storyline_candidate_goal"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    template_id: Mapped[str] = mapped_column(
        ForeignKey("trend_identity_templates.template_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    window_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    window_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    trend_count: Mapped[int] = mapped_column(Integer, nullable=False)
    storyline_candidate_goal: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_version: Mapped[str] = mapped_column(String(200), nullable=False)
    card_prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    trend_prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model_version: Mapped[str] = mapped_column(String(200), nullable=False)
    candidate_storyline_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TrendResult(Base):
    """One published storyline conclusion belonging to a successful template run."""

    __tablename__ = "trend_results"
    __table_args__ = (
        CheckConstraint(
            "category IN ('emerging_trend', 'hot_event', 'periodic_activity', 'attention_declining', 'unverified_change')",
            name="ck_trend_results_category",
        ),
    )

    result_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("trend_runs.run_id", ondelete="CASCADE"), nullable=False, index=True
    )
    storyline_id: Mapped[str] = mapped_column(
        ForeignKey("storylines.storyline_id", ondelete="CASCADE"), nullable=False, index=True
    )
    overall_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    overall_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    window_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    window_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    window_score: Mapped[float] = mapped_column(Float, nullable=False)
    template_relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    trend_rank_score: Mapped[float] = mapped_column(Float, nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    topic: Mapped[str | None] = mapped_column(String(300), nullable=True)
    trend_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_review: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TrendResultItem(Base):
    """Exact item references used later for carousel/list filtering."""

    __tablename__ = "trend_result_items"

    result_id: Mapped[str] = mapped_column(
        ForeignKey("trend_results.result_id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)


class TrendScheduleRun(Base):
    """One planned instant of the single global schedule rule.

    ``(schedule_rule, scheduled_for)`` is unique so a restarted process cannot
    execute the same planned instant twice; the row is claimed before any work
    starts and is the durable record the workbench reads back.
    """

    __tablename__ = "trend_schedule_runs"
    __table_args__ = (
        UniqueConstraint("schedule_rule", "scheduled_for", name="uq_trend_schedule_runs_slot"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'skipped', 'reused')",
            name="ck_trend_schedule_runs_status",
        ),
    )

    schedule_run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    schedule_rule: Mapped[str] = mapped_column(String(100), nullable=False)
    # Beijing wall-clock instant the rule planned, stored naive like the mail and
    # morning-crawl schedulers so the value is compared and displayed verbatim.
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, index=True)
    template_id: Mapped[str | None] = mapped_column(
        ForeignKey("trend_identity_templates.template_id", ondelete="SET NULL"),
        nullable=True,
    )
    trend_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("trend_runs.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running", index=True)
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    window_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    trend_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storyline_candidate_goal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
