from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import ItemStatus, TagKind
from app.models import Entity, Item, ItemSource, ItemTag, Tag, TagAlias
from app.processing.dedup import content_hash, url_hash
from app.schemas import EnrichedFields, NormalizedItem

MAX_TAGS_PER_ITEM = 5


class Repository:
    def __init__(
        self,
        session: Session,
        *,
        before_commit: Callable[[], None] | None = None,
    ):
        self._s = session
        self._before_commit = before_commit

    def _commit(self) -> None:
        if self._before_commit is not None:
            self._before_commit()
        self._s.commit()

    def exists_by_canonical(self, canonical_url: str) -> bool:
        h = url_hash(canonical_url)
        return self._s.scalar(select(Item.id).where(Item.url_hash == h)) is not None

    def count_items(self) -> int:
        return self._s.scalar(select(func.count(Item.id))) or 0

    def list_existing_sub_tags(self, *, limit: int = 100) -> list[dict]:
        rows = self._s.execute(
            select(Tag.id, Tag.name, func.count(ItemTag.item_id).label("usage_count"))
            .outerjoin(ItemTag, Tag.id == ItemTag.tag_id)
            .where(Tag.kind == TagKind.SUB_TAG)
            .group_by(Tag.id, Tag.name)
            .order_by(func.count(ItemTag.item_id).desc(), Tag.name.asc())
            .limit(limit)
        ).all()
        return [
            {"id": tag_id, "name": name, "usage_count": usage_count}
            for tag_id, name, usage_count in rows
        ]

    def _get_or_create_tag(self, name: str, kind: str) -> Tag:
        tag = self._s.scalar(select(Tag).where(Tag.name == name, Tag.kind == kind))
        if tag is None:
            tag = Tag(name=name, kind=kind)
            self._s.add(tag)
        return tag

    def _get_or_create_entity(self, type_: str, name: str) -> Entity:
        entity = self._s.scalar(select(Entity).where(Entity.type == type_, Entity.name == name))
        if entity is None:
            entity = Entity(type=type_, name=name)
            self._s.add(entity)
        return entity

    def _add_source_link_if_new(self, item_id: int, source_id: int, url: str) -> bool:
        """Insert an ItemSource row only if the (item_id, source_id, url) triple doesn't exist yet."""
        exists = self._s.scalar(
            select(ItemSource.id).where(
                ItemSource.item_id == item_id,
                ItemSource.source_id == source_id,
                ItemSource.url == url,
            )
        )
        if exists is not None:
            return False
        self._s.add(ItemSource(item_id=item_id, source_id=source_id, url=url))
        return True

    def _select_sub_tags(self, fields: EnrichedFields) -> list[str]:
        return self._select_sub_tag_names(fields.sub_tags)

    def _select_sub_tag_names(self, names: list[str]) -> list[str]:
        max_sub_tags = MAX_TAGS_PER_ITEM - 1  # reserve one tag for main_category
        return list(dict.fromkeys(name.strip() for name in names if name.strip()))[:max_sub_tags]

    def _record_tag_alias_suggestions(self, fields: EnrichedFields) -> None:
        for suggestion in fields.merge_suggestions:
            child_tag_id = (
                suggestion.get("child_tag_id")
                if isinstance(suggestion, dict)
                else suggestion.child_tag_id
            )
            parent_tag_id = (
                suggestion.get("parent_tag_id")
                if isinstance(suggestion, dict)
                else suggestion.parent_tag_id
            )
            parent_tag_name = (
                suggestion.get("parent_tag_name")
                if isinstance(suggestion, dict)
                else suggestion.parent_tag_name
            )
            reason = suggestion.get("reason") if isinstance(suggestion, dict) else suggestion.reason
            confidence = (
                suggestion.get("confidence")
                if isinstance(suggestion, dict)
                else suggestion.confidence
            )
            child = self._s.get(Tag, child_tag_id)
            if child is None or child.kind != TagKind.SUB_TAG:
                continue
            parent = (
                self._s.get(Tag, parent_tag_id)
                if parent_tag_id is not None
                else None
            )
            if parent is None:
                parent = self._get_or_create_tag(parent_tag_name, TagKind.SUB_TAG)
                if parent.id is None:
                    self._s.flush()
            if parent.id == child.id:
                continue
            existing = self._s.scalar(
                select(TagAlias).where(TagAlias.child_tag_id == child.id)
            )
            if existing is None:
                self._s.add(
                    TagAlias(
                        child_tag_id=child.id,
                        parent_tag_id=parent.id,
                        status="approved",
                        source="llm",
                        confidence=confidence,
                        reason=reason,
                    )
                )
            else:
                existing.parent_tag_id = parent.id
                existing.status = "approved"
                existing.source = "llm"
                existing.confidence = confidence
                existing.reason = reason

    def save_enriched(self, item: NormalizedItem, fields: EnrichedFields) -> Item:
        sub_tags = self._select_sub_tags(fields)
        db_item = Item(
            source_id=item.source_id,
            title=item.title,
            url=item.canonical_url,
            url_hash=url_hash(item.canonical_url),
            content_hash=content_hash(item.clean_content),
            clean_content=item.clean_content,
            published_at=item.published_at,
            main_category=fields.main_category,
            title_tldr=fields.title_zh,
            summary=fields.summary,
            key_points=fields.tech_highlights,
            info_type=fields.info_type,
            importance=fields.importance,
            why_it_matters=None,
            status=ItemStatus.ENRICHED,
            llm_confidence=fields.confidence,
        )
        for tag_name in sub_tags:
            db_item.tags.append(self._get_or_create_tag(tag_name, TagKind.SUB_TAG))
        db_item.tags.append(self._get_or_create_tag(fields.main_category, TagKind.MAIN_CATEGORY))
        self._s.add(db_item)
        self._s.flush()
        self._record_tag_alias_suggestions(fields)
        self._add_source_link_if_new(db_item.id, item.source_id, item.canonical_url)
        self._commit()
        return db_item

    def merge_source_link(self, canonical_url: str, source_id: int, url: str) -> bool:
        """Link a source+url to an existing item.  Returns True only if a new row was inserted."""
        h = url_hash(canonical_url)
        item_id = self._s.scalar(select(Item.id).where(Item.url_hash == h))
        if item_id is None:
            return False
        added = self._add_source_link_if_new(item_id, source_id, url)
        self._commit()
        return added
