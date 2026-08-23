"""Central redaction boundary for persisted Discovery checkpoints and events."""

from __future__ import annotations

import re
from typing import Any


REDACTED = "[redacted]"
MAX_REDACTION_DEPTH = 32
MAX_REDACTION_NODES = 20_000
MAX_REDACTION_STRING_CHARS = 512_000
MAX_REDACTION_APPROX_BYTES = 4 * 1024 * 1024
_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "header",
    "headers",
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "proxy_authorization",
    "set_cookie",
}
_PRIVATE_REASONING_KEYS = {
    "chain_of_thought",
    "private_reasoning",
    "raw_model_response",
    "reasoning_content",
}
_EPHEMERAL_KEYS = {
    "heartbeat",
    "model_token",
    "model_token_delta",
    "raw_stdout",
    "stdout_chunk",
    "stdout_fragment",
    "token_delta",
}
_TOKEN_METRIC_KEYS = {
    "completion_tokens",
    "llm_token_usage",
    "max_tokens",
    "prompt_tokens",
    "remaining_tokens",
    "token_count",
    "token_usage",
    "total_tokens",
}
_SENSITIVE_KEY_PART = re.compile(
    r"(^|_)(auth|authentication|authorization|token|secret|password|passwd|cookie|credential|credentials|api_key|apikey)($|_)"
)
_TEXT_SECRET_KEY = (
    r"[A-Za-z0-9_.-]*(?:secret|credential|auth(?:orization|entication)?|token|"
    r"password|passwd|cookie|api[_-]?key|signature|signed)[A-Za-z0-9_.-]*"
)
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_BEARER_CREDENTIAL = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_QUOTED_SECRET = re.compile(
    rf"(?i)([\"']{_TEXT_SECRET_KEY}[\"']\s*:\s*[\"'])[^\"']*([\"'])"
)
_QUERY_SECRET = re.compile(rf"(?i)([?&]{_TEXT_SECRET_KEY}=)[^&#\s]*")
_ASSIGNED_SECRET = re.compile(
    rf"(?i)(\b{_TEXT_SECRET_KEY}\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&}}]+)"
)


def validate_discovery_data_bounds(
    value: Any,
    *,
    max_depth: int = MAX_REDACTION_DEPTH,
    max_nodes: int = MAX_REDACTION_NODES,
    max_string_chars: int = MAX_REDACTION_STRING_CHARS,
    max_approx_bytes: int = MAX_REDACTION_APPROX_BYTES,
) -> None:
    """Iteratively reject hostile depth, cycles, node counts and oversized strings."""
    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    ancestors: set[int] = set()
    nodes = 0
    approximate_bytes = 0
    while stack:
        current, depth, leaving = stack.pop()
        if leaving:
            ancestors.remove(id(current))
            continue
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValueError("Discovery persistence payload exceeds traversal limits")
        if isinstance(current, str):
            if len(current) > max_string_chars:
                raise ValueError("Discovery persistence string exceeds its limit")
            approximate_bytes += len(current) * 4
        elif isinstance(current, dict):
            identity = id(current)
            if identity in ancestors:
                raise ValueError("Discovery persistence payload contains a cycle")
            ancestors.add(identity)
            stack.append((current, depth, True))
            for key, item in current.items():
                key_text = str(key)
                if len(key_text) > max_string_chars:
                    raise ValueError("Discovery persistence key exceeds its limit")
                approximate_bytes += len(key_text) * 4
                stack.append((item, depth + 1, False))
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in ancestors:
                raise ValueError("Discovery persistence payload contains a cycle")
            ancestors.add(identity)
            stack.append((current, depth, True))
            stack.extend((item, depth + 1, False) for item in current)
        else:
            approximate_bytes += min(len(str(current)), max_string_chars) * 4
        if approximate_bytes > max_approx_bytes:
            raise ValueError("Discovery persistence payload exceeds its byte limit")


def redact_discovery_data(
    value: Any,
    *,
    max_string_chars: int = MAX_REDACTION_STRING_CHARS,
    max_approx_bytes: int = MAX_REDACTION_APPROX_BYTES,
) -> Any:
    """Return a bounded JSON-compatible copy with secrets and private fields removed."""
    validate_discovery_data_bounds(
        value,
        max_string_chars=max_string_chars,
        max_approx_bytes=max_approx_bytes,
    )
    holder: list[Any] = [None]
    stack: list[tuple[Any, Any, str | int]] = [(value, holder, 0)]
    while stack:
        current, parent, slot = stack.pop()
        if isinstance(current, dict):
            output: dict[str, Any] = {}
            parent[slot] = output
            child_tasks: list[tuple[Any, dict[str, Any], str]] = []
            for key, item in current.items():
                key_text = str(key)
                redacted_key = redact_discovery_text(key_text)
                normalized_key = normalize_discovery_key(key_text)
                if _is_private_reasoning_key(normalized_key) or _is_ephemeral_key(normalized_key):
                    continue
                if _is_sensitive_key(normalized_key):
                    safe_key = _unique_redacted_key(output, "[redacted-key]")
                    output[safe_key] = REDACTED
                else:
                    safe_key = _unique_redacted_key(output, redacted_key)
                    output[safe_key] = None
                    child_tasks.append((item, output, safe_key))
            stack.extend(reversed(child_tasks))
        elif isinstance(current, (list, tuple)):
            output_list: list[Any] = [None] * len(current)
            parent[slot] = output_list
            for index in range(len(current) - 1, -1, -1):
                stack.append((current[index], output_list, index))
        elif isinstance(current, str):
            parent[slot] = redact_discovery_text(current)
        elif current is None or isinstance(current, (bool, int, float)):
            parent[slot] = current
        else:
            parent[slot] = redact_discovery_text(str(current))
    return holder[0]


def redact_discovery_text(value: str) -> str:
    """Redact credentials in prose, JSON snippets, signed URLs and PEM material."""
    text = _PEM_PRIVATE_KEY.sub(REDACTED, value)
    text = _BEARER_CREDENTIAL.sub(lambda match: f"{match.group(1)} {REDACTED}", text)
    text = _QUOTED_SECRET.sub(lambda match: f"{match.group(1)}{REDACTED}{match.group(2)}", text)
    text = _QUERY_SECRET.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    return _ASSIGNED_SECRET.sub(lambda match: f"{match.group(1)}{REDACTED}", text)


def _unique_redacted_key(output: dict[str, Any], candidate: str) -> str:
    """Keep every field when multiple secret-bearing names redact to the same key."""
    if candidate not in output:
        return candidate
    for suffix in range(2, 102):
        suffixed = f"{candidate}#{suffix}"
        if suffixed not in output:
            return suffixed
    raise ValueError("Discovery persistence payload has too many redacted key collisions")


def normalize_discovery_key(key: str) -> str:
    """Normalize persisted field/event names across camelCase, kebab-case and snake_case."""
    snake_key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", snake_key.lower())).strip("_")


def _is_private_reasoning_key(normalized_key: str) -> bool:
    return normalized_key in _PRIVATE_REASONING_KEYS or any(
        marker in normalized_key
        for marker in (
            "chain_of_thought",
            "private_reasoning",
            "raw_model_response",
            "reasoning_content",
        )
    )


def _is_ephemeral_key(normalized_key: str) -> bool:
    return normalized_key in _EPHEMERAL_KEYS or normalized_key.endswith(
        (
            "_heartbeat",
            "_model_token",
            "_model_token_delta",
            "_raw_stdout",
            "_stdout_chunk",
            "_stdout_fragment",
            "_token_delta",
        )
    )


def should_drop_discovery_field(key: str) -> bool:
    """Return whether a field/event name is private reasoning or ephemeral stream data."""
    normalized_key = normalize_discovery_key(key)
    return _is_private_reasoning_key(normalized_key) or _is_ephemeral_key(normalized_key)


def _is_sensitive_key(normalized_key: str) -> bool:
    if normalized_key in _TOKEN_METRIC_KEYS or normalized_key.endswith(
        ("_token_usage", "_token_count")
    ):
        return False
    if normalized_key in _SENSITIVE_KEYS:
        return True
    if any(
        marker in normalized_key
        for marker in ("authorization", "credential", "password", "passwd", "cookie")
    ):
        return True
    return _SENSITIVE_KEY_PART.search(normalized_key) is not None
