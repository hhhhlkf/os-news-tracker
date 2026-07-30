from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Item, ItemSource, ItemTag, Tag, TagAlias
from app.processing.reason import generate_recommendation_reason
from app.run_logs import list_run_logs

router = APIRouter()

SortBy = Literal["published_at", "fetched_at"]
SortDir = Literal["desc", "asc"]
BoundaryMode = Literal["none", "absolute", "relative"]
RelativeRange = Literal["24h", "7d", "30d"]


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
        "why_it_matters": item.why_it_matters,
    }


def _approved_parent_map(db: Session) -> dict[int, int]:
    aliases = db.scalars(select(TagAlias).where(TagAlias.status == "approved")).all()
    return {alias.child_tag_id: alias.parent_tag_id for alias in aliases}


def _find_root_tag_id(tag_id: int, parent_by_child: dict[int, int]) -> int:
    seen: set[int] = set()
    current = tag_id
    while current in parent_by_child and current not in seen:
        seen.add(current)
        current = parent_by_child[current]
    return current


def _sub_tag_root_name(tag: Tag, parent_by_child: dict[int, int], tags_by_id: dict[int, Tag]) -> str:
    root_id = _find_root_tag_id(tag.id, parent_by_child)
    return tags_by_id.get(root_id, tag).name


def _tag_ids_for_root_name(db: Session, name: str) -> list[int]:
    tags = db.scalars(select(Tag).where(Tag.kind == "sub_tag")).all()
    tags_by_id = {tag.id: tag for tag in tags}
    parent_by_child = _approved_parent_map(db)
    root_ids = {
        tag.id
        for tag in tags
        if _sub_tag_root_name(tag, parent_by_child, tags_by_id) == name
    }
    if not root_ids:
        return []
    return [
        tag.id
        for tag in tags
        if _find_root_tag_id(tag.id, parent_by_child) in root_ids
    ]


def _split_filter_values(raw: str | None) -> list[str]:
    """Parse comma-separated multi-select filter values (trim, drop empties, dedupe)."""
    if not raw:
        return []
    values: list[str] = []
    seen: set[str] = set()
    for part in str(raw).split(","):
        value = part.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def _tag_ids_for_root_names(db: Session, names: list[str]) -> list[int]:
    tag_ids: list[int] = []
    seen: set[int] = set()
    for name in names:
        for tag_id in _tag_ids_for_root_name(db, name):
            if tag_id in seen:
                continue
            seen.add(tag_id)
            tag_ids.append(tag_id)
    return tag_ids


def _parse_item_ids(raw: str | None) -> list[int]:
    """Parse the exact `item_ids=1,2,3` filter into deduped positive integers."""
    ids: list[int] = []
    seen: set[int] = set()
    for value in _split_filter_values(raw):
        if not value.isdigit() or int(value) <= 0:
            raise HTTPException(status_code=422, detail=f"invalid item_ids value: {value!r}")
        item_id = int(value)
        if item_id in seen:
            continue
        seen.add(item_id)
        ids.append(item_id)
    return ids


def _relative_time_delta(value: str | None) -> timedelta | None:
    if value == "24h":
        return timedelta(hours=24)
    if value == "7d":
        return timedelta(days=7)
    if value == "30d":
        return timedelta(days=30)
    return None


def _resolve_time_boundary(
    *,
    mode: str | None,
    value: str | None,
    absolute_date: str | None,
    inclusive_end: bool,
    field_name: str,
) -> datetime | None:
    if mode == "relative":
        delta = _relative_time_delta(value)
        if delta is None:
            raise HTTPException(status_code=422, detail=f"{field_name}_value is required for relative mode")
        return datetime.now(timezone.utc) - delta
    if absolute_date:
        try:
            parsed = date.fromisoformat(absolute_date)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid {field_name} date: {absolute_date!r}")
        boundary = datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)
        return boundary + timedelta(days=1) if inclusive_end else boundary
    return None


@router.get("/items")
def list_items(
    db: Session = Depends(get_db),
    main_category: str | None = None,
    info_type: str | None = None,
    importance: str | None = None,
    sub_tag: str | None = None,
    source_id: str | None = None,
    item_ids: str | None = None,
    q: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    sort_by: SortBy = "published_at",
    sort_dir: SortDir = "desc",
    published_after_mode: BoundaryMode | None = None,
    published_after_value: RelativeRange | None = None,
    published_after: str | None = None,
    published_before_mode: BoundaryMode | None = None,
    published_before_value: RelativeRange | None = None,
    published_before: str | None = None,
    fetched_after_mode: BoundaryMode | None = None,
    fetched_after_value: RelativeRange | None = None,
    fetched_after: str | None = None,
    fetched_before_mode: BoundaryMode | None = None,
    fetched_before_value: RelativeRange | None = None,
    fetched_before: str | None = None,
):
    stmt = select(Item)
    # Exact primary-key filter used by the trend carousel; titles never take part.
    exact_item_ids = _parse_item_ids(item_ids)
    if exact_item_ids:
        stmt = stmt.where(Item.id.in_(exact_item_ids))
    main_categories = _split_filter_values(main_category)
    if main_categories:
        stmt = stmt.where(Item.main_category.in_(main_categories))
    if info_type:
        stmt = stmt.where(Item.info_type == info_type)
    importances = _split_filter_values(importance)
    if importances:
        stmt = stmt.where(Item.importance.in_(importances))
    sub_tags = _split_filter_values(sub_tag)
    if sub_tags:
        tag_ids = _tag_ids_for_root_names(db, sub_tags)
        stmt = stmt.where(
            Item.id.in_(
                select(ItemTag.item_id)
                .where(ItemTag.tag_id.in_(tag_ids))
            )
        )
    source_ids = [
        int(value)
        for value in _split_filter_values(source_id)
        if value.isdigit()
    ]
    if source_ids:
        stmt = stmt.where(
            or_(
                Item.source_id.in_(source_ids),
                Item.id.in_(
                    select(ItemSource.item_id)
                    .where(ItemSource.source_id.in_(source_ids))
                ),
            )
        )
    if q:
        # title_tldr is the headline the list and detail pages actually render,
        # so a word copied off the screen has to match it as well as the source title.
        like = f"%{q}%"
        stmt = stmt.where(
            Item.title.ilike(like) | Item.title_tldr.ilike(like) | Item.summary.ilike(like)
        )

    # Time-range filters on published_at.
    # Absolute dates keep the historical natural-day semantics; relative presets
    # are rolling windows from the current instant so the list count matches mail previews.
    after_boundary = _resolve_time_boundary(
        mode=published_after_mode,
        value=published_after_value,
        absolute_date=published_after,
        inclusive_end=False,
        field_name="published_after",
    )
    if after_boundary is not None:
        stmt = stmt.where(Item.published_at >= after_boundary)

    before_boundary = _resolve_time_boundary(
        mode=published_before_mode,
        value=published_before_value,
        absolute_date=published_before,
        inclusive_end=True,
        field_name="published_before",
    )
    if before_boundary is not None:
        stmt = stmt.where(Item.published_at < before_boundary)

    fetched_after_boundary = _resolve_time_boundary(
        mode=fetched_after_mode,
        value=fetched_after_value,
        absolute_date=fetched_after,
        inclusive_end=False,
        field_name="fetched_after",
    )
    if fetched_after_boundary is not None:
        stmt = stmt.where(Item.fetched_at >= fetched_after_boundary)

    fetched_before_boundary = _resolve_time_boundary(
        mode=fetched_before_mode,
        value=fetched_before_value,
        absolute_date=fetched_before,
        inclusive_end=True,
        field_name="fetched_before",
    )
    if fetched_before_boundary is not None:
        stmt = stmt.where(Item.fetched_at < fetched_before_boundary)

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


# 重要度筛选项固定顺序：高 → 中 → 低
_IMPORTANCE_ORDER = {"高": 0, "中": 1, "低": 2}


@router.get("/facets")
def facets(db: Session = Depends(get_db)):
    def _counts(column):
        rows = db.execute(select(column, func.count()).group_by(column)).all()
        return [{"value": value, "count": count} for value, count in rows if value is not None]

    parent_by_child = _approved_parent_map(db)
    tags_by_id = {tag.id: tag for tag in db.scalars(select(Tag)).all()}
    raw_sub_tag_rows = db.execute(
        select(Tag.id, Tag.name, func.count(func.distinct(ItemTag.item_id)))
        .join(ItemTag, Tag.id == ItemTag.tag_id)
        .where(Tag.kind == "sub_tag")
        .group_by(Tag.id, Tag.name)
        .order_by(func.count(func.distinct(ItemTag.item_id)).desc())
    ).all()
    sub_tag_counts: dict[str, int] = {}
    for tag_id, name, count in raw_sub_tag_rows:
        tag = tags_by_id.get(tag_id)
        root_name = _sub_tag_root_name(tag, parent_by_child, tags_by_id) if tag else name
        sub_tag_counts[root_name] = sub_tag_counts.get(root_name, 0) + count
    sub_tag_rows = sorted(
        sub_tag_counts.items(),
        key=lambda row: row[1],
        reverse=True,
    )[:30]

    return {
        "main_category": _counts(Item.main_category),
        "info_type": _counts(Item.info_type),
        "importance": sorted(
            _counts(Item.importance),
            key=lambda facet: _IMPORTANCE_ORDER.get(facet["value"], len(_IMPORTANCE_ORDER)),
        ),
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
    parent_by_child = _approved_parent_map(db)
    tags_by_id = {tag.id: tag for tag in db.scalars(select(Tag)).all()}
    sub_tags = list(
        dict.fromkeys(
            _sub_tag_root_name(tag, parent_by_child, tags_by_id)
            for tag in item.tags
            if tag.kind == "sub_tag"
        )
    )
    return {
        **_item_summary(item),
        "summary": item.summary,
        "key_points": item.key_points or [],
        "why_it_matters": item.why_it_matters,
        "llm_confidence": item.llm_confidence,
        "sub_tags": sub_tags,
        "entities": [{"type": entity.type, "name": entity.name} for entity in item.entities],
        "source_links": unique_links,
    }


@router.post("/items/{item_id}/reason")
def item_reason(item_id: int, db: Session = Depends(get_db)):
    """Return the recommendation reason for an item, generating + caching it on first request."""
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    if item.why_it_matters:
        return {"reason": item.why_it_matters, "generated": False}
    try:
        reason = generate_recommendation_reason(item)
    except Exception:
        # LLM unavailable — fall back to summary text without persisting.
        fallback = item.summary or item.title_tldr or item.title
        return {"reason": fallback, "generated": False}
    item.why_it_matters = reason
    db.commit()
    return {"reason": reason, "generated": True}


@router.get("/run-logs")
def get_run_logs(
    after_id: int | None = None,
    limit: int = Query(200, le=300),
):
    return {"logs": list_run_logs(after_id=after_id, limit=limit)}
