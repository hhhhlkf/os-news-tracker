import logging
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.enums import SourceType
from app.models import Source
from app.processing.normalizer import normalize
from app.repository import Repository

logger = logging.getLogger(__name__)

INTERNAL_AI_CATEGORY = "司内AI工具"
INTERNAL_AI_HOSTS = {"km.woa.com", "iwiki.woa.com"}


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
        if self._repo.exists_by_canonical(normalized.canonical_url):
            self._repo.merge_source_link(normalized.canonical_url, source.id, raw.url)
            return False
        try:
            fields = self._enricher.enrich(normalized)
        except Exception:
            logger.exception("enrich failed for %s", normalized.canonical_url)
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
            return self._extractor.extract(raw.url)
        return ExtractedDoc(
            url=raw.url,
            title=raw.title,
            clean_content=raw.raw_content or "",
            published_at=raw.published_at,
        )

    def _apply_category_constraints(self, source: Source, fields):
        if fields.main_category != INTERNAL_AI_CATEGORY:
            return fields

        host = urlparse(source.url).hostname or ""
        if host in INTERNAL_AI_HOSTS:
            return fields

        fallback_category = source.main_category or "OS跟踪来源"
        return fields.model_copy(update={"main_category": fallback_category})
