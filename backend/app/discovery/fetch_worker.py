"""Child-process entry for discovery method fetch (killable by parent)."""

from __future__ import annotations

from typing import Any


def execute_discovery_fetch(
    method_id: int,
    run_id: int,
    request_payload: dict[str, Any] | None,
    *,
    log_queue: Any | None = None,
    db: Any | None = None,
) -> dict[str, Any]:
    from app.discovery.runner import execute_discovery_fetch as _execute_discovery_fetch

    return _execute_discovery_fetch(
        method_id,
        run_id,
        request_payload,
        log_queue=log_queue,
        db=db,
    )
