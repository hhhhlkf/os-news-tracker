"""Deterministic acceptance gate for generated website connectors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from dateutil.parser import isoparse

from app.discovery.plugin.contracts import ConnectorOutput


@dataclass(frozen=True)
class EvaluationResult:
    passed: bool
    checks: tuple[dict[str, Any], ...]
    failures: tuple[dict[str, Any], ...]
    sample_urls: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": list(self.checks),
            "failures": list(self.failures),
            "sample_urls": list(self.sample_urls),
        }


def evaluate_connector_outputs(
    outputs: list[ConnectorOutput],
    *,
    reachable_urls: set[str],
    supports_pagination: bool,
    pagination_output: ConnectorOutput | None,
    require_url_accessibility: bool = True,
) -> EvaluationResult:
    """Apply every section-9 mechanical rule; no Agent verdict is accepted."""
    checks: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if len(outputs) != 2:
        _record(checks, failures, "independent_runs", False, actual=len(outputs), required=2)
        return EvaluationResult(False, tuple(checks), tuple(failures), ())

    for run_number, output in enumerate(outputs, start=1):
        items = output.items
        total = len(items)
        prefix = f"run_{run_number}"
        if total == 0:
            _record(
                checks,
                failures,
                f"{prefix}.empty_output_diagnostic",
                False,
                stage="sandbox_output",
                trial_run=run_number,
                stats=output.stats.model_dump(mode="json"),
                request_contract={
                    "entry": "request['entry']",
                    "pagination": "request.get('config', {}).get('page', 1)",
                },
                repair_hint=(
                    "Connector returned zero items without an execution error. Verify that it reads "
                    "request['entry']; do not read request['site_url'] or request['url']; do not swallow "
                    "missing-entry, HTTP, status-code, or parser exceptions into an empty result."
                ),
            )
        _record(checks, failures, f"{prefix}.minimum_items", total >= 5, actual=total, required=5,
                note="低频站点只能由人工例外审核，Loop 不自动豁免。")

        invalid_titles = [index for index, item in enumerate(items) if not item.title.strip()]
        _record(checks, failures, f"{prefix}.titles", not invalid_titles,
                invalid_indexes=invalid_titles[:20], valid_rate=_rate(total - len(invalid_titles), total), required=1.0)

        invalid_urls = [
            {"index": index, "url": item.url[:500]}
            for index, item in enumerate(items)
            if not _absolute_http_url(item.url)
        ]
        _record(checks, failures, f"{prefix}.absolute_urls", not invalid_urls,
                invalid_samples=invalid_urls[:10], valid_rate=_rate(total - len(invalid_urls), total), required=1.0)

        invalid_dates: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            try:
                if not item.published_at:
                    raise ValueError("missing")
                parsed = isoparse(item.published_at)
                if not isinstance(parsed, datetime):
                    raise ValueError("not datetime")
            except (TypeError, ValueError, OverflowError) as exc:
                invalid_dates.append({"index": index, "value": item.published_at, "error": str(exc)[:200]})
        _record(checks, failures, f"{prefix}.published_at", not invalid_dates,
                invalid_samples=invalid_dates[:10], parse_rate=_rate(total - len(invalid_dates), total), required=1.0)

        missing_text = [
            index for index, item in enumerate(items)
            if not ((item.content or "").strip() or (item.summary or "").strip())
        ]
        _record(checks, failures, f"{prefix}.content_or_summary", not missing_text,
                invalid_indexes=missing_text[:20], valid_rate=_rate(total - len(missing_text), total), required=1.0)

        unique_urls = len({item.url for item in items if item.url})
        _record(checks, failures, f"{prefix}.url_deduplication", total > 0 and unique_urls / total >= 0.9,
                unique=unique_urls, total=total, rate=_rate(unique_urls, total), required=0.9)

    sample_urls = tuple(dict.fromkeys(item.url for output in outputs for item in output.items))[:10]
    if require_url_accessibility:
        inaccessible = [url for url in sample_urls if url not in reachable_urls]
        _record(checks, failures, "sample_url_accessibility", bool(sample_urls) and not inaccessible,
                sampled=len(sample_urls), reachable=len(sample_urls) - len(inaccessible),
                inaccessible=inaccessible, required_rate=1.0)
    else:
        checks.append({
            "check": "sample_url_accessibility",
            "passed": True,
            "applicable": False,
            "note": "summary-only source validates the public search result, not the article body URL",
        })

    if supports_pagination:
        first_urls = {item.url for item in outputs[0].items}
        next_urls = {item.url for item in pagination_output.items} if pagination_output else set()
        new_urls = sorted(next_urls - first_urls)
        _record(checks, failures, "pagination_new_items", bool(new_urls),
                next_page_items=len(next_urls), new_count=len(new_urls), new_samples=new_urls[:10])
    else:
        checks.append({"check": "pagination_new_items", "passed": True, "applicable": False})

    _record(checks, failures, "independent_runs", True, actual=2, required=2)
    return EvaluationResult(not failures, tuple(checks), tuple(failures), sample_urls)


def _record(
    checks: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    name: str,
    passed: bool,
    **evidence: Any,
) -> None:
    result = {"check": name, "passed": passed, **evidence}
    checks.append(result)
    if not passed:
        failures.append(result)


def _absolute_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not (
        parsed.username or parsed.password
    )


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0
