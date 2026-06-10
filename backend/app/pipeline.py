import logging

from sqlalchemy.orm import Session

from app.enums import SourceType
from app.models import Source
from app.processing.normalizer import normalize
from app.repository import Repository

logger = logging.getLogger(__name__)


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

        new_count = 0
        for raw in raw_items:
            doc = self._extract_for(raw, source)
            normalized = normalize(raw, doc)
            if self._repo.exists_by_canonical(normalized.canonical_url):
                self._repo.merge_source_link(normalized.canonical_url, source.id, raw.url)
                continue
            try:
                fields = self._enricher.enrich(normalized)
            except Exception:
                logger.exception("enrich failed for %s", normalized.canonical_url)
                continue
            self._repo.save_enriched(normalized, fields)
            new_count += 1

        source.health_status = "ok"
        self._session.commit()
        return new_count

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
