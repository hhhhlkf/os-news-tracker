import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import SourceType
from app.models import Source
from app.processing.normalizer import normalize
from app.processing.relevance import llm_relevance
from app.repository import Repository
from app.schemas import EnrichedFields, NormalizedItem, RawItem

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

INCOMPLETE_CONTENT_RETRY_MARKERS = (
    "正文不完整",
    "正文截断",
    "摘要碎片",
    "信息不足",
    "内容不足",
    "缺少具体技术",
    "无法支撑",
)

FULL_TEXT_RETRY_KEYWORDS = (
    "ai agent",
    "agent mesh",
    "agentic",
    "llm",
    "mcp",
    "model context protocol",
    "openshift ai",
    "red hat ai",
    "inference",
    "vllm",
    "devstral",
    "ministral",
)

MIN_EXTRACTED_RETRY_CONTENT_LEN = 500


@dataclass(frozen=True)
class ProcessItemResult:
    stored: bool
    reason: str
    detail: str | None = None


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
        return self.process_item_result(source, raw).stored

    def process_item_result(self, source: Source, raw) -> ProcessItemResult:
        # Agent crawl 条目绕过 Enricher — 摘要已由 SummaryWorkerPool 完成
        if raw.extra and raw.extra.get("agent_item"):
            return self._process_agent_item(source, raw)

        doc = self._extract_for(raw, source)
        normalized = normalize(raw, doc)
        if normalized.published_at is None:
            logger.warning(
                "item %s has no published_at after extraction (source=%s, type=%s)",
                normalized.canonical_url, source.name, source.type,
            )
        if self._repo.exists_by_canonical(normalized.canonical_url):
            self._repo.merge_source_link(normalized.canonical_url, source.id, raw.url)
            return ProcessItemResult(stored=False, reason="duplicate")
        if self._is_bot_challenge_page(normalized):
            logger.info(
                "bot challenge page filtered out %s (source=%s)",
                normalized.canonical_url,
                source.name,
            )
            return ProcessItemResult(stored=False, reason="bot_challenge")
        if self._is_legacy_page_monitor_item(source, raw):
            gate_result = self._legacy_page_quality_gate_result(source, normalized)
            if gate_result is not None:
                return gate_result
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
                return ProcessItemResult(stored=False, reason="relevance")
        try:
            fields = self._enrich(normalized)
        except Exception:
            logger.exception("enrich failed for %s", normalized.canonical_url)
            return ProcessItemResult(stored=False, reason="enrich_failed")
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
            return ProcessItemResult(
                stored=False,
                reason="enrich_reject",
                detail=fields.reject_reason or "unknown",
            )
        fields = self._apply_category_constraints(source, fields)
        try:
            self._repo.save_enriched(normalized, fields)
        except IntegrityError:
            self._session.rollback()
            if self._repo.exists_by_canonical(normalized.canonical_url):
                self._repo.merge_source_link(normalized.canonical_url, source.id, raw.url)
                return ProcessItemResult(stored=False, reason="duplicate", detail="integrity_conflict")
            logger.exception("save failed for %s", normalized.canonical_url)
            return ProcessItemResult(stored=False, reason="save_failed", detail="integrity_conflict")
        return ProcessItemResult(stored=True, reason="stored")

    def _enrich(self, item: NormalizedItem) -> EnrichedFields:
        try:
            return self._enricher.enrich(
                item,
                existing_tags=self._repo.list_existing_sub_tags(),
            )
        except TypeError:
            return self._enricher.enrich(item)

    def _retry_with_extracted_content(
        self,
        source: Source,
        raw: RawItem,
        original: NormalizedItem,
        rejected_fields: EnrichedFields,
    ) -> ProcessItemResult | None:
        if self._extractor is None:
            return None
        if not self._should_retry_with_extracted_content(raw, original, rejected_fields):
            return None

        try:
            doc = self._extractor.extract(raw.url)
        except Exception as exc:
            logger.info(
                "full-text retry extraction failed for %s (source=%s): %s",
                original.canonical_url,
                source.name,
                exc,
            )
            return None

        doc.published_at = doc.published_at or raw.published_at
        extracted = normalize(raw, doc)
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
        if self._is_bot_challenge_page(extracted):
            return ProcessItemResult(stored=False, reason="bot_challenge")

        try:
            fields = self._enrich(extracted)
        except Exception:
            logger.exception("full-text retry enrich failed for %s", extracted.canonical_url)
            return ProcessItemResult(stored=False, reason="enrich_failed", detail="full_text_retry")

        if not fields.should_store:
            logger.info(
                "full-text retry enricher rejected %s (source=%s, reason=%s)",
                extracted.canonical_url,
                source.name,
                fields.reject_reason or "unknown",
            )
            return ProcessItemResult(
                stored=False,
                reason="enrich_reject",
                detail=f"补抓正文后仍拒收：{fields.reject_reason or 'unknown'}",
            )

        fields = self._apply_category_constraints(source, fields)
        try:
            self._repo.save_enriched(extracted, fields)
        except IntegrityError:
            self._session.rollback()
            if self._repo.exists_by_canonical(extracted.canonical_url):
                self._repo.merge_source_link(extracted.canonical_url, source.id, raw.url)
                return ProcessItemResult(stored=False, reason="duplicate", detail="integrity_conflict")
            logger.exception("full-text retry save failed for %s", extracted.canonical_url)
            return ProcessItemResult(stored=False, reason="save_failed", detail="integrity_conflict")

        logger.info(
            "full-text retry stored %s (source=%s)",
            extracted.canonical_url,
            source.name,
        )
        return ProcessItemResult(stored=True, reason="stored")

    def _should_retry_with_extracted_content(
        self,
        raw: RawItem,
        item: NormalizedItem,
        fields: EnrichedFields,
    ) -> bool:
        reason = (fields.reject_reason or "").lower()
        if not any(marker.lower() in reason for marker in INCOMPLETE_CONTENT_RETRY_MARKERS):
            return False

        text = f"{item.title}\n{item.clean_content}\n{raw.raw_content or ''}".lower()
        return any(keyword in text for keyword in FULL_TEXT_RETRY_KEYWORDS)

    def _process_agent_item(self, source: Source, raw) -> ProcessItemResult:
        """处理 agent crawl 条目：跳过 LLM 富化，直接存储。

        Agent 条目已经由 SummaryWorkerPool 完成了摘要、关键词提取和重要性评估，
        Pipeline 只需做去重检查后直接写入。
        """
        if self._repo.exists_by_canonical(raw.url):
            self._repo.merge_source_link(raw.url, source.id, raw.url)
            return ProcessItemResult(stored=False, reason="duplicate")
        try:
            self._repo.save_agent_enriched(raw)
        except IntegrityError:
            self._session.rollback()
            if self._repo.exists_by_canonical(raw.url):
                self._repo.merge_source_link(raw.url, source.id, raw.url)
                return ProcessItemResult(stored=False, reason="duplicate", detail="integrity_conflict")
            logger.exception("agent item save failed for %s", raw.url)
            return ProcessItemResult(stored=False, reason="save_failed", detail="integrity_conflict")
        return ProcessItemResult(stored=True, reason="stored")

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

    def _legacy_page_quality_gate_result(
        self,
        source: Source,
        item: NormalizedItem,
    ) -> ProcessItemResult | None:
        if len(item.clean_content) < MIN_CONTENT_LEN:
            logger.info(
                "legacy page_monitor skipped %s (source=%s, reason=short_content, len=%s)",
                item.canonical_url,
                source.name,
                len(item.clean_content),
            )
            return ProcessItemResult(
                stored=False,
                reason="legacy_page_short_content",
                detail=f"len={len(item.clean_content)} < {MIN_CONTENT_LEN}",
            )
        if item.published_at is None:
            logger.info(
                "legacy page_monitor skipped %s (source=%s, reason=missing_published_at)",
                item.canonical_url,
                source.name,
            )
            return ProcessItemResult(stored=False, reason="legacy_page_missing_published_at")
        return None
