"""Source-neutral representation of a usable partial connector result."""

from __future__ import annotations

from typing import Any


class PartialExecutionError(RuntimeError):
    """Execution failed after producing candidates that may still be ingested."""

    def __init__(
        self,
        message: str,
        *,
        items: list[dict[str, Any]],
        stats: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.items = items
        self.stats = stats
