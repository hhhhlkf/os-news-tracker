from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.source_cleanup import delete_source_and_related
from app.enums import MAIN_CATEGORIES, Stream
from app.models import Source
from app.sources.detector import SourceDetectionError, detect_source

router = APIRouter(prefix="/sources", tags=["sources"])
UNABLE_TO_DETECT_DETAIL = "无法识别链接形态，请改用 RSS/API/网页首页链接"


class SourceDetectRequest(BaseModel):
    url: HttpUrl


class SourceCreateRequest(BaseModel):
    url: HttpUrl
    name: str | None = None
    main_category: str


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

    result = _detect_or_422(str(body.url))
    source = Source(
        name=(body.name or result.name_suggestion).strip(),
        type=result.detected_type,
        url=str(body.url),
        api_config=result.api_config,
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
