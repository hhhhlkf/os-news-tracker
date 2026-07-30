from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.trends.cards import (
    CardGenerationCandidate,
    CardGenerationOutcome,
    card_backfill_controller,
)
from app.trends.embedding import current_embedding_descriptor
from app.trends.embedding_client import (
    EmbeddingWorkerUnavailableError,
    fetch_worker_health,
    request_model_preparation,
)
from app.trends.embedding_config import get_trend_embedding_settings
from app.trends.clusters import (
    AGENT_CONTEXT_CARD_LIMIT,
    PENDING_MATCH_DAYS,
    generate_candidate_clusters,
    vector_backfill_controller,
)
from app.llm.client import LlmClient
from app.trends.evaluation import (
    build_trend_run,
    run_to_status_response,
    runtime_to_status_response,
    trend_evaluation_controller,
)
from app.trends.models import TrendIdentityTemplate, TrendRun, TrendSettings, TrendStorylineReview
from app.trends.repository import TrendRepository
from app.trends.schemas import (
    TREND_CATEGORY_LABELS,
    TrendCardBackfillRequest,
    TrendCardListItemResponse,
    TrendCardListResponse,
    TrendCardListStatus,
    TrendCardStageStatusResponse,
    TrendClusterRunRequest,
    TrendCandidateClusterListItemResponse,
    TrendCandidateClusterListResponse,
    TrendCarouselItemResponse,
    TrendCarouselResponse,
    TrendClusterStageStatusResponse,
    TrendEmbeddingStatusResponse,
    TrendIdentityTemplateCreateRequest,
    TrendLatestResultsResponse,
    TrendResultItemResponse,
    TrendResultResponse,
    TrendRunListItemResponse,
    TrendRunListResponse,
    TrendRunStatusResponse,
    TrendRunTriggerRequest,
    TrendSettingsUpdateRequest,
    TrendVectorBackfillRequest,
    TrendVectorStageStatusResponse,
    TrendStorylineListItemResponse,
    TrendStorylineListResponse,
    TrendStorylineMemberResponse,
    TrendStorylineReviewListItemResponse,
    TrendStorylineReviewListResponse,
    TrendStorylineReviewRequest,
    TrendStorylineStageStatusResponse,
)
from app.trends.storylines import (
    StorylineReviewTask,
    process_storyline_review,
    review_key_for_candidate,
    storyline_review_controller,
)


class TrendIdentityTemplateNotFoundError(Exception):
    def __init__(self, template_id: str) -> None:
        super().__init__(f"trend identity template {template_id} not found")
        self.template_id = template_id


class TrendRunNotFoundError(Exception):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"trend run {run_id} not found")
        self.run_id = run_id


ORPHANED_TREND_RUN_MESSAGE = (
    "趋势任务因服务重启或热重载中断，未发布不完整结果；"
    "最近成功发布保持不变，可安全重新运行。"
)


class TrendService:
    def __init__(self, db: Session) -> None:
        self._db = db
        self._repository = TrendRepository(db)

    def list_identity_templates(self) -> list[TrendIdentityTemplate]:
        return self._repository.list_identity_templates()

    def create_identity_template(self, payload: TrendIdentityTemplateCreateRequest) -> TrendIdentityTemplate:
        template = TrendIdentityTemplate(
            name=payload.name.strip(),
            identity_text=payload.identity_text.strip(),
        )
        self._repository.add_identity_template(template)
        self._db.commit()
        self._db.refresh(template)
        return template

    def delete_identity_template(self, template_id: str) -> None:
        template = self._repository.get_identity_template(template_id)
        if template is None:
            raise TrendIdentityTemplateNotFoundError(template_id)

        settings = self._repository.get_settings()
        if settings is not None and settings.scheduled_template_id == template_id:
            settings.scheduled_template_id = None
            settings.trigger_mode = "manual"
            settings.schedule_rule = None
        # Opinion-layer runs/results cascade with the template; fact-layer tables stay.
        self._repository.delete_identity_template(template)
        self._db.commit()

    def get_settings(self) -> TrendSettings:
        settings = self._repository.get_settings()
        if settings is not None:
            return settings
        settings = TrendSettings(id=1)
        self._repository.add_settings(settings)
        self._db.commit()
        self._db.refresh(settings)
        return settings

    def update_settings(self, payload: TrendSettingsUpdateRequest) -> TrendSettings:
        if payload.scheduled_template_id is not None:
            template = self._repository.get_identity_template(payload.scheduled_template_id)
            if template is None:
                raise TrendIdentityTemplateNotFoundError(payload.scheduled_template_id)

        settings = self.get_settings()
        settings.window_mode = payload.window_mode
        settings.window_start_date = payload.window_start_date
        settings.window_end_date = payload.window_end_date
        settings.relative_window_unit = payload.relative_window_unit
        settings.relative_window_value = payload.relative_window_value
        settings.trend_count = payload.trend_count
        settings.storyline_candidate_goal = payload.storyline_candidate_goal
        settings.trigger_mode = payload.trigger_mode
        settings.schedule_rule = payload.schedule_rule
        settings.scheduled_template_id = payload.scheduled_template_id
        self._db.commit()
        self._db.refresh(settings)
        return settings

    def get_embedding_status(self) -> TrendEmbeddingStatusResponse:
        return self._embedding_status()

    def prepare_embedding_model(self) -> TrendEmbeddingStatusResponse:
        """Ask the dedicated worker container to materialize the model cache."""
        request_model_preparation()
        return self._embedding_status()

    def get_card_stage_status(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> TrendCardStageStatusResponse:
        running_status = card_backfill_controller.running_status()
        if running_status is not None:
            return running_status

        resolved_start, resolved_end = self._resolve_card_window(
            start_date=start_date,
            end_date=end_date,
        )
        counts = self._repository.get_card_stage_counts(
            start_date=resolved_start,
            end_date=resolved_end,
        )
        return card_backfill_controller.status(
            start_date=resolved_start,
            end_date=resolved_end,
            fallback_counts=counts,
        )

    def start_card_backfill(
        self,
        payload: TrendCardBackfillRequest | None = None,
    ) -> TrendCardStageStatusResponse:
        running_status = card_backfill_controller.running_status()
        if running_status is not None:
            return running_status.model_copy(
                update={"message": "已有新闻卡片补齐任务正在执行，本次触发复用现有任务。"}
            )

        resolved_start, resolved_end = self._resolve_card_window(
            start_date=payload.start_date if payload else None,
            end_date=payload.end_date if payload else None,
        )
        counts = self._repository.get_card_stage_counts(
            start_date=resolved_start,
            end_date=resolved_end,
        )
        candidates = self._repository.list_card_generation_candidates(
            start_date=resolved_start,
            end_date=resolved_end,
        )
        return card_backfill_controller.start(
            start_date=resolved_start,
            end_date=resolved_end,
            candidates=candidates,
            counts=counts,
            session_factory=SessionLocal,
            persist_outcome=self._persist_card_outcome,
        )

    def list_card_stage_items(
        self,
        *,
        card_status: TrendCardListStatus | None,
        offset: int,
        limit: int,
    ) -> TrendCardListResponse:
        active_backfill = card_backfill_controller.active_backfill()
        if active_backfill is not None:
            resolved_start = active_backfill.start_date
            resolved_end = active_backfill.end_date
            runtime_pending_item_ids = active_backfill.pending_item_ids
        else:
            resolved_start, resolved_end = self._resolve_card_window(
                start_date=None,
                end_date=None,
            )
            runtime_pending_item_ids = frozenset()
        page = self._repository.list_card_stage_items(
            start_date=resolved_start,
            end_date=resolved_end,
            card_status=card_status,
            runtime_pending_item_ids=runtime_pending_item_ids,
            offset=offset,
            limit=limit,
        )
        return TrendCardListResponse(
            start_date=resolved_start,
            end_date=resolved_end,
            total=page.total,
            offset=offset,
            limit=limit,
            items=[
                TrendCardListItemResponse(
                    item_id=item.item_id,
                    title=item.title,
                    published_at=item.published_at,
                    fetched_at=item.fetched_at,
                    status=item.status,
                    news_actor=item.news_actor if item.status == "ready" else None,
                    action=item.action if item.status == "ready" else None,
                    result=item.result if item.status == "ready" else None,
                    potential_impact=item.potential_impact if item.status == "ready" else None,
                    cause=item.cause if item.status == "ready" else None,
                    skip_reason=item.skip_reason,
                    error_message=item.error_message,
                    attempt_count=item.attempt_count,
                    card_updated_at=item.card_updated_at,
                )
                for item in page.items
            ],
        )

    def get_vector_stage_status(
        self, *, start_date: date | None = None, end_date: date | None = None
    ) -> TrendVectorStageStatusResponse:
        running = vector_backfill_controller.snapshot()
        if running is not None and running.is_running:
            return self._vector_response(running, message="向量补齐任务正在串行执行。")
        resolved_start, resolved_end = self._resolve_card_window(start_date=start_date, end_date=end_date)
        counts = self._repository.get_vector_stage_counts(
            start_date=resolved_start,
            end_date=resolved_end,
            embedding_version=current_embedding_descriptor().embedding_version,
        )
        if running is not None and (running.start_date, running.end_date) == (resolved_start, resolved_end):
            return self._vector_response(running)
        return TrendVectorStageStatusResponse(
            is_running=False,
            start_date=resolved_start,
            end_date=resolved_end,
            embedding_version=current_embedding_descriptor().embedding_version,
            pending_count=counts.pending_count,
            generated_count=counts.generated_count,
            failed_count=0,
            started_at=None,
            finished_at=None,
            error_message=None,
            retry_guidance=None,
        )

    def start_vector_backfill(
        self, payload: TrendVectorBackfillRequest | None = None
    ) -> TrendVectorStageStatusResponse:
        running = vector_backfill_controller.snapshot()
        if running is not None and running.is_running:
            return self._vector_response(running, message="已有向量补齐任务正在执行，本次触发复用现有任务。")
        resolved_start, resolved_end = self._resolve_card_window(
            start_date=payload.start_date if payload else None,
            end_date=payload.end_date if payload else None,
        )
        version = current_embedding_descriptor().embedding_version
        counts = self._repository.get_vector_stage_counts(
            start_date=resolved_start, end_date=resolved_end, embedding_version=version
        )
        candidates = self._repository.list_vector_generation_candidates(
            start_date=resolved_start, end_date=resolved_end, embedding_version=version
        )
        state = vector_backfill_controller.start(
            start_date=resolved_start,
            end_date=resolved_end,
            candidates=candidates,
            counts=counts,
            embedding_version=version,
            session_factory=SessionLocal,
            write_batch=lambda db, results: TrendRepository(db).write_card_vectors(list(results)),
        )
        message = "向量补齐任务已启动。" if candidates else "当前时间范围内没有待生成向量。"
        return self._vector_response(state, message=message)

    def get_cluster_stage_status(
        self, *, start_date: date | None = None, end_date: date | None = None
    ) -> TrendClusterStageStatusResponse:
        resolved_start, resolved_end = self._resolve_card_window(start_date=start_date, end_date=end_date)
        version = current_embedding_descriptor().embedding_version
        settings = self.get_settings()
        vector = self.get_vector_stage_status(start_date=resolved_start, end_date=resolved_end)
        pool_start = max(resolved_start, resolved_end - timedelta(days=PENDING_MATCH_DAYS - 1))
        clusters, pending_matches, threshold = self._repository.get_candidate_cluster_summary(
            start_date=pool_start, end_date=resolved_end, embedding_version=version
        )
        pool_count = vector.generated_count if pool_start == resolved_start else len(
            self._repository.list_cluster_pool(start_date=pool_start, end_date=resolved_end, embedding_version=version)
        )
        maximum = min(AGENT_CONTEXT_CARD_LIMIT, max(2, (pool_count + (2 * settings.storyline_candidate_goal) - 1) // (2 * settings.storyline_candidate_goal)))
        return TrendClusterStageStatusResponse(
            is_running=False,
            start_date=resolved_start,
            end_date=resolved_end,
            embedding_version=version,
            candidate_goal=settings.storyline_candidate_goal,
            max_cluster_size=maximum,
            pending_vector_count=vector.pending_count,
            generated_vector_count=vector.generated_count,
            vector_failed_count=vector.failed_count,
            candidate_cluster_count=clusters,
            pending_match_count=pending_matches,
            used_threshold=threshold,
            started_at=None,
            finished_at=None,
            error_message=vector.error_message,
            retry_guidance=vector.retry_guidance,
        )

    def run_candidate_clustering(
        self, payload: TrendClusterRunRequest | None = None
    ) -> TrendClusterStageStatusResponse:
        resolved_start, resolved_end = self._resolve_card_window(
            start_date=payload.start_date if payload else None,
            end_date=payload.end_date if payload else None,
        )
        vector = self.get_vector_stage_status(start_date=resolved_start, end_date=resolved_end)
        if vector.is_running:
            raise ValueError("向量补齐仍在执行；请等待待生成向量归零后再运行候选簇生成。")
        if vector.pending_count:
            raise ValueError("当前窗口仍有待生成或旧版本向量；请先完成向量补齐，避免混用向量版本。")
        if vector.failed_count:
            raise ValueError("向量补齐存在失败批次；请根据失败原因修复后重试，候选聚类不会使用不完整向量。")
        settings = self.get_settings()
        version = current_embedding_descriptor().embedding_version
        pool_start = max(resolved_start, resolved_end - timedelta(days=PENDING_MATCH_DAYS - 1))
        pool = self._repository.list_cluster_pool(
            start_date=pool_start, end_date=resolved_end, embedding_version=version
        )
        clusters, threshold, maximum = generate_candidate_clusters(
            pool, candidate_goal=settings.storyline_candidate_goal
        )
        self._repository.replace_candidate_clusters(
            embedding_version=version,
            max_cluster_size=maximum,
            window_start_date=pool_start,
            window_end_date=resolved_end,
            clusters=clusters,
        )
        self._db.commit()
        return self.get_cluster_stage_status(start_date=resolved_start, end_date=resolved_end).model_copy(
            update={
                "used_threshold": threshold,
                "message": "候选簇已生成。" if clusters else "当前范围内没有满足纯度阈值的候选簇。",
            }
        )

    def list_candidate_clusters(
        self, *, offset: int, limit: int
    ) -> TrendCandidateClusterListResponse:
        resolved_start, resolved_end = self._resolve_card_window(start_date=None, end_date=None)
        pool_start = max(resolved_start, resolved_end - timedelta(days=PENDING_MATCH_DAYS - 1))
        page = self._repository.list_candidate_clusters(
            embedding_version=current_embedding_descriptor().embedding_version,
            window_start_date=pool_start,
            window_end_date=resolved_end,
            offset=offset,
            limit=limit,
        )
        return TrendCandidateClusterListResponse(
            start_date=resolved_start,
            end_date=resolved_end,
            total=page.total,
            offset=offset,
            limit=limit,
            items=[
                TrendCandidateClusterListItemResponse(
                    candidate_cluster_id=item.candidate_cluster_id,
                    member_count=item.member_count,
                    cohesion_score=item.cohesion_score,
                    threshold=item.threshold,
                )
                for item in page.items
            ],
        )

    def get_storyline_stage_status(
        self, *, start_date: date | None = None, end_date: date | None = None
    ) -> TrendStorylineStageStatusResponse:
        running = storyline_review_controller.snapshot()
        if running is not None and running.is_running:
            return self._storyline_response(running, message="故事线审查任务正在逐簇执行。")
        resolved_start, resolved_end = self._resolve_card_window(start_date=start_date, end_date=end_date)
        pool_start = max(resolved_start, resolved_end - timedelta(days=PENDING_MATCH_DAYS - 1))
        version = current_embedding_descriptor().embedding_version
        candidates = self._repository.list_storyline_review_candidates(
            embedding_version=version,
            window_start_date=pool_start,
            window_end_date=resolved_end,
        )
        counts = {"pending": 0, "accepted": 0, "split": 0, "rejected": 0, "failed": 0}
        for candidate in candidates:
            review = self._repository.get_storyline_review(review_key_for_candidate(candidate))
            if review is None or review.status in {"pending", "running"}:
                counts["pending"] += 1
            else:
                counts[review.status] = counts.get(review.status, 0) + 1
        return TrendStorylineStageStatusResponse(
            is_running=False,
            start_date=resolved_start,
            end_date=resolved_end,
            embedding_version=version,
            pending_count=counts["pending"],
            accepted_count=counts["accepted"],
            split_count=counts["split"],
            rejected_count=counts["rejected"],
            failed_count=counts["failed"],
            started_at=None,
            finished_at=None,
            error_message=None,
            retry_guidance=("失败候选簇未写入故事线；修复 LLM 输出或服务后可重新触发。" if counts["failed"] else None),
        )

    def start_storyline_review(
        self, payload: TrendStorylineReviewRequest | None = None
    ) -> TrendStorylineStageStatusResponse:
        running = storyline_review_controller.snapshot()
        if running is not None and running.is_running:
            return self._storyline_response(running, message="已有故事线审查任务正在执行，本次触发复用现有任务。")
        resolved_start, resolved_end = self._resolve_card_window(
            start_date=payload.start_date if payload else None,
            end_date=payload.end_date if payload else None,
        )
        pool_start = max(resolved_start, resolved_end - timedelta(days=PENDING_MATCH_DAYS - 1))
        version = current_embedding_descriptor().embedding_version
        self._repository.archive_stale_storylines(cutoff=resolved_end - timedelta(days=PENDING_MATCH_DAYS - 1))
        # SessionLocal disables autoflush; continuation recall must observe the
        # lifecycle transition before it queries active versus archived lines.
        self._db.flush()
        candidates = self._repository.list_storyline_review_candidates(
            embedding_version=version,
            window_start_date=pool_start,
            window_end_date=resolved_end,
        )
        versions = {candidate.embedding_version for candidate in candidates}
        if versions and versions != {version}:
            rendered = "、".join(sorted(versions))
            raise ValueError(
                f"故事线审查检测到混合 embedding_version（{rendered}）；请重新生成当前版本 {version} 的候选簇。"
            )
        tasks: list[StorylineReviewTask] = []
        persisted_counts = {"accepted": 0, "split": 0, "rejected": 0}
        for candidate in candidates:
            key = review_key_for_candidate(candidate)
            review = self._repository.get_storyline_review(key)
            if review is not None and review.status in {"accepted", "split", "rejected"}:
                persisted_counts[review.status] += 1
                continue
            if review is None:
                review = TrendStorylineReview(
                    review_key=key,
                    candidate_cluster_id=candidate.candidate_cluster_id,
                    embedding_version=candidate.embedding_version,
                    window_start_date=candidate.window_start_date,
                    window_end_date=candidate.window_end_date,
                    member_card_ids=[card.card_id for card in candidate.cards],
                    removed_card_ids=[],
                    status="pending",
                )
                self._repository.add_storyline_review(review)
                self._db.flush()
            else:
                review.candidate_cluster_id = candidate.candidate_cluster_id
                review.status = "pending"
                review.error_message = None
                review.completed_at = None
            tasks.append(StorylineReviewTask(review_id=review.review_id, candidate=candidate))
        self._db.commit()
        state = storyline_review_controller.start(
            tasks=tasks,
            start_date=resolved_start,
            end_date=resolved_end,
            embedding_version=version,
            accepted_count=persisted_counts["accepted"],
            split_count=persisted_counts["split"],
            rejected_count=persisted_counts["rejected"],
            process=lambda task: process_storyline_review(task=task, session_factory=SessionLocal, llm=LlmClient()),
        )
        message = "故事线审查任务已启动。" if tasks else "当前候选簇均已有可复用的审查结论。"
        return self._storyline_response(state, message=message)

    def list_storyline_reviews(
        self, *, offset: int, limit: int, status_filter: str | None = None
    ) -> TrendStorylineReviewListResponse:
        total, reviews = self._repository.list_storyline_reviews(
            offset=offset, limit=limit, status_filter=status_filter
        )
        return TrendStorylineReviewListResponse(
            total=total,
            offset=offset,
            limit=limit,
            items=[
                TrendStorylineReviewListItemResponse(
                    review_id=review.review_id,
                    candidate_cluster_id=review.candidate_cluster_id,
                    status=review.status,
                    decision=review.decision,
                    member_card_ids=review.member_card_ids,
                    removed_card_ids=review.removed_card_ids,
                    agent_review=review.agent_review,
                    error_message=review.error_message,
                    attempt_count=review.attempt_count,
                    created_at=review.created_at,
                    completed_at=review.completed_at,
                )
                for review in reviews
            ],
        )

    def list_storylines(self, *, offset: int, limit: int) -> TrendStorylineListResponse:
        total, rows = self._repository.list_storylines(offset=offset, limit=limit)
        return TrendStorylineListResponse(
            total=total,
            offset=offset,
            limit=limit,
            items=[
                TrendStorylineListItemResponse(
                    storyline_id=row.storyline.storyline_id,
                    title=row.storyline.title,
                    overall_start_date=row.storyline.overall_start_date,
                    overall_end_date=row.storyline.overall_end_date,
                    overall_influence_score=row.storyline.overall_influence_score,
                    cohesion_score=row.storyline.cohesion_score,
                    decision=row.storyline.decision,
                    status=row.storyline.status,
                    agent_review=row.storyline.agent_review,
                    members=[
                        TrendStorylineMemberResponse(card_id=member.card_id, at=member.at, membership=member.membership)
                        for member in row.members
                    ],
                )
                for row in rows
            ],
        )

    def get_trend_run_status(self, *, template_id: str) -> TrendRunStatusResponse:
        template = self._repository.get_identity_template(template_id)
        if template is None:
            raise TrendIdentityTemplateNotFoundError(template_id)
        runtime = trend_evaluation_controller.snapshot(template_id)
        if runtime is not None and runtime.is_running:
            return runtime_to_status_response(runtime)
        run = self._repository.get_latest_trend_run(template_id)
        if run is not None:
            self._reclaim_orphaned_run(run)
        if run is None:
            return TrendRunStatusResponse(
                run_id=None,
                template_id=template_id,
                is_running=False,
                status=None,
                window_start_date=None,
                window_end_date=None,
                trend_count=None,
                storyline_candidate_goal=None,
                candidate_count=0,
                completed_candidate_count=0,
                result_count=0,
                unverified_count=0,
                reusable=False,
                started_at=None,
                finished_at=None,
                error_message=None,
                retry_guidance=None,
                message="尚未运行趋势总结。",
            )
        result_count, unverified_count = self._repository.count_trend_results(run.run_id)
        return run_to_status_response(
            run,
            result_count=result_count,
            unverified_count=unverified_count,
            message=(
                "趋势总结已发布。"
                if run.status == "succeeded"
                else "趋势总结失败，未覆盖该模板最近成功结果。"
                if run.status == "failed"
                else None
            ),
        )

    def start_trend_run(self, payload: TrendRunTriggerRequest) -> TrendRunStatusResponse:
        template = self._repository.get_identity_template(payload.template_id)
        if template is None:
            raise TrendIdentityTemplateNotFoundError(payload.template_id)

        runtime = trend_evaluation_controller.snapshot(payload.template_id)
        if runtime is not None and runtime.is_running:
            return runtime_to_status_response(
                replace(runtime, reusable=True),
                message="已有该模板的趋势运行正在执行，本次触发复用现有任务。",
            )
        existing = self._repository.get_running_trend_run(payload.template_id)
        if existing is not None:
            # A process restart/reload can orphan a DB "running" row without an
            # in-memory worker. Reclaim it so the template is not permanently blocked.
            existing.status = "failed"
            existing.error_message = (
                existing.error_message
                or "趋势运行在进程重启后被回收；未发布不完整结果，最近成功发布保持不变。"
            )
            existing.finished_at = datetime.now(timezone.utc)
            self._db.commit()

        settings = self.get_settings()
        window_start, window_end = self._resolve_card_window(start_date=None, end_date=None)
        run = build_trend_run(
            template_id=template.template_id,
            window_start=window_start,
            window_end=window_end,
            trend_count=settings.trend_count,
            storyline_candidate_goal=settings.storyline_candidate_goal,
        )
        self._repository.add_trend_run(run)
        self._db.commit()
        self._db.refresh(run)
        state = trend_evaluation_controller.start(
            run=run,
            analysis_identity=template.identity_text,
            session_factory=SessionLocal,
        )
        return runtime_to_status_response(state)

    def get_trend_run(self, run_id: str) -> TrendRunStatusResponse:
        run = self._repository.get_trend_run(run_id)
        if run is None:
            raise TrendRunNotFoundError(run_id)
        runtime = trend_evaluation_controller.snapshot(run.template_id)
        if runtime is not None and runtime.is_running and runtime.run_id == run_id:
            return runtime_to_status_response(runtime)
        self._reclaim_orphaned_run(run)
        result_count, unverified_count = self._repository.count_trend_results(run.run_id)
        return run_to_status_response(run, result_count=result_count, unverified_count=unverified_count)

    def reclaim_orphaned_trend_runs(self) -> int:
        """Fail persisted runs that cannot survive this process startup.

        Trend evaluation workers are in-process daemon threads.  Once this web
        process starts, every pre-existing pending/running row belongs to an
        earlier process and cannot make further progress.
        """

        orphaned_runs = list(
            self._db.scalars(
                select(TrendRun).where(TrendRun.status.in_(("pending", "running")))
            )
        )
        for run in orphaned_runs:
            self._fail_orphaned_run(run)
        if orphaned_runs:
            self._db.commit()
        return len(orphaned_runs)

    def list_trend_runs(
        self, *, template_id: str, offset: int, limit: int
    ) -> TrendRunListResponse:
        template = self._repository.get_identity_template(template_id)
        if template is None:
            raise TrendIdentityTemplateNotFoundError(template_id)
        total, rows = self._repository.list_trend_runs(
            template_id=template_id, offset=offset, limit=limit
        )
        return TrendRunListResponse(
            total=total,
            offset=offset,
            limit=limit,
            items=[
                TrendRunListItemResponse(
                    run_id=run.run_id,
                    template_id=run.template_id,
                    status=run.status,  # type: ignore[arg-type]
                    window_start_date=run.window_start_date,
                    window_end_date=run.window_end_date,
                    trend_count=run.trend_count,
                    candidate_count=run.candidate_count,
                    completed_candidate_count=run.completed_candidate_count,
                    error_message=run.error_message,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    created_at=run.created_at,
                )
                for run in rows
            ],
        )

    def get_latest_trend_results(self, *, template_id: str) -> TrendLatestResultsResponse:
        template = self._repository.get_identity_template(template_id)
        if template is None:
            raise TrendIdentityTemplateNotFoundError(template_id)
        settings = self.get_settings()
        run = self._repository.get_latest_succeeded_trend_run(template_id)
        if run is None:
            return TrendLatestResultsResponse(
                template_id=template_id,
                run_id=None,
                window_start_date=None,
                window_end_date=None,
                trend_count=settings.trend_count,
                status=None,
                finished_at=None,
                items=[],
                message="当前模板尚无成功发布的趋势结果。",
            )
        entries = self._repository.list_trend_results_for_run(run.run_id)
        verified_count = sum(1 for entry in entries if entry.result.category != "unverified_change")
        return TrendLatestResultsResponse(
            template_id=template_id,
            run_id=run.run_id,
            window_start_date=run.window_start_date,
            window_end_date=run.window_end_date,
            trend_count=settings.trend_count,
            status="succeeded",
            finished_at=run.finished_at,
            items=[
                TrendResultResponse(
                    result_id=entry.result.result_id,
                    run_id=entry.result.run_id,
                    storyline_id=entry.result.storyline_id,
                    overall_start_date=entry.result.overall_start_date,
                    overall_end_date=entry.result.overall_end_date,
                    window_start_date=entry.result.window_start_date,
                    window_end_date=entry.result.window_end_date,
                    overall_score=entry.result.overall_score,
                    window_score=entry.result.window_score,
                    template_relevance_score=entry.result.template_relevance_score,
                    trend_rank_score=entry.result.trend_rank_score,
                    category=entry.result.category,  # type: ignore[arg-type]
                    topic=entry.result.topic,
                    trend_summary=entry.result.trend_summary,
                    agent_review=entry.result.agent_review,
                    item_ids=entry.item_ids,
                    sources=[
                        TrendResultItemResponse(item_id=row.item_id, title=row.title)
                        for row in entry.items
                    ],
                )
                for entry in entries
            ],
            message=(
                None
                if verified_count > 0
                else "当前范围内暂无可验证趋势。"
            ),
        )

    def get_trend_carousel(self, *, template_id: str) -> TrendCarouselResponse:
        """Return the top X verified trends of the latest successful run.

        Only trends whose referenced news actually intersects the currently
        resolved global window are shown, so changing the window narrows the
        carousel without needing a re-evaluation.
        """
        template = self._repository.get_identity_template(template_id)
        if template is None:
            raise TrendIdentityTemplateNotFoundError(template_id)
        settings = self.get_settings()
        run = self._repository.get_latest_succeeded_trend_run(template_id)
        if run is None:
            return TrendCarouselResponse(
                template_id=template_id,
                run_id=None,
                window_start_date=None,
                window_end_date=None,
                trend_count=settings.trend_count,
                finished_at=None,
                items=[],
                message="当前模板尚无成功发布的趋势结果。",
            )
        window_start, window_end = self._resolve_card_window(start_date=None, end_date=None)
        entries = self._repository.list_carousel_results(
            run_id=run.run_id,
            window_start=window_start,
            window_end=window_end,
        )
        visible = [entry for entry in entries if entry.window_item_count > 0][: settings.trend_count]
        return TrendCarouselResponse(
            template_id=template_id,
            run_id=run.run_id,
            window_start_date=window_start,
            window_end_date=window_end,
            trend_count=settings.trend_count,
            finished_at=run.finished_at,
            items=[
                TrendCarouselItemResponse(
                    result_id=entry.result.result_id,
                    storyline_id=entry.result.storyline_id,
                    category=entry.result.category,  # type: ignore[arg-type]
                    category_label=TREND_CATEGORY_LABELS.get(entry.result.category, entry.result.category),
                    topic=entry.result.topic or "",
                    trend_summary=entry.result.trend_summary or "",
                    trend_rank_score=entry.result.trend_rank_score,
                    window_start_date=entry.result.window_start_date,
                    window_end_date=entry.result.window_end_date,
                    window_item_count=entry.window_item_count,
                    item_ids=[row.item_id for row in entry.items],
                    sources=[
                        TrendResultItemResponse(item_id=row.item_id, title=row.title)
                        for row in entry.items
                    ],
                )
                for entry in visible
            ],
            message=None if visible else "当前范围内暂无可验证趋势。",
        )

    def resolve_active_window(self) -> tuple[date, date]:
        """Public window resolution shared by manual runs and the scheduler."""
        return self._resolve_card_window(start_date=None, end_date=None)

    def get_active_trend_run_id(self, template_id: str) -> str | None:
        """Return the in-flight run of one template, or None when idle."""
        runtime = trend_evaluation_controller.snapshot(template_id)
        if runtime is not None and runtime.is_running:
            return runtime.run_id
        return None

    def _reclaim_orphaned_run(self, run: TrendRun) -> None:
        if run.status not in {"pending", "running"}:
            return
        self._fail_orphaned_run(run)
        self._db.commit()

    @staticmethod
    def _fail_orphaned_run(run: TrendRun) -> None:
        run.status = "failed"
        run.error_message = ORPHANED_TREND_RUN_MESSAGE
        run.finished_at = datetime.now(timezone.utc)

    @staticmethod
    def _persist_card_outcome(
        db: Session,
        candidate: CardGenerationCandidate,
        outcome: CardGenerationOutcome,
    ) -> None:
        TrendRepository(db).save_card_generation_outcome(candidate, outcome)

    @staticmethod
    def _storyline_response(state: object, *, message: str | None = None) -> TrendStorylineStageStatusResponse:
        runtime = state
        return TrendStorylineStageStatusResponse(
            is_running=runtime.is_running,
            start_date=runtime.start_date,
            end_date=runtime.end_date,
            embedding_version=runtime.embedding_version,
            pending_count=runtime.pending_count,
            accepted_count=runtime.accepted_count,
            split_count=runtime.split_count,
            rejected_count=runtime.rejected_count,
            failed_count=runtime.failed_count,
            started_at=runtime.started_at,
            finished_at=runtime.finished_at,
            error_message=runtime.error_message,
            retry_guidance=("失败候选簇未写入故事线；修复 LLM 输出或服务后可重新触发。" if runtime.failed_count else None),
            message=message,
        )

    @staticmethod
    def _vector_response(state: object, *, message: str | None = None) -> TrendVectorStageStatusResponse:
        runtime = state
        return TrendVectorStageStatusResponse(
            is_running=runtime.is_running,
            start_date=runtime.start_date,
            end_date=runtime.end_date,
            embedding_version=current_embedding_descriptor().embedding_version,
            pending_count=runtime.pending_count,
            generated_count=runtime.generated_count,
            failed_count=runtime.failed_count,
            started_at=runtime.started_at,
            finished_at=runtime.finished_at,
            error_message=runtime.error_message,
            retry_guidance=(
                "检查 Embedding Worker 状态与版本配置后重新触发；失败批不会写入任何部分向量。"
                if runtime.failed_count or runtime.error_message else None
            ),
            message=message,
        )

    def _resolve_card_window(
        self,
        *,
        start_date: date | None,
        end_date: date | None,
    ) -> tuple[date, date]:
        if (start_date is None) != (end_date is None):
            raise ValueError("start_date 和 end_date 必须同时提供")
        if start_date is not None and end_date is not None:
            if start_date > end_date:
                raise ValueError("start_date 不能晚于 end_date")
            return start_date, end_date

        settings = self.get_settings()
        if settings.window_mode == "date_range":
            if settings.window_start_date is None or settings.window_end_date is None:
                raise ValueError("趋势全局设置缺少完整的日期范围")
            return settings.window_start_date, settings.window_end_date

        resolved_end = datetime.now(timezone.utc).date()
        unit_days = 7 if settings.relative_window_unit == "week" else 30
        resolved_start = resolved_end - timedelta(
            days=(settings.relative_window_value * unit_days) - 1
        )
        return resolved_start, resolved_end

    def _embedding_status(self) -> TrendEmbeddingStatusResponse:
        settings = get_trend_embedding_settings()
        # The web process owns no model: cache truth lives in the worker container.
        try:
            health = fetch_worker_health()
        except EmbeddingWorkerUnavailableError as exc:
            health = None
            worker_error: str | None = f"{exc.message} {exc.remedy}"
        else:
            worker_error = None

        expected_version = current_embedding_descriptor().embedding_version
        if health is not None and health.embedding_version != expected_version:
            worker_error = (
                f"Worker 的向量版本 {health.embedding_version} 与后端 {expected_version} 不一致；"
                "生成前请统一两侧的 TRENDS_EMBEDDING_* 配置，否则同批向量会落在不同向量空间。"
            )

        if health is None:
            state = self._repository.sync_embedding_state()
        else:
            state = self._repository.apply_embedding_cache_report(
                installed=health.cache_installed,
                model_version=health.model_version,
            )

        cache_installed = health.cache_installed if health else False
        return TrendEmbeddingStatusResponse(
            provider=state.provider,
            model_id=state.model_id,
            model_revision=state.model_revision,
            model_version=state.model_version,
            embedding_version=state.embedding_version,
            dimension=state.dimension,
            normalization=state.normalization,
            cache_dir=health.cache_dir if health else settings.cache_dir,
            cache_installed=cache_installed,
            status=state.status,
            error_message=state.error_message,
            remedy=state.remedy,
            worker_base_url=settings.worker_base_url,
            worker_reachable=health is not None,
            worker_active=health.worker_active if health else False,
            worker_error=worker_error,
            status_updated_at=state.status_updated_at,
        )
