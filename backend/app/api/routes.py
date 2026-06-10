from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Item, ItemSource

router = APIRouter()


def _item_summary(item: Item) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "title_tldr": item.title_tldr,
        "main_category": item.main_category,
        "info_type": item.info_type,
        "importance": item.importance,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "url": item.url,
    }


@router.get("/items")
def list_items(
    db: Session = Depends(get_db),
    main_category: str | None = None,
    info_type: str | None = None,
    importance: str | None = None,
    q: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    stmt = select(Item)
    if main_category:
        stmt = stmt.where(Item.main_category == main_category)
    if info_type:
        stmt = stmt.where(Item.info_type == info_type)
    if importance:
        stmt = stmt.where(Item.importance == importance)
    if q:
        like = f"%{q}%"
        stmt = stmt.where((Item.title.ilike(like)) | (Item.summary.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Item.published_at.desc().nullslast()).limit(limit).offset(offset)).all()
    return {"total": total, "items": [_item_summary(item) for item in rows]}


@router.get("/facets")
def facets(db: Session = Depends(get_db)):
    def _counts(column):
        rows = db.execute(select(column, func.count()).group_by(column)).all()
        return [{"value": value, "count": count} for value, count in rows if value is not None]

    return {
        "main_category": _counts(Item.main_category),
        "info_type": _counts(Item.info_type),
        "importance": _counts(Item.importance),
    }


@router.get("/items/{item_id}")
def item_detail(item_id: int, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    sources = db.scalars(select(ItemSource).where(ItemSource.item_id == item_id)).all()
    return {
        **_item_summary(item),
        "summary": item.summary,
        "key_points": item.key_points or [],
        "why_it_matters": item.why_it_matters,
        "llm_confidence": item.llm_confidence,
        "sub_tags": [tag.name for tag in item.tags if tag.kind == "sub_tag"],
        "entities": [{"type": entity.type, "name": entity.name} for entity in item.entities],
        "source_links": [{"source_id": source.source_id, "url": source.url} for source in sources],
    }
