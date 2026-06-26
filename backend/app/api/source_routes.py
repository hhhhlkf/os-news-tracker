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
from app.sources.html_list_discovery import discover_html_list_source

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
    link_selector: str | None = None
    title_selector: str | None = None
    date_selector: str | None = None


class DiscoverRequest(BaseModel):
    """智能探测请求：探测页面背后的 JSON API 并生成 probe 配置。"""
    url: HttpUrl
    create_source: bool = False
    name: str | None = None
    main_category: str | None = None


class CreateFromProbeRequest(BaseModel):
    """用已探测出的 probe 配置创建标准 API 来源（无需重新探测）。"""
    api_url: str
    method: str = "GET"
    items_path: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
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

    if body.type and body.type in _VALID_SOURCE_TYPES:
        source_type = body.type
        api_config = body.api_config
        name = (body.name or "").strip() or str(body.url)
    else:
        result = _detect_or_422(str(body.url))
        source_type = result.detected_type
        api_config = result.api_config
        name = (body.name or result.name_suggestion).strip()

    link_selector = body.link_selector
    title_selector = body.title_selector
    date_selector = body.date_selector
    if source_type == SourceType.PAGE_MONITOR.value and not link_selector:
        discovery = discover_html_list_source(str(body.url))
        if not discovery.success or not discovery.link_selector:
            raise HTTPException(
                status_code=422,
                detail="未能自动识别新闻列表结构，请改用 RSS/API 或智能探测",
            )
        link_selector = discovery.link_selector
        title_selector = discovery.title_selector
        date_selector = discovery.date_selector

    source = Source(
        name=name,
        type=source_type,
        url=str(body.url),
        api_config=api_config,
        adapter=body.adapter,
        main_category=body.main_category,
        link_selector=link_selector,
        title_selector=title_selector,
        date_selector=date_selector,
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


@router.post("/discover")
def discover_source_route(body: DiscoverRequest, db: Session = Depends(get_db)):
    """智能探测：用 Playwright 渲染页面，捕获 JSON XHR/Fetch 响应，
    识别文章列表 API，生成与 ApiAdapterFetcher 兼容的 probe 配置并自检。

    若 ``create_source`` 为真，则把 probe 落地为标准 ``api`` 来源。
    """
    from app.sources.api_discovery import discover_api_source

    try:
        result = discover_api_source(str(body.url))
    except Exception as exc:  # noqa: BLE001 - surface discovery failure to caller
        logger.warning("api discovery failed for %s: %s", body.url, exc)
        raise HTTPException(status_code=502, detail=f"智能探测失败: {exc}") from exc

    payload = result.to_dict()

    if body.create_source:
        if not result.success or not result.api_url:
            raise HTTPException(status_code=422, detail="未能发现可用 API，无法创建来源")
        main_category = body.main_category or (MAIN_CATEGORIES[0] if MAIN_CATEGORIES else None)
        if main_category not in MAIN_CATEGORIES:
            raise HTTPException(status_code=422, detail="未知内容类型")
        source = _create_api_source_from_probe(
            db,
            api_url=result.api_url,
            method=result.method,
            items_path=result.items_path or "",
            fields=result.fields,
            name=(body.name or result.name_suggestion or "").strip(),
            main_category=main_category,
        )
        payload["created_source"] = {
            "id": source.id,
            "name": source.name,
            "url": source.url,
            "type": source.type,
            "main_category": source.main_category,
        }

    return payload


@router.post("/create-from-probe", status_code=201)
def create_source_from_probe_route(body: CreateFromProbeRequest, db: Session = Depends(get_db)):
    """用智能探测得到的 probe 配置直接创建标准 API 来源（不重新跑探测）。"""
    if body.main_category not in MAIN_CATEGORIES:
        raise HTTPException(status_code=422, detail="未知内容类型")
    if not body.api_url:
        raise HTTPException(status_code=422, detail="缺少 api_url")

    source = _create_api_source_from_probe(
        db,
        api_url=body.api_url,
        method=body.method,
        items_path=body.items_path or "",
        fields=body.fields or {},
        name=(body.name or "").strip(),
        main_category=body.main_category,
    )
    return _source_response(source)


def _create_api_source_from_probe(
    db: Session,
    *,
    api_url: str,
    method: str,
    items_path: str,
    fields: dict,
    name: str,
    main_category: str,
) -> Source:
    """把 probe 配置落地为标准 ``api`` 来源（type=api + api_config.probe）。"""
    probe: dict[str, Any] = {
        "mode": "json_list",
        "method": method.upper() if method else "GET",
        "url": api_url,
        "items_path": items_path,
        "fields": fields,
    }
    source = Source(
        name=name or api_url,
        type=SourceType.API.value,
        url=api_url,
        api_config={"probe": probe},
        main_category=main_category,
        stream=Stream.NEWS,
        enabled=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


@router.delete("/{source_id}", status_code=204)
def delete_source(source_id: int, db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    delete_source_and_related(db, source)
