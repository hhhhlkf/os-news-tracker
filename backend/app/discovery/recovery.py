"""Process-restart recovery and the phase-4 handoff seam for resumed Discovery runs."""

from __future__ import annotations

import logging
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from threading import Lock

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.discovery.checkpoints import (
    CheckpointStore,
    DiscoveryCheckpoint,
    cleanup_orphaned_checkpoints,
)
from app.discovery.events import append_discovery_event
from app.discovery.plugin.artifact import load_connector_artifact
from app.discovery.plugin.contracts import ConnectorManifest
from app.discovery.redaction import redact_discovery_data, redact_discovery_text
from app.discovery.sandbox.runtime import SandboxRuntime, set_sandbox_cleanup_degraded
from app.llm.usage import (
    UsageScope,
    activate_usage_scope,
    deactivate_usage_scope,
    record_trusted_relay_usage,
)
from app.models import SiteDiscoveryRun


logger = logging.getLogger(__name__)
UNFINISHED_DISCOVERY_STATUSES = ("queued", "running", "repairing")
STALE_RUN_TIMEOUT_SECONDS = 1800
STALE_RUN_PATROL_INTERVAL_MINUTES = 5
ResumeDispatcher = Callable[[int, DiscoveryCheckpoint], None]
_dispatcher_lock = Lock()
_resume_dispatcher: ResumeDispatcher | None = None


def _db_wall_clock_expression(session: Session) -> object:
    if session.get_bind().dialect.name == "postgresql":
        return func.clock_timestamp()
    return func.current_timestamp()


def _db_wall_clock(session: Session) -> datetime:
    value = session.scalar(select(_db_wall_clock_expression(session)))
    if not isinstance(value, datetime):
        raise RuntimeError("database wall clock is unavailable")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _validated_package_recovery_checkpoint(
    *,
    run: SiteDiscoveryRun,
    marker: dict[str, object],
    store: CheckpointStore,
) -> DiscoveryCheckpoint:
    """Rebuild Package state only from a host-validated immutable trial identity."""
    package = marker.get("package")
    if not isinstance(package, dict):
        raise ValueError("package finalization identity is unavailable")
    required = {
        "connector_draft_path", "manifest_path", "connector_key", "version",
        "checksum", "signature", "runtime_version", "evaluation_result",
        "display_name", "force",
    }
    if not required.issubset(package):
        raise ValueError("package finalization identity is incomplete")
    if (
        not isinstance(package["connector_key"], str)
        or not isinstance(package["version"], int)
        or isinstance(package["version"], bool)
        or package["version"] < 1
        or not isinstance(package["checksum"], str)
        or len(package["checksum"]) != 64
        or not isinstance(package["signature"], str)
        or len(package["signature"]) != 64
        or not isinstance(package["runtime_version"], str)
    ):
        raise ValueError("package artifact identity fields are invalid")
    draft_path = package["connector_draft_path"]
    manifest_key = package["manifest_path"]
    if not isinstance(draft_path, str) or not isinstance(manifest_key, str):
        raise ValueError("package artifact paths are invalid")
    # DiscoveryCheckpoint path validators reject absolute/traversal paths;
    # resolve_workspace_path additionally enforces the host-owned run root.
    probe = DiscoveryCheckpoint(
        run_id=run.id,
        phase="package",
        round=max(0, int(marker.get("round") or run.round)),
        connector_draft_path=draft_path,
        manifest_path=manifest_key,
    )
    resolved_draft = store.resolve_workspace_path(run.id, probe.connector_draft_path or "")
    resolved_manifest = store.resolve_workspace_path(run.id, probe.manifest_path or "")
    manifest = ConnectorManifest.model_validate_json(
        resolved_manifest.read_text(encoding="utf-8")
    )
    trial_root = store.workspace_root / f"run-{run.id}" / "trial"
    artifact = load_connector_artifact(
        trial_root,
        kind="sites",
        connector_key=manifest.connector_key,
        version=manifest.version,
    )
    if artifact.connector_path.resolve() != resolved_draft.resolve():
        raise ValueError("package connector path does not match validated artifact")
    if artifact.manifest_path.resolve() != resolved_manifest.resolve():
        raise ValueError("package manifest path does not match validated artifact")
    if (
        package["connector_key"] != manifest.connector_key
        or package["version"] != manifest.version
        or package["checksum"] != manifest.checksum
        or package["signature"] != artifact.signature
        or package["runtime_version"] != manifest.runtime_version
        or manifest.runtime_version != get_settings().discovery_runtime_version
    ):
        raise ValueError("package artifact identity does not match immutable files")
    evaluation = package["evaluation_result"]
    if not isinstance(evaluation, dict):
        raise ValueError("package evaluation evidence is invalid")
    low_frequency = bool(
        ((evaluation.get("plugin_review") or {}).get("method_audit") or {}).get(
            "low_frequency_exception_eligible"
        )
    ) if isinstance(evaluation.get("plugin_review"), dict) else False
    if evaluation.get("passed") is not True and not low_frequency:
        raise ValueError("package evaluation was not host-approved")
    display_name = package["display_name"]
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 300:
        raise ValueError("package display name is invalid")
    if not isinstance(package["force"], bool):
        raise ValueError("package force flag is invalid")
    return DiscoveryCheckpoint(
        run_id=run.id,
        phase="package",
        round=probe.round,
        connector_draft_path=draft_path,
        manifest_path=manifest_key,
        processing_summary={"package_finalization": package},
        evaluation_result=evaluation,
        error=marker.get("error"),
        token_usage=max(run.llm_token_usage, int(marker.get("token_usage") or 0)),
        runtime_version=manifest.runtime_version,
    )


def _container_absent_or_stopped(runtime: SandboxRuntime, name: str) -> bool:
    result = runtime._docker(
        "inspect", "--format", "{{.State.Running}}", name,
        check=False, timeout=5.0,
    )
    if result.returncode == 0:
        return result.stdout.strip().lower() != b"true"
    error = result.stderr.decode("utf-8", errors="replace").lower()
    return "no such" in error or "not found" in error


def _recover_openhands_cleanup_pending() -> tuple[set[int], set[str]]:
    """Resume secret-free cleanup jobs before generic startup sandbox cleanup."""
    runtime = SandboxRuntime()
    store = CheckpointStore()
    still_pending: set[int] = set()
    protected_jobs: set[str] = set()
    db = SessionLocal()
    try:
        run_ids = list(db.scalars(select(SiteDiscoveryRun.id).where(
            SiteDiscoveryRun.status.in_(UNFINISHED_DISCOVERY_STATUSES),
        )))
        for run_id in run_ids:
            job_id: str | None = None
            claim_owner = uuid.uuid4().hex
            try:
                db_now = _db_wall_clock(db)
                db_clock = _db_wall_clock_expression(db)
                claimed = db.execute(
                    update(SiteDiscoveryRun)
                    .where(
                        SiteDiscoveryRun.id == run_id,
                        SiteDiscoveryRun.status.in_(UNFINISHED_DISCOVERY_STATUSES),
                        or_(
                            SiteDiscoveryRun.cleanup_claim_owner.is_(None),
                            and_(
                                SiteDiscoveryRun.cleanup_claim_owner.is_not(None),
                                SiteDiscoveryRun.cleanup_claim_expires_at < db_clock,
                            ),
                        ),
                    )
                    .values(
                        cleanup_claim_owner=claim_owner,
                        cleanup_claim_expires_at=db_now + timedelta(minutes=10),
                    )
                )
                db.commit()
                if claimed.rowcount != 1:
                    claimed_run = db.get(SiteDiscoveryRun, run_id)
                    if claimed_run is not None:
                        still_pending.add(run_id)
                        found_job = False
                        for item in reversed(list(claimed_run.node_trace or [])):
                            if (
                                isinstance(item, dict)
                                and item.get("type") == "openhands_cleanup_pending_metadata"
                                and isinstance(item.get("metadata"), dict)
                                and isinstance(item["metadata"].get("job_id"), str)
                            ):
                                protected_jobs.add(item["metadata"]["job_id"])
                                found_job = True
                                break
                        if not found_job:
                            protected_jobs.add("__cleanup_pending_scan_incomplete__")
                    continue
                run = db.get(SiteDiscoveryRun, run_id)
                if run is None:
                    continue
                checkpoint: DiscoveryCheckpoint | None = None
                if run.checkpoint_path:
                    try:
                        checkpoint = store.load(run.checkpoint_path, expected_run_id=run.id)
                    except Exception:
                        checkpoint = None
                metadata = (
                    checkpoint.processing_summary.get("cleanup_pending")
                    if checkpoint is not None
                    else None
                )
                if not isinstance(metadata, dict):
                    for item in reversed(list(run.node_trace or [])):
                        if (
                            isinstance(item, dict)
                            and item.get("type") == "openhands_cleanup_pending_metadata"
                            and isinstance(item.get("metadata"), dict)
                        ):
                            metadata = item["metadata"]
                            break
                if not isinstance(metadata, dict):
                    if str(run.error_message or "").startswith("OpenHands cleanup_pending"):
                        raise ValueError("cleanup-pending recovery metadata is unavailable")
                    db.execute(
                        update(SiteDiscoveryRun)
                        .where(
                            SiteDiscoveryRun.id == run_id,
                            SiteDiscoveryRun.cleanup_claim_owner == claim_owner,
                        )
                        .values(cleanup_claim_owner=None, cleanup_claim_expires_at=None)
                    )
                    db.commit()
                    continue
                if checkpoint is None:
                    checkpoint = DiscoveryCheckpoint(
                        run_id=run.id,
                        phase=run.phase or str(metadata.get("stage") or "cleanup"),
                        round=max(0, run.round),
                        processing_summary={"cleanup_pending": metadata},
                        token_usage=max(0, run.llm_token_usage),
                        runtime_version=run.runtime_version,
                    )
                required = {
                    "job_id", "agent_container", "proxy_container",
                    "relay_session_id", "acked_usage_sequences",
                }
                if not required.issubset(metadata):
                    raise ValueError("cleanup metadata is incomplete")
                job_id = metadata["job_id"]
                agent_name = metadata["agent_container"]
                proxy_name = metadata["proxy_container"]
                relay_session_id = metadata["relay_session_id"]
                if not all(
                    isinstance(value, str) and value
                    for value in (job_id, agent_name, proxy_name, relay_session_id)
                ):
                    raise ValueError("cleanup metadata identifiers are invalid")
                host_usage_total = metadata.get("host_usage_total", 0)
                if (
                    not isinstance(host_usage_total, int)
                    or isinstance(host_usage_total, bool)
                    or host_usage_total < 0
                ):
                    raise ValueError("cleanup metadata usage total is invalid")
                acked = {
                    value for value in metadata["acked_usage_sequences"]
                    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 80
                }
                resources_removed = bool(
                    metadata.get("resources_removed") or metadata.get("cleanup_confirmed")
                )
                usage_drained = bool(metadata.get("usage_drained") or resources_removed)
                scope = UsageScope(
                    context_type="discovery",
                    trigger_type=run.trigger_type,
                    stage=str(metadata.get("stage") or "build")[:40],
                    discovery_run_id=run.id,
                    total_tokens=max(
                        run.llm_token_usage,
                        checkpoint.token_usage,
                        host_usage_total,
                    ),
                )
                usage_token = activate_usage_scope(scope)
                try:
                    if not usage_drained:
                        # Stop both producers first.  Keep the relay container
                        # until every log record and the usage_drained phase are
                        # committed atomically under the durable claim.
                        for name in (agent_name, proxy_name):
                            runtime._docker("kill", name, check=False, timeout=5.0)
                        if not all(
                            _container_absent_or_stopped(runtime, name)
                            for name in (agent_name, proxy_name)
                        ):
                            raise RuntimeError("cleanup workloads could not be stopped")
                        logs = runtime._docker("logs", proxy_name, check=False, timeout=5.0)
                        if logs.returncode != 0:
                            raise RuntimeError("trusted relay logs are unavailable")
                        for line in logs.stdout.decode("utf-8", errors="replace").splitlines():
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(event, dict) or event.get("type") != "llm_relay":
                                continue
                            sequence = event.get("sequence")
                            if (
                                not isinstance(sequence, int)
                                or isinstance(sequence, bool)
                                or sequence < 1
                                or sequence > runtime.settings.discovery_agent_max_llm_requests
                            ):
                                raise ValueError("trusted relay recovery sequence is invalid")
                            usage_status = event.get("usage_status")
                            response_tokens = event.get("response_tokens")
                            prompt_tokens = event.get("prompt_tokens", 0)
                            completion_tokens = event.get("completion_tokens", 0)
                            if usage_status == "exact":
                                if (
                                    not isinstance(response_tokens, int)
                                    or isinstance(response_tokens, bool)
                                    or response_tokens < 0
                                ):
                                    raise ValueError("trusted relay exact usage is invalid")
                                for value in (prompt_tokens, completion_tokens):
                                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                                        raise ValueError("trusted relay token breakdown is invalid")
                                persisted_tokens = response_tokens
                            elif usage_status == "unknown":
                                persisted_tokens = 0
                                prompt_tokens = 0
                                completion_tokens = 0
                            else:
                                raise ValueError("trusted relay usage status is invalid")
                            if not record_trusted_relay_usage(
                                relay_session_id=relay_session_id,
                                relay_sequence=sequence,
                                total_tokens=persisted_tokens,
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                model=runtime.settings.llm_model,
                                stage=scope.stage,
                                usage_status=usage_status,
                                session=db,
                            ):
                                raise RuntimeError("trusted relay usage persistence failed")
                            acked.add(sequence)
                        transaction_total = db.scalar(
                            select(SiteDiscoveryRun.llm_token_usage).where(
                                SiteDiscoveryRun.id == run.id
                            )
                        )
                        if transaction_total is None:
                            raise LookupError("cleanup run disappeared during usage persistence")
                        updated_metadata = dict(metadata)
                        updated_metadata.update({
                            "acked_usage_sequences": sorted(acked),
                            "host_usage_total": int(transaction_total),
                            "usage_drained": True,
                            "resources_removed": False,
                            "cleanup_confirmed": False,
                        })
                        summary = dict(checkpoint.processing_summary)
                        summary["cleanup_pending"] = updated_metadata
                        checkpoint = checkpoint.model_copy(update={
                            "processing_summary": summary,
                            "token_usage": int(transaction_total),
                        })
                        store.save(checkpoint, session=db)
                        run.cleanup_claim_expires_at = _db_wall_clock(db) + timedelta(minutes=10)
                        # Usage rows, unknown audit markers, ack projection and
                        # the safe-to-delete phase become durable together.
                        db.commit()
                        authoritative_total = db.scalar(
                            select(SiteDiscoveryRun.llm_token_usage).where(
                                SiteDiscoveryRun.id == run.id
                            )
                        )
                        if authoritative_total is None:
                            raise LookupError("cleanup run disappeared after usage commit")
                        scope.total_tokens = int(authoritative_total)
                        metadata = updated_metadata
                        usage_drained = True
                finally:
                    deactivate_usage_scope(usage_token)
                if not resources_removed:
                    # A prior worker may have committed usage_drained and then
                    # crashed.  Re-establish/verify quiescence without needing
                    # logs before retrying idempotent resource removal.
                    for name in (agent_name, proxy_name):
                        runtime._docker("kill", name, check=False, timeout=5.0)
                    if not all(
                        _container_absent_or_stopped(runtime, name)
                        for name in (agent_name, proxy_name)
                    ):
                        raise RuntimeError("cleanup workloads could not be re-confirmed stopped")
                    failures = runtime.cleanup_job_resources(job_id)
                    if failures or not all(
                        _container_absent_or_stopped(runtime, name)
                        for name in (agent_name, proxy_name)
                    ):
                        raise RuntimeError("cleanup resources remain unconfirmed")
                    resources_removed = True
                run = db.get(SiteDiscoveryRun, run.id)
                assert run is not None
                if run.cleanup_claim_owner != claim_owner:
                    raise RuntimeError("cleanup recovery claim was lost")
                updated_metadata = dict(metadata)
                updated_metadata.update({
                    "usage_drained": True,
                    "resources_removed": True,
                    "cleanup_confirmed": True,
                    "host_usage_total": scope.total_tokens,
                })
                summary = dict(checkpoint.processing_summary)
                summary["cleanup_pending"] = updated_metadata
                checkpoint = checkpoint.model_copy(update={
                    "processing_summary": summary,
                    "token_usage": scope.total_tokens,
                })
                store.save(checkpoint, session=db)
                run.status = "failed"
                run.ended_at = datetime.now(timezone.utc)
                run.error_message = "OpenHands cleanup recovered after process restart."
                run.llm_token_usage = scope.total_tokens
                run.cleanup_claim_owner = None
                run.cleanup_claim_expires_at = None
                append_discovery_event(
                    run.id,
                    event_type="openhands_cleanup_recovered",
                    summary="启动恢复器已确认 Agent 与 relay 停止；cleanup-degraded 失败终态已收敛。",
                    phase=str(metadata.get("stage") or run.phase),
                    round_number=checkpoint.round,
                    level="error",
                    payload={"provenance": "host_cleanup_recovery", "cleanup_confirmed": True},
                    session=db,
                )
                db.commit()
            except Exception as exc:  # keep pending and protect its resources/logs
                db.rollback()
                still_pending.add(run_id)
                if isinstance(job_id, str):
                    protected_jobs.add(job_id)
                else:
                    protected_jobs.add("__cleanup_pending_scan_incomplete__")
                logger.error(
                    "OpenHands cleanup recovery remains pending run_id=%s error=%s",
                    run_id,
                    type(exc).__name__,
                )
        return still_pending, protected_jobs
    finally:
        db.close()


def register_resume_dispatcher(dispatcher: ResumeDispatcher) -> None:
    """Register the Single Agent Loop scheduler without importing legacy Graph code."""
    global _resume_dispatcher
    with _dispatcher_lock:
        _resume_dispatcher = dispatcher
    _dispatch_queued_resumes(dispatcher)


def _recover_openhands_finalization_pending() -> set[int]:
    """Converge post-cleanup checkpoint failures without losing their outcome."""
    pending: set[int] = set()
    package_ready: list[int] = []
    db = SessionLocal()
    store = CheckpointStore()
    try:
        run_ids = list(db.scalars(select(SiteDiscoveryRun.id).where(
            SiteDiscoveryRun.status.in_(UNFINISHED_DISCOVERY_STATUSES),
            SiteDiscoveryRun.error_message.like("OpenHands finalization_pending:%"),
        )))
        pending.update(run_ids)
        for run_id in run_ids:
            claim_owner = uuid.uuid4().hex
            try:
                db_now = _db_wall_clock(db)
                db_clock = _db_wall_clock_expression(db)
                claimed = db.execute(
                    update(SiteDiscoveryRun)
                    .where(
                        SiteDiscoveryRun.id == run_id,
                        SiteDiscoveryRun.status.in_(UNFINISHED_DISCOVERY_STATUSES),
                        SiteDiscoveryRun.error_message.like("OpenHands finalization_pending:%"),
                        or_(
                            SiteDiscoveryRun.cleanup_claim_owner.is_(None),
                            and_(
                                SiteDiscoveryRun.cleanup_claim_owner.is_not(None),
                                SiteDiscoveryRun.cleanup_claim_expires_at < db_clock,
                            ),
                        ),
                    )
                    .values(
                        cleanup_claim_owner=claim_owner,
                        cleanup_claim_expires_at=db_now + timedelta(minutes=10),
                    )
                )
                db.commit()
                if claimed.rowcount != 1:
                    continue
                run = db.get(SiteDiscoveryRun, run_id)
                if run is None:
                    continue
                marker = next(
                    (
                        item.get("metadata")
                        for item in reversed(list(run.node_trace or []))
                        if isinstance(item, dict)
                        and item.get("type") == "openhands_finalization_pending_metadata"
                        and isinstance(item.get("metadata"), dict)
                    ),
                    None,
                )
                if not isinstance(marker, dict):
                    raise ValueError("finalization metadata is unavailable")
                intended = marker.get("intended_status")
                if intended not in {"package", "cancelled", "failed"}:
                    raise ValueError("finalization outcome is invalid")
                if marker.get("compensation_confirmed", True) is not True:
                    raise ValueError("Package compensation remains unconfirmed")
                if intended == "package":
                    checkpoint = _validated_package_recovery_checkpoint(
                        run=run, marker=marker, store=store
                    )
                else:
                    try:
                        checkpoint = (
                            store.load(run.checkpoint_path, expected_run_id=run.id)
                            if run.checkpoint_path
                            else None
                        )
                    except Exception:
                        checkpoint = None
                    if checkpoint is None:
                        checkpoint = DiscoveryCheckpoint(
                            run_id=run.id,
                            phase=str(marker.get("phase") or run.phase or "cleanup"),
                            round=max(0, int(marker.get("round") or run.round)),
                            token_usage=max(
                                run.llm_token_usage, int(marker.get("token_usage") or 0)
                            ),
                            runtime_version=run.runtime_version,
                        )
                    checkpoint = checkpoint.model_copy(update={
                        "round": max(checkpoint.round, int(marker.get("round") or 0)),
                        "token_usage": max(
                            checkpoint.token_usage,
                            run.llm_token_usage,
                            int(marker.get("token_usage") or 0),
                        ),
                        "error": marker.get("error"),
                    })
                store.save(checkpoint, session=db)
                run.llm_token_usage = checkpoint.token_usage
                if intended == "package":
                    run.status = "queued"
                    run.phase = "package"
                    run.error_message = "OpenHands finalization_pending:package_ready"
                    run.cleanup_claim_owner = None
                    run.cleanup_claim_expires_at = None
                    pending.add(run.id)
                    package_ready.append(run.id)
                else:
                    run.status = intended
                    run.ended_at = datetime.now(timezone.utc)
                    error = marker.get("error")
                    run.error_message = redact_discovery_text(
                        str(error.get("message") if isinstance(error, dict) else error or "")
                    )[:4000]
                    append_discovery_event(
                        run.id,
                        event_type="run_cancelled" if intended == "cancelled" else "run_failed",
                        summary="OpenHands 延迟终态已从持久 finalization checkpoint 收敛。",
                        phase=checkpoint.phase,
                        round_number=checkpoint.round,
                        level="warning" if intended == "cancelled" else "error",
                        payload={"provenance": "host_finalization_recovery"},
                        session=db,
                    )
                    run.cleanup_claim_owner = None
                    run.cleanup_claim_expires_at = None
                    pending.discard(run.id)
                db.commit()
            except Exception as exc:
                db.rollback()
                pending.add(run_id)
                audit_db = SessionLocal()
                try:
                    audit_run = audit_db.scalar(
                        select(SiteDiscoveryRun)
                        .where(SiteDiscoveryRun.id == run_id)
                        .with_for_update()
                    )
                    entries = list(audit_run.node_trace or []) if audit_run is not None else []
                    for index in range(len(entries) - 1, -1, -1):
                        item = entries[index]
                        if (
                            isinstance(item, dict)
                            and item.get("type") == "openhands_finalization_pending_metadata"
                            and isinstance(item.get("metadata"), dict)
                        ):
                            item_metadata = dict(item["metadata"])
                            if item_metadata.get("recovery_validation_error") is None:
                                item_metadata["recovery_validation_error"] = type(exc).__name__
                                entries[index] = {**item, "metadata": item_metadata}
                                assert audit_run is not None
                                audit_run.node_trace = entries[-200:]
                                append_discovery_event(
                                    run_id,
                                    event_type="openhands_finalization_validation_failed",
                                    summary="延迟终态的宿主制品校验失败；保持 finalization_pending。",
                                    phase=str(item_metadata.get("phase") or "package"),
                                    round_number=max(0, int(item_metadata.get("round") or 0)),
                                    level="error",
                                    payload={
                                        "provenance": "host_finalization_recovery",
                                        "error_type": type(exc).__name__,
                                    },
                                    session=audit_db,
                                )
                            break
                    audit_db.commit()
                except Exception:
                    audit_db.rollback()
                    logger.error(
                        "failed to persist finalization validation audit run_id=%s", run_id
                    )
                finally:
                    audit_db.close()
                logger.error(
                    "OpenHands finalization recovery remains pending run_id=%s error=%s",
                    run_id,
                    type(exc).__name__,
                )
    finally:
        db.close()
    with _dispatcher_lock:
        dispatcher = _resume_dispatcher
    if dispatcher is not None:
        for run_id in package_ready:
            _claim_and_dispatch_resume(run_id, dispatcher)
    return pending


def unregister_resume_dispatcher(dispatcher: ResumeDispatcher) -> None:
    """Remove a dispatcher only when it is the currently registered instance."""
    global _resume_dispatcher
    with _dispatcher_lock:
        if _resume_dispatcher is dispatcher:
            _resume_dispatcher = None


def _dispatch_queued_resumes(dispatcher: ResumeDispatcher) -> None:
    """Hand persisted in-process resume requests to the newly registered Loop scheduler."""
    db = SessionLocal()
    try:
        queued_ids = list(
            db.scalars(
                select(SiteDiscoveryRun.id).where(
                    SiteDiscoveryRun.status == "queued",
                    SiteDiscoveryRun.checkpoint_path.is_not(None),
                    or_(
                        SiteDiscoveryRun.trigger_type == "resume",
                        SiteDiscoveryRun.error_message == "OpenHands finalization_pending:package_ready",
                    ),
                )
            )
        )
    finally:
        db.close()
    for run_id in queued_ids:
        _claim_and_dispatch_resume(run_id, dispatcher)


def dispatch_resumed_run_if_registered(run_id: int) -> bool:
    """Claim and dispatch only after the caller has committed the queued run."""
    with _dispatcher_lock:
        dispatcher = _resume_dispatcher
    return dispatcher is not None and _claim_and_dispatch_resume(run_id, dispatcher)


def _claim_and_dispatch_resume(run_id: int, dispatcher: ResumeDispatcher) -> bool:
    """Atomically claim queued→running so concurrent scanners dispatch exactly once."""
    db = SessionLocal()
    claim_owned = False
    try:
        claimed = db.execute(
            update(SiteDiscoveryRun)
            .where(
                SiteDiscoveryRun.id == run_id,
                SiteDiscoveryRun.status == "queued",
                or_(
                    SiteDiscoveryRun.trigger_type == "resume",
                    SiteDiscoveryRun.error_message == "OpenHands finalization_pending:package_ready",
                ),
            )
            .values(status="running", ended_at=None)
        )
        db.commit()
        if claimed.rowcount != 1:
            return False
        claim_owned = True
        run = db.get(SiteDiscoveryRun, run_id)
        if run is None or not run.checkpoint_path:
            raise ValueError("claimed resume run has no checkpoint")
        if run.trigger_type == "resume":
            run.error_message = None
            db.commit()
        checkpoint = CheckpointStore().load(run.checkpoint_path)
        dispatcher(run_id, checkpoint)
        try:
            append_discovery_event(
                run_id,
                event_type="resume_dispatched",
                summary="恢复运行已由 Single Agent Loop 调度器领取。",
                phase=run.phase,
                round_number=run.round,
                session=db,
            )
            db.commit()
        except Exception:  # noqa: BLE001 - accepted work must never be made dispatchable again
            db.rollback()
            logger.error("failed to persist resume dispatch event run_id=%s", run_id)
        return True
    except Exception as exc:  # noqa: BLE001 - persist a resumable terminal state
        db.rollback()
        safe_error = redact_discovery_text(str(exc))[:4000]
        if claim_owned:
            db.execute(
                update(SiteDiscoveryRun)
                .where(SiteDiscoveryRun.id == run_id, SiteDiscoveryRun.status == "running")
                .values(
                    status="interrupted",
                    ended_at=datetime.now(timezone.utc),
                    error_message=f"恢复任务调度失败：{safe_error}",
                )
            )
            db.commit()
        logger.error(
            "failed to dispatch queued Discovery resume run_id=%s error=%s",
            run_id,
            safe_error,
        )
        return False
    finally:
        db.close()


def recover_discovery_state() -> int:
    """Clean labeled sandbox resources and mark process-owned runs interrupted."""
    cleanup_pending_run_ids: set[int] = set()
    cleanup_pending_job_ids: set[str] = set()
    cleanup_pending_scan_failed = False
    finalization_pending_run_ids: set[int] = set()
    try:
        cleanup_pending_run_ids, cleanup_pending_job_ids = _recover_openhands_cleanup_pending()
        if "__cleanup_pending_scan_incomplete__" in cleanup_pending_job_ids:
            cleanup_pending_job_ids.discard("__cleanup_pending_scan_incomplete__")
            cleanup_pending_scan_failed = True
    except Exception as exc:
        cleanup_pending_scan_failed = True
        set_sandbox_cleanup_degraded(True)
        logger.error(
            "OpenHands cleanup recovery scan failed: %s",
            redact_discovery_text(str(exc)),
        )
    try:
        finalization_pending_run_ids = _recover_openhands_finalization_pending()
    except Exception as exc:
        logger.error(
            "OpenHands finalization recovery scan failed: %s",
            redact_discovery_text(str(exc)),
        )
    try:
        from app.discovery.fetch_runs import get_live_method_fetch_sandbox_job_ids

        cleanup_failures = (
            ({"resource_type": "cleanup_pending", "resource": "scan", "error": "scan_failed"},)
            if cleanup_pending_scan_failed
            else SandboxRuntime().cleanup_stale_resources(
                protected_job_ids={
                    *get_live_method_fetch_sandbox_job_ids(),
                    *cleanup_pending_job_ids,
                }
            )
        )
        if cleanup_pending_run_ids:
            cleanup_failures = (
                *cleanup_failures,
                {
                    "resource_type": "cleanup_pending",
                    "resource": "protected_jobs",
                    "error": "cleanup_unconfirmed",
                },
            )
        if cleanup_failures:
            set_sandbox_cleanup_degraded(True)
            logger.error(
                "Discovery sandbox startup cleanup was incomplete: %s",
                redact_discovery_data(cleanup_failures),
            )
        else:
            set_sandbox_cleanup_degraded(False)
    except Exception as exc:  # noqa: BLE001 - unavailable Docker must not become a fallback runtime
        set_sandbox_cleanup_degraded(True)
        logger.error(
            "Discovery sandbox startup cleanup could not inspect Docker resources: %s",
            redact_discovery_text(str(exc)),
        )

    db = SessionLocal()
    try:
        try:
            cleanup_orphaned_checkpoints(db)
        except Exception as exc:  # noqa: BLE001 - DB recovery must still converge run status
            logger.error(
                "Discovery checkpoint orphan cleanup failed: %s",
                redact_discovery_text(str(exc)),
            )
        runs = list(
            db.scalars(
                select(SiteDiscoveryRun)
                .where(SiteDiscoveryRun.status.in_(UNFINISHED_DISCOVERY_STATUSES))
                .with_for_update()
            )
        )
        interrupted_at = datetime.now(timezone.utc)
        interrupted_count = 0
        for run in runs:
            if run.id in cleanup_pending_run_ids or run.id in finalization_pending_run_ids:
                continue
            previous_status = run.status
            run.status = "interrupted"
            run.ended_at = interrupted_at
            run.error_message = "服务进程已重启，任务已中断；可从最近检查点恢复。"
            append_discovery_event(
                run.id,
                event_type="run_interrupted",
                summary="服务重启后运行被标记为 interrupted，旧容器不会被重新连接。",
                phase=run.phase,
                round_number=run.round,
                level="warning",
                payload={"previous_status": previous_status, "checkpoint_available": bool(run.checkpoint_path)},
                session=db,
            )
            interrupted_count += 1
        db.commit()
        return interrupted_count
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def reclaim_stale_runs(
    older_than_seconds: int | None = None,
    db: Session | None = None,
) -> int:
    """Fail stale in-process runs without importing the retired website Graph."""
    cleanup_pending_ids: set[int] = set()
    finalization_pending_ids: set[int] = set()
    try:
        cleanup_pending_ids, _ = _recover_openhands_cleanup_pending()
        finalization_pending_ids = _recover_openhands_finalization_pending()
    except Exception as exc:
        logger.error(
            "OpenHands cleanup watchdog scan failed: %s",
            redact_discovery_text(str(exc)),
        )
    own_session = db is None
    if own_session:
        db = SessionLocal()
    assert db is not None
    try:
        query = select(SiteDiscoveryRun).where(
            SiteDiscoveryRun.status.in_(("running", "repairing"))
        )
        if older_than_seconds is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
            query = query.where(SiteDiscoveryRun.started_at < cutoff)
        stale = list(db.scalars(query.with_for_update()))
        ended_at = datetime.now(timezone.utc)
        message = (
            "进程重启时回收：run 未正常结束（遗留 running）"
            if older_than_seconds is None
            else f"定时巡检回收：run 运行超过 {older_than_seconds}s 未完成，判超时"
        )
        reclaimed_count = 0
        lease_now = _db_wall_clock(db)
        for run in stale:
            if run.id in cleanup_pending_ids or run.id in finalization_pending_ids:
                continue
            if (
                run.cleanup_claim_owner is not None
                and run.cleanup_claim_expires_at is not None
                and run.cleanup_claim_expires_at >= lease_now
            ):
                continue
            if run.checkpoint_path:
                try:
                    checkpoint = CheckpointStore().load(
                        run.checkpoint_path, expected_run_id=run.id
                    )
                    if checkpoint.processing_summary.get("cleanup_pending"):
                        continue
                except Exception:
                    # Unknown checkpoint state must not authorize generic
                    # terminalization of a potentially live cleanup job.
                    continue
            run.status = "failed"
            run.ended_at = ended_at
            run.cleanup_claim_owner = None
            run.cleanup_claim_expires_at = None
            if not run.error_message:
                run.error_message = message
            reclaimed_count += 1
        db.commit()
        return reclaimed_count
    except BaseException:
        db.rollback()
        raise
    finally:
        if own_session:
            db.close()


def create_resumed_run(original_run_id: int, *, session: Session) -> SiteDiscoveryRun:
    """Flush a queued resume and its event; the route owns commit and later dispatch."""
    original = session.scalar(
        select(SiteDiscoveryRun)
        .where(SiteDiscoveryRun.id == original_run_id)
        .with_for_update()
    )
    if original is None:
        raise LookupError("run not found")
    if original.status != "interrupted":
        raise ValueError("only interrupted Discovery runs can be resumed")
    if not original.checkpoint_path:
        raise ValueError("interrupted run has no checkpoint")
    checkpoint = CheckpointStore().load(
        original.checkpoint_path,
        expected_run_id=original.id,
    )
    existing = session.scalar(
        select(SiteDiscoveryRun).where(
            SiteDiscoveryRun.trigger_type == "resume",
            SiteDiscoveryRun.checkpoint_path == original.checkpoint_path,
            SiteDiscoveryRun.status.in_(UNFINISHED_DISCOVERY_STATUSES),
        )
    )
    if existing is not None:
        return existing
    resumed = SiteDiscoveryRun(
        site_url=original.site_url,
        source_kind=original.source_kind,
        status="queued",
        trigger_type="resume",
        phase=checkpoint.phase,
        round=checkpoint.round,
        checkpoint_path=original.checkpoint_path,
        repair_method_id=original.repair_method_id,
        runtime_version=checkpoint.runtime_version or original.runtime_version,
        agent_budget=dict(original.agent_budget or {}),
        node_trace=list(original.node_trace or []),
        retry_count=original.retry_count,
        error_message="已进入恢复队列，等待 Single Agent Loop 调度器。",
    )
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        driver_connection = connection.connection.driver_connection
        if not driver_connection.in_transaction:
            # pysqlite otherwise starts with SAVEPOINT as the outermost DB
            # transaction, whose RELEASE would commit behind the caller's back.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        with session.begin_nested():
            session.add(resumed)
            session.flush()
            append_discovery_event(
                resumed.id,
                event_type="resume_queued",
                summary="已从最近检查点创建恢复运行；恢复时将使用新沙箱容器。",
                phase=resumed.phase,
                round_number=resumed.round,
                payload={"original_run_id": original.id, "checkpoint_available": True},
                session=session,
            )
        return resumed
    except IntegrityError:
        session.expire_all()
        existing = session.scalar(
            select(SiteDiscoveryRun).where(
                SiteDiscoveryRun.trigger_type == "resume",
                SiteDiscoveryRun.checkpoint_path == original.checkpoint_path,
                SiteDiscoveryRun.status.in_(UNFINISHED_DISCOVERY_STATUSES),
            )
        )
        if existing is None:
            raise
        return existing
