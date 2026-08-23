from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Item, ItemEntity, ItemSource, ItemTag, Source


def delete_source_and_related(db: Session, source: Source) -> None:
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
    db.delete(source)
    db.commit()
