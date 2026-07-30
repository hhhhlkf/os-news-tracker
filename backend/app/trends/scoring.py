"""Reusable storyline influence scoring shared by fact and future window layers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class InfluenceEvidence:
    at: date
    importance: str | None
    membership: str


_QUALITY = {"高": 100.0, "中": 65.0, "低": 35.0}
_COUNT_CAP = 12
_RECENCY_HALF_LIFE_DAYS = 30.0


def calculate_influence_score(
    *,
    evidence: list[InfluenceEvidence],
    cohesion_score: float,
    recency_anchor: date | None = None,
) -> float:
    """Score non-duplicate evidence using the design's reusable four-part formula.

    The caller supplies the complete storyline or a window subset.  When
    ``recency_anchor`` is omitted, recency is relative to that subset's own
    latest date so an old but internally coherent storyline is not penalized
    merely for being historical.  Template window scoring passes the window
    end date so older evidence decays relative to the evaluation window.
    """

    independent = [item for item in evidence if item.membership != "duplicate"]
    if not independent:
        return 0.0
    quality = sum(_QUALITY.get(item.importance or "", 50.0) for item in independent) / len(independent)
    saturation = min(1.0, math.log1p(len(independent)) / math.log1p(_COUNT_CAP)) * 100.0
    ending = recency_anchor if recency_anchor is not None else max(item.at for item in independent)
    recency = sum(
        math.exp(-math.log(2) * max(0, (ending - item.at).days) / _RECENCY_HALF_LIFE_DAYS) * 100.0
        for item in independent
    ) / len(independent)
    cohesion = max(0.0, min(1.0, cohesion_score)) * 100.0
    return round((0.20 * quality) + (0.30 * cohesion) + (0.25 * saturation) + (0.25 * recency), 2)


def calculate_trend_rank_score(*, window_influence_score: float, template_relevance_score: float) -> float:
    """Combine system window influence with identity relevance for publication order."""

    return round((0.4 * window_influence_score) + (0.6 * template_relevance_score), 2)
