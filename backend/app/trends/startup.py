"""Public startup entry of the trends module.

Called by the embedding worker service only: the worker container owns the
embedding job lifecycle, so it is the only process allowed to reclaim state left
behind by its own crash. A web restart must not touch an in-flight download —
those are aged out by the job timeout instead.
"""

from __future__ import annotations

import logging

from app.trends.embedding_state import reclaim_stale_state

logger = logging.getLogger(__name__)


def register_trend_startup() -> None:
    try:
        reclaim_stale_state()
    except Exception:  # noqa: BLE001 - startup must not fail on trend bookkeeping
        logger.exception("failed to reclaim stale trend embedding worker state")
