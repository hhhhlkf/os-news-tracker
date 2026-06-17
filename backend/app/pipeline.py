import logging
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.enums import SourceType
from app.models import Source
from app.processing.normalizer import normalize
from app.processing.relevance import llm_relevance
from app.repository import Repository
from app.schemas import NormalizedItem, RawItem

logger = logging.getLogger(__name__)

INTERNAL_AI_CATEGORY = "司内AI工具"
INTERNAL_AI_HOSTS = {"km.woa.com", "iwiki.woa.com"}

# Legacy whole-page monitors need enough article-like content before LLM enrichment.
MIN_CONTENT_LEN = 500

BOT_CHALLENGE_MARKERS = (
    "making sure you're not a bot",
    "确保您不是机器人",
    "anubis",
    "proof-of-work",
    "hashcash",
    "please enable javascript",
    "enable javascript",
    "browser verification",
)


class Pipeline:
    def __init__(self, session: Session, extractor, enricher):
        self._session = session
        self._extractor = extractor
        self._enricher = enricher
        self._repo = Repository(session)

    def run_source(self, source: Source, fetcher) -> int:
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
        raw_items,
        *,
        should_stop=None,
        on_processed=None,
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

    def process_item(self, source: Source, raw) -> bool:
        doc = self._extract_for(raw, source)
        normalized = normalize(raw, doc)
        if normalized.published_at is None:
            logger.warning(
                "item %s has no published_at after extraction (source=%s, type=%s)",
                normalized.canonical_url, source.name, source.type,
            )
        if self._repo.exists_by_canonical(normalized.canonical_url):
            self._repo.merge_source_link(normalized.canonical_url, source.id, raw.url)
            return False
        if self._is_bot_challenge_page(normalized):
            logger.info(
                "bot challenge page filtered out %s (source=%s)",
                normalized.canonical_url,
                source.name,
            )
            return False
        if self._is_legacy_page_monitor_item(source, raw):
            if not self._passes_legacy_page_quality_gate(source, normalized):
                return False
        if source.relevance_filter:
            if not llm_relevance(
                normalized.title,
                normalized.clean_content,
                source.relevance_keywords or "",
            ):
                logger.info(
                    "relevance filtered out %s (source=%s)",
                    normalized.canonical_url,
                    source.name,
                )
                return False
        try:
            fields = self._enricher.enrich(normalized)
        except Exception:
            logger.exception("enrich failed for %s", normalized.canonical_url)
            return False
        if not fields.should_store:
            logger.info(
                "enricher rejected %s (source=%s, reason=%s)",
                normalized.canonical_url,
                source.name,
                fields.reject_reason or "unknown",
            )
            return False
        fields = self._apply_category_constraints(source, fields)
        self._repo.save_enriched(normalized, fields)
        return True

    def _extract_for(self, raw, source: Source):
        from app.schemas import ExtractedDoc

        if source.type == SourceType.RSS:
            if raw.raw_content and len(raw.raw_content) > 200:
                return ExtractedDoc(
                    url=raw.url,
                    title=raw.title,
                    clean_content=raw.raw_content,
                    published_at=raw.published_at,
                )
            doc = self._extractor.extract(raw.url)
            # Preserve the RSS-provided date if page extraction didn't find one.
            doc.published_at = doc.published_at or raw.published_at
            return doc

        # List-mode page_monitor: raw_content is None → fetch article page.
        if source.type == SourceType.PAGE_MONITOR and raw.raw_content is None:
            doc = self._extractor.extract(raw.url)
            doc.published_at = doc.published_at or raw.published_at
            return doc

        return ExtractedDoc(
            url=raw.url,
            title=raw.title,
            clean_content=raw.raw_content or "",
            published_at=raw.published_at,
        )

    def _is_bot_challenge_page(self, item: NormalizedItem) -> bool:
        text = f"{item.title}\n{item.clean_content}".lower()
        marker_count = sum(1 for marker in BOT_CHALLENGE_MARKERS if marker in text)
        return marker_count >= 2

    def _apply_category_constraints(self, source: Source, fields):
        if fields.main_category != INTERNAL_AI_CATEGORY:
            return fields

        host = urlparse(source.url).hostname or ""
        if host in INTERNAL_AI_HOSTS:
            return fields

        fallback_category = source.main_category or "OS跟踪来源"
        return fields.model_copy(update={"main_category": fallback_category})

    def _is_legacy_page_monitor_item(self, source: Source, raw: RawItem) -> bool:
        return (
            source.type == SourceType.PAGE_MONITOR
            and not source.link_selector
            and raw.raw_content is not None
        )

    def _passes_legacy_page_quality_gate(
        self,
        source: Source,
        item: NormalizedItem,
    ) -> bool:
        if len(item.clean_content) < MIN_CONTENT_LEN:
            logger.info(
                "legacy page_monitor skipped %s (source=%s, reason=short_content, len=%s)",
                item.canonical_url,
                source.name,
                len(item.clean_content),
            )
            return False
        if item.published_at is None:
            logger.info(
                "legacy page_monitor skipped %s (source=%s, reason=missing_published_at)",
                item.canonical_url,
                source.name,
            )
            return False
        return True
