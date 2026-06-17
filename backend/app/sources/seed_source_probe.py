from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import SourceType
from app.extract.scrapling_extractor import ScraplingExtractor
from app.fetchers.api_adapters import ApiAdapterFetcher, supported_api_adapters
from app.fetchers.base import Fetcher
from app.fetchers.json_api import GenericJsonApiFetcher
from app.fetchers.page_monitor import PageMonitorFetcher
from app.fetchers.rss import RssFetcher
from app.fetchers.search import SearchFetcher
from app.llm.client import LlmClient
from app.models import Source
from app.schemas import RawItem
from app.search.base import get_search_provider
from app.sources.registry import _load_source_entries


class ProbeStatus(StrEnum):
    SUPPORTED = "supported"
    UNIMPLEMENTED = "adapter_unimplemented"
    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"


@dataclass(frozen=True)
class ProbeRoute:
    source_name: str
    source_type: str
    url: str
    status: ProbeStatus
    reason: str | None = None


@dataclass
class ProbeResult:
    source_name: str
    source_type: str
    url: str
    status: ProbeStatus
    items_count: int = 0
    sample_3: list[dict[str, Any]] = field(default_factory=list)
    validation_errors: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    elapsed_ms: float = 0.0


def probe_route_for_entry(entry: dict[str, Any]) -> ProbeRoute:
    source_type = str(entry.get("type", ""))
    adapter = entry.get("adapter")
    api_config = entry.get("api_config") if isinstance(entry.get("api_config"), dict) else {}
    has_probe_config = isinstance(api_config.get("probe"), dict)
    url = str(entry.get("url") or "")
    status = ProbeStatus.SUPPORTED
    reason: str | None = None

    if (
        source_type == SourceType.API
        and not has_probe_config
        and adapter != "generic_json_list"
        and adapter not in supported_api_adapters()
    ):
        status = ProbeStatus.UNIMPLEMENTED
        reason = f"api adapter {adapter!r} is not implemented for seed probing"
    elif source_type not in {
        SourceType.RSS,
        SourceType.PAGE_MONITOR,
        SourceType.SEARCH,
        SourceType.API,
    }:
        status = ProbeStatus.UNIMPLEMENTED
        reason = f"source type {source_type!r} is not implemented for seed probing"

    return ProbeRoute(
        source_name=str(entry["name"]),
        source_type=source_type,
        url=url,
        status=status,
        reason=reason,
    )


def probe_routes_for_manifest(path: str | Path) -> dict[str, ProbeRoute]:
    entries = _load_source_entries(Path(path))
    return {entry["name"]: probe_route_for_entry(entry) for entry in entries}


def build_probe_fetcher(
    source: Source,
    *,
    extractor: Any | None = None,
    search: Any | None = None,
) -> Fetcher | None:
    if source.type == SourceType.RSS:
        return RssFetcher()
    if source.type == SourceType.API and (source.api_config or {}).get("probe"):
        return ApiAdapterFetcher()
    if source.type == SourceType.API and source.adapter == "generic_json_list":
        return GenericJsonApiFetcher()
    if source.type == SourceType.API and source.adapter in supported_api_adapters():
        return ApiAdapterFetcher()
    if source.type == SourceType.PAGE_MONITOR:
        return PageMonitorFetcher(
            extractor=extractor or ScraplingExtractor(use_stealth=source.stealth)
        )
    if source.type == SourceType.SEARCH:
        return SearchFetcher(
            search=search or get_search_provider(),
            extractor=extractor or ScraplingExtractor(use_stealth=source.stealth),
        )
    return None


def validate_sample_items(
    items: list[RawItem], *, limit: int = 3
) -> tuple[list[RawItem], dict[str, int]]:
    errors: Counter[str] = Counter()
    seen_urls: set[str] = set()
    sample: list[RawItem] = []

    for item in items:
        title = item.title.strip() if item.title else ""
        url = item.url.strip() if item.url else ""
        content = item.raw_content.strip() if item.raw_content else ""

        if not title:
            errors["empty_title"] += 1
            continue
        if not url:
            errors["empty_url"] += 1
            continue
        if url in seen_urls:
            errors["duplicate_url"] += 1
            continue
        seen_urls.add(url)
        if not content:
            errors["empty_content"] += 1
            continue
        if item.published_at is not None and not isinstance(item.published_at, datetime):
            errors["invalid_date"] += 1
            continue

        if len(sample) < limit:
            sample.append(item)

    return sample, dict(errors)


def probe_source_live(source: Source) -> ProbeResult:
    fetcher = build_probe_fetcher(source)
    if fetcher is None:
        return ProbeResult(
            source_name=source.name,
            source_type=source.type,
            url=source.url,
            status=ProbeStatus.UNIMPLEMENTED,
            error="current adapter is not implemented",
        )

    started_at = time.monotonic()
    try:
        items = fetcher.fetch(source)
    except Exception as exc:
        return ProbeResult(
            source_name=source.name,
            source_type=source.type,
            url=source.url,
            status=ProbeStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=(time.monotonic() - started_at) * 1000,
        )

    sample, validation_errors = validate_sample_items(items)
    return ProbeResult(
        source_name=source.name,
        source_type=source.type,
        url=source.url,
        status=ProbeStatus.OK if sample else ProbeStatus.EMPTY,
        items_count=len(items),
        sample_3=[_item_to_report(item) for item in sample],
        validation_errors=validation_errors,
        elapsed_ms=(time.monotonic() - started_at) * 1000,
    )


def probe_all_seed_sources_live(session: Session) -> list[ProbeResult]:
    sources = session.scalars(select(Source).order_by(Source.main_category, Source.name)).all()
    return [probe_source_live(source) for source in sources]


def summarize_probe_results_with_llm(
    results: list[ProbeResult], *, client: LlmClient | None = None
) -> str:
    payload = [result_to_dict(result) for result in results]
    prompt = (
        "你是 OS 技术新闻源健康检查助手。请根据以下 seed source live smoke "
        "结构化结果，用中文总结整体健康状况、主要失败模式、需要优先修复的 "
        "adapter/source，并给出下一步建议。保持简洁。\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    return (client or LlmClient()).complete(prompt, temperature=0.1)


def result_to_dict(result: ProbeResult) -> dict[str, Any]:
    return {
        "source_name": result.source_name,
        "type": result.source_type,
        "url": result.url,
        "status": result.status,
        "items_count": result.items_count,
        "sample_3": result.sample_3,
        "validation_errors": result.validation_errors,
        "error": result.error,
        "elapsed_ms": round(result.elapsed_ms, 1),
    }


def _item_to_report(item: RawItem) -> dict[str, Any]:
    return {
        "title": item.title,
        "url": item.url,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "content_chars": len(item.raw_content or ""),
    }
