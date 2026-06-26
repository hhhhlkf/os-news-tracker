import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import ItemStatus, TagKind
from app.models import Entity, Item, ItemSource, ItemTag, Tag, TagAlias
from app.processing.dedup import content_hash, url_hash
from app.schemas import EnrichedFields, NormalizedItem, RawItem

MAX_TAGS_PER_ITEM = 5


class Repository:
    def __init__(self, session: Session):
        self._s = session

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

    def _agent_sub_tags_from_key_points(self, key_points: list[str]) -> list[str]:
        tags: list[str] = []
        for point in key_points:
            if point.startswith("__type:"):
                continue
            match = re.match(r"^\s*[［\[]([^］\]]{1,40})[］\]]", point)
            if match:
                tags.append(self._canonical_agent_sub_tag(match.group(1)))
        return self._select_sub_tag_names(tags)

    def _canonical_agent_sub_tag(self, name: str) -> str:
        compact = re.sub(r"\s+", " ", name).strip()
        lowered = compact.lower()
        if lowered in {"kernel", "linux kernel", "内核"}:
            return "Linux Kernel"
        if lowered in {"openeuler", "open euler", "欧拉"}:
            return "openEuler"
        if lowered in {"openanolis", "anolis", "龙蜥"}:
            return "OpenAnolis"
        if lowered in {"cve", "漏洞", "安全漏洞"}:
            return "CVE"
        return compact

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
        self._s.commit()
        return db_item

    def save_agent_enriched(self, item: RawItem) -> Item:
        """存储 agent crawl 产出的已富化条目，跳过 LLM Enricher。

        AgentItem 的摘要数据通过 RawItem.extra 字段传入（agent_item=True）。
        此方法直接从 extra 读取 main_category、importance、key_points 等字段，
        无需再调用 LLM 富化。
        """
        extra = item.extra or {}
        key_points = extra.get("key_points", [])
        if not isinstance(key_points, list):
            key_points = []
        configured_sub_tags = extra.get("sub_tags", [])
        if not isinstance(configured_sub_tags, list):
            configured_sub_tags = []
        sub_tags = self._select_sub_tag_names(
            [str(tag) for tag in configured_sub_tags]
        )
        if not sub_tags:
            sub_tags = self._agent_sub_tags_from_key_points(
                [str(point) for point in key_points]
            )

        db_item = Item(
            source_id=item.source_id,
            title=item.title,
            url=item.url,
            url_hash=url_hash(item.url),
            content_hash=content_hash(item.raw_content or ""),
            clean_content=item.raw_content,
            published_at=item.published_at,
            main_category=extra.get("main_category", "agent_crawl"),
            summary=item.raw_content or "",
            key_points=key_points,
            importance=extra.get("importance", "低"),
            info_type=extra.get("info_type", "其他"),
            status=ItemStatus.AGENT_ENRICHED,
            llm_confidence=None,
        )
        for tag_name in sub_tags:
            db_item.tags.append(self._get_or_create_tag(tag_name, TagKind.SUB_TAG))
        # 添加 main_category tag
        db_item.tags.append(
            self._get_or_create_tag(extra.get("main_category", "agent_crawl"), TagKind.MAIN_CATEGORY),
        )
        self._s.add(db_item)
        self._s.flush()
        merge_suggestions = extra.get("merge_suggestions", [])
        if isinstance(merge_suggestions, list):
            # 丢弃 child_tag_id 非整数的无效建议：LLM 偶尔产出 null/字符串，
            # 而 TagAlias.child_tag_id 是外键不能为空，这类建议既存不进也会
            # 让 EnrichedFields 校验失败、连累整条 item 入库失败。
            clean_suggestions = [
                s for s in merge_suggestions
                if isinstance(s, dict)
                and isinstance(s.get("child_tag_id"), int)
            ]
            fields = EnrichedFields(
                title_zh=item.title,
                summary=item.raw_content or "",
                tech_highlights=key_points,
                info_type=extra.get("info_type", "其他"),
                importance=extra.get("importance", "低"),
                main_category=extra.get("main_category", "agent_crawl"),
                sub_tags=sub_tags,
                merge_suggestions=clean_suggestions,
                confidence=0.0,
            )
            self._record_tag_alias_suggestions(fields)
        self._add_source_link_if_new(db_item.id, item.source_id, item.url)
        self._s.commit()
        return db_item

    def merge_source_link(self, canonical_url: str, source_id: int, url: str) -> bool:
        """Link a source+url to an existing item.  Returns True only if a new row was inserted."""
        h = url_hash(canonical_url)
        item_id = self._s.scalar(select(Item.id).where(Item.url_hash == h))
        if item_id is None:
            return False
        added = self._add_source_link_if_new(item_id, source_id, url)
        self._s.commit()
        return added
