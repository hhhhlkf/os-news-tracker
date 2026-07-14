from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any

_MAX_LOGS = 300
_logs: deque[dict[str, Any]] = deque(maxlen=_MAX_LOGS)
_lock = Lock()
_next_id = 1


def clear_run_logs() -> None:
    global _next_id
    with _lock:
        _logs.clear()
        _next_id = 1


def append_run_log(
    stage: str,
    message: str,
    *,
    source: str | None = None,
    level: str = "info",
    **fields: Any,
) -> dict[str, Any]:
    global _next_id
    with _lock:
        row = {
            "id": _next_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "stage": stage,
            "source": source,
            "message": message,
            **fields,
        }
        _next_id += 1
        _logs.append(row)
        return row


def list_run_logs(*, after_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
    with _lock:
        rows = list(_logs)
    if after_id is not None:
        rows = [row for row in rows if row["id"] > after_id]
    return rows[-limit:]


def build_not_stored_log_fields(result: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"reason": result.reason}
    if getattr(result, "detail", None):
        fields["reason_detail"] = result.detail
    return fields
