from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import Item
from app.trends.cards import (
    CardGenerationCandidate,
    CardGenerationOutcome,
    CardStageCounts,
    select_card_content,
)
from app.trends.embedding_state import apply_cache_report, sync_state
from app.trends.models import (
    NewsExplanationCard,
    NewsCardEmbedding,
    Storyline,
    StorylineMember,
    StorylineSnapshot,
    TrendCandidateCluster,
    TrendCandidateClusterMember,
    TrendEmbeddingModelState,
    TrendIdentityTemplate,
    TrendResult,
    TrendResultItem,
    TrendRun,
    TrendScheduleRun,
    TrendSettings,
    TrendStorylineReview,
)
from app.trends.prompts import CARD_PROMPT_VERSION
from app.trends.vectors import CardEmbeddingRequestItem, CardEmbeddingResult, CardVectorSink


@dataclass(frozen=True)
class TrendCardListEntry:
    item_id: int
    title: str
    published_at: datetime | None
    fetched_at: datetime
    status: str
    news_actor: str | None
    action: str | None
    result: str | None
    potential_impact: str | None
    cause: str | None
    skip_reason: str | None
    error_message: str | None
    attempt_count: int
    card_updated_at: datetime | None


@dataclass(frozen=True)
class TrendCardListPage:
    total: int
    items: list[TrendCardListEntry]


@dataclass(frozen=True)
class VectorStageCounts:
    pending_count: int
    generated_count: int


@dataclass(frozen=True)
class ClusterPoolCard:
    card_id: str
    embedding: list[float]


@dataclass(frozen=True)
class StoredCandidateCluster:
    member_card_ids: list[str]
    threshold: float
    cohesion_score: float


@dataclass(frozen=True)
class CandidateClusterListEntry:
    candidate_cluster_id: str
    member_count: int
    cohesion_score: float
    threshold: float


@dataclass(frozen=True)
class CandidateClusterListPage:
    total: int
    items: list[CandidateClusterListEntry]


@dataclass(frozen=True)
class StorylineReviewCandidateCard:
    card_id: str
    at: date
    title: str
    news_actor: str
    action: str
    result: str
    potential_impact: str
    cause: str
    content: str
    importance: str | None


@dataclass(frozen=True)
class StorylineReviewCandidate:
    candidate_cluster_id: str
    embedding_version: str
    threshold: float
    cohesion_score: float
    window_start_date: date
    window_end_date: date
    cards: list[StorylineReviewCandidateCard]
    active_continuations: list["StorylineContinuation"] = field(default_factory=list)
    archived_continuations: list["StorylineContinuation"] = field(default_factory=list)


@dataclass(frozen=True)
class StorylineContinuation:
    storyline_id: str
    title: str
    last_member_at: date
    similarity: float
    evidence: list[dict[str, str]]


@dataclass(frozen=True)
class StorylineReviewListEntry:
    review: TrendStorylineReview


@dataclass(frozen=True)
class StorylineListEntry:
    storyline: Storyline
    members: list[StorylineMember]


@dataclass(frozen=True)
class TrendResultItemRow:
    """One exact news reference plus the display title used by source pills."""

    item_id: int
    title: str


@dataclass(frozen=True)
class TrendResultListEntry:
    result: TrendResult
    item_ids: list[int]
    items: list[TrendResultItemRow]


@dataclass(frozen=True)
class TrendCarouselEntry:
    result: TrendResult
    items: list[TrendResultItemRow]
    window_item_count: int


class TrendRepository(CardVectorSink):
    def __init__(self, db: Session) -> None:
        self._db = db

    def list_identity_templates(self) -> list[TrendIdentityTemplate]:
        statement = select(TrendIdentityTemplate).order_by(
            TrendIdentityTemplate.created_at.desc(),
            TrendIdentityTemplate.template_id.desc(),
        )
        return list(self._db.scalars(statement))

    def get_identity_template(self, template_id: str) -> TrendIdentityTemplate | None:
        return self._db.get(TrendIdentityTemplate, template_id)

    def add_identity_template(self, template: TrendIdentityTemplate) -> TrendIdentityTemplate:
        self._db.add(template)
        return template

    def delete_identity_template(self, template: TrendIdentityTemplate) -> None:
        # Opinion-layer runs/results cascade via FK; fact-layer tables stay intact.
        self._db.delete(template)

    def add_trend_run(self, run: TrendRun) -> TrendRun:
        self._db.add(run)
        return run

    def get_trend_run(self, run_id: str) -> TrendRun | None:
        return self._db.get(TrendRun, run_id)

    def get_running_trend_run(self, template_id: str) -> TrendRun | None:
        return self._db.scalar(
            select(TrendRun)
            .where(
                TrendRun.template_id == template_id,
                TrendRun.status.in_(("pending", "running")),
            )
            .order_by(TrendRun.created_at.desc(), TrendRun.run_id.desc())
            .limit(1)
        )

    def get_latest_trend_run(self, template_id: str) -> TrendRun | None:
        return self._db.scalar(
            select(TrendRun)
            .where(TrendRun.template_id == template_id)
            .order_by(TrendRun.created_at.desc(), TrendRun.run_id.desc())
            .limit(1)
        )

    def get_latest_succeeded_trend_run(self, template_id: str) -> TrendRun | None:
        return self._db.scalar(
            select(TrendRun)
            .where(
                TrendRun.template_id == template_id,
                TrendRun.status == "succeeded",
            )
            .order_by(TrendRun.finished_at.desc(), TrendRun.run_id.desc())
            .limit(1)
        )

    def list_trend_runs(
        self, *, template_id: str, offset: int, limit: int
    ) -> tuple[int, list[TrendRun]]:
        total = (
            self._db.scalar(
                select(func.count()).select_from(TrendRun).where(TrendRun.template_id == template_id)
            )
            or 0
        )
        items = list(
            self._db.scalars(
                select(TrendRun)
                .where(TrendRun.template_id == template_id)
                .order_by(TrendRun.created_at.desc(), TrendRun.run_id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        return total, items

    def count_trend_results(self, run_id: str) -> tuple[int, int]:
        rows = list(
            self._db.scalars(select(TrendResult.category).where(TrendResult.run_id == run_id))
        )
        unverified = sum(1 for category in rows if category == "unverified_change")
        return len(rows), unverified

    def list_trend_results_for_run(self, run_id: str) -> list[TrendResultListEntry]:
        results = self._ordered_trend_results(run_id=run_id, verified_only=False)
        if not results:
            return []
        by_result = self._trend_result_items([result.result_id for result in results])
        return [
            TrendResultListEntry(
                result=result,
                item_ids=[row.item_id for row in by_result[result.result_id]],
                items=by_result[result.result_id],
            )
            for result in results
        ]

    def list_carousel_results(
        self, *, run_id: str, window_start: date, window_end: date
    ) -> list[TrendCarouselEntry]:
        """Return verified results of one run together with their window overlap.

        The Item join stays inside this repository so the carousel gets both the
        exact ``item_id`` set and its display title without leaking trend tables.
        """
        results = self._ordered_trend_results(run_id=run_id, verified_only=True)
        if not results:
            return []
        result_ids = [result.result_id for result in results]
        by_result = self._trend_result_items(result_ids)
        start_datetime, end_datetime = self._range_datetimes(
            start_date=window_start, end_date=window_end
        )
        business_datetime = func.coalesce(Item.published_at, Item.fetched_at)
        window_counts: dict[str, int] = {result_id: 0 for result_id in result_ids}
        rows = self._db.execute(
            select(TrendResultItem.result_id, func.count())
            .join(Item, Item.id == TrendResultItem.item_id)
            .where(
                TrendResultItem.result_id.in_(result_ids),
                business_datetime >= start_datetime,
                business_datetime < end_datetime,
            )
            .group_by(TrendResultItem.result_id)
        )
        for result_id, count in rows:
            window_counts[result_id] = count
        return [
            TrendCarouselEntry(
                result=result,
                items=by_result[result.result_id],
                window_item_count=window_counts[result.result_id],
            )
            for result in results
        ]

    def _ordered_trend_results(self, *, run_id: str, verified_only: bool) -> list[TrendResult]:
        filters = [TrendResult.run_id == run_id]
        if verified_only:
            filters.append(TrendResult.category != "unverified_change")
        return list(
            self._db.scalars(
                select(TrendResult)
                .where(*filters)
                .order_by(
                    TrendResult.trend_rank_score.desc(),
                    TrendResult.result_id.asc(),
                )
            )
        )

    def _trend_result_items(self, result_ids: list[str]) -> dict[str, list[TrendResultItemRow]]:
        by_result: dict[str, list[TrendResultItemRow]] = {result_id: [] for result_id in result_ids}
        if not result_ids:
            return by_result
        statement = (
            select(TrendResultItem.result_id, Item.id, Item.title, Item.title_tldr)
            .join(Item, Item.id == TrendResultItem.item_id)
            .where(TrendResultItem.result_id.in_(result_ids))
            .order_by(TrendResultItem.result_id.asc(), Item.id.asc())
        )
        for result_id, item_id, title, title_tldr in self._db.execute(statement):
            by_result[result_id].append(
                TrendResultItemRow(item_id=item_id, title=(title_tldr or title or "").strip() or f"新闻#{item_id}")
            )
        return by_result

    def add_schedule_run(self, schedule_run: TrendScheduleRun) -> TrendScheduleRun:
        self._db.add(schedule_run)
        return schedule_run

    def get_schedule_run(self, schedule_run_id: str) -> TrendScheduleRun | None:
        return self._db.get(TrendScheduleRun, schedule_run_id)

    def get_latest_schedule_run(self, *, schedule_rule: str | None = None) -> TrendScheduleRun | None:
        filters = [] if schedule_rule is None else [TrendScheduleRun.schedule_rule == schedule_rule]
        return self._db.scalar(
            select(TrendScheduleRun)
            .where(*filters)
            .order_by(
                TrendScheduleRun.scheduled_for.desc(),
                TrendScheduleRun.created_at.desc(),
            )
            .limit(1)
        )

    def list_running_schedule_runs(self) -> list[TrendScheduleRun]:
        return list(
            self._db.scalars(select(TrendScheduleRun).where(TrendScheduleRun.status == "running"))
        )

    def get_settings(self) -> TrendSettings | None:
        return self._db.scalar(select(TrendSettings).order_by(TrendSettings.id).limit(1))

    def add_settings(self, settings: TrendSettings) -> TrendSettings:
        self._db.add(settings)
        return settings

    def sync_embedding_state(self) -> TrendEmbeddingModelState:
        return sync_state(self._db)

    def apply_embedding_cache_report(
        self,
        *,
        installed: bool,
        model_version: str | None,
    ) -> TrendEmbeddingModelState:
        return apply_cache_report(self._db, installed=installed, model_version=model_version)

    def get_card_stage_counts(self, *, start_date: date, end_date: date) -> CardStageCounts:
        start_datetime, end_datetime = self._range_datetimes(
            start_date=start_date,
            end_date=end_date,
        )
        business_datetime = func.coalesce(Item.published_at, Item.fetched_at)
        statement = (
            select(
                NewsExplanationCard.card_prompt_version,
                NewsExplanationCard.status,
                Item.summary,
            )
            .select_from(Item)
            .outerjoin(NewsExplanationCard, NewsExplanationCard.item_id == Item.id)
            .where(
                business_datetime >= start_datetime,
                business_datetime < end_datetime,
            )
        )
        pending = generated = skipped = failed = 0
        for prompt_version, card_status, summary in self._db.execute(statement):
            if prompt_version is None:
                pending += 1
            elif card_status == "ready":
                generated += 1
            elif card_status == "skipped" and (summary or "").strip():
                pending += 1
            elif card_status == "skipped":
                skipped += 1
            else:
                failed += 1
        return CardStageCounts(
            pending_count=pending,
            generated_count=generated,
            skipped_count=skipped,
            failed_count=failed,
        )

    def list_card_stage_items(
        self,
        *,
        start_date: date,
        end_date: date,
        card_status: str | None,
        runtime_pending_item_ids: frozenset[int],
        offset: int,
        limit: int,
    ) -> TrendCardListPage:
        """List every news item in the active window with its effective card status.

        A missing card is deliberately represented as ``pending`` so the list
        uses the exact same status semantics as the stage counters. Existing
        cards remain usable when the prompt version changes.
        """
        start_datetime, end_datetime = self._range_datetimes(
            start_date=start_date,
            end_date=end_date,
        )
        business_datetime = func.coalesce(Item.published_at, Item.fetched_at)
        effective_status = self._effective_card_status_expression(runtime_pending_item_ids)
        filters = [
            business_datetime >= start_datetime,
            business_datetime < end_datetime,
        ]
        if card_status is not None:
            filters.append(effective_status == card_status)

        total = self._db.scalar(
            select(func.count())
            .select_from(Item)
            .outerjoin(NewsExplanationCard, NewsExplanationCard.item_id == Item.id)
            .where(*filters)
        )
        statement = (
            select(
                Item.id,
                Item.title,
                Item.title_tldr,
                Item.published_at,
                Item.fetched_at,
                effective_status.label("effective_status"),
                NewsExplanationCard.news_actor,
                NewsExplanationCard.action,
                NewsExplanationCard.result,
                NewsExplanationCard.potential_impact,
                NewsExplanationCard.cause,
                NewsExplanationCard.skip_reason,
                NewsExplanationCard.error_message,
                NewsExplanationCard.attempt_count,
                NewsExplanationCard.updated_at,
            )
            .select_from(Item)
            .outerjoin(NewsExplanationCard, NewsExplanationCard.item_id == Item.id)
            .where(*filters)
            .order_by(business_datetime.desc(), Item.id.desc())
            .offset(offset)
            .limit(limit)
        )
        items: list[TrendCardListEntry] = []
        for row in self._db.execute(statement):
            items.append(
                TrendCardListEntry(
                    item_id=row.id,
                    title=row.title_tldr or row.title,
                    published_at=row.published_at,
                    fetched_at=row.fetched_at,
                    status=row.effective_status,
                    news_actor=row.news_actor,
                    action=row.action,
                    result=row.result,
                    potential_impact=row.potential_impact,
                    cause=row.cause,
                    skip_reason=row.skip_reason,
                    error_message=row.error_message,
                    attempt_count=row.attempt_count or 0,
                    card_updated_at=row.updated_at,
                )
            )
        return TrendCardListPage(total=total or 0, items=items)

    def list_card_generation_candidates(
        self,
        *,
        start_date: date,
        end_date: date,
    ) -> list[CardGenerationCandidate]:
        start_datetime, end_datetime = self._range_datetimes(
            start_date=start_date,
            end_date=end_date,
        )
        business_datetime = func.coalesce(Item.published_at, Item.fetched_at)
        skipped_with_summary = and_(
            NewsExplanationCard.status == "skipped",
            func.length(func.trim(Item.summary)) > 0,
        )
        statement = (
            select(
                Item.id,
                Item.title,
                Item.clean_content,
                Item.summary,
                Item.key_points,
                Item.published_at,
                Item.fetched_at,
            )
            .select_from(Item)
            .outerjoin(NewsExplanationCard, NewsExplanationCard.item_id == Item.id)
            .where(
                business_datetime >= start_datetime,
                business_datetime < end_datetime,
                or_(
                    NewsExplanationCard.card_id.is_(None),
                    NewsExplanationCard.status == "failed",
                    skipped_with_summary,
                ),
            )
            .order_by(business_datetime.asc(), Item.id.asc())
        )
        candidates: list[CardGenerationCandidate] = []
        for (
            item_id,
            title,
            clean_content,
            summary,
            key_points,
            published_at,
            fetched_at,
        ) in self._db.execute(statement):
            item_business_datetime = published_at or fetched_at
            content, content_source = select_card_content(
                clean_content=clean_content,
                summary=summary,
            )
            candidates.append(
                CardGenerationCandidate(
                    item_id=item_id,
                    title=title,
                    content=content,
                    at=item_business_datetime.date(),
                    content_source=content_source,
                    key_points=[str(point) for point in key_points] if isinstance(key_points, list) else None,
                )
            )
        return candidates

    def save_card_generation_outcome(
        self,
        candidate: CardGenerationCandidate,
        outcome: CardGenerationOutcome,
    ) -> NewsExplanationCard:
        card = self._db.scalar(
            select(NewsExplanationCard).where(NewsExplanationCard.item_id == candidate.item_id)
        )
        if card is None:
            card = NewsExplanationCard(
                item_id=candidate.item_id,
                at=candidate.at,
                card_prompt_version=CARD_PROMPT_VERSION,
                status=outcome.status,
            )
            self._db.add(card)

        card.at = candidate.at
        card.card_prompt_version = CARD_PROMPT_VERSION
        card.status = outcome.status
        card.skip_reason = outcome.skip_reason
        card.error_message = outcome.error_message
        card.attempt_count = outcome.attempt_count
        if outcome.content is None:
            card.news_actor = None
            card.action = None
            card.result = None
            card.potential_impact = None
            card.cause = None
        else:
            card.news_actor = outcome.content.news_actor
            card.action = outcome.content.action
            card.result = outcome.content.result
            card.potential_impact = outcome.content.potential_impact
            card.cause = outcome.content.cause
        return card

    def get_vector_stage_counts(
        self, *, start_date: date, end_date: date, embedding_version: str
    ) -> VectorStageCounts:
        start_datetime, end_datetime = self._range_datetimes(start_date=start_date, end_date=end_date)
        business_datetime = func.coalesce(Item.published_at, Item.fetched_at)
        statement = (
            select(NewsCardEmbedding.embedding_version)
            .select_from(NewsExplanationCard)
            .join(Item, Item.id == NewsExplanationCard.item_id)
            .outerjoin(NewsCardEmbedding, NewsCardEmbedding.card_id == NewsExplanationCard.card_id)
            .where(
                NewsExplanationCard.status == "ready",
                business_datetime >= start_datetime,
                business_datetime < end_datetime,
            )
        )
        pending = generated = 0
        for stored_version in self._db.scalars(statement):
            if stored_version == embedding_version:
                generated += 1
            else:
                pending += 1
        return VectorStageCounts(pending_count=pending, generated_count=generated)

    def list_vector_generation_candidates(
        self, *, start_date: date, end_date: date, embedding_version: str
    ) -> list[CardEmbeddingRequestItem]:
        start_datetime, end_datetime = self._range_datetimes(start_date=start_date, end_date=end_date)
        business_datetime = func.coalesce(Item.published_at, Item.fetched_at)
        statement = (
            select(
                NewsExplanationCard.card_id,
                NewsExplanationCard.news_actor,
                NewsExplanationCard.action,
                NewsExplanationCard.result,
                NewsExplanationCard.cause,
                NewsExplanationCard.potential_impact,
            )
            .select_from(NewsExplanationCard)
            .join(Item, Item.id == NewsExplanationCard.item_id)
            .outerjoin(NewsCardEmbedding, NewsCardEmbedding.card_id == NewsExplanationCard.card_id)
            .where(
                NewsExplanationCard.status == "ready",
                business_datetime >= start_datetime,
                business_datetime < end_datetime,
                or_(
                    NewsCardEmbedding.card_id.is_(None),
                    NewsCardEmbedding.embedding_version != embedding_version,
                ),
            )
            .order_by(business_datetime.asc(), NewsExplanationCard.card_id.asc())
        )
        from app.trends.vectors import build_event_core_text

        return [
            CardEmbeddingRequestItem(
                card_id=row.card_id,
                event_core_text=build_event_core_text(
                    news_actor=row.news_actor or "",
                    action=row.action or "",
                    result=row.result or "",
                    cause=row.cause or "",
                    potential_impact=row.potential_impact or "",
                ),
            )
            for row in self._db.execute(statement)
        ]

    def write_card_vectors(self, results: list[CardEmbeddingResult]) -> None:
        """Upsert one already validated atomic Worker batch; caller owns commit."""
        for result in results:
            if result.dimension != 1024 or len(result.vector) != result.dimension:
                raise ValueError(f"card_id={result.card_id} 的向量维度不符合 1024 维契约。")
            if not result.vector:
                raise ValueError(f"card_id={result.card_id} 的向量为空，拒绝写入。")
            stored = self._db.get(NewsCardEmbedding, result.card_id)
            if stored is None:
                stored = NewsCardEmbedding(card_id=result.card_id, embedding=result.vector)
                self._db.add(stored)
            else:
                stored.embedding = result.vector
            stored.provider = result.provider
            stored.model_id = result.model_id
            stored.model_version = result.model_version
            stored.embedding_version = result.embedding_version
            stored.dimension = result.dimension
            stored.normalized = result.normalized
            stored.generated_on = result.generated_on

    def list_cluster_pool(
        self, *, start_date: date, end_date: date, embedding_version: str, strict_version: bool = True
    ) -> list[ClusterPoolCard]:
        """Read one version-only candidate pool; version mixing is a hard error."""
        start_datetime, end_datetime = self._range_datetimes(start_date=start_date, end_date=end_date)
        business_datetime = func.coalesce(Item.published_at, Item.fetched_at)
        statement = (
            select(
                NewsCardEmbedding.card_id,
                NewsCardEmbedding.embedding,
                NewsCardEmbedding.embedding_version,
                NewsCardEmbedding.dimension,
                NewsCardEmbedding.normalized,
            )
            .select_from(NewsCardEmbedding)
            .join(NewsExplanationCard, NewsExplanationCard.card_id == NewsCardEmbedding.card_id)
            .join(Item, Item.id == NewsExplanationCard.item_id)
            .where(
                NewsExplanationCard.status == "ready",
                business_datetime >= start_datetime,
                business_datetime < end_datetime,
            )
            .order_by(NewsExplanationCard.card_id.asc())
        )
        rows = list(self._db.execute(statement))
        versions = {row.embedding_version for row in rows}
        if strict_version and versions and versions != {embedding_version}:
            rendered = "、".join(sorted(versions))
            raise ValueError(
                f"候选池检测到混合 embedding_version（{rendered}），请先补齐当前版本 {embedding_version} 的向量后重试。"
            )
        if strict_version:
            invalid = [row.card_id for row in rows if row.dimension != 1024 or not row.normalized]
            if invalid:
                raise ValueError("候选池包含非归一化或非 1024 维向量；请重新生成当前版本向量后重试。")
        return [
            ClusterPoolCard(card_id=row.card_id, embedding=list(row.embedding))
            for row in rows
            if row.embedding_version == embedding_version
        ]

    def replace_candidate_clusters(
        self,
        *,
        embedding_version: str,
        max_cluster_size: int,
        window_start_date: date,
        window_end_date: date,
        clusters: list[StoredCandidateCluster],
    ) -> None:
        """Atomically replace the pending-review candidate set for this fact layer."""
        existing_ids = list(
            self._db.scalars(
                select(TrendCandidateCluster.candidate_cluster_id).where(
                    TrendCandidateCluster.embedding_version == embedding_version,
                    TrendCandidateCluster.window_start_date == window_start_date,
                    TrendCandidateCluster.window_end_date == window_end_date,
                )
            )
        )
        if existing_ids:
            self._db.execute(
                delete(TrendCandidateClusterMember).where(
                    TrendCandidateClusterMember.candidate_cluster_id.in_(existing_ids)
                )
            )
            self._db.execute(delete(TrendCandidateCluster).where(TrendCandidateCluster.candidate_cluster_id.in_(existing_ids)))
        today = date.today()
        for cluster in clusters:
            stored = TrendCandidateCluster(
                embedding_version=embedding_version,
                threshold=cluster.threshold,
                cohesion_score=cluster.cohesion_score,
                max_cluster_size=max_cluster_size,
                window_start_date=window_start_date,
                window_end_date=window_end_date,
                generated_on=today,
            )
            self._db.add(stored)
            self._db.flush()
            self._db.add_all(
                TrendCandidateClusterMember(
                    candidate_cluster_id=stored.candidate_cluster_id,
                    card_id=card_id,
                    member_order=index,
                )
                for index, card_id in enumerate(cluster.member_card_ids)
            )

    def get_candidate_cluster_summary(
        self, *, start_date: date, end_date: date, embedding_version: str
    ) -> tuple[int, int, float | None]:
        pool = self.list_cluster_pool(
            start_date=start_date, end_date=end_date, embedding_version=embedding_version, strict_version=False
        )
        cluster_rows = list(
            self._db.execute(
                select(TrendCandidateCluster.candidate_cluster_id, TrendCandidateCluster.threshold)
                .where(
                    TrendCandidateCluster.embedding_version == embedding_version,
                    TrendCandidateCluster.window_start_date == start_date,
                    TrendCandidateCluster.window_end_date == end_date,
                )
            )
        )
        member_rows = list(
            self._db.execute(
                select(
                    TrendCandidateClusterMember.candidate_cluster_id,
                    TrendCandidateClusterMember.card_id,
                )
                .join(
                    TrendCandidateCluster,
                    TrendCandidateCluster.candidate_cluster_id == TrendCandidateClusterMember.candidate_cluster_id,
                )
                .where(
                    TrendCandidateCluster.embedding_version == embedding_version,
                    TrendCandidateCluster.window_start_date == start_date,
                    TrendCandidateCluster.window_end_date == end_date,
                )
            )
        )
        members_by_cluster: dict[str, set[str]] = {}
        for cluster_id, card_id in member_rows:
            members_by_cluster.setdefault(cluster_id, set()).add(card_id)
        failed_sets = {
            frozenset(review.member_card_ids)
            for review in self._db.scalars(
                select(TrendStorylineReview).where(
                    TrendStorylineReview.embedding_version == embedding_version,
                    TrendStorylineReview.status == "failed",
                )
            )
        }
        # A failed review has no fact-layer outcome. Its whole candidate set is
        # therefore returned to the same derived pending-match pool instead of
        # being hidden merely because a transient candidate row still exists.
        member_ids = {
            card_id
            for cluster_id, card_ids in members_by_cluster.items()
            if frozenset(card_ids) not in failed_sets
            for card_id in card_ids
        }
        pool_ids = {card.card_id for card in pool}
        threshold = max((row.threshold for row in cluster_rows), default=None)
        return len(cluster_rows), len(pool_ids - member_ids), threshold

    def list_candidate_clusters(
        self,
        *,
        embedding_version: str,
        window_start_date: date,
        window_end_date: date,
        offset: int,
        limit: int,
    ) -> CandidateClusterListPage:
        filters = (
            TrendCandidateCluster.embedding_version == embedding_version,
            TrendCandidateCluster.window_start_date == window_start_date,
            TrendCandidateCluster.window_end_date == window_end_date,
        )
        total = self._db.scalar(
            select(func.count()).select_from(TrendCandidateCluster).where(*filters)
        ) or 0
        member_count = func.count(TrendCandidateClusterMember.card_id).label("member_count")
        statement = (
            select(
                TrendCandidateCluster.candidate_cluster_id,
                TrendCandidateCluster.cohesion_score,
                TrendCandidateCluster.threshold,
                member_count,
            )
            .outerjoin(
                TrendCandidateClusterMember,
                TrendCandidateClusterMember.candidate_cluster_id
                == TrendCandidateCluster.candidate_cluster_id,
            )
            .where(*filters)
            .group_by(
                TrendCandidateCluster.candidate_cluster_id,
                TrendCandidateCluster.cohesion_score,
                TrendCandidateCluster.threshold,
            )
            .order_by(
                TrendCandidateCluster.cohesion_score.desc(),
                TrendCandidateCluster.candidate_cluster_id.asc(),
            )
            .offset(offset)
            .limit(limit)
        )
        return CandidateClusterListPage(
            total=total,
            items=[
                CandidateClusterListEntry(
                    candidate_cluster_id=row.candidate_cluster_id,
                    member_count=row.member_count,
                    cohesion_score=row.cohesion_score,
                    threshold=row.threshold,
                )
                for row in self._db.execute(statement)
            ],
        )

    def list_storyline_review_candidates(
        self, *, embedding_version: str, window_start_date: date, window_end_date: date
    ) -> list[StorylineReviewCandidate]:
        """Copy the current candidate-cluster evidence before any review work starts."""
        statement = (
            select(
                TrendCandidateCluster.candidate_cluster_id,
                TrendCandidateCluster.embedding_version,
                TrendCandidateCluster.threshold,
                TrendCandidateCluster.cohesion_score,
                TrendCandidateCluster.window_start_date,
                TrendCandidateCluster.window_end_date,
                TrendCandidateClusterMember.member_order,
                NewsExplanationCard.card_id,
                NewsExplanationCard.at,
                Item.title,
                Item.title_tldr,
                Item.summary,
                Item.clean_content,
                Item.importance,
                NewsExplanationCard.news_actor,
                NewsExplanationCard.action,
                NewsExplanationCard.result,
                NewsExplanationCard.potential_impact,
                NewsExplanationCard.cause,
            )
            .join(
                TrendCandidateClusterMember,
                TrendCandidateClusterMember.candidate_cluster_id == TrendCandidateCluster.candidate_cluster_id,
            )
            .join(NewsExplanationCard, NewsExplanationCard.card_id == TrendCandidateClusterMember.card_id)
            .join(Item, Item.id == NewsExplanationCard.item_id)
            .where(
                TrendCandidateCluster.embedding_version == embedding_version,
                TrendCandidateCluster.window_start_date == window_start_date,
                TrendCandidateCluster.window_end_date == window_end_date,
            )
            .order_by(TrendCandidateCluster.candidate_cluster_id.asc(), TrendCandidateClusterMember.member_order.asc())
        )
        grouped: dict[str, StorylineReviewCandidate] = {}
        for row in self._db.execute(statement):
            candidate = grouped.get(row.candidate_cluster_id)
            if candidate is None:
                candidate = StorylineReviewCandidate(
                    candidate_cluster_id=row.candidate_cluster_id,
                    embedding_version=row.embedding_version,
                    threshold=row.threshold,
                    cohesion_score=row.cohesion_score,
                    window_start_date=row.window_start_date,
                    window_end_date=row.window_end_date,
                    cards=[],
                )
                grouped[row.candidate_cluster_id] = candidate
            content = (row.clean_content or row.summary or "").strip()[:1800]
            candidate.cards.append(
                StorylineReviewCandidateCard(
                    card_id=row.card_id,
                    at=row.at,
                    title=row.title_tldr or row.title,
                    news_actor=row.news_actor or "无",
                    action=row.action or "无",
                    result=row.result or "无",
                    potential_impact=row.potential_impact or "无",
                    cause=row.cause or "无",
                    content=content,
                    importance=row.importance,
                )
            )
        candidates = list(grouped.values())
        self._attach_continuations(candidates, embedding_version=embedding_version)
        return candidates

    def _attach_continuations(
        self, candidates: list[StorylineReviewCandidate], *, embedding_version: str
    ) -> None:
        """Recall high-similarity active/archived lines for explicit Agent review."""
        if not candidates:
            return
        card_ids = [card.card_id for candidate in candidates for card in candidate.cards]
        vectors = dict(
            self._db.execute(
                select(NewsCardEmbedding.card_id, NewsCardEmbedding.embedding).where(
                    NewsCardEmbedding.card_id.in_(card_ids),
                    NewsCardEmbedding.embedding_version == embedding_version,
                )
            ).all()
        )
        if not vectors:
            return
        statement = (
            select(
                Storyline.storyline_id,
                Storyline.title,
                Storyline.last_member_at,
                NewsCardEmbedding.embedding,
                NewsExplanationCard.card_id,
                NewsExplanationCard.at,
                Item.title,
                Item.title_tldr,
                NewsExplanationCard.news_actor,
                NewsExplanationCard.action,
                NewsExplanationCard.result,
                NewsExplanationCard.cause,
            )
            .join(StorylineMember, StorylineMember.storyline_id == Storyline.storyline_id)
            .join(NewsCardEmbedding, NewsCardEmbedding.card_id == StorylineMember.card_id)
            .join(NewsExplanationCard, NewsExplanationCard.card_id == StorylineMember.card_id)
            .join(Item, Item.id == NewsExplanationCard.item_id)
            .where(Storyline.status.in_(("active", "archived")), NewsCardEmbedding.embedding_version == embedding_version)
        )
        continuations: dict[str, dict[str, object]] = {}
        for (
            storyline_id,
            title,
            last_member_at,
            embedding,
            card_id,
            card_at,
            item_title,
            title_tldr,
            news_actor,
            action,
            result,
            cause,
            status,
        ) in self._db.execute(
            statement.add_columns(Storyline.status)
        ):
            if storyline_id not in continuations:
                continuations[storyline_id] = {
                    "title": title,
                    "status": status,
                    "last_member_at": last_member_at,
                    "vectors": [],
                    "evidence": [],
                }
            continuations[storyline_id]["vectors"].append(list(embedding))  # type: ignore[index]
            continuations[storyline_id]["evidence"].append({  # type: ignore[index]
                "card_id": card_id,
                "at": card_at.isoformat(),
                "title": title_tldr or item_title,
                "news_actor": news_actor or "无",
                "action": action or "无",
                "result": result or "无",
                "cause": cause or "无",
            })
        for candidate in candidates:
            candidate_vectors = [vectors[card.card_id] for card in candidate.cards if card.card_id in vectors]
            active_ranked: list[StorylineContinuation] = []
            archived_ranked: list[StorylineContinuation] = []
            for storyline_id, continuation in continuations.items():
                title = str(continuation["title"])
                status = str(continuation["status"])
                last_member_at = continuation["last_member_at"]
                storyline_vectors = continuation["vectors"]
                evidence = continuation["evidence"]
                similarity = max(
                    (sum(left * right for left, right in zip(current, prior)) for current in candidate_vectors for prior in storyline_vectors),
                    default=-1.0,
                )
                if similarity >= 0.85:
                    target = active_ranked if status == "active" else archived_ranked
                    target.append(
                        StorylineContinuation(
                            storyline_id=storyline_id,
                            title=title,
                            last_member_at=last_member_at,
                            similarity=round(similarity, 4),
                            evidence=evidence,
                        )
                    )
            candidate.active_continuations.extend(sorted(active_ranked, key=lambda item: (-item.similarity, item.storyline_id))[:3])
            candidate.archived_continuations.extend(sorted(archived_ranked, key=lambda item: (-item.similarity, item.storyline_id))[:3])

    def get_storyline_review(self, review_key: str) -> TrendStorylineReview | None:
        return self._db.scalar(select(TrendStorylineReview).where(TrendStorylineReview.review_key == review_key))

    def add_storyline_review(self, review: TrendStorylineReview) -> TrendStorylineReview:
        self._db.add(review)
        return review

    def list_storyline_reviews(
        self, *, offset: int, limit: int, status_filter: str | None = None
    ) -> tuple[int, list[TrendStorylineReview]]:
        filters = []
        if status_filter == "unreviewed":
            filters.append(TrendStorylineReview.status.in_(("pending", "running", "failed")))
        elif status_filter is not None:
            filters.append(TrendStorylineReview.status == status_filter)
        total = self._db.scalar(
            select(func.count()).select_from(TrendStorylineReview).where(*filters)
        ) or 0
        statement = (
            select(TrendStorylineReview)
            .where(*filters)
            .order_by(TrendStorylineReview.created_at.desc(), TrendStorylineReview.review_id.desc())
            .offset(offset)
            .limit(limit)
        )
        return total, list(self._db.scalars(statement))

    def archive_stale_storylines(self, *, cutoff: date) -> int:
        """Archive lines outside the automatic 90-day continuation horizon."""
        statement = select(Storyline).where(
            Storyline.status == "active",
            Storyline.last_member_at < cutoff,
        )
        rows = list(self._db.scalars(statement))
        for storyline in rows:
            storyline.status = "archived"
        return len(rows)

    def list_storylines(self, *, offset: int, limit: int) -> tuple[int, list[StorylineListEntry]]:
        total = self._db.scalar(select(func.count()).select_from(Storyline)) or 0
        storylines = list(
            self._db.scalars(
                select(Storyline)
                .order_by(Storyline.status.asc(), Storyline.overall_influence_score.desc(), Storyline.storyline_id.asc())
                .offset(offset)
                .limit(limit)
            )
        )
        if not storylines:
            return total, []
        ids = [storyline.storyline_id for storyline in storylines]
        members = list(
            self._db.scalars(
                select(StorylineMember)
                .where(StorylineMember.storyline_id.in_(ids))
                .order_by(StorylineMember.at.asc(), StorylineMember.card_id.asc())
            )
        )
        by_storyline: dict[str, list[StorylineMember]] = {storyline_id: [] for storyline_id in ids}
        for member in members:
            by_storyline[member.storyline_id].append(member)
        return total, [StorylineListEntry(storyline=value, members=by_storyline[value.storyline_id]) for value in storylines]

    def add_snapshot(self, snapshot: StorylineSnapshot) -> None:
        self._db.add(snapshot)

    @staticmethod
    def _range_datetimes(*, start_date: date, end_date: date) -> tuple[datetime, datetime]:
        return (
            datetime.combine(start_date, time.min),
            datetime.combine(end_date + timedelta(days=1), time.min),
        )

    @staticmethod
    def _effective_card_status_expression(
        runtime_pending_item_ids: frozenset[int],
    ) -> ColumnElement[str]:
        """Return the status expression shared by the list's filter and rows."""
        skipped_with_summary = and_(
            NewsExplanationCard.status == "skipped",
            func.length(func.trim(func.coalesce(Item.summary, ""))) > 0,
        )
        status_cases = [
            (
                or_(
                    NewsExplanationCard.card_id.is_(None),
                    skipped_with_summary,
                ),
                "pending",
            ),
            (NewsExplanationCard.status == "ready", "ready"),
            (NewsExplanationCard.status == "skipped", "skipped"),
        ]
        if runtime_pending_item_ids:
            status_cases.insert(0, (Item.id.in_(runtime_pending_item_ids), "pending"))
        return case(*status_cases, else_="failed")
