import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.extract.base import ContentExtractor
from app.models import Source
from app.processing.content_policy import (
    constrain_category,
    is_bot_challenge_page,
    legacy_page_quality_rejection,
)
from app.processing.document_resolution import resolve_document
from app.processing.enriched_item_store import EnrichedItemStore
from app.processing.normalizer import normalize
from app.processing.relevance import llm_relevance
from app.repository import Repository
from app.schemas import EnrichedFields, NormalizedItem, RawItem

logger = logging.getLogger(__name__)
MIN_EXTRACTED_RETRY_CONTENT_LEN = 500


class SourceFetcher(Protocol):
    def fetch(self, source: Source) -> Iterable[RawItem]: ...


class ItemEnricher(Protocol):
    """Minimum enrichment interface shared by production and test enrichers."""

    def enrich(self, item: NormalizedItem) -> EnrichedFields: ...


@runtime_checkable
class ExistingTagItemEnricher(Protocol):
    """Optional enrichment capability supported by the production enricher."""

    def enrich(
        self,
        item: NormalizedItem,
        *,
        existing_tags: list[dict] | None = None,
    ) -> EnrichedFields: ...


@dataclass(frozen=True)
class ProcessItemResult:
    stored: bool
    reason: str
    detail: str | None = None


class Pipeline:
    """Orchestrate fetching, enrichment, and storage of source items."""

    def __init__(
        self,
        session: Session,
        extractor: ContentExtractor | None,
        enricher: ItemEnricher,
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._extractor = extractor
        self._enricher = enricher
        self._cancel_check = cancel_check
        self._repo = Repository(session, before_commit=cancel_check)
        self._item_store = EnrichedItemStore(session, self._repo)

    def run_source(self, source: Source, fetcher: SourceFetcher) -> int:
        try:
            raw_items = fetcher.fetch(source)
        except Exception:
            logger.exception("fetch failed for source %s", source.name)
            source.fail_count += 1
            source.health_status = "error"
            self._session.commit()
            return 0
        return self.process_items(source, raw_items)

    def process_items(
        self,
        source: Source,
        raw_items: Iterable[RawItem],
        *,
        should_stop: Callable[[], bool] | None = None,
        on_processed: Callable[[RawItem], None] | None = None,
    ) -> int:
        new_count = 0
        for raw in raw_items:
            if should_stop and should_stop():
                break
            if self.process_item(source, raw):
                new_count += 1
            if on_processed:
                on_processed(raw)
        source.health_status = "ok"
        self._session.commit()
        return new_count

    def process_item(self, source: Source, raw: RawItem) -> bool:
        return self.process_item_result(source, raw).stored

    def process_item_result(self, source: Source, raw: RawItem) -> ProcessItemResult:
        normalized = self._normalize_item(source, raw)
        if self._repo.exists_by_canonical(normalized.canonical_url):
            self._repo.merge_source_link(normalized.canonical_url, source.id, raw.url)
            return ProcessItemResult(False, "duplicate")
        if is_bot_challenge_page(normalized):
            logger.info(
                "bot challenge page filtered out %s (source=%s)",
                normalized.canonical_url,
                source.name,
            )
            return ProcessItemResult(False, "bot_challenge")
        quality_rejection = legacy_page_quality_rejection(source, raw, normalized)
        if quality_rejection is not None:
            self._log_legacy_quality_rejection(source, normalized, quality_rejection.reason)
            return ProcessItemResult(False, quality_rejection.reason, quality_rejection.detail)
        if source.relevance_filter and not llm_relevance(
            normalized.title,
            normalized.clean_content,
            source.relevance_keywords or "",
        ):
            logger.info(
                "relevance filtered out %s (source=%s)",
                normalized.canonical_url,
                source.name,
            )
            return ProcessItemResult(False, "relevance")
        try:
            fields = self._enrich(normalized)
        except Exception:
            logger.exception("enrich failed for %s", normalized.canonical_url)
            return ProcessItemResult(False, "enrich_failed")
        if self._cancel_check is not None:
            self._cancel_check()
        if not fields.should_store:
            retry_result = self._retry_with_extracted_content(source, raw, normalized, fields)
            if retry_result is not None:
                return retry_result
            logger.info(
                "enricher rejected %s (source=%s, reason=%s)",
                normalized.canonical_url,
                source.name,
                fields.reject_reason or "unknown",
            )
            return ProcessItemResult(False, "enrich_reject", fields.reject_reason or "unknown")
        return self._store_enriched(source, raw, normalized, fields)

    def _normalize_item(self, source: Source, raw: RawItem) -> NormalizedItem:
        document = resolve_document(source, raw, self._extractor)
        item = normalize(raw, document)
        if item.published_at is None:
            logger.warning(
                "item %s has no published_at after extraction (source=%s, type=%s)",
                item.canonical_url,
                source.name,
                source.type,
            )
        return item

    def _enrich(self, item: NormalizedItem) -> EnrichedFields:
        if isinstance(self._enricher, ExistingTagItemEnricher):
            try:
                return self._enricher.enrich(
                    item,
                    existing_tags=self._repo.list_existing_sub_tags(),
                )
            except TypeError:
                pass
        return self._enricher.enrich(item)

    def _store_enriched(
        self,
        source: Source,
        raw: RawItem,
        item: NormalizedItem,
        fields: EnrichedFields,
        *,
        failure_log_prefix: str = "save",
    ) -> ProcessItemResult:
        result = self._item_store.store(
            item,
            constrain_category(source, fields),
            source,
            raw.url,
            failure_log_prefix=failure_log_prefix,
        )
        return ProcessItemResult(result.stored, result.reason, result.detail)

    def _retry_with_extracted_content(
        self,
        source: Source,
        raw: RawItem,
        original: NormalizedItem,
        rejected_fields: EnrichedFields,
    ) -> ProcessItemResult | None:
        if self._extractor is None or not rejected_fields.should_fetch_full_text:
            return None
        try:
            document = self._extractor.extract(raw.url)
        except Exception as exc:
            logger.info(
                "full-text retry extraction failed for %s (source=%s): %s",
                original.canonical_url,
                source.name,
                exc,
            )
            return None
        document.published_at = document.published_at or raw.published_at
        extracted = normalize(raw, document)
        if len(extracted.clean_content) < MIN_EXTRACTED_RETRY_CONTENT_LEN:
            logger.info(
                "full-text retry skipped %s (source=%s, reason=short_extracted_content, len=%s)",
                original.canonical_url,
                source.name,
                len(extracted.clean_content),
            )
            return None
        if extracted.clean_content.strip() == original.clean_content.strip():
            return None
        if is_bot_challenge_page(extracted):
            return ProcessItemResult(False, "bot_challenge")
        try:
            fields = self._enrich(extracted)
        except Exception:
            logger.exception("full-text retry enrich failed for %s", extracted.canonical_url)
            return ProcessItemResult(False, "enrich_failed", "full_text_retry")
        if self._cancel_check is not None:
            self._cancel_check()
        if not fields.should_store:
            logger.info(
                "full-text retry enricher rejected %s (source=%s, reason=%s)",
                extracted.canonical_url,
                source.name,
                fields.reject_reason or "unknown",
            )
            return ProcessItemResult(
                False,
                "enrich_reject",
                f"补抓正文后仍拒收：{fields.reject_reason or 'unknown'}",
            )
        result = self._store_enriched(source, raw, extracted, fields, failure_log_prefix="full-text retry save")
        if result.stored:
            logger.info(
                "full-text retry stored %s (source=%s)",
                extracted.canonical_url,
                source.name,
            )
        return result

    def _log_legacy_quality_rejection(self, source: Source, item: NormalizedItem, reason: str) -> None:
        if reason == "legacy_page_short_content":
            logger.info(
                "legacy page_monitor skipped %s (source=%s, reason=short_content, len=%s)",
                item.canonical_url,
                source.name,
                len(item.clean_content),
            )
        else:
            logger.info(
                "legacy page_monitor skipped %s (source=%s, reason=missing_published_at)",
                item.canonical_url,
                source.name,
            )
