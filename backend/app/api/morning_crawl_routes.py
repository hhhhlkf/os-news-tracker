from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.morning_crawl.service import (
    MorningCrawlRunNotFoundError,
    config_to_response,
    get_morning_crawl_dashboard,
    get_run_detail,
    list_runs,
    resolve_default_run_id,
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
def get_system_morning_crawl(db: Session = Depends(get_db)) -> MorningCrawlDashboardResponse:
    return get_morning_crawl_dashboard(db)


@router.put("")
def update_system_morning_crawl(
    request: MorningCrawlConfigUpdateRequest, db: Session = Depends(get_db)
) -> MorningCrawlConfigResponse:
    return config_to_response(update_morning_crawl_config(db, request))


@router.post("/run-now", status_code=202)
def run_system_morning_crawl_now(db: Session = Depends(get_db)) -> MorningCrawlRunSummary:
    return run_to_summary(trigger_morning_crawl_async(db, trigger_type="manual"))


@router.post("/stop")
def stop_system_morning_crawl(db: Session = Depends(get_db)) -> MorningCrawlDashboardResponse:
    stop_morning_crawl(db)
    return get_morning_crawl_dashboard(db)


@router.get("/runs")
def list_system_morning_crawl_runs(
    limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)
) -> dict:
    return {
        "runs": list_runs(db, limit=limit),
        "default_run_id": resolve_default_run_id(db),
    }


@router.get("/runs/{run_id}")
def get_system_morning_crawl_run(
    run_id: int, db: Session = Depends(get_db)
) -> MorningCrawlRunDetailResponse:
    try:
        return get_run_detail(db, run_id)
    except MorningCrawlRunNotFoundError:
        raise HTTPException(404, "run not found")
