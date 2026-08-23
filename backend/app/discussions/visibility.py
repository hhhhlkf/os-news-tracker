"""Shared visibility rule for technical-discussion Items."""

from sqlalchemy import or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.models import DiscussionGroup, Item


def visible_item_clause() -> ColumnElement[bool]:
    """Keep candidate, waiting, and hidden discussion Items out of every consumer.

    Discussion Items are durable while their source mail groups may move back to
    waiting after a stricter value decision.  Callers only need this single
    clause; the group lifecycle stays internal to the discussion module.
    """
    published_item_ids = select(DiscussionGroup.item_id).where(
        DiscussionGroup.processing_status == "published",
        DiscussionGroup.hidden.is_(False),
        DiscussionGroup.item_id.is_not(None),
    )
    return or_(Item.item_kind != "discussion", Item.id.in_(published_item_ids))
