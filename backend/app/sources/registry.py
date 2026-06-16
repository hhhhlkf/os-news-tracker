import logging
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Source

logger = logging.getLogger(__name__)

# Fields that the YAML seed is authoritative for — synced on every startup.
_SYNC_FIELDS = [
    "type",
    "url",
    "keywords",
    "adapter",
    "api_config",
    "stream",
    "vendor",
    "fetch_cron",
    "main_category",
    "relevance_filter",
    "relevance_keywords",
    "link_selector",
    "title_selector",
    "date_selector",
    "stealth",
    "enabled",
]


def _read_yaml_file(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or []


def _load_source_entries(path: Path) -> list[dict[str, Any]]:
    data = _read_yaml_file(path)
    if isinstance(data, list):
        return data

    if isinstance(data, dict) and isinstance(data.get("includes"), list):
        entries: list[dict[str, Any]] = []
        for include in data["includes"]:
            include_path = path.parent / include
            entries.extend(_load_source_entries(include_path))
        return entries

    raise ValueError(f"Unsupported source seed YAML format: {path}")


def _ensure_unique_source_names(entries: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for entry in entries:
        name = entry["name"]
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        duplicate_list = ", ".join(sorted(duplicates))
        raise ValueError(f"Duplicate source name(s) in seed YAML: {duplicate_list}")


def seed_sources_from_yaml(session: Session, path: str) -> dict[str, int]:
    """Idempotent seed: insert new sources, sync changed fields, delete removed.

    Returns ``{added, updated, deleted}`` so callers can log a summary.
    """
    entries = _load_source_entries(Path(path))
    _ensure_unique_source_names(entries)

    yaml_names: set[str] = set()
    added = 0
    updated = 0

    for entry in entries:
        name = entry["name"]
        yaml_names.add(name)
        existing = session.scalar(select(Source).where(Source.name == name))

        if existing:
            # ── Sync every field the YAML owns ──────────────────
            changed = False
            for field in _SYNC_FIELDS:
                if field not in entry:
                    continue
                new_val = entry[field]
                old_val = getattr(existing, field)
                if old_val != new_val:
                    setattr(existing, field, new_val)
                    changed = True
            if changed:
                updated += 1
            continue

        # ── Insert ──────────────────────────────────────────────
        session.add(
            Source(
                name=name,
                type=entry["type"],
                url=entry.get("url", ""),
                keywords=entry.get("keywords"),
                adapter=entry.get("adapter"),
                api_config=entry.get("api_config"),
                stream=entry.get("stream", "news"),
                vendor=entry.get("vendor"),
                fetch_cron=entry.get("fetch_cron"),
                main_category=entry.get("main_category"),
                relevance_filter=entry.get("relevance_filter", False),
                relevance_keywords=entry.get("relevance_keywords"),
                link_selector=entry.get("link_selector"),
                title_selector=entry.get("title_selector"),
                date_selector=entry.get("date_selector"),
                stealth=entry.get("stealth", False),
                enabled=entry.get("enabled", True),
            )
        )
        added += 1

    # ── Delete sources that disappeared from YAML ──────────────────
    from app.models import Item, ItemEntity, ItemSource, ItemTag

    deleted = 0
    orphans = session.scalars(
        select(Source).where(Source.name.notin_(yaml_names))
    ).all()
    for orphan in orphans:
        if orphan.items:
            logger.warning(
                "Deleting source %r (%d items will be orphaned)",
                orphan.name,
                len(orphan.items),
            )
            # Cascade-delete items and their junction rows manually.
            for item in orphan.items:
                session.execute(
                    ItemTag.__table__.delete().where(ItemTag.item_id == item.id)
                )
                session.execute(
                    ItemEntity.__table__.delete().where(ItemEntity.item_id == item.id)
                )
                session.execute(
                    ItemSource.__table__.delete().where(ItemSource.item_id == item.id)
                )
                session.delete(item)
        session.delete(orphan)
        deleted += 1

    session.commit()
    logger.info("Seed: +%d added, ~%d updated, -%d deleted", added, updated, deleted)
    return {"added": added, "updated": updated, "deleted": deleted}
