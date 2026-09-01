"""Strict public projection for persisted Discovery audit events."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.discovery.redaction import redact_discovery_data, redact_discovery_text
from app.models import DiscoveryRunEvent


MAX_PUBLIC_DIFF_CHARS = 40_000
MAX_PUBLIC_SAMPLES = 12
MAX_PUBLIC_TEXT_CHARS = 2_000
MAX_PUBLIC_PAYLOAD_BYTES = 64 * 1024
_PUBLIC_PHASES = frozenset({"context", "explore", "build", "execute", "evaluate", "repair", "package"})
_PRIVATE_LABEL_RE = re.compile(
    r"(?i)\b(?:runtime_attestations?|auxiliary_proofs?|host_proof|nonce|runtime_binary|"
    r"invocation|reviewer_reason|key_id|signing_key_id)\b\s*[:=]\s*[^\s,;}]+"
)
_HOST_PATH_RE = re.compile(
    r"(?<![:/])/(?:home|root|tmp|var|etc|usr|opt|srv|data|workspace|app)"
    r"(?:/[A-Za-z0-9._-]+)+"
)
_PRIVATE_REASONING_TEXT_RE = re.compile(
    r"(?i)\b(?:chain[_\s-]*of[_\s-]*thought|private[_\s-]*reasoning|"
    r"reasoning[_\s-]*content|raw[_\s-]*model[_\s-]*response|thoughts?)\b"
    r"\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n]+)"
)
_SAFE_CREDENTIAL_CODE_RE = re.compile(
    r"(?i)\b(?:next[_-]?token|password|passwd|token|secret|api[_-]?key|authorization|"
    r"credential|cookie)s?\b"
    r"\s*[:=]\s*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?:\([^\r\n)]*\))?"
)
_CREDENTIAL_PROSE_VALUE_RE = re.compile(
    r"(?i)\b((?:access\s+)?credentials?|password|passwd|token|secret|api[ _-]?key|"
    r"authorization|cookie)\b"
    r"(\s+(?:is|was|are|were)\s+)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^,;\r\n}\]]+)"
)

PUBLIC_EVENT_TYPES = frozenset(
    {
        "agent_draft_rejected",
        "agent_turn_timeout",
        "artifact_pending_review",
        "artifact_rejected",
        "connector_written",
        "evaluation_completed",
        "evaluation_processing_failed",
        "execution_evidence_processing_failed",
        "field_smoke_failed",
        "explore_action_selected",
        "explore_probe_step",
        "explore_tool_observation",
        "formal_failure_evidence",
        "openhands_action_event",
        "phase_changed",
        "rag_retrieved",
        "rag_unavailable",
        "repair_attempt_recorded",
        "repair_attempt_limit_reached",
        "repair_noop_rejected",
        "resume_dispatched",
        "resume_queued",
        "run_cancelled",
        "run_failed",
        "run_interrupted",
        "sandbox_capacity_acquired",
        "sandbox_capacity_queued",
        "sandbox_execution_failed",
        "sandbox_tool_evidence",
        "wechat_evaluation_completed",
        "wechat_plugin_packaged",
    }
)


def _text(value: object, *, limit: int = MAX_PUBLIC_TEXT_CHARS) -> str:
    if not isinstance(value, (str, int, float, bool)):
        if isinstance(value, dict):
            return f"structured summary ({len(value)} fields)"
        if isinstance(value, (list, tuple)):
            return f"structured summary ({len(value)} entries)"
        return "structured summary"
    raw = str(value)
    # The persistence redactor is intentionally broad.  Preserve references to
    # credential-shaped *field names* in safe code expressions, while still
    # applying it first to every other fragment and never restoring a literal.
    protected: dict[str, str] = {}

    def protect_safe_code(match: re.Match[str]) -> str:
        marker = f"PUBLIC_SAFE_FIELD_{len(protected)}"
        protected[marker] = match.group(0)
        return marker

    guarded = _SAFE_CREDENTIAL_CODE_RE.sub(protect_safe_code, raw)
    cleaned = redact_discovery_text(guarded)
    cleaned = _CREDENTIAL_PROSE_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted]",
        cleaned,
    )
    for marker, safe_code in protected.items():
        cleaned = cleaned.replace(marker, safe_code)
    cleaned = _PRIVATE_LABEL_RE.sub("[private runtime field]", cleaned)
    cleaned = _PRIVATE_REASONING_TEXT_RE.sub("[private reasoning]", cleaned)
    cleaned = _HOST_PATH_RE.sub("[private path]", cleaned)
    return cleaned[:limit]


def _failure_samples(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    samples: list[dict[str, Any]] = []
    for raw in value[:MAX_PUBLIC_SAMPLES]:
        if not isinstance(raw, dict):
            samples.append({"summary": _text(raw, limit=500)})
            continue
        sample: dict[str, Any] = {}
        for key in (
            "check", "passed", "message", "error", "reason", "count", "expected", "actual",
            "candidate_count", "rejected_count", "requested_target_count", "diagnosis", "repair_hint",
        ):
            if key not in raw:
                continue
            item = raw[key]
            sample[key] = item if isinstance(item, (bool, int, float)) else _text(item, limit=500)
        if sample:
            samples.append(sample)
    return samples


def _public_job_id(value: object) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()
    return f"job-{digest[:12]}"


def _count(value: object) -> int:
    try:
        return min(1_000_000, max(0, int(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _public_timing_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Allow only bounded, mechanism-independent elapsed-time metadata."""
    projected: dict[str, Any] = {}
    for key in (
        "active_execution_elapsed_seconds",
        "completed_phase_elapsed_seconds",
        "phase_elapsed_seconds",
        "queue_wait_seconds",
    ):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            projected[key] = min(86_400.0, max(0.0, round(float(value), 3)))
    for key in ("completed_phase", "timed_phase"):
        value = payload.get(key)
        if isinstance(value, str) and value in _PUBLIC_PHASES:
            projected[key] = value
    return projected


def _sandbox_log_samples(events: object) -> list[dict[str, str]]:
    if not isinstance(events, list):
        return []
    samples: list[dict[str, str]] = []
    for raw in events[:8]:
        if not isinstance(raw, dict):
            continue
        sample: dict[str, str] = {}
        for key in ("event", "type", "level"):
            value = raw.get(key)
            if value is not None:
                sample[key] = _text(value, limit=300)
        if sample:
            samples.append(sample)
    return samples


def _runner_error_message(stderr: object) -> str | None:
    """Extract the connector-owned failure without exposing arbitrary raw stderr."""
    if not isinstance(stderr, str):
        return None
    for line in stderr.splitlines()[:100]:
        try:
            event = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("event") != "runner_error":
            continue
        message = event.get("message")
        if isinstance(message, str) and message.strip():
            prefix = "connector execution failed: "
            cleaned = message.strip()
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix):]
            return _text(cleaned, limit=1_000)
    return None


def _public_error_fields(error: Any) -> dict[str, Any]:
    if not isinstance(error, dict):
        return {"error_summary": _text(error, limit=1_000)}
    projected: dict[str, Any] = {
        "error_summary": _text(error.get("message") or "execution failed", limit=1_000),
    }
    for source_key, target_key in (
        ("type", "error_type"),
        ("code", "error_code"),
        ("stage", "error_stage"),
    ):
        if error.get(source_key) is not None:
            projected[target_key] = _text(error[source_key], limit=200)
    details = error.get("details")
    if isinstance(details, dict):
        if isinstance(details.get("returncode"), int):
            projected["returncode"] = details["returncode"]
        if details.get("stderr") is not None:
            projected["stderr_summary"] = _text(details["stderr"], limit=2_000)
            runner_error = _runner_error_message(details["stderr"])
            if runner_error:
                projected["container_error_summary"] = projected["error_summary"]
                projected["error_summary"] = runner_error
    return projected


def _project_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    projected: dict[str, Any] = {}
    if event_type == "openhands_action_event":
        projected.update(payload)
    elif event_type == "phase_changed":
        if payload.get("source_kind") in {"website", "wechat", "internal_forum", "unknown"}:
            projected["source_kind"] = payload["source_kind"]
    elif event_type == "rag_retrieved":
        if payload.get("reference_count") is not None:  # historical event compatibility
            projected["reference_count"] = _count(payload.get("reference_count"))
        if payload.get("rag_stage") in {"explore", "build", "repair"}:
            projected["rag_stage"] = payload["rag_stage"]
        for key in ("scanned_count", "matched_count", "injected_count"):
            projected[key] = _count(payload.get(key))
    elif event_type in {"connector_written", "artifact_rejected"}:
        if payload.get("change_summary") is not None:
            projected["change_summary"] = _text(payload["change_summary"])
        if payload.get("code_diff") is not None:
            projected["code_diff"] = _text(payload["code_diff"], limit=MAX_PUBLIC_DIFF_CHARS)
        if isinstance(payload.get("allowed_domains"), list):
            projected["allowed_domain_count"] = len(payload["allowed_domains"])
    elif event_type in {"evaluation_completed", "wechat_evaluation_completed", "field_smoke_failed"}:
        if isinstance(payload.get("passed"), bool):
            projected["passed"] = payload["passed"]
        failures = _failure_samples(payload.get("failures"))
        projected["failure_count"] = len(payload.get("failures") or []) if isinstance(payload.get("failures"), list) else 0
        if failures:
            projected["failures"] = failures
        for key in ("item_count", "discovered_count", "valid_title_count", "reachable_count", "duplicate_count"):
            if isinstance(payload.get(key), (int, float)):
                projected[key] = payload[key]
    elif event_type == "sandbox_tool_evidence":
        projected["public_job_id"] = _public_job_id(payload.get("job_id", ""))
        projected["event_count"] = _count(payload.get("event_count"))
        samples = _sandbox_log_samples(payload.get("events"))
        if samples:
            projected["log_samples"] = samples
    elif event_type == "explore_action_selected":
        if payload.get("action") is not None:
            projected["action"] = _text(payload["action"], limit=100)
        if isinstance(payload.get("action_number"), int):
            projected["action_number"] = payload["action_number"]
        if isinstance(payload.get("allowed_domains"), list):
            projected["allowed_domain_count"] = len(payload["allowed_domains"])
    elif event_type == "explore_probe_step":
        for key in ("action_number", "sequence"):
            if isinstance(payload.get(key), int):
                projected[key] = payload[key]
        if payload.get("operation") is not None:
            projected["operation"] = _text(payload["operation"], limit=100)
        details = payload.get("details")
        if isinstance(details, dict):
            safe_details: dict[str, Any] = {}
            for key in (
                "method", "url", "status", "content_type", "body_chars", "selector",
                "key", "seconds", "direction", "amount", "scroll_number", "count",
                "chars", "bytes", "page_number", "title", "resource_types", "kind",
                "url_contains", "name", "value_chars",
            ):
                value = details.get(key)
                if isinstance(value, (bool, int, float)):
                    safe_details[key] = value
                elif isinstance(value, (str, list, tuple)):
                    safe_details[key] = _text(value, limit=500)
            if safe_details:
                projected["details"] = safe_details
    elif event_type == "explore_tool_observation":
        for key in ("action_number", "action"):
            value = payload.get(key)
            if isinstance(value, (int, str)):
                projected[key] = value if isinstance(value, int) else _text(value, limit=100)
        if payload.get("error") is not None:
            projected.update(_public_error_fields(payload["error"]))
        elif payload.get("result") is not None:
            projected["observation_summary"] = _text(payload["result"], limit=800)
    elif event_type == "agent_turn_timeout":
        if payload.get("stage") in {"build", "repair"}:
            projected["stage"] = payload["stage"]
        for key in ("limit_seconds", "elapsed_seconds"):
            if isinstance(payload.get(key), (int, float)):
                projected[key] = max(0, float(payload[key]))
        if payload.get("error") is not None:
            projected["error_summary"] = _text(payload["error"], limit=1_000)
    elif event_type in {
        "repair_attempt_recorded",
        "repair_attempt_limit_reached",
        "repair_noop_rejected",
    }:
        for key in (
            "repair_attempts",
            "effective_repair_attempts",
            "max_repair_attempts",
        ):
            if isinstance(payload.get(key), int):
                projected[key] = _count(payload[key])
        failures = _failure_samples(payload.get("failures"))
        projected["failure_count"] = len(payload.get("failures") or []) if isinstance(payload.get("failures"), list) else 0
        if failures:
            projected["failures"] = failures
        if payload.get("error") is not None:
            projected.update(_public_error_fields(payload["error"]))
    elif event_type in {
        "run_failed",
        "sandbox_execution_failed",
        "evaluation_processing_failed",
        "execution_evidence_processing_failed",
        "rag_unavailable",
        "agent_draft_rejected",
    }:
        error = payload.get("error") or payload.get("message")
        if error is None and any(key in payload for key in ("code", "type", "details", "stage")):
            error = payload
        if error is not None:
            projected.update(_public_error_fields(error))
        if isinstance(payload.get("action_number"), int):
            projected["action_number"] = payload["action_number"]
        if payload.get("action") is not None:
            projected["action"] = _text(payload["action"], limit=100)
    elif event_type == "formal_failure_evidence":
        failures = _failure_samples(payload.get("failures"))
        projected["failure_count"] = len(payload.get("failures") or []) if isinstance(payload.get("failures"), list) else 0
        if failures:
            projected["failures"] = failures
    elif event_type in {"sandbox_capacity_acquired", "sandbox_capacity_queued"}:
        for key in ("maximum_seconds", "already_elapsed_seconds", "elapsed_snapshot"):
            if isinstance(payload.get(key), (int, float)):
                projected[key] = max(0, float(payload[key]))
    elif event_type in {"artifact_pending_review", "wechat_plugin_packaged"}:
        if isinstance(payload.get("method_id"), int):
            projected["method_id"] = payload["method_id"]
        if payload.get("review_status") in {"pending", "approved", "rejected"}:
            projected["review_status"] = payload["review_status"]
    elif event_type in {"resume_queued", "resume_dispatched", "run_interrupted", "run_cancelled"}:
        if isinstance(payload.get("checkpoint_available"), bool):
            projected["checkpoint_available"] = payload["checkpoint_available"]
    projected.update(_public_timing_fields(payload))
    if not projected:
        return None
    redacted = redact_discovery_data(projected)
    encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PUBLIC_PAYLOAD_BYTES:
        return {"summary": "[public evidence exceeded size limit]"}
    return redacted


def public_discovery_event(event: DiscoveryRunEvent) -> dict[str, Any] | None:
    """Return an allowlisted, bounded projection with no private runtime/audit proof data."""
    event_type = event.event_type.strip().lower()
    if event_type not in PUBLIC_EVENT_TYPES:
        return None
    payload = event.payload if isinstance(event.payload, dict) else {}
    return {
        "id": event.sequence,
        "sequence": event.sequence,
        "run_id": event.run_id,
        "event_type": event_type,
        "phase": event.phase if event.phase in {"context", "explore", "build", "execute", "evaluate", "repair", "package"} else None,
        "round": event.round if isinstance(event.round, int) and event.round >= 0 else None,
        "level": event.level if event.level in {"info", "warning", "error"} else "info",
        "summary": _text(event.summary),
        "payload": _project_payload(event_type, payload),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
