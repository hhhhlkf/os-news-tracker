"""Program-controlled Single Agent Loop for ordinary website Discovery."""

from __future__ import annotations

import difflib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Any, Callable
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import func, or_, select, update

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.discovery.cancel import (
    DiscoveryCancelled,
    activate_run,
    deactivate_run,
    ensure_not_cancelled,
    register_run,
    unregister_run,
)
from app.discovery.checkpoints import CheckpointStore, DiscoveryCheckpoint
from app.discovery.audit import audit_plugin_trial, sandbox_execution_proof
from app.discovery.agent_budget import normalize_agent_budget
from app.discovery.events import append_discovery_event
from app.discovery.loop.artifacts import (
    activate_packaging_method,
    create_packaging_method,
    discard_packaging_method,
    discard_staged_artifact,
    publish_staged_artifact,
    stage_pending_artifact,
    write_trial_artifact,
)
from app.discovery.loop.evaluator import (
    EvaluationResult,
    evaluate_connector_field_smoke,
    evaluate_connector_outputs,
)
from app.discovery.loop.explore_session import (
    explore_action_fingerprint,
    explore_action_summary,
    project_explore_observation,
    validate_explore_actions,
)
from app.discovery.loop.rag import DiscoveryExperienceRetriever, RagStage
from app.discovery.plugin.artifact import ConnectorArtifact, load_connector_artifact
from app.discovery.plugin.contracts import (
    ConnectorContext,
    ConnectorInvocation,
    ConnectorManifest,
    ConnectorRequest,
)
from app.discovery.plugin.errors import ConnectorErrorCode, ConnectorProtocolError
from app.discovery.plugin.review import (
    artifact_evidence,
    bind_packaged_artifact_evidence,
    quality_audit_from_evidence,
)
from app.discovery.plugin.recipe import resolve_plugin_recipe
from app.discovery.quality_audit import audit_plugin_trial_quality
from app.discovery.recovery import register_resume_dispatcher
from app.discovery.redaction import (
    DiscoveryDataBoundsError,
    redact_discovery_data,
    redact_discovery_text,
)
from app.discovery.sandbox.capacity import SandboxJobPriority
from app.discovery.sandbox.runtime import (
    SandboxExecution,
    SandboxExecutionResult,
    SandboxExploreSession,
    SandboxExploreSessionSpec,
    SandboxRuntime,
)
from app.discovery.sandbox.openhands_runtime import (
    OpenHandsAgentSession,
    OpenHandsAgentRuntimeError,
    OpenHandsAgentSessionSpec,
    open_openhands_agent_session,
    openhands_turn_prompt,
)
from app.llm.usage import (
    UsageScope,
    activate_usage_scope,
    deactivate_usage_scope,
    record_trusted_relay_usage,
)
from app.models import CrawlMethod, CrawlMethodRun, SiteDiscoveryRun


logger = logging.getLogger(__name__)
PHASES = ("context", "explore", "build", "execute", "evaluate", "repair", "package")
MAX_EXPLORE_CHECKPOINT_OBSERVATIONS = 6
MAX_EXPLORE_CHECKPOINT_BYTES = 512 * 1024
MAX_EXPLORE_LEDGER_DOCUMENTS = 12
MAX_EXPLORE_LEDGER_ENTRY_REDIRECTS = 12
MAX_EXPLORE_LEDGER_CANDIDATE_RECORDS = 12
MAX_EXPLORE_LEDGER_DETAIL_FIELD_EVIDENCE = 12
MIN_SUBSTANTIAL_TEXT_CHARS = 200
MAX_EXPLORE_SESSION_BATCHES = 2
# Only a Repair with different crawler source consumes this budget. A separate
# bounded streak stops repeated unchanged drafts before the shared deadline.
MAX_REPAIR_ATTEMPTS = 10
MAX_CONSECUTIVE_AGENT_DRAFT_CONTRACT_FAILURES = 3
MAX_CONSECUTIVE_REPAIR_NOOPS = 3
_URL_VERIFIER_SOURCE = r'''
from datetime import datetime, timezone
import httpx

async def crawl(request, context):
    urls = request.get("config", {}).get("urls") or []
    items = []
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers={"User-Agent": "os-news-tracker/discovery-verifier"}) as client:
        for url in urls[:10]:
            try:
                response = await client.get(url)
                if response.status_code < 400:
                    items.append({"title": "reachable", "url": url, "published_at": datetime.now(timezone.utc).isoformat(), "summary": "reachable " * 30})
            except Exception as exc:
                print('{"event":"url_check_failed","url":' + __import__('json').dumps(url) + ',"error":' + __import__('json').dumps(str(exc)[:300]) + '}', file=__import__('sys').stderr)
    return {"items": items, "stats": {"discovered_count": len(items)}}
'''.strip()

class _LoopDeadline:
    """Hard monotonic budget beginning at the first acquired sandbox capacity."""

    def __init__(self, maximum_seconds: float, *, consumed_seconds: float = 0.0) -> None:
        self.maximum_seconds = min(1800.0, max(0.1, maximum_seconds))
        self.consumed_seconds = max(0.0, consumed_seconds)
        self.deadline: float | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self.deadline is None:
                remaining = self.maximum_seconds - self.consumed_seconds
                if remaining <= 0:
                    raise TimeoutError("Discovery Loop 已达到 30 分钟执行预算。")
                self.deadline = time.monotonic() + remaining

    def pause_for_capacity(self) -> float:
        """Freeze the consumed active time while the next sandbox waits in queue."""
        with self._lock:
            if self.deadline is not None:
                remaining = max(0.0, self.deadline - time.monotonic())
                self.consumed_seconds = min(
                    self.maximum_seconds,
                    self.maximum_seconds - remaining,
                )
                self.deadline = None
            return self.consumed_seconds

    def remaining(self) -> float:
        with self._lock:
            if self.deadline is None:
                return max(0.0, self.maximum_seconds - self.consumed_seconds)
            return max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        if self.remaining() <= 0:
            raise TimeoutError("Discovery Loop 已达到 30 分钟执行预算。")

    def elapsed(self) -> float:
        with self._lock:
            if self.deadline is None:
                return self.consumed_seconds
            return min(self.maximum_seconds, self.maximum_seconds - max(0.0, self.deadline - time.monotonic()))


class _PackageFailureAfterCompensation(RuntimeError):
    """Package failed and every staged filesystem/DB side effect was removed."""


class _PackageCompensationFailed(RuntimeError):
    """Package failed and host compensation could not be confirmed."""


class _TerminalClaimUnavailable(RuntimeError):
    """No proof that this process acquired the durable terminal claim."""


def _db_wall_clock_expression(session: Any) -> Any:
    """Use a transaction-volatile wall clock on PostgreSQL and a SQLite fallback."""
    if session.get_bind().dialect.name == "postgresql":
        return func.clock_timestamp()
    return func.current_timestamp()


def _db_wall_clock(session: Any) -> datetime:
    value = session.scalar(select(_db_wall_clock_expression(session)))
    if not isinstance(value, datetime):
        raise RuntimeError("database wall clock is unavailable")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _run_with_durable_terminal_claim(
    run_id: int,
    action: Callable[[str, threading.Event], None],
) -> None:
    """Lease one terminal side effect across processes and heartbeat it."""
    owner = uuid.uuid4().hex
    stopped = threading.Event()
    claim_lost = threading.Event()

    def acquire() -> bool:
        db = SessionLocal()
        try:
            db_now = _db_wall_clock(db)
            db_clock = _db_wall_clock_expression(db)
            claimed = db.execute(
                update(SiteDiscoveryRun)
                .where(
                    SiteDiscoveryRun.id == run_id,
                    SiteDiscoveryRun.status.in_(("queued", "running", "repairing")),
                    or_(
                        SiteDiscoveryRun.cleanup_claim_owner.is_(None),
                        SiteDiscoveryRun.cleanup_claim_expires_at < db_clock,
                    ),
                )
                .values(
                    cleanup_claim_owner=owner,
                    cleanup_claim_expires_at=db_now + timedelta(minutes=10),
                )
            )
            db.commit()
            return claimed.rowcount == 1
        except Exception:
            db.rollback()
            return False
        finally:
            db.close()

    def renew() -> bool:
        db = SessionLocal()
        try:
            db_now = _db_wall_clock(db)
            db_clock = _db_wall_clock_expression(db)
            renewed = db.execute(
                update(SiteDiscoveryRun)
                .where(
                    SiteDiscoveryRun.id == run_id,
                    SiteDiscoveryRun.status.in_(("queued", "running", "repairing")),
                    SiteDiscoveryRun.cleanup_claim_owner == owner,
                    SiteDiscoveryRun.cleanup_claim_expires_at > db_clock,
                )
                .values(cleanup_claim_expires_at=db_now + timedelta(minutes=10))
            )
            db.commit()
            return renewed.rowcount == 1
        except Exception:
            db.rollback()
            return False
        finally:
            db.close()

    if not acquire():
        raise _TerminalClaimUnavailable(
            "Discovery terminal claim is held by another host or acquisition is unknown"
        )

    def heartbeat() -> None:
        while not stopped.wait(30.0):
            if not renew():
                claim_lost.set()
                logger.error("lost Discovery terminal claim run_id=%s", run_id)
                return

    thread = threading.Thread(target=heartbeat, daemon=True, name=f"discovery-finalize-{run_id}")
    thread.start()
    succeeded = False
    failure: BaseException | None = None
    try:
        action(owner, claim_lost)
        succeeded = True
    except BaseException as exc:
        failure = exc
        raise
    finally:
        stopped.set()
        thread.join()
        if succeeded or isinstance(failure, _PackageFailureAfterCompensation):
            db = SessionLocal()
            try:
                db.execute(
                    update(SiteDiscoveryRun)
                    .where(
                        SiteDiscoveryRun.id == run_id,
                        SiteDiscoveryRun.cleanup_claim_owner == owner,
                    )
                    .values(cleanup_claim_owner=None, cleanup_claim_expires_at=None)
                )
                db.commit()
            except Exception:
                db.rollback()
                logger.warning("terminal claim release will rely on expiry run_id=%s", run_id)
            finally:
                db.close()


def _assert_terminal_claim(
    run_id: int,
    owner: str,
    claim_lost: threading.Event,
) -> None:
    """Fence Package immediately when heartbeat or durable ownership is lost."""
    if claim_lost.is_set():
        raise RuntimeError("Discovery terminal claim heartbeat was lost")
    db = SessionLocal()
    try:
        row = db.execute(
            select(
                SiteDiscoveryRun.cleanup_claim_owner,
                SiteDiscoveryRun.cleanup_claim_expires_at,
            ).where(
                SiteDiscoveryRun.id == run_id,
                SiteDiscoveryRun.status.in_(("queued", "running", "repairing")),
                SiteDiscoveryRun.cleanup_claim_expires_at > _db_wall_clock_expression(db),
            )
        ).one_or_none()
        if (
            row is None
            or row.cleanup_claim_owner != owner
            or row.cleanup_claim_expires_at is None
        ):
            claim_lost.set()
            raise RuntimeError("Discovery terminal claim fencing check failed")
    finally:
        db.close()


def _persist_idempotent_terminal_outcome(
    *,
    run_id: int,
    status: str,
    error_message: str,
    event_type: str,
    summary: str,
    phase: str,
    round_number: int,
    payload: dict[str, Any] | None,
    level: str,
    node_trace: list[dict[str, Any]],
    token_usage: int,
) -> None:
    """Atomically persist one terminal audit event and its matching run state."""
    db = SessionLocal()
    try:
        terminal_run = db.scalar(
            select(SiteDiscoveryRun)
            .where(SiteDiscoveryRun.id == run_id)
            .with_for_update()
        )
        if terminal_run is None:
            raise LookupError("terminal Discovery run is missing")
        if terminal_run.status == status and terminal_run.ended_at is not None:
            db.rollback()
            return
        if terminal_run.status in {"completed", "cancelled", "failed"}:
            raise RuntimeError("Discovery run already has a conflicting terminal state")
        append_discovery_event(
            run_id,
            event_type=event_type,
            summary=summary,
            phase=phase,
            round_number=max(0, round_number),
            level=level,
            payload=payload,
            session=db,
        )
        terminal_run.status = status
        terminal_run.node_trace = list(node_trace)
        terminal_run.error_message = error_message
        terminal_run.llm_token_usage = token_usage
        terminal_run.ended_at = datetime.now(timezone.utc)
        terminal_run.cleanup_claim_owner = None
        terminal_run.cleanup_claim_expires_at = None
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _persist_minimal_finalization_pending(
    *,
    run_id: int,
    intended_status: str,
    error: dict[str, Any],
    compensation_confirmed: bool,
) -> bool:
    """Persist recovery ownership unless another durable owner/marker is authoritative."""
    db = SessionLocal()
    try:
        run = db.scalar(
            select(SiteDiscoveryRun).where(SiteDiscoveryRun.id == run_id).with_for_update()
        )
        if run is None:
            raise LookupError("finalization-pending run is missing")
        if run.status in {"completed", "cancelled", "failed"} and run.ended_at is not None:
            db.rollback()
            return True
        db_now = _db_wall_clock(db)
        claim_expiry = run.cleanup_claim_expires_at
        if claim_expiry is not None and claim_expiry.tzinfo is None:
            claim_expiry = claim_expiry.replace(tzinfo=timezone.utc)
        if run.cleanup_claim_owner is not None and (
            claim_expiry is None
            or claim_expiry > db_now
        ):
            # A live (or conservatively unbounded) owner may be between staging
            # and publication.  Only it, or recovery after expiry, may change
            # the durable recovery marker.
            db.rollback()
            return False
        existing_marker = next(
            (
                item.get("metadata")
                for item in reversed(list(run.node_trace or []))
                if isinstance(item, dict)
                and item.get("type") == "openhands_finalization_pending_metadata"
                and isinstance(item.get("metadata"), dict)
            ),
            None,
        )
        if isinstance(existing_marker, dict) and (
            existing_marker.get("compensation_confirmed") is False
        ):
            # Unconfirmed cleanup is an authoritative fail-closed state.  Only
            # the dedicated compensation recovery path may prove cleanup and
            # replace it; generic init/run failures must not erase that proof
            # obligation, even after the prior owner's lease expires.
            db.rollback()
            return False
        if isinstance(existing_marker, dict) and (
            existing_marker.get("intended_status") == "package"
            or isinstance(existing_marker.get("package"), dict)
        ):
            # Artifact identity is the only restart-safe proof that Package may
            # be resumed.  A generic failure path must never erase it.
            db.rollback()
            return False
        entries = [
            item for item in list(run.node_trace or [])
            if not (
                isinstance(item, dict)
                and item.get("type") == "openhands_finalization_pending_metadata"
            )
        ]
        entries.append({
            "type": "openhands_finalization_pending_metadata",
            "metadata": {
                "protocol": 1,
                "intended_status": intended_status,
                "phase": run.phase or "package",
                "round": max(0, run.round),
                "token_usage": max(0, run.llm_token_usage),
                "error": redact_discovery_data(error),
                "package": None,
                "compensation_confirmed": compensation_confirmed,
            },
        })
        run.node_trace = entries[-200:]
        run.error_message = f"OpenHands finalization_pending:{intended_status}"
        db.commit()
        return True
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _ensure_minimal_finalization_pending(**kwargs: Any) -> None:
    """Never let a terminal-claim failure escape without durable recovery state."""
    attempt = 0
    while True:
        attempt += 1
        try:
            persisted = _persist_minimal_finalization_pending(**kwargs)
            if not persisted:
                logger.info(
                    "finalization marker retained for current owner/recovery run_id=%s",
                    kwargs.get("run_id"),
                )
            return
        except Exception:
            if attempt == 1 or attempt % 10 == 0:
                logger.exception("finalization-pending persistence retry attempt=%s", attempt)
            time.sleep(min(0.25 * attempt, 5.0))


class _FieldSmokeFailure(Exception):
    """Carry deterministic smoke evidence into the next bounded Repair turn."""

    def __init__(self, evaluation: EvaluationResult) -> None:
        self.evaluation = evaluation
        failed_checks = ", ".join(
            str(item.get("check") or "unknown")
            for item in evaluation.failures[:5]
        )
        super().__init__(f"field smoke validation failed: {failed_checks or 'unknown check'}")


class _RepairAttemptLimitReached(RuntimeError):
    """Stop a failed connector from consuming the whole loop budget in Repair."""

    def __init__(self, *, attempts: int, limit: int, failures: list[dict[str, Any]]) -> None:
        self.attempts = attempts
        self.limit = limit
        self.failures = failures
        failed_checks = ", ".join(
            str(item.get("check") or "unknown")
            for item in failures[:5]
            if isinstance(item, dict)
        )
        super().__init__(
            f"Repair attempt limit reached ({attempts}/{limit}); "
            f"latest deterministic failures: {failed_checks or 'unknown'}"
        )


class _RepairNoOpLimitReached(RuntimeError):
    """Stop after repeated Repairs cannot propose a source change for known failures."""

    def __init__(self, *, attempts: int, limit: int) -> None:
        self.attempts = attempts
        self.limit = limit
        super().__init__(
            f"Repair returned unchanged crawler.py {attempts} consecutive times "
            f"(limit {limit}); stopping to avoid a no-op loop"
        )


class _AgentDraftContractLimitReached(RuntimeError):
    """Stop a malformed Agent response from repeatedly consuming the Loop."""

    def __init__(self, *, attempts: int, limit: int) -> None:
        self.attempts = attempts
        self.limit = limit
        super().__init__(
            f"Agent draft contract failed {attempts} consecutive times "
            f"(limit {limit}); stopping to avoid a no-op loop"
        )


class WebsiteLoopEngine:
    """One Agent, deterministic phases, bounded rounds, durable evidence and review gate."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        sandbox: SandboxRuntime | None = None,
        retriever: DiscoveryExperienceRetriever | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.sandbox = sandbox or SandboxRuntime(settings=self.settings)
        self.retriever = retriever or DiscoveryExperienceRetriever()
        self.checkpoints = checkpoint_store or CheckpointStore(self.settings)
        self._run_jobs: dict[int, set[str]] = {}
        self._deadlines: dict[int, _LoopDeadline] = {}
        # Timings are measured against _LoopDeadline rather than wall clock so
        # sandbox-capacity queue time is never reported as active execution.
        # The values are intentionally per-run only; durable public events keep
        # the resulting bounded numbers, not monotonic timestamps.
        self._phase_started_elapsed: dict[int, tuple[str, float]] = {}
        self._capacity_wait_started_at: dict[int, float] = {}
        self._job_lock = threading.Lock()

    def start(
        self,
        site_url: str,
        *,
        force: bool = True,
        name: str | None = None,
        agent_budget: dict[str, int] | None = None,
    ) -> int:
        budget_snapshot = normalize_agent_budget(agent_budget)
        db = SessionLocal()
        try:
            run = SiteDiscoveryRun(
                site_url=site_url,
                source_kind="website",
                status="queued",
                trigger_type="manual",
                phase="context",
                round=0,
                runtime_version=self.settings.discovery_runtime_version,
                agent_budget=budget_snapshot,
            )
            db.add(run)
            db.commit()
            run_id = run.id
        finally:
            db.close()
        register_run(run_id)
        threading.Thread(
            target=self.run,
            kwargs={"run_id": run_id, "site_url": site_url, "force": force, "name": name},
            daemon=True,
            name=f"discovery-loop-{run_id}",
        ).start()
        return run_id

    def start_repair(self, method_id: int) -> int:
        """Start one bounded repair from the currently approved site artifact."""
        db = SessionLocal()
        try:
            method = db.get(CrawlMethod, method_id)
            if method is None:
                raise ValueError(f"method {method_id} not found")
            if method.review_status != "approved" or method.status != "active":
                raise ValueError("automatic repair requires an approved active method")
            resolved = resolve_plugin_recipe(method.dsl_recipe)
            if resolved.connector_kind != "sites":
                raise ValueError("website repair cannot load a shared connector")
            artifact = load_connector_artifact(
                self.settings.discovery_connector_root,
                kind="sites",
                connector_key=resolved.manifest.connector_key,
                version=resolved.manifest.version,
            )
            if (
                artifact.manifest != resolved.manifest
                or artifact.signature != resolved.reviewed_signature
                or method.signature != resolved.reviewed_signature
            ):
                raise ValueError("approved repair source no longer matches review evidence")
            source = artifact.connector_path.read_text(encoding="utf-8")
            failures = list(
                db.scalars(
                    select(CrawlMethodRun)
                    .where(
                        CrawlMethodRun.method_id == method.id,
                        CrawlMethodRun.status == "failed",
                    )
                    .order_by(CrawlMethodRun.id.desc())
                    .limit(3)
                )
            )
            failure_summary = redact_discovery_text(
                "; ".join(
                    json.dumps(
                        item.failure_evidence
                        or {"message": item.error_message or "formal plugin execution failed"},
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    for item in reversed(failures)
                )
            )[:4000]
            run = SiteDiscoveryRun(
                site_url=method.entry_url,
                source_kind="website",
                status="queued",
                trigger_type="repair",
                phase="context",
                round=0,
                repair_method_id=method.id,
                runtime_version=self.settings.discovery_runtime_version,
                agent_budget=normalize_agent_budget(),
            )
            db.add(run)
            db.commit()
            run_id = run.id
            site_url, name = method.entry_url, method.domain
        finally:
            db.close()
        register_run(run_id)
        threading.Thread(
            target=self.run,
            kwargs={
                "run_id": run_id,
                "site_url": site_url,
                "force": True,
                "name": name,
                "initial_source": source,
                "initial_allowed_domains": artifact.manifest.allowed_domains,
                "initial_error": {
                    "type": "FormalExecutionFailureStreak",
                    "message": failure_summary,
                },
            },
            daemon=True,
            name=f"discovery-loop-repair-{run_id}",
        ).start()
        return run_id

    def resume(self, run_id: int, checkpoint: DiscoveryCheckpoint) -> None:
        register_run(run_id)
        threading.Thread(
            target=self.run,
            kwargs={
                "run_id": run_id,
                "site_url": self._site_url(run_id),
                "force": True,
                "name": None,
                "resume_checkpoint": checkpoint,
            },
            daemon=True,
            name=f"discovery-loop-resume-{run_id}",
        ).start()

    def cancel(self, run_id: int) -> bool:
        with self._job_lock:
            jobs = tuple(self._run_jobs.get(run_id, ()))
        cancelled = False
        for job_id in jobs:
            cancelled = self.sandbox.cancel(job_id) or cancelled
        return cancelled

    def current_timing_payload(self, run_id: int) -> dict[str, Any]:
        """Return a live, queue-excluding timing snapshot for cancellation."""
        return self._current_timing_payload(run_id, phase="context")

    def run(
        self,
        *,
        run_id: int,
        site_url: str,
        force: bool,
        name: str | None,
        resume_checkpoint: DiscoveryCheckpoint | None = None,
        initial_source: str | None = None,
        initial_allowed_domains: tuple[str, ...] | None = None,
        initial_error: dict[str, Any] | None = None,
    ) -> None:
        usage = UsageScope(
            context_type="discovery",
            trigger_type="resume" if resume_checkpoint else self._trigger_type(run_id),
            stage="single_agent_loop",
            discovery_run_id=run_id,
            total_tokens=resume_checkpoint.token_usage if resume_checkpoint else 0,
        )
        cancel_token = None
        usage_token = None
        try:
            cancel_token = activate_run(run_id)
            usage_token = activate_usage_scope(usage)
            self._run_impl(
                run_id=run_id,
                site_url=site_url,
                force=force,
                name=name,
                resume_checkpoint=resume_checkpoint,
                initial_source=initial_source,
                initial_allowed_domains=initial_allowed_domains,
                initial_error=initial_error,
                usage=usage,
            )
        except DiscoveryCancelled as exc:
            try:
                _run_with_durable_terminal_claim(
                    run_id,
                    lambda _owner, _lost: _persist_idempotent_terminal_outcome(
                        run_id=run_id, status="cancelled", error_message=str(exc),
                        event_type="run_cancelled", summary="Discovery 初始化阶段已安全取消。",
                        phase="context", round_number=0,
                        payload={"provenance": "host_terminal"}, level="warning",
                        node_trace=[], token_usage=usage.total_tokens,
                    ),
                )
            except Exception as terminal_exc:
                _ensure_minimal_finalization_pending(
                    run_id=run_id,
                    intended_status="cancelled",
                    error=_structured_error(terminal_exc),
                    compensation_confirmed=True,
                )
        except Exception as exc:  # initialization failures converge here
            terminal_error = _structured_error(exc)
            terminal_error["stage"] = "initialization"
            safe_error = terminal_error["message"]
            logger.exception("website Discovery initialization failed run_id=%s", run_id)
            try:
                _run_with_durable_terminal_claim(
                    run_id,
                    lambda _owner, _lost: _persist_idempotent_terminal_outcome(
                        run_id=run_id, status="failed", error_message=safe_error,
                        event_type="run_failed", summary="Single Agent Loop 初始化失败并停止。",
                        phase="context", round_number=0,
                        payload={"error": terminal_error}, level="error",
                        node_trace=[], token_usage=usage.total_tokens,
                    ),
                )
            except Exception as terminal_exc:
                _ensure_minimal_finalization_pending(
                    run_id=run_id,
                    intended_status="failed",
                    error=_structured_error(terminal_exc),
                    compensation_confirmed=True,
                )
        finally:
            with self._job_lock:
                self._run_jobs.pop(run_id, None)
                self._deadlines.pop(run_id, None)
                self._phase_started_elapsed.pop(run_id, None)
                self._capacity_wait_started_at.pop(run_id, None)
            if usage_token is not None:
                deactivate_usage_scope(usage_token)
            if cancel_token is not None:
                deactivate_run(cancel_token)
            unregister_run(run_id)

    def _run_impl(
        self,
        *,
        run_id: int,
        site_url: str,
        force: bool,
        name: str | None,
        resume_checkpoint: DiscoveryCheckpoint | None,
        initial_source: str | None,
        initial_allowed_domains: tuple[str, ...] | None,
        initial_error: dict[str, Any] | None,
        usage: UsageScope,
    ) -> None:
        trace: list[dict[str, Any]] = []
        agent_budget = self._agent_budget(run_id)
        deadline = _LoopDeadline(
            self.settings.discovery_loop_max_seconds,
            consumed_seconds=float(resume_checkpoint.elapsed_seconds if resume_checkpoint else 0.0),
        )
        with self._job_lock:
            self._deadlines[run_id] = deadline
        if resume_checkpoint is not None and resume_checkpoint.elapsed_seconds > 0:
            deadline.start()
        previous_source = initial_source or self._load_resume_source(resume_checkpoint)
        trusted_initial_domains = tuple(_exact_entry_domain(site_url))
        if initial_allowed_domains is not None:
            if not self._is_automatic_repair_run(run_id):
                raise ValueError("initial allowed domains are trusted only for automatic artifact repair")
            trusted_initial_domains = ConnectorManifest.validate_allowed_domains(
                initial_allowed_domains
            )
        elif resume_checkpoint is not None and resume_checkpoint.manifest_path:
            resume_trial = self._load_resume_trial(resume_checkpoint)
            trusted_initial_domains = ConnectorManifest.validate_allowed_domains(
                resume_trial.manifest.allowed_domains
            )
        entry_domain = _exact_entry_domain(site_url)[0]
        if entry_domain not in trusted_initial_domains:
            raise ValueError("trusted repair/resume domains omit the exact entry hostname")
        previous_evaluation = (resume_checkpoint.evaluation_result or {}) if resume_checkpoint else {}
        if initial_error is not None:
            previous_evaluation = {
                "passed": False,
                "failures": [
                    {
                        "check": "formal_execution_failure_streak",
                        "passed": False,
                        **initial_error,
                    }
                ],
            }
        resume_low_frequency_exception = bool(
            resume_checkpoint
            and ((previous_evaluation.get("plugin_review") or {}).get("method_audit") or {}).get(
                "low_frequency_exception_eligible"
            )
        )
        resume_package = bool(
            resume_checkpoint
            and resume_checkpoint.phase in {"evaluate", "package"}
            and (
                previous_evaluation.get("passed")
                or resume_low_frequency_exception
            )
        )
        start_round = max(
            1,
            resume_checkpoint.round if resume_package and resume_checkpoint else (
                resume_checkpoint.round + 1 if resume_checkpoint else 1
            ),
        )
        trial: ConnectorArtifact | None = None
        rag_references: list[dict[str, Any]] = list(
            resume_checkpoint.rag_references if resume_checkpoint else []
        )
        build_references: list[dict[str, Any]] = [
            reference for reference in rag_references
            if reference.get("experience_kind") == "build_pattern"
        ]
        exploration: dict[str, Any] = dict(resume_checkpoint.processing_summary if resume_checkpoint else {})
        repair_attempts = _resume_repair_attempts(
            resume_checkpoint,
            is_automatic_repair=self._is_automatic_repair_run(run_id),
        )
        exploration["effective_repair_attempts"] = repair_attempts
        agent_draft_contract_failures = _bounded_agent_draft_contract_failures(
            exploration.get("consecutive_agent_draft_contract_failures")
        )
        consecutive_repair_noops = _bounded_consecutive_repair_noops(
            exploration.get("consecutive_repair_noops")
        )
        last_error: dict[str, Any] | None = initial_error or _resume_failure_error(
            resume_checkpoint,
            previous_evaluation,
        )
        last_tool_evidence: list[dict[str, Any]] = list(
            resume_checkpoint.tool_evidence if resume_checkpoint else []
        )
        last_execution_result = resume_checkpoint.execution_result if resume_checkpoint else None
        last_code_diff = resume_checkpoint.code_diff if resume_checkpoint else None
        last_evaluation: EvaluationResult | None = None
        agent_session: OpenHandsAgentSession | None = None
        cleanup_retry_scheduled = False
        package_side_effect_started = False
        active_phase = "context"
        active_round = max(0, start_round - 1)
        try:
            if resume_package and resume_checkpoint is not None:
                trial = self._load_resume_trial(resume_checkpoint)
                resumed_package = resume_checkpoint.processing_summary.get(
                    "package_finalization"
                )
                if not isinstance(resumed_package, dict):
                    resumed_package = {}
                self._mark_sandbox_started(run_id)
                active_phase = "package"
                active_round = resume_checkpoint.round
                self._phase(
                    run_id,
                    "package",
                    resume_checkpoint.round,
                    trace,
                    "从已通过确定性验收的检查点继续封装。",
                )
                resume_package_invoked = False

                def package_under_claim(
                    claim_owner: str,
                    claim_lost: threading.Event,
                ) -> None:
                    nonlocal resume_package_invoked
                    resume_package_invoked = True
                    self._package_trial(
                        run_id=run_id,
                        trial=trial,
                        display_name=str(
                            resumed_package.get("display_name")
                            or name
                            or (urlsplit(site_url).hostname or "website")
                        )[:300],
                        force=(
                            resumed_package.get("force")
                            if isinstance(resumed_package.get("force"), bool)
                            else force
                        ),
                        round_number=resume_checkpoint.round,
                        trace=trace,
                        token_usage=usage.total_tokens,
                        deadline=deadline,
                        plugin_review=previous_evaluation.get("plugin_review") or {},
                        expected_claim_owner=claim_owner,
                        claim_lost=claim_lost,
                    )

                try:
                    _run_with_durable_terminal_claim(
                        run_id,
                        package_under_claim,
                    )
                except Exception as package_exc:
                    package_error = _structured_error(package_exc)
                    compensation_confirmed = (
                        isinstance(package_exc, _PackageFailureAfterCompensation)
                        or (
                            not isinstance(package_exc, _TerminalClaimUnavailable)
                            and not resume_package_invoked
                        )
                    )
                    if compensation_confirmed:
                        try:
                            _run_with_durable_terminal_claim(
                                run_id,
                                lambda _owner, _lost: _persist_idempotent_terminal_outcome(
                                    run_id=run_id,
                                    status="failed",
                                    error_message=package_error["message"],
                                    event_type="run_failed",
                                    summary="延迟 Package 失败且已确认补偿，运行安全停止。",
                                    phase="package",
                                    round_number=resume_checkpoint.round,
                                    payload={"error": package_error},
                                    level="error",
                                    node_trace=trace,
                                    token_usage=usage.total_tokens,
                                ),
                            )
                            return
                        except Exception as terminal_exc:
                            package_error = _structured_error(terminal_exc)
                    _ensure_minimal_finalization_pending(
                        run_id=run_id,
                        intended_status="failed",
                        error=package_error,
                        compensation_confirmed=compensation_confirmed,
                    )
                return
            active_phase = "context"
            active_round = max(0, start_round - 1)
            self._phase(run_id, "context", max(0, start_round - 1), trace, "准备目标与受限经验上下文。")
            domain = (urlsplit(site_url).hostname or "").lower()
            explore_references = self._retrieve_experiences(
                run_id=run_id,
                stage="explore",
                event_phase="context",
                round_number=start_round - 1,
                domain=domain,
                technical_features=exploration.get("technical_features") or {},
                query=f"{site_url} website news pagination article API RSS XML HTML SSR",
            )
            rag_references = _merge_rag_references(rag_references, explore_references)

            active_phase = "explore"
            active_round = start_round - 1
            self._phase(
                run_id,
                "explore",
                start_round - 1,
                trace,
                "启动保留式 OpenHands SDK gVisor workspace；Explore 与 Build 在同一会话连续完成。",
            )
            agent_job_id = f"discovery-{run_id}-openhands-agent"
            with self._job_lock:
                self._run_jobs.setdefault(run_id, set()).add(agent_job_id)
            self._sandbox_capacity_queued(
                run_id,
                deadline,
                phase="explore",
                round_number=start_round - 1,
                summary="OpenHands Agent Runtime 正在等待 gVisor 沙箱容量，执行预算暂停计时。",
                extra_payload={"agent_runtime": "openhands-sdk", "sdk_version": "1.43.1"},
            )
            self._mark_waiting_for_sandbox(run_id)
            openhands_event_lock = threading.Lock()

            def persist_openhands_event(event: dict[str, Any]) -> None:
                nonlocal last_tool_evidence
                with openhands_event_lock:
                    projected_event = {
                        key: value for key, value in event.items() if key != "token_usage"
                    }
                    safe_event = {
                        **redact_discovery_data(projected_event),
                        "provenance": "agent_untrusted",
                    }
                    last_tool_evidence = [*last_tool_evidence[-99:], safe_event]
                    self._event(
                        run_id,
                        "openhands_action_event",
                        str(safe_event.get("summary") or "OpenHands 工具事件已记录。")[:500],
                        active_phase,
                        active_round,
                        safe_event,
                        level="warning" if safe_event.get("status") == "error" else "info",
                    )
                    self._save_round_checkpoint(
                        run_id=run_id,
                        phase=active_phase,
                        round_number=active_round,
                        trial=trial,
                        rag_references=rag_references,
                        exploration=exploration,
                        execution_result=last_execution_result,
                        tool_evidence=last_tool_evidence,
                        evaluation_result=previous_evaluation,
                        code_diff=last_code_diff,
                        error=last_error,
                        elapsed_seconds=self._active_elapsed(run_id),
                        token_usage=usage.total_tokens,
                    )

            relay_usage_stage = "explore"
            terminal_claim_owner = uuid.uuid4().hex
            terminal_claim_lost = threading.Event()
            terminal_claim_acquired = False

            def acquire_terminal_claim() -> bool:
                nonlocal terminal_claim_owner, terminal_claim_acquired
                if terminal_claim_acquired:
                    return renew_terminal_claim()
                candidate = uuid.uuid4().hex
                db = SessionLocal()
                try:
                    db_now = _db_wall_clock(db)
                    db_clock = _db_wall_clock_expression(db)
                    claimed = db.execute(
                        update(SiteDiscoveryRun)
                        .where(
                            SiteDiscoveryRun.id == run_id,
                            SiteDiscoveryRun.status.in_(("queued", "running", "repairing")),
                            or_(
                                SiteDiscoveryRun.cleanup_claim_owner.is_(None),
                                SiteDiscoveryRun.cleanup_claim_expires_at < db_clock,
                            ),
                        )
                        .values(
                            cleanup_claim_owner=candidate,
                            cleanup_claim_expires_at=db_now + timedelta(minutes=10),
                        )
                    )
                    db.commit()
                    if claimed.rowcount == 1:
                        terminal_claim_owner = candidate
                        terminal_claim_acquired = True
                        terminal_claim_lost.clear()
                        return True
                    return False
                except Exception:
                    db.rollback()
                    return False
                finally:
                    db.close()

            def renew_terminal_claim() -> bool:
                if not terminal_claim_acquired or terminal_claim_lost.is_set():
                    return False
                db = SessionLocal()
                try:
                    db_now = _db_wall_clock(db)
                    db_clock = _db_wall_clock_expression(db)
                    renewed = db.execute(
                        update(SiteDiscoveryRun)
                        .where(
                            SiteDiscoveryRun.id == run_id,
                            SiteDiscoveryRun.status.in_(("queued", "running", "repairing")),
                            SiteDiscoveryRun.cleanup_claim_owner == terminal_claim_owner,
                            SiteDiscoveryRun.cleanup_claim_expires_at > db_clock,
                        )
                        .values(cleanup_claim_expires_at=db_now + timedelta(minutes=10))
                    )
                    db.commit()
                    return renewed.rowcount == 1
                except Exception:
                    db.rollback()
                    return False
                finally:
                    db.close()

            def release_terminal_claim() -> None:
                nonlocal terminal_claim_acquired
                db = SessionLocal()
                try:
                    db.execute(
                        update(SiteDiscoveryRun)
                        .where(
                            SiteDiscoveryRun.id == run_id,
                            SiteDiscoveryRun.cleanup_claim_owner == terminal_claim_owner,
                        )
                        .values(cleanup_claim_owner=None, cleanup_claim_expires_at=None)
                    )
                    db.commit()
                    terminal_claim_acquired = False
                except Exception:
                    db.rollback()
                    logger.warning("terminal claim release will rely on expiry run_id=%s", run_id)
                finally:
                    db.close()

            def run_with_terminal_claim(action: Callable[[], None]) -> None:
                if not (
                    renew_terminal_claim() if terminal_claim_acquired else acquire_terminal_claim()
                ):
                    terminal_claim_lost.set()
                    raise RuntimeError("OpenHands terminal claim is held by another host")
                terminal_claim_lost.clear()
                stopped = threading.Event()

                def heartbeat() -> None:
                    while not stopped.wait(30.0):
                        if not renew_terminal_claim():
                            terminal_claim_lost.set()
                            logger.error("lost OpenHands terminal claim run_id=%s", run_id)
                            return

                heartbeat_thread = threading.Thread(
                    target=heartbeat,
                    daemon=True,
                    name=f"discovery-openhands-terminal-claim-{run_id}",
                )
                heartbeat_thread.start()
                succeeded = False
                failure: BaseException | None = None
                try:
                    action()
                    succeeded = True
                except BaseException as exc:
                    failure = exc
                    raise
                finally:
                    stopped.set()
                    heartbeat_thread.join()
                    if succeeded or isinstance(failure, _PackageFailureAfterCompensation):
                        release_terminal_claim()

            def drain_openhands_relay_usage(stage: str, round_number: int) -> bool:
                if agent_session is None:
                    return True
                try:
                    relay_events = agent_session.read_unacknowledged_trusted_relay_usage()
                except Exception as exc:  # best effort; never replace the primary Loop outcome
                    logger.warning(
                        "failed to drain trusted OpenHands relay usage run_id=%s error=%s",
                        run_id,
                        type(exc).__name__,
                    )
                    return False
                fully_drained_and_acked = True
                for relay_event in relay_events:
                    sequence = relay_event.get("sequence")
                    if not isinstance(sequence, int):
                        fully_drained_and_acked = False
                        continue
                    exact = relay_event.get("usage_status") == "exact"
                    exact_tokens = relay_event.get("response_tokens")
                    prompt_tokens = relay_event.get("prompt_tokens")
                    completion_tokens = relay_event.get("completion_tokens")
                    persisted = (
                        isinstance(exact_tokens, int) if exact else exact_tokens is None
                    ) and record_trusted_relay_usage(
                            relay_session_id=agent_session.relay_session_id,
                            relay_sequence=sequence,
                            total_tokens=exact_tokens if exact else 0,
                            prompt_tokens=prompt_tokens if exact else 0,
                            completion_tokens=completion_tokens if exact else 0,
                            model=self.settings.llm_model,
                            stage=stage,
                            usage_status="exact" if exact else "unknown",
                        )
                    if not persisted:
                        logger.warning(
                            "trusted OpenHands relay usage remains unacknowledged run_id=%s sequence=%s",
                            run_id,
                            sequence,
                        )
                        fully_drained_and_acked = False
                        continue
                    # The idempotent LlmUsageEvent is the durable ack fact for
                    # both exact and unknown usage.  Agent memory is updated
                    # only after that transaction commits.
                    agent_session.ack_trusted_relay_usage(sequence)
                    if exploration.get("cleanup_pending"):
                        exploration["cleanup_pending"].update(
                            agent_session.cleanup_recovery_metadata()
                        )
                        exploration["cleanup_pending"]["host_usage_total"] = usage.total_tokens
                    try:
                        audit_event = append_discovery_event(
                            run_id,
                            event_type="openhands_relay_usage",
                            summary=(
                                "可信 LLM relay 已记录 provider 返回的精确 token usage。"
                                if exact
                                else "可信 LLM relay 未收到 provider usage；仅记录 unknown，不估算 token。"
                            ),
                            phase=stage,
                            round_number=round_number,
                            payload={
                                **relay_event,
                                "model": self.settings.llm_model,
                                "provenance": "host_trusted_relay",
                            },
                        )
                        if audit_event is None:
                            raise RuntimeError("trusted relay usage audit was not persisted")
                        self._save_round_checkpoint(
                            run_id=run_id,
                            phase=active_phase,
                            round_number=active_round,
                            trial=trial,
                            rag_references=rag_references,
                            exploration=exploration,
                            execution_result=last_execution_result,
                            tool_evidence=last_tool_evidence,
                            evaluation_result=previous_evaluation,
                            code_diff=last_code_diff,
                            error=last_error,
                            elapsed_seconds=self._active_elapsed(run_id),
                            token_usage=usage.total_tokens,
                        )
                    except Exception as exc:  # keep cleanup and the primary outcome authoritative
                        logger.warning(
                            "failed to persist trusted OpenHands relay audit/checkpoint run_id=%s sequence=%s error=%s",
                            run_id,
                            sequence,
                            type(exc).__name__,
                        )
                return fully_drained_and_acked

            def persist_cleanup_stage(
                session: OpenHandsAgentSession,
                *,
                stage: str,
                round_number: int,
                usage_drained: bool,
                resources_removed: bool,
            ) -> bool:
                """Persist a secret-free cleanup phase before destructive removal."""
                metadata = {
                    **session.cleanup_recovery_metadata(),
                    "stage": stage,
                    "round": max(0, round_number),
                    "host_usage_total": usage.total_tokens,
                    "usage_drained": usage_drained,
                    "resources_removed": resources_removed,
                    "cleanup_confirmed": resources_removed,
                }
                exploration["cleanup_pending"] = metadata
                if not save_terminal_usage_checkpoint(last_error):
                    return False
                db = SessionLocal()
                try:
                    cleanup_run = db.get(SiteDiscoveryRun, run_id)
                    if cleanup_run is None:
                        return False
                    entries = [
                        item for item in list(cleanup_run.node_trace or [])
                        if not (
                            isinstance(item, dict)
                            and item.get("type") == "openhands_cleanup_pending_metadata"
                        )
                    ]
                    entries.append({
                        "type": "openhands_cleanup_pending_metadata",
                        "metadata": redact_discovery_data(metadata),
                    })
                    cleanup_run.node_trace = entries[-200:]
                    db.commit()
                    return True
                except Exception:
                    db.rollback()
                    logger.warning("failed to persist OpenHands cleanup phase run_id=%s", run_id)
                    return False
                finally:
                    db.close()

            def close_openhands_before_terminal(stage: str, round_number: int) -> bool:
                """Make the final usage retry and close before terminal persistence."""
                nonlocal agent_session
                session = agent_session
                if session is None:
                    return True
                last_error_type = "RuntimeError"
                for attempt in range(1, 4):
                    if not (
                        renew_terminal_claim()
                        if terminal_claim_acquired
                        else acquire_terminal_claim()
                    ):
                        last_error_type = "CleanupClaimUnavailable"
                        continue
                    if session.cleanup_confirmed:
                        if persist_cleanup_stage(
                            session,
                            stage=stage,
                            round_number=round_number,
                            usage_drained=True,
                            resources_removed=True,
                        ):
                            agent_session = None
                            return True
                        last_error_type = "CleanupConfirmationPersistenceFailed"
                        continue
                    try:
                        if not persist_cleanup_stage(
                            session,
                            stage=stage,
                            round_number=round_number,
                            usage_drained=False,
                            resources_removed=False,
                        ):
                            last_error_type = "CleanupClaimProjectionFailed"
                            continue
                        session.stop_for_usage_drain()
                    except Exception as exc:
                        last_error_type = type(exc).__name__
                        continue
                    if not drain_openhands_relay_usage(stage, round_number):
                        last_error_type = "RelayUsageNotFullyAcknowledged"
                        logger.warning(
                            "OpenHands relay usage remains unacknowledged run_id=%s attempt=%s",
                            run_id,
                            attempt,
                        )
                        continue
                    if not persist_cleanup_stage(
                        session,
                        stage=stage,
                        round_number=round_number,
                        usage_drained=True,
                        resources_removed=False,
                    ):
                        last_error_type = "CleanupStagePersistenceFailed"
                        continue
                    try:
                        session.remove_after_usage_drain()
                        if not persist_cleanup_stage(
                            session,
                            stage=stage,
                            round_number=round_number,
                            usage_drained=True,
                            resources_removed=True,
                        ):
                            last_error_type = "CleanupConfirmationPersistenceFailed"
                            continue
                        agent_session = None
                        return True
                    except Exception as exc:  # retry cleanup without losing the live reference
                        last_error_type = type(exc).__name__
                        logger.warning(
                            "failed to confirm OpenHands cleanup run_id=%s attempt=%s error=%s",
                            run_id,
                            attempt,
                            last_error_type,
                        )
                try:
                    append_discovery_event(
                        run_id,
                        event_type="openhands_cleanup_degraded",
                        summary="OpenHands Agent/relay 清理未获宿主确认；运行已 fail closed。",
                        phase=stage,
                        round_number=max(0, round_number),
                        level="error",
                        payload={
                            "provenance": "host_cleanup_verification",
                            "cleanup_confirmed": False,
                            "error_type": last_error_type,
                        },
                    )
                except Exception:
                    logger.exception("failed to persist OpenHands cleanup degradation run_id=%s", run_id)
                return False

            def schedule_cleanup_pending(
                stage: str,
                round_number: int,
                intended_status: str,
                primary_error: str,
            ) -> None:
                """Keep the run non-terminal until a host worker confirms cleanup."""
                nonlocal cleanup_retry_scheduled
                if cleanup_retry_scheduled or agent_session is None:
                    return
                cleanup_retry_scheduled = True
                session = agent_session
                safe_primary_error = redact_discovery_text(primary_error)[:1000]
                exploration["cleanup_pending"] = {
                    **session.cleanup_recovery_metadata(),
                    "stage": stage,
                    "round": max(0, round_number),
                    "intended_terminal_status": intended_status,
                    "primary_error": safe_primary_error,
                    "host_usage_total": usage.total_tokens,
                }
                save_terminal_usage_checkpoint({
                    "type": "OpenHandsCleanupPending",
                    "message": "OpenHands cleanup is awaiting host confirmation.",
                })
                metadata_persisted = False
                for metadata_attempt in range(1, 4):
                    metadata_db = SessionLocal()
                    try:
                        cleanup_run = metadata_db.get(SiteDiscoveryRun, run_id)
                        if cleanup_run is None:
                            raise LookupError("cleanup-pending run is missing")
                        trace_entries = list(cleanup_run.node_trace or [])
                        trace_entries = [
                            item for item in trace_entries
                            if not (
                                isinstance(item, dict)
                                and item.get("type") == "openhands_cleanup_pending_metadata"
                            )
                        ]
                        trace_entries.append({
                            "type": "openhands_cleanup_pending_metadata",
                            "metadata": redact_discovery_data(exploration["cleanup_pending"]),
                        })
                        cleanup_run.node_trace = trace_entries[-200:]
                        cleanup_run.error_message = "OpenHands cleanup_pending; awaiting host confirmation."
                        metadata_db.commit()
                        metadata_persisted = True
                        break
                    except Exception:
                        metadata_db.rollback()
                        logger.warning(
                            "failed to persist cleanup metadata run_id=%s attempt=%s",
                            run_id,
                            metadata_attempt,
                        )
                    finally:
                        metadata_db.close()
                if not metadata_persisted:
                    logger.error(
                        "cleanup metadata persistence degraded run_id=%s; in-process worker remains required",
                        run_id,
                    )

                def renew_cleanup_claim() -> bool:
                    return renew_terminal_claim()
                try:
                    append_discovery_event(
                        run_id,
                        event_type="openhands_cleanup_pending",
                        summary="OpenHands 清理等待宿主重试；运行保持非终态并继续占用容量。",
                        phase=stage,
                        round_number=max(0, round_number),
                        level="error",
                        payload={
                            "provenance": "host_cleanup_verification",
                            "cleanup_confirmed": False,
                            "intended_terminal_status": intended_status,
                        },
                    )
                except Exception:
                    logger.exception("failed to persist OpenHands cleanup-pending audit run_id=%s", run_id)

                def retry_cleanup() -> None:
                    usage_token = activate_usage_scope(usage)
                    try:
                        attempt = 0
                        while True:
                            attempt += 1
                            if not renew_cleanup_claim():
                                time.sleep(min(0.25 * attempt, 5.0))
                                continue
                            if session.cleanup_confirmed:
                                if persist_cleanup_stage(
                                    session,
                                    stage=stage,
                                    round_number=round_number,
                                    usage_drained=True,
                                    resources_removed=True,
                                ):
                                    break
                                time.sleep(min(0.25 * attempt, 5.0))
                                continue
                            # The trusted relay may have completed a response
                            # since the main thread's last attempt.  Drain it in
                            # an explicitly activated host UsageScope before
                            # every stop attempt.
                            try:
                                session.stop_for_usage_drain()
                            except Exception:
                                time.sleep(min(0.25 * attempt, 5.0))
                                continue
                            if not drain_openhands_relay_usage(stage, round_number):
                                time.sleep(min(0.25 * attempt, 5.0))
                                continue
                            if not persist_cleanup_stage(
                                session,
                                stage=stage,
                                round_number=round_number,
                                usage_drained=True,
                                resources_removed=False,
                            ):
                                time.sleep(min(0.25 * attempt, 5.0))
                                continue
                            try:
                                session.remove_after_usage_drain()
                                if not persist_cleanup_stage(
                                    session,
                                    stage=stage,
                                    round_number=round_number,
                                    usage_drained=True,
                                    resources_removed=True,
                                ):
                                    time.sleep(min(0.25 * attempt, 5.0))
                                    continue
                                break
                            except Exception as exc:
                                if attempt == 1 or attempt % 10 == 0:
                                    logger.warning(
                                        "OpenHands cleanup pending run_id=%s attempt=%s error=%s",
                                        run_id,
                                        attempt,
                                        type(exc).__name__,
                                    )
                                time.sleep(min(0.25 * attempt, 5.0))
                        cleanup_error = {
                            "type": "OpenHandsCleanupDegraded",
                            "message": "OpenHands cleanup required deferred host retries.",
                            "intended_terminal_status": intended_status,
                            "primary_error": safe_primary_error,
                        }
                        exploration["cleanup_pending"].update(
                            session.cleanup_recovery_metadata()
                        )
                        exploration["cleanup_pending"]["cleanup_confirmed"] = True
                        while not save_terminal_usage_checkpoint(cleanup_error):
                            time.sleep(1.0)
                        terminal_confirmed = False
                        while not terminal_confirmed:
                            try:
                                run_with_terminal_claim(lambda: persist_terminal_outcome(
                                    status="failed",
                                    error_message=cleanup_error["message"],
                                    event_type="openhands_cleanup_recovered",
                                    summary="宿主已确认 OpenHands Agent 与 relay 停止；以 cleanup-degraded 失败终态收敛。",
                                    phase=stage,
                                    round_number=max(0, round_number),
                                    level="error",
                                    payload={
                                        "provenance": "host_cleanup_verification",
                                        "cleanup_confirmed": True,
                                        "attempts": attempt,
                                    },
                                ))
                                terminal_confirmed = True
                            except Exception:
                                logger.exception(
                                    "failed to converge atomic OpenHands cleanup terminal run_id=%s",
                                    run_id,
                                )
                            if not terminal_confirmed:
                                time.sleep(1.0)
                    finally:
                        deactivate_usage_scope(usage_token)

                try:
                    threading.Thread(
                        target=retry_cleanup,
                        daemon=True,
                        name=f"discovery-openhands-cleanup-{run_id}",
                    ).start()
                except Exception as exc:
                    # Durable cleanup_pending metadata remains the owner; a
                    # startup recovery pass can take over without any secret.
                    logger.error(
                        "failed to start OpenHands cleanup worker run_id=%s error=%s",
                        run_id,
                        type(exc).__name__,
                    )
                    retry_cleanup()

            def save_terminal_usage_checkpoint(error: dict[str, Any] | None) -> bool:
                """Persist the post-drain token total before the run becomes terminal."""
                try:
                    self._save_round_checkpoint(
                        run_id=run_id,
                        phase=active_phase,
                        round_number=max(0, active_round),
                        trial=trial,
                        rag_references=rag_references,
                        exploration=exploration,
                        execution_result=last_execution_result,
                        tool_evidence=last_tool_evidence,
                        evaluation_result=previous_evaluation,
                        code_diff=last_code_diff,
                        error=error,
                        elapsed_seconds=deadline.elapsed(),
                        token_usage=usage.total_tokens,
                        enforce_deadline=False,
                    )
                    return True
                except Exception as exc:  # do not replace the primary terminal outcome
                    logger.warning(
                        "failed to persist terminal usage checkpoint run_id=%s error=%s",
                        run_id,
                        type(exc).__name__,
                    )
                    return False

            def persist_terminal_outcome(
                *,
                status: str,
                error_message: str,
                event_type: str,
                summary: str,
                phase: str,
                round_number: int,
                payload: dict[str, Any] | None,
                level: str,
            ) -> None:
                """Commit the unique terminal event and run state in one transaction."""
                _persist_idempotent_terminal_outcome(
                    run_id=run_id,
                    status=status,
                    error_message=error_message,
                    event_type=event_type,
                    summary=summary,
                    phase=phase,
                    round_number=round_number,
                    payload=payload,
                    level=level,
                    node_trace=trace,
                    token_usage=usage.total_tokens,
                )

            def finalize_or_defer(
                *,
                error: dict[str, Any] | None,
                intended_status: str,
                action: Callable[[], None],
                package_metadata: dict[str, Any] | None = None,
                compensation_confirmed: bool = True,
            ) -> bool:
                """Require a durable terminal checkpoint before any terminal side effect."""
                claim_ready = (
                    renew_terminal_claim()
                    if terminal_claim_acquired
                    else acquire_terminal_claim()
                )
                if compensation_confirmed and claim_ready and save_terminal_usage_checkpoint(error):
                    run_with_terminal_claim(action)
                    return True
                if package_metadata is not None:
                    exploration["package_finalization"] = redact_discovery_data(package_metadata)
                marker = {
                    "type": "openhands_finalization_pending_metadata",
                    "metadata": {
                        "protocol": 1,
                        "intended_status": intended_status,
                        "phase": active_phase,
                        "round": max(0, active_round),
                        "token_usage": usage.total_tokens,
                        "error": redact_discovery_data(error),
                        "package": redact_discovery_data(package_metadata),
                        "compensation_confirmed": compensation_confirmed,
                    },
                }
                marker_saved = False
                marker_attempt = 0
                while not marker_saved:
                    marker_attempt += 1
                    marker_db = SessionLocal()
                    try:
                        pending_run = marker_db.scalar(
                            select(SiteDiscoveryRun)
                            .where(SiteDiscoveryRun.id == run_id)
                            .with_for_update()
                        )
                        if pending_run is None:
                            raise LookupError("finalization-pending run is missing")
                        entries = [
                            item for item in list(pending_run.node_trace or [])
                            if not (
                                isinstance(item, dict)
                                and item.get("type") == "openhands_finalization_pending_metadata"
                            )
                        ]
                        entries.append(marker)
                        pending_run.node_trace = entries[-200:]
                        pending_run.error_message = (
                            f"OpenHands finalization_pending:{intended_status}"
                        )
                        marker_db.commit()
                        marker_saved = True
                    except Exception:
                        marker_db.rollback()
                        if marker_attempt == 1 or marker_attempt % 10 == 0:
                            logger.exception(
                                "failed to persist finalization-pending marker run_id=%s attempt=%s",
                                run_id,
                                marker_attempt,
                            )
                    finally:
                        marker_db.close()
                    if not marker_saved:
                        time.sleep(min(0.25 * marker_attempt, 5.0))

                if not compensation_confirmed:
                    logger.error(
                        "Package compensation remains unconfirmed; finalization stays pending run_id=%s",
                        run_id,
                    )
                    return False

                def retry_finalization() -> None:
                    token = activate_usage_scope(usage)
                    try:
                        while True:
                            claimed = (
                                renew_terminal_claim()
                                if terminal_claim_acquired
                                else acquire_terminal_claim()
                            )
                            if not claimed:
                                time.sleep(1.0)
                                continue
                            if save_terminal_usage_checkpoint(error):
                                break
                            time.sleep(1.0)
                        while True:
                            try:
                                run_with_terminal_claim(action)
                                return
                            except Exception:
                                logger.exception(
                                    "failed to converge deferred finalization run_id=%s", run_id
                                )
                                time.sleep(1.0)
                    finally:
                        deactivate_usage_scope(token)

                try:
                    threading.Thread(
                        target=retry_finalization,
                        daemon=True,
                        name=f"discovery-openhands-finalize-{run_id}",
                    ).start()
                except Exception:
                    logger.exception("failed to start finalization worker run_id=%s", run_id)
                    retry_finalization()
                return False

            agent_session = open_openhands_agent_session(
                self.sandbox,
                OpenHandsAgentSessionSpec(
                    job_id=agent_job_id,
                    site_url=site_url,
                    runtime_version=self.settings.discovery_agent_runtime_version,
                    connector_runtime_version=self.settings.discovery_runtime_version,
                    priority=(
                        SandboxJobPriority.REPAIR
                        if self._trigger_type(run_id) == "repair"
                        else SandboxJobPriority.DISCOVERY
                    ),
                    timeout_seconds=max(0.1, deadline.remaining()),
                    initial_allowed_domains=trusted_initial_domains,
                    context_memory=agent_budget["contextMemory"],
                    tool_kb=agent_budget["toolKb"],
                    max_iterations=agent_budget["depth"],
                    token_budget=agent_budget["tokenBudget"],
                ),
                settings=self.settings,
                on_started=lambda: self._sandbox_started(
                    run_id,
                    deadline,
                    phase="explore",
                    round_number=start_round - 1,
                ),
                on_event=persist_openhands_event,
            )
            exploration.update({
                "explore_complete": True,
                "agent_runtime": {
                    "name": "openhands-sdk",
                    "version": "1.43.1",
                    "retained_gvisor_workspace": True,
                },
                "exploration_summary": (
                    "OpenHands performs live Explore and writes the first candidate in one retained gVisor workspace."
                ),
            })
            for round_number in count(start_round):
                ensure_not_cancelled()
                self._guard_active(run_id, deadline)
                phase = "repair" if previous_source or last_error else "build"
                active_phase = phase
                active_round = round_number
                if phase == "repair":
                    if repair_attempts >= MAX_REPAIR_ATTEMPTS:
                        limit_error, limit_evaluation = _repair_limit_diagnostics(
                            attempts=repair_attempts,
                            limit=MAX_REPAIR_ATTEMPTS,
                            previous_evaluation=previous_evaluation,
                        )
                        last_round = max(0, round_number - 1)
                        active_round = last_round
                        if trial is None and resume_checkpoint is not None:
                            trial = self._copy_resume_trial_for_checkpoint(
                                run_id=run_id,
                                checkpoint=resume_checkpoint,
                                site_url=site_url,
                            )
                        self._save_round_checkpoint(
                            run_id=run_id,
                            phase="repair",
                            round_number=last_round,
                            trial=trial,
                            rag_references=rag_references,
                            exploration=exploration,
                            execution_result=last_execution_result,
                            tool_evidence=last_tool_evidence,
                            evaluation_result=limit_evaluation,
                            code_diff=last_code_diff,
                            error=limit_error,
                            elapsed_seconds=deadline.elapsed(),
                            token_usage=usage.total_tokens,
                        )
                        self._terminal_event(
                            run_id,
                            "repair_attempt_limit_reached",
                            (
                                f"已完成 {repair_attempts} 次有效 Repair 仍未通过确定性验收；"
                                "保留检查点并停止，等待人工查看失败证据。"
                            ),
                            "repair",
                            last_round,
                            {
                                "effective_repair_attempts": repair_attempts,
                                "max_repair_attempts": MAX_REPAIR_ATTEMPTS,
                                "failures": limit_evaluation["failures"],
                                "error": limit_error,
                            },
                            level="error",
                        )
                        raise _RepairAttemptLimitReached(
                            attempts=repair_attempts,
                            limit=MAX_REPAIR_ATTEMPTS,
                            failures=limit_evaluation["failures"],
                        )
                agent_phase = "repair" if phase == "repair" else "explore"
                relay_usage_stage = phase
                active_phase = agent_phase
                self._phase(
                    run_id,
                    agent_phase,
                    round_number,
                    trace,
                    (
                        "OpenHands 在保留 workspace 中根据宿主真实反馈修复采集器。"
                        if phase == "repair"
                        else "OpenHands 在保留 workspace 中连续探查并编写首版采集器。"
                    ),
                )
                if not build_references:
                    build_references = self._retrieve_experiences(
                        run_id=run_id,
                        stage="build",
                        event_phase=phase,
                        round_number=round_number,
                        domain=domain,
                        technical_features=exploration.get("technical_features") or {},
                        query=json.dumps({
                            "domain": domain,
                            "technical_features": exploration.get("technical_features") or {},
                            "explore_session": exploration.get("explore_session", {}).get("evidence"),
                        }, ensure_ascii=False, default=str),
                    )
                    rag_references = _merge_rag_references(rag_references, build_references)
                stage_references = build_references
                if phase == "repair":
                    repair_references = self._retrieve_experiences(
                        run_id=run_id,
                        stage="repair",
                        event_phase="repair",
                        round_number=round_number,
                        domain=domain,
                        technical_features=exploration.get("technical_features") or {},
                        query=json.dumps({
                            "domain": domain,
                            "execution_error": last_error,
                            "evaluation_failures": previous_evaluation.get("failures") or [],
                        }, ensure_ascii=False, default=str),
                    )
                    stage_references = _merge_rag_references(build_references, repair_references)
                    rag_references = _merge_rag_references(rag_references, repair_references)
                agent_turn_limit_seconds = max(0.1, deadline.remaining())
                agent_started_at = time.monotonic()
                try:
                    if agent_session is None:
                        raise RuntimeError("OpenHands Agent Runtime session is unavailable")
                    try:
                        turn = agent_session.run_turn(
                            prompt=openhands_turn_prompt(
                                mode=("repair" if phase == "repair" else "explore_and_build"),
                                site_url=site_url,
                                connector_runtime_version=self.settings.discovery_runtime_version,
                                round_number=round_number,
                                rag_references=stage_references,
                                previous_source=previous_source,
                                evaluation_failures=list(previous_evaluation.get("failures") or []),
                                execution_error=(
                                    {"error": last_error, "tool_evidence": last_tool_evidence}
                                    if last_error or last_tool_evidence else None
                                ),
                            ),
                            initial_source=(previous_source if round_number == start_round else None),
                            timeout_seconds=agent_turn_limit_seconds,
                        )
                    finally:
                        # This runs on the host Loop thread where UsageScope's
                        # ContextVar is active, including timeout/error paths.
                        drain_openhands_relay_usage(phase, round_number)
                    draft = turn.draft
                    turn_events = [
                        {
                            **redact_discovery_data({
                                key: value for key, value in event.items() if key != "token_usage"
                            }),
                            "provenance": "agent_untrusted",
                        }
                        for event in turn.tool_events
                    ]
                    last_tool_evidence = turn_events
                    self._event(
                        run_id,
                        "openhands_turn_completed",
                        "OpenHands 已完成本轮工具循环并提交候选；是否成功仍由宿主执行与验收决定。",
                        phase,
                        round_number,
                        {
                            "sdk_version": "1.43.1",
                            "tool_events": turn_events,
                            "usage_provenance": "relay_only",
                        },
                    )
                    if phase == "build":
                        active_phase = "build"
                        self._phase(
                            run_id,
                            "build",
                            round_number,
                            trace,
                            "OpenHands 已提交 workspace 候选文件，进入宿主合同校验。",
                        )
                except TimeoutError as exc:
                    elapsed_seconds = round(time.monotonic() - agent_started_at, 3)
                    self._event(
                        run_id,
                        "agent_turn_timeout",
                        f"{phase.title()} Agent 在 {agent_turn_limit_seconds:.0f} 秒内未完成代码草稿。",
                        phase,
                        round_number,
                        {
                            "stage": phase,
                            "limit_seconds": agent_turn_limit_seconds,
                            "elapsed_seconds": elapsed_seconds,
                            "error": redact_discovery_text(str(exc))[:4000],
                        },
                        level="warning",
                    )
                    raise TimeoutError(
                        f"{phase.title()} Agent did not complete within "
                        f"{agent_turn_limit_seconds:.0f} seconds"
                    ) from exc
                except OpenHandsAgentRuntimeError as exc:
                    runtime_error = {
                        "type": "agent_runtime_error",
                        "message": redact_discovery_text(exc.summary)[:1000],
                        "evidence": redact_discovery_data(exc.evidence),
                    }
                    last_error = runtime_error
                    previous_evaluation = {"passed": False, "failures": [
                        {"check": "agent_runtime", "passed": False, **runtime_error}
                    ]}
                    self._event(
                        run_id,
                        "agent_runtime_error",
                        "OpenHands Agent Runtime 已退出，本次任务停止；不会向死亡会话继续发送 Repair。",
                        phase,
                        round_number,
                        runtime_error,
                        level="error",
                    )
                    self._save_round_checkpoint(
                        run_id=run_id,
                        phase=phase,
                        round_number=round_number,
                        trial=trial,
                        rag_references=rag_references,
                        exploration=exploration,
                        execution_result=None,
                        tool_evidence=last_tool_evidence,
                        evaluation_result=previous_evaluation,
                        code_diff=None,
                        error=runtime_error,
                        elapsed_seconds=deadline.elapsed(),
                        token_usage=usage.total_tokens,
                    )
                    raise
                except Exception as exc:  # malformed model output is repair evidence, not success
                    if isinstance(exc, (TimeoutError, DiscoveryCancelled)):
                        raise
                    agent_draft_contract_failures += 1
                    exploration["consecutive_agent_draft_contract_failures"] = (
                        agent_draft_contract_failures
                    )
                    last_error = {
                        "type": type(exc).__name__,
                        "message": redact_discovery_text(str(exc))[:4000],
                        "consecutive_attempt": agent_draft_contract_failures,
                        "maximum_consecutive_attempts": MAX_CONSECUTIVE_AGENT_DRAFT_CONTRACT_FAILURES,
                    }
                    previous_evaluation = {"passed": False, "failures": [
                        {"check": "agent_draft_contract", "passed": False, **last_error}
                    ]}
                    self._event(run_id, "agent_draft_rejected", "Agent 草稿不符合合同，进入下一轮修复。",
                                phase, round_number, last_error, level="warning")
                    self._save_round_checkpoint(
                        run_id=run_id,
                        phase="repair",
                        round_number=round_number,
                        trial=trial,
                        rag_references=rag_references,
                        exploration=exploration,
                        execution_result=None,
                        tool_evidence=last_tool_evidence,
                        evaluation_result=previous_evaluation,
                        code_diff=None,
                        error=last_error,
                        elapsed_seconds=deadline.elapsed(),
                        token_usage=usage.total_tokens,
                    )
                    if agent_draft_contract_failures >= MAX_CONSECUTIVE_AGENT_DRAFT_CONTRACT_FAILURES:
                        raise _AgentDraftContractLimitReached(
                            attempts=agent_draft_contract_failures,
                            limit=MAX_CONSECUTIVE_AGENT_DRAFT_CONTRACT_FAILURES,
                        )
                    self._guard_active(run_id, deadline)
                    continue
                agent_draft_contract_failures = 0
                exploration.pop("consecutive_agent_draft_contract_failures", None)
                self._guard_active(run_id, deadline)
                if phase == "repair" and previous_source == draft.crawler_py:
                    consecutive_repair_noops += 1
                    exploration["consecutive_repair_noops"] = consecutive_repair_noops
                    prior_error = dict(last_error) if last_error else None
                    last_error = {
                        "type": "RepairNoOpRejected",
                        "stage": "repair",
                        "message": "Repair 草稿与上一版 crawler.py 完全相同，未执行且不计入有效 Repair。",
                        "consecutive_attempt": consecutive_repair_noops,
                        "maximum_consecutive_attempts": MAX_CONSECUTIVE_REPAIR_NOOPS,
                    }
                    if prior_error:
                        last_error["previous_error"] = prior_error
                    if trial is None and resume_checkpoint is not None:
                        trial = self._copy_resume_trial_for_checkpoint(
                            run_id=run_id,
                            checkpoint=resume_checkpoint,
                            site_url=site_url,
                        )
                    elif trial is None:
                        # Automatic repair begins from an approved source rather
                        # than a run-local trial. Persist that same source so a
                        # later resume retains the deterministic failure context.
                        # Do not persist domains proposed by the rejected draft.
                        trial = write_trial_artifact(
                            run_id=run_id,
                            site_url=site_url,
                            source=previous_source,
                            allowed_domains=list(trusted_initial_domains),
                            runtime_version=self.settings.discovery_runtime_version,
                            version=round_number,
                            time_semantics=str(exploration.get("time_semantics") or "publication"),
                            settings=self.settings,
                        )
                    self._event(
                        run_id,
                        "repair_noop_rejected",
                        "Repair 草稿与上一版源码相同，未执行且不计入有效 Repair。",
                        "repair",
                        round_number,
                        {
                            "effective_repair_attempts": repair_attempts,
                            "max_repair_attempts": MAX_REPAIR_ATTEMPTS,
                            "consecutive_attempt": consecutive_repair_noops,
                            "maximum_consecutive_attempts": MAX_CONSECUTIVE_REPAIR_NOOPS,
                            "error": last_error,
                        },
                        level="warning",
                    )
                    self._save_round_checkpoint(
                        run_id=run_id,
                        phase="repair",
                        round_number=round_number,
                        trial=trial,
                        rag_references=rag_references,
                        exploration=exploration,
                        execution_result=last_execution_result,
                        tool_evidence=last_tool_evidence,
                        evaluation_result=previous_evaluation,
                        code_diff=last_code_diff,
                        error=last_error,
                        elapsed_seconds=deadline.elapsed(),
                        token_usage=usage.total_tokens,
                    )
                    if consecutive_repair_noops >= MAX_CONSECUTIVE_REPAIR_NOOPS:
                        raise _RepairNoOpLimitReached(
                            attempts=consecutive_repair_noops,
                            limit=MAX_CONSECUTIVE_REPAIR_NOOPS,
                        )
                    self._guard_active(run_id, deadline)
                    continue
                consecutive_repair_noops = 0
                exploration.pop("consecutive_repair_noops", None)
                if phase == "repair":
                    repair_attempts += 1
                    exploration["effective_repair_attempts"] = repair_attempts
                    self._event(
                        run_id,
                        "repair_attempt_recorded",
                        f"已接受新源码，计为第 {repair_attempts}/{MAX_REPAIR_ATTEMPTS} 次有效 Repair。",
                        "repair",
                        round_number,
                        {
                            "effective_repair_attempts": repair_attempts,
                            "max_repair_attempts": MAX_REPAIR_ATTEMPTS,
                        },
                    )
                try:
                    trial = write_trial_artifact(
                        run_id=run_id,
                        site_url=site_url,
                        source=draft.crawler_py,
                        # Explore always starts with the entry host plus its
                        # www/non-www alias.  A generated connector commonly
                        # follows the page's canonical URLs, so preserve that
                        # already-approved alias even if the Agent omitted it
                        # from its manifest draft.  Extra domains still have
                        # to be separately observed and declared by the Agent.
                        allowed_domains=_artifact_allowed_domains(
                            site_url,
                            draft.allowed_domains,
                        ),
                        runtime_version=self.settings.discovery_runtime_version,
                        version=round_number,
                        time_semantics=draft.time_semantics,
                        settings=self.settings,
                    )
                except Exception as exc:
                    last_error = {"type": type(exc).__name__,
                                  "message": redact_discovery_text(str(exc))[:4000]}
                    previous_evaluation = {"passed": False, "failures": [
                        {"check": "artifact_contract", "passed": False, **last_error}
                    ]}
                    self._event(run_id, "artifact_rejected", "草稿制品未通过 Manifest/文件合同校验。",
                                phase, round_number, last_error, level="warning")
                    self._save_round_checkpoint(
                        run_id=run_id, phase="repair", round_number=round_number, trial=trial,
                        rag_references=rag_references, exploration=exploration,
                        execution_result=None, evaluation_result=previous_evaluation,
                        tool_evidence=last_tool_evidence,
                        code_diff=None, error=last_error, elapsed_seconds=deadline.elapsed(),
                        token_usage=usage.total_tokens,
                    )
                    continue
                code_diff = _code_diff(previous_source, draft.crawler_py)
                last_code_diff = code_diff
                self._event(run_id, "connector_written", draft.action_summary, phase, round_number,
                            {"change_summary": draft.change_summary, "checksum": trial.manifest.checksum,
                             "allowed_domains": list(trial.manifest.allowed_domains), "code_diff": code_diff[:100_000]})

                outputs = []
                trial_attestations: list[dict[str, Any]] = []
                auxiliary_proofs: list[dict[str, Any]] = []
                pagination_output = None
                reachable: set[str] = set()
                round_tool_evidence: list[dict[str, Any]] = []
                last_error = None
                sandbox_stage_complete = False
                field_smoke_started = False
                transport_failure = False
                transport_route_selected = False
                try:
                    field_smoke_started = True
                    active_phase = "execute"
                    active_round = round_number
                    smoke_execution = self._execute(
                        run_id, "execute", round_number, trial,
                        request=ConnectorRequest(entry=site_url, config={"page": 1}, target_count=1),
                        suffix="field-smoke", deadline=deadline,
                    )
                    round_tool_evidence.extend(list(smoke_execution.events))
                    smoke_evaluation = evaluate_connector_field_smoke(
                        smoke_execution.audit_output,
                        time_semantics=trial.manifest.time_semantics,
                        snapshot_observed_at=smoke_execution.attestation.completed_at,
                        requested_target_count=1,
                    )
                    if not smoke_evaluation.passed:
                        raise _FieldSmokeFailure(smoke_evaluation)
                    self._phase(run_id, "execute", round_number, trace, "通过 SandboxRuntime 独立试运行两次。")
                    for trial_number in (1, 2):
                        execution = self._execute(
                            run_id, "execute", round_number, trial,
                            request=ConnectorRequest(entry=site_url, config={"page": 1}),
                            suffix=f"trial-{trial_number}", deadline=deadline,
                        )
                        round_tool_evidence.extend(list(execution.events))
                        outputs.append(execution.audit_output)
                        trial_attestations.append(execution.attestation.as_dict())
                    page_execution = self._execute(
                        run_id, "execute", round_number, trial,
                        request=ConnectorRequest(entry=site_url, config={"page": 2}),
                        suffix="page-2", deadline=deadline,
                    )
                    round_tool_evidence.extend(list(page_execution.events))
                    pagination_output = page_execution.audit_output
                    auxiliary_proofs.append(sandbox_execution_proof(page_execution))

                    sample_urls = list(dict.fromkeys(item.url for output in outputs for item in output.items))[:10]
                    allowed_samples = [url for url in sample_urls if _host_allowed(url, trial.manifest.allowed_domains)]
                    verifier = write_trial_artifact(
                        run_id=run_id,
                        site_url=site_url,
                        source=_URL_VERIFIER_SOURCE,
                        allowed_domains=list(trial.manifest.allowed_domains),
                        runtime_version=self.settings.discovery_runtime_version,
                        connector_key=f"{trial.manifest.connector_key[:56]}_verify",
                        version=round_number,
                        settings=self.settings,
                    )
                    verified = self._execute(
                        run_id, "evaluate", round_number, verifier,
                        request=ConnectorRequest(entry=site_url, config={"urls": allowed_samples}),
                        suffix="url-verify", deadline=deadline,
                    )
                    round_tool_evidence.extend(list(verified.events))
                    reachable = {item.url for item in verified.audit_output.items}
                    auxiliary_proofs.append(sandbox_execution_proof(verified))
                    sandbox_stage_complete = True

                    active_phase = "evaluate"
                    active_round = round_number
                    self._phase(run_id, "evaluate", round_number, trace, "程序执行第 9 节确定性验收。")
                    self._guard_active(run_id, deadline)
                    last_evaluation = evaluate_connector_outputs(
                        outputs,
                        reachable_urls=reachable,
                        supports_pagination=draft.supports_pagination,
                        pagination_output=pagination_output,
                        require_url_accessibility=False,
                        time_semantics=trial.manifest.time_semantics,
                        snapshot_observed_at=[item["completed_at"] for item in trial_attestations],
                    )
                    # A connector may return links to arbitrary publishers (for
                    # example an aggregator feed).  Manifest domains constrain
                    # network egress, not the URLs carried as result data.
                    evaluation_dict = last_evaluation.as_dict()
                    previous_evaluation = evaluation_dict
                    method_audit = audit_plugin_trial(
                        evaluation=evaluation_dict,
                        outputs=outputs,
                        artifact_evidence={
                            **artifact_evidence(trial, kind="sites"),
                            "trial_run_id": run_id,
                        },
                        runtime_attestations=trial_attestations,
                        auxiliary_proofs=auxiliary_proofs,
                    )
                    quality_audit, quality_trials = audit_plugin_trial_quality(
                        items_by_run=[
                            [item.model_dump(mode="json") for item in output.items]
                            for output in outputs
                        ]
                    )
                    plugin_review = {
                        "method_audit": method_audit,
                        "quality_audit": {
                            key: (value.isoformat() if isinstance(value, datetime) else value)
                            for key, value in quality_audit.as_update_values().items()
                        },
                        "quality_trials": quality_trials,
                        "package_eligible": bool(
                            method_audit["passed"]
                            or method_audit["low_frequency_exception_eligible"]
                        ),
                    }
                    previous_evaluation["plugin_review"] = plugin_review
                    self._event(run_id, "evaluation_completed", "确定性验收完成。", "evaluate", round_number,
                                _evaluation_event_projection(evaluation_dict),
                                level="info" if evaluation_dict["passed"] else "warning")
                    last_tool_evidence = [redact_discovery_data(event) for event in round_tool_evidence[:50]]
                except _FieldSmokeFailure as exc:
                    previous_evaluation = exc.evaluation.as_dict()
                    last_error = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "stage": "field_smoke",
                    }
                    self._event(
                        run_id,
                        "field_smoke_failed",
                        "字段 smoke run 未通过，跳过独立试运行并进入修复。",
                        "execute",
                        round_number,
                        previous_evaluation,
                        level="warning",
                    )
                    last_tool_evidence = [redact_discovery_data(event) for event in round_tool_evidence[:50]]
                except (ConnectorProtocolError, TimeoutError, RuntimeError, ValueError) as exc:
                    if isinstance(exc, TimeoutError):
                        raise
                    if isinstance(exc, ConnectorProtocolError):
                        if exc.code == ConnectorErrorCode.CANCELLED:
                            raise DiscoveryCancelled("用户已取消智能探查") from exc
                        if exc.code == ConnectorErrorCode.RUNTIME_ERROR:
                            raise
                    last_error = _structured_error(exc)
                    last_tool_evidence = [redact_discovery_data(event) for event in round_tool_evidence[:50]]
                    field_smoke_error = field_smoke_started and not outputs and not sandbox_stage_complete
                    transport_failure = _is_transport_failure(exc)
                    if transport_failure:
                        selected_route = _select_transport_route(exploration)
                        last_error["stage"] = "transport_route"
                        last_error["selected_route"] = selected_route
                        previous_evaluation = {"passed": False, "failures": [
                            {"check": "transport_route", "passed": False, **last_error}
                        ]}
                        self._event(
                            run_id,
                            "transport_route_selected",
                            "同一制品网络重试失败；已选择已验证传输路径并继续修复，不终止探查。",
                            "execute",
                            round_number,
                            last_error,
                            level="warning",
                        )
                        # This is a transport-mode correction, not a webpage
                        # logic repair. Keep the Run active and pass the
                        # deterministic route selection to the next Build turn.
                        transport_failure = False
                        transport_route_selected = True
                    elif field_smoke_error:
                        last_error["stage"] = "field_smoke"
                        previous_evaluation = {"passed": False, "failures": [
                            {"check": "field_smoke_execution", "passed": False, **last_error}
                        ]}
                        self._event(
                            run_id,
                            "field_smoke_failed",
                            "字段 smoke run 在导入、依赖或首次执行时失败，跳过独立试运行并进入修复。",
                            "execute",
                            round_number,
                            previous_evaluation,
                            level="warning",
                        )
                    if (
                        not transport_failure
                        and not field_smoke_error
                        and not transport_route_selected
                        and not isinstance(exc, ConnectorProtocolError)
                    ):
                        failure_stage = (
                            "evaluation_or_persistence"
                            if sandbox_stage_complete
                            else "execution_evidence_or_persistence"
                        )
                        last_error["stage"] = failure_stage
                        previous_evaluation = {"passed": False, "failures": [
                            {
                                "check": failure_stage,
                                "passed": False,
                                **last_error,
                            }
                        ]}
                        self._event(
                            run_id,
                            (
                                "evaluation_processing_failed"
                                if sandbox_stage_complete
                                else "execution_evidence_processing_failed"
                            ),
                            (
                                "沙箱执行已成功，但确定性验收或结果持久化失败。"
                                if sandbox_stage_complete
                                else "沙箱执行证据或结果持久化失败；未判定为插件执行失败。"
                            ),
                            "evaluate" if sandbox_stage_complete else "execute",
                            round_number,
                            last_error,
                            level="error",
                        )
                        failure_label = (
                            "确定性验收或结果持久化失败"
                            if sandbox_stage_complete
                            else "沙箱执行证据或结果持久化失败"
                        )
                        raise RuntimeError(
                            f"{failure_label}: {last_error['message']}"
                        ) from exc
                    if not transport_failure and not field_smoke_error and not transport_route_selected:
                        previous_evaluation = {"passed": False, "failures": [
                            {"check": "sandbox_execution", "passed": False, **last_error}
                        ]}
                        self._event(run_id, "sandbox_execution_failed", "沙箱真实执行失败。", "execute", round_number,
                                    last_error, level="error")

                exploration["technical_features"] = draft.technical_features
                exploration["supports_pagination"] = draft.supports_pagination
                exploration["time_semantics"] = draft.time_semantics
                last_execution_result = {
                    "successful_runs": len(outputs),
                    "trial_outputs": [output.model_dump(mode="json") for output in outputs],
                    "pagination_output": (
                        pagination_output.model_dump(mode="json")
                        if pagination_output is not None else None
                    ),
                    "reachable_urls": sorted(reachable),
                    "requires_url_verification": False,
                    "supports_pagination": draft.supports_pagination,
                    "time_semantics": draft.time_semantics,
                    "snapshot_observed_at": [item["completed_at"] for item in trial_attestations],
                    "auxiliary_proofs": auxiliary_proofs,
                }
                self._save_round_checkpoint(
                    run_id=run_id,
                    phase=(
                        "execute"
                        if transport_failure
                        else "evaluate"
                        if previous_evaluation.get("passed")
                        or ((previous_evaluation.get("plugin_review") or {}).get("method_audit") or {}).get(
                            "low_frequency_exception_eligible"
                        )
                        else "repair"
                    ),
                    round_number=round_number,
                    trial=trial,
                    rag_references=rag_references,
                    exploration=exploration,
                    execution_result=last_execution_result,
                    tool_evidence=last_tool_evidence,
                    evaluation_result=previous_evaluation,
                    code_diff=code_diff,
                    error=last_error,
                    elapsed_seconds=deadline.elapsed(),
                    token_usage=usage.total_tokens,
                )

                plugin_review = previous_evaluation.get("plugin_review") or {}
                low_frequency_exception = bool(
                    (plugin_review.get("method_audit") or {}).get(
                        "low_frequency_exception_eligible"
                    )
                )
                if previous_evaluation.get("passed") or low_frequency_exception:
                    self._guard_active(run_id, deadline)
                    active_phase = "package"
                    active_round = round_number
                    self._phase(run_id, "package", round_number, trace, "固化新版本并进入待审核；不自动发布。")
                    if not close_openhands_before_terminal(
                        "repair" if phase == "repair" else "build", round_number
                    ):
                        raise RuntimeError("OpenHands cleanup degraded; packaging is forbidden")
                    trial_base = (
                        f"trial/sites/{trial.manifest.connector_key}/v{trial.manifest.version}"
                    )
                    package_metadata = {
                        "connector_draft_path": f"{trial_base}/crawler.py",
                        "manifest_path": f"{trial_base}/manifest.json",
                        "connector_key": trial.manifest.connector_key,
                        "version": trial.manifest.version,
                        "checksum": trial.manifest.checksum,
                        "signature": trial.signature,
                        "runtime_version": trial.manifest.runtime_version,
                        "evaluation_result": previous_evaluation,
                        "display_name": name or domain,
                        "force": force,
                    }
                    def package_action() -> None:
                        nonlocal package_side_effect_started
                        package_side_effect_started = True
                        self._package_trial(
                            run_id=run_id,
                            trial=trial,
                            display_name=name or domain,
                            force=force,
                            round_number=round_number,
                            trace=trace,
                            token_usage=usage.total_tokens,
                            deadline=deadline,
                            plugin_review=plugin_review,
                            expected_claim_owner=terminal_claim_owner,
                            claim_lost=terminal_claim_lost,
                        )

                    finalize_or_defer(
                        error=last_error,
                        intended_status="package",
                        package_metadata=package_metadata,
                        action=package_action,
                    )
                    return
                previous_source = draft.crawler_py
                self._guard_active(run_id, deadline)

        except DiscoveryCancelled as exc:
            if not close_openhands_before_terminal(relay_usage_stage, active_round):
                schedule_cleanup_pending(
                    relay_usage_stage, active_round, "cancelled", str(exc)
                )
                return
            terminal_error = {"message": redact_discovery_text(str(exc))[:4000]}
            cancel_message = str(exc)
            finalize_or_defer(
                error=terminal_error,
                intended_status="cancelled",
                action=lambda cancel_message=cancel_message: persist_terminal_outcome(
                    status="cancelled",
                    error_message=cancel_message,
                    event_type="run_cancelled",
                    summary="Single Agent Loop 已响应取消并安全收敛。",
                    phase=active_phase,
                    round_number=active_round,
                    payload={"error": terminal_error},
                    level="warning",
                ),
            )
        except _RepairAttemptLimitReached as exc:
            if not close_openhands_before_terminal("repair", active_round):
                schedule_cleanup_pending("repair", active_round, "failed", str(exc))
                return
            terminal_error = _structured_error(exc)
            terminal_error.update({
                "stage": "repair",
                "effective_repair_attempts": exc.attempts,
                "max_repair_attempts": exc.limit,
            })
            def finalize_repair_limit() -> None:
                persist_terminal_outcome(
                    status="failed", error_message=terminal_error["message"],
                    event_type="run_failed",
                    summary="Single Agent Loop 已达到 Repair 尝试上限并停止。",
                    phase="repair", round_number=active_round,
                    payload={"error": terminal_error}, level="error",
                )
            finalize_or_defer(
                error=terminal_error, intended_status="failed", action=finalize_repair_limit
            )
        except _RepairNoOpLimitReached as exc:
            if not close_openhands_before_terminal("repair", active_round):
                schedule_cleanup_pending("repair", active_round, "failed", str(exc))
                return
            terminal_error = _structured_error(exc)
            terminal_error.update({
                "stage": "repair",
                "consecutive_attempts": exc.attempts,
                "maximum_consecutive_attempts": exc.limit,
            })
            def finalize_repair_noop() -> None:
                persist_terminal_outcome(
                    status="failed", error_message=terminal_error["message"],
                    event_type="run_failed",
                    summary="Repair 连续未提交源码变更，已停止以避免重复空转。",
                    phase="repair", round_number=active_round,
                    payload={"error": terminal_error}, level="error",
                )
            finalize_or_defer(
                error=terminal_error, intended_status="failed", action=finalize_repair_noop
            )
        except _AgentDraftContractLimitReached as exc:
            if not close_openhands_before_terminal(relay_usage_stage, active_round):
                schedule_cleanup_pending(
                    relay_usage_stage, active_round, "failed", str(exc)
                )
                return
            terminal_error = _structured_error(exc)
            terminal_error.update({
                "stage": active_phase,
                "consecutive_attempts": exc.attempts,
                "maximum_consecutive_attempts": exc.limit,
            })
            def finalize_draft_limit() -> None:
                persist_terminal_outcome(
                    status="failed", error_message=terminal_error["message"],
                    event_type="run_failed",
                    summary="Agent 连续返回不符合合同的草稿，已停止以避免空转。",
                    phase=active_phase, round_number=active_round,
                    payload={"error": terminal_error}, level="error",
                )
            finalize_or_defer(
                error=terminal_error, intended_status="failed", action=finalize_draft_limit
            )
        except OpenHandsAgentRuntimeError as exc:
            if not close_openhands_before_terminal(relay_usage_stage, active_round):
                schedule_cleanup_pending(
                    relay_usage_stage, active_round, "failed", exc.summary
                )
                return
            terminal_error = {
                "type": "agent_runtime_error",
                "stage": active_phase,
                "message": redact_discovery_text(exc.summary)[:1000],
                "evidence": redact_discovery_data(exc.evidence),
            }
            logger.error(
                "OpenHands Agent Runtime failed run_id=%s exit_code=%s oom_killed=%s",
                run_id,
                exc.evidence.get("exit_code"),
                exc.evidence.get("oom_killed"),
            )

            def finalize_agent_runtime_failure() -> None:
                persist_terminal_outcome(
                    status="failed",
                    error_message=terminal_error["message"],
                    event_type="run_failed",
                    summary="OpenHands Agent Runtime 异常退出，任务已停止且未进入草稿 Repair 空转。",
                    phase=active_phase,
                    round_number=active_round,
                    payload={"error": terminal_error},
                    level="error",
                )

            finalize_or_defer(
                error=terminal_error,
                intended_status="failed",
                action=finalize_agent_runtime_failure,
                compensation_confirmed=not package_side_effect_started,
            )
        except Exception as exc:  # noqa: BLE001 - terminal state must always converge
            if not close_openhands_before_terminal(relay_usage_stage, active_round):
                schedule_cleanup_pending(
                    relay_usage_stage, active_round, "failed", str(exc)
                )
                return
            terminal_error = _structured_error(exc)
            terminal_error["stage"] = active_phase
            safe_error = terminal_error["message"]
            logger.exception("website Discovery Loop failed run_id=%s", run_id)
            def finalize_failure() -> None:
                persist_terminal_outcome(
                    status="failed", error_message=safe_error,
                    event_type="run_failed", summary="Single Agent Loop 失败并停止。",
                    phase=active_phase, round_number=active_round,
                    payload={"error": terminal_error}, level="error",
                )
            finalize_or_defer(
                error=terminal_error,
                intended_status="failed",
                action=finalize_failure,
                compensation_confirmed=(
                    not package_side_effect_started
                    or isinstance(exc, _PackageFailureAfterCompensation)
                ),
            )
        finally:
            with self._job_lock:
                self._run_jobs.pop(run_id, None)
                self._deadlines.pop(run_id, None)
            if agent_session is not None:
                if not cleanup_retry_scheduled and not close_openhands_before_terminal(
                    relay_usage_stage, active_round
                ):
                    schedule_cleanup_pending(
                        relay_usage_stage,
                        active_round,
                        "failed",
                        "OpenHands cleanup remained unconfirmed during Loop finalization.",
                    )

    def _explore(
        self,
        *,
        run_id: int,
        site_url: str,
        round_number: int,
        rag_references: list[dict[str, Any]],
        deadline: _LoopDeadline,
        initial_exploration: dict[str, Any],
        token_usage: UsageScope,
    ) -> dict[str, Any]:
        """Run the fixed-tool Explore session until the Agent asks to build.

        The Agent may batch declarative HTTP, browser, and workspace actions,
        but only the fixed gVisor broker executes them.  Therefore the session ledger is
        derived from the returned operation records, never from a generated
        probe, a strategy object, or a field name supplied by the model.
        """
        raise RuntimeError(
            "legacy fixed-action website Explore is frozen; OpenHands owns Explore + Build"
        )
        saved = initial_exploration.get("explore_session")
        saved_observations = (
            saved.get("observations")
            if isinstance(saved, dict) and saved.get("version") == 2
            else None
        )
        observations = _bounded_explore_checkpoint_observations(
            [item for item in list(saved_observations or []) if isinstance(item, dict)]
        )
        saved_evidence = saved.get("evidence") if isinstance(saved, dict) and saved.get("version") == 2 else None
        ledger = _explore_session_ledger(
            [] if isinstance(saved_evidence, dict) else observations,
            site_url=site_url,
            base=saved_evidence if isinstance(saved_evidence, dict) else None,
        )
        # v2 checkpoints written before meaningful-document batches were
        # introduced retain their bounded action observations but not this
        # aggregate.  Derive only the missing counter from those host-recorded
        # observations; do not replay them into redirects, documents, or any
        # other ledger aggregate already restored from the checkpoint.
        if (
            isinstance(saved_evidence, dict)
            and "meaningful_document_batches" not in saved_evidence
        ):
            ledger["meaningful_document_batches"] = sum(
                1
                for observation in observations
                if _observation_has_meaningful_explore_document(observation)
            )
        # A redirect, HEAD, failed request, or non-document broker observation
        # is useful trusted ledger evidence, but it must not spend one of the
        # two bounded document-observation turns.  Keep the total separately
        # for audit/UI compatibility while the stop rule uses only the durable
        # host-derived meaningful-document count below.
        meaningful_document_batches = _nonnegative_int(
            ledger.get("meaningful_document_batches")
        )
        seen_fingerprints: set[str] = set()
        action_start = max(
            [
                int(item.get("action_number"))
                for item in observations
                if isinstance(item.get("action_number"), int)
            ],
            default=0,
        ) + 1

        entry_host = (urlsplit(site_url).hostname or "").lower().rstrip(".")
        if not entry_host:
            raise ValueError("site URL must contain a hostname")
        job_id = f"discovery-{run_id}-{round_number}-explore"
        priority = (
            SandboxJobPriority.REPAIR
            if self._trigger_type(run_id) == "repair"
            else SandboxJobPriority.DISCOVERY
        )
        session: SandboxExploreSession | None = None
        with self._job_lock:
            self._run_jobs.setdefault(run_id, set()).add(job_id)
        try:
            self._sandbox_capacity_queued(
                run_id,
                deadline,
                phase="explore",
                round_number=round_number,
                summary="Explore 保留会话正在等待 gVisor 沙箱容量，执行预算暂停计时。",
                extra_payload={"retained_session": True},
            )
            self._mark_waiting_for_sandbox(run_id)
            session = self.sandbox.open_explore_session(
                SandboxExploreSessionSpec(
                    job_id=job_id,
                    runtime_version=self.settings.discovery_runtime_version,
                    allowed_domains=(entry_host,),
                    priority=priority,
                    entry_url=site_url,
                    timeout_seconds=max(0.1, deadline.remaining()),
                    purpose="explore_tool",
                ),
                on_started=lambda: self._sandbox_started(
                    run_id, deadline, phase="explore", round_number=round_number
                ),
            )

            for action_number in count(action_start):
                self._guard_active(run_id, deadline)
                if (
                    meaningful_document_batches >= MAX_EXPLORE_SESSION_BATCHES
                    and ledger["entry_observed"]
                    and not _explore_requires_candidate_records(ledger)
                    and not _explore_requires_detail_evidence(ledger)
                ):
                    return {
                        "explore_session": {
                            "version": 2,
                            "observations": observations,
                            "evidence": ledger,
                        },
                        "technical_features": initial_exploration.get("technical_features") or {},
                        "exploration_summary": (
                            "Engine completed the bounded retained Explore session after "
                            f"{meaningful_document_batches} meaningful document action batches."
                        ),
                        "explore_complete": True,
                        "tool_action_count": _nonnegative_int(ledger.get("completed_batches")),
                        "meaningful_document_batch_count": meaningful_document_batches,
                    }
                try:
                    decide = getattr(self.agent, "decide_explore_native", self.agent.decide_explore)
                    decision = decide(
                        site_url=site_url,
                        runtime_version=self.settings.discovery_runtime_version,
                        rag_references=rag_references,
                        observations=observations,
                        remaining_actions=None,
                        timeout_seconds=deadline.remaining(),
                        session_id=run_id,
                        evidence_ledger=ledger,
                        verified_evidence={"explore_session": ledger},
                    )
                    if decision.action == "inspect" and not str(decision.action_summary).strip():
                        raise ValueError("Explore inspect action requires an objective")
                except (ValueError, ValidationError) as exc:
                    observation = {
                        "action_number": action_number,
                        "action": "agent_tool_call",
                        "error": {
                            "type": type(exc).__name__,
                            "message": redact_discovery_text(str(exc))[:4000],
                            "stage": "explore_decision",
                        },
                        "repair_hint": "提交 run_explore_actions 的 1..6 个声明式动作，或在入口文档已真实观察后结束 Explore。",
                    }
                    observations.append(observation)
                    observations = _bounded_explore_checkpoint_observations(observations)
                    self._event(
                        run_id, "explore_tool_observation", "Explore 动作计划格式错误，已反馈 Agent 修正。",
                        "explore", round_number, observation, level="warning",
                    )
                    continue

                if decision.action == "finish":
                    if not ledger["entry_observed"]:
                        observation = {
                            "action_number": action_number,
                            "action": "finish",
                            "error": {
                                "type": "ExploreDecisionRejected",
                                "message": "Explore cannot finish without a successful fixed-session entry document observation",
                                "stage": "explore",
                            },
                            "session_ledger": ledger,
                            "repair_hint": "先用 HTTP GET 或 browser open 成功读取入口文档；不需要生成分页或字段 proof。",
                        }
                        observations.append(observation)
                        observations = _bounded_explore_checkpoint_observations(observations)
                        self._event(
                            run_id, "explore_tool_observation", "入口文档尚未由保留 gVisor 会话成功观察，继续 Explore。",
                            "explore", round_number, observation, level="warning",
                        )
                        continue
                    if _explore_requires_candidate_records(ledger):
                        observation = {
                            "action_number": action_number,
                            "action": "finish",
                            "error": {
                                "type": "ExploreDecisionRejected",
                                "message": "Explore cannot finish an HTML entry with observed links without record-level candidate evidence",
                                "stage": "explore",
                            },
                            "session_ledger": ledger,
                            "repair_hint": (
                                "对已铸造的入口 page_id 使用 browser records；selector 选择重复候选记录容器，"
                                "让每条记录保留同一条目的文本和链接。"
                            ),
                        }
                        observations.append(observation)
                        observations = _bounded_explore_checkpoint_observations(observations)
                        self._event(
                            run_id, "explore_tool_observation", "入口 HTML 尚缺记录级候选证据，继续 Explore。",
                            "explore", round_number, observation, level="warning",
                        )
                        continue
                    if _explore_requires_detail_evidence(ledger):
                        observation = {
                            "action_number": action_number,
                            "action": "finish",
                            "error": {
                                "type": "ExploreDecisionRejected",
                                "message": (
                                    "Explore cannot finish a short-text listing without one "
                                    "candidate-bound detail content and published_at observation"
                                ),
                                "stage": "explore",
                            },
                            "session_ledger": ledger,
                            "repair_hint": (
                                "打开 candidate_records 中一个单链接候选详情 URL；随后对同一详情 page_id "
                                "分别执行 browser text 或 browser attribute，设置 evidence_role=content 和 published_at。"
                                "content 的清洗文本至少需要 200 字符。"
                            ),
                        }
                        observations.append(observation)
                        observations = _bounded_explore_checkpoint_observations(observations)
                        self._event(
                            run_id,
                            "explore_tool_observation",
                            "列表文字不足，尚缺同一候选详情的正文与日期来源证据，继续 Explore。",
                            "explore",
                            round_number,
                            observation,
                            level="warning",
                        )
                        continue
                    technical_features = (
                        dict(decision.technical_features)
                        if isinstance(decision.technical_features, dict)
                        else {}
                    )
                    return {
                        "explore_session": {
                            "version": 2,
                            "observations": observations,
                            "evidence": ledger,
                        },
                        "technical_features": technical_features,
                        "exploration_summary": decision.exploration_summary,
                        "explore_complete": True,
                        "tool_action_count": _nonnegative_int(ledger.get("completed_batches")),
                        "meaningful_document_batch_count": meaningful_document_batches,
                    }

                try:
                    requested_extensions = [
                        domain for domain in decision.allowed_domains
                        if domain not in session.allowed_domains
                    ]
                    if requested_extensions:
                        session.extend_allowed_domains(requested_extensions)
                    actions = validate_explore_actions(
                        decision.args.get("actions"),
                        allowed_domains=session.allowed_domains,
                    )
                    if (
                        ledger.get("browser_success") is True
                        and _explore_requires_candidate_records(ledger)
                        and not any(
                            action.get("tool") == "browser"
                            and action.get("operation") == "records"
                            for action in actions
                        )
                    ):
                        raise ValueError(
                            "browser records is required before more Explore actions on an observed HTML link list"
                        )
                    fingerprint = explore_action_fingerprint(actions)
                    if fingerprint in seen_fingerprints:
                        raise ValueError(
                            "this exact retained Explore action batch already ran; choose new actions or finish Explore"
                        )
                    seen_fingerprints.add(fingerprint)
                except ValueError as exc:
                    observation = {
                        "action_number": action_number,
                        "action": "inspect",
                        "error": {
                            "type": "ExploreSessionPlanRejected",
                            "message": redact_discovery_text(str(exc))[:4000],
                            "stage": "explore",
                        },
                        "session_ledger": ledger,
                    }
                    observations.append(observation)
                    observations = _bounded_explore_checkpoint_observations(observations)
                    self._event(
                        run_id, "explore_tool_observation", "Explore 声明式动作被宿主策略拒绝。",
                        "explore", round_number, observation, level="warning",
                    )
                    continue

                self._event(
                    run_id,
                    "explore_action_selected",
                    decision.action_summary,
                    "explore",
                    round_number,
                    {
                        "action": "inspect",
                        "action_number": action_number,
                        "objective": str(decision.args.get("objective") or decision.action_summary)[:2000],
                        "operations": explore_action_summary(actions),
                        "allowed_domains": list(session.allowed_domains),
                        "retained_session": True,
                    },
                )
                try:
                    raw_observation = session.execute(actions)
                    observation = {
                        "action_number": action_number,
                        "action": "inspect",
                        "objective": str(decision.args.get("objective") or decision.action_summary)[:2000],
                        "result": project_explore_observation(raw_observation),
                    }
                    records = raw_observation.get("records") if isinstance(raw_observation, dict) else []
                    self._event(
                        run_id,
                        "sandbox_tool_evidence",
                        "保留 gVisor Explore 会话已记录本批宿主编号的工具结果。",
                        "explore",
                        round_number,
                        {
                            "job_id": job_id,
                            "record_count": len(records) if isinstance(records, list) else 0,
                            "record_ids": [
                                record.get("record_id") for record in records[:6]
                                if isinstance(record, dict) and isinstance(record.get("record_id"), str)
                            ] if isinstance(records, list) else [],
                        },
                    )
                except ConnectorProtocolError as exc:
                    if exc.code == ConnectorErrorCode.CANCELLED:
                        raise DiscoveryCancelled("用户已取消智能探查") from exc
                    raise

                observations.append(observation)
                observations = _bounded_explore_checkpoint_observations(observations)
                ledger = _explore_session_ledger([observation], site_url=site_url, base=ledger)
                meaningful_document_batches = _nonnegative_int(
                    ledger.get("meaningful_document_batches")
                )
                progress_exploration = {
                    "explore_session": {
                        "version": 2,
                        "observations": observations,
                        "evidence": ledger,
                    },
                    "technical_features": initial_exploration.get("technical_features") or {},
                    "explore_complete": False,
                }
                self._save_round_checkpoint(
                    run_id=run_id,
                    phase="explore",
                    round_number=round_number,
                    trial=None,
                    rag_references=rag_references,
                    exploration=progress_exploration,
                    execution_result={"explore_session": ledger},
                    tool_evidence=[observation],
                    evaluation_result={"passed": False, "session_entry_observed": ledger["entry_observed"]},
                    code_diff=None,
                    error=None,
                    elapsed_seconds=deadline.elapsed(),
                    token_usage=token_usage.total_tokens,
                )
                self._event(
                    run_id,
                    "explore_tool_observation",
                    "保留 Explore 会话已返回本批真实工具观察。",
                    "explore",
                    round_number,
                    observation,
                )
        finally:
            if session is not None:
                session.close()
            with self._job_lock:
                self._run_jobs.get(run_id, set()).discard(job_id)

    def _execute(
        self,
        run_id: int,
        phase: str,
        round_number: int,
        artifact: ConnectorArtifact,
        *,
        request: ConnectorRequest,
        suffix: str,
        deadline: _LoopDeadline,
    ) -> SandboxExecutionResult:
        self._guard_active(run_id, deadline)
        job_id = f"discovery-{run_id}-{round_number}-{suffix}"
        invocation = ConnectorInvocation(
            request=request,
            context=ConnectorContext(
                run_id=run_id,
                connector_key=artifact.manifest.connector_key,
                connector_version=artifact.manifest.version,
                allowed_domains=artifact.manifest.allowed_domains,
            ),
        )
        priority = SandboxJobPriority.REPAIR if self._trigger_type(run_id) == "repair" else SandboxJobPriority.DISCOVERY
        execution = SandboxExecution(
            job_id=job_id,
            artifact=artifact,
            invocation=invocation,
            kind="sites",
            priority=priority,
            expected_checksum=artifact.manifest.checksum,
            expected_signature=artifact.signature,
            timeout_seconds=max(0.1, min(self.settings.discovery_sandbox_timeout_seconds, deadline.remaining())),
            purpose=(
                "primary_trial" if suffix in {"trial-1", "trial-2"}
                else "pagination" if suffix == "page-2"
                else "url_verifier" if suffix == "url-verify"
                else "field_smoke" if suffix == "field-smoke"
                else "other"
            ),
            trial_index=(int(suffix[-1]) if suffix in {"trial-1", "trial-2"} else None),
        )
        for transport_attempt in range(2):
            with self._job_lock:
                self._run_jobs.setdefault(run_id, set()).add(job_id)
            self._sandbox_capacity_queued(
                run_id,
                deadline,
                phase=phase,
                round_number=round_number,
                summary="沙箱任务已进入容量队列，执行预算暂停计时。",
                extra_payload={"transport_attempt": transport_attempt + 1},
            )
            self._mark_waiting_for_sandbox(run_id)
            try:
                result = self.sandbox.execute(
                    execution,
                    on_started=lambda: self._sandbox_started(
                        run_id, deadline, phase=phase, round_number=round_number
                    ),
                )
                self._guard_active(run_id, deadline)
                self._event(
                    run_id,
                    "sandbox_tool_evidence",
                    "沙箱工具证据已按单次执行聚合持久化。",
                    phase,
                    round_number,
                    {
                        "job_id": job_id,
                        "event_count": len(result.events),
                        "events": [redact_discovery_data(event) for event in result.events[:50]],
                    },
                )
                return result
            except ConnectorProtocolError as exc:
                if transport_attempt == 0 and _is_transport_failure(exc):
                    self._event(
                        run_id,
                        "transport_retry",
                        "检测到连接层失败；不改网页逻辑，原样重试同一制品一次。",
                        phase,
                        round_number,
                        {"job_id": job_id, "error": _structured_error(exc), "attempt": 2},
                        level="warning",
                    )
                    continue
                raise
            finally:
                with self._job_lock:
                    self._run_jobs.get(run_id, set()).discard(job_id)
        raise AssertionError("bounded transport retry loop did not converge")

    def _phase(self, run_id: int, phase: str, round_number: int,
               trace: list[dict[str, Any]], summary: str) -> None:
        self._check_registered_deadline(run_id)
        if phase not in PHASES:
            raise ValueError(f"invalid Loop phase: {phase}")
        timing_payload, _ = self._phase_change_timing(run_id, phase)
        db = SessionLocal()
        try:
            trace.append({"step": phase, "round": round_number, "summary": summary})
            changed = db.execute(
                update(SiteDiscoveryRun)
                .where(
                    SiteDiscoveryRun.id == run_id,
                    SiteDiscoveryRun.status.in_(("queued", "running", "repairing")),
                )
                .values(phase=phase, round=round_number, node_trace=list(trace))
            )
            if changed.rowcount != 1:
                db.rollback()
                raise DiscoveryCancelled("Discovery run 已取消或不再允许推进阶段")
            append_discovery_event(
                run_id,
                event_type="phase_changed",
                summary=summary,
                phase=phase,
                round_number=round_number,
                payload=timing_payload,
                session=db,
            )
            db.commit()
        finally:
            db.close()
        # Start the new phase after its durable transition commits.  This keeps
        # the next phase's elapsed time from charging event persistence to it.
        phase_started_elapsed = self._active_elapsed(run_id)
        with self._job_lock:
            self._phase_started_elapsed[run_id] = (phase, phase_started_elapsed)
        self._check_registered_deadline(run_id)

    @staticmethod
    def _mark_waiting_for_sandbox(run_id: int) -> None:
        db = SessionLocal()
        try:
            changed = db.execute(
                update(SiteDiscoveryRun)
                .where(
                    SiteDiscoveryRun.id == run_id,
                    SiteDiscoveryRun.trigger_type != "repair",
                    SiteDiscoveryRun.status.in_(("queued", "running")),
                )
                .values(status="queued")
            )
            if changed.rowcount == 0:
                run = db.get(SiteDiscoveryRun, run_id)
                if run is None or run.status not in {"queued", "running", "repairing"}:
                    raise DiscoveryCancelled("Discovery run 已取消")
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _mark_sandbox_started(run_id: int) -> None:
        db = SessionLocal()
        try:
            run = db.get(SiteDiscoveryRun, run_id)
            if run is None:
                raise DiscoveryCancelled("Discovery run 不存在")
            target = "repairing" if run.trigger_type == "repair" else "running"
            changed = db.execute(
                update(SiteDiscoveryRun)
                .where(
                    SiteDiscoveryRun.id == run_id,
                    SiteDiscoveryRun.status.in_(("queued", "running", "repairing")),
                )
                .values(status=target)
            )
            if changed.rowcount != 1:
                db.rollback()
                raise DiscoveryCancelled("Discovery run 已取消")
            db.commit()
        finally:
            db.close()

    def _event(self, run_id: int, event_type: str, summary: str, phase: str, round_number: int,
               payload: dict[str, Any] | None = None, level: str = "info") -> None:
        self._check_registered_deadline(run_id)
        append_discovery_event(run_id, event_type=event_type, summary=summary, phase=phase,
                               round_number=round_number, payload=payload, level=level)
        self._check_registered_deadline(run_id)

    def _retrieve_experiences(
        self,
        *,
        run_id: int,
        stage: RagStage,
        event_phase: str,
        round_number: int,
        domain: str,
        technical_features: dict[str, Any],
        query: str,
    ) -> list[dict[str, Any]]:
        """Retrieve one trusted experience kind and persist non-secret hit counts."""
        db = SessionLocal()
        try:
            try:
                result = self.retriever.retrieve(
                    db,
                    stage=stage,
                    domain=domain,
                    technical_features=technical_features,
                    query=query,
                )
                references = [reference.as_prompt_data() for reference in result.references]
                append_discovery_event(
                    run_id,
                    event_type="rag_retrieved",
                    summary=(
                        f"RAG {stage} 检索完成：扫描 {result.scanned_count} 条，"
                        f"命中 {result.matched_count} 条，注入 {result.injected_count} 条。"
                    ),
                    phase=event_phase,
                    round_number=round_number,
                    payload={
                        "rag_stage": stage,
                        "scanned_count": result.scanned_count,
                        "matched_count": result.matched_count,
                        "injected_count": result.injected_count,
                    },
                    session=db,
                )
                db.commit()
                return references
            except Exception as exc:
                db.rollback()
                append_discovery_event(
                    run_id,
                    event_type="rag_unavailable",
                    summary=f"RAG {stage} 检索不可用；本阶段不注入经验。",
                    phase=event_phase,
                    round_number=round_number,
                    level="warning",
                    payload={"rag_stage": stage, "error": redact_discovery_text(str(exc))},
                    session=db,
                )
                db.commit()
                return []
        finally:
            db.close()

    def _check_registered_deadline(self, run_id: int) -> None:
        with self._job_lock:
            deadline = self._deadlines.get(run_id)
        if deadline is not None:
            deadline.check()

    def _terminal_event(
        self,
        run_id: int,
        event_type: str,
        summary: str,
        phase: str,
        round_number: int,
        payload: dict[str, Any] | None = None,
        level: str = "info",
    ) -> None:
        """Persist only the terminal diagnosis after timeout/cancel cleanup begins."""
        terminal_payload = dict(payload or {})
        terminal_payload.update(self._current_timing_payload(run_id, phase=phase))
        append_discovery_event(run_id, event_type=event_type, summary=summary, phase=phase,
                               round_number=round_number, payload=terminal_payload, level=level)

    def _phase_change_timing(self, run_id: int, phase: str) -> tuple[dict[str, Any], float]:
        """Return completed-phase and cumulative active time for a phase transition."""
        active_elapsed = self._active_elapsed(run_id)
        payload: dict[str, Any] = {
            "active_execution_elapsed_seconds": active_elapsed,
        }
        with self._job_lock:
            previous = self._phase_started_elapsed.get(run_id)
        if previous is not None:
            previous_phase, previous_started_elapsed = previous
            completed_elapsed = max(
                0.0,
                round(active_elapsed - previous_started_elapsed, 3),
            )
            payload.update({
                "completed_phase": previous_phase,
                "completed_phase_elapsed_seconds": completed_elapsed,
                # Keep the stable generic field present on phase boundaries as
                # well.  timed_phase removes any ambiguity that it describes
                # the phase just completed, not the one being entered.
                "phase_elapsed_seconds": completed_elapsed,
                "timed_phase": previous_phase,
            })
        return payload, active_elapsed

    def _current_timing_payload(self, run_id: int, *, phase: str) -> dict[str, Any]:
        """Project current phase and total active time into a terminal public event."""
        active_elapsed = self._active_elapsed(run_id)
        payload: dict[str, Any] = {
            "active_execution_elapsed_seconds": active_elapsed,
        }
        with self._job_lock:
            current = self._phase_started_elapsed.get(run_id)
        if current is not None:
            current_phase, phase_started_elapsed = current
            payload["phase_elapsed_seconds"] = max(
                0.0,
                round(active_elapsed - phase_started_elapsed, 3),
            )
            # Do not trust a caller-supplied arbitrary label when presenting
            # timing; the phase recorded by the deterministic loop wins.
            payload["timed_phase"] = current_phase if current_phase in PHASES else phase
        return payload

    def _active_elapsed(self, run_id: int) -> float:
        with self._job_lock:
            deadline = self._deadlines.get(run_id)
        return round(deadline.elapsed(), 3) if deadline is not None else 0.0

    def _sandbox_capacity_queued(
        self,
        run_id: int,
        deadline: _LoopDeadline,
        *,
        phase: str,
        round_number: int,
        summary: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist a queue boundary and begin measuring wall-clock queue wait."""
        elapsed_snapshot = deadline.pause_for_capacity()
        with self._job_lock:
            self._capacity_wait_started_at[run_id] = time.monotonic()
        payload: dict[str, Any] = {
            "maximum_seconds": deadline.maximum_seconds,
            "elapsed_snapshot": round(elapsed_snapshot, 3),
            "active_execution_elapsed_seconds": round(elapsed_snapshot, 3),
        }
        if extra_payload:
            payload.update(extra_payload)
        append_discovery_event(
            run_id,
            event_type="sandbox_capacity_queued",
            summary=summary,
            phase=phase,
            round_number=round_number,
            payload=payload,
        )

    def _sandbox_started(
        self,
        run_id: int,
        deadline: _LoopDeadline,
        *,
        phase: str,
        round_number: int,
    ) -> None:
        deadline.start()
        deadline.check()
        self._mark_sandbox_started(run_id)
        with self._job_lock:
            queued_at = self._capacity_wait_started_at.pop(run_id, None)
        queue_wait_seconds = (
            max(0.0, round(time.monotonic() - queued_at, 3))
            if queued_at is not None
            else 0.0
        )
        append_discovery_event(
            run_id,
            event_type="sandbox_capacity_acquired",
            summary="已获得 gVisor 沙箱容量，三十分钟总执行预算继续计时。",
            phase=phase,
            round_number=round_number,
            payload={
                "maximum_seconds": deadline.maximum_seconds,
                "already_elapsed_seconds": deadline.elapsed(),
                "active_execution_elapsed_seconds": self._active_elapsed(run_id),
                "queue_wait_seconds": queue_wait_seconds,
            },
        )

    @staticmethod
    def _guard_active(run_id: int, deadline: _LoopDeadline) -> None:
        ensure_not_cancelled()
        deadline.check()
        db = SessionLocal()
        try:
            run = db.get(SiteDiscoveryRun, run_id)
            if run is None:
                raise LookupError(f"Discovery run {run_id} does not exist")
            if run.status == "cancelled":
                raise DiscoveryCancelled("用户已取消智能探查")
            if run.status not in {"queued", "running", "repairing"}:
                raise RuntimeError(f"Discovery run is no longer active: {run.status}")
        finally:
            db.close()

    @staticmethod
    def _site_url(run_id: int) -> str:
        db = SessionLocal()
        try:
            run = db.get(SiteDiscoveryRun, run_id)
            if run is None:
                raise LookupError(f"Discovery run {run_id} does not exist")
            return run.site_url
        finally:
            db.close()

    @staticmethod
    def _trigger_type(run_id: int) -> str:
        db = SessionLocal()
        try:
            run = db.get(SiteDiscoveryRun, run_id)
            return run.trigger_type if run else "manual"
        finally:
            db.close()

    @staticmethod
    def _agent_budget(run_id: int) -> dict[str, int]:
        db = SessionLocal()
        try:
            run = db.get(SiteDiscoveryRun, run_id)
            if run is None:
                raise LookupError(f"Discovery run {run_id} does not exist")
            return normalize_agent_budget(run.agent_budget)
        finally:
            db.close()

    @staticmethod
    def _is_automatic_repair_run(run_id: int) -> bool:
        """Keep the legacy-checkpoint fallback correct after a resumed repair run."""
        db = SessionLocal()
        try:
            run = db.get(SiteDiscoveryRun, run_id)
            return bool(run and run.repair_method_id is not None)
        finally:
            db.close()

    def _load_resume_source(self, checkpoint: DiscoveryCheckpoint | None) -> str | None:
        if checkpoint is None or not checkpoint.connector_draft_path:
            return None
        path = self.checkpoints.resolve_workspace_path(checkpoint.run_id, checkpoint.connector_draft_path)
        if not path.is_file() or path.is_symlink():
            raise ValueError("resume checkpoint connector draft is missing or unsafe")
        return path.read_text(encoding="utf-8")

    def _load_resume_trial(self, checkpoint: DiscoveryCheckpoint) -> ConnectorArtifact:
        if not checkpoint.manifest_path:
            raise ValueError("passed resume checkpoint is missing its manifest")
        manifest_path = self.checkpoints.resolve_workspace_path(
            checkpoint.run_id,
            checkpoint.manifest_path,
        )
        manifest = ConnectorManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        trial_root = self.checkpoints.workspace_root / f"run-{checkpoint.run_id}" / "trial"
        return load_connector_artifact(
            trial_root,
            kind="sites",
            connector_key=manifest.connector_key,
            version=manifest.version,
        )

    def _copy_resume_trial_for_checkpoint(
        self,
        *,
        run_id: int,
        checkpoint: DiscoveryCheckpoint,
        site_url: str,
    ) -> ConnectorArtifact | None:
        """Copy a safe prior draft into the new resume workspace for terminal evidence."""
        if not checkpoint.connector_draft_path or not checkpoint.manifest_path:
            return None
        previous_trial = self._load_resume_trial(checkpoint)
        source = self._load_resume_source(checkpoint)
        if source is None:
            return None
        return write_trial_artifact(
            run_id=run_id,
            site_url=site_url,
            source=source,
            allowed_domains=list(previous_trial.manifest.allowed_domains),
            runtime_version=self.settings.discovery_runtime_version,
            connector_key=previous_trial.manifest.connector_key,
            version=previous_trial.manifest.version,
            time_semantics=previous_trial.manifest.time_semantics,
            settings=self.settings,
        )

    def _package_trial(
        self,
        *,
        run_id: int,
        trial: ConnectorArtifact,
        display_name: str,
        force: bool,
        round_number: int,
        trace: list[dict[str, Any]],
        token_usage: int,
        deadline: _LoopDeadline,
        plugin_review: dict[str, Any],
        expected_claim_owner: str,
        claim_lost: threading.Event,
    ) -> None:
        """Package a previously evaluated trial; shared by round-5 resume."""
        staged = None
        method_id: int | None = None
        try:
            _assert_terminal_claim(run_id, expected_claim_owner, claim_lost)
            staged = stage_pending_artifact(trial=trial, settings=self.settings)
            _assert_terminal_claim(run_id, expected_claim_owner, claim_lost)
            packaged_method_audit = bind_packaged_artifact_evidence(
                dict(plugin_review.get("method_audit") or {}),
                trial=trial,
                packaged=staged.artifact,
                kind="sites",
                packaging_run_id=run_id,
            )
            self._guard_active(run_id, deadline)
            packaging_db = SessionLocal()
            try:
                locked_run = packaging_db.scalar(
                    select(SiteDiscoveryRun).where(
                        SiteDiscoveryRun.id == run_id,
                        SiteDiscoveryRun.cleanup_claim_owner == expected_claim_owner,
                        SiteDiscoveryRun.cleanup_claim_expires_at
                        > _db_wall_clock_expression(packaging_db),
                    ).with_for_update()
                )
                if (
                    claim_lost.is_set()
                    or locked_run is None
                    or locked_run.status not in {"running", "repairing"}
                ):
                    raise DiscoveryCancelled("Discovery run 已取消或不再允许封装")
                method = create_packaging_method(
                    packaging_db,
                    staged=staged,
                    display_name=display_name,
                    force=force,
                    method_audit=packaged_method_audit,
                    quality_audit=quality_audit_from_evidence(
                        dict(plugin_review.get("quality_audit") or {})
                    ),
                    quality_trial_evidence=list(plugin_review.get("quality_trials") or []),
                )
                method_id = method.id
                self._guard_active(run_id, deadline)
                packaging_db.commit()
            except BaseException:
                packaging_db.rollback()
                raise
            finally:
                packaging_db.close()
            _assert_terminal_claim(run_id, expected_claim_owner, claim_lost)
            self._guard_active(run_id, deadline)
            final_db = SessionLocal()
            final_commit_attempted = False
            try:
                locked_run = final_db.scalar(
                    select(SiteDiscoveryRun).where(
                        SiteDiscoveryRun.id == run_id,
                        SiteDiscoveryRun.cleanup_claim_owner == expected_claim_owner,
                        SiteDiscoveryRun.cleanup_claim_expires_at
                        > _db_wall_clock_expression(final_db),
                    ).with_for_update()
                )
                method = final_db.scalar(
                    select(CrawlMethod).where(CrawlMethod.id == method_id).with_for_update()
                )
                if (
                    locked_run is None
                    or claim_lost.is_set()
                    or locked_run.status not in {"running", "repairing"}
                    or method is None
                    or method.status != "packaging"
                ):
                    raise DiscoveryCancelled("Discovery run 在制品发布前已取消")
                db_now = _db_wall_clock(final_db)
                db_clock = _db_wall_clock_expression(final_db)
                fenced_renewal = final_db.execute(
                    update(SiteDiscoveryRun)
                    .where(
                        SiteDiscoveryRun.id == run_id,
                        SiteDiscoveryRun.status.in_(("running", "repairing")),
                        SiteDiscoveryRun.cleanup_claim_owner == expected_claim_owner,
                        SiteDiscoveryRun.cleanup_claim_expires_at > db_clock,
                    )
                    .values(cleanup_claim_expires_at=db_now + timedelta(minutes=10))
                )
                if fenced_renewal.rowcount != 1:
                    raise RuntimeError("Package fencing lease expired before publication")
                # The run row lock fences takeover across the filesystem
                # publication and terminal DB commit.  A stale owner cannot
                # pass the owner-qualified final UPDATE.
                publish_staged_artifact(staged)
                if claim_lost.is_set():
                    raise RuntimeError("terminal claim heartbeat was lost during publication")
                activate_packaging_method(final_db, method)
                ensure_not_cancelled()
                deadline.check()
                completed = final_db.execute(
                    update(SiteDiscoveryRun)
                    .where(
                        SiteDiscoveryRun.id == run_id,
                        SiteDiscoveryRun.status.in_(("running", "repairing")),
                        SiteDiscoveryRun.cleanup_claim_owner == expected_claim_owner,
                        SiteDiscoveryRun.cleanup_claim_expires_at
                        > _db_wall_clock_expression(final_db),
                    )
                    .values(
                        status="completed",
                        resulting_method_id=method_id,
                        node_trace=trace,
                        llm_token_usage=token_usage,
                        phase="package",
                        round=round_number,
                        runtime_version=self.settings.discovery_runtime_version,
                        error_message=None,
                        ended_at=datetime.now(timezone.utc),
                        cleanup_claim_owner=None,
                        cleanup_claim_expires_at=None,
                    )
                )
                if completed.rowcount != 1:
                    raise DiscoveryCancelled("Discovery run 在封装前已取消")
                append_discovery_event(
                    run_id,
                    event_type="artifact_pending_review",
                    summary="插件制品已进入待审核，未发布。",
                    phase="package",
                    round_number=round_number,
                    payload={
                        "method_id": method_id,
                        "review_status": "pending",
                        **self._current_timing_payload(run_id, phase="package"),
                    },
                    session=final_db,
                )
                if claim_lost.is_set():
                    raise RuntimeError("terminal claim heartbeat was lost before Package commit")
                ensure_not_cancelled()
                deadline.check()
                final_commit_attempted = True
                final_db.commit()
            except BaseException:
                # If publication happened but the lease expired or commit was
                # fenced, remove the unactivated artifact while the run row is
                # still locked so a successor cannot observe stale output.
                if (
                    not final_commit_attempted
                    and staged is not None
                    and staged.final_directory.exists()
                ):
                    try:
                        discard_staged_artifact(staged)
                    except BaseException as cleanup_exc:
                        final_db.rollback()
                        raise _PackageCompensationFailed(
                            "failed to remove fenced Package artifact"
                        ) from cleanup_exc
                final_db.rollback()
                raise
            finally:
                final_db.close()
        except BaseException as package_exc:
            cleanup_db = None
            try:
                if method_id is not None and self._package_is_committed(run_id, method_id):
                    return
                # A staging call that raised before returning an owned path may
                # have partially mutated disk.  No handle means compensation
                # is unknown, never optimistically clean.
                if staged is None:
                    raise RuntimeError("Package staging ownership is unknown")
                cleanup_db = SessionLocal()
                if method_id is not None:
                    method = cleanup_db.scalar(
                        select(CrawlMethod).where(CrawlMethod.id == method_id).with_for_update()
                    )
                    if method is not None and (
                        method.status != "packaging" or method.review_status != "pending"
                    ):
                        raise ValueError("packaging method left its internal state")
                if staged is not None:
                    discard_staged_artifact(staged)
                if method_id is not None:
                    discard_packaging_method(cleanup_db, method_id)
                cleanup_db.commit()
            except BaseException as cleanup_exc:
                if cleanup_db is not None:
                    cleanup_db.rollback()
                logger.exception("failed to compensate package method_id=%s", method_id)
                raise _PackageCompensationFailed(
                    "Package failed and compensation could not be confirmed"
                ) from cleanup_exc
            finally:
                if cleanup_db is not None:
                    try:
                        cleanup_db.close()
                    except BaseException as close_exc:
                        raise _PackageCompensationFailed(
                            "Package cleanup session close was not confirmed"
                        ) from close_exc
            raise _PackageFailureAfterCompensation(
                f"Package failed after compensated {type(package_exc).__name__}"
            ) from package_exc

    @staticmethod
    def _package_is_committed(run_id: int, method_id: int) -> bool:
        """Resolve an ambiguous commit result before attempting compensation."""
        db = SessionLocal()
        try:
            run = db.get(SiteDiscoveryRun, run_id)
            method = db.get(CrawlMethod, method_id)
            return bool(
                run is not None
                and run.status == "completed"
                and run.resulting_method_id == method_id
                and method is not None
                and method.status == "pending"
                and method.review_status == "pending"
            )
        finally:
            db.close()

    def _save_initial_failure_checkpoint(
        self,
        run_id: int,
        token_usage: int,
        elapsed_seconds: float,
        error: str,
    ) -> None:
        """Preserve diagnostics when failure happens before the first round checkpoint."""
        db = SessionLocal()
        try:
            run = db.get(SiteDiscoveryRun, run_id)
            if run is None or run.checkpoint_path:
                return
            checkpoint = DiscoveryCheckpoint(
                run_id=run_id,
                phase=run.phase or "context",
                round=max(0, run.round),
                error={"message": redact_discovery_text(error)[:4000]},
                elapsed_seconds=max(0.0, elapsed_seconds),
                token_usage=max(0, token_usage),
                runtime_version=self.settings.discovery_runtime_version,
            )
            self.checkpoints.save(checkpoint, session=db)
            db.commit()
        except Exception:  # noqa: BLE001 - do not mask the original terminal error
            db.rollback()
            logger.exception("failed to persist initial Discovery failure checkpoint run_id=%s", run_id)
        finally:
            db.close()

    def _save_round_checkpoint(
        self,
        *,
        run_id: int,
        phase: str,
        round_number: int,
        trial: ConnectorArtifact | None,
        rag_references: list[dict[str, Any]],
        exploration: dict[str, Any],
        execution_result: dict[str, Any] | None,
        tool_evidence: list[dict[str, Any]],
        evaluation_result: dict[str, Any],
        code_diff: str | None,
        error: dict[str, Any] | None,
        elapsed_seconds: float,
        token_usage: int,
        enforce_deadline: bool = True,
    ) -> None:
        if enforce_deadline:
            self._check_registered_deadline(run_id)
        connector_path = None
        manifest_path = None
        if trial is not None:
            base = f"trial/sites/{trial.manifest.connector_key}/v{trial.manifest.version}"
            connector_path = f"{base}/crawler.py"
            manifest_path = f"{base}/manifest.json"
        checkpoint = DiscoveryCheckpoint(
            run_id=run_id,
            phase=phase,
            round=round_number,
            connector_draft_path=connector_path,
            manifest_path=manifest_path,
            rag_references=rag_references,
            tool_evidence=[
                {"kind": "sandbox", "round": round_number, "error": error},
                *tool_evidence[:100],
            ],
            processing_summary={
                "explore_session": exploration.get("explore_session"),
                "explore_complete": bool(exploration.get("explore_complete")),
                "technical_features": exploration.get("technical_features") or {},
                "exploration_summary": exploration.get("exploration_summary"),
                "supports_pagination": bool(exploration.get("supports_pagination")),
                "effective_repair_attempts": _bounded_repair_attempts(
                    exploration.get("effective_repair_attempts")
                ),
                "consecutive_repair_noops": _bounded_consecutive_repair_noops(
                    exploration.get("consecutive_repair_noops")
                ),
                "consecutive_agent_draft_contract_failures": (
                    _bounded_agent_draft_contract_failures(
                        exploration.get("consecutive_agent_draft_contract_failures")
                    )
                ),
                "cleanup_pending": exploration.get("cleanup_pending"),
                "package_finalization": exploration.get("package_finalization"),
            },
            execution_result=execution_result,
            evaluation_result=evaluation_result,
            code_diff=code_diff,
            error=error,
            elapsed_seconds=elapsed_seconds,
            token_usage=token_usage,
            runtime_version=self.settings.discovery_runtime_version,
        )
        db = SessionLocal()
        try:
            self.checkpoints.save(checkpoint, session=db)
            db.commit()
        finally:
            db.close()
        if enforce_deadline:
            self._check_registered_deadline(run_id)


_engine: WebsiteLoopEngine | None = None
_engine_lock = threading.Lock()


def get_website_loop_engine() -> WebsiteLoopEngine:
    global _engine
    created = False
    with _engine_lock:
        if _engine is None:
            _engine = WebsiteLoopEngine()
            created = True
        engine = _engine
    if created:
        from app.discovery.loop.resume_dispatcher import dispatch_resumed_loop

        register_resume_dispatcher(dispatch_resumed_loop)
    return engine


def start_website_loop_run(
    site_url: str,
    *,
    force: bool = True,
    name: str | None = None,
    agent_budget: dict[str, int] | None = None,
) -> int:
    return get_website_loop_engine().start(
        site_url,
        force=force,
        name=name,
        agent_budget=agent_budget,
    )


def cancel_website_loop_run(run_id: int) -> bool:
    # Cancellation must not bootstrap a new engine (or query its process DB) for
    # a run that belongs to another route/test session.
    with _engine_lock:
        engine = _engine
    return engine.cancel(run_id) if engine is not None else False


def website_loop_timing_payload(run_id: int) -> dict[str, Any]:
    """Read in-process active timing without bootstrapping an idle loop engine."""
    with _engine_lock:
        engine = _engine
    return engine.current_timing_payload(run_id) if engine is not None else {}


def _connector_base_key(site_url: str) -> str:
    from app.discovery.loop.artifacts import connector_key_for_url

    return connector_key_for_url(site_url)


def _host_allowed(url: str, allowed_domains: tuple[str, ...]) -> bool:
    try:
        host = (urlsplit(url).hostname or "").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return False
    return host in allowed_domains


def _exact_entry_domain(site_url: str) -> list[str]:
    """Return only the exact user-supplied entry hostname."""
    host = (urlsplit(site_url).hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("site URL must contain a hostname")
    return [host]


def _artifact_allowed_domains(site_url: str, domains: list[str]) -> list[str]:
    """Retain the exact entry host plus host-observed extensions."""
    return list(ConnectorManifest.validate_allowed_domains(
        tuple([*domains, *_exact_entry_domain(site_url)])
    ))


def _code_diff(previous: str | None, current: str) -> str:
    return "\n".join(difflib.unified_diff(
        (previous or "").splitlines(), current.splitlines(),
        fromfile="previous/crawler.py", tofile="current/crawler.py", lineterm="",
    ))


def _bounded_repair_attempts(value: object) -> int:
    """Coerce persisted accounting without allowing malformed state to extend retries."""
    if isinstance(value, bool):
        return 0
    try:
        return min(MAX_REPAIR_ATTEMPTS, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _bounded_agent_draft_contract_failures(value: object) -> int:
    """Restore only a safe consecutive malformed-draft count from checkpoints."""
    if isinstance(value, bool):
        return 0
    try:
        return min(
            MAX_CONSECUTIVE_AGENT_DRAFT_CONTRACT_FAILURES,
            max(0, int(value)),
        )
    except (TypeError, ValueError, OverflowError):
        return 0


def _bounded_consecutive_repair_noops(value: object) -> int:
    """Restore a bounded no-op streak without allowing checkpoints to extend it."""
    if isinstance(value, bool):
        return 0
    try:
        return min(MAX_CONSECUTIVE_REPAIR_NOOPS, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _resume_repair_attempts(
    checkpoint: DiscoveryCheckpoint | None,
    *,
    is_automatic_repair: bool,
) -> int:
    """Restore bounded Repair accounting, including checkpoints written before it existed."""
    if checkpoint is None:
        return 0
    summary = checkpoint.processing_summary or {}
    persisted = summary.get("effective_repair_attempts", summary.get("repair_attempts"))
    if persisted is not None:
        return _bounded_repair_attempts(persisted)
    # Older checkpoints only record a round. A normal run begins with Build in
    # round 1; an automatic repair run starts directly in Repair. This fallback
    # is deliberately conservative so an interrupted legacy run cannot regain
    # an unbounded retry budget.
    completed_turns = max(0, checkpoint.round)
    return _bounded_repair_attempts(
        completed_turns if is_automatic_repair else max(0, completed_turns - 1)
    )


def _resume_failure_error(
    checkpoint: DiscoveryCheckpoint | None,
    evaluation: dict[str, Any],
) -> dict[str, Any] | None:
    """Retain an interrupted failure as Repair evidence instead of silently rebuilding."""
    if checkpoint is None or evaluation.get("passed"):
        return None
    if isinstance(checkpoint.error, dict):
        return dict(checkpoint.error)
    if isinstance(checkpoint.error, str) and checkpoint.error.strip():
        return {"type": "ResumeCheckpointFailure", "message": checkpoint.error[:4000]}
    failures = evaluation.get("failures")
    if isinstance(failures, list) and failures:
        return {
            "type": "ResumeCheckpointFailure",
            "message": "恢复最近一次未通过的确定性验收。",
        }
    return None


def _repair_limit_diagnostics(
    *,
    attempts: int,
    limit: int,
    previous_evaluation: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Produce a mechanism-independent terminal failure from existing evidence."""
    message = (
        f"已完成 {attempts} 次 Repair，仍未通过确定性验收；"
        "已停止自动重试并保留最近检查点。"
    )
    error = {
        "type": "RepairAttemptLimitReached",
        "message": message,
        "stage": "repair",
        "effective_repair_attempts": attempts,
        "max_repair_attempts": limit,
    }
    evaluation = dict(previous_evaluation)
    failures = [
        dict(item) for item in evaluation.get("failures") or []
        if isinstance(item, dict)
    ]
    failures.append({
        "check": "repair_attempt_limit",
        "passed": False,
        "attempts": attempts,
        "limit": limit,
        "message": message,
    })
    evaluation["passed"] = False
    evaluation["failures"] = failures
    return error, evaluation


def _merge_rag_references(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve stage order while avoiding duplicate experience injection/checkpoints."""
    merged: list[dict[str, Any]] = []
    seen: set[int] = set()
    for group in groups:
        for reference in group:
            experience_id = reference.get("experience_id")
            if not isinstance(experience_id, int) or experience_id in seen:
                continue
            seen.add(experience_id)
            merged.append(reference)
    return merged


def _structured_error(exc: BaseException) -> dict[str, Any]:
    error: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": redact_discovery_text(str(exc))[:4000],
    }
    if isinstance(exc, ConnectorProtocolError):
        error["code"] = exc.code.value
        try:
            error["details"] = redact_discovery_data(exc.details)
        except ValueError:
            error["details"] = {"truncated": True}
    elif isinstance(exc, DiscoveryDataBoundsError):
        error["details"] = dict(exc.details)
    return error


def _evaluation_event_projection(evaluation: dict[str, Any]) -> dict[str, Any]:
    """Keep SSE evaluation events useful without duplicating full audit evidence."""
    checks = evaluation.get("checks")
    failures = evaluation.get("failures")
    review = evaluation.get("plugin_review")
    method_audit = review.get("method_audit") if isinstance(review, dict) else None
    return {
        "passed": evaluation.get("passed") is True,
        "checks": [
            {
                key: item.get(key)
                for key in ("check", "passed", "actual", "required", "applicable", "message")
                if key in item
            }
            for item in checks[:30]
            if isinstance(item, dict)
        ] if isinstance(checks, list) else [],
        "failures": [
            {
                key: item.get(key)
                for key in ("check", "passed", "actual", "required", "message", "stage")
                if key in item
            }
            for item in failures[:20]
            if isinstance(item, dict)
        ] if isinstance(failures, list) else [],
        "plugin_review": {
            "package_eligible": review.get("package_eligible") if isinstance(review, dict) else None,
            "method_audit": {
                key: method_audit.get(key)
                for key in ("passed", "status", "low_frequency_exception_eligible")
                if key in method_audit
            } if isinstance(method_audit, dict) else {},
        },
    }


def _is_meaningful_explore_document_record(record: dict[str, Any]) -> bool:
    """Return whether one trusted broker record contains a readable document.

    Redirects remain independently recorded in the ledger, but an HTTP redirect
    or HEAD response is not a document observation.  This is intentionally
    transport/outcome based: it does not assume a site's URL shape, HTML
    structure, or pagination mechanism.
    """
    if record.get("error") is not None:
        return False
    document = record.get("observation")
    if not isinstance(document, dict):
        return False
    status = document.get("status")
    if not isinstance(status, int) or not 200 <= status < 300:
        return False
    has_text = bool(str(document.get("text_excerpt") or document.get("text") or "").strip())
    tool = record.get("tool")
    action = record.get("operation")
    if tool == "http" and action == "GET":
        return (
            isinstance(document.get("body_size"), int)
            and document["body_size"] > 0
            and (has_text or isinstance(document.get("workspace_path"), str))
        )
    return tool == "browser" and action == "open" and has_text


def _observation_has_meaningful_explore_document(observation: dict[str, Any]) -> bool:
    """Count an Explore batch once when any record observed a real document."""
    result = observation.get("result")
    records = result.get("records") if isinstance(result, dict) else None
    return isinstance(records, list) and any(
        _is_meaningful_explore_document_record(record)
        for record in records
        if isinstance(record, dict)
    )


def _explore_requires_candidate_records(ledger: dict[str, Any]) -> bool:
    """Require DOM record evidence before building from an observed HTML link list."""
    candidate_records = ledger.get("candidate_records")
    if isinstance(candidate_records, list):
        for record in candidate_records:
            items = record.get("items") if isinstance(record, dict) else None
            if isinstance(items, list) and any(
                isinstance(item, dict)
                and bool(str(item.get("text") or "").strip())
                and bool(item.get("links"))
                for item in items
            ):
                return False
    documents = ledger.get("documents")
    if not isinstance(documents, list):
        return False
    return any(
        isinstance(document, dict)
        and (
            str(document.get("content_type") or "").lower().startswith("text/html")
            or (
                document.get("tool") == "browser"
                and document.get("operation") == "open"
            )
        )
        and bool(document.get("links"))
        for document in documents
    )


def _cleaned_observation_chars(value: Any) -> int:
    return len(" ".join(str(value or "").split()))


def _candidate_detail_urls(candidate_records: Any) -> set[str]:
    urls: set[str] = set()
    if not isinstance(candidate_records, list):
        return urls
    for record in candidate_records:
        items = record.get("items") if isinstance(record, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            links = item.get("links") if isinstance(item, dict) else None
            if not isinstance(links, list) or len(links) != 1:
                continue
            url = _canonical_observed_http_url(links[0])
            if url is not None:
                urls.add(url)
    return urls


def _explore_requires_detail_evidence(ledger: dict[str, Any]) -> bool:
    """Require one complete detail sample only when list records are too short."""
    candidate_records = ledger.get("candidate_records")
    candidate_urls = _candidate_detail_urls(candidate_records)
    if not candidate_urls:
        return False
    for record in candidate_records:
        items = record.get("items") if isinstance(record, dict) else None
        if not isinstance(items, list):
            continue
        if any(
            isinstance(item, dict)
            and len(item.get("links") or []) == 1
            and _cleaned_observation_chars(item.get("text")) >= MIN_SUBSTANTIAL_TEXT_CHARS
            for item in items
        ):
            return False
    roles_by_url: dict[str, dict[str, int]] = {}
    evidence = ledger.get("detail_field_evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if not isinstance(item, dict):
                continue
            url = _canonical_observed_http_url(item.get("url"))
            role = item.get("role")
            chars = item.get("cleaned_text_chars")
            if (
                url not in candidate_urls
                or role not in {"content", "published_at"}
                or isinstance(chars, bool)
                or not isinstance(chars, int)
            ):
                continue
            roles_by_url.setdefault(url, {})[role] = max(
                chars, roles_by_url.get(url, {}).get(role, 0)
            )
    return not any(
        roles.get("content", 0) >= MIN_SUBSTANTIAL_TEXT_CHARS
        and roles.get("published_at", 0) > 0
        for roles in roles_by_url.values()
    )


def _explore_session_ledger(
    observations: list[dict[str, Any]],
    *,
    site_url: str,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive the Explore handoff only from host-numbered broker records."""
    entry_url = _canonical_entry_observation_url(site_url)
    documents = [
        dict(document)
        for document in (base or {}).get("documents", [])
        if isinstance(document, dict)
    ][-MAX_EXPLORE_LEDGER_DOCUMENTS:]
    candidate_records = [
        dict(candidate)
        for candidate in (base or {}).get("candidate_records", [])
        if isinstance(candidate, dict)
    ][-MAX_EXPLORE_LEDGER_CANDIDATE_RECORDS:]
    detail_field_evidence = [
        dict(item)
        for item in (base or {}).get("detail_field_evidence", [])
        if isinstance(item, dict)
    ][-MAX_EXPLORE_LEDGER_DETAIL_FIELD_EVIDENCE:]
    observed_hosts = [
        host.lower().rstrip(".")
        for host in (base or {}).get("observed_hosts", [])
        if isinstance(host, str) and host
    ][:32]
    # Rebuild only a contiguous redirect chain rooted at the entry.  A stored
    # ledger must not turn an unrelated observed host into an entry alias.
    entry_redirects: list[dict[str, Any]] = []
    entry_redirect_targets: set[str] = set()
    for candidate in (base or {}).get("entry_redirects", []):
        if not isinstance(candidate, dict):
            continue
        source_url = _canonical_observed_http_url(candidate.get("from_url"))
        target_url = _canonical_observed_http_url(candidate.get("to_url"))
        if (
            source_url is None
            or target_url is None
            or (source_url != entry_url and source_url not in entry_redirect_targets)
        ):
            continue
        entry_redirects.append({
            "record_id": candidate.get("record_id"),
            "sequence": candidate.get("sequence"),
            "from_url": source_url,
            "to_url": target_url,
            "status": candidate.get("status"),
        })
        entry_redirect_targets.add(target_url)
    entry_redirects = entry_redirects[-MAX_EXPLORE_LEDGER_ENTRY_REDIRECTS:]
    entry_observed = bool((base or {}).get("entry_observed"))
    successful_operations = _nonnegative_int((base or {}).get("successful_operations"))
    browser_success = bool((base or {}).get("browser_success"))
    completed_batches = _nonnegative_int((base or {}).get("completed_batches"))
    meaningful_document_batches = _nonnegative_int(
        (base or {}).get("meaningful_document_batches")
    )

    for observation in observations:
        if _observation_has_meaningful_explore_document(observation):
            meaningful_document_batches += 1
        result = observation.get("result") if isinstance(observation, dict) else None
        records = result.get("records") if isinstance(result, dict) else None
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            tool = record.get("tool")
            action = record.get("operation")
            document = record.get("observation")
            if not isinstance(document, dict):
                continue
            if tool == "browser" and action == "records" and record.get("error") is None:
                elements = document.get("records")
                if isinstance(elements, list):
                    candidate_records.append({
                        "record_id": record.get("record_id"),
                        "sequence": record.get("sequence"),
                        "page_id": document.get("page_id"),
                        "selector": document.get("record_selector"),
                        "items": _representative_candidate_records(elements),
                    })
                    if len(candidate_records) > MAX_EXPLORE_LEDGER_CANDIDATE_RECORDS:
                        candidate_records.pop(0)
            if tool == "browser" and action in {"text", "attribute"} and record.get("error") is None:
                role = document.get("evidence_role")
                page_id = document.get("page_id")
                source_selector = document.get("source_selector")
                cleaned_chars = document.get("cleaned_text_chars")
                detail_document = next(
                    (
                        item for item in reversed(documents)
                        if isinstance(item, dict) and item.get("page_id") == page_id
                    ),
                    None,
                )
                candidate_urls = _candidate_detail_urls(candidate_records)
                requested_detail_url = _canonical_observed_http_url(
                    detail_document.get("requested_url") if detail_document else None
                )
                final_detail_url = _canonical_observed_http_url(
                    detail_document.get("final_url") if detail_document else None
                )
                detail_url = (
                    requested_detail_url
                    if requested_detail_url in candidate_urls
                    else final_detail_url if final_detail_url in candidate_urls else None
                )
                if (
                    role in {"content", "published_at"}
                    and isinstance(source_selector, str)
                    and source_selector
                    and isinstance(cleaned_chars, int)
                    and not isinstance(cleaned_chars, bool)
                    and detail_url is not None
                ):
                    detail_field_evidence = [
                        item for item in detail_field_evidence
                        if not (
                            item.get("url") == detail_url and item.get("role") == role
                        )
                    ]
                    detail_field_evidence.append({
                        "record_id": record.get("record_id"),
                        "page_id": page_id,
                        "url": detail_url,
                        "final_url": final_detail_url,
                        "role": role,
                        "source": {
                            "tool": "browser",
                            "operation": action,
                            "selector": source_selector[:1_024],
                            "attribute": str(document.get("source_attribute") or "")[:128] or None,
                        },
                        "cleaned_text_chars": cleaned_chars,
                        "text_sample": str(document.get("text") or "")[:500],
                    })
                    detail_field_evidence = detail_field_evidence[
                        -MAX_EXPLORE_LEDGER_DETAIL_FIELD_EVIDENCE:
                    ]
            requested_url = document.get("requested_url")
            final_url = document.get("final_url")
            redirect_target = document.get("redirect_target")
            status = document.get("status")
            succeeded = (
                record.get("error") is None
                and isinstance(status, int)
                and 200 <= status < 400
            )
            for url in (requested_url, final_url, document.get("redirect_target")):
                host = (urlsplit(url).hostname or "").lower().rstrip(".") if isinstance(url, str) else ""
                if host and host not in observed_hosts:
                    observed_hosts.append(host)
            for link in document.get("links", []):
                host = (urlsplit(link).hostname or "").lower().rstrip(".") if isinstance(link, str) else ""
                if host and host not in observed_hosts:
                    observed_hosts.append(host)
            for network_event in document.get("network", []):
                url = network_event.get("url") if isinstance(network_event, dict) else None
                host = (urlsplit(url).hostname or "").lower().rstrip(".") if isinstance(url, str) else ""
                if host and host not in observed_hosts:
                    observed_hosts.append(host)
            if not succeeded:
                continue
            successful_operations += 1
            if tool == "browser":
                browser_success = True
            requested_observed_url = _canonical_observed_http_url(requested_url)
            final_observed_url = _canonical_observed_http_url(final_url)
            redirect_observed_url = _canonical_observed_http_url(redirect_target)
            is_entry_request = requested_observed_url == entry_url
            follows_entry_redirect = requested_observed_url in entry_redirect_targets
            if (
                isinstance(status, int)
                and 300 <= status < 400
                and (is_entry_request or follows_entry_redirect)
                and redirect_observed_url is not None
            ):
                entry_redirect_targets.add(redirect_observed_url)
                entry_redirects.append({
                    "record_id": record.get("record_id"),
                    "sequence": record.get("sequence"),
                    "from_url": requested_observed_url,
                    "to_url": redirect_observed_url,
                    "status": status,
                })
                if len(entry_redirects) > MAX_EXPLORE_LEDGER_ENTRY_REDIRECTS:
                    entry_redirects.pop(0)
            has_document = _is_meaningful_explore_document_record(record)
            if (
                has_document
                and (is_entry_request or follows_entry_redirect)
                # A redirected document is accepted only if its request was
                # reached through a previously broker-recorded redirect.  The
                # final URL is still retained as evidence, but is not used to
                # treat an arbitrary linked host as the entry document.
                and final_observed_url is not None
            ):
                entry_observed = True
            if not has_document:
                continue
            document_summary = {
                "record_id": record.get("record_id"),
                "sequence": record.get("sequence"),
                "tool": tool,
                "operation": action,
                "page_id": document.get("page_id"),
                "requested_url": requested_url,
                "final_url": final_url,
                "redirect_target": redirect_target,
                "status": status,
                "content_type": document.get("content_type"),
                "title": document.get("title"),
                "text_excerpt": str(document.get("text_excerpt") or document.get("text") or "")[:4_000],
                "links": list(document.get("links") or [])[:20],
                "body_size": document.get("body_size"),
                "body_sha256": document.get("body_sha256"),
                "body_truncated": document.get("body_truncated") is True,
            }
            documents.append(document_summary)
            if len(documents) > MAX_EXPLORE_LEDGER_DOCUMENTS:
                documents.pop(0)

    return {
        "version": 2,
        "entry_observed": entry_observed,
        "successful_operations": successful_operations,
        "browser_success": browser_success,
        "meaningful_document_batches": meaningful_document_batches,
        "observed_hosts": observed_hosts[:32],
        "entry_redirects": entry_redirects,
        "documents": documents,
        "candidate_records": candidate_records,
        "detail_field_evidence": detail_field_evidence,
        "completed_batches": completed_batches + sum(
            1 for item in observations if item.get("action") == "inspect"
        ),
    }


def _representative_candidate_records(elements: list[Any]) -> list[dict[str, Any]]:
    """Keep compact, distinct record examples rather than document-order navigation."""
    candidates: list[dict[str, Any]] = []
    seen_links: set[tuple[str, ...]] = set()
    for raw_item in elements:
        if not isinstance(raw_item, dict):
            continue
        text = str(raw_item.get("text") or "").strip()[:1_000]
        links = [
            str(link)[:256]
            for link in raw_item.get("links", [])[:4]
            if isinstance(link, str) and link
        ] if isinstance(raw_item.get("links"), list) else []
        link_key = tuple(dict.fromkeys(links))
        if not text or not link_key or link_key in seen_links:
            continue
        seen_links.add(link_key)
        candidates.append({
            "text": text,
            "links": list(link_key),
            "html": str(raw_item.get("html") or "")[:2_000],
        })
    candidates.sort(key=lambda item: (len(item["links"]) != 1, -len(item["text"])))
    return candidates[:6]


def _bounded_explore_checkpoint_observations(
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep checkpoints resumable without allowing Explore evidence to exceed storage bounds."""
    retained = list(observations[-MAX_EXPLORE_CHECKPOINT_OBSERVATIONS:])
    while (
        len(retained) > 1
        and len(json.dumps(retained, ensure_ascii=False, default=str).encode("utf-8"))
        > MAX_EXPLORE_CHECKPOINT_BYTES
    ):
        retained.pop(0)
    return retained


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _canonical_entry_observation_url(value: str) -> str:
    """Compare entry observations without treating a trailing slash as a new target."""
    parsed = urlsplit(value.split("#", 1)[0])
    path = parsed.path or "/"
    return parsed._replace(path=path, fragment="").geturl()


def _canonical_observed_http_url(value: Any) -> str | None:
    """Return a comparable public URL only when a broker record exposed it."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.split("#", 1)[0])
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    path = parsed.path or "/"
    return parsed._replace(path=path, fragment="").geturl()


def _is_transport_failure(exc: BaseException) -> bool:
    """Classify only runner-attested connectivity failures as non-repairable."""
    return isinstance(exc, ConnectorProtocolError) and exc.code == ConnectorErrorCode.NETWORK_ERROR


def _select_transport_route(exploration: dict[str, Any]) -> str:
    """Choose only between routes recorded by the fixed Explore session tool."""
    session = exploration.get("explore_session") if isinstance(exploration, dict) else None
    evidence = session.get("evidence") if isinstance(session, dict) else None
    if isinstance(evidence, dict) and evidence.get("browser_success") is True:
        return "browser"
    return "http"
