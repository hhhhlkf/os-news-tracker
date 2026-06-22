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

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models import (
    AgentCrawlRun,
    AgentSiteMemory,
    AgentSourceConfig,
    Source,
    User,
)
from app.scheduler import run_source_job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sources/agent", tags=["agent-sources"])


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


def _start_agent_source_run(source_id: int) -> None:
    """在后台线程中触发单源 agent crawl。"""
    threading.Thread(
        target=run_source_job,
        args=(source_id,),
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

@router.get("")
def list_agent_sources(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
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


@router.post("", status_code=201)
def create_agent_source(
    body: AgentSourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
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


@router.put("/{source_id}")
def update_agent_source(
    source_id: int,
    body: AgentSourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
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


@router.delete("/{source_id}", status_code=204)
def delete_agent_source(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除 agent_crawl 源。

    AgentSourceConfig、AgentSiteMemory、AgentCrawlRun 通过外键 CASCADE 自动清理。
    """
    source = db.get(Source, source_id)
    if source is None or source.type != "agent_crawl":
        raise HTTPException(status_code=404, detail="Agent source not found")
    db.delete(source)
    db.commit()


# ── 运行记录 ──────────────────────────────────────────────────────

@router.get("/{source_id}/runs")
def list_runs(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
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


@router.post("/{source_id}/run", status_code=202)
def trigger_agent_run(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """立即触发指定 agent source 的一次抓取。"""
    source = db.get(Source, source_id)
    if source is None or source.type != "agent_crawl":
        raise HTTPException(status_code=404, detail="Agent source not found")

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

    _start_agent_source_run(source_id)
    return {
        "message": "Agent crawl accepted",
        "source_id": source_id,
        "accepted": True,
    }


# ── SiteMemory 管理（管理员专用）──────────────────────────────────

@router.get("/{source_id}/memory")
def view_memory(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查看指定源的 SiteMemory 记录（最近 100 条）。管理员专用。"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

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


@router.delete("/{source_id}/memory/{url_pattern:path}", status_code=204)
def delete_memory_record(
    source_id: int,
    url_pattern: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除指定的 SiteMemory 记录。管理员专用。

    url_pattern 为完整的 URL pattern（含斜杠），由 FastAPI :path 转换器捕获。
    """
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

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
