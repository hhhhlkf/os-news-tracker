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
    enforce_fixed_listing_page: bool = True,
    enforce_text_coverage: bool = True,
    time_semantics: str = "publication",
    snapshot_observed_at: list[str] | None = None,
) -> EvaluationResult:
    """Apply every section-9 mechanical rule; no Agent verdict is accepted."""
    checks: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if len(outputs) != 2:
        _record(checks, failures, "independent_runs", False, actual=len(outputs), required=2)
        return EvaluationResult(False, tuple(checks), tuple(failures), ())

    snapshot_times = snapshot_observed_at or []
    if time_semantics not in {"publication", "snapshot"}:
        _record(checks, failures, "time_semantics", False, actual=time_semantics)
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
        host_snapshot_time = snapshot_times[run_number - 1] if run_number <= len(snapshot_times) else None
        snapshot_time_valid = _utc_datetime(host_snapshot_time)
        for index, item in enumerate(items):
            if time_semantics == "snapshot":
                if item.published_at is not None:
                    invalid_dates.append({"index": index, "value": item.published_at, "error": "snapshot items must leave published_at null"})
                elif not snapshot_time_valid:
                    invalid_dates.append({"index": index, "value": None, "error": "missing host-attested snapshot observation time"})
                continue
            try:
                if not item.published_at:
                    raise ValueError("missing")
                parsed = isoparse(item.published_at)
                if not isinstance(parsed, datetime):
                    raise ValueError("not datetime")
            except (TypeError, ValueError, OverflowError) as exc:
                invalid_dates.append({"index": index, "value": item.published_at, "error": str(exc)[:200]})
        date_evidence: dict[str, Any] = {}
        if time_semantics == "snapshot":
            date_evidence = {
                "time_semantics": time_semantics,
                "host_snapshot_observed_at": host_snapshot_time,
            }
        _record(checks, failures, f"{prefix}.published_at", not invalid_dates,
                invalid_samples=invalid_dates[:10], parse_rate=_rate(total - len(invalid_dates), total), required=1.0,
                **date_evidence)

        missing_text = [
            index for index, item in enumerate(items)
            if not ((item.content or "").strip() or (item.summary or "").strip())
        ]
        _record(checks, failures, f"{prefix}.content_or_summary", not missing_text,
                invalid_indexes=missing_text[:20], valid_rate=_rate(total - len(missing_text), total), required=1.0)

        if enforce_text_coverage and time_semantics == "publication":
            substantial_text = [
                index
                for index, item in enumerate(items)
                if max(len((item.content or "").strip()), len((item.summary or "").strip())) >= 200
            ]
            _record(
                checks,
                failures,
                f"{prefix}.text_coverage",
                _rate(len(substantial_text), total) >= 0.8,
                at_least_200=len(substantial_text),
                total=total,
                rate=_rate(len(substantial_text), total),
                required_rate=0.8,
                short_indexes=[index for index in range(total) if index not in substantial_text][:20],
                repair_hint="At least 80% of items need cleaned content or summary with 200 or more characters.",
            )
        elif enforce_text_coverage:
            checks.append({
                "check": f"{prefix}.text_coverage",
                "passed": True,
                "applicable": False,
                "note": "snapshot collections retain a non-empty item summary but are not article bodies",
            })

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

    first_urls = {item.url for item in outputs[0].items}
    next_urls = {item.url for item in pagination_output.items} if pagination_output else set()
    new_urls = sorted(next_urls - first_urls)
    if supports_pagination:
        _record(checks, failures, "pagination_new_items", bool(new_urls),
                next_page_items=len(next_urls), new_count=len(new_urls), new_samples=new_urls[:10])
    elif enforce_fixed_listing_page:
        _record(
            checks,
            failures,
            "pagination_fixed_listing",
            pagination_output is not None and not next_urls,
            next_page_items=len(next_urls),
            new_count=len(new_urls),
            next_samples=sorted(next_urls)[:10],
            repair_hint=(
                "A connector that declares no pagination must return no items for config.page=2; "
                "returning the first page again is not a fixed listing."
            ),
        )
    else:
        checks.append({"check": "pagination_new_items", "passed": True, "applicable": False})

    _record(checks, failures, "independent_runs", True, actual=2, required=2)
    return EvaluationResult(not failures, tuple(checks), tuple(failures), sample_urls)


def evaluate_connector_field_smoke(
    output: ConnectorOutput,
    *,
    time_semantics: str = "publication",
    snapshot_observed_at: str | None = None,
    requested_target_count: int = 1,
) -> EvaluationResult:
    """Check one real output before paying for independent certification trials."""
    checks: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    items = output.items
    _record(
        checks,
        failures,
        "smoke.nonempty_items",
        bool(items),
        actual=len(items),
        required=1,
        stats=output.stats.model_dump(mode="json"),
        request_contract={
            "entry": "request['entry']",
            "pagination": "request.get('config', {}).get('page', 1)",
        },
        repair_hint=(
            "Connector returned zero items without an execution error. Use the record-level Explore evidence; "
            "preserve list title/url/date or summary when a detail request fails, and do not swallow fetch, "
            "status-code, or parser errors into an empty result."
        ),
    )
    if not items:
        diagnostics = _bounded_stats_diagnostics(output.stats.model_dump(mode="json"))
        _record(
            checks,
            failures,
            "smoke.empty_output_diagnostics",
            diagnostics is not None,
            diagnostics=diagnostics,
            repair_hint=(
                "When returning zero items, stats must expose candidate_count, rejected_count, and bounded "
                "rejection_reasons so Repair can distinguish a selector mismatch from item-level rejection."
            ),
        )
        if diagnostics is not None:
            candidate_count = diagnostics["candidate_count"]
            rejected_count = diagnostics["rejected_count"]
            stopped_before_exhaustion = (
                candidate_count > requested_target_count
                and 0 < rejected_count <= requested_target_count
                and rejected_count < candidate_count
            )
            if stopped_before_exhaustion:
                _record(
                    checks,
                    failures,
                    "smoke.candidate_processing_order",
                    False,
                    candidate_count=candidate_count,
                    rejected_count=rejected_count,
                    requested_target_count=requested_target_count,
                    diagnosis=(
                        "The connector discovered more candidates than it processed before returning zero valid items."
                    ),
                    repair_hint=(
                        "Treat target_count as a limit on final valid output, not as a pre-validation candidate slice. "
                        "Complete and validate bounded candidates in order, continue after an invalid early candidate, "
                        "and stop only when target_count valid items are ready or the candidate set is exhausted."
                    ),
                )
    invalid_titles = [index for index, item in enumerate(items) if not item.title.strip()]
    _record(checks, failures, "smoke.titles", not invalid_titles, invalid_indexes=invalid_titles[:10])
    invalid_urls = [index for index, item in enumerate(items) if not _absolute_http_url(item.url)]
    _record(checks, failures, "smoke.absolute_urls", not invalid_urls, invalid_indexes=invalid_urls[:10])
    invalid_dates: list[dict[str, Any]] = []
    snapshot_time_valid = _utc_datetime(snapshot_observed_at)
    for index, item in enumerate(items):
        if time_semantics == "snapshot":
            if item.published_at is not None:
                invalid_dates.append({"index": index, "value": item.published_at, "error": "snapshot items must leave published_at null"})
            elif not snapshot_time_valid:
                invalid_dates.append({"index": index, "value": None, "error": "missing host-attested snapshot observation time"})
            continue
        try:
            if not item.published_at:
                raise ValueError("missing")
            if not isinstance(isoparse(item.published_at), datetime):
                raise ValueError("not datetime")
        except (TypeError, ValueError, OverflowError) as exc:
            invalid_dates.append({"index": index, "value": item.published_at, "error": str(exc)[:200]})
    _record(
        checks,
        failures,
        "smoke.published_at",
        not invalid_dates,
        invalid_samples=invalid_dates[:10],
        time_semantics=time_semantics,
        host_snapshot_observed_at=snapshot_observed_at if time_semantics == "snapshot" else None,
    )
    missing_text = [index for index, item in enumerate(items) if not ((item.content or "").strip() or (item.summary or "").strip())]
    _record(checks, failures, "smoke.content_or_summary", not missing_text, invalid_indexes=missing_text[:10])
    return EvaluationResult(not failures, tuple(checks), tuple(failures), tuple(item.url for item in items[:10]))


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


def _utc_datetime(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = isoparse(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return isinstance(parsed, datetime) and parsed.tzinfo is not None and parsed.utcoffset() is not None


def _bounded_stats_diagnostics(stats: dict[str, Any]) -> dict[str, Any] | None:
    candidate_count = stats.get("candidate_count")
    rejected_count = stats.get("rejected_count")
    reasons = stats.get("rejection_reasons")
    if (
        isinstance(candidate_count, bool) or not isinstance(candidate_count, int) or candidate_count < 0
        or isinstance(rejected_count, bool) or not isinstance(rejected_count, int) or rejected_count < 0
        or not isinstance(reasons, dict)
    ):
        return None
    bounded_reasons = {
        str(reason)[:120]: count
        for reason, count in list(reasons.items())[:20]
        if isinstance(reason, str) and isinstance(count, int) and not isinstance(count, bool) and count >= 0
    }
    if len(bounded_reasons) != len(reasons):
        return None
    return {
        "candidate_count": candidate_count,
        "rejected_count": rejected_count,
        "rejection_reasons": bounded_reasons,
    }
