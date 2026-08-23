"""Restricted hybrid retrieval over approved Discovery experience only."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.discovery.redaction import redact_discovery_data
from app.discovery.loop.experience_store import experience_has_valid_provenance
from app.models import CrawlMethod, DiscoveryExperience
from app.trends.embedding_client import request_embeddings


_WORDS = re.compile(r"[a-zA-Z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}")
RagStage = Literal["explore", "build", "repair"]
_KINDS_BY_STAGE: dict[RagStage, tuple[str, ...]] = {
    "explore": ("explore_strategy",),
    "build": ("build_pattern",),
    "repair": ("confirmed_repair",),
}
_ALLOWED_KINDS = tuple(kind for kinds in _KINDS_BY_STAGE.values() for kind in kinds)


@dataclass(frozen=True)
class RagReference:
    """A bounded, source-attributed experience safe to place in an Agent prompt."""

    experience_id: int
    experience_kind: str
    domain: str | None
    technical_features: dict[str, Any]
    summary: str
    failure_summary: str | None
    repair_summary: str | None
    score: float
    embedding_version: str | None

    def as_prompt_data(self) -> dict[str, Any]:
        return redact_discovery_data(
            {
                "experience_id": self.experience_id,
                "experience_kind": self.experience_kind,
                "domain": self.domain,
                "technical_features": self.technical_features,
                "summary": self.summary,
                "failure_summary": self.failure_summary,
                "repair_summary": self.repair_summary,
                "score": round(self.score, 6),
                "embedding_version": self.embedding_version,
            }
        )


@dataclass(frozen=True)
class RagRetrievalResult:
    """Stage-specific retrieval result plus auditable, non-secret counts."""

    stage: RagStage
    scanned_count: int
    matched_count: int
    references: tuple[RagReference, ...]

    @property
    def injected_count(self) -> int:
        return len(self.references)


class DiscoveryExperienceRetriever:
    """domain/technology filter, keyword shortlist, then app-local cosine Top-K."""

    def retrieve(
        self,
        session: Session,
        *,
        domain: str,
        technical_features: dict[str, Any] | None = None,
        query: str,
        stage: RagStage,
        top_k: int | None = None,
    ) -> RagRetrievalResult:
        limit = min(10, max(1, top_k or get_settings().discovery_rag_top_k))
        stmt = (
            select(DiscoveryExperience)
            .outerjoin(CrawlMethod, DiscoveryExperience.source_method_id == CrawlMethod.id)
            .where(
                DiscoveryExperience.approved.is_(True),
                DiscoveryExperience.experience_kind.in_(_KINDS_BY_STAGE[stage]),
                DiscoveryExperience.source_method_id.is_not(None),
                CrawlMethod.review_status == "approved",
            )
            .limit(200)
        )
        rows = [
            row for row in session.scalars(stmt)
            if experience_has_valid_provenance(session, row)
        ]
        scanned_count = len(rows)
        requested_features = _feature_tokens(technical_features or {})
        if requested_features:
            rows = [
                row for row in rows
                if row.domain in {domain, None}
                or bool(requested_features & _feature_tokens(row.technical_features or {}))
            ]
        # Before Explore there may be no reliable feature classification yet.
        # In that case retain cross-domain approved patterns and let hybrid
        # ranking decide; otherwise RAG can never transfer RSS/SSR/API lessons.
        query_tokens = _tokens(query)
        rows = [
            row for row in rows
            if row.domain == domain or _keyword_score(query_tokens, _experience_text(row)) > 0
        ]
        matched_count = len(rows)
        keyword_ranked = sorted(
            rows,
            key=lambda row: (
                -_rank_keyword(domain, query_tokens, row),
                row.id,
            ),
        )[: max(limit * 8, limit)]
        if not keyword_ranked:
            return RagRetrievalResult(stage, scanned_count, matched_count, ())

        versions = {
            row.embedding_version
            for row in keyword_ranked
            if row.embedding and row.embedding_version
        }
        if not versions:
            references = tuple(
                self._reference(row, float(_rank_keyword(domain, query_tokens, row)))
                for row in keyword_ranked[:limit]
            )
            return RagRetrievalResult(stage, scanned_count, matched_count, references)

        # Never mix vector spaces. Prefer the most represented version, then a stable name.
        version = min(
            versions,
            key=lambda candidate: (
                -sum(row.embedding_version == candidate for row in keyword_ranked),
                candidate,
            ),
        )
        candidates = [row for row in keyword_ranked if row.embedding_version == version and row.embedding]
        try:
            query_vector = request_embeddings([query], embedding_version=version)[0]
        except Exception:
            references = tuple(
                self._reference(row, float(_rank_keyword(domain, query_tokens, row)))
                for row in keyword_ranked[:limit]
            )
            return RagRetrievalResult(stage, scanned_count, matched_count, references)
        vector_ids = {row.id for row in candidates}
        scored = [
            (
                row,
                _cosine(query_vector, [float(value) for value in row.embedding or []])
                + 0.05 * _rank_keyword(domain, query_tokens, row),
            )
            for row in candidates
        ]
        # Approved rows without a compatible vector remain first-class keyword
        # candidates; a partially completed backfill must never erase them.
        scored.extend(
            (row, 0.05 * _rank_keyword(domain, query_tokens, row))
            for row in keyword_ranked
            if row.id not in vector_ids
        )
        scored = sorted(
            scored,
            key=lambda pair: (-pair[1], pair[0].id),
        )
        references = tuple(self._reference(row, score) for row, score in scored[:limit])
        return RagRetrievalResult(stage, scanned_count, matched_count, references)

    @staticmethod
    def _reference(row: DiscoveryExperience, score: float) -> RagReference:
        return RagReference(
            experience_id=row.id,
            experience_kind=row.experience_kind,
            domain=row.domain,
            technical_features=redact_discovery_data(row.technical_features or {}),
            summary=str(redact_discovery_data(row.summary))[:4000],
            failure_summary=(str(redact_discovery_data(row.failure_summary))[:2000]
                             if row.failure_summary else None),
            repair_summary=(str(redact_discovery_data(row.repair_summary))[:2000]
                            if row.repair_summary else None),
            score=score,
            embedding_version=row.embedding_version,
        )


def _tokens(text: str) -> set[str]:
    return {word.lower() for word in _WORDS.findall(text or "")}


def _feature_tokens(features: dict[str, Any]) -> set[str]:
    return _tokens(" ".join(f"{key} {value}" for key, value in features.items()))


def _experience_text(row: DiscoveryExperience) -> str:
    return " ".join(
        value for value in (
            row.domain,
            row.summary,
            row.failure_summary,
            row.repair_summary,
            " ".join(f"{key} {value}" for key, value in (row.technical_features or {}).items()),
        ) if value
    )


def _keyword_score(query_tokens: set[str], text: str) -> int:
    return len(query_tokens & _tokens(text))


def _rank_keyword(domain: str, query_tokens: set[str], row: DiscoveryExperience) -> int:
    """Prefer an exact domain without preventing cross-domain pattern reuse."""
    return _keyword_score(query_tokens, _experience_text(row)) + (3 if row.domain == domain else 0)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return -1.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return numerator / denominator if denominator else -1.0


def backfill_missing_experience_embeddings(session: Session, *, limit: int = 50) -> int:
    """Best-effort indexing seam for approved provenance rows missing vectors."""
    rows = list(
        session.scalars(
            select(DiscoveryExperience)
            .where(
                DiscoveryExperience.approved.is_(True),
                DiscoveryExperience.experience_kind.in_(_ALLOWED_KINDS),
                DiscoveryExperience.embedding.is_(None),
            )
            .order_by(DiscoveryExperience.id)
            .limit(min(200, max(1, limit)))
        )
    )
    rows = [row for row in rows if experience_has_valid_provenance(session, row)]
    if not rows:
        return 0
    from app.trends.embedding import build_embedding_version
    from app.trends.embedding_config import get_trend_embedding_settings

    version = build_embedding_version(get_trend_embedding_settings())
    texts = [_experience_text(row)[:16_000] for row in rows]
    vectors = request_embeddings(texts, embedding_version=version)
    if len(vectors) != len(rows):
        raise ValueError("embedding backfill returned an unexpected vector count")
    for row, vector in zip(rows, vectors, strict=True):
        row.embedding = [float(value) for value in vector]
        row.embedding_version = version
    session.commit()
    return len(rows)
