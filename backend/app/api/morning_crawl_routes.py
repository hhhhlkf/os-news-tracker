from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.morning_crawl.service import (
    config_to_response,
    get_morning_crawl_dashboard,
    run_to_summary,
    trigger_morning_crawl_async,
    update_morning_crawl_config,
)
from app.schemas import (
    MorningCrawlConfigResponse,
    MorningCrawlConfigUpdateRequest,
    MorningCrawlDashboardResponse,
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
