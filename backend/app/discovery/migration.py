"""Per-site, no-ingestion migration from legacy Discovery DSLs to plugins.

The legacy method remains the published mapping until a reviewed plugin has
real gVisor shadow evidence, a deterministic comparison, and an explicit
system-admin acceptance.  Formal execution never dual-ingests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import logging
import multiprocessing as mp
import os
from queue import Empty
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.discovery.plugin.contracts import ConnectorOutput
from app.discovery.redaction import (
    redact_discovery_data,
    redact_discovery_text,
    validate_discovery_data_bounds,
)
from app.models import (
    CrawlMethod,
    CrawlMethodDomain,
    CrawlMethodRun,
    DiscoveryMethodMigration,
    DiscoveryMigrationComparison,
    MorningCrawlRunMethod,
    SiteDiscoveryRun,
    Source,
)

logger = logging.getLogger(__name__)

ROLLBACK_DAYS = 7
REQUIRED_FORMAL_SUCCESSES = 3
MAX_SHADOW_ATTEMPTS_PER_MIGRATION = 3
MIGRATABLE_SOURCE_KINDS = {"website", "wechat", "wechat_search", "wechat_history"}
ACTIVE_MIGRATION_STATES = {
    "shadow_pending", "shadow_running", "shadow_passed", "blocked", "ready", "cutover", "eligible",
}
_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "igshid", "yclid", "ref_src",
}
LEGACY_SHADOW_TIMEOUT_SECONDS = 120.0
# A live backend PID is only supporting evidence once its DB heartbeat is
# stale.  Ten minutes covers the bounded legacy child, repeated sandbox
# evaluations, and cleanup, while preventing a dead worker thread (or a PID
# collision across hosts) from retaining the unique comparison slot forever.
SHADOW_COMPARISON_HARD_LEASE_SECONDS = 10 * 60
# Capacity is eight with two workers: a legitimate tail job can wait through
# three preceding 10-minute waves.  Forty-five minutes covers that 30-minute
# queue plus scheduler/claim margin without conflating queued and running
# ownership leases.
SHADOW_COMPARISON_QUEUED_LEASE_SECONDS = 45 * 60
_SHADOW_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="migration-shadow")
_SHADOW_CAPACITY = BoundedSemaphore(8)
_SHADOW_JOB_LOCK = Lock()
_SHADOW_CANCEL_EVENTS: dict[int, Event] = {}
_LEGACY_SHADOW_PROCESSES: dict[int, Any] = {}


class UnsupportedLegacyShadow(RuntimeError):
    """Legacy recipe cannot be safely executed as migration-only evidence."""


class LegacyCleanupNotReady(RuntimeError):
    """Website/WeChat legacy runtime cannot yet be removed safely."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _legacy_source_kind(method: CrawlMethod) -> str | None:
    recipe = method.dsl_recipe or {}
    recipe_type = recipe.get("recipe_type")
    if recipe_type == "dsl":
        return "website"
    if recipe_type != "multi_dsl":
        return None
    kind = str(recipe.get("source_kind") or "website")
    if kind == "internal_forum":
        return None
    return kind if kind in MIGRATABLE_SOURCE_KINDS else None


def legacy_cleanup_readiness(db: Session) -> dict[str, Any]:
    """Return the fail-closed global gate for deleting legacy interpreters.

    Generation code can be removed once connector discovery owns all new
    requests.  The interpreter and stored recipes are a separate irreversible
    cleanup: every ordinary-site/WeChat legacy method must first be retired
    through its per-site 7-day/3-success migration.
    """
    legacy_methods = [
        method
        for method in db.scalars(select(CrawlMethod).order_by(CrawlMethod.id))
        if _legacy_source_kind(method) is not None
    ]
    legacy_ids = {method.id for method in legacy_methods}
    mapped_legacy_ids = set()
    if legacy_ids:
        mapped_legacy_ids = set(
            db.scalars(
                select(CrawlMethodDomain.method_id).where(
                    CrawlMethodDomain.method_id.in_(legacy_ids)
                )
            )
        )
    blocking_migrations = list(
        db.scalars(
            select(DiscoveryMethodMigration.id)
            .where(
                (DiscoveryMethodMigration.legacy_method_id.is_not(None))
                | (DiscoveryMethodMigration.state.in_(ACTIVE_MIGRATION_STATES))
            )
            .order_by(DiscoveryMethodMigration.id)
        )
    )
    historical_migrations = list(
        db.scalars(
            select(DiscoveryMethodMigration.id)
            .where(
                DiscoveryMethodMigration.legacy_method_id.is_(None),
                DiscoveryMethodMigration.state.not_in(ACTIVE_MIGRATION_STATES),
            )
            .order_by(DiscoveryMethodMigration.id)
        )
    )
    active_legacy_runs: list[int] = []
    if legacy_ids:
        active_legacy_runs = list(
            db.scalars(
                select(CrawlMethodRun.id)
                .where(
                    CrawlMethodRun.method_id.in_(legacy_ids),
                    CrawlMethodRun.status == "running",
                )
                .order_by(CrawlMethodRun.id)
            )
        )
    active_comparisons = list(
        db.scalars(
            select(DiscoveryMigrationComparison.id)
            .where(DiscoveryMigrationComparison.status.in_(("queued", "running")))
            .order_by(DiscoveryMigrationComparison.id)
        )
    )
    blockers = {
        "legacy_method_count": len(legacy_methods),
        "mapped_legacy_method_count": len(mapped_legacy_ids),
        "blocking_migration_count": len(blocking_migrations),
        "active_legacy_run_count": len(active_legacy_runs),
        "active_comparison_count": len(active_comparisons),
    }
    return {
        "ready": all(value == 0 for value in blockers.values()),
        "blockers": blockers,
        "legacy_method_ids": sorted(legacy_ids)[:100],
        "blocking_migration_ids": blocking_migrations[:100],
        "active_legacy_run_ids": active_legacy_runs[:100],
        "active_comparison_ids": active_comparisons[:100],
        "historical_migration_count": len(historical_migrations),
        "historical_migration_ids": historical_migrations[:100],
        "requirements": {
            "rollback_days": ROLLBACK_DAYS,
            "formal_successes": REQUIRED_FORMAL_SUCCESSES,
        },
    }


def assert_legacy_cleanup_ready(db: Session) -> None:
    """Refuse interpreter/artifact cleanup until every migration is retired."""
    readiness = legacy_cleanup_readiness(db)
    if not readiness["ready"]:
        blockers = readiness["blockers"]
        raise LegacyCleanupNotReady(
            "legacy cleanup is blocked: "
            + ", ".join(f"{key}={value}" for key, value in blockers.items() if value)
        )


def assert_formal_method_current(db: Session, method: CrawlMethod) -> None:
    """Single gate for every formal caller; only internal-forum keeps its legacy seam."""
    if method.review_status != "approved" or method.status != "active":
        raise RuntimeError("crawl method is not approved and active")
    recipe = method.dsl_recipe or {}
    if recipe.get("recipe_type") == "multi_dsl" and recipe.get("source_kind") == "internal_forum":
        return
    mapping = db.scalar(select(CrawlMethodDomain).where(CrawlMethodDomain.domain == method.domain))
    if mapping is None and recipe.get("recipe_type") in {"dsl", "multi_dsl"}:
        # Old deployments may predate domain mappings. This seam is only for
        # already-persisted methods and closes when migration registration
        # creates a domain record; new Discovery cannot generate a DSL.
        migration_exists = db.scalar(select(DiscoveryMethodMigration.id).where(
            DiscoveryMethodMigration.domain == method.domain,
        ).limit(1))
        if migration_exists is None:
            return
    if mapping is None or mapping.method_id != method.id:
        raise RuntimeError("crawl method is not the current published domain mapping")


def _legacy_snapshot(method: CrawlMethod) -> dict[str, Any]:
    """Keep audit identity, never copy a possibly credential-bearing DSL."""
    recipe = method.dsl_recipe or {}
    return {
        "method_id": method.id,
        "source_id": method.source_id,
        "domain": method.domain,
        "entry_url": _public_url(method.entry_url),
        "signature": method.signature,
        "recipe_type": recipe.get("recipe_type"),
        "source_kind": recipe.get("source_kind"),
    }


def _bounded_redacted_text(value: str, *, field: str, max_length: int = 4000) -> str:
    text = redact_discovery_text(" ".join(value.strip().split()))
    validate_discovery_data_bounds(
        text,
        max_string_chars=max_length,
        max_approx_bytes=max_length * 4,
    )
    if len(text) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    return text


def _normalized_netloc(host: str, port: int | None, scheme: str) -> str:
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return display_host
    return f"{display_host}:{port}"


def _public_url(value: str) -> str:
    """Project a URL without userinfo or credential-bearing query values."""
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = parts.port
        netloc = _normalized_netloc(host, port, parts.scheme.lower())
        query = redact_discovery_text(f"?{parts.query}")[1:] if parts.query else ""
        return urlunsplit((parts.scheme.lower(), netloc, parts.path, query, ""))
    except (UnicodeError, ValueError):
        return redact_discovery_text(value).split("#", 1)[0][:2000]


def normalize_comparison_url(value: str) -> str:
    """Deterministic comparison key; preserve functional and signed params."""
    try:
        parts = urlsplit(value.strip())
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError):
        return value.strip()
    netloc = _normalized_netloc(host, port, scheme)
    kept_query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not (key.lower().startswith("utm_") or key.lower() in _TRACKING_QUERY_KEYS)
    ]
    kept_query.sort(key=lambda pair: (pair[0], pair[1]))
    # Do not decode/re-encode paths or collapse trailing slashes: either can
    # change signed URLs or distinct application routes.
    return urlunsplit((scheme, netloc, parts.path or "/", urlencode(kept_query, doseq=True), ""))


def prepare_plugin_migration_on_approval(
    db: Session,
    plugin: CrawlMethod,
) -> DiscoveryMethodMigration | None:
    """Register a replacement without publishing it over the legacy mapping."""
    if (plugin.dsl_recipe or {}).get("recipe_type") != "python_plugin":
        return None
    existing = db.scalar(
        select(DiscoveryMethodMigration)
        .where(DiscoveryMethodMigration.plugin_method_id == plugin.id)
        .order_by(DiscoveryMethodMigration.id.desc())
        .with_for_update()
    )
    if existing is not None:
        plugin.status = "active" if existing.state in {"cutover", "eligible", "retired"} else "inactive"
        return existing
    active_domain = db.scalar(select(DiscoveryMethodMigration).where(
        DiscoveryMethodMigration.domain == plugin.domain,
        DiscoveryMethodMigration.state.in_(ACTIVE_MIGRATION_STATES),
    ).order_by(DiscoveryMethodMigration.id.desc()).with_for_update())
    if active_domain is not None:
        raise ValueError("domain already has an active migration candidate")
    mapping = db.scalar(
        select(CrawlMethodDomain)
        .where(CrawlMethodDomain.domain == plugin.domain)
        .with_for_update()
    )
    if mapping is None or mapping.method_id == plugin.id:
        return None
    legacy = db.scalar(
        select(CrawlMethod).where(CrawlMethod.id == mapping.method_id).with_for_update()
    )
    if legacy is None:
        raise ValueError("published legacy migration method is missing")
    source_kind = _legacy_source_kind(legacy)
    if source_kind is None:
        return None
    if legacy.domain != plugin.domain:
        raise ValueError("legacy and plugin migration domains differ")

    # Preserve the existing Source identity, group bindings, and ingestion history.
    staging_source_id = plugin.source_id
    plugin.source_id = legacy.source_id
    plugin.status = "inactive"
    legacy.status = "active"
    migration = DiscoveryMethodMigration(
        domain=plugin.domain,
        source_kind=source_kind,
        legacy_method_id=legacy.id,
        plugin_method_id=plugin.id,
        state="shadow_pending",
        legacy_snapshot=_legacy_snapshot(legacy),
    )
    db.add(migration)
    db.flush()
    if staging_source_id != legacy.source_id:
        staging = db.get(Source, staging_source_id)
        if staging is not None:
            staging.enabled = False
    return migration


def register_migration(
    db: Session,
    *,
    legacy_method_id: int,
    plugin_method_id: int,
) -> DiscoveryMethodMigration:
    from app.discovery.domain_transition import domain_transition_lock

    # Identifying reads take no row lock.  All migration writers first enter
    # the domain protocol, then lock mapping -> migration -> methods.
    observed_legacy = db.get(CrawlMethod, legacy_method_id)
    observed_plugin = db.get(CrawlMethod, plugin_method_id)
    if observed_plugin is None or observed_legacy is None:
        raise LookupError("legacy or plugin method not found")
    if observed_legacy.domain != observed_plugin.domain:
        raise ValueError("methods are not a migratable website/WeChat pair")
    domain = observed_legacy.domain
    with domain_transition_lock(db, domain) as mapping:
        current = db.scalar(
            select(DiscoveryMethodMigration)
            .where(DiscoveryMethodMigration.plugin_method_id == plugin_method_id)
            .order_by(DiscoveryMethodMigration.id.desc())
            .with_for_update()
        )
        if current is not None:
            db.commit()
            db.refresh(current)
            return current
        methods = list(db.scalars(
            select(CrawlMethod)
            .where(CrawlMethod.id.in_((legacy_method_id, plugin_method_id)))
            .order_by(CrawlMethod.id)
            .with_for_update()
        ))
        methods_by_id = {method.id: method for method in methods}
        legacy = methods_by_id.get(legacy_method_id)
        plugin = methods_by_id.get(plugin_method_id)
        if plugin is None or legacy is None:
            raise LookupError("legacy or plugin method not found")
        if plugin.review_status != "approved":
            raise ValueError("plugin must be approved before migration registration")
        _validate_migration_plugin(plugin)
        source_kind = _legacy_source_kind(legacy)
        if legacy.domain != domain or plugin.domain != domain or source_kind is None:
            raise ValueError("methods are not a migratable website/WeChat pair")
        if mapping is None or mapping.method_id != legacy.id:
            raise ValueError("registration requires the legacy method to remain the current domain mapping")
        staging_source_id = plugin.source_id
        plugin.source_id = legacy.source_id
        plugin.status = "inactive"
        legacy.status = "active"
        legacy_source = db.get(Source, legacy.source_id)
        if legacy_source is None:
            raise ValueError("legacy Source is missing")
        legacy_source.enabled = True
        if staging_source_id != legacy.source_id:
            staging_source = db.get(Source, staging_source_id)
            if staging_source is not None:
                staging_source.enabled = False
        migration = DiscoveryMethodMigration(
            domain=domain,
            source_kind=source_kind,
            legacy_method_id=legacy.id,
            plugin_method_id=plugin.id,
            state="shadow_pending",
            legacy_snapshot=_legacy_snapshot(legacy),
        )
        db.add(migration)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError("domain migration candidate changed concurrently") from exc
        db.refresh(migration)
        return migration


def _validate_migration_plugin(plugin: CrawlMethod) -> Any:
    """Strictly parse recipe/config and prove approved artifact health."""
    from app.discovery.plugin.recipe import resolve_plugin_recipe
    from app.discovery.plugin.review import load_validated_review_artifact, validate_plugin_activation

    resolved = resolve_plugin_recipe(plugin.dsl_recipe or {})
    if resolved.connector_kind not in {"sites", "shared"}:
        raise ValueError("unsupported migration plugin kind")
    validated = load_validated_review_artifact(plugin)
    validate_plugin_activation(plugin)
    return validated


def _bounded_items(output: dict[str, Any]) -> list[dict[str, Any]]:
    items = output.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)][:500]


def compare_shadow_outputs(legacy_output: dict[str, Any], plugin_output: dict[str, Any]) -> dict[str, Any]:
    """Compare exact URLs and field values without retaining article bodies."""
    legacy_items = _bounded_items(legacy_output)
    plugin_items = _bounded_items(plugin_output)
    legacy_by_url = {
        normalize_comparison_url(str(item.get("url") or "")): item
        for item in legacy_items if item.get("url")
    }
    plugin_by_url = {
        normalize_comparison_url(str(item.get("url") or "")): item
        for item in plugin_items if item.get("url")
    }
    common = sorted(set(legacy_by_url) & set(plugin_by_url))
    legacy_only = sorted(set(legacy_by_url) - set(plugin_by_url))
    plugin_only = sorted(set(plugin_by_url) - set(legacy_by_url))
    title_mismatches = [
        {"url": url, "legacy": str(legacy_by_url[url].get("title") or "")[:500],
         "plugin": str(plugin_by_url[url].get("title") or "")[:500]}
        for url in common
        if str(legacy_by_url[url].get("title") or "").strip() != str(plugin_by_url[url].get("title") or "").strip()
    ]
    date_mismatches = [
        {"url": url, "legacy": str(legacy_by_url[url].get("published_at") or "")[:100],
         "plugin": str(plugin_by_url[url].get("published_at") or "")[:100]}
        for url in common
        if str(legacy_by_url[url].get("published_at") or "") != str(plugin_by_url[url].get("published_at") or "")
    ]
    plugin_missing_fields = [
        {"index": index, "fields": [name for name in ("title", "url", "published_at") if not item.get(name)]}
        for index, item in enumerate(plugin_items)
        if any(not item.get(name) for name in ("title", "url", "published_at"))
    ]
    legacy_count, plugin_count = len(legacy_items), len(plugin_items)
    missing_ratio = len(legacy_only) / max(1, len(legacy_by_url))
    legacy_error = (legacy_output.get("stats") or {}).get("error") if isinstance(legacy_output.get("stats"), dict) else None
    plugin_error = (plugin_output.get("stats") or {}).get("error") if isinstance(plugin_output.get("stats"), dict) else None
    passed = not plugin_error and not plugin_missing_fields and (legacy_count == 0 or missing_ratio <= 0.30)
    return redact_discovery_data({
        "passed": passed,
        "counts": {"legacy": legacy_count, "plugin": plugin_count, "common_urls": len(common)},
        "normalized_url_counts": {
            "legacy": len(legacy_by_url),
            "plugin": len(plugin_by_url),
            "common": len(common),
        },
        "raw_url_samples": {
            "legacy": [_public_url(str(item.get("url") or "")) for item in legacy_items[:20]],
            "plugin": [_public_url(str(item.get("url") or "")) for item in plugin_items[:20]],
        },
        "missing_ratio": round(missing_ratio, 6),
        "legacy_only_urls": [url[:8000] for url in legacy_only[:100]],
        "plugin_only_urls": [url[:8000] for url in plugin_only[:100]],
        "title_mismatches": title_mismatches[:100],
        "published_at_mismatches": date_mismatches[:100],
        "plugin_missing_fields": plugin_missing_fields[:100],
        "errors": {
            "legacy": redact_discovery_text(str(legacy_error))[:2000] if legacy_error else None,
            "plugin": redact_discovery_text(str(plugin_error))[:2000] if plugin_error else None,
        },
    })


def _host_allowed(url: str, allowed_domains: tuple[str, ...]) -> bool:
    try:
        host = (urlsplit(url).hostname or "").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)


def _execute_migration_artifact(
    validated: Any,
    *,
    comparison_id: int,
    suffix: str,
    config: dict[str, Any],
    purpose: str,
    trial_index: int | None = None,
    cancel_event: Event | None = None,
) -> Any:
    from app.discovery.plugin.contracts import ConnectorContext, ConnectorInvocation, ConnectorRequest
    from app.discovery.sandbox import SandboxExecution, SandboxJobPriority
    from app.discovery.sandbox.runtime import get_formal_sandbox_runtime

    artifact = validated.artifact
    invocation = ConnectorInvocation(
        request=ConnectorRequest(entry=artifact.manifest.entry, config=config),
        context=ConnectorContext(
            run_id=f"migration-{comparison_id}",
            connector_key=artifact.manifest.connector_key,
            connector_version=artifact.manifest.version,
            allowed_domains=artifact.manifest.allowed_domains,
        ),
    )
    return get_formal_sandbox_runtime().execute(SandboxExecution(
        job_id=f"migration-shadow-{comparison_id}-{suffix}",
        artifact=artifact,
        invocation=invocation,
        kind=validated.resolved.connector_kind,
        priority=SandboxJobPriority.DISCOVERY,
        expected_checksum=artifact.manifest.checksum,
        expected_signature=artifact.signature,
        purpose=purpose,
        trial_index=trial_index,
    ), cancel_event=cancel_event)


def _execute_url_verifier(
    validated: Any,
    *,
    comparison_id: int,
    urls: list[str],
    cancel_event: Event | None = None,
) -> tuple[set[str], dict[str, Any]]:
    from app.config import get_settings
    from app.discovery.loop.artifacts import write_trial_artifact
    from app.discovery.loop.engine import _URL_VERIFIER_SOURCE
    from app.discovery.plugin.contracts import ConnectorContext, ConnectorInvocation, ConnectorRequest
    from app.discovery.sandbox import SandboxExecution, SandboxJobPriority
    from app.discovery.sandbox.runtime import get_formal_sandbox_runtime

    manifest = validated.artifact.manifest
    verifier = write_trial_artifact(
        run_id=2_000_000_000 + comparison_id,
        site_url=manifest.entry,
        source=_URL_VERIFIER_SOURCE,
        allowed_domains=list(manifest.allowed_domains),
        runtime_version=manifest.runtime_version,
        connector_key="migration_url_verifier",
        version=1,
        settings=get_settings(),
    )
    result = get_formal_sandbox_runtime().execute(SandboxExecution(
        job_id=f"migration-shadow-{comparison_id}-url-verify",
        artifact=verifier,
        invocation=ConnectorInvocation(
            request=ConnectorRequest(entry=manifest.entry, config={"urls": urls[:10]}),
            context=ConnectorContext(
                run_id=f"migration-{comparison_id}",
                connector_key=verifier.manifest.connector_key,
                connector_version=verifier.manifest.version,
                allowed_domains=verifier.manifest.allowed_domains,
            ),
        ),
        kind="sites",
        priority=SandboxJobPriority.DISCOVERY,
        expected_checksum=verifier.manifest.checksum,
        expected_signature=verifier.signature,
        purpose="url_verifier",
    ), cancel_event=cancel_event)
    return (
        {item.url for item in result.audit_output.items},
        result.attestation.model_dump(mode="json"),
    )


def _comparison_heartbeat(
    comparison_id: int,
    owner_id: str,
    stop: Event,
    cancel_event: Event | None,
) -> None:
    from app.db import SessionLocal

    while not stop.wait(5.0):
        db = None
        try:
            db = SessionLocal()
            heartbeat = db.execute(update(DiscoveryMigrationComparison).where(
                DiscoveryMigrationComparison.id == comparison_id,
                DiscoveryMigrationComparison.status == "running",
                DiscoveryMigrationComparison.owner_id == owner_id,
            ).values(owner_heartbeat_at=_utcnow()))
            if heartbeat.rowcount != 1:
                db.rollback()
                if cancel_event is not None:
                    cancel_event.set()
                return
            cancel_requested = db.scalar(select(DiscoveryMigrationComparison.cancel_requested_at).where(
                DiscoveryMigrationComparison.id == comparison_id,
                DiscoveryMigrationComparison.status == "running",
                DiscoveryMigrationComparison.owner_id == owner_id,
            ))
            db.commit()
            if cancel_requested is not None and cancel_event is not None:
                cancel_event.set()
        except Exception:
            if db is not None:
                db.rollback()
            logger.warning(
                "shadow comparison heartbeat failed comparison_id=%s",
                comparison_id,
                exc_info=True,
            )
            if stop.wait(1.0):
                return
        finally:
            if db is not None:
                db.close()


def _legacy_recipe_is_unsupported(recipe: dict[str, Any]) -> bool:
    if recipe.get("source_kind") == "wechat_history" or recipe.get("requires_auth") is True:
        return True
    stack: list[Any] = [recipe]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                normalized = str(key).lower().replace("-", "_")
                if value not in (None, "", False, [], {}) and (
                    "cookie" in normalized
                    or "token" in normalized
                    or normalized in {
                        "auth", "auth_ref", "authorization", "credential", "credentials",
                        "secret", "signature",
                    }
                ):
                    return True
                stack.append(value)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str):
            if redact_discovery_text(current) != current:
                return True
            try:
                parsed = urlsplit(current.strip())
                if parsed.scheme.lower() in {"http", "https"}:
                    if parsed.username is not None or parsed.password is not None:
                        return True
                    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
                        normalized = key.lower().replace("-", "_")
                        if any(marker in normalized for marker in (
                            "token", "signature", "secret", "credential", "cookie", "auth", "api_key",
                        )) or normalized in {"key", "sig", "signed"}:
                            return True
            except (UnicodeError, ValueError):
                # URL-shaped strings that cannot be parsed safely are not
                # admissible migration evidence.
                if "://" in current:
                    return True
    return False


def _bounded_legacy_shadow_output(output: dict[str, Any]) -> dict[str, Any]:
    validated = ConnectorOutput.model_validate(output)
    if len(validated.items) > 500:
        raise ValueError("legacy shadow returned more than 500 items")
    projected = {
        "items": [
            {
                "title": _bounded_redacted_text(item.title, field="legacy title", max_length=1000),
                "url": _public_url(item.url),
                "published_at": (
                    _bounded_redacted_text(item.published_at, field="legacy published_at", max_length=200)
                    if item.published_at else None
                ),
            }
            for item in validated.items
        ],
        "stats": {"discovered_count": len(validated.items)},
    }
    validate_discovery_data_bounds(
        projected,
        max_string_chars=8000,
        max_approx_bytes=1_000_000,
    )
    return projected


def _legacy_shadow_worker(recipe: dict[str, Any], result_queue: Any) -> None:
    try:
        from app.discovery.execution import run_method

        result_queue.put({
            "ok": True,
            "output": _bounded_legacy_shadow_output(
                run_method(recipe, allow_legacy_compatibility=True)
            ),
        })
    except BaseException as exc:
        result_queue.put({
            "ok": False,
            "error_type": type(exc).__name__,
            "error": redact_discovery_text(str(exc))[:2000],
        })


def _execute_legacy_shadow(
    recipe: dict[str, Any],
    *,
    comparison_id: int,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    """Execute public legacy DSL in a killable child with a hard deadline."""
    if _legacy_recipe_is_unsupported(recipe):
        raise UnsupportedLegacyShadow(
            "authenticated or wechat_history legacy recipes are unsupported for shadow migration"
        )
    context = mp.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_legacy_shadow_worker,
        args=(recipe, result_queue),
        name="discovery-legacy-shadow",
        daemon=True,
    )
    process.start()
    with _SHADOW_JOB_LOCK:
        _LEGACY_SHADOW_PROCESSES[comparison_id] = process
    deadline = datetime.now(timezone.utc) + timedelta(seconds=LEGACY_SHADOW_TIMEOUT_SECONDS)
    payload: dict[str, Any] | None = None
    while process.is_alive() and datetime.now(timezone.utc) < deadline:
        if cancel_event is not None and cancel_event.is_set():
            break
        try:
            # Drain before join: multiprocessing's feeder cannot finish a
            # near-1MB payload while the parent waits for child exit.
            payload = result_queue.get(timeout=0.05)
            break
        except Empty:
            process.join(timeout=0.05)
    if process.is_alive() and payload is None:
        process.terminate()
        process.join(timeout=2.0)
        if process.is_alive():
            process.kill()
            process.join(timeout=1.0)
        result_queue.close()
        with _SHADOW_JOB_LOCK:
            _LEGACY_SHADOW_PROCESSES.pop(comparison_id, None)
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("legacy shadow cancelled")
        raise TimeoutError("legacy shadow exceeded its hard timeout")
    try:
        if payload is None:
            payload = result_queue.get(timeout=0.5)
        process.join(timeout=1.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1.0)
    except Empty as exc:
        raise RuntimeError("legacy shadow exited without a bounded result") from exc
    finally:
        result_queue.close()
        with _SHADOW_JOB_LOCK:
            _LEGACY_SHADOW_PROCESSES.pop(comparison_id, None)
    if not payload.get("ok"):
        raise RuntimeError(f"{payload.get('error_type')}: {payload.get('error')}")
    return ConnectorOutput.model_validate(payload["output"]).model_dump(mode="json")


def _finalize_owned_shadow(
    db: Session,
    *,
    comparison_id: int,
    migration_id: int,
    owner_id: str,
    status: str,
    values: dict[str, Any],
    migration_state: str,
    migration_error: str | None,
) -> DiscoveryMigrationComparison | None:
    """Only the persisted running owner may publish a terminal shadow result."""
    now = _utcnow()
    cancel_requested = db.scalar(select(DiscoveryMigrationComparison.cancel_requested_at).where(
        DiscoveryMigrationComparison.id == comparison_id,
        DiscoveryMigrationComparison.migration_id == migration_id,
        DiscoveryMigrationComparison.status == "running",
        DiscoveryMigrationComparison.owner_id == owner_id,
    ).with_for_update())
    if cancel_requested is not None:
        status = "cancelled"
        values = {
            "evaluator_passed": False,
            "comparison_passed": False,
            "legacy_count": 0,
            "plugin_count": 0,
            "evidence": {},
            "error_message": "shadow comparison cancelled",
        }
        migration_state = "shadow_failed"
        migration_error = "shadow comparison cancelled"
    terminal_values = {
        **values,
        "status": status,
        "completed_at": now,
        "cancel_acknowledged_at": (now if status == "cancelled" else None),
    }
    changed = db.execute(update(DiscoveryMigrationComparison).where(
        DiscoveryMigrationComparison.id == comparison_id,
        DiscoveryMigrationComparison.migration_id == migration_id,
        DiscoveryMigrationComparison.status == "running",
        DiscoveryMigrationComparison.owner_id == owner_id,
    ).values(**terminal_values))
    if changed.rowcount != 1:
        db.rollback()
        return db.get(DiscoveryMigrationComparison, comparison_id)
    migration = db.scalar(select(DiscoveryMethodMigration).where(
        DiscoveryMethodMigration.id == migration_id,
        DiscoveryMethodMigration.state == "shadow_running",
    ).with_for_update())
    if migration is not None:
        migration.state = migration_state
        migration.last_error = migration_error
    db.commit()
    return db.get(DiscoveryMigrationComparison, comparison_id)


def run_shadow_comparison(
    db: Session,
    migration_id: int,
    *,
    queued_comparison_id: int,
    cancel_event: Event | None = None,
) -> DiscoveryMigrationComparison:
    """Run plugin in gVisor and legacy DSL as shadows; neither output is ingested."""
    from app.discovery.loop.evaluator import evaluate_connector_outputs
    from app.discovery.plugin.review import parse_plugin_review_evidence
    from app.discovery.recipe_prepare import prepare_fetch_recipe
    from app.discovery.fetch_runs import process_start_token

    migration = db.scalar(
        select(DiscoveryMethodMigration).where(DiscoveryMethodMigration.id == migration_id).with_for_update()
    )
    if migration is None:
        raise LookupError("migration not found")
    if migration.state in {"cutover", "eligible", "rolled_back", "retired"}:
        raise ValueError("shadow comparison is closed after cutover")
    other_active = db.scalar(select(DiscoveryMethodMigration.id).where(
        DiscoveryMethodMigration.domain == migration.domain,
        DiscoveryMethodMigration.id != migration.id,
        DiscoveryMethodMigration.state.in_(ACTIVE_MIGRATION_STATES),
    ).limit(1))
    if other_active is not None:
        raise ValueError("a newer active migration candidate owns this domain")
    attempt_count = int(db.scalar(select(func.count()).select_from(
        DiscoveryMigrationComparison
    ).where(DiscoveryMigrationComparison.migration_id == migration.id)) or 0)
    if attempt_count > MAX_SHADOW_ATTEMPTS_PER_MIGRATION:
        raise ValueError("migration shadow retry limit reached")
    if migration.legacy_method_id is None:
        raise ValueError("legacy rollback method is missing")
    legacy = db.get(CrawlMethod, migration.legacy_method_id)
    plugin = db.get(CrawlMethod, migration.plugin_method_id)
    if legacy is None or plugin is None or plugin.review_status != "approved":
        raise ValueError("migration methods are not available and approved")
    assert_formal_method_current(db, legacy)
    legacy_recipe = prepare_fetch_recipe(legacy.dsl_recipe, None)
    if _legacy_recipe_is_unsupported(legacy_recipe):
        message = "authenticated or wechat_history legacy recipes are unsupported for shadow migration"
        comparison = db.scalar(select(DiscoveryMigrationComparison).where(
            DiscoveryMigrationComparison.id == queued_comparison_id,
            DiscoveryMigrationComparison.migration_id == migration.id,
            DiscoveryMigrationComparison.status == "queued",
        ).with_for_update())
        if comparison is None:
            raise ValueError("queued shadow comparison is no longer available")
        comparison.status = "blocked"
        comparison.error_message = message
        comparison.completed_at = _utcnow()
        migration.state = "blocked"
        migration.last_error = message
        db.commit()
        db.refresh(comparison)
        return comparison
    evidence = parse_plugin_review_evidence(plugin)
    validated = _validate_migration_plugin(plugin)
    owner_id = uuid.uuid4().hex
    comparison = db.scalar(
        select(DiscoveryMigrationComparison)
        .where(
            DiscoveryMigrationComparison.id == queued_comparison_id,
            DiscoveryMigrationComparison.migration_id == migration.id,
            DiscoveryMigrationComparison.status == "queued",
        )
        .with_for_update()
    )
    if comparison is None:
        raise ValueError("queued shadow comparison is no longer available")
    comparison.status = "running"
    comparison.owner_id = owner_id
    comparison.owner_pid = os.getpid()
    comparison.owner_start_token = process_start_token()
    comparison.owner_heartbeat_at = _utcnow()
    migration.state = "shadow_running"
    db.add(comparison)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("a shadow comparison is already running") from exc

    heartbeat_stop = Event()
    heartbeat = Thread(
        target=_comparison_heartbeat,
        args=(comparison.id, owner_id, heartbeat_stop, cancel_event),
        name=f"migration-shadow-heartbeat-{comparison.id}",
        daemon=True,
    )
    heartbeat.start()
    try:
        # Two independent gVisor executions are mandatory; historical review
        # evidence is only the approval lineage, never a substitute.
        base_config = dict(validated.resolved.config)
        trial_executions = [
            _execute_migration_artifact(
                validated,
                comparison_id=comparison.id,
                suffix=f"plugin-trial-{trial_index}",
                config=base_config,
                purpose="primary_trial",
                trial_index=trial_index,
                cancel_event=cancel_event,
            )
            for trial_index in (1, 2)
        ]
        outputs = [execution.audit_output for execution in trial_executions]
        review_checks = ((evidence.get("method_audit") or {}).get("evaluator") or {}).get("checks") or []
        pagination_check = next(
            (
                check
                for check in review_checks
                if check.get("check") in {"pagination_new_items", "pagination_fixed_listing"}
            ),
            {},
        )
        supports_pagination = (
            pagination_check.get("check") == "pagination_new_items"
            and pagination_check.get("applicable") is not False
        )
        accessibility_check = next(
            (check for check in review_checks if check.get("check") == "sample_url_accessibility"),
            {},
        )
        requires_url_verification = accessibility_check.get("applicable") is not False
        pagination_output = None
        requires_page_validation = (
            validated.resolved.connector_kind != "shared" or supports_pagination
        )
        if requires_page_validation:
            page_config = {**base_config, "page": 2}
            if "max_pages" in page_config:
                page_config["max_pages"] = 1
            pagination_execution = _execute_migration_artifact(
                validated,
                comparison_id=comparison.id,
                suffix="plugin-page-2",
                config=page_config,
                purpose="pagination",
                cancel_event=cancel_event,
            )
            pagination_output = pagination_execution.audit_output
        sample_urls = list(dict.fromkeys(item.url for output in outputs for item in output.items))[:10]
        declared_samples = [
            url for url in sample_urls if _host_allowed(url, validated.artifact.manifest.allowed_domains)
        ]
        reachable: set[str] = set()
        verifier_attestation = None
        if requires_url_verification or validated.resolved.connector_kind == "sites":
            reachable, verifier_attestation = _execute_url_verifier(
                validated,
                comparison_id=comparison.id,
                urls=declared_samples,
                cancel_event=cancel_event,
            )
        evaluation = evaluate_connector_outputs(
            outputs,
            reachable_urls=reachable,
            supports_pagination=supports_pagination,
            pagination_output=pagination_output,
            require_url_accessibility=requires_url_verification,
            enforce_fixed_listing_page=validated.resolved.connector_kind != "shared",
        ).as_dict()
        # Result links are not egress requests. Aggregator connectors may
        # legitimately return publisher domains absent from the sandbox
        # network allowlist.
        exception = evidence.get("low_frequency_exception") or {}
        failure_names = {str(item.get("check") or "") for item in evaluation.get("failures") or []}
        low_frequency_applied = bool(
            failure_names
            and all(name.endswith(".minimum_items") for name in failure_names)
            and exception.get("accepted") is True
            and exception.get("deterministic_failure_scope") == "minimum_items_only"
        )
        evaluator_passed = bool(evaluation.get("passed") is True or low_frequency_applied)
        plugin_output = outputs[0].model_dump(mode="json")
        legacy_output = _execute_legacy_shadow(
            legacy_recipe,
            comparison_id=comparison.id,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("shadow comparison cancelled")
        result = compare_shadow_outputs(legacy_output, plugin_output)
        shadow_evidence = {
            "plugin_evaluator": redact_discovery_data(evaluation),
            "low_frequency_exception_applied": low_frequency_applied,
            "independent_plugin_runs": 2,
            "runtime_attestations": [
                execution.attestation.model_dump(mode="json")
                for execution in trial_executions
            ],
            "pagination_attestation": (
                pagination_execution.attestation.model_dump(mode="json")
                if pagination_output is not None else None
            ),
            "url_verifier_attestation": verifier_attestation,
            "url_verifier": {"sampled": len(declared_samples), "reachable": len(reachable)},
            "pagination_checked": supports_pagination,
            "comparison": result,
        }
        validate_discovery_data_bounds(
            shadow_evidence,
            max_string_chars=1_000_000,
            max_approx_bytes=1_000_000,
        )
        comparison_passed = bool(result.get("passed"))
        terminal_status = "passed" if evaluator_passed and comparison_passed else "failed"
        finalized = _finalize_owned_shadow(
            db,
            comparison_id=comparison.id,
            migration_id=migration.id,
            owner_id=owner_id,
            status=terminal_status,
            values={
                "evaluator_passed": evaluator_passed,
                "comparison_passed": comparison_passed,
                "legacy_count": len(_bounded_items(legacy_output)),
                "plugin_count": len(_bounded_items(plugin_output)),
                "evidence": redact_discovery_data(shadow_evidence),
                "error_message": None,
            },
            migration_state=("shadow_passed" if terminal_status == "passed" else "shadow_failed"),
            migration_error=(None if terminal_status == "passed" else "deterministic shadow comparison failed"),
        )
        if finalized is None:
            raise RuntimeError("shadow comparison owner lost before finalization")
        return finalized
    except Exception as exc:
        from app.discovery.plugin.errors import ConnectorErrorCode, ConnectorProtocolError

        db.rollback()
        runtime_blocked = isinstance(exc, UnsupportedLegacyShadow) or bool(
            isinstance(exc, ConnectorProtocolError)
            and exc.code == ConnectorErrorCode.RUNTIME_ERROR
        ) or "runsc" in str(exc).lower() or "gvisor" in str(exc).lower()
        cancelled = bool(cancel_event is not None and cancel_event.is_set())
        terminal_status = "cancelled" if cancelled else ("blocked" if runtime_blocked else "failed")
        safe_error = redact_discovery_text(str(exc))[:4000]
        finalized = _finalize_owned_shadow(
            db,
            comparison_id=comparison.id,
            migration_id=migration.id,
            owner_id=owner_id,
            status=terminal_status,
            values={"error_message": safe_error},
            migration_state=("blocked" if terminal_status == "blocked" else "shadow_failed"),
            migration_error=safe_error,
        )
        if finalized is None:
            raise
        return finalized
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1.0)


def _run_queued_shadow(migration_id: int, comparison_id: int) -> None:
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        with _SHADOW_JOB_LOCK:
            cancel_event = _SHADOW_CANCEL_EVENTS.get(comparison_id)
        run_shadow_comparison(
            db,
            migration_id,
            queued_comparison_id=comparison_id,
            cancel_event=cancel_event,
        )
    except Exception as exc:
        db.rollback()
        comparison = db.get(DiscoveryMigrationComparison, comparison_id)
        migration = db.get(DiscoveryMethodMigration, migration_id)
        if comparison is not None and comparison.status == "queued":
            comparison.status = "failed"
            comparison.error_message = redact_discovery_text(str(exc))[:2000]
            comparison.completed_at = _utcnow()
        if migration is not None and migration.state == "shadow_pending":
            migration.state = "shadow_failed"
            migration.last_error = redact_discovery_text(str(exc))[:2000]
        db.commit()
    finally:
        db.close()
        with _SHADOW_JOB_LOCK:
            _SHADOW_CANCEL_EVENTS.pop(comparison_id, None)
        _SHADOW_CAPACITY.release()


def enqueue_shadow_comparison(db: Session, migration_id: int) -> DiscoveryMigrationComparison:
    """Persist a bounded queued job and return without blocking the API thread."""
    from app.discovery.domain_transition import MigrationBusyError, domain_transition_lock

    if not _SHADOW_CAPACITY.acquire(blocking=False):
        raise MigrationBusyError("shadow comparison capacity is full; retry later")
    capacity_owned = True
    try:
        observed = db.get(DiscoveryMethodMigration, migration_id)
        if observed is None:
            raise LookupError("migration not found")
        domain = observed.domain
        with domain_transition_lock(db, domain):
            migration = db.scalar(
                select(DiscoveryMethodMigration)
                .where(DiscoveryMethodMigration.id == migration_id)
                .with_for_update()
            )
            if migration is None:
                raise LookupError("migration not found")
            if migration.state in {"cutover", "eligible", "rolled_back", "retired"}:
                raise ValueError("shadow comparison is closed after cutover")
            outstanding = db.scalar(select(DiscoveryMigrationComparison).where(
                DiscoveryMigrationComparison.migration_id == migration.id,
                DiscoveryMigrationComparison.status.in_(("queued", "running")),
            ).order_by(DiscoveryMigrationComparison.id).limit(1).with_for_update())
            if outstanding is not None:
                db.commit()
                _SHADOW_CAPACITY.release()
                capacity_owned = False
                return outstanding
            attempt_count = int(db.scalar(select(func.count()).select_from(
                DiscoveryMigrationComparison
            ).where(DiscoveryMigrationComparison.migration_id == migration.id)) or 0)
            if attempt_count >= MAX_SHADOW_ATTEMPTS_PER_MIGRATION:
                raise ValueError("migration shadow retry limit reached")
            comparison = DiscoveryMigrationComparison(
                migration_id=migration.id,
                status="queued",
                evidence={},
            )
            db.add(comparison)
            db.commit()
            db.refresh(comparison)
        with _SHADOW_JOB_LOCK:
            _SHADOW_CANCEL_EVENTS[comparison.id] = Event()
        try:
            _SHADOW_WORKERS.submit(_run_queued_shadow, migration.id, comparison.id)
        except BaseException as exc:
            with _SHADOW_JOB_LOCK:
                _SHADOW_CANCEL_EVENTS.pop(comparison.id, None)
            comparison.status = "failed"
            comparison.error_message = redact_discovery_text(str(exc))[:2000]
            comparison.completed_at = _utcnow()
            migration.state = "shadow_failed"
            migration.last_error = comparison.error_message
            db.commit()
            raise
        capacity_owned = False  # queued worker releases the reservation
        return comparison
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(DiscoveryMigrationComparison).where(
            DiscoveryMigrationComparison.migration_id == migration_id,
            DiscoveryMigrationComparison.status.in_(("queued", "running")),
        ).order_by(DiscoveryMigrationComparison.id).limit(1))
        if existing is not None:
            return existing
        raise
    except BaseException:
        db.rollback()
        raise
    finally:
        if capacity_owned:
            _SHADOW_CAPACITY.release()


def cancel_shadow_comparison(comparison_id: int) -> bool:
    """Signal a queued/running shadow and hard-stop its legacy child if present."""
    with _SHADOW_JOB_LOCK:
        cancel_event = _SHADOW_CANCEL_EVENTS.get(comparison_id)
        process = _LEGACY_SHADOW_PROCESSES.get(comparison_id)
    if cancel_event is None:
        return False
    cancel_event.set()
    if process is not None and process.is_alive():
        process.terminate()
    return True


def request_shadow_comparison_cancel(
    db: Session,
    migration_id: int,
    comparison_id: int,
) -> DiscoveryMigrationComparison:
    """Durably request cancellation; queued jobs acknowledge atomically."""
    comparison = db.scalar(select(DiscoveryMigrationComparison).where(
        DiscoveryMigrationComparison.id == comparison_id,
        DiscoveryMigrationComparison.migration_id == migration_id,
    ).with_for_update())
    if comparison is None:
        raise LookupError("comparison not found")
    if comparison.status not in {"queued", "running"}:
        db.commit()
        db.refresh(comparison)
        return comparison
    now = _utcnow()
    comparison.cancel_requested_at = comparison.cancel_requested_at or now
    if comparison.status == "queued":
        comparison.status = "cancelled"
        comparison.cancel_acknowledged_at = now
        comparison.completed_at = now
        comparison.error_message = "shadow comparison cancelled before execution"
        migration = db.get(DiscoveryMethodMigration, migration_id)
        if migration is not None and migration.state == "shadow_pending":
            migration.state = "shadow_failed"
            migration.last_error = comparison.error_message
    db.commit()
    db.refresh(comparison)
    # Wake a same-process owner immediately; another worker observes the
    # durable request through its heartbeat.
    cancel_shadow_comparison(comparison.id)
    return comparison


def _cleanup_shadow_runtime_jobs(comparison_id: int) -> bool:
    """Best-effort proof that no sandbox job remains before stale-owner ack."""
    from app.discovery.sandbox.runtime import get_formal_sandbox_runtime

    runtime = get_formal_sandbox_runtime()
    suffixes = (
        "plugin-trial-1", "plugin-trial-2", "plugin-page-2", "url-verify",
    )
    try:
        failures: list[dict[str, str]] = []
        for suffix in suffixes:
            failures.extend(runtime.cleanup_job_resources(
                f"migration-shadow-{comparison_id}-{suffix}"
            ))
        return not failures
    except Exception:
        logger.warning("shadow sandbox cleanup deferred comparison_id=%s", comparison_id, exc_info=True)
        return False


def accept_comparison(
    db: Session,
    migration_id: int,
    *,
    explanation: str,
    reviewer: str,
) -> DiscoveryMigrationComparison:
    note = _bounded_redacted_text(explanation, field="comparison explanation")
    if len(note) < 10:
        raise ValueError("comparison explanation must be at least 10 characters")
    migration = db.scalar(select(DiscoveryMethodMigration).where(DiscoveryMethodMigration.id == migration_id).with_for_update())
    if migration is None:
        raise LookupError("migration not found")
    comparison = db.scalar(
        select(DiscoveryMigrationComparison)
        .where(DiscoveryMigrationComparison.migration_id == migration.id)
        .order_by(DiscoveryMigrationComparison.id.desc())
        .limit(1)
        .with_for_update()
    )
    if comparison is None or comparison.status != "passed" or not comparison.evaluator_passed or not comparison.comparison_passed:
        raise ValueError("latest deterministic comparison has not passed")
    safe_reviewer = _bounded_redacted_text(reviewer, field="reviewer", max_length=200)
    comparison.explanation = note
    comparison.accepted_by = safe_reviewer
    comparison.accepted_at = _utcnow()
    migration.state = "ready"
    migration.approved_by = safe_reviewer
    migration.approved_at = comparison.accepted_at
    migration.approval_note = note
    db.commit()
    db.refresh(comparison)
    return comparison


def cutover_migration(db: Session, migration_id: int) -> DiscoveryMethodMigration:
    from app.discovery.plugin.review import validate_plugin_activation
    from app.discovery.domain_transition import active_formal_run_id, domain_transition_lock

    observed = db.get(DiscoveryMethodMigration, migration_id)
    if observed is None:
        raise LookupError("migration not found")
    domain = observed.domain
    with domain_transition_lock(db, domain) as mapping:
        migration = db.scalar(
            select(DiscoveryMethodMigration)
            .where(DiscoveryMethodMigration.id == migration_id)
            .with_for_update()
        )
        if migration is None:
            raise LookupError("migration not found")
        if migration.state in {"cutover", "eligible", "retired"}:
            db.commit()
            db.refresh(migration)
            return migration
        if migration.state != "ready" or migration.approved_at is None:
            raise ValueError("migration is not approved for cutover")
        method_ids = tuple(
            method_id for method_id in (migration.legacy_method_id, migration.plugin_method_id)
            if method_id is not None
        )
        methods = list(db.scalars(
            select(CrawlMethod)
            .where(CrawlMethod.id.in_(method_ids))
            .order_by(CrawlMethod.id)
            .with_for_update()
        ))
        methods_by_id = {method.id: method for method in methods}
        legacy = methods_by_id.get(migration.legacy_method_id)
        plugin = methods_by_id.get(migration.plugin_method_id)
        comparison = db.scalar(select(DiscoveryMigrationComparison).where(
            DiscoveryMigrationComparison.migration_id == migration.id,
            DiscoveryMigrationComparison.accepted_at.is_not(None),
        ).order_by(DiscoveryMigrationComparison.id.desc()).limit(1))
        if legacy is None or plugin is None or plugin.review_status != "approved" or comparison is None:
            raise ValueError("cutover methods or accepted evidence are missing")
        active_run_id = active_formal_run_id(db, (legacy.id, plugin.id))
        if active_run_id is not None:
            raise ValueError(f"domain transition waits for active formal run {active_run_id}")
        validate_plugin_activation(plugin)
        if mapping is None or mapping.method_id != legacy.id:
            raise ValueError("published mapping changed before cutover")
        changed = db.execute(update(CrawlMethodDomain).where(
            CrawlMethodDomain.id == mapping.id, CrawlMethodDomain.method_id == legacy.id
        ).values(method_id=plugin.id))
        if changed.rowcount != 1:
            raise ValueError("cutover lost concurrent mapping update")
        now = _utcnow()
        legacy.status = "inactive"
        plugin.status = "active"
        migration.state = "cutover"
        migration.cutover_at = now
        migration.rollback_until = now + timedelta(days=ROLLBACK_DAYS)
        migration.formal_success_count_since_cutover = 0
        migration.rollback_at = None
        migration.rollback_reason = None
        migration.last_error = None
        db.commit()
        db.refresh(migration)
        return migration


def rollback_migration(db: Session, migration_id: int, *, reason: str) -> DiscoveryMethodMigration:
    from app.discovery.domain_transition import active_formal_run_id, domain_transition_lock

    safe_reason = _bounded_redacted_text(reason, field="rollback reason")
    if not safe_reason:
        raise ValueError("rollback reason is required")
    observed = db.get(DiscoveryMethodMigration, migration_id)
    if observed is None:
        raise LookupError("migration not found")
    domain = observed.domain
    with domain_transition_lock(db, domain) as mapping:
        migration = db.scalar(
            select(DiscoveryMethodMigration)
            .where(DiscoveryMethodMigration.id == migration_id)
            .with_for_update()
        )
        if migration is None:
            raise LookupError("migration not found")
        if migration.state == "rolled_back":
            db.commit()
            db.refresh(migration)
            return migration
        if migration.state not in {"cutover", "eligible"} or migration.legacy_method_id is None:
            raise ValueError("migration is not rollback-capable")
        methods = list(db.scalars(
            select(CrawlMethod)
            .where(CrawlMethod.id.in_((migration.legacy_method_id, migration.plugin_method_id)))
            .order_by(CrawlMethod.id)
            .with_for_update()
        ))
        methods_by_id = {method.id: method for method in methods}
        legacy = methods_by_id.get(migration.legacy_method_id)
        plugin = methods_by_id.get(migration.plugin_method_id)
        if legacy is None or plugin is None or mapping is None or mapping.method_id != plugin.id:
            raise ValueError("rollback mapping or methods changed")
        active_run_id = active_formal_run_id(db, (legacy.id, plugin.id))
        if active_run_id is not None:
            raise ValueError(f"domain transition waits for active formal run {active_run_id}")
        mapping.method_id = legacy.id
        legacy.status = "active"
        plugin.status = "inactive"
        migration.state = "rolled_back"
        migration.rollback_at = _utcnow()
        migration.rollback_reason = safe_reason
        migration.formal_success_count_since_cutover = 0
        db.commit()
        db.refresh(migration)
        return migration


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _complete_formal_success_count(db: Session, migration: DiscoveryMethodMigration) -> int:
    if migration.cutover_at is None:
        return 0
    return int(db.scalar(select(func.count()).select_from(CrawlMethodRun).where(
        CrawlMethodRun.method_id == migration.plugin_method_id,
        CrawlMethodRun.status == "ok",
        CrawlMethodRun.completed_at.is_not(None),
        CrawlMethodRun.migration_recorded_at.is_not(None),
        CrawlMethodRun.started_at >= migration.cutover_at,
    )) or 0)


def record_formal_terminal(
    db: Session,
    run_id: int,
    status: str,
    *,
    completed_at: datetime,
) -> None:
    """Atomically terminalize migration accounting with the CrawlMethodRun CAS."""
    run = db.scalar(
        select(CrawlMethodRun)
        .where(CrawlMethodRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None or run.status != status or run.completed_at is None:
        return
    claimed = db.execute(update(CrawlMethodRun).where(
        CrawlMethodRun.id == run_id,
        CrawlMethodRun.migration_recorded_at.is_(None),
    ).values(migration_recorded_at=completed_at))
    if claimed.rowcount != 1:
        return
    method = db.get(CrawlMethod, run.method_id)
    if method is not None:
        method.last_run_at = completed_at
        method.last_run_status = status
    migration = db.scalar(
        select(DiscoveryMethodMigration)
        .where(
            DiscoveryMethodMigration.plugin_method_id == run.method_id,
            DiscoveryMethodMigration.state.in_(("cutover", "eligible")),
        )
        .order_by(DiscoveryMethodMigration.id.desc())
        .with_for_update()
    )
    if migration is None:
        return
    if migration.cutover_at is None or _aware(run.started_at) < _aware(migration.cutover_at):
        return
    mapping = db.scalar(select(CrawlMethodDomain).where(CrawlMethodDomain.domain == migration.domain))
    if mapping is None or mapping.method_id != migration.plugin_method_id:
        return
    migration.last_formal_status = status
    migration.last_formal_at = completed_at
    # Rollback retirement requires complete formal successes.  Empty and
    # partial executions remain useful health signals but never count toward
    # the three-run irreversible deletion gate.
    if status == "ok":
        migration.formal_success_count_since_cutover += 1
        if (
            migration.rollback_until is not None
            and _aware(completed_at) >= _aware(migration.rollback_until)
            and migration.formal_success_count_since_cutover >= REQUIRED_FORMAL_SUCCESSES
        ):
            migration.state = "eligible"
        return
    if status == "failed" and migration.legacy_method_id is not None:
        legacy = db.get(CrawlMethod, migration.legacy_method_id)
        plugin = db.get(CrawlMethod, migration.plugin_method_id)
        mapping = db.scalar(select(CrawlMethodDomain).where(CrawlMethodDomain.domain == migration.domain).with_for_update())
        if legacy is not None and plugin is not None and mapping is not None and mapping.method_id == plugin.id:
            mapping.method_id = legacy.id
            legacy.status = "active"
            plugin.status = "inactive"
            migration.state = "rolled_back"
            migration.rollback_at = completed_at
            migration.rollback_reason = "automatic rollback after formal plugin failure"
            migration.formal_success_count_since_cutover = 0


def record_formal_terminal_with_domain_lock(db: Session, run_id: int) -> bool:
    """Reconcile a terminal run through the same domain transition protocol."""
    from app.discovery.domain_transition import domain_transition_lock

    observed = db.get(CrawlMethodRun, run_id)
    if observed is None or observed.completed_at is None or observed.status == "running":
        return False
    method = db.get(CrawlMethod, observed.method_id)
    if method is None:
        return False
    domain = method.domain
    with domain_transition_lock(db, domain):
        run = db.scalar(
            select(CrawlMethodRun)
            .where(CrawlMethodRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None or run.completed_at is None or run.status == "running":
            db.commit()
            return False
        was_unrecorded = run.migration_recorded_at is None
        record_formal_terminal(db, run.id, run.status, completed_at=run.completed_at)
        db.commit()
        return was_unrecorded


def refresh_migration_eligibility(db: Session) -> int:
    from app.discovery.domain_transition import MigrationBusyError, domain_transition_lock

    now = _utcnow()
    candidates = list(db.execute(select(
        DiscoveryMethodMigration.id,
        DiscoveryMethodMigration.domain,
    ).where(
        DiscoveryMethodMigration.state == "cutover",
    ).order_by(DiscoveryMethodMigration.id)))
    db.rollback()
    eligible_count = 0
    deadline = monotonic() + 0.5
    for migration_id, domain in candidates:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            with domain_transition_lock(db, domain, timeout_seconds=min(0.1, remaining)):
                migration = db.scalar(select(DiscoveryMethodMigration).where(
                    DiscoveryMethodMigration.id == migration_id,
                    DiscoveryMethodMigration.state == "cutover",
                ).with_for_update())
                if migration is None:
                    db.commit()
                    continue
                migration.formal_success_count_since_cutover = _complete_formal_success_count(db, migration)
                if (
                    migration.rollback_until is not None
                    and _aware(now) >= _aware(migration.rollback_until)
                    and migration.formal_success_count_since_cutover >= REQUIRED_FORMAL_SUCCESSES
                ):
                    migration.state = "eligible"
                    eligible_count += 1
                db.commit()
        except MigrationBusyError:
            db.rollback()
            logger.debug("eligibility refresh deferred for busy domain %s", domain)
    return eligible_count


def reconcile_unrecorded_formal_runs(db: Session) -> int:
    """Close crash windows left by older/non-hook terminal writers."""
    run_ids = list(db.scalars(select(CrawlMethodRun.id).where(
        CrawlMethodRun.status.in_(("ok", "empty", "partial", "failed", "cancelled")),
        CrawlMethodRun.completed_at.is_not(None),
        CrawlMethodRun.migration_recorded_at.is_(None),
        CrawlMethodRun.method_id.in_(select(DiscoveryMethodMigration.plugin_method_id)),
    ).order_by(CrawlMethodRun.id)))
    db.rollback()
    recorded = 0
    for run_id in run_ids:
        recorded += int(record_formal_terminal_with_domain_lock(db, run_id))
    return recorded


def recover_stale_migration_comparisons(db: Session) -> int:
    """Release dead comparison ownership without touching a proven live process."""
    from app.discovery.domain_transition import MigrationBusyError, domain_transition_lock
    from app.discovery.fetch_runs import process_start_token

    candidates = list(db.execute(
        select(DiscoveryMigrationComparison.id, DiscoveryMethodMigration.domain)
        .join(
            DiscoveryMethodMigration,
            DiscoveryMethodMigration.id == DiscoveryMigrationComparison.migration_id,
        )
        .where(DiscoveryMigrationComparison.status.in_(("queued", "running")))
        .order_by(DiscoveryMigrationComparison.id)
    ))
    db.rollback()
    recovered = 0
    deadline = monotonic() + 0.5
    for comparison_id, domain in candidates:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        recovery_owner: str | None = None
        migration_id: int | None = None
        queued_terminal = False
        try:
            with domain_transition_lock(db, domain, timeout_seconds=min(0.1, remaining)):
                comparison = db.scalar(select(DiscoveryMigrationComparison).where(
                    DiscoveryMigrationComparison.id == comparison_id,
                    DiscoveryMigrationComparison.status.in_(("queued", "running")),
                ).with_for_update())
                if comparison is None:
                    db.commit()
                    continue
                now = _utcnow()
                owner_live = False
                if comparison.owner_pid is not None and comparison.owner_start_token:
                    try:
                        owner_live = process_start_token(comparison.owner_pid) == comparison.owner_start_token
                    except (OSError, ValueError, IndexError):
                        owner_live = False
                heartbeat = comparison.owner_heartbeat_at or comparison.started_at
                heartbeat_stale = now - _aware(heartbeat) > timedelta(minutes=2)
                comparison_age = now - _aware(comparison.started_at)
                if comparison.status == "queued":
                    if comparison_age <= timedelta(
                        seconds=SHADOW_COMPARISON_QUEUED_LEASE_SECONDS
                    ):
                        db.commit()
                        continue
                    queued_terminal = True
                elif not heartbeat_stale or (
                    owner_live
                    and comparison_age <= timedelta(
                        seconds=SHADOW_COMPARISON_HARD_LEASE_SECONDS
                    )
                ):
                    # Heartbeat is the cross-worker lease.  A worker PID is
                    # generally invisible outside its container/process, so a
                    # fresh heartbeat always wins; even an old heartbeat is
                    # retained within the hard wall-time budget while its exact
                    # local process is proven alive.
                    db.commit()
                    continue
                migration_id = comparison.migration_id
                terminal_status = "cancelled" if comparison.cancel_requested_at is not None else "interrupted"
                message = (
                    "shadow comparison cancellation recovered after owner exit"
                    if terminal_status == "cancelled"
                    else "shadow comparison owner exited or exceeded its lease"
                )
                if queued_terminal:
                    changed = db.execute(update(DiscoveryMigrationComparison).where(
                        DiscoveryMigrationComparison.id == comparison.id,
                        DiscoveryMigrationComparison.status == "queued",
                        DiscoveryMigrationComparison.owner_id == comparison.owner_id,
                    ).values(
                        status=terminal_status,
                        error_message=message,
                        completed_at=now,
                        cancel_acknowledged_at=(now if terminal_status == "cancelled" else None),
                    ))
                    if changed.rowcount == 1:
                        migration = db.get(DiscoveryMethodMigration, comparison.migration_id)
                        if migration is not None and migration.state in {"shadow_pending", "shadow_running"}:
                            migration.state = "shadow_failed"
                            migration.last_error = message
                        recovered += 1
                    db.commit()
                    continue

                # Claim recovery ownership before cleanup.  This is a lease
                # CAS under the domain lock; the old worker's heartbeat and
                # final-result CAS will both fail after this commit.
                recovery_owner = f"recovery:{uuid.uuid4().hex}"
                changed = db.execute(update(DiscoveryMigrationComparison).where(
                    DiscoveryMigrationComparison.id == comparison.id,
                    DiscoveryMigrationComparison.status == "running",
                    DiscoveryMigrationComparison.owner_id == comparison.owner_id,
                ).values(
                    owner_id=recovery_owner,
                    owner_pid=None,
                    owner_start_token=None,
                    owner_heartbeat_at=now,
                ))
                if changed.rowcount != 1:
                    db.rollback()
                    continue
                db.commit()
        except MigrationBusyError:
            db.rollback()
            logger.debug("stale comparison recovery deferred for busy domain %s", domain)
            continue

        if recovery_owner is None or migration_id is None:
            continue
        # Container cleanup is deliberately outside the domain lock so formal
        # start/finish for this site cannot be reverse-blocked.  The recovery
        # owner remains a fresh lease while this work is in progress.
        if not _cleanup_shadow_runtime_jobs(comparison_id):
            continue
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            with domain_transition_lock(db, domain, timeout_seconds=min(0.1, remaining)):
                comparison = db.scalar(select(DiscoveryMigrationComparison).where(
                    DiscoveryMigrationComparison.id == comparison_id,
                    DiscoveryMigrationComparison.status == "running",
                    DiscoveryMigrationComparison.owner_id == recovery_owner,
                ).with_for_update())
                if comparison is None:
                    db.commit()
                    continue
                now = _utcnow()
                terminal_status = "cancelled" if comparison.cancel_requested_at is not None else "interrupted"
                message = (
                    "shadow comparison cancellation recovered after owner exit"
                    if terminal_status == "cancelled"
                    else "shadow comparison owner exited or exceeded its lease"
                )
                changed = db.execute(update(DiscoveryMigrationComparison).where(
                    DiscoveryMigrationComparison.id == comparison.id,
                    DiscoveryMigrationComparison.status == "running",
                    DiscoveryMigrationComparison.owner_id == recovery_owner,
                ).values(
                    status=terminal_status,
                    error_message=message,
                    completed_at=now,
                    cancel_acknowledged_at=(now if terminal_status == "cancelled" else None),
                ))
                if changed.rowcount == 1:
                    migration = db.get(DiscoveryMethodMigration, migration_id)
                    if migration is not None and migration.state in {"shadow_pending", "shadow_running"}:
                        migration.state = "shadow_failed"
                        migration.last_error = message
                    recovered += 1
                db.commit()
        except MigrationBusyError:
            db.rollback()
            logger.debug("stale comparison finalization deferred for busy domain %s", domain)
    return recovered


def bootstrap_legacy_migrations(db: Session) -> int:
    """Discover only explicit domains whose current mapping is still legacy."""
    mappings = list(db.scalars(select(CrawlMethodDomain).order_by(CrawlMethodDomain.domain)))
    created = 0
    for mapping in mappings:
        legacy = db.get(CrawlMethod, mapping.method_id)
        if legacy is None or _legacy_source_kind(legacy) is None:
            continue
        active = db.scalar(select(DiscoveryMethodMigration.id).where(
            DiscoveryMethodMigration.domain == mapping.domain,
            DiscoveryMethodMigration.state.in_(ACTIVE_MIGRATION_STATES),
        ).limit(1))
        if active is not None:
            continue
        attempted_plugin_ids = set(db.scalars(select(DiscoveryMethodMigration.plugin_method_id).where(
            DiscoveryMethodMigration.domain == mapping.domain,
        )))
        candidates = list(db.scalars(select(CrawlMethod).where(
            CrawlMethod.domain == mapping.domain,
            CrawlMethod.id != legacy.id,
            CrawlMethod.review_status == "approved",
        ).order_by(CrawlMethod.id.desc())))
        for plugin in candidates:
            if (
                plugin.id in attempted_plugin_ids
                or (plugin.dsl_recipe or {}).get("recipe_type") != "python_plugin"
            ):
                continue
            try:
                register_migration(db, legacy_method_id=legacy.id, plugin_method_id=plugin.id)
            except (ValueError, LookupError):
                db.rollback()
                continue
            created += 1
            break
    return created


def coordinate_next_migration_shadow() -> dict[str, Any] | None:
    """Scheduled/startup seam: atomically enqueue at most one pending shadow."""
    from app.db import SessionLocal

    from app.discovery.domain_transition import MigrationBusyError

    db = SessionLocal()
    try:
        try:
            recover_stale_migration_comparisons(db)
            bootstrap_legacy_migrations(db)
            refresh_migration_eligibility(db)
            reconcile_unrecorded_formal_runs(db)
            candidate_id = db.scalar(select(DiscoveryMethodMigration.id).where(
                DiscoveryMethodMigration.state == "shadow_pending",
            ).order_by(DiscoveryMethodMigration.id).limit(1))
            if candidate_id is None:
                return None
            comparison = enqueue_shadow_comparison(db, candidate_id)
            return {
                "migration_id": candidate_id,
                "comparison_id": comparison.id,
                "status": comparison.status,
            }
        except MigrationBusyError as exc:
            db.rollback()
            logger.warning("deferred Discovery migration shadow coordination: %s", exc)
            return {"status": "deferred", "retryable": True}
    finally:
        db.close()


def retire_legacy_method(db: Session, migration_id: int) -> DiscoveryMethodMigration:
    """Permanently delete one retained legacy DSL only after both rollback gates."""
    from app.discovery.domain_transition import active_formal_run_id, domain_transition_lock

    observed = db.get(DiscoveryMethodMigration, migration_id)
    if observed is None:
        raise LookupError("migration not found")
    domain = observed.domain
    with domain_transition_lock(db, domain) as mapping:
        migration = db.scalar(
            select(DiscoveryMethodMigration)
            .where(DiscoveryMethodMigration.id == migration_id)
            .with_for_update()
        )
        if migration is None:
            raise LookupError("migration not found")
        if migration.state == "retired":
            db.commit()
            db.refresh(migration)
            return migration
        now = _utcnow()
        if (
            migration.state not in {"cutover", "eligible"}
            or migration.rollback_until is None
            or now < _aware(migration.rollback_until)
        ):
            raise ValueError("legacy retirement requires 7 days and 3 successful formal plugin runs")
        method_ids = tuple(
            method_id for method_id in (migration.legacy_method_id, migration.plugin_method_id)
            if method_id is not None
        )
        methods = list(db.scalars(
            select(CrawlMethod)
            .where(CrawlMethod.id.in_(method_ids))
            .order_by(CrawlMethod.id)
            .with_for_update()
        ))
        methods_by_id = {method.id: method for method in methods}
        legacy = methods_by_id.get(migration.legacy_method_id)
        plugin = methods_by_id.get(migration.plugin_method_id)
        if legacy is None or plugin is None or mapping is None or mapping.method_id != plugin.id or plugin.status != "active":
            raise ValueError("plugin is not the active published method")
        active_run_id = active_formal_run_id(db, (legacy.id, plugin.id))
        if active_run_id is not None:
            raise ValueError(f"domain transition waits for active formal run {active_run_id}")
        unrecorded_ids = list(db.scalars(
            select(CrawlMethodRun.id).where(
                CrawlMethodRun.method_id.in_((legacy.id, plugin.id)),
                CrawlMethodRun.completed_at.is_not(None),
                CrawlMethodRun.status.in_(("ok", "empty", "partial", "failed", "cancelled")),
                CrawlMethodRun.migration_recorded_at.is_(None),
            ).order_by(CrawlMethodRun.id).with_for_update()
        ))
        for run_id in unrecorded_ids:
            terminal = db.get(CrawlMethodRun, run_id)
            if terminal is not None and terminal.completed_at is not None:
                record_formal_terminal(
                    db,
                    terminal.id,
                    terminal.status,
                    completed_at=terminal.completed_at,
                )
        still_unrecorded = db.scalar(select(CrawlMethodRun.id).where(
            CrawlMethodRun.method_id.in_((legacy.id, plugin.id)),
            CrawlMethodRun.completed_at.is_not(None),
            CrawlMethodRun.migration_recorded_at.is_(None),
        ).limit(1))
        if still_unrecorded is not None:
            raise ValueError(f"formal run {still_unrecorded} has not completed migration accounting")
        db.flush()
        migration.formal_success_count_since_cutover = _complete_formal_success_count(db, migration)
        if (
            migration.state not in {"cutover", "eligible"}
            or mapping.method_id != plugin.id
            or plugin.status != "active"
            or migration.formal_success_count_since_cutover < REQUIRED_FORMAL_SUCCESSES
        ):
            # Persist reconciliation (notably an automatic rollback) while
            # refusing the irreversible delete requested by this call.
            db.commit()
            raise ValueError("migration accounting changed the retirement eligibility")
        db.execute(update(SiteDiscoveryRun).where(SiteDiscoveryRun.resulting_method_id == legacy.id).values(resulting_method_id=None))
        db.execute(update(SiteDiscoveryRun).where(SiteDiscoveryRun.repair_method_id == legacy.id).values(repair_method_id=None))
        db.execute(update(MorningCrawlRunMethod).where(MorningCrawlRunMethod.method_id == legacy.id).values(method_id=None))
        migration.legacy_method_id = None
        db.delete(legacy)
        db.flush()
        migration.state = "retired"
        migration.retired_at = now
        migration.last_error = None
        db.commit()
        db.refresh(migration)
        return migration


def method_deletion_blocker(db: Session, method_id: int) -> str | None:
    migration = db.scalar(select(DiscoveryMethodMigration).where(
        (DiscoveryMethodMigration.legacy_method_id == method_id)
        | (DiscoveryMethodMigration.plugin_method_id == method_id)
    ))
    if migration is None:
        return None
    if migration.legacy_method_id == method_id and migration.state != "retired":
        return "legacy method is retained by an active migration rollback window"
    return "plugin method is referenced by a Discovery migration"


def _iso_utc(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None


def _public_text(value: str | None, max_length: int = 4000) -> str | None:
    return redact_discovery_text(value)[:max_length] if value is not None else None


def serialize_migration(
    db: Session,
    migration: DiscoveryMethodMigration,
    *,
    include_evidence: bool = False,
) -> dict[str, Any]:
    latest = db.scalar(select(DiscoveryMigrationComparison).where(
        DiscoveryMigrationComparison.migration_id == migration.id
    ).order_by(DiscoveryMigrationComparison.id.desc()).limit(1))
    shadow_attempt_count = int(db.scalar(select(func.count()).select_from(
        DiscoveryMigrationComparison
    ).where(DiscoveryMigrationComparison.migration_id == migration.id)) or 0)
    verified_success_count = _complete_formal_success_count(db, migration)
    now = _utcnow()
    retirement_eligible = bool(
        migration.state in {"cutover", "eligible"}
        and migration.rollback_until is not None
        and _aware(now) >= _aware(migration.rollback_until)
        and verified_success_count >= REQUIRED_FORMAL_SUCCESSES
    )
    return {
        "id": migration.id,
        "domain": migration.domain,
        "source_kind": migration.source_kind,
        "legacy_method_id": migration.legacy_method_id,
        "plugin_method_id": migration.plugin_method_id,
        "state": migration.state,
        "approved_at": _iso_utc(migration.approved_at),
        "approved_by": _public_text(migration.approved_by, 200),
        "approval_note": _public_text(migration.approval_note),
        "cutover_at": _iso_utc(migration.cutover_at),
        "rollback_until": _iso_utc(migration.rollback_until),
        "formal_success_count_since_cutover": verified_success_count,
        "required_formal_successes": REQUIRED_FORMAL_SUCCESSES,
        "shadow_attempt_count": shadow_attempt_count,
        "max_shadow_attempts": MAX_SHADOW_ATTEMPTS_PER_MIGRATION,
        "retirement_eligible": retirement_eligible,
        "last_formal_status": migration.last_formal_status,
        "last_formal_at": _iso_utc(migration.last_formal_at),
        "rollback_at": _iso_utc(migration.rollback_at),
        "rollback_reason": _public_text(migration.rollback_reason),
        "retired_at": _iso_utc(migration.retired_at),
        "last_error": _public_text(migration.last_error),
        "latest_comparison": (
            serialize_comparison(latest, include_evidence=include_evidence) if latest else None
        ),
    }


def serialize_comparison(
    comparison: DiscoveryMigrationComparison,
    *,
    include_evidence: bool = False,
) -> dict[str, Any]:
    result = {
        "id": comparison.id,
        "migration_id": comparison.migration_id,
        "status": comparison.status,
        "evaluator_passed": comparison.evaluator_passed,
        "comparison_passed": comparison.comparison_passed,
        "legacy_count": comparison.legacy_count,
        "plugin_count": comparison.plugin_count,
        "explanation": _public_text(comparison.explanation),
        "accepted_at": _iso_utc(comparison.accepted_at),
        "accepted_by": _public_text(comparison.accepted_by, 200),
        "error_message": _public_text(comparison.error_message),
        "cancel_requested_at": _iso_utc(comparison.cancel_requested_at),
        "cancel_acknowledged_at": _iso_utc(comparison.cancel_acknowledged_at),
        "started_at": _iso_utc(comparison.started_at),
        "completed_at": _iso_utc(comparison.completed_at),
    }
    if include_evidence:
        evidence = redact_discovery_data(comparison.evidence or {})
        try:
            validate_discovery_data_bounds(
                evidence,
                max_string_chars=128_000,
                max_approx_bytes=512_000,
            )
        except ValueError:
            evidence = {"omitted": True, "reason": "evidence exceeds public response limit"}
        result["evidence"] = evidence
    return result
