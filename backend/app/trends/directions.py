"""Direction labels embedded in a trend identity template.

The template text is the sole authoring surface for directions.  This module
owns the small ``{方向}`` language so persistence, request validation and LLM
prompts cannot drift into separate interpretations.
"""

from __future__ import annotations

import re


MAX_DIRECTION_COUNT = 12
MAX_DIRECTION_LABEL_LENGTH = 50
_DIRECTION_TOKEN = re.compile(r"\{([^{}]*)\}")
_INVALID_BRACE = re.compile(r"[{}]")


class TrendDirectionValidationError(ValueError):
    """The identity template does not contain a valid direction declaration."""


def parse_template_directions(identity_text: str, *, strict: bool = False) -> list[str]:
    """Return ordered, de-duplicated labels declared as ``{label}``.

    In normal read paths it is intentionally tolerant so pre-feature template
    text (including prose that happened to contain braces) remains readable.
    ``strict=True`` turns the same interface into the authoring validator.
    """

    tokens = list(_DIRECTION_TOKEN.finditer(identity_text))
    remainder = _DIRECTION_TOKEN.sub("", identity_text)
    if strict and _INVALID_BRACE.search(remainder):
        raise TrendDirectionValidationError("方向必须使用成对的 {方向名称} 标记")

    directions: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        label = token.group(1).strip()
        if not label:
            if strict:
                raise TrendDirectionValidationError("方向名称不能为空；请使用例如 {AI方向}")
            continue
        if "\n" in label or "\r" in label:
            if strict:
                raise TrendDirectionValidationError("方向名称不能换行")
            continue
        if len(label) > MAX_DIRECTION_LABEL_LENGTH:
            if strict:
                raise TrendDirectionValidationError(
                    f"方向名称不能超过 {MAX_DIRECTION_LABEL_LENGTH} 个字符"
                )
            continue
        if label not in seen:
            seen.add(label)
            directions.append(label)
    if strict and len(directions) > MAX_DIRECTION_COUNT:
        raise TrendDirectionValidationError(
            f"最多可声明 {MAX_DIRECTION_COUNT} 个方向"
        )
    return directions[:MAX_DIRECTION_COUNT]


def require_template_directions(identity_text: str) -> list[str]:
    """Parse directions and require at least one for a new/editable template."""

    directions = parse_template_directions(identity_text, strict=True)
    if not directions:
        raise TrendDirectionValidationError(
            "身份文本至少要包含一个方向标记，例如 {AI方向} 或 {OS方向}"
        )
    return directions
