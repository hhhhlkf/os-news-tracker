from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import ItemStatus, TagKind
from app.models import Entity, Item, ItemSource, Tag
from app.processing.dedup import content_hash, url_hash
from app.schemas import EnrichedFields, NormalizedItem


class Repository:
    def __init__(self, session: Session):
        self._s = session

    def exists_by_canonical(self, canonical_url: str) -> bool:
        h = url_hash(canonical_url)
        return self._s.scalar(select(Item.id).where(Item.url_hash == h)) is not None

    def count_items(self) -> int:
        return self._s.scalar(select(func.count(Item.id))) or 0

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

    def save_enriched(self, item: NormalizedItem, fields: EnrichedFields) -> Item:
        db_item = Item(
            source_id=item.source_id,
            title=item.title,
            url=item.canonical_url,
            url_hash=url_hash(item.canonical_url),
            content_hash=content_hash(item.clean_content),
            clean_content=item.clean_content,
            published_at=item.published_at,
            main_category=fields.main_category,
            title_tldr=fields.title_tldr,
            summary=fields.summary,
            key_points=fields.key_points,
            info_type=fields.info_type,
            importance=fields.importance,
            why_it_matters=fields.why_it_matters,
            status=ItemStatus.ENRICHED,
            llm_confidence=fields.confidence,
        )
        for tag_name in fields.sub_tags:
            db_item.tags.append(self._get_or_create_tag(tag_name, TagKind.SUB_TAG))
        db_item.tags.append(self._get_or_create_tag(fields.main_category, TagKind.MAIN_CATEGORY))
        for entity in fields.entities:
            db_item.entities.append(self._get_or_create_entity(entity.type, entity.name))
        self._s.add(db_item)
        self._s.flush()
        self._s.add(ItemSource(item_id=db_item.id, source_id=item.source_id, url=item.canonical_url))
        self._s.commit()
        return db_item

    def merge_source_link(self, canonical_url: str, source_id: int, url: str) -> bool:
        h = url_hash(canonical_url)
        item_id = self._s.scalar(select(Item.id).where(Item.url_hash == h))
        if item_id is None:
            return False
        self._s.add(ItemSource(item_id=item_id, source_id=source_id, url=url))
        self._s.commit()
        return True
