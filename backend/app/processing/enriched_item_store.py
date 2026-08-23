"""Persistence of enriched items, including concurrent deduplication recovery."""

import logging
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Source
from app.repository import Repository
from app.schemas import EnrichedFields, NormalizedItem

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnrichedItemStoreResult:
    stored: bool
    reason: str
    detail: str | None = None


class EnrichedItemStore:
    """Persist items and turn unique-key races into source-link merges."""

    def __init__(self, session: Session, repository: Repository) -> None:
        self._session = session
        self._repository = repository

    def store(
        self,
        item: NormalizedItem,
        fields: EnrichedFields,
        source: Source,
        source_item_url: str,
        *,
        failure_log_prefix: str = "save",
    ) -> EnrichedItemStoreResult:
        try:
            self._repository.save_enriched(item, fields)
        except IntegrityError:
            self._session.rollback()
            if self._repository.exists_by_canonical(item.canonical_url):
                self._repository.merge_source_link(item.canonical_url, source.id, source_item_url)
                return EnrichedItemStoreResult(False, "duplicate", "integrity_conflict")
            logger.exception("%s failed for %s", failure_log_prefix, item.canonical_url)
            return EnrichedItemStoreResult(False, "save_failed", "integrity_conflict")
        return EnrichedItemStoreResult(True, "stored")
