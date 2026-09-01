"""Integrity-bound review metadata for deterministic connector trials."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.discovery.plugin.artifact import (
    ConnectorArtifact,
    compute_connector_checksum,
    compute_connector_signature,
    load_connector_artifact,
)
from app.discovery.plugin.recipe import ResolvedPluginRecipe, resolve_plugin_recipe
from app.discovery.plugin.recipe import configured_plugin_config_hash
from app.discovery.plugin.contracts import (
    ConnectorContext,
    ConnectorInvocation,
    ConnectorManifest,
    ConnectorOutput,
    ConnectorRequest,
)
from app.discovery.quality_audit import (
    SourceQualityAudit,
    apply_quality_audit_to_method,
    audit_plugin_source_quality,
)
from app.discovery.redaction import redact_discovery_data, validate_discovery_data_bounds
from app.models import CrawlMethod, SiteDiscoveryRun
from app.discovery.sandbox.runtime import (
    canonical_connector_digest,
    connector_audit_projection,
    validate_runtime_attestation,
)


REVIEW_EVIDENCE_SCHEMA_VERSION = 1
MAX_REVIEW_EVIDENCE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ValidatedReviewArtifact:
    resolved: ResolvedPluginRecipe
    artifact: ConnectorArtifact
    evidence: dict[str, Any]


def artifact_evidence(artifact: ConnectorArtifact, *, kind: str) -> dict[str, Any]:
    manifest = artifact.manifest
    evidence = {
        "kind": kind,
        "connector_key": manifest.connector_key,
        "version": manifest.version,
        "checksum": manifest.checksum,
        "signature": artifact.signature,
        "runtime_version": manifest.runtime_version,
        "entry": manifest.entry,
        "entrypoint": manifest.entrypoint,
        "allowed_domains": list(manifest.allowed_domains),
    }
    # Legacy publication evidence did not carry this field.  Its manifest
    # default is deterministic, so only snapshot artifacts need an explicit
    # audit marker.
    if manifest.time_semantics == "snapshot":
        evidence["time_semantics"] = manifest.time_semantics
    return evidence


def bind_packaged_artifact_evidence(
    method_audit: dict[str, Any],
    *,
    trial: ConnectorArtifact,
    packaged: ConnectorArtifact,
    kind: str,
    packaging_run_id: int,
) -> dict[str, Any]:
    """Rebind only after host verification proves staged bytes/config are equivalent."""
    trial_manifest = trial.manifest
    packaged_manifest = packaged.manifest
    invariant_fields = (
        "recipe_type", "connector_key", "entry", "entrypoint", "runtime_version",
        "checksum", "allowed_domains", "time_semantics",
    )
    if any(
        getattr(trial_manifest, field_name) != getattr(packaged_manifest, field_name)
        for field_name in invariant_fields
    ):
        raise ValueError("packaged connector is not content-equivalent to the audited trial")
    recorded_trial = method_audit.get("artifact") or {}
    actual_trial = artifact_evidence(trial, kind=kind)
    for key in ("kind", "connector_key", "version", "checksum", "signature", "runtime_version"):
        if recorded_trial.get(key) != actual_trial[key]:
            raise ValueError(f"trial audit artifact mismatch before packaging: {key}")
    rebound = dict(method_audit)
    rebound["trial_artifact"] = dict(recorded_trial)
    rebound["artifact"] = {
        **artifact_evidence(packaged, kind=kind),
        "packaging_run_id": packaging_run_id,
    }
    return rebound


def attach_plugin_review_evidence(
    method: CrawlMethod,
    *,
    method_audit: dict[str, Any],
    quality_audit: SourceQualityAudit,
    quality_trial_evidence: list[dict[str, Any]],
) -> None:
    """Bind bounded audit evidence and existing quality fields to a pending method."""
    apply_quality_audit_to_method(method, quality_audit)
    document = {
        "schema_version": REVIEW_EVIDENCE_SCHEMA_VERSION,
        "kind": "plugin_review_evidence",
        "public_summary": {
            "low_frequency_exception_eligible": bool(
                method_audit.get("status") == "low_frequency_exception_required"
                and method_audit.get("low_frequency_exception_eligible") is True
            ),
        },
        "method_audit": method_audit,
        "quality_audit": {
            key: (value.isoformat() if isinstance(value, datetime) else value)
            for key, value in quality_audit.as_update_values().items()
        },
        "quality_trials": quality_trial_evidence,
    }
    validate_discovery_data_bounds(
        document,
        max_string_chars=MAX_REVIEW_EVIDENCE_BYTES,
        max_approx_bytes=MAX_REVIEW_EVIDENCE_BYTES,
    )
    method.review_note = json.dumps(
        redact_discovery_data(document),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def quality_audit_from_evidence(value: dict[str, Any]) -> SourceQualityAudit:
    """Rebuild the immutable quality result stored in a resumable checkpoint."""
    required = {
        "quality_score", "quality_grade", "quality_reason", "quality_sample_count",
        "density_score", "density_daily_avg", "density_weekly_avg", "quality_audit_status",
    }
    if not required.issubset(value):
        raise ValueError("checkpoint is missing plugin quality audit evidence")
    return SourceQualityAudit(
        quality_score=int(value["quality_score"]),
        quality_grade=str(value["quality_grade"]),
        quality_reason=str(value["quality_reason"]),
        quality_sample_count=int(value["quality_sample_count"]),
        density_score=int(value["density_score"]),
        density_daily_avg=float(value["density_daily_avg"]),
        density_weekly_avg=float(value["density_weekly_avg"]),
        quality_audit_status=str(value["quality_audit_status"]),
    )


def parse_plugin_review_evidence(method: CrawlMethod) -> dict[str, Any]:
    if (method.dsl_recipe or {}).get("recipe_type") != "python_plugin":
        raise ValueError("method is not a python_plugin")
    try:
        document = json.loads(method.review_note or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("plugin review evidence is missing or invalid") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != REVIEW_EVIDENCE_SCHEMA_VERSION
        or document.get("kind") != "plugin_review_evidence"
        or not isinstance(document.get("method_audit"), dict)
        or not isinstance(document.get("quality_audit"), dict)
        or not isinstance(document.get("quality_trials"), list)
    ):
        raise ValueError("plugin review evidence does not match the supported schema")
    return document


def load_validated_review_artifact(
    method: CrawlMethod,
    *,
    settings: Settings | None = None,
) -> ValidatedReviewArtifact:
    """Controlled validation boundary: corrupt evidence is never an API 500."""
    try:
        return _load_validated_review_artifact(method, settings=settings)
    except ValueError:
        raise
    except Exception as exc:  # filesystem/protocol/type corruption all fail closed
        raise ValueError("plugin artifact or review evidence validation failed") from exc


def _load_validated_review_artifact(
    method: CrawlMethod,
    *,
    settings: Settings | None = None,
) -> ValidatedReviewArtifact:
    """Validate DB recipe/config/signature against immutable artifact bytes."""
    settings = settings or get_settings()
    resolved = resolve_plugin_recipe(method.dsl_recipe or {})
    manifest = resolved.manifest
    if resolved.connector_kind == "shared":
        if manifest.connector_key != "wechat_sogou":
            raise ValueError("unapproved shared connector key")
        from app.discovery.wechat_plugin import load_wechat_sogou_artifact

        artifact = load_wechat_sogou_artifact(settings)
    else:
        artifact = load_connector_artifact(
            settings.discovery_connector_root,
            kind="sites",
            connector_key=manifest.connector_key,
            version=manifest.version,
        )
    if artifact.manifest != manifest:
        raise ValueError("review recipe Manifest does not match immutable artifact")
    if resolved.reviewed_signature != method.signature:
        raise ValueError("reviewed config/Manifest signature does not match the method")
    evidence = parse_plugin_review_evidence(method)
    method_audit = evidence.get("method_audit") or {}
    recorded_artifact = method_audit.get("artifact") or {}
    current = artifact_evidence(artifact, kind=resolved.connector_kind)
    for key in ("kind", "connector_key", "version", "checksum", "signature", "runtime_version"):
        if recorded_artifact.get(key) != current[key]:
            raise ValueError(f"plugin review evidence artifact binding mismatch: {key}")
    _validate_trial_attestations(method_audit, settings=settings)
    if resolved.connector_kind == "shared":
        trial_artifact = method_audit.get("trial_artifact") or recorded_artifact
        if (
            trial_artifact.get("trial_config_hash") != configured_plugin_config_hash(resolved.config)
            or trial_artifact.get("config_signature") != method.signature
        ):
            raise ValueError("shared connector trial configuration binding mismatch")
    return ValidatedReviewArtifact(resolved=resolved, artifact=artifact, evidence=evidence)


def _validate_trial_attestations(audit: dict[str, Any], *, settings: Settings) -> None:
    raw_attestations = audit.get("runtime_attestations") or []
    trial_artifact = audit.get("trial_artifact") or audit.get("artifact") or {}
    trial_run_id = trial_artifact.get("trial_run_id")
    if not isinstance(trial_run_id, int) or len(raw_attestations) != 2:
        raise ValueError("plugin audit lacks two runtime attestations")
    attestations = [validate_runtime_attestation(raw, settings=settings) for raw in raw_attestations]
    expected_image = settings.discovery_sandbox_runtime_images.get(trial_artifact.get("runtime_version"))
    for index, attestation in enumerate(attestations, start=1):
        if (
            not attestation.job_id.startswith(f"discovery-{trial_run_id}-")
            or attestation.connector_key != trial_artifact.get("connector_key")
            or attestation.connector_version != trial_artifact.get("version")
            or attestation.artifact_checksum != trial_artifact.get("checksum")
            or attestation.artifact_signature != trial_artifact.get("signature")
            or attestation.runtime_version != trial_artifact.get("runtime_version")
            or attestation.runtime_binary != str(Path(settings.discovery_sandbox_runsc_binary).resolve())
            or attestation.runsc_version != settings.discovery_sandbox_runsc_version
            or attestation.platform != settings.discovery_sandbox_platform
            or not expected_image
            or attestation.image_digest != expected_image
            or attestation.purpose != "primary_trial"
            or attestation.trial_index != index
            or not attestation.job_id.endswith(f"trial-{index}")
        ):
            raise ValueError("sandbox runtime attestation does not match audited trial")
    uniqueness = {
        (item.job_id, item.probe_nonce, item.started_nonce, item.completed_nonce, item.host_proof)
        for item in attestations
    }
    if len(uniqueness) != 2:
        raise ValueError("plugin independent trials reused runtime attestation identity")


def validate_plugin_approval(
    method: CrawlMethod,
    *,
    session: Session,
    exception_reason: str | None,
    reviewer: str,
) -> dict[str, Any]:
    """Validate artifact identity and successful sandbox execution before human approval."""
    validated = load_validated_review_artifact(method)
    evidence = validated.evidence
    method_audit = evidence["method_audit"]
    _validate_review_run_lineage(
        session,
        method=method,
        review_evidence=evidence,
        validated=validated,
    )
    evidence["human_approval"] = {
        "accepted": True,
        "reviewed_by": reviewer[:200],
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    validate_discovery_data_bounds(
        evidence,
        max_string_chars=MAX_REVIEW_EVIDENCE_BYTES,
        max_approx_bytes=MAX_REVIEW_EVIDENCE_BYTES,
    )
    method.review_note = json.dumps(
        redact_discovery_data(evidence), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return evidence


def _validate_review_run_lineage(
    session: Session,
    *,
    method: CrawlMethod,
    review_evidence: dict[str, Any],
    validated: ValidatedReviewArtifact,
) -> None:
    method_audit = review_evidence["method_audit"]
    trial_artifact = method_audit.get("trial_artifact") or {}
    packaged_artifact = method_audit.get("artifact") or {}
    origin_run_id = trial_artifact.get("trial_run_id")
    packaging_run_id = packaged_artifact.get("packaging_run_id")
    if not isinstance(origin_run_id, int) or not isinstance(packaging_run_id, int):
        raise ValueError("plugin review run lineage is incomplete")
    origin = session.get(SiteDiscoveryRun, origin_run_id)
    packaging = session.get(SiteDiscoveryRun, packaging_run_id)
    if (
        origin is None
        or packaging is None
        or packaging.status != "completed"
        or packaging.resulting_method_id != method.id
        or packaging.runtime_version != packaged_artifact.get("runtime_version")
    ):
        raise ValueError("plugin packaging run provenance is invalid")
    if origin.id != packaging.id and (
        packaging.trigger_type != "resume"
        or origin.status != "interrupted"
        or not origin.checkpoint_path
        or packaging.checkpoint_path != origin.checkpoint_path
    ):
        raise ValueError("plugin resume lineage does not reference its origin checkpoint")
    actual_artifact = artifact_evidence(validated.artifact, kind=validated.resolved.connector_kind)
    for key in ("kind", "connector_key", "version", "checksum", "signature", "runtime_version"):
        if packaged_artifact.get(key) != actual_artifact.get(key):
            raise ValueError(f"plugin packaging run does not match reviewed artifact: {key}")
    # Trial artifacts may use a temporary version number. Packaging assigns
    # the final per-site version and therefore also produces a new manifest
    # signature. Content identity is the connector key/checksum/runtime tuple.
    for key in ("kind", "connector_key", "checksum", "runtime_version"):
        if trial_artifact.get(key) != actual_artifact.get(key):
            raise ValueError(f"plugin trial run does not match reviewed artifact: {key}")
    # Approval is a human decision.  The checkpoint contains display data and
    # may be redacted during persistence, so it is deliberately not replayed
    # as a second deterministic audit here.  Artifact identity and the signed
    # successful runtime attestations were validated above.


def _without_audited_at(value: dict[str, Any]) -> dict[str, Any]:
    """Packaging may refresh only this ORM timestamp; every audit fact is immutable."""
    return {key: item for key, item in value.items() if key != "quality_audited_at"}


def _validate_checkpoint_trial_io(
    *,
    checkpoint: Any,
    method_audit: dict[str, Any],
    validated: ValidatedReviewArtifact,
    origin_run_id: int,
) -> None:
    # Legacy deep-audit helper retained only for reading old evidence tooling.
    # Human approval no longer calls it.
    from app.discovery.loop.evaluator import evaluate_connector_outputs

    execution = checkpoint.execution_result or {}
    raw_outputs = execution.get("trial_outputs") or []
    if len(raw_outputs) != 2:
        raise ValueError("origin checkpoint does not retain two complete trial outputs")
    outputs: list[ConnectorOutput] = []
    for raw in raw_outputs:
        if isinstance(raw, dict) and "complete" in raw:
            if raw.get("complete") is not True or not isinstance(raw.get("output"), dict):
                raise ValueError("origin checkpoint contains an incomplete trial output")
            raw = raw["output"]
        outputs.append(ConnectorOutput.model_validate(raw))
    raw_page = execution.get("pagination_output", execution.get("page_output"))
    if isinstance(raw_page, dict) and "complete" in raw_page:
        raw_page = raw_page.get("output") if raw_page.get("complete") is True else None
    pagination_output = ConnectorOutput.model_validate(raw_page) if raw_page else None
    supports_pagination = bool(
        execution.get("supports_pagination")
        if "supports_pagination" in execution
        else validated.resolved.config.get("max_pages", 1) > 1
    )
    attestations = [
        validate_runtime_attestation(raw)
        for raw in method_audit.get("runtime_attestations") or []
    ]
    recomputed_evaluation = evaluate_connector_outputs(
        outputs,
        reachable_urls=set(execution.get("reachable_urls") or []),
        supports_pagination=supports_pagination,
        pagination_output=pagination_output,
        require_url_accessibility=bool(execution.get("requires_url_verification", True)),
        enforce_fixed_listing_page=(
            method_audit.get("schema_version") == 2
            and validated.resolved.connector_kind != "shared"
        ),
        enforce_text_coverage=method_audit.get("schema_version") == 2,
        time_semantics=validated.artifact.manifest.time_semantics,
        snapshot_observed_at=[attestation.completed_at for attestation in attestations],
    ).as_dict()
    # Result URLs are data and may point at arbitrary publishers.  The
    # manifest allowlist is enforced only when the sandbox actually connects.
    if _canonical_json(recomputed_evaluation) != _canonical_json(
        _without_legacy_text_length_checks(method_audit.get("evaluator") or {})
    ):
        raise ValueError("deterministic evaluator evidence does not match persisted trial inputs")
    digests = method_audit.get("trial_digests") or []
    trial_artifact = method_audit.get("trial_artifact") or {}
    for index, (output, attestation, digest_evidence) in enumerate(
        zip(outputs, attestations, digests, strict=True), start=1
    ):
        output_digest = canonical_connector_digest(output)
        if (
            output_digest != attestation.output_sha256
            or output_digest != digest_evidence.get("output_sha256")
            or output_digest != digest_evidence.get("evaluator_input_sha256")
            or canonical_connector_digest(
                [item.model_dump(mode="json") for item in output.items]
            ) != digest_evidence.get("quality_input_sha256")
        ):
            raise ValueError("persisted trial output digest does not match its runtime proof")
        config = (
            {**validated.resolved.config, "page": 1, "max_pages": 1}
            if validated.resolved.connector_kind == "shared"
            else {"page": 1}
        )
        invocation = ConnectorInvocation(
            request=ConnectorRequest(entry=trial_artifact.get("entry"), config=config),
            context=ConnectorContext(
                run_id=origin_run_id,
                connector_key=str(trial_artifact.get("connector_key") or ""),
                connector_version=trial_artifact.get("version"),
                allowed_domains=tuple(trial_artifact.get("allowed_domains") or ()),
            ),
        )
        audit_invocation = ConnectorInvocation.model_validate(
            connector_audit_projection(invocation)
        )
        if canonical_connector_digest(audit_invocation) != attestation.invocation_sha256:
            raise ValueError(f"trial {index} invocation digest does not match reviewed input")
        review_trial = validated.evidence["quality_trials"][index - 1]
        recorded_quality = review_trial
        try:
            evaluation_time = datetime.fromisoformat(str(review_trial.get("evaluation_time")))
        except ValueError as exc:
            raise ValueError("trial quality evaluation time is invalid") from exc
        if evaluation_time.tzinfo is None or evaluation_time.utcoffset() != timezone.utc.utcoffset(evaluation_time):
            raise ValueError("trial quality evaluation time must be UTC")
        recomputed = audit_plugin_source_quality(
            items=[item.model_dump(mode="json") for item in output.items],
            now=evaluation_time,
        ).as_update_values()
        expected_quality = {
            "run": index,
            "item_count": len(output.items),
            "evaluation_time": review_trial.get("evaluation_time"),
            **{
                key: value
                for key, value in recomputed.items()
                if key != "quality_audited_at"
            },
        }
        recorded_without_audit_time = {
            key: value
            for key, value in recorded_quality.items()
            if key != "quality_audited_at"
        }
        if _canonical_json(expected_quality) != _canonical_json(recorded_without_audit_time):
            raise ValueError(f"trial {index} quality audit does not match persisted output")
    _validate_auxiliary_proofs(
        execution=execution,
        method_audit=method_audit,
        outputs=outputs,
        pagination_output=pagination_output,
        validated=validated,
        origin_run_id=origin_run_id,
        origin_round=int(checkpoint.round),
        supports_pagination=supports_pagination,
    )


def _validate_auxiliary_proofs(
    *,
    execution: dict[str, Any],
    method_audit: dict[str, Any],
    outputs: list[ConnectorOutput],
    pagination_output: ConnectorOutput | None,
    validated: ValidatedReviewArtifact,
    origin_run_id: int,
    origin_round: int,
    supports_pagination: bool,
) -> None:
    """Rebuild every pagination/verifier decision from host-signed projections."""
    raw_proofs = method_audit.get("auxiliary_proofs") or []
    checkpoint_proofs = execution.get("auxiliary_proofs") or []
    if _canonical_json(raw_proofs) != _canonical_json(checkpoint_proofs):
        raise ValueError("auxiliary runtime proofs differ from the origin checkpoint")
    requires_url_verification = bool(execution.get("requires_url_verification", True))
    expected_verifier_count = (
        len(outputs) + (1 if pagination_output is not None else 0)
        if validated.resolved.connector_kind == "shared" and requires_url_verification
        else 1 if validated.resolved.connector_kind != "shared"
        else 0
    )
    # Schema v2 requires ordinary website connectors to run config.page=2 so an
    # Agent cannot suppress the second execution by declaring a fixed listing.
    # Schema v1 evidence predates that rule and must remain runnable/revertible.
    requires_page_proof = (
        (method_audit.get("schema_version") == 2 and validated.resolved.connector_kind != "shared")
        or supports_pagination
    )
    expected_count = (1 if requires_page_proof else 0) + expected_verifier_count
    if len(raw_proofs) != expected_count or len(raw_proofs) > 4:
        raise ValueError("pagination or URL-verifier proof coverage is incomplete")

    parsed: list[tuple[Any, ConnectorInvocation, ConnectorOutput]] = []
    identities: set[tuple[str, str, str, str]] = set()
    for raw in raw_proofs:
        if not isinstance(raw, dict) or set(raw) != {
            "attestation", "audit_invocation", "audit_output"
        }:
            raise ValueError("auxiliary runtime proof schema is invalid")
        attestation = validate_runtime_attestation(raw["attestation"])
        try:
            invocation = ConnectorInvocation.model_validate_json(
                _canonical_json(raw["audit_invocation"]), strict=True
            )
            output = ConnectorOutput.model_validate_json(
                _canonical_json(raw["audit_output"]), strict=True
            )
        except Exception as exc:
            raise ValueError("auxiliary runtime proof payload is invalid") from exc
        if (
            canonical_connector_digest(invocation) != attestation.invocation_sha256
            or canonical_connector_digest(output) != attestation.output_sha256
            or attestation.trial_index is not None
            or not attestation.job_id.startswith(f"discovery-{origin_run_id}-")
            or attestation.connector_key != invocation.context.connector_key
            or attestation.connector_version != invocation.context.connector_version
        ):
            raise ValueError("auxiliary runtime attestation does not bind its payload")
        _validate_auxiliary_artifact(
            attestation=attestation,
            invocation=invocation,
            method_audit=method_audit,
        )
        identity = (
            attestation.job_id, attestation.probe_nonce,
            attestation.completed_nonce, attestation.host_proof,
        )
        if identity in identities:
            raise ValueError("auxiliary runtime proof identity was reused")
        identities.add(identity)
        parsed.append((attestation, invocation, output))

    offset = 0
    if requires_page_proof:
        attestation, invocation, output = parsed[0]
        offset = 1
        expected_config = (
            {**validated.resolved.config, "page": 2, "max_pages": 1}
            if validated.resolved.connector_kind == "shared"
            else {"page": 2}
        )
        trial_artifact = method_audit.get("trial_artifact") or {}
        expected = ConnectorInvocation(
            request=ConnectorRequest(entry=trial_artifact.get("entry"), config=expected_config),
            context=ConnectorContext(
                run_id=origin_run_id,
                connector_key=str(trial_artifact.get("connector_key") or ""),
                connector_version=trial_artifact.get("version"),
                allowed_domains=tuple(trial_artifact.get("allowed_domains") or ()),
            ),
        )
        expected_projection = ConnectorInvocation.model_validate(
            connector_audit_projection(expected)
        )
        expected_job = (
            f"discovery-{origin_run_id}-wechat-page-2"
            if validated.resolved.connector_kind == "shared"
            else f"discovery-{origin_run_id}-{origin_round}-page-2"
        )
        if (
            attestation.purpose != "pagination"
            or attestation.job_id != expected_job
            or _canonical_json(invocation.model_dump(mode="json"))
            != _canonical_json(expected_projection.model_dump(mode="json"))
            or pagination_output is None
            or canonical_connector_digest(output) != canonical_connector_digest(pagination_output)
        ):
            raise ValueError("pagination proof does not match the reviewed page-two execution")

    verifier_inputs = (
        [*outputs, *([pagination_output] if pagination_output is not None else [])]
        if validated.resolved.connector_kind == "shared" and requires_url_verification
        else [outputs[0]] if validated.resolved.connector_kind != "shared"
        else []
    )
    reachable: set[str] = set()
    for index, (source_output, proof) in enumerate(
        zip(verifier_inputs, parsed[offset:], strict=True), start=1
    ):
        attestation, invocation, verified = proof
        if validated.resolved.connector_kind == "shared":
            expected_urls = [
                item.url for item in source_output.items[:10]
                if _host_allowed(item.url, ("mp.weixin.qq.com",))
            ]
            expected_suffix = f"wechat-url-verify-{index}"
        else:
            # Website verification is one bounded batch across both trials.
            if index != 1:
                raise ValueError("website URL verifier proof count is invalid")
            allowed = tuple((method_audit.get("trial_artifact") or {}).get("allowed_domains") or ())
            expected_urls = list(dict.fromkeys(
                item.url for source in outputs for item in source.items
            ))[:10]
            expected_urls = [url for url in expected_urls if _host_allowed(url, allowed)]
            expected_suffix = "url-verify"
        expected_job = (
            f"discovery-{origin_run_id}-{expected_suffix}"
            if validated.resolved.connector_kind == "shared"
            else f"discovery-{origin_run_id}-{origin_round}-{expected_suffix}"
        )
        actual_urls = invocation.request.config.get("urls")
        if (
            attestation.purpose != "url_verifier"
            or attestation.job_id != expected_job
            or actual_urls != expected_urls
            or invocation.request.entry is None
            or any(item.url not in expected_urls for item in verified.items)
        ):
            raise ValueError("URL-verifier proof does not cover the reviewed sample set")
        reachable.update(item.url for item in verified.items)
        if validated.resolved.connector_kind != "shared":
            break
    if reachable != set(execution.get("reachable_urls") or []):
        raise ValueError("reachable URL evidence does not match verifier proofs")


def _validate_auxiliary_artifact(
    *,
    attestation: Any,
    invocation: ConnectorInvocation,
    method_audit: dict[str, Any],
) -> None:
    settings = get_settings()
    expected_image = settings.discovery_sandbox_runtime_images.get(attestation.runtime_version)
    if (
        attestation.runtime_binary
        != str(Path(settings.discovery_sandbox_runsc_binary).resolve())
        or attestation.runsc_version != settings.discovery_sandbox_runsc_version
        or attestation.platform != settings.discovery_sandbox_platform
        or not expected_image
        or attestation.image_digest != expected_image
    ):
        raise ValueError("auxiliary proof runtime does not match host configuration")
    if attestation.purpose == "pagination":
        trial = method_audit.get("trial_artifact") or {}
        if (
            attestation.connector_key != trial.get("connector_key")
            or attestation.connector_version != trial.get("version")
            or attestation.artifact_checksum != trial.get("checksum")
            or attestation.artifact_signature != trial.get("signature")
            or attestation.runtime_version != trial.get("runtime_version")
        ):
            raise ValueError("pagination proof did not execute the audited trial artifact")
        return
    if attestation.purpose != "url_verifier":
        raise ValueError("unsupported auxiliary proof purpose")
    if attestation.connector_key == "wechat_url_verifier":
        from app.discovery.wechat_plugin import _WECHAT_URL_VERIFIER_SOURCE

        expected_source = _WECHAT_URL_VERIFIER_SOURCE
    else:
        from app.discovery.loop.engine import _URL_VERIFIER_SOURCE

        expected_source = _URL_VERIFIER_SOURCE
    checksum = compute_connector_checksum(expected_source.encode("utf-8"))
    manifest = ConnectorManifest(
        connector_key=invocation.context.connector_key,
        version=invocation.context.connector_version,
        entry=str(invocation.request.entry or ""),
        runtime_version=attestation.runtime_version,
        checksum=checksum,
        allowed_domains=invocation.context.allowed_domains,
    )
    if (
        attestation.artifact_checksum != checksum
        or attestation.artifact_signature != compute_connector_signature(manifest)
    ):
        raise ValueError("URL-verifier proof did not execute the host-owned verifier artifact")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _without_legacy_text_length_checks(value: Any) -> Any:
    """Ignore the retired length score in already-packaged review evidence."""
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    for field in ("checks", "failures"):
        rows = normalized.get(field)
        if isinstance(rows, list):
            normalized[field] = [
                row for row in rows
                if not (
                    isinstance(row, dict)
                    and str(row.get("check") or "").endswith(".text_length")
                )
            ]
    return normalized


def _host_allowed(url: str, allowed_domains: tuple[str, ...]) -> bool:
    try:
        host = (urlsplit(url).hostname or "").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return False
    return host in allowed_domains


def validate_plugin_activation(method: CrawlMethod) -> None:
    """Revalidate immutable bytes and the completed human review before enabling."""
    validated = load_validated_review_artifact(method)
    if method.review_status != "approved":
        raise ValueError("plugin has not received human approval")
    approval = validated.evidence.get("human_approval") or {}
    if approval.get("accepted") is True:
        return
    # Backward compatibility for connector versions approved before explicit
    # human-approval metadata was persisted.
    method_audit = validated.evidence["method_audit"]
    if method_audit.get("passed") is True and method_audit.get("status") == "passed":
        return
    exception = validated.evidence.get("low_frequency_exception") or {}
    if not (
        method_audit.get("low_frequency_exception_eligible") is True
        and exception.get("accepted") is True
        and exception.get("deterministic_failure_scope") == "minimum_items_only"
    ):
        raise ValueError("plugin method audit has not passed or received a valid exception")


def plugin_review_summary(method: CrawlMethod) -> dict[str, Any] | None:
    """Return a bounded DB-only public summary without reading proof/key/artifact data."""
    if (method.dsl_recipe or {}).get("recipe_type") != "python_plugin":
        return None
    recipe = method.dsl_recipe or {}
    manifest = recipe.get("manifest") if isinstance(recipe.get("manifest"), dict) else recipe
    eligible = False
    if method.review_status == "pending" and isinstance(method.review_note, str):
        # Packaging writes this one safe boolean before the potentially large
        # proof.  Never parse or traverse the proof on a GET path.
        prefix = method.review_note[:2048]
        eligible = re.search(
            r'"public_summary":\{"low_frequency_exception_eligible":true\}',
            prefix,
        ) is not None
    return {
        "artifact_status": (
            "approved" if method.review_status == "approved" else "pending_review"
        ),
        "artifact": {
            key: manifest.get(key)
            for key in ("connector_key", "version", "runtime_version")
        },
        "method_audit_status": (
            "approved" if method.review_status == "approved" else "pending"
        ),
        "low_frequency_exception_eligible": eligible,
        "low_frequency_exception": None,
    }


def plugin_artifact_is_referenced(
    session: Session,
    *,
    excluded_method_id: int,
    connector_key: str,
    version: int,
    kind: str,
) -> bool:
    """Portable reference check used before deleting a never-approved site artifact."""
    for candidate in session.query(CrawlMethod).filter(CrawlMethod.id != excluded_method_id):
        try:
            resolved = resolve_plugin_recipe(candidate.dsl_recipe or {})
        except Exception:
            continue
        if (
            resolved.connector_kind == kind
            and resolved.manifest.connector_key == connector_key
            and resolved.manifest.version == version
        ):
            return True
    return False
