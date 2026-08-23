"""Database-configured positive mailing-list header matcher."""

from __future__ import annotations

from email.message import Message

from app.discussions.mime import decoded_header
from app.models import DiscussionSourceRule


_STANDARD_HEADERS = {
    "list_id": "List-Id",
    "list_post": "List-Post",
    "delivered_to": "Delivered-To",
    "to": "To",
    "cc": "Cc",
}


def matching_source_ids(message: Message, rules: list[DiscussionSourceRule]) -> set[int]:
    """Return every enabled source rule which positively matches a header.

    Rules are intentionally conjunct-free and source-neutral: adding a new
    list never requires a code deployment and one message may match many lists.
    """
    matched: set[int] = set()
    for rule in rules:
        if not rule.enabled:
            continue
        header = rule.header_name or _STANDARD_HEADERS.get(rule.rule_type)
        if not header:
            continue
        actual = decoded_header(message, header)
        expected = (rule.match_value or "").strip()
        if actual and expected and expected.casefold() in actual.casefold():
            matched.add(rule.source_id)
    return matched
