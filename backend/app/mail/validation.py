"""Email recipient normalization and validation for mail APIs."""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)
_MAX_EMAIL_LENGTH = 254


def is_valid_email(value: str) -> bool:
    email = (value or "").strip()
    if not email or len(email) > _MAX_EMAIL_LENGTH:
        return False
    if ".." in email:
        return False
    return bool(_EMAIL_RE.fullmatch(email))


def normalize_recipients(values: list[str] | None) -> list[str]:
    """Trim, validate, and de-dupe recipients. Raises ValueError on invalid entries."""
    if values is None:
        return []

    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()

    for raw in values:
        token = str(raw or "").strip()
        if not token:
            continue
        if not is_valid_email(token):
            if token not in invalid:
                invalid.append(token)
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        valid.append(token)

    if invalid:
        sample = "、".join(invalid[:3])
        more = f" 等 {len(invalid)} 个" if len(invalid) > 3 else ""
        raise ValueError(f"存在非法邮箱：{sample}{more}")

    return valid
