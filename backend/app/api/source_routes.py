from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.source_cleanup import delete_source_and_related
from app.enums import MAIN_CATEGORIES, SourceType, Stream
from app.models import Source
from app.sources.detector import SourceDetectionError, detect_source

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sources", tags=["sources"])
UNABLE_TO_DETECT_DETAIL = "无法识别链接形态，请改用 RSS/API/网页首页链接"

_VALID_SOURCE_TYPES = {"rss", "api", "page_monitor", "search"}


class SourceDetectRequest(BaseModel):
    url: HttpUrl


class ProbeFieldsConfig(BaseModel):
    title: str | None = None
    url: str | None = None
    url_template: str | None = None
    published_at: str | None = None
    content: list[str] | str | None = None


class ProbeConfig(BaseModel):
    mode: str = "json_list"
    method: str = "GET"
    url: str | None = None
    headers: dict[str, str] | None = None
    query: dict[str, str] | None = None
    json_body: dict[str, Any] | None = None
    items_path: str | None = None
    fields: ProbeFieldsConfig = Field(default_factory=ProbeFieldsConfig)


class SourceCreateRequest(BaseModel):
    url: HttpUrl
    name: str | None = None
    main_category: str
    type: str | None = None
    adapter: str | None = None
    api_config: dict[str, Any] | None = None


class XhrDetectRequest(BaseModel):
    url: HttpUrl


class XhrSelectRequest(BaseModel):
    page_url: str
    candidates: list[dict[str, Any]]


def _detect_or_422(url: str):
    try:
        return detect_source(url)
    except SourceDetectionError as exc:
        raise HTTPException(status_code=422, detail=UNABLE_TO_DETECT_DETAIL) from exc


def _source_response(source: Source) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "url": source.url,
        "type": source.type,
        "main_category": source.main_category,
        "enabled": source.enabled,
    }


@router.post("/detect")
def detect_source_route(body: SourceDetectRequest):
    result = _detect_or_422(str(body.url))
    return {
        "detected_type": result.detected_type,
        "name_suggestion": result.name_suggestion,
        "api_config": result.api_config,
        "notes": result.notes,
    }


@router.post("", status_code=201)
def create_source(
    body: SourceCreateRequest,
    db: Session = Depends(get_db),
):
    if body.main_category not in MAIN_CATEGORIES:
        raise HTTPException(status_code=422, detail="未知内容类型")

    if body.type and body.type in _VALID_SOURCE_TYPES:
        source_type = body.type
        api_config = body.api_config
        name = (body.name or "").strip() or str(body.url)
    else:
        result = _detect_or_422(str(body.url))
        source_type = result.detected_type
        api_config = result.api_config
        name = (body.name or result.name_suggestion).strip()

    source = Source(
        name=name,
        type=source_type,
        url=str(body.url),
        api_config=api_config,
        adapter=body.adapter,
        main_category=body.main_category,
        stream=Stream.NEWS,
        enabled=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return _source_response(source)


@router.get("")
def list_sources(db: Session = Depends(get_db)):
    sources = db.scalars(
        select(Source)
        .where(Source.stream == Stream.NEWS)
        .order_by(Source.name.asc(), Source.id.asc())
    ).all()
    return [_source_response(source) for source in sources]


@router.delete("/{source_id}", status_code=204)
def delete_source(source_id: int, db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    delete_source_and_related(db, source)


@router.post("/detect-xhr")
def detect_xhr_route(body: XhrDetectRequest):
    """用浏览器引擎加载网页，拦截 XHR/Fetch JSON 响应，返回打分后的候选列表。"""
    from app.sources.xhr_detector import detect_xhr_apis

    try:
        candidates = detect_xhr_apis(str(body.url))
    except Exception as exc:
        logger.warning("XHR detection failed for %s: %s", body.url, exc)
        raise HTTPException(
            status_code=502,
            detail=f"浏览器引擎加载失败: {exc}",
        ) from exc
    return {"page_url": str(body.url), "candidates": candidates, "total": len(candidates)}


@router.post("/select-xhr")
def select_xhr_route(body: XhrSelectRequest):
    """让 Agent 从 XHR 候选列表中选择最适合抓取的 API。"""
    from app.sources.xhr_agent import select_best_xhr_candidate

    try:
        result = select_best_xhr_candidate(body.page_url, body.candidates)
    except Exception as exc:
        logger.warning("XHR agent selection failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Agent 选择失败: {exc}",
        ) from exc
    return result
