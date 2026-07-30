from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_system_access
from app.trends.embedding_client import EmbeddingWorkerRejectedError, EmbeddingWorkerUnavailableError
from app.trends.models import TrendIdentityTemplate, TrendSettings
from app.trends.scheduler import build_schedule_status
from app.trends.schemas import (
    TrendCardBackfillRequest,
    TrendCardListResponse,
    TrendCardListStatus,
    TrendCardStageStatusResponse,
    TrendCarouselResponse,
    TrendCarouselTemplateResponse,
    TrendClusterRunRequest,
    TrendCandidateClusterListResponse,
    TrendClusterStageStatusResponse,
    TrendEmbeddingStatusResponse,
    TrendIdentityTemplateCreateRequest,
    TrendIdentityTemplateResponse,
    TrendLatestResultsResponse,
    TrendRunListResponse,
    TrendRunStatusResponse,
    TrendRunTriggerRequest,
    TrendScheduleStatusResponse,
    TrendSettingsResponse,
    TrendSettingsUpdateRequest,
    TrendStorylineListResponse,
    TrendStorylineReviewListResponse,
    TrendStorylineReviewRequest,
    TrendStorylineStageStatusResponse,
    TrendVectorBackfillRequest,
    TrendVectorStageStatusResponse,
)
from app.trends.service import (
    TrendIdentityTemplateNotFoundError,
    TrendRunNotFoundError,
    TrendService,
)

router = APIRouter(prefix="/trends", tags=["trends"])


def template_to_response(template: TrendIdentityTemplate) -> TrendIdentityTemplateResponse:
    return TrendIdentityTemplateResponse(
        template_id=template.template_id,
        name=template.name,
        identity_text=template.identity_text,
        created_at=template.created_at,
    )


def settings_to_response(settings: TrendSettings) -> TrendSettingsResponse:
    return TrendSettingsResponse(
        window_mode=settings.window_mode,
        window_start_date=settings.window_start_date,
        window_end_date=settings.window_end_date,
        relative_window_unit=settings.relative_window_unit,
        relative_window_value=settings.relative_window_value,
        trend_count=settings.trend_count,
        storyline_candidate_goal=settings.storyline_candidate_goal,
        trigger_mode=settings.trigger_mode,
        schedule_rule=settings.schedule_rule,
        scheduled_template_id=settings.scheduled_template_id,
    )


@router.get("/templates")
def list_trend_identity_templates(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> list[TrendIdentityTemplateResponse]:
    return [template_to_response(template) for template in TrendService(db).list_identity_templates()]


@router.post("/templates", status_code=status.HTTP_201_CREATED)
def create_trend_identity_template(
    payload: TrendIdentityTemplateCreateRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendIdentityTemplateResponse:
    return template_to_response(TrendService(db).create_identity_template(payload))


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trend_identity_template(
    template_id: str,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> None:
    try:
        TrendService(db).delete_identity_template(template_id)
    except TrendIdentityTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/settings")
def get_trend_settings(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendSettingsResponse:
    return settings_to_response(TrendService(db).get_settings())


@router.put("/settings")
def update_trend_settings(
    payload: TrendSettingsUpdateRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendSettingsResponse:
    try:
        return settings_to_response(TrendService(db).update_settings(payload))
    except TrendIdentityTemplateNotFoundError as exc:
        raise HTTPException(status_code=422, detail="scheduled_template_id does not reference an existing template") from exc


@router.get("/embedding/status")
def get_trend_embedding_status(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendEmbeddingStatusResponse:
    return TrendService(db).get_embedding_status()


@router.post("/embedding/prepare", status_code=status.HTTP_202_ACCEPTED)
def prepare_trend_embedding_model(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendEmbeddingStatusResponse:
    try:
        return TrendService(db).prepare_embedding_model()
    except EmbeddingWorkerRejectedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except EmbeddingWorkerUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"{exc.message} {exc.remedy}") from exc


@router.get("/cards/status")
def get_trend_card_stage_status(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendCardStageStatusResponse:
    try:
        return TrendService(db).get_card_stage_status(
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/cards")
def list_trend_news_explanation_cards(
    card_status: TrendCardListStatus | None = Query(default=None, alias="status"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendCardListResponse:
    try:
        return TrendService(db).list_card_stage_items(
            card_status=card_status,
            offset=offset,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/cards/backfill", status_code=status.HTTP_202_ACCEPTED)
def backfill_trend_news_explanation_cards(
    payload: TrendCardBackfillRequest | None = None,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendCardStageStatusResponse:
    try:
        return TrendService(db).start_card_backfill(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/vectors/status")
def get_trend_vector_stage_status(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendVectorStageStatusResponse:
    try:
        return TrendService(db).get_vector_stage_status(start_date=start_date, end_date=end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/vectors/backfill", status_code=status.HTTP_202_ACCEPTED)
def backfill_trend_vectors(
    payload: TrendVectorBackfillRequest | None = None,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendVectorStageStatusResponse:
    try:
        return TrendService(db).start_vector_backfill(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/clusters/status")
def get_trend_cluster_stage_status(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendClusterStageStatusResponse:
    try:
        return TrendService(db).get_cluster_stage_status(start_date=start_date, end_date=end_date)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/clusters")
def list_trend_candidate_clusters(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendCandidateClusterListResponse:
    try:
        return TrendService(db).list_candidate_clusters(offset=offset, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/clusters/run", status_code=status.HTTP_202_ACCEPTED)
def run_trend_candidate_clustering(
    payload: TrendClusterRunRequest | None = None,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendClusterStageStatusResponse:
    try:
        return TrendService(db).run_candidate_clustering(payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/storylines/status")
def get_trend_storyline_stage_status(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendStorylineStageStatusResponse:
    try:
        return TrendService(db).get_storyline_stage_status(start_date=start_date, end_date=end_date)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/storylines/review", status_code=status.HTTP_202_ACCEPTED)
def review_trend_candidate_clusters(
    payload: TrendStorylineReviewRequest | None = None,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendStorylineStageStatusResponse:
    try:
        return TrendService(db).start_storyline_review(payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/storylines/reviews")
def list_trend_storyline_reviews(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    review_status: Literal["accepted", "split", "rejected", "unreviewed"] | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendStorylineReviewListResponse:
    return TrendService(db).list_storyline_reviews(
        offset=offset, limit=limit, status_filter=review_status
    )


@router.get("/storylines")
def list_trend_storylines(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendStorylineListResponse:
    return TrendService(db).list_storylines(offset=offset, limit=limit)


@router.get("/runs/status")
def get_trend_run_status(
    template_id: str = Query(min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendRunStatusResponse:
    try:
        return TrendService(db).get_trend_run_status(template_id=template_id)
    except TrendIdentityTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
def start_trend_run(
    payload: TrendRunTriggerRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendRunStatusResponse:
    try:
        return TrendService(db).start_trend_run(payload)
    except TrendIdentityTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/runs")
def list_trend_runs(
    template_id: str = Query(min_length=1, max_length=36),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendRunListResponse:
    try:
        return TrendService(db).list_trend_runs(template_id=template_id, offset=offset, limit=limit)
    except TrendIdentityTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}")
def get_trend_run(
    run_id: str,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendRunStatusResponse:
    try:
        return TrendService(db).get_trend_run(run_id)
    except TrendRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/results/latest")
def get_latest_trend_results(
    template_id: str = Query(min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendLatestResultsResponse:
    try:
        return TrendService(db).get_latest_trend_results(template_id=template_id)
    except TrendIdentityTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/results/carousel")
def get_trend_carousel(
    template_id: str = Query(min_length=1, max_length=36),
    db: Session = Depends(get_db),
) -> TrendCarouselResponse:
    try:
        return TrendService(db).get_trend_carousel(template_id=template_id)
    except TrendIdentityTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/results/carousel/templates")
def list_trend_carousel_templates(
    db: Session = Depends(get_db),
) -> list[TrendCarouselTemplateResponse]:
    """List public template names without exposing their identity instructions."""
    return [
        TrendCarouselTemplateResponse(template_id=template.template_id, name=template.name)
        for template in TrendService(db).list_identity_templates()
    ]


@router.get("/schedule/status")
def get_trend_schedule_status(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> TrendScheduleStatusResponse:
    return build_schedule_status(db)
