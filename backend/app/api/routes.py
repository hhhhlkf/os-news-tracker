from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.manual_news_run import (
    get_manual_news_run_status,
    start_manual_news_run,
    stop_manual_news_run,
)
from app.models import Item, ItemSource, ItemTag, Tag
from app.schemas import ManualNewsRunRequest

router = APIRouter()

SortBy = Literal["published_at", "fetched_at"]
SortDir = Literal["desc", "asc"]


def _item_summary(item: Item) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "title_tldr": item.title_tldr,
        "main_category": item.main_category,
        "info_type": item.info_type,
        "importance": item.importance,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "fetched_at": item.fetched_at.isoformat() if item.fetched_at else None,
        "url": item.url,
    }


@router.get("/items")
def list_items(
    db: Session = Depends(get_db),
    main_category: str | None = None,
    info_type: str | None = None,
    importance: str | None = None,
    sub_tag: str | None = None,
    q: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    sort_by: SortBy = "published_at",
    sort_dir: SortDir = "desc",
    published_after: str | None = None,
    published_before: str | None = None,
):
    stmt = select(Item)
    if main_category:
        stmt = stmt.where(Item.main_category == main_category)
    if info_type:
        stmt = stmt.where(Item.info_type == info_type)
    if importance:
        stmt = stmt.where(Item.importance == importance)
    if sub_tag:
        stmt = stmt.where(
            Item.id.in_(
                select(ItemTag.item_id)
                .join(Tag, Tag.id == ItemTag.tag_id)
                .where(Tag.name == sub_tag, Tag.kind == "sub_tag")
            )
        )
    if q:
        like = f"%{q}%"
        stmt = stmt.where((Item.title.ilike(like)) | (Item.summary.ilike(like)))

    # Time-range filters on published_at
    if published_after is not None:
        try:
            after_date = date.fromisoformat(published_after)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid published_after date: {published_after!r}")
        stmt = stmt.where(Item.published_at >= datetime(after_date.year, after_date.month, after_date.day, tzinfo=timezone.utc))

    if published_before is not None:
        try:
            before_date = date.fromisoformat(published_before)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid published_before date: {published_before!r}")
        before_end = datetime(before_date.year, before_date.month, before_date.day, tzinfo=timezone.utc) + timedelta(days=1)
        stmt = stmt.where(Item.published_at < before_end)

    sort_col = Item.published_at if sort_by == "published_at" else Item.fetched_at
    if sort_dir == "desc":
        order_clause = sort_col.desc()
    else:
        order_clause = sort_col.asc()
    if sort_by == "published_at":
        order_clause = order_clause.nullslast()

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(order_clause).limit(limit).offset(offset)).all()
    return {"total": total, "items": [_item_summary(item) for item in rows]}


@router.get("/facets")
def facets(db: Session = Depends(get_db)):
    def _counts(column):
        rows = db.execute(select(column, func.count()).group_by(column)).all()
        return [{"value": value, "count": count} for value, count in rows if value is not None]

    sub_tag_rows = db.execute(
        select(Tag.name, func.count(func.distinct(ItemTag.item_id)))
        .join(ItemTag, Tag.id == ItemTag.tag_id)
        .where(Tag.kind == "sub_tag")
        .group_by(Tag.name)
        .order_by(func.count(func.distinct(ItemTag.item_id)).desc())
        .limit(30)
    ).all()

    return {
        "main_category": _counts(Item.main_category),
        "info_type": _counts(Item.info_type),
        "importance": _counts(Item.importance),
        "sub_tags": [{"value": name, "count": count} for name, count in sub_tag_rows],
    }


@router.get("/items/{item_id}")
def item_detail(item_id: int, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    sources = db.scalars(
        select(ItemSource)
        .where(ItemSource.item_id == item_id)
        .order_by(ItemSource.id)
    ).all()
    seen: set[tuple[int, str]] = set()
    unique_links: list[dict] = []
    for src in sources:
        key = (src.source_id, src.url)
        if key not in seen:
            seen.add(key)
            unique_links.append({"source_id": src.source_id, "url": src.url})
    return {
        **_item_summary(item),
        "summary": item.summary,
        "key_points": item.key_points or [],
        "why_it_matters": item.why_it_matters,
        "llm_confidence": item.llm_confidence,
        "sub_tags": [tag.name for tag in item.tags if tag.kind == "sub_tag"],
        "entities": [{"type": entity.type, "name": entity.name} for entity in item.entities],
        "source_links": unique_links,
    }


@router.get("/news-run")
def get_news_run():
    return get_manual_news_run_status()


@router.post("/news-run/start")
def start_news_run(request: ManualNewsRunRequest):
    if not start_manual_news_run(request):
        raise HTTPException(status_code=409, detail="manual news run already active")
    return get_manual_news_run_status()


@router.post("/news-run/stop")
def stop_news_run():
    return stop_manual_news_run()
