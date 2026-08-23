from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_system_access
from app.morning_crawl.service import (
    MorningCrawlRunNotFoundError,
    MorningCrawlRetryNotAvailableError,
    config_to_response,
    get_morning_crawl_dashboard,
    get_run_detail,
    list_runs,
    resolve_default_run_id,
    retry_today_failed_methods_async,
    run_to_summary,
    stop_morning_crawl,
    trigger_morning_crawl_async,
    update_morning_crawl_config,
)
from app.schemas import (
    MorningCrawlConfigResponse,
    MorningCrawlConfigUpdateRequest,
    MorningCrawlDashboardResponse,
    MorningCrawlRunDetailResponse,
    MorningCrawlRunSummary,
)

router = APIRouter(prefix="/system-morning-crawl", tags=["system-morning-crawl"])


@router.get("")
def get_system_morning_crawl(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> MorningCrawlDashboardResponse:
    return get_morning_crawl_dashboard(db)


@router.put("")
def update_system_morning_crawl(
    request: MorningCrawlConfigUpdateRequest,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> MorningCrawlConfigResponse:
    return config_to_response(update_morning_crawl_config(db, request))


@router.post("/run-now", status_code=202)
def run_system_morning_crawl_now(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> MorningCrawlRunSummary:
    return run_to_summary(trigger_morning_crawl_async(db, trigger_type="manual"))


@router.post("/retry-today", status_code=202)
def retry_system_morning_crawl_today(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> MorningCrawlRunSummary:
    try:
        return run_to_summary(retry_today_failed_methods_async(db))
    except MorningCrawlRetryNotAvailableError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/stop")
def stop_system_morning_crawl(
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> MorningCrawlDashboardResponse:
    stop_morning_crawl(db)
    return get_morning_crawl_dashboard(db)


@router.get("/runs")
def list_system_morning_crawl_runs(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> dict:
    return {
        "runs": list_runs(db, limit=limit),
        "default_run_id": resolve_default_run_id(db),
    }


@router.get("/runs/{run_id}")
def get_system_morning_crawl_run(
    run_id: int,
    db: Session = Depends(get_db),
    _access: dict = Depends(require_system_access),
) -> MorningCrawlRunDetailResponse:
    try:
        return get_run_detail(db, run_id)
    except MorningCrawlRunNotFoundError:
        raise HTTPException(404, "run not found")
