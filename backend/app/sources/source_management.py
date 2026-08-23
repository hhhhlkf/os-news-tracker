"""Source creation and probe persistence for the news-source catalog.

The module owns the source-specific decisions that HTTP handlers previously
mixed with request parsing: validating categories, resolving source types,
discovering page-monitor selectors, and persisting API probes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.categories import get_main_category_names
from app.enums import SourceType, Stream
from app.models import Source
from app.sources.detector import DetectResult, SourceDetectionError, detect_source
from app.sources.html_list_discovery import discover_html_list_source


class UnknownMainCategoryError(ValueError):
    """Raised when a source is assigned to a category not in the catalog."""


class HtmlListDiscoveryError(ValueError):
    """Raised when a page-monitor source has no usable news-list selectors."""


class MissingApiUrlError(ValueError):
    """Raised when an API probe source is missing its endpoint URL."""


@dataclass(frozen=True)
class SourceCreateInput:
    url: str
    name: str | None
    main_category: str
    source_type: str | None
    adapter: str | None
    api_config: dict[str, Any] | None
    link_selector: str | None
    title_selector: str | None
    date_selector: str | None


@dataclass(frozen=True)
class ApiProbeSourceInput:
    api_url: str
    method: str
    items_path: str
    fields: dict[str, Any]
    pagination: dict[str, Any] | None
    json_body: dict[str, Any] | None
    name: str
    main_category: str


_CONFIGURABLE_SOURCE_TYPES = frozenset(
    {
        SourceType.RSS.value,
        SourceType.API.value,
        SourceType.PAGE_MONITOR.value,
        SourceType.SEARCH.value,
    }
)


def detect_source_shape(url: str) -> DetectResult:
    """Detect a source's fetch shape, preserving detector error semantics."""
    return detect_source(url)


def validate_main_category(db: Session, main_category: str) -> None:
    """Require a category that exists in the current category catalog."""
    if main_category not in get_main_category_names(db):
        raise UnknownMainCategoryError(main_category)


def default_main_category(db: Session) -> str | None:
    """Return the first configured category for discovery-created sources."""
    category_names = get_main_category_names(db)
    return category_names[0] if category_names else None


def create_news_source(db: Session, source_input: SourceCreateInput) -> Source:
    """Resolve and persist a user-created news source.

    An explicitly supported type keeps the submitted configuration; all other
    type values trigger automatic source detection, matching the legacy route.
    """
    validate_main_category(db, source_input.main_category)
    source_type, api_config, name = _resolve_source_details(source_input)
    link_selector, title_selector, date_selector = _resolve_page_monitor_selectors(
        source_input,
        source_type,
    )

    source = Source(
        name=name,
        type=source_type,
        url=source_input.url,
        api_config=api_config,
        adapter=source_input.adapter,
        main_category=source_input.main_category,
        link_selector=link_selector,
        title_selector=title_selector,
        date_selector=date_selector,
        stream=Stream.NEWS,
        enabled=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def upsert_api_source_from_probe(db: Session, source_input: ApiProbeSourceInput) -> Source:
    """Persist a discovered API probe, replacing an existing source by URL."""
    validate_api_probe_source_input(db, source_input)
    probe = _build_probe(source_input)
    existing = db.scalar(
        select(Source).where(
            Source.type == SourceType.API.value,
            Source.url == source_input.api_url,
        )
    )
    if existing is not None:
        existing.api_config = {"probe": probe}
        if source_input.name:
            existing.name = source_input.name
        existing.main_category = source_input.main_category
        existing.enabled = True
        db.commit()
        db.refresh(existing)
        return existing

    source = Source(
        name=source_input.name or source_input.api_url,
        type=SourceType.API.value,
        url=source_input.api_url,
        api_config={"probe": probe},
        main_category=source_input.main_category,
        stream=Stream.NEWS,
        enabled=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def validate_api_probe_source_input(db: Session, source_input: ApiProbeSourceInput) -> None:
    """Validate an API probe input in the legacy category-then-URL order."""
    validate_main_category(db, source_input.main_category)
    if not source_input.api_url:
        raise MissingApiUrlError()


def _resolve_source_details(
    source_input: SourceCreateInput,
) -> tuple[str, dict[str, Any] | None, str]:
    if source_input.source_type in _CONFIGURABLE_SOURCE_TYPES:
        return (
            source_input.source_type,
            source_input.api_config,
            (source_input.name or "").strip() or source_input.url,
        )

    detected = detect_source_shape(source_input.url)
    return (
        detected.detected_type,
        detected.api_config,
        (source_input.name or detected.name_suggestion).strip(),
    )


def _resolve_page_monitor_selectors(
    source_input: SourceCreateInput,
    source_type: str,
) -> tuple[str | None, str | None, str | None]:
    link_selector = source_input.link_selector
    title_selector = source_input.title_selector
    date_selector = source_input.date_selector
    if source_type != SourceType.PAGE_MONITOR.value or link_selector:
        return link_selector, title_selector, date_selector

    discovery = discover_html_list_source(source_input.url)
    if not discovery.success or not discovery.link_selector:
        raise HtmlListDiscoveryError(source_input.url)
    return discovery.link_selector, discovery.title_selector, discovery.date_selector


def _build_probe(source_input: ApiProbeSourceInput) -> dict[str, Any]:
    probe: dict[str, Any] = {
        "mode": "json_list",
        "method": source_input.method.upper() if source_input.method else "GET",
        "url": source_input.api_url,
        "items_path": source_input.items_path,
        "fields": source_input.fields,
    }
    if source_input.pagination:
        probe["pagination"] = source_input.pagination
    if source_input.json_body:
        probe["json_body"] = source_input.json_body
    return probe
