"""Serial vector backfill and deterministic complete-linkage candidate clustering."""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Callable, Sequence

import numpy as np
from sqlalchemy.orm import Session

from app.trends.repository import ClusterPoolCard, StoredCandidateCluster, VectorStageCounts
from app.trends.vectors import (
    CardEmbeddingRequestItem,
    CardEmbeddingResult,
    EmbeddingNotReadyError,
    EmbeddingVersionMismatchError,
    generate_card_vectors,
)

logger = logging.getLogger(__name__)

# Persist and publish progress per card so the UI can reflect each completed
# event-core vector instead of appearing stalled until a large batch finishes.
VECTOR_BATCH_SIZE = 1
MIN_CANDIDATE_CLUSTER_SIZE = 3
# Agents must receive a bounded evidence set; this is the non-negotiable number
# of cards one future storyline-review prompt may be asked to inspect at once.
AGENT_CONTEXT_CARD_LIMIT = 12
# Complete linkage requires every card pair in a cluster to meet the threshold.
# The lower bands retain enough three-card technical-event candidates for Agent
# review while the 3G cap still bounds its total context cost.
CANDIDATE_THRESHOLDS: tuple[float, ...] = (
    0.92,
    0.88,
    0.84,
    0.80,
    0.76,
    0.72,
    0.68,
    0.64,
    0.60,
    0.58,
    0.56,
    0.54,
    0.52,
    0.50,
    0.48,
)
PENDING_MATCH_DAYS = 90


@dataclass(frozen=True)
class CandidateCluster:
    member_indices: tuple[int, ...]
    threshold: float
    cohesion_score: float


def _cohesion(matrix: np.ndarray, members: Sequence[int]) -> float:
    if len(members) < 2:
        return 0.0
    selected = matrix[np.ix_(members, members)]
    return float((selected.sum() - len(members)) / (len(members) * (len(members) - 1)))


def _complete_linkage(similarities: np.ndarray, threshold: float) -> list[tuple[int, ...]]:
    """Agglomerate by complete linkage using incremental minimum similarities."""
    count = similarities.shape[0]
    members: list[tuple[int, ...]] = [(index,) for index in range(count)]
    active = np.ones(count, dtype=bool)
    cluster_similarities = similarities.copy()
    np.fill_diagonal(cluster_similarities, -np.inf)
    while True:
        available = np.outer(active, active)
        scores = np.where(available, cluster_similarities, -np.inf)
        np.fill_diagonal(scores, -np.inf)
        left, right = np.unravel_index(np.argmax(scores), scores.shape)
        if float(scores[left, right]) < threshold:
            return [members[index] for index, is_active in enumerate(active) if is_active]
        if right < left:
            left, right = right, left
        # For complete linkage, sim(A ∪ B, C) is min(sim(A, C), sim(B, C)).
        merged_similarities = np.minimum(
            cluster_similarities[left, :], cluster_similarities[right, :]
        )
        members[left] = tuple(sorted((*members[left], *members[right])))
        active[right] = False
        cluster_similarities[left, :] = merged_similarities
        cluster_similarities[:, left] = merged_similarities
        cluster_similarities[right, :] = -np.inf
        cluster_similarities[:, right] = -np.inf
        cluster_similarities[left, left] = -np.inf


def _split_oversized(
    similarities: np.ndarray, members: tuple[int, ...], threshold: float, maximum: int
) -> list[tuple[int, ...]]:
    if len(members) <= maximum:
        return [members]
    stricter = next((value for value in reversed(CANDIDATE_THRESHOLDS) if value > threshold), min(0.999, threshold + 0.02))
    local = similarities[np.ix_(members, members)]
    parts = [tuple(members[index] for index in part) for part in _complete_linkage(local, stricter)]
    if len(parts) == 1 and len(parts[0]) > maximum:
        # Identical texts can remain inseparable even at the strictest threshold;
        # retain deterministic, bounded chunks rather than overflow Agent context.
        return [parts[0][start : start + maximum] for start in range(0, len(parts[0]), maximum)]
    result: list[tuple[int, ...]] = []
    for part in parts:
        result.extend(_split_oversized(similarities, part, stricter, maximum))
    return result


def _clusters_for_threshold(
    similarities: np.ndarray, *, threshold: float, maximum: int
) -> list[CandidateCluster]:
    raw = _complete_linkage(similarities, threshold)
    candidates: list[CandidateCluster] = []
    for members in raw:
        if len(members) < MIN_CANDIDATE_CLUSTER_SIZE:
            continue
        for split in _split_oversized(similarities, members, threshold, maximum):
            cohesion = _cohesion(similarities, split)
            if len(split) >= MIN_CANDIDATE_CLUSTER_SIZE and cohesion >= threshold:
                candidates.append(CandidateCluster(split, threshold, cohesion))
    return candidates


def generate_candidate_clusters(
    cards: Sequence[ClusterPoolCard], *, candidate_goal: int
) -> tuple[list[StoredCandidateCluster], float | None, int]:
    """Use only one normalized vector space and return bounded, pure clusters."""
    pool_size = len(cards)
    maximum = min(AGENT_CONTEXT_CARD_LIMIT, max(2, math.ceil(pool_size / (2 * candidate_goal))))
    if pool_size < 2:
        return [], None, maximum
    vectors = np.asarray([card.embedding for card in cards], dtype=np.float64)
    if vectors.ndim != 2 or vectors.shape[1] != 1024:
        raise ValueError("候选池包含非 1024 维向量，已拒绝聚类；请重新生成当前版本向量。")
    # Vectors were L2-normalized by the Worker, therefore cosine similarity is
    # exactly their dot product. Do not re-normalize or convert to distance.
    similarities = vectors @ vectors.T
    cap = 3 * candidate_goal
    selected: list[CandidateCluster] = []
    selected_threshold: float | None = None
    for threshold in CANDIDATE_THRESHOLDS:
        candidates = _clusters_for_threshold(similarities, threshold=threshold, maximum=maximum)
        if not candidates:
            continue
        # Evaluate every threshold. Prefer the result with the most candidates
        # after applying the 3G hard budget; equal counts keep the higher,
        # purer threshold because this sequence is high → low. If every raw
        # result exceeds 3G, this same cap retains its top-cohesion 3G entries.
        capped = sorted(
            candidates,
            key=lambda value: (-value.cohesion_score, value.member_indices),
        )[:cap]
        if len(capped) > len(selected):
            selected = capped
            selected_threshold = threshold
    return (
        [
            StoredCandidateCluster(
                member_card_ids=[cards[index].card_id for index in cluster.member_indices],
                threshold=cluster.threshold,
                cohesion_score=cluster.cohesion_score,
            )
            for cluster in selected
        ],
        selected_threshold,
        maximum,
    )


@dataclass(frozen=True)
class VectorRuntimeState:
    is_running: bool
    start_date: date
    end_date: date
    pending_count: int
    generated_count: int
    failed_count: int
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None


class VectorBackfillController:
    """One process-wide, deliberately serial client of the singleton Worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: VectorRuntimeState | None = None

    def snapshot(self) -> VectorRuntimeState | None:
        with self._lock:
            return self._state

    def start(
        self,
        *,
        start_date: date,
        end_date: date,
        candidates: list[CardEmbeddingRequestItem],
        counts: VectorStageCounts,
        embedding_version: str,
        session_factory: Callable[[], Session],
        write_batch: Callable[[Session, Sequence[CardEmbeddingResult]], None],
    ) -> VectorRuntimeState:
        with self._lock:
            if self._state is not None and self._state.is_running:
                return self._state
            now = datetime.now(timezone.utc)
            self._state = VectorRuntimeState(
                is_running=bool(candidates), start_date=start_date, end_date=end_date,
                pending_count=len(candidates), generated_count=counts.generated_count,
                failed_count=0, started_at=now if candidates else None,
                finished_at=None if candidates else now, error_message=None,
            )
            if candidates:
                threading.Thread(
                    target=self._run,
                    kwargs={"candidates": candidates, "embedding_version": embedding_version,
                            "session_factory": session_factory, "write_batch": write_batch},
                    name="trend-vector-backfill", daemon=True,
                ).start()
            return self._state

    def _run(
        self, *, candidates: list[CardEmbeddingRequestItem], embedding_version: str,
        session_factory: Callable[[], Session], write_batch: Callable[[Session, Sequence[CardEmbeddingResult]], None],
    ) -> None:
        error_message: str | None = None
        for start in range(0, len(candidates), VECTOR_BATCH_SIZE):
            batch = candidates[start : start + VECTOR_BATCH_SIZE]
            try:
                # Intentionally no executor / asyncio: /embed is a singleton service.
                results = generate_card_vectors(batch, embedding_version=embedding_version)
                db = session_factory()
                try:
                    write_batch(db, results)
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()
            except EmbeddingNotReadyError as exc:  # preserve atomic pending batch
                error_message = f"{exc.message} {exc.remedy or ''}".strip()
                logger.exception("trend vector batch is retryable; no vectors from this batch were written")
                with self._lock:
                    assert self._state is not None
                    self._state = replace(self._state, failed_count=self._state.failed_count + len(batch), error_message=error_message)
                break
            except EmbeddingVersionMismatchError as exc:
                error_message = f"{exc}；请统一后端和 Worker 的 embedding_version 后重新触发。"
                logger.exception("trend vector batch failed; no vectors from this batch were written")
                with self._lock:
                    assert self._state is not None
                    self._state = replace(self._state, failed_count=self._state.failed_count + len(batch), error_message=error_message)
                break
            except Exception as exc:  # preserve atomic pending batch
                error_message = str(exc)
                logger.exception("trend vector batch failed; no vectors from this batch were written")
                with self._lock:
                    assert self._state is not None
                    self._state = replace(self._state, failed_count=self._state.failed_count + len(batch), error_message=error_message)
                break
            with self._lock:
                assert self._state is not None
                self._state = replace(
                    self._state,
                    pending_count=self._state.pending_count - len(batch),
                    generated_count=self._state.generated_count + len(batch),
                )
        with self._lock:
            assert self._state is not None
            self._state = replace(self._state, is_running=False, finished_at=datetime.now(timezone.utc), error_message=error_message)


vector_backfill_controller = VectorBackfillController()
