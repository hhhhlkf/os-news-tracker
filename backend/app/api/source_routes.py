from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.source_cleanup import delete_source_and_related
from app.enums import Stream
from app.models import Source
from app.sources.detector import DetectResult, SourceDetectionError
from app.sources.source_management import (
    ApiProbeSourceInput,
    HtmlListDiscoveryError,
    MissingApiUrlError,
    SourceCreateInput,
    UnknownMainCategoryError,
    create_news_source,
    default_main_category,
    detect_source_shape,
    upsert_api_source_from_probe,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sources", tags=["sources"])
UNABLE_TO_DETECT_DETAIL = "无法识别链接形态，请改用 RSS/API/网页首页链接"

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
    pagination: dict[str, Any] | None = None
    json_body: dict[str, Any] | None = None
    name: str | None = None
    main_category: str


def _detect_or_422(url: str) -> DetectResult:
    try:
        return detect_source_shape(url)
    except SourceDetectionError as exc:
        raise HTTPException(status_code=422, detail=UNABLE_TO_DETECT_DETAIL) from exc


def _source_response(source: Source) -> dict[str, Any]:
    return {
        "id": source.id,
        "name": source.name,
        "url": source.url,
        "type": source.type,
        "main_category": source.main_category,
        "enabled": source.enabled,
    }


@router.post("/detect")
def detect_source_route(body: SourceDetectRequest) -> dict[str, Any]:
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
) -> dict[str, Any]:
    try:
        source = create_news_source(
            db,
            SourceCreateInput(
                url=str(body.url),
                name=body.name,
                main_category=body.main_category,
                source_type=body.type,
                adapter=body.adapter,
                api_config=body.api_config,
                link_selector=body.link_selector,
                title_selector=body.title_selector,
                date_selector=body.date_selector,
            ),
        )
    except UnknownMainCategoryError as exc:
        raise HTTPException(status_code=422, detail="未知内容类型") from exc
    except SourceDetectionError as exc:
        raise HTTPException(status_code=422, detail=UNABLE_TO_DETECT_DETAIL) from exc
    except HtmlListDiscoveryError as exc:
        raise HTTPException(
            status_code=422,
            detail="未能自动识别新闻列表结构，请改用 RSS/API 或智能探测",
        ) from exc
    return _source_response(source)


@router.get("")
def list_sources(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    sources = db.scalars(
        select(Source)
        .where(Source.stream == Stream.NEWS)
        .order_by(Source.name.asc(), Source.id.asc())
    ).all()
    return [_source_response(source) for source in sources]


@router.post("/discover")
def discover_source_route(
    body: DiscoverRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """智能探测：用 Playwright 渲染页面，捕获 JSON XHR/Fetch 响应，
    识别文章列表 API，生成与 ApiAdapterFetcher 兼容的 probe 配置并自检。

    若 ``create_source`` 为真，则把 probe 落地为标准 ``api`` 来源
    （同 URL 的已存在 api 来源会被覆盖更新，而非新建）。
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
        main_category = body.main_category or default_main_category(db)
        try:
            source = upsert_api_source_from_probe(
                db,
                ApiProbeSourceInput(
                    api_url=result.api_url,
                    method=result.method,
                    items_path=result.items_path or "",
                    fields=result.fields,
                    pagination=result.pagination,
                    json_body=result.json_body,
                    name=(body.name or result.name_suggestion or "").strip(),
                    main_category=main_category or "",
                ),
            )
        except UnknownMainCategoryError as exc:
            raise HTTPException(status_code=422, detail="未知内容类型") from exc
        payload["created_source"] = {
            "id": source.id,
            "name": source.name,
            "url": source.url,
            "type": source.type,
            "main_category": source.main_category,
        }

    return payload


@router.post("/create-from-probe", status_code=201)
def create_source_from_probe_route(
    body: CreateFromProbeRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """用智能探测得到的 probe 配置创建/更新标准 API 来源（不重新跑探测）。

    同 URL 的已存在 api 来源会被覆盖更新，而非新建。
    """
    try:
        source = upsert_api_source_from_probe(
            db,
            ApiProbeSourceInput(
                api_url=body.api_url,
                method=body.method,
                items_path=body.items_path or "",
                fields=body.fields or {},
                pagination=body.pagination,
                json_body=body.json_body,
                name=(body.name or "").strip(),
                main_category=body.main_category,
            ),
        )
    except UnknownMainCategoryError as exc:
        raise HTTPException(status_code=422, detail="未知内容类型") from exc
    except MissingApiUrlError as exc:
        raise HTTPException(status_code=422, detail="缺少 api_url") from exc
    return _source_response(source)


@router.delete("/{source_id}", status_code=204)
def delete_source(source_id: int, db: Session = Depends(get_db)) -> None:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    delete_source_and_related(db, source)
