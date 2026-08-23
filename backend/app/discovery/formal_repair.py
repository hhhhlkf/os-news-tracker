"""Bounded automatic-repair trigger for repeatedly failing formal plugins."""

from __future__ import annotations

import json
import logging
from itertools import islice
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db import SessionLocal
from app.discovery.plugin.errors import ConnectorErrorCode, ConnectorProtocolError
from app.discovery.redaction import redact_discovery_data, redact_discovery_text
from app.models import CrawlMethod, CrawlMethodRun, SiteDiscoveryRun

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)
_EVIDENCE_MAX_BYTES = 32 * 1024
_EVIDENCE_MAX_DEPTH = 8
_EVIDENCE_MAX_NODES = 512
_EVIDENCE_MAX_STRING_CHARS = 4096
_EVIDENCE_MAX_TOTAL_CHARS = 20_000
_REPAIRABLE_CODES = {
    ConnectorErrorCode.INVALID_INPUT,
    ConnectorErrorCode.INVALID_MANIFEST,
    ConnectorErrorCode.ARTIFACT_INTEGRITY_ERROR,
    ConnectorErrorCode.PLUGIN_LOAD_ERROR,
    ConnectorErrorCode.PLUGIN_EXECUTION_ERROR,
    ConnectorErrorCode.INVALID_OUTPUT,
    ConnectorErrorCode.TIMEOUT,
}


def formal_failure_evidence(exc: Exception, *, stage: str) -> dict[str, object]:
    """Build bounded evidence; hostile exception details can never escape this boundary."""
    safe_stage = _small_text(stage, fallback="unknown")
    is_connector = isinstance(exc, ConnectorProtocolError)
    safe_code = _small_text(
        exc.code.value if is_connector else type(exc).__name__,
        fallback="unknown",
    )
    repairable = bool(
        is_connector
        and safe_stage in {"connector_sandbox", "connector_contract"}
        and exc.code in _REPAIRABLE_CODES
    )
    try:
        projected, truncated = _bounded_projection(exc.details if is_connector else {})
        details = redact_discovery_data(
            projected,
            max_string_chars=_EVIDENCE_MAX_STRING_CHARS,
            # The redactor deliberately uses a conservative four-bytes-per-
            # character estimate. Projection is already capped at 20k chars;
            # allow that bounded tree through and enforce the exact UTF-8 JSON
            # byte ceiling immediately below.
            max_approx_bytes=_EVIDENCE_MAX_BYTES * 4,
        )
        evidence: dict[str, object] = {
            "stage": safe_stage,
            "code": safe_code,
            "message": redact_discovery_text(_small_text(exc, fallback="formal fetch failed")),
            "details": details,
            "details_truncated": truncated,
            "repairable": repairable,
        }
        encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()
        if len(encoded) > _EVIDENCE_MAX_BYTES:
            return _minimal_failure_evidence(
                stage=safe_stage,
                code=safe_code,
                message=str(evidence["message"]),
                error="bounded evidence exceeded its final byte limit",
                repairable=repairable,
            )
        return evidence
    except BaseException as evidence_error:  # evidence must be total, including hostile __str__
        return _minimal_failure_evidence(
            stage=safe_stage,
            code=safe_code,
            message="formal fetch failed; detailed evidence unavailable",
            error=type(evidence_error).__name__,
            repairable=repairable,
        )


def _bounded_projection(value: object) -> tuple[object, bool]:
    """Iteratively project arbitrary details into a small JSON-compatible tree."""
    holder: list[object] = [None]
    stack: list[tuple[object, object, str | int, int]] = [(value, holder, 0, 0)]
    seen: set[int] = set()
    nodes = 0
    total_chars = 0
    truncated = False
    while stack:
        current, parent, slot, depth = stack.pop()
        nodes += 1
        if nodes > _EVIDENCE_MAX_NODES or depth > _EVIDENCE_MAX_DEPTH:
            parent[slot] = "[truncated]"
            truncated = True
            continue
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen:
                parent[slot] = "[cycle]"
                truncated = True
                continue
            seen.add(identity)
            output: dict[str, object] = {}
            parent[slot] = output
            entries = list(islice(current.items(), 64))
            truncated = truncated or len(current) > len(entries)
            tasks: list[tuple[object, object, str | int, int]] = []
            for raw_key, item in entries:
                key = _small_text(raw_key, fallback="unknown-key", limit=200)
                total_chars += len(key)
                if total_chars > _EVIDENCE_MAX_TOTAL_CHARS:
                    truncated = True
                    break
                while key in output:
                    key = f"{key}#"
                output[key] = None
                tasks.append((item, output, key, depth + 1))
            stack.extend(reversed(tasks))
            continue
        if isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in seen:
                parent[slot] = "[cycle]"
                truncated = True
                continue
            seen.add(identity)
            length = min(len(current), 64)
            output_list: list[object] = [None] * length
            parent[slot] = output_list
            truncated = truncated or len(current) > length
            for index in range(length - 1, -1, -1):
                stack.append((current[index], output_list, index, depth + 1))
            continue
        if current is None or isinstance(current, (bool, int, float)):
            parent[slot] = current
            continue
        if isinstance(current, (str, bytes)) and len(current) > _EVIDENCE_MAX_STRING_CHARS:
            truncated = True
        text = _small_text(current, fallback="[unprintable]")
        remaining = max(0, _EVIDENCE_MAX_TOTAL_CHARS - total_chars)
        if len(text) > remaining:
            text = text[:remaining]
            truncated = True
        parent[slot] = text
        total_chars += len(text)
    return holder[0], truncated


def _small_text(value: object, *, fallback: str, limit: int = _EVIDENCE_MAX_STRING_CHARS) -> str:
    try:
        if isinstance(value, str):
            return value[:limit]
        if isinstance(value, bytes):
            return value[:limit].decode("utf-8", errors="replace")
        return str(value)[:limit]
    except BaseException:
        return fallback[:limit]


def _minimal_failure_evidence(
    *,
    stage: str,
    code: str,
    message: str,
    error: str,
    repairable: bool,
) -> dict[str, object]:
    return {
        "stage": _small_text(stage, fallback="unknown", limit=100),
        "code": _small_text(code, fallback="unknown", limit=200),
        "message": redact_discovery_text(_small_text(message, fallback="formal fetch failed")),
        "details": {},
        "details_truncated": True,
        "evidence_error": _small_text(error, fallback="unknown", limit=200),
        "repairable": repairable,
    }


def trigger_repair_after_formal_result(
    method_id: int,
    *,
    db: Session | None = None,
) -> int | None:
    """Start one repair after exactly three latest formal attempts failed.

    The durable run history is shared by manual and scheduled execution.  An
    existing repair associated with the same three-run failure streak makes
    this operation idempotent across workers.  Cancellation is not a plugin
    failure and therefore breaks the streak.
    """
    try:
        return _trigger_repair_after_formal_result(method_id, db=db)
    except Exception:
        # Formal result persistence must never be rolled back or masked merely
        # because the bounded repair queue is unavailable.
        logger.exception("failed to trigger bounded plugin repair method_id=%s", method_id)
        return None


def _trigger_repair_after_formal_result(
    method_id: int,
    *,
    db: Session | None,
) -> int | None:
    """Implement the durable streak check; the public wrapper is fail-safe."""
    owns_session = db is None
    session = db or SessionLocal()
    try:
        method = session.get(CrawlMethod, method_id)
        if method is None or (method.dsl_recipe or {}).get("recipe_type") != "python_plugin":
            return None
        recent = list(
            session.scalars(
                select(CrawlMethodRun)
                .where(
                    CrawlMethodRun.method_id == method_id,
                    CrawlMethodRun.status != "running",
                )
                .order_by(CrawlMethodRun.started_at.desc(), CrawlMethodRun.id.desc())
                .limit(3)
            )
        )
        if len(recent) < 3 or any(
            run.status != "failed"
            or not isinstance(run.failure_evidence, dict)
            or run.failure_evidence.get("repairable") is not True
            for run in recent
        ):
            return None
        existing = session.scalar(
            select(SiteDiscoveryRun.id)
            .where(
                SiteDiscoveryRun.repair_method_id == method_id,
                SiteDiscoveryRun.trigger_type == "repair",
            )
            .order_by(SiteDiscoveryRun.id.desc())
            .limit(1)
        )
        if existing is not None:
            return int(existing)
        recipe = dict(method.dsl_recipe)
    finally:
        if owns_session:
            session.close()

    if recipe.get("connector_kind") == "shared":
        from app.discovery.wechat_plugin import start_wechat_repair_run

        return start_wechat_repair_run(method_id)
    from app.discovery.loop.engine import get_website_loop_engine

    return get_website_loop_engine().start_repair(method_id)
