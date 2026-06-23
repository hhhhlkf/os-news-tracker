"""/sources/agent API — Agent Crawl 源的 CRUD 及运行记录、SiteMemory 查看。

端点概览：
- GET    /sources/agent                    列出所有 agent crawl 源
- POST   /sources/agent                    创建 agent crawl 源
- PUT    /sources/agent/{source_id}         更新 agent crawl 源
- DELETE /sources/agent/{source_id}         删除 agent crawl 源
- GET    /sources/agent/{source_id}/runs   查看最近 20 次运行记录
- GET    /sources/agent/{source_id}/memory 查看 SiteMemory（管理员）
- DELETE /sources/agent/{source_id}/memory/{url_pattern}  删除记忆记录（管理员）
"""

import logging
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.source_cleanup import delete_source_and_related
from app.api.deps import get_db
from app.enums import SourceType, Stream
from app.models import (
    AgentCrawlRun,
    AgentSiteMemory,
    AgentSourceConfig,
    Source,
)
from app.scheduler import run_source_job
from app.schemas import AgentCrawlRunRequest
from app.sources.registry import seed_sources_from_yaml

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sources/agent", tags=["agent-sources"])
public_router = APIRouter(prefix="/crawl-sources", tags=["agent-sources"])
SEED_SOURCES_YAML = Path(__file__).resolve().parent.parent / "sources" / "seed_sources.yaml"


# ── 请求模型 ──────────────────────────────────────────────────────

class AgentSourceCreate(BaseModel):
    """创建/更新 Agent Crawl 源的请求体。"""
    name: str
    root_url: str
    focus_areas: list[str] = []
    topic_groups: list[str] = []
    crawl_depth: int = 1
    max_urls_per_run: int = 20
    quality_threshold: int = 4
    crawl_workers: int = 5
    quality_workers: int = 3
    summary_workers: int = 3


def _start_agent_source_run(source_id: int, time_window: dict | None = None) -> None:
    """在后台线程中触发单源 agent crawl。"""
    threading.Thread(
        target=run_source_job,
        args=(source_id,),
        kwargs={"agent_time_window": time_window},
        daemon=True,
        name=f"agent-source-run-{source_id}",
    ).start()


def _find_active_agent_run(db: Session, source_id: int) -> AgentCrawlRun | None:
    """查找同源当前活跃的运行记录。"""
    return db.scalars(
        select(AgentCrawlRun)
        .where(
            AgentCrawlRun.source_id == source_id,
            AgentCrawlRun.status == "running",
        )
        .order_by(AgentCrawlRun.started_at.desc())
        .limit(1)
    ).first()


def _build_default_agent_source_config(candidate: Source) -> AgentSourceConfig:
    """将标准抓取来源转换成默认 Agent source 配置。"""
    focus_areas = [candidate.name] if candidate.name else []
    topic_groups = [candidate.main_category] if candidate.main_category else []
    return AgentSourceConfig(
        focus_areas=focus_areas,
        topic_groups=topic_groups,
        crawl_depth=1,
        max_urls_per_run=20,
        quality_threshold=4,
        crawl_workers=5,
        quality_workers=3,
        summary_workers=3,
    )


def _list_standard_agent_candidates(db: Session) -> list[Source]:
    return db.scalars(
        select(Source)
        .where(
            Source.enabled.is_(True),
            Source.stream == Stream.NEWS,
            Source.type != SourceType.AGENT_CRAWL,
        )
        .order_by(Source.name.asc())
    ).all()


def _ensure_seed_candidates_available(db: Session) -> list[Source]:
    sources = _list_standard_agent_candidates(db)
    if sources:
        return sources

    if not SEED_SOURCES_YAML.exists():
        logger.warning("Agent candidate seed YAML missing: %s", SEED_SOURCES_YAML)
        return sources

    summary = seed_sources_from_yaml(db, str(SEED_SOURCES_YAML))
    logger.info("Agent candidate list backfilled from seed YAML: %s", summary)
    return _list_standard_agent_candidates(db)


def _candidate_response(source: Source) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "url": source.url,
        "source_type": source.type,
        "main_category": source.main_category,
    }


def _ensure_agent_source_for_candidate(db: Session, candidate: Source) -> tuple[Source, bool]:
    """为标准抓取来源复用或创建对应的 agent source。"""
    api_config = None
    if candidate.type == SourceType.RSS:
        api_config = {
            "seed_type": "rss",
            "seed_url": candidate.url,
            "candidate_source_id": candidate.id,
        }

    existing = db.scalars(
        select(Source)
        .where(
            Source.type == SourceType.AGENT_CRAWL,
            Source.url == candidate.url,
        )
        .limit(1)
    ).first()
    if existing is not None:
        if api_config and not existing.api_config:
            existing.api_config = api_config
            db.commit()
        return existing, False

    source = Source(
        name=candidate.name,
        type=SourceType.AGENT_CRAWL,
        url=candidate.url,
        api_config=api_config,
        main_category=candidate.main_category,
        stream=Stream.NEWS,
        enabled=True,
    )
    db.add(source)
    db.flush()

    config = _build_default_agent_source_config(candidate)
    config.source_id = source.id
    db.add(config)
    db.commit()
    db.refresh(source)
    return source, True


def _time_window_payload(body: AgentCrawlRunRequest | None) -> dict:
    request = body or AgentCrawlRunRequest()
    return request.model_dump(mode="json")


def _trigger_response_for_agent_source(
    source_id: int,
    db: Session,
    time_window: dict | None = None,
):
    active_run = _find_active_agent_run(db, source_id)
    if active_run is not None:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Agent source is already running",
                "run_id": active_run.id,
                "current_stage": active_run.current_stage or "planning",
            },
        )

    _start_agent_source_run(source_id, time_window)
    return None


# ── 响应辅助 ──────────────────────────────────────────────────────

def _source_response(source: Source, config: AgentSourceConfig | None) -> dict:
    """将 Source + AgentSourceConfig 组装为 API 响应格式。"""
    return {
        "id": source.id,
        "name": source.name,
        "url": source.url,
        "enabled": source.enabled,
        "config": {
            "focus_areas": config.focus_areas if config else [],
            "topic_groups": config.topic_groups if config else [],
            "crawl_depth": config.crawl_depth if config else 1,
            "max_urls_per_run": config.max_urls_per_run if config else 20,
            "quality_threshold": config.quality_threshold if config else 4,
            "crawl_workers": config.crawl_workers if config else 5,
            "quality_workers": config.quality_workers if config else 3,
            "summary_workers": config.summary_workers if config else 3,
        } if config else None,
    }


# ── CRUD 端点 ─────────────────────────────────────────────────────

@public_router.get("")
@router.get("")
def list_agent_sources(
    db: Session = Depends(get_db),
):
    """列出所有 agent_crawl 类型的源及其配置。"""
    sources = db.scalars(
        select(Source).where(Source.type == "agent_crawl")
    ).all()
    result = []
    for s in sources:
        cfg = db.get(AgentSourceConfig, s.id)
        result.append(_source_response(s, cfg))
    return result


@public_router.get("/candidates")
@router.get("/candidates")
def list_agent_source_candidates(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=50),
):
    """列出可一键转为 Agent Crawl 的标准抓取来源。"""
    sources = _ensure_seed_candidates_available(db)
    total = len(sources)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "items": [_candidate_response(source) for source in sources[start:end]],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@public_router.post("/candidates/{candidate_source_id}/run", status_code=202)
@router.post("/candidates/{candidate_source_id}/run", status_code=202)
def trigger_agent_run_from_candidate(
    candidate_source_id: int,
    body: AgentCrawlRunRequest | None = None,
    db: Session = Depends(get_db),
):
    """从标准抓取来源一键创建/复用 agent source 并立即运行。"""
    candidate = db.get(Source, candidate_source_id)
    if (
        candidate is None
        or candidate.type == SourceType.AGENT_CRAWL
        or candidate.stream != Stream.NEWS
    ):
        raise HTTPException(status_code=404, detail="Candidate source not found")

    agent_source, created = _ensure_agent_source_for_candidate(db, candidate)
    time_window = _time_window_payload(body)
    conflict = _trigger_response_for_agent_source(agent_source.id, db, time_window)
    if conflict is not None:
        return conflict

    return {
        "accepted": True,
        "created": created,
        "candidate_source_id": candidate_source_id,
        "agent_source_id": agent_source.id,
        "message": "Agent crawl accepted",
    }


@public_router.post("", status_code=201)
@router.post("", status_code=201)
def create_agent_source(
    body: AgentSourceCreate,
    db: Session = Depends(get_db),
):
    """创建新的 agent_crawl 源。

    同时创建 Source 记录（type=agent_crawl, stream=news）和
    对应的 AgentSourceConfig 配置记录。
    """
    source = Source(
        name=body.name,
        type="agent_crawl",
        url=body.root_url,
        stream="news",
        enabled=True,
    )
    db.add(source)
    db.flush()

    config = AgentSourceConfig(
        source_id=source.id,
        focus_areas=body.focus_areas,
        topic_groups=body.topic_groups,
        crawl_depth=body.crawl_depth,
        max_urls_per_run=body.max_urls_per_run,
        quality_threshold=body.quality_threshold,
        crawl_workers=body.crawl_workers,
        quality_workers=body.quality_workers,
        summary_workers=body.summary_workers,
    )
    db.add(config)
    db.commit()
    return _source_response(source, config)


@public_router.put("/{source_id}")
@router.put("/{source_id}")
def update_agent_source(
    source_id: int,
    body: AgentSourceCreate,
    db: Session = Depends(get_db),
):
    """更新 agent_crawl 源的基本信息及配置。"""
    source = db.get(Source, source_id)
    if source is None or source.type != "agent_crawl":
        raise HTTPException(status_code=404, detail="Agent source not found")

    source.name = body.name
    source.url = body.root_url

    config = db.get(AgentSourceConfig, source_id)
    if config is None:
        config = AgentSourceConfig(source_id=source_id)
        db.add(config)

    config.focus_areas = body.focus_areas
    config.topic_groups = body.topic_groups
    config.crawl_depth = body.crawl_depth
    config.max_urls_per_run = body.max_urls_per_run
    config.quality_threshold = body.quality_threshold
    config.crawl_workers = body.crawl_workers
    config.quality_workers = body.quality_workers
    config.summary_workers = body.summary_workers
    db.commit()
    return _source_response(source, config)


@public_router.delete("/{source_id}", status_code=204)
@router.delete("/{source_id}", status_code=204)
def delete_agent_source(
    source_id: int,
    db: Session = Depends(get_db),
):
    """删除 agent_crawl 源及其所有关联数据。

    多张关联表（items、item_sources、agent_crawl_runs）的 source_id 外键
    未设 ON DELETE CASCADE，需按依赖顺序手动清理，避免 ForeignKeyViolation。

    清理顺序：
    1. item_sources（引用 items 和 sources）
    2. items（引用 sources）
    3. agent_crawl_runs（引用 sources）
    4. source 本身（agent_source_configs + agent_site_memory 有 CASCADE，自动清理）
    """
    source = db.get(Source, source_id)
    if source is None or source.type != "agent_crawl":
        raise HTTPException(status_code=404, detail="Agent source not found")
    delete_source_and_related(db, source)


# ── 运行记录 ──────────────────────────────────────────────────────

@public_router.get("/{source_id}/runs")
@router.get("/{source_id}/runs")
def list_runs(
    source_id: int,
    db: Session = Depends(get_db),
):
    """查看指定源的最近 20 次运行记录。"""
    runs = db.scalars(
        select(AgentCrawlRun)
        .where(AgentCrawlRun.source_id == source_id)
        .order_by(AgentCrawlRun.started_at.desc())
        .limit(20)
    ).all()
    return [
        {
            "id": r.id,
            "status": r.status,
            "current_stage": r.current_stage,
            "stage_message": r.stage_message,
            "plan_urls_count": r.plan_urls_count,
            "fetched_count": r.fetched_count,
            "quality_passed": r.quality_passed,
            "items_created": r.items_created,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "error_message": r.error_message,
        }
        for r in runs
    ]


@public_router.post("/{source_id}/run", status_code=202)
@router.post("/{source_id}/run", status_code=202)
def trigger_agent_run(
    source_id: int,
    body: AgentCrawlRunRequest | None = None,
    db: Session = Depends(get_db),
):
    """立即触发指定 agent source 的一次抓取。"""
    source = db.get(Source, source_id)
    if source is None or source.type != "agent_crawl":
        raise HTTPException(status_code=404, detail="Agent source not found")

    time_window = _time_window_payload(body)
    conflict = _trigger_response_for_agent_source(source_id, db, time_window)
    if conflict is not None:
        return conflict
    return {
        "message": "Agent crawl accepted",
        "source_id": source_id,
        "accepted": True,
    }


# ── 取消运行 ────────────────────────────────────────────────────────

@public_router.post("/{source_id}/runs/{run_id}/cancel", status_code=200)
@router.post("/{source_id}/runs/{run_id}/cancel", status_code=200)
def cancel_agent_run(
    source_id: int,
    run_id: int,
    db: Session = Depends(get_db),
):
    """取消指定的运行记录。

    将 status 标记为 failed，stage_message 设为「已取消」。
    后台线程仍会继续运行至自然结束，但 UI 立即反映取消状态。
    """
    from datetime import datetime, timezone
    run = db.get(AgentCrawlRun, run_id)
    if run is None or run.source_id != source_id:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in ("running",):
        raise HTTPException(status_code=409, detail=f"Run is already {run.status}")
    run.status = "failed"
    run.stage_message = "已取消"
    run.current_stage = "failed"
    run.completed_at = datetime.now(timezone.utc)
    db.commit()
    return {"cancelled": True, "run_id": run_id}


# ── SiteMemory 管理（管理员专用）──────────────────────────────────

@public_router.get("/{source_id}/memory")
@router.get("/{source_id}/memory")
def view_memory(
    source_id: int,
    db: Session = Depends(get_db),
):
    """查看指定源的 SiteMemory 记录（最近 100 条）。"""
    records = db.scalars(
        select(AgentSiteMemory)
        .where(AgentSiteMemory.source_id == source_id)
        .order_by(AgentSiteMemory.last_seen_at.desc())
        .limit(100)
    ).all()
    return [
        {
            "url_pattern": r.url_pattern,
            "verdict": r.verdict,
            "score": r.quality_score,
            "reason": r.quality_reason,
            "seen_count": r.seen_count,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        }
        for r in records
    ]


@public_router.delete("/{source_id}/memory/{url_pattern:path}", status_code=204)
@router.delete("/{source_id}/memory/{url_pattern:path}", status_code=204)
def delete_memory_record(
    source_id: int,
    url_pattern: str,
    db: Session = Depends(get_db),
):
    """删除指定的 SiteMemory 记录。

    url_pattern 为完整的 URL pattern（含斜杠），由 FastAPI :path 转换器捕获。
    """
    record = db.scalars(
        select(AgentSiteMemory)
        .where(
            AgentSiteMemory.source_id == source_id,
            AgentSiteMemory.url_pattern == url_pattern,
        )
    ).first()
    if record:
        db.delete(record)
        db.commit()
