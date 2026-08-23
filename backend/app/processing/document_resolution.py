"""Resolve the document representation used to normalize a fetched item."""

from app.enums import SourceType
from app.extract.base import ContentExtractor
from app.models import Source
from app.schemas import ExtractedDoc, RawItem


def resolve_document(
    source: Source,
    raw: RawItem,
    extractor: ContentExtractor | None,
) -> ExtractedDoc:
    """Return the best document for a source item while preserving item dates."""
    if source.type == SourceType.RSS:
        if raw.raw_content and len(raw.raw_content) > 200:
            return ExtractedDoc(
                url=raw.url,
                title=raw.title,
                clean_content=raw.raw_content,
                published_at=raw.published_at,
            )
        document = _extract(raw.url, extractor)
        document.published_at = raw.published_at or document.published_at
        return document
    if source.type == SourceType.PAGE_MONITOR and raw.raw_content is None:
        document = _extract(raw.url, extractor)
        document.published_at = raw.published_at or document.published_at
        return document
    return ExtractedDoc(
        url=raw.url,
        title=raw.title,
        clean_content=raw.raw_content or "",
        published_at=raw.published_at,
    )


def _extract(url: str, extractor: ContentExtractor | None) -> ExtractedDoc:
    if extractor is None:
        raise AttributeError("'NoneType' object has no attribute 'extract'")
    return extractor.extract(url)
