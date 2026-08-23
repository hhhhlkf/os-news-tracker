"""Stable visual color contract for dynamic trend directions and categories.

The frontend mirrors these values and the hash below. Keep this small module as
the email-rendering seam so dynamic template directions look the same in the
web workspace and in delivered mail.
"""

from __future__ import annotations

from typing import TypedDict


class TrendColorGroup(TypedDict):
    background: str
    color: str
    border: str
    line: str


DIRECTION_COLOR_GROUPS: tuple[TrendColorGroup, ...] = (
    {"background": "#eff8ff", "color": "#175cd3", "border": "#b2ddff", "line": "#d1e9ff"},
    {"background": "#f4f3ff", "color": "#5925dc", "border": "#d9d6fe", "line": "#e9e7fe"},
    {"background": "#ecfdf3", "color": "#027a48", "border": "#abefc6", "line": "#d1fadf"},
    {"background": "#fffaeb", "color": "#b54708", "border": "#fedf89", "line": "#fef0c7"},
    {"background": "#fff1f3", "color": "#c01048", "border": "#fecdd6", "line": "#ffe4e8"},
    {"background": "#eef4ff", "color": "#3538cd", "border": "#c7d7fe", "line": "#e0eaff"},
    {"background": "#ecfdff", "color": "#0e7090", "border": "#a5f0fc", "line": "#cff9fe"},
    {"background": "#fff6ed", "color": "#c4320a", "border": "#fed7aa", "line": "#ffead5"},
)

FALLBACK_CATEGORY_COLOR: TrendColorGroup = {
    "background": "#f2f4f7", "color": "#667085", "border": "#d0d5dd", "line": "#eaecf0"
}

CATEGORY_COLOR_GROUPS: dict[str, TrendColorGroup] = {
    "emerging_trend": DIRECTION_COLOR_GROUPS[1],
    "hot_event": {"background": "#fef3f2", "color": "#b42318", "border": "#fecdca", "line": "#fee4e2"},
    "periodic_activity": DIRECTION_COLOR_GROUPS[0],
    "attention_declining": FALLBACK_CATEGORY_COLOR,
    "unverified_change": FALLBACK_CATEGORY_COLOR,
}


def trend_direction_color(direction: str | None) -> TrendColorGroup:
    label = (direction or "").strip()
    if not label:
        return FALLBACK_CATEGORY_COLOR
    value = 0
    for char in label:
        value = ((value * 31) + ord(char)) & 0xFFFFFFFF
    return DIRECTION_COLOR_GROUPS[value % len(DIRECTION_COLOR_GROUPS)]


def trend_category_color(category: str | None) -> TrendColorGroup:
    return CATEGORY_COLOR_GROUPS.get(category or "", FALLBACK_CATEGORY_COLOR)


def trend_direction_sort_key(direction: str) -> int:
    """Keep OS and AI first without changing the authored order of other labels."""
    normalized = direction.strip().lower()
    if "os" in normalized or "操作系统" in normalized:
        return 0
    if "ai" in normalized or "人工智能" in normalized:
        return 1
    return 2
