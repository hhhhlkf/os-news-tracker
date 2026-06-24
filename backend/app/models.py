import uuid as _uuid
from datetime import datetime
from typing import Any
from sqlalchemy import (
    String, Text, Integer, DateTime, ForeignKey, Float, JSON, UniqueConstraint, CheckConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(20))           # rss | api | page_monitor | search
    url: Mapped[str] = mapped_column(String(1000))
    keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    adapter: Mapped[str | None] = mapped_column(String(100), nullable=True)   # api adapter name
    api_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    stream: Mapped[str] = mapped_column(String(20), default="news")           # news | structured
    vendor: Mapped[str | None] = mapped_column(String(50), nullable=True)
    fetch_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    main_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    relevance_filter: Mapped[bool] = mapped_column(default=False)
    relevance_keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_selector: Mapped[str | None] = mapped_column(String(500), nullable=True)
    title_selector: Mapped[str | None] = mapped_column(String(500), nullable=True)
    date_selector: Mapped[str | None] = mapped_column(String(500), nullable=True)
    stealth: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    health_status: Mapped[str] = mapped_column(String(20), default="ok")
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    items: Mapped[list["Item"]] = relationship(back_populates="source")


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("name", "kind", name="uq_tag_name_kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))


class TagAlias(Base):
    __tablename__ = "tag_aliases"
    __table_args__ = (
        UniqueConstraint("child_tag_id", name="uq_tag_alias_child"),
        CheckConstraint("child_tag_id != parent_tag_id", name="ck_tag_alias_not_self"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    child_tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"), index=True)
    parent_tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="approved", index=True)
    source: Mapped[str] = mapped_column(String(20), default="llm")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (UniqueConstraint("type", "name", name="uq_entity_type_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(300))


class ItemTag(Base):
    __tablename__ = "item_tags"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"), primary_key=True)


class ItemEntity(Base):
    __tablename__ = "item_entities"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), primary_key=True)
    role: Mapped[str | None] = mapped_column(String(50), nullable=True)


class ItemSource(Base):
    __tablename__ = "item_sources"
    __table_args__ = (
        UniqueConstraint("item_id", "source_id", "url", name="uq_item_source_url"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    url: Mapped[str] = mapped_column(String(1000))


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    title: Mapped[str] = mapped_column(String(1000))
    url: Mapped[str] = mapped_column(String(1000))
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    clean_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    main_category: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    title_tldr: Mapped[str | None] = mapped_column(String(200), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_points: Mapped[list | None] = mapped_column(JSON, nullable=True)
    info_type: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    importance: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    why_it_matters: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    llm_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    source: Mapped["Source"] = relationship(back_populates="items")
    tags: Mapped[list["Tag"]] = relationship(secondary="item_tags")
    entities: Mapped[list["Entity"]] = relationship(secondary="item_entities")


# --- Structured-data stream tables (no LLM; parsed straight from APIs) ---

class SecurityAdvisory(Base):
    __tablename__ = "security_advisories"
    __table_args__ = (UniqueConstraint("vendor", "advisory_id", name="uq_adv_vendor_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    vendor: Mapped[str] = mapped_column(String(50), index=True)
    advisory_id: Mapped[str] = mapped_column(String(100))
    cve_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    title: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_products: Mapped[list | None] = mapped_column(JSON, nullable=True)
    fixed_versions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class ProductLifecycle(Base):
    __tablename__ = "product_lifecycles"
    __table_args__ = (UniqueConstraint("vendor", "product", "version", name="uq_lc_vpv"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    vendor: Mapped[str] = mapped_column(String(50), index=True)
    product: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(100))
    release_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ga_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    eol_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    eus_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    phase: Mapped[str | None] = mapped_column(String(50), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class ImageRelease(Base):
    __tablename__ = "image_releases"
    __table_args__ = (
        UniqueConstraint("vendor", "product", "image_tag", "arch", "cloud", name="uq_img"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    vendor: Mapped[str] = mapped_column(String(50), index=True)
    product: Mapped[str] = mapped_column(String(200))
    image_tag: Mapped[str] = mapped_column(String(200))
    arch: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cloud: Mapped[str | None] = mapped_column(String(50), nullable=True)
    image_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(200), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CompatibilityEntry(Base):
    __tablename__ = "compatibility_entries"
    __table_args__ = (
        UniqueConstraint("vendor", "kind", "name", "product", "version", "arch", name="uq_compat"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    vendor: Mapped[str] = mapped_column(String(50), index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)   # hardware|software|package|image|osv
    name: Mapped[str] = mapped_column(String(300))
    product: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    arch: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)


# --- V2 tables: user system, personalised scoring, digest, agent crawl ---


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(_uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="user")   # user | admin
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    profile: Mapped["UserProfile | None"] = relationship(back_populates="user", uselist=False)


class UserProfile(Base):
    __tablename__ = "user_profiles"
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    criteria: Mapped[list] = mapped_column(JSON, default=list)
    free_text_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enable_llm_scoring: Mapped[bool] = mapped_column(default=False)
    min_score_threshold: Mapped[int] = mapped_column(Integer, default=25)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    user: Mapped["User"] = relationship(back_populates="profile")


class UserItemScore(Base):
    __tablename__ = "user_item_scores"
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), primary_key=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    scoring_method: Mapped[str] = mapped_column(String(20), default="fast")   # fast | llm
    score_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserItemInteraction(Base):
    __tablename__ = "user_item_interactions"
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), primary_key=True)
    action: Mapped[str] = mapped_column(String(20), primary_key=True)   # view | bookmark
    interacted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Digest(Base):
    __tablename__ = "digests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(_uuid.uuid4()))
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)   # manual | scheduled
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    time_range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    time_range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), default="personalized")   # personalized | global
    period_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    hotspots: Mapped[list] = mapped_column(JSON, default=list)
    emerging_topics: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="generating")   # generating | ready | failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentSourceConfig(Base):
    __tablename__ = "agent_source_configs"
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    focus_areas: Mapped[list] = mapped_column(JSON, default=list)
    topic_groups: Mapped[list] = mapped_column(JSON, default=list)
    crawl_depth: Mapped[int] = mapped_column(Integer, default=1)
    max_urls_per_run: Mapped[int] = mapped_column(Integer, default=20)
    quality_threshold: Mapped[int] = mapped_column(Integer, default=4)
    crawl_workers: Mapped[int] = mapped_column(Integer, default=5)
    quality_workers: Mapped[int] = mapped_column(Integer, default=3)
    summary_workers: Mapped[int] = mapped_column(Integer, default=3)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentSiteMemory(Base):
    __tablename__ = "agent_site_memory"
    __table_args__ = (UniqueConstraint("source_id", "url_pattern", name="uq_agent_memory_source_url"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    url_pattern: Mapped[str] = mapped_column(String(2000), nullable=False)
    quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)   # keep | discard
    relevant_topic: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    seen_count: Mapped[int] = mapped_column(Integer, default=1)


class AgentCrawlRun(Base):
    __tablename__ = "agent_crawl_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    plan_urls_count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    quality_passed: Mapped[int] = mapped_column(Integer, default=0)
    items_created: Mapped[int] = mapped_column(Integer, default=0)
    target_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_stage: Mapped[str] = mapped_column(String(20), default="planning")
    stage_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")   # running | completed | failed | plan_failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
