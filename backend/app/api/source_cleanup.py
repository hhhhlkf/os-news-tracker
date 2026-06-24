from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import AgentCrawlRun, Item, ItemEntity, ItemSource, ItemTag, Source


def _fail_running_agent_runs(db: Session, source: Source) -> None:
    running_runs = db.scalars(
        select(AgentCrawlRun).where(
            AgentCrawlRun.source_id == source.id,
            AgentCrawlRun.status == "running",
        )
    ).all()
    for run in running_runs:
        run.status = "failed"
        run.stage_message = "来源已删除"
        run.completed_at = datetime.now(timezone.utc)
    if running_runs:
        db.flush()


def _resolve_fallback_source_id(db: Session, source: Source) -> int | None:
    """Pick a surviving source to re-home items previously crawled by *source*.

    Preference order:
    1. The original standard source recorded in ``api_config.candidate_source_id``
       (set when the agent source is created from a 一键 Agent 运行 candidate).
    2. Any other source sharing the same URL (the candidate before linkage).
    """
    api_config = source.api_config or {}
    candidate_id = api_config.get("candidate_source_id")
    if (
        isinstance(candidate_id, int)
        and candidate_id != source.id
        and db.get(Source, candidate_id) is not None
    ):
        return candidate_id

    twin = db.scalars(
        select(Source.id)
        .where(Source.url == source.url, Source.id != source.id)
        .limit(1)
    ).first()
    return twin


def delete_agent_source_keep_items(db: Session, source: Source) -> None:
    """Delete an agent crawl source (the flow) while preserving crawled items.

    The delete button on an agent run card removes the crawl *flow* — the
    source, its config, run history and site memory — but must NOT erase the
    news items it previously collected. Because ``Item.source_id`` is a
    non-nullable FK, surviving items are re-homed to a fallback source (the
    original candidate / a URL twin). Only items with no surviving source to
    anchor them are removed, since they would otherwise dangle.
    """
    _fail_running_agent_runs(db, source)

    fallback_source_id = _resolve_fallback_source_id(db, source)

    item_ids = db.scalars(select(Item.id).where(Item.source_id == source.id)).all()
    orphan_ids: list[int] = []
    for item_id in item_ids:
        target = fallback_source_id
        if target is None:
            target = db.scalars(
                select(ItemSource.source_id)
                .where(
                    ItemSource.item_id == item_id,
                    ItemSource.source_id != source.id,
                )
                .limit(1)
            ).first()
        if target is None:
            orphan_ids.append(item_id)
        else:
            db.execute(
                update(Item).where(Item.id == item_id).values(source_id=target)
            )

    orphan_set = set(orphan_ids)

    # Re-home or drop the junction links owned by the agent source so the
    # surviving items keep a meaningful source link where possible.
    agent_links = db.scalars(
        select(ItemSource).where(ItemSource.source_id == source.id)
    ).all()
    for link in agent_links:
        if link.item_id in orphan_set or fallback_source_id is None:
            db.delete(link)
            continue
        collision = db.scalars(
            select(ItemSource.id)
            .where(
                ItemSource.item_id == link.item_id,
                ItemSource.source_id == fallback_source_id,
                ItemSource.url == link.url,
            )
            .limit(1)
        ).first()
        if collision is not None:
            db.delete(link)
        else:
            link.source_id = fallback_source_id
    db.flush()

    if orphan_ids:
        db.execute(sa_delete(ItemTag).where(ItemTag.item_id.in_(orphan_ids)))
        db.execute(sa_delete(ItemEntity).where(ItemEntity.item_id.in_(orphan_ids)))
        db.execute(sa_delete(ItemSource).where(ItemSource.item_id.in_(orphan_ids)))
        try:
            from app.models import UserItemInteraction, UserItemScore

            db.execute(sa_delete(UserItemScore).where(UserItemScore.item_id.in_(orphan_ids)))
            db.execute(sa_delete(UserItemInteraction).where(UserItemInteraction.item_id.in_(orphan_ids)))
        except ImportError:
            pass
        db.execute(sa_delete(Item).where(Item.id.in_(orphan_ids)))

    db.execute(sa_delete(ItemSource).where(ItemSource.source_id == source.id))
    db.execute(sa_delete(AgentCrawlRun).where(AgentCrawlRun.source_id == source.id))
    db.delete(source)
    db.commit()


def delete_source_and_related(db: Session, source: Source) -> None:
    _fail_running_agent_runs(db, source)

    item_ids = db.scalars(select(Item.id).where(Item.source_id == source.id)).all()
    if item_ids:
        db.execute(sa_delete(ItemTag).where(ItemTag.item_id.in_(item_ids)))
        db.execute(sa_delete(ItemEntity).where(ItemEntity.item_id.in_(item_ids)))
        db.execute(sa_delete(ItemSource).where(ItemSource.item_id.in_(item_ids)))
        try:
            from app.models import UserItemInteraction, UserItemScore

            db.execute(sa_delete(UserItemScore).where(UserItemScore.item_id.in_(item_ids)))
            db.execute(sa_delete(UserItemInteraction).where(UserItemInteraction.item_id.in_(item_ids)))
        except ImportError:
            pass
        db.execute(sa_delete(Item).where(Item.id.in_(item_ids)))

    db.execute(sa_delete(ItemSource).where(ItemSource.source_id == source.id))
    db.execute(sa_delete(AgentCrawlRun).where(AgentCrawlRun.source_id == source.id))
    db.delete(source)
    db.commit()
