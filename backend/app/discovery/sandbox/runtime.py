"""Docker + runsc orchestration for deterministic connector execution."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, RLock
from typing import Any, Callable, Iterator, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings, get_settings
from app.discovery.plugin.artifact import (
    CONNECTOR_FILENAME,
    MANIFEST_FILENAME,
    MAX_CONNECTOR_BYTES,
    MAX_MANIFEST_BYTES,
    ConnectorArtifact,
    load_connector_artifact,
)
from app.discovery.plugin.contracts import (
    MAX_CONNECTOR_TEXT_CHARS,
    ConnectorInvocation,
    ConnectorOutput,
)
from app.discovery.plugin.errors import (
    ConnectorErrorCode,
    ConnectorExitCode,
    ConnectorProtocolError,
)
from app.discovery.redaction import DiscoveryDataBoundsError, redact_discovery_data
from app.discovery.sandbox.capacity import (
    CapacityQueue,
    SandboxJobPriority,
    get_sandbox_capacity_queue,
)
from app.discovery.sandbox.attestation_keys import (
    sign_host_attestation,
    verify_host_attestation,
)


_DIGEST_IMAGE = re.compile(r"^[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}$")
_SAFE_JOB_ID = re.compile(r"[^a-zA-Z0-9_.-]+")
MAX_HOST_STDOUT_BYTES = 4 * 1024 * 1024
MAX_HOST_STDERR_BYTES = 2 * 1024 * 1024
MAX_HOST_EVENTS = 50
MAX_EXPLORE_SESSION_STDERR_BYTES = 128 * 1024
MAX_EXPLORE_SESSION_RESPONSE_BYTES = 512 * 1024
MAX_EXPLORE_SESSION_ACTIONS = 6
MAX_EXPLORE_SESSION_DURATION_SECONDS = 1_800.0
HOST_PIPE_DRAIN_TIMEOUT_SECONDS = 10.0
logger = logging.getLogger(__name__)
_sandbox_cleanup_degraded = Event()


def canonical_connector_document(value: Any) -> bytes:
    document = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_connector_digest(value: Any) -> str:
    return hashlib.sha256(canonical_connector_document(value)).hexdigest()


def connector_audit_projection(value: BaseModel) -> dict[str, Any]:
    """Return the single host-owned, bounded, redacted representation used by audits."""
    document = value.model_dump(mode="json")
    projected = redact_discovery_data(
        document,
        max_string_chars=MAX_HOST_STDOUT_BYTES,
        max_approx_bytes=MAX_HOST_STDOUT_BYTES,
    )
    if not isinstance(projected, dict):
        raise ValueError("connector audit projection must be an object")
    if isinstance(value, ConnectorOutput):
        items = projected.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                for field in ("summary", "content"):
                    field_value = item.get(field)
                    if isinstance(field_value, str):
                        # Redaction may expand a short secret into "[redacted]".
                        # Keep the persisted audit copy inside the same connector
                        # contract as the original output without changing the
                        # output handed to the ingestion pipeline.
                        item[field] = field_value[:MAX_CONNECTOR_TEXT_CHARS]
    return projected


def set_sandbox_cleanup_degraded(degraded: bool) -> None:
    """Fail-closed process gate controlled by startup orphan cleanup."""
    if degraded:
        _sandbox_cleanup_degraded.set()
    else:
        _sandbox_cleanup_degraded.clear()


def is_sandbox_cleanup_degraded() -> bool:
    return _sandbox_cleanup_degraded.is_set()


@dataclass(frozen=True)
class SandboxExecution:
    """All host-owned inputs for one connector container."""

    job_id: str
    artifact: ConnectorArtifact
    invocation: ConnectorInvocation
    kind: str
    priority: SandboxJobPriority
    expected_checksum: str
    expected_signature: str
    timeout_seconds: float | None = None
    purpose: str = "other"
    trial_index: int | None = None


@dataclass(frozen=True)
class SandboxExploreSessionSpec:
    """Host-owned configuration for one retained Explore gVisor session.

    The Engine creates this once per Discovery run.  It contains policy inputs,
    never Agent code or a connector artifact.  ``allowed_domains`` is an exact
    public-domain allowlist and is copied into a host-controlled policy file.
    """

    job_id: str
    runtime_version: str
    allowed_domains: tuple[str, ...]
    priority: SandboxJobPriority
    entry_url: str
    timeout_seconds: float | None = None
    purpose: Literal["explore_tool", "explore_session"] = "explore_tool"


@dataclass(frozen=True)
class SandboxExploreSessionRecord:
    """One host-numbered action record returned by the fixed Explore broker."""

    sequence: int
    record_id: str
    tool: str
    operation: str
    observation: dict[str, Any]
    error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "record_id": self.record_id,
            "tool": self.tool,
            "operation": self.operation,
            "observation": self.observation,
            "error": self.error,
        }


class SandboxRuntimeAttestation(BaseModel):
    """Host-signed proof created only after live runsc probing and execution."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    job_id: str = Field(min_length=1, max_length=200)
    connector_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    connector_version: int = Field(strict=True, ge=1)
    artifact_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_version: str = Field(min_length=1, max_length=200)
    runtime_binary: str = Field(min_length=1, max_length=1000)
    runsc_version: str = Field(min_length=1, max_length=200)
    platform: Literal["systrap"]
    image_digest: str = Field(pattern=r"^[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}$")
    probe_nonce: str = Field(pattern=r"^[0-9a-f]{48}$")
    started_nonce: str = Field(pattern=r"^[0-9a-f]{48}$")
    completed_nonce: str = Field(pattern=r"^[0-9a-f]{48}$")
    completed_at: str = Field(min_length=20, max_length=60)
    purpose: Literal["primary_trial", "pagination", "url_verifier", "explore_tool", "explore_session", "field_smoke", "formal", "other"]
    trial_index: int | None = Field(default=None, strict=True, ge=1, le=2)
    invocation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    key_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    host_proof: str = Field(pattern=r"^[0-9a-f]{64}$")

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def validate_runtime_attestation(
    raw: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> SandboxRuntimeAttestation:
    """Validate schema and host HMAC without trusting persisted evidence."""
    settings = settings or get_settings()
    if not isinstance(raw, dict):
        raise ValueError("sandbox runtime attestation must be an object")
    field_names = set(SandboxRuntimeAttestation.model_fields)
    if set(raw) != field_names:
        raise ValueError("sandbox runtime attestation schema mismatch")
    try:
        attestation = SandboxRuntimeAttestation.model_validate(raw, strict=True)
    except ValidationError as exc:
        raise ValueError("sandbox runtime attestation is invalid") from exc
    try:
        completed = datetime.fromisoformat(attestation.completed_at)
    except ValueError as exc:
        raise ValueError("sandbox runtime attestation completion time is invalid") from exc
    if completed.tzinfo is None or completed.utcoffset() != timezone.utc.utcoffset(completed):
        raise ValueError("sandbox runtime attestation completion time must be UTC")
    unsigned = {
        key: value
        for key, value in attestation.as_dict().items()
        if key not in {"key_id", "host_proof"}
    }
    verify_host_attestation(
        unsigned,
        key_id=attestation.key_id,
        proof=attestation.host_proof,
        settings=settings,
    )
    return attestation


def _create_runtime_attestation(
    *,
    execution: SandboxExecution,
    settings: Settings,
    image: str,
    probe_nonce: str,
    started_nonce: str,
    invocation_sha256: str,
    output_sha256: str,
) -> SandboxRuntimeAttestation:
    document: dict[str, Any] = {
        "schema_version": 1,
        "job_id": execution.job_id,
        "connector_key": execution.artifact.manifest.connector_key,
        "connector_version": execution.artifact.manifest.version,
        "artifact_checksum": execution.artifact.manifest.checksum,
        "artifact_signature": execution.artifact.signature,
        "runtime_version": execution.artifact.manifest.runtime_version,
        "runtime_binary": str(Path(settings.discovery_sandbox_runsc_binary).resolve()),
        "runsc_version": settings.discovery_sandbox_runsc_version,
        "platform": settings.discovery_sandbox_platform,
        "image_digest": image,
        "probe_nonce": probe_nonce,
        "started_nonce": started_nonce,
        "completed_nonce": secrets.token_hex(24),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "purpose": execution.purpose,
        "trial_index": execution.trial_index,
        "invocation_sha256": invocation_sha256,
        "output_sha256": output_sha256,
    }
    key_id, proof = sign_host_attestation(document, settings=settings)
    document["key_id"] = key_id
    document["host_proof"] = proof
    return SandboxRuntimeAttestation.model_validate(document, strict=True)


@dataclass(frozen=True)
class SandboxExecutionResult:
    """Validated connector output plus auditable structured stderr events."""

    output: ConnectorOutput
    audit_invocation: ConnectorInvocation
    audit_output: ConnectorOutput
    events: tuple[dict[str, Any], ...]
    exit_code: ConnectorExitCode
    elapsed_seconds: float
    attestation: SandboxRuntimeAttestation


@dataclass
class _ActiveExecution:
    cancel_event: Event
    container_name: str | None = None
    process: subprocess.Popen[bytes] | None = None
    cleanup_safe: bool = True
    lock: Lock = field(default_factory=Lock)


class SandboxExploreSession:
    """One retained gVisor broker process with host-owned lifetime and trace.

    The session's stdin is a controlled JSON protocol consumed by a static
    broker program.  Agent data is only a validated declarative action; the
    host assigns ordering and opaque record ids after checking the response.
    """

    def __init__(
        self,
        *,
        runtime: "SandboxRuntime",
        spec: SandboxExploreSessionSpec,
        active: _ActiveExecution,
        lease: Any,
        started_at: float,
        deadline: float,
        image: str,
        network_name: str,
        proxy_name: str,
        container_name: str,
        runtime_volume_name: str,
        resource_root: Path | None,
        process: subprocess.Popen[bytes],
    ) -> None:
        self._runtime = runtime
        self.spec = spec
        self._active = active
        self._lease = lease
        self._started_at = started_at
        self._deadline = deadline
        self.image = image
        self.network_name = network_name
        self.proxy_name = proxy_name
        self.container_name = container_name
        self._resource_root = resource_root
        self.runtime_volume_name = runtime_volume_name
        self._process = process
        self._lock = RLock()
        self._closed = False
        self._sequence = 0
        self._records: list[SandboxExploreSessionRecord] = []
        self._responses: Queue[dict[str, Any]] = Queue()
        self._stderr = bytearray()
        self._reader_error: BaseException | None = None
        self._reader_lock = Lock()
        self._allowed_domains = tuple(spec.allowed_domains)
        self._observed_hosts: set[str] = set()
        self._start_readers()

    @property
    def job_id(self) -> str:
        return self.spec.job_id

    @property
    def allowed_domains(self) -> tuple[str, ...]:
        return self._allowed_domains

    @property
    def records(self) -> tuple[SandboxExploreSessionRecord, ...]:
        return tuple(self._records)

    def execute(self, actions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Run up to six host-validated actions without creating another sandbox."""
        if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
            raise ValueError("Explore session actions must be a sequence")
        if not 1 <= len(actions) <= MAX_EXPLORE_SESSION_ACTIONS:
            raise ValueError(
                f"Explore session accepts 1 to {MAX_EXPLORE_SESSION_ACTIONS} actions per request"
            )
        records: list[dict[str, Any]] = []
        with self._lock:
            self._ensure_open()
            for action in actions:
                if not isinstance(action, Mapping):
                    raise ValueError("Explore session action must be an object")
                records.append(self._execute_one(dict(action)).as_dict())
        return {"records": records, "session_elapsed_seconds": self.elapsed_seconds}

    def extend_allowed_domains(self, domains: Collection[str]) -> tuple[str, ...]:
        """Permit only exact hosts already exposed by a fixed broker observation.

        This is intentionally a host API, not a JSON action.  The egress proxy
        and broker read the same mounted policy document for each new request,
        so the retained runsc container keeps its browser/workspace state.
        """
        if isinstance(domains, (str, bytes)):
            raise ValueError("Explore domain extension must be a collection")
        with self._lock:
            self._ensure_open()
            requested = tuple(_normalize_explore_domain(value) for value in domains)
            missing = [domain for domain in requested if domain not in self._observed_hosts]
            if missing:
                raise ValueError("Explore may extend domains only from fixed broker observations")
            combined = tuple(dict.fromkeys((*self._allowed_domains, *requested)))
            if len(combined) > 32:
                raise ValueError("Explore session allowlist exceeds 32 domains")
            self._runtime._update_explore_allowlist(self.proxy_name, combined)
            self._allowed_domains = combined
            return combined

    def cancel(self) -> None:
        """Cancel this retained session and release its capacity after cleanup."""
        self._runtime.cancel(self.spec.job_id)
        self.close()

    def close(self) -> None:
        """Stop the broker and atomically release the single retained lease."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._runtime._close_explore_session(self)

    def __enter__(self) -> "SandboxExploreSession":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started_at)

    def _start_readers(self) -> None:
        if self._process.stdout is None or self._process.stderr is None:
            raise self._runtime._runtime_error("Explore broker output pipes were not created")

        def read_stdout() -> None:
            try:
                while line := self._process.stdout.readline(MAX_EXPLORE_SESSION_RESPONSE_BYTES + 1):
                    if len(line) > MAX_EXPLORE_SESSION_RESPONSE_BYTES:
                        raise ValueError("Explore broker response exceeded the host line limit")
                    try:
                        payload = json.loads(line)
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        raise ValueError("Explore broker emitted invalid JSON") from exc
                    if not isinstance(payload, dict):
                        raise ValueError("Explore broker emitted a non-object response")
                    self._responses.put(payload)
            except BaseException as exc:  # explicit error is safer than stale partial observations
                with self._reader_lock:
                    self._reader_error = exc

        def read_stderr() -> None:
            try:
                while line := self._process.stderr.readline(16 * 1024):
                    remaining = MAX_EXPLORE_SESSION_STDERR_BYTES - len(self._stderr)
                    if remaining > 0:
                        self._stderr.extend(line[:remaining])
            except BaseException as exc:
                with self._reader_lock:
                    self._reader_error = self._reader_error or exc

        threading.Thread(target=read_stdout, name="explore-broker-stdout", daemon=True).start()
        threading.Thread(target=read_stderr, name="explore-broker-stderr", daemon=True).start()

    def _wait_ready(self) -> None:
        response = self._next_response(timeout_seconds=min(15.0, self._remaining_seconds()))
        if response.get("type") != "ready" or response.get("protocol") != 1:
            raise self._runtime._runtime_error("Explore broker did not confirm the fixed protocol")

    def _execute_one(self, action: dict[str, Any]) -> SandboxExploreSessionRecord:
        self._ensure_open()
        self._sequence += 1
        request_id = secrets.token_hex(16)
        payload = {
            "type": "action",
            "protocol": 1,
            "request_id": request_id,
            "action": action,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_EXPLORE_SESSION_RESPONSE_BYTES:
            raise ValueError("Explore action exceeds the host protocol limit")
        if self._process.stdin is None:
            raise self._runtime._runtime_error("Explore broker stdin is unavailable")
        try:
            self._process.stdin.write(encoded + b"\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise self._broker_runtime_error("Explore broker stopped before accepting an action") from exc
        response = self._next_response(timeout_seconds=min(35.0, self._remaining_seconds()))
        if response.get("type") != "action_result" or response.get("request_id") != request_id:
            self.close()
            raise self._runtime._runtime_error("Explore broker response does not bind to the host request")
        tool = action.get("tool")
        operation = action.get("operation") or action.get("method")
        if response.get("tool") != tool or response.get("operation") != operation:
            self.close()
            raise self._runtime._runtime_error("Explore broker response does not match the requested action")
        observation = response.get("observation")
        if not isinstance(observation, dict):
            observation = {}
        # This is the host-validated card boundary that produced a records
        # observation, not an Agent-authored extraction claim.
        if tool == "browser" and operation == "records" and isinstance(action.get("selector"), str):
            observation = {**observation, "record_selector": action["selector"]}
        error = response.get("error")
        if not isinstance(error, str):
            error = None
        record = SandboxExploreSessionRecord(
            sequence=self._sequence,
            record_id=f"record_{secrets.token_hex(12)}",
            tool=str(tool or "")[:32],
            operation=str(operation or "")[:32],
            observation=_bound_explore_observation(observation),
            error=error[:2_000] if error else None,
        )
        self._records.append(record)
        self._record_observed_hosts(record.observation)
        return record

    def _record_observed_hosts(self, observation: Mapping[str, Any]) -> None:
        candidates: list[Any] = [
            observation.get("final_url"),
            observation.get("requested_url"),
            observation.get("redirect_target"),
        ]
        candidates.extend(observation.get("links") if isinstance(observation.get("links"), list) else [])
        network = observation.get("network")
        if isinstance(network, list):
            candidates.extend(item.get("url") for item in network if isinstance(item, Mapping))
        for value in candidates:
            if not isinstance(value, str):
                continue
            try:
                parsed = urlsplit(value)
                if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
                    self._observed_hosts.add(_normalize_explore_domain(parsed.hostname))
            except (ValueError, UnicodeError):
                continue

    def _next_response(self, *, timeout_seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = min(deadline - time.monotonic(), self._remaining_seconds())
            if remaining <= 0:
                self.close()
                raise ConnectorProtocolError(
                    ConnectorErrorCode.TIMEOUT,
                    "Explore broker did not return a bounded action response",
                )
            try:
                return self._responses.get(timeout=min(0.25, remaining))
            except Empty:
                # Re-check cancellation and the broker process several times a
                # second instead of making cancellation wait for an action's
                # full response timeout.
                continue

    def _remaining_seconds(self) -> float:
        if self._active.cancel_event.is_set():
            raise ConnectorProtocolError(ConnectorErrorCode.CANCELLED, "Explore session was cancelled")
        with self._reader_lock:
            reader_error = self._reader_error
        if reader_error is not None:
            raise self._broker_runtime_error("Explore broker output stream failed")
        if self._process.poll() is not None:
            raise self._broker_runtime_error("Explore broker process exited unexpectedly")
        return self._deadline - time.monotonic()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConnectorProtocolError(ConnectorErrorCode.CANCELLED, "Explore session is closed")
        self._remaining_seconds()

    def _broker_runtime_error(self, message: str) -> ConnectorProtocolError:
        return self._runtime._runtime_error(
            message,
            stderr=self._runtime._safe_text(bytes(self._stderr)),
            container=self.container_name,
        )


class SandboxRuntime:
    """Fail closed unless Docker exposes the configured gVisor runsc runtime."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        capacity_queue: CapacityQueue | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.capacity_queue = capacity_queue or get_sandbox_capacity_queue()
        self._active: dict[str, _ActiveExecution] = {}
        self._explore_sessions: dict[str, SandboxExploreSession] = {}
        self._active_lock = Lock()
        self._retained_capacity: set[str] = set()

    def open_explore_session(
        self,
        spec: SandboxExploreSessionSpec,
        *,
        on_started: Callable[[], None] | None = None,
    ) -> SandboxExploreSession:
        """Open one retained, fixed-broker gVisor container for an Explore run.

        Queue waiting is outside the returned session's wall-clock budget.  Once
        acquired, exactly one runsc container stays alive until ``close`` or
        ``cancel``.  The separately trusted egress proxy is ordinary isolated
        infrastructure and never runs Agent-supplied code.
        """
        if is_sandbox_cleanup_degraded():
            raise self._runtime_error(
                "sandbox startup cleanup is degraded; new Explore sessions are fail-closed"
            )
        if not spec.job_id or _SAFE_JOB_ID.sub("", spec.job_id) != spec.job_id:
            raise ValueError("Explore session job id is unsafe")
        if not spec.runtime_version:
            raise ValueError("Explore session runtime version is required")
        active = _ActiveExecution(cancel_event=Event())
        with self._active_lock:
            if spec.job_id in self._active:
                raise ValueError(f"duplicate sandbox execution id: {spec.job_id}")
            self._active[spec.job_id] = active

        lease = None
        resource_root: Path | None = None
        network_name = proxy_name = container_name = runtime_volume_name = ""
        try:
            normalized_domains = _initial_explore_allowed_domains(
                spec.entry_url,
                spec.allowed_domains,
            )
            if len(normalized_domains) > 32:
                raise ValueError("Explore session allowlist exceeds 32 domains")
            spec = replace(spec, allowed_domains=normalized_domains)
            lease = self.capacity_queue.acquire(spec.job_id, spec.priority, active.cancel_event)
            started_at = time.monotonic()
            if on_started is not None:
                on_started()
            timeout_seconds = min(
                MAX_EXPLORE_SESSION_DURATION_SECONDS,
                max(0.1, spec.timeout_seconds or MAX_EXPLORE_SESSION_DURATION_SECONDS),
            )
            deadline = started_at + timeout_seconds
            self._assert_runtime_available(active, deadline=deadline)
            self._raise_if_cancelled(active, "after Explore runtime validation")
            image = self._resolve_immutable_image(spec.runtime_version, deadline=deadline)
            self._raise_if_cancelled(active, "after Explore Runtime image validation")
            suffix = _SAFE_JOB_ID.sub("-", spec.job_id).strip("-.")[:32] or "explore"
            nonce = uuid.uuid4().hex[:10]
            network_name = f"osnews-explore-net-{suffix}-{nonce}"
            proxy_name = f"osnews-explore-proxy-{suffix}-{nonce}"
            container_name = f"osnews-explore-broker-{suffix}-{nonce}"
            runtime_volume_name = f"osnews-explore-runtime-{suffix}-{nonce}"
            resource_root = Path(tempfile.mkdtemp(prefix="osnews-explore-broker-"))
            # The runsc payload is UID 65532, so fixed source/policy files must
            # be traversable and readable but never writable from the container.
            resource_root.chmod(0o755)
            allowlist_path = resource_root / "allowed-domains.json"
            self._write_explore_allowlist(allowlist_path, spec.allowed_domains)
            self._write_explore_runtime_sources(resource_root)
            self._docker(
                "volume", "create",
                "--label", "osnews.discovery.sandbox=true",
                "--label", f"osnews.discovery.job={spec.job_id}",
                runtime_volume_name,
                timeout=self._step_timeout(deadline, 10.0),
            )
            self._populate_explore_runtime_volume(
                volume_name=runtime_volume_name,
                resource_root=resource_root,
                image=image,
                job_id=spec.job_id,
                deadline=deadline,
            )
            if not _remove_explore_resource_root(resource_root):
                raise self._runtime_error("host Explore broker source cleanup failed after volume population")
            resource_root = None
            self._docker(
                "network", "create", "--internal",
                "--label", "osnews.discovery.sandbox=true",
                "--label", f"osnews.discovery.job={spec.job_id}",
                network_name,
                timeout=self._step_timeout(deadline, 15.0),
            )
            self._raise_if_cancelled(active, "after Explore network creation")
            self._start_explore_proxy(
                proxy_name=proxy_name,
                network_name=network_name,
                image=image,
                runtime_volume_name=runtime_volume_name,
                job_id=spec.job_id,
                active=active,
                deadline=deadline,
            )
            proxy_address = self._explore_proxy_address(
                proxy_name=proxy_name,
                network_name=network_name,
                active=active,
                deadline=deadline,
            )
            command = self._explore_broker_command(
                container_name=container_name,
                network_name=network_name,
                proxy_address=proxy_address,
                runtime_volume_name=runtime_volume_name,
                image=image,
                job_id=spec.job_id,
            )
            self._docker(*command[1:], timeout=self._step_timeout(deadline, 20.0))
            with active.lock:
                active.container_name = container_name
            process = subprocess.Popen(
                [self.settings.discovery_sandbox_docker_command, "start", "--attach", "--interactive", container_name],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._docker_environment(),
            )
            with active.lock:
                active.process = process
            session = SandboxExploreSession(
                runtime=self,
                spec=spec,
                active=active,
                lease=lease,
                started_at=started_at,
                deadline=deadline,
                image=image,
                network_name=network_name,
                proxy_name=proxy_name,
                container_name=container_name,
                runtime_volume_name=runtime_volume_name,
                resource_root=resource_root,
                process=process,
            )
            session._wait_ready()
            self._assert_live_gvisor_container(container_name, active=active, deadline=deadline)
            with self._active_lock:
                self._explore_sessions[spec.job_id] = session
            return session
        except BaseException:
            self._cleanup_failed_explore_open(
                active=active,
                lease=lease,
                job_id=spec.job_id,
                container_name=container_name,
                proxy_name=proxy_name,
                network_name=network_name,
                runtime_volume_name=runtime_volume_name,
                resource_root=resource_root,
            )
            raise

    def execute(
        self,
        execution: SandboxExecution,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_started: Callable[[], None] | None = None,
        cancel_event: Event | None = None,
    ) -> SandboxExecutionResult:
        """Queue, execute, validate and always remove all per-job Docker resources."""
        if (
            execution.purpose == "primary_trial"
            and execution.trial_index not in {1, 2}
        ) or (
            execution.purpose != "primary_trial"
            and execution.trial_index is not None
        ):
            raise ValueError("sandbox primary trial purpose/index binding is invalid")
        if is_sandbox_cleanup_degraded():
            raise self._runtime_error(
                "sandbox startup cleanup is degraded; new sandbox execution is fail-closed"
            )
        active = _ActiveExecution(cancel_event=cancel_event or Event())
        with self._active_lock:
            if execution.job_id in self._active:
                raise ValueError(f"duplicate sandbox execution id: {execution.job_id}")
            self._active[execution.job_id] = active
        lease = None
        started_at: float | None = None
        try:
            lease = self.capacity_queue.acquire(
                execution.job_id,
                execution.priority,
                active.cancel_event,
            )
            started_at = time.monotonic()
            if on_started is not None:
                on_started()
            timeout_seconds = (
                execution.timeout_seconds or self.settings.discovery_sandbox_timeout_seconds
            )
            deadline = started_at + timeout_seconds
            self._raise_if_cancelled(active, "before runtime validation")
            self._assert_runtime_available(active, deadline=deadline)
            self._raise_if_cancelled(active, "after runtime validation")
            image = self._resolve_immutable_image(
                execution.artifact.manifest.runtime_version,
                deadline=deadline,
            )
            self._raise_if_cancelled(active, "after Runtime image validation")
            started_nonce = secrets.token_hex(24)
            probe_nonce = self._probe_live_gvisor(
                image,
                active=active,
                deadline=deadline,
                job_id=execution.job_id,
            )
            if not isinstance(probe_nonce, str) or len(probe_nonce) < 32:
                raise self._runtime_error("live gVisor probe did not return host attestation evidence")
            with self._immutable_artifact_snapshot(execution, deadline=deadline) as snapshot:
                return self._execute_container(
                    replace(execution, artifact=snapshot),
                    active,
                    image=image,
                    started_at=started_at,
                    deadline=deadline,
                    timeout_seconds=timeout_seconds,
                    on_event=on_event,
                    probe_nonce=probe_nonce,
                    started_nonce=started_nonce,
                )
        except ConnectorProtocolError as exc:
            if started_at is not None:
                exc.details.setdefault(
                    "execution_elapsed_seconds",
                    max(0.0, time.monotonic() - started_at),
                )
            raise
        finally:
            if lease is not None:
                if active.cleanup_safe:
                    lease.release()
                else:
                    with self._active_lock:
                        self._retained_capacity.add(execution.job_id)
                    logger.error(
                        "sandbox capacity retained because container cleanup was not confirmed job_id=%s",
                        execution.job_id,
                    )
            with self._active_lock:
                self._active.pop(execution.job_id, None)

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued job or kill its named running container."""
        with self._active_lock:
            active = self._active.get(job_id)
            session = self._explore_sessions.get(job_id)
        if active is None:
            return False
        active.cancel_event.set()
        self.capacity_queue.cancel_waiting(job_id)
        with active.lock:
            container_name = active.container_name
        if container_name:
            self._docker("kill", container_name, check=False, timeout=5.0)
            self._cleanup_resource("container", container_name, ("rm", "-f", container_name))
        if session is not None:
            session.close()
        return True

    def queue_position(self, job_id: str) -> int | None:
        return self.capacity_queue.queue_position(job_id)

    def _close_explore_session(self, session: SandboxExploreSession) -> None:
        """Clean every retained resource before releasing its one capacity lease."""
        active = session._active
        with active.lock:
            process = active.process
            active.process = None
            active.container_name = None
        if process is not None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
        cleanup = self._cleanup_explore_resources(
            container_name=session.container_name,
            proxy_name=session.proxy_name,
            network_name=session.network_name,
            runtime_volume_name=session.runtime_volume_name,
        )
        host_cleanup = _remove_explore_resource_root(session._resource_root)
        active.cleanup_safe = active.cleanup_safe and all(cleanup.values()) and host_cleanup
        with self._active_lock:
            self._explore_sessions.pop(session.spec.job_id, None)
            self._active.pop(session.spec.job_id, None)
        if active.cleanup_safe:
            session._lease.release()
        else:
            with self._active_lock:
                self._retained_capacity.add(session.spec.job_id)
            logger.error(
                "sandbox capacity retained because retained Explore cleanup was not confirmed job_id=%s",
                session.spec.job_id,
            )

    def _cleanup_failed_explore_open(
        self,
        *,
        active: _ActiveExecution,
        lease: Any,
        job_id: str,
        container_name: str,
        proxy_name: str,
        network_name: str,
        runtime_volume_name: str,
        resource_root: Path | None,
    ) -> None:
        """Best-effort rollback that still retains capacity if cleanup is uncertain."""
        with active.lock:
            process = active.process
            active.process = None
            active.container_name = None
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        cleanup = self._cleanup_explore_resources(
            container_name=container_name,
            proxy_name=proxy_name,
            network_name=network_name,
            runtime_volume_name=runtime_volume_name,
        )
        active.cleanup_safe = active.cleanup_safe and all(cleanup.values()) and _remove_explore_resource_root(resource_root)
        with self._active_lock:
            self._explore_sessions.pop(job_id, None)
            self._active.pop(job_id, None)
        if lease is not None:
            if active.cleanup_safe:
                lease.release()
            else:
                with self._active_lock:
                    self._retained_capacity.add(job_id)

    def _cleanup_explore_resources(
        self,
        *,
        container_name: str,
        proxy_name: str,
        network_name: str,
        runtime_volume_name: str,
    ) -> dict[tuple[str, str], bool]:
        """Respect Docker ownership order before deciding whether capacity is safe."""
        requests = (
            ("container", container_name, ("rm", "-f", container_name)),
            ("container", proxy_name, ("rm", "-f", proxy_name)),
            ("network", network_name, ("network", "rm", network_name)),
            ("volume", runtime_volume_name, ("volume", "rm", "-f", runtime_volume_name)),
        )
        result: dict[tuple[str, str], bool] = {}
        for kind, name, args in requests:
            if name:
                result[(kind, name)] = self._cleanup_resource(kind, name, args)
        return result

    def _write_explore_runtime_sources(self, resource_root: Path) -> None:
        """Create only host-owned static broker/proxy programs for this session."""
        _write_private_text(resource_root / "explore_broker.py", _EXPLORE_BROKER_SOURCE)
        _write_private_text(resource_root / "dynamic_proxy.py", _DYNAMIC_EXPLORE_PROXY_SOURCE)

    def _write_explore_allowlist(self, path: Path, allowed_domains: Sequence[str]) -> None:
        document = json.dumps(list(allowed_domains), ensure_ascii=True, separators=(",", ":"))
        _write_private_text(path, document)

    def _populate_explore_runtime_volume(
        self,
        *,
        volume_name: str,
        resource_root: Path,
        image: str,
        job_id: str,
        deadline: float,
    ) -> None:
        """Stream static host-owned broker bytes into a daemon-owned volume."""
        loader_name = f"osnews-explore-loader-{uuid.uuid4().hex[:12]}"
        try:
            self._docker(
                "create", "--name", loader_name,
                "--label", "osnews.discovery.sandbox=true",
                "--label", "osnews.discovery.role=explore-loader",
                "--label", f"osnews.discovery.job={job_id}",
                "--mount", f"type=volume,src={volume_name},dst=/opt/osnews-explore",
                image, "true",
                timeout=self._step_timeout(deadline, 10.0),
            )
            self._docker(
                "cp", "--archive", f"{resource_root}/.", f"{loader_name}:/opt/osnews-explore",
                timeout=self._step_timeout(deadline, 15.0),
            )
        finally:
            self._cleanup_resource("container", loader_name, ("rm", "-f", loader_name))

    def _update_explore_allowlist(
        self,
        proxy_name: str,
        allowed_domains: Sequence[str],
    ) -> None:
        """Host-only atomic policy update inside the trusted proxy volume."""
        payload = json.dumps(list(allowed_domains), ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        updater = (
            "import json,os,sys,tempfile; "
            "value=json.load(sys.stdin); "
            "assert isinstance(value,list) and 1<=len(value)<=32 and all(isinstance(x,str) for x in value); "
            "target='/opt/osnews-explore/allowed-domains.json'; "
            "fd,path=tempfile.mkstemp(prefix='.allowed-',dir='/opt/osnews-explore'); "
            "os.write(fd,json.dumps(value,separators=(',',':')).encode()); os.fsync(fd); os.close(fd); os.chmod(path,0o444); os.replace(path,target)"
        )
        try:
            result = subprocess.run(
                # The runtime image deliberately defaults to the unprivileged
                # UID used by the gVisor broker.  Its shared policy volume is
                # populated as root, however, so the fixed host updater must
                # enter the *trusted proxy* as root to create and atomically
                # replace the policy file.  This is not Agent-controlled: the
                # command, target, and JSON shape are all fixed here, while
                # ``extend_allowed_domains`` has already restricted values to
                # broker-observed exact hosts.
                [
                    self.settings.discovery_sandbox_docker_command,
                    "exec",
                    "-i",
                    "--user",
                    "0:0",
                    proxy_name,
                    "python",
                    "-c",
                    updater,
                ],
                input=payload,
                capture_output=True,
                check=False,
                timeout=5.0,
                env=self._docker_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise self._runtime_error("host Explore allowlist update could not reach the trusted proxy") from exc
        if result.returncode != 0:
            raise self._runtime_error(
                "host Explore allowlist update was rejected by the trusted proxy",
                stderr=self._safe_text(result.stderr),
            )

    def _start_explore_proxy(
        self,
        *,
        proxy_name: str,
        network_name: str,
        image: str,
        runtime_volume_name: str,
        job_id: str,
        active: _ActiveExecution,
        deadline: float,
    ) -> None:
        """Start the normal trusted proxy with a host-reloadable exact allowlist."""
        port = self.settings.discovery_sandbox_proxy_port
        self._raise_if_cancelled(active, "before Explore egress proxy creation")
        self._docker(
            "run", "--detach", "--pull=never", "--name", proxy_name,
            "--label", "osnews.discovery.sandbox=true",
            "--label", "osnews.discovery.role=egress-proxy",
            "--label", f"osnews.discovery.job={job_id}",
            "--network", "bridge", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=32", "--memory=128m",
            "--memory-swap=128m", "--cpus=0.25",
            "--mount", f"type=volume,src={runtime_volume_name},dst=/opt/osnews-explore",
            "--env", "SANDBOX_ALLOWED_DOMAINS_FILE=/opt/osnews-explore/allowed-domains.json",
            "--env", f"SANDBOX_PROXY_PORT={port}",
            image, "python", "/opt/osnews-explore/dynamic_proxy.py",
            timeout=self._step_timeout(deadline, 20.0),
        )
        self._docker(
            "network", "connect", "--alias", "egress-proxy", network_name, proxy_name,
            timeout=self._step_timeout(deadline, 10.0),
        )
        self._wait_for_proxy(proxy_name, port, active, deadline=deadline)

    def _explore_proxy_address(
        self,
        *,
        proxy_name: str,
        network_name: str,
        active: _ActiveExecution,
        deadline: float,
    ) -> str:
        """Resolve the proxy's internal address without relying on gVisor DNS.

        Some runsc environments cannot resolve Docker's embedded DNS alias even
        though the container is connected to the same internal network.  The
        host owns the network and pins this short-lived private address into the
        broker's proxy configuration instead of asking generated code to infer it.
        """
        result = self._docker(
            "network", "inspect", network_name,
            timeout=self._step_timeout(deadline, 10.0),
        )
        self._raise_if_cancelled(active, "after Explore proxy address inspection")
        try:
            networks = json.loads(result.stdout)
            containers = networks[0].get("Containers") if isinstance(networks, list) else None
            entries = containers.values() if isinstance(containers, dict) else ()
            raw_address = next(
                (
                    entry.get("IPv4Address")
                    for entry in entries
                    if isinstance(entry, dict) and entry.get("Name") == proxy_name
                ),
                None,
            )
            address = ipaddress.ip_address(str(raw_address).split("/", 1)[0])
        except (AttributeError, IndexError, StopIteration, ValueError, json.JSONDecodeError) as exc:
            raise self._runtime_error("Explore proxy has no valid internal address") from exc
        if address.version != 4 or not address.is_private:
            raise self._runtime_error("Explore proxy internal address is not private")
        return str(address)

    def _explore_broker_command(
        self,
        *,
        container_name: str,
        network_name: str,
        proxy_address: str,
        runtime_volume_name: str,
        image: str,
        job_id: str,
    ) -> list[str]:
        proxy_url = f"http://{proxy_address}:{self.settings.discovery_sandbox_proxy_port}"
        return [
            self.settings.discovery_sandbox_docker_command, "create", "--interactive", "--pull=never",
            "--runtime=runsc", "--name", container_name,
            "--label", "osnews.discovery.sandbox=true",
            "--label", "osnews.discovery.role=explore-broker",
            "--label", f"osnews.discovery.job={job_id}",
            "--network", network_name, "--read-only", "--cap-drop=ALL",
            "--add-host", f"egress-proxy:{proxy_address}",
            "--security-opt=no-new-privileges", "--pids-limit", str(self.settings.discovery_sandbox_pids_limit),
            "--cpus", str(self.settings.discovery_sandbox_cpus), "--memory", self.settings.discovery_sandbox_memory,
            "--memory-swap", self.settings.discovery_sandbox_memory, "--user", "65532:65532",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--tmpfs", "/workspace:rw,nosuid,nodev,size=128m",
            "--mount", f"type=volume,src={runtime_volume_name},dst=/opt/osnews-explore,readonly",
            "--env", f"HTTP_PROXY={proxy_url}", "--env", f"HTTPS_PROXY={proxy_url}",
            "--env", f"http_proxy={proxy_url}", "--env", f"https_proxy={proxy_url}",
            "--env", "SANDBOX_ALLOWED_DOMAINS_FILE=/opt/osnews-explore/allowed-domains.json",
            "--workdir", "/workspace", image, "python", "/opt/osnews-explore/explore_broker.py",
        ]

    def _assert_live_gvisor_container(
        self,
        container_name: str,
        *,
        active: _ActiveExecution,
        deadline: float,
    ) -> None:
        """Prove gVisor from the retained broker container, not a second probe."""
        result = self._docker(
            "exec", container_name, "dmesg", check=False,
            timeout=self._step_timeout(deadline, 10.0),
        )
        self._raise_if_cancelled(active, "after retained Explore gVisor inspection")
        if result.returncode != 0 or "gvisor" not in self._safe_text(result.stdout).lower():
            raise self._runtime_error("retained Explore broker did not identify a gVisor kernel")

    def _assert_runtime_available(
        self,
        active: _ActiveExecution | None = None,
        *,
        deadline: float | None = None,
    ) -> None:
        self._assert_local_docker_endpoint()
        if self.settings.discovery_sandbox_runtime != "runsc":
            raise self._runtime_error("sandbox runtime must be exactly runsc; fallback runtimes are forbidden")
        if self.settings.discovery_sandbox_platform != "systrap":
            raise self._runtime_error("this deployment requires the runsc systrap platform")
        result = self._docker(
            "info",
            "--format",
            "{{json .Runtimes}}",
            check=False,
            timeout=self._step_timeout(deadline, 10.0),
        )
        if result.returncode != 0:
            raise self._runtime_error("Docker daemon is unavailable", stderr=self._safe_text(result.stderr))
        if active is not None:
            self._raise_if_cancelled(active, "after Docker runtime inspection")
        try:
            runtimes = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise self._runtime_error("Docker returned invalid runtime metadata") from exc
        if "runsc" not in runtimes:
            raise self._runtime_error(
                "gVisor runsc is not registered with Docker; refusing ordinary Docker execution",
                available_runtimes=sorted(runtimes),
            )
        runtime_metadata = runtimes["runsc"]
        docker_runtime_path = (
            runtime_metadata.get("path") if isinstance(runtime_metadata, dict) else None
        )
        if not isinstance(docker_runtime_path, str) or "runsc" not in os.path.basename(
            docker_runtime_path
        ):
            raise self._runtime_error(
                "Docker runtime named runsc does not point to a runsc binary",
                docker_runtime_path=docker_runtime_path,
            )
        daemon_path = Path(self.settings.discovery_sandbox_docker_daemon_config)
        try:
            daemon_config = json.loads(daemon_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise self._runtime_error(
                "Docker daemon configuration proof is unavailable; mount daemon.json read-only",
                path=str(daemon_path),
            ) from exc
        configured_runtime = daemon_config.get("runtimes", {}).get("runsc")
        if not isinstance(configured_runtime, dict):
            raise self._runtime_error("daemon.json does not define the runsc runtime")
        configured_path = configured_runtime.get("path")
        configured_args = configured_runtime.get("runtimeArgs")
        if docker_runtime_path != configured_path:
            raise self._runtime_error(
                "Docker's active runsc path does not match daemon.json proof",
                docker_runtime_path=docker_runtime_path,
                configured_path=configured_path,
            )
        expected_binary = str(Path(self.settings.discovery_sandbox_runsc_binary).resolve())
        try:
            actual_configured_path = str(Path(str(configured_path)).resolve(strict=True))
        except OSError as exc:
            raise self._runtime_error("configured runsc binary does not exist on the backend host") from exc
        if actual_configured_path != expected_binary:
            raise self._runtime_error(
                "daemon.json runsc path does not match the approved host binary",
                configured_path=actual_configured_path,
                expected_path=expected_binary,
            )
        if not isinstance(configured_args, list) or "--platform=systrap" not in configured_args:
            raise self._runtime_error("daemon.json runsc runtimeArgs must include --platform=systrap")
        if active is not None:
            self._raise_if_cancelled(active, "before host runsc version inspection")
        try:
            version_result = subprocess.run(
                [expected_binary, "--version"],
                capture_output=True,
                check=False,
                timeout=self._step_timeout(deadline, 5.0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise self._runtime_error("unable to execute the configured host runsc binary") from exc
        version_text = self._safe_text(version_result.stdout + version_result.stderr)
        if active is not None:
            self._raise_if_cancelled(active, "after host runsc version inspection")
        if version_result.returncode != 0 or "runsc version" not in version_text.lower():
            raise self._runtime_error(
                "configured Docker runtime binary did not identify itself as runsc",
                version=version_text,
            )
        version_match = re.search(r"(?im)^runsc version\s+(\S+)", version_text)
        approved_version = self.settings.discovery_sandbox_runsc_version.strip()
        if not approved_version:
            raise self._runtime_error("approved runsc version is not configured")
        if version_match is None or version_match.group(1) != approved_version:
            raise self._runtime_error(
                "host runsc version does not match the approved deployment version",
                actual_version=version_match.group(1) if version_match else None,
                approved_version=approved_version,
            )

    def _resolve_immutable_image(self, runtime_version: str, *, deadline: float | None = None) -> str:
        image = self.settings.discovery_sandbox_runtime_images.get(runtime_version)
        if not image:
            raise self._runtime_error(
                "Manifest runtime_version has no approved immutable image mapping",
                runtime_version=runtime_version,
            )
        if not _DIGEST_IMAGE.fullmatch(image):
            raise self._runtime_error(
                "approved Runtime image must be pinned by sha256 digest",
                runtime_version=runtime_version,
            )
        inspected = self._docker(
            "image",
            "inspect",
            image,
            check=False,
            timeout=self._step_timeout(deadline, 10.0),
        )
        if inspected.returncode != 0:
            raise self._runtime_error(
                "approved Runtime image is not present locally; runtime pulls are disabled",
                runtime_version=runtime_version,
                image=image,
            )
        return image

    def _assert_local_docker_endpoint(self) -> None:
        if os.environ.get("DOCKER_HOST", "").startswith(("tcp://", "ssh://", "http://", "https://")):
            raise self._runtime_error("remote Docker endpoints are forbidden for sandbox execution")
        if os.environ.get("DOCKER_CONTEXT") not in {None, "", "default"}:
            raise self._runtime_error("non-default Docker contexts are forbidden for sandbox execution")
        socket_path = Path(self.settings.discovery_sandbox_docker_socket)
        try:
            socket_mode = socket_path.stat().st_mode
        except OSError as exc:
            raise self._runtime_error("local Docker unix socket is unavailable") from exc
        if not socket_path.is_absolute() or not stat.S_ISSOCK(socket_mode):
            raise self._runtime_error("sandbox Docker endpoint must be a local unix socket")
        docker_binary = shutil.which(self.settings.discovery_sandbox_docker_command)
        if docker_binary is None or os.path.basename(docker_binary) != "docker":
            raise self._runtime_error("sandbox Docker command must resolve to the local docker CLI")

    def _probe_live_gvisor(
        self,
        image: str,
        *,
        active: _ActiveExecution,
        deadline: float,
        job_id: str,
    ) -> str:
        """Run a disposable no-network probe and prove the live kernel is gVisor."""
        self._raise_if_cancelled(active, "before live gVisor probe")
        probe_nonce = secrets.token_hex(24)
        probe_name = f"osnews-runsc-probe-{uuid.uuid4().hex[:12]}"
        try:
            result = self._docker(
                "run",
                "--rm",
                "--pull=never",
                "--runtime=runsc",
                "--name",
                probe_name,
                "--label",
                "osnews.discovery.sandbox=true",
                "--label",
                "osnews.discovery.role=runtime-probe",
                "--label",
                f"osnews.discovery.job={job_id}",
                "--label",
                f"osnews.discovery.probe_nonce={probe_nonce}",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                image,
                "dmesg",
                check=False,
                timeout=self._step_timeout(deadline, 20.0),
            )
            if result.returncode != 0 or "gvisor" not in self._safe_text(result.stdout).lower():
                raise self._runtime_error(
                    "live runsc probe did not identify a gVisor kernel",
                    stderr=self._safe_text(result.stderr),
                )
        finally:
            try:
                cleaned = self._cleanup_resource(
                    "container", probe_name, ("rm", "-f", probe_name)
                )
            except Exception:  # noqa: BLE001 - retain capacity when probe cleanup is uncertain
                cleaned = False
                logger.exception("live gVisor probe cleanup failed container=%s", probe_name)
            active.cleanup_safe = active.cleanup_safe and cleaned
        self._raise_if_cancelled(active, "after live gVisor probe")
        return probe_nonce

    @contextmanager
    def _immutable_artifact_snapshot(
        self,
        execution: SandboxExecution,
        *,
        deadline: float,
    ) -> Iterator[ConnectorArtifact]:
        """Copy reviewed bytes after queueing and mount only the verified snapshot."""
        if not execution.expected_checksum or not execution.expected_signature:
            raise self._runtime_error("approved checksum and signature are required for execution")
        self._step_timeout(deadline, 1.0)
        with tempfile.TemporaryDirectory(prefix="osnews-connector-snapshot-") as temporary_root:
            root = Path(temporary_root)
            version_directory = (
                root
                / execution.kind
                / execution.artifact.manifest.connector_key
                / f"v{execution.artifact.manifest.version}"
            )
            version_directory.mkdir(parents=True, mode=0o700)
            self._copy_artifact_file(
                execution.artifact.manifest_path,
                version_directory / MANIFEST_FILENAME,
                MAX_MANIFEST_BYTES,
            )
            self._copy_artifact_file(
                execution.artifact.connector_path,
                version_directory / CONNECTOR_FILENAME,
                MAX_CONNECTOR_BYTES,
            )
            snapshot = load_connector_artifact(
                root,
                kind=execution.kind,
                connector_key=execution.artifact.manifest.connector_key,
                version=execution.artifact.manifest.version,
            )
            if not secrets.compare_digest(snapshot.manifest.checksum, execution.expected_checksum):
                raise self._runtime_error("snapshot checksum differs from the approved checksum")
            if not secrets.compare_digest(snapshot.signature, execution.expected_signature):
                raise self._runtime_error("snapshot signature differs from the approved signature")
            for path in (snapshot.manifest_path, snapshot.connector_path):
                path.chmod(0o444)
            snapshot.directory.chmod(0o555)
            self._step_timeout(deadline, 1.0)
            yield snapshot

    @staticmethod
    def _copy_artifact_file(source: Path, destination: Path, maximum_bytes: int) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
                raise ConnectorProtocolError(
                    ConnectorErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "connector artifact source is not a bounded regular file",
                )
            output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                copied = 0
                while chunk := os.read(descriptor, 64 * 1024):
                    copied += len(chunk)
                    if copied > maximum_bytes:
                        raise ConnectorProtocolError(
                            ConnectorErrorCode.ARTIFACT_INTEGRITY_ERROR,
                            "connector artifact changed beyond its size limit while snapshotting",
                        )
                    pending = memoryview(chunk)
                    while pending:
                        written = os.write(output, pending)
                        pending = pending[written:]
                os.fsync(output)
            finally:
                os.close(output)
        finally:
            os.close(descriptor)

    def _execute_container(
        self,
        execution: SandboxExecution,
        active: _ActiveExecution,
        *,
        image: str,
        started_at: float,
        deadline: float,
        timeout_seconds: float,
        on_event: Callable[[dict[str, Any]], None] | None,
        probe_nonce: str,
        started_nonce: str,
    ) -> SandboxExecutionResult:
        suffix = _SAFE_JOB_ID.sub("-", execution.job_id).strip("-.")[:32] or "job"
        nonce = uuid.uuid4().hex[:10]
        network_name = f"osnews-sbx-{suffix}-{nonce}"
        proxy_name = f"osnews-proxy-{suffix}-{nonce}"
        container_name = f"osnews-connector-{suffix}-{nonce}"
        artifact_loader_name = f"osnews-artifact-loader-{suffix}-{nonce}"
        artifact_volume_name = f"osnews-artifacts-{suffix}-{nonce}"
        events: list[dict[str, Any]] = []
        try:
            self._raise_if_cancelled(active, "before isolated network creation")
            self._docker(
                "network",
                "create",
                "--internal",
                "--label",
                "osnews.discovery.sandbox=true",
                "--label",
                f"osnews.discovery.job={execution.job_id}",
                network_name,
                timeout=self._step_timeout(deadline, 15.0),
            )
            self._raise_if_cancelled(active, "after isolated network creation")
            proxy_address = self._start_proxy(
                proxy_name=proxy_name,
                network_name=network_name,
                image=image,
                allowed_domains=execution.artifact.manifest.allowed_domains,
                job_id=execution.job_id,
                active=active,
                deadline=deadline,
            )
            self._raise_if_cancelled(active, "before connector artifact volume creation")
            self._docker(
                "volume",
                "create",
                "--label",
                "osnews.discovery.sandbox=true",
                "--label",
                f"osnews.discovery.job={execution.job_id}",
                artifact_volume_name,
                timeout=self._step_timeout(deadline, 10.0),
            )
            self._docker(
                "create",
                "--name",
                artifact_loader_name,
                "--label",
                "osnews.discovery.sandbox=true",
                "--label",
                "osnews.discovery.role=artifact-loader",
                "--label",
                f"osnews.discovery.job={execution.job_id}",
                "--mount",
                f"type=volume,src={artifact_volume_name},dst=/connectors",
                image,
                "true",
                timeout=self._step_timeout(deadline, 10.0),
            )
            snapshot_root = execution.artifact.directory.parents[2]
            self._docker(
                "cp",
                "--archive",
                f"{snapshot_root}/.",
                f"{artifact_loader_name}:/connectors",
                timeout=self._step_timeout(deadline, 10.0),
            )
            self._docker(
                "rm",
                "-f",
                artifact_loader_name,
                timeout=self._step_timeout(deadline, 5.0),
            )
            self._raise_if_cancelled(active, "after connector artifact volume population")
            self._raise_if_cancelled(active, "before connector container creation")
            remaining_seconds = self._step_timeout(deadline, timeout_seconds)
            runner_timeout = max(0.1, remaining_seconds - min(1.0, remaining_seconds / 10))
            command = self._connector_command(
                execution,
                image=image,
                network_name=network_name,
                container_name=container_name,
                artifact_volume_name=artifact_volume_name,
                proxy_address=proxy_address,
                timeout_seconds=runner_timeout,
            )
            self._docker(*command[1:], timeout=self._step_timeout(deadline, 20.0))
            with active.lock:
                active.container_name = container_name
            remaining_seconds = self._step_timeout(deadline, timeout_seconds)
            self._raise_if_cancelled(active, "before connector container start")
            invocation_json = canonical_connector_document(execution.invocation)
            try:
                audit_invocation = ConnectorInvocation.model_validate(
                    connector_audit_projection(execution.invocation)
                )
            except (ValidationError, ValueError) as exc:
                raise ConnectorProtocolError(
                    ConnectorErrorCode.INVALID_INPUT,
                    "connector invocation cannot be represented safely for audit",
                ) from exc
            invocation_sha256 = canonical_connector_digest(audit_invocation)
            process = subprocess.Popen(
                [
                    self.settings.discovery_sandbox_docker_command,
                    "start",
                    "--attach",
                    "--interactive",
                    container_name,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._docker_environment(),
            )
            with active.lock:
                active.process = process
            stdout, stderr, streamed_events = self._communicate_streaming(
                process,
                invocation_json,
                timeout_seconds=remaining_seconds,
                on_event=on_event,
            )
            events.extend(streamed_events)
            proxy_event = self._collect_proxy_denial_event(proxy_name)
            if proxy_event is not None:
                if len(events) >= MAX_HOST_EVENTS:
                    events = events[: MAX_HOST_EVENTS - 1]
                events.append(proxy_event)
                if on_event is not None:
                    on_event(proxy_event)
            if active.cancel_event.is_set():
                raise ConnectorProtocolError(ConnectorErrorCode.CANCELLED, "sandbox execution was cancelled")
            if process.returncode != int(ConnectorExitCode.SUCCESS):
                self._raise_process_error(
                    process.returncode,
                    stderr,
                    events=events,
                )
            if len(stdout) > MAX_HOST_STDOUT_BYTES:
                raise ConnectorProtocolError(
                    ConnectorErrorCode.INVALID_OUTPUT,
                    "connector stdout exceeded the host output limit",
                )
            try:
                raw_output = json.loads(stdout)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ConnectorProtocolError(
                    ConnectorErrorCode.INVALID_OUTPUT,
                    "sandbox stdout does not contain one valid {items, stats} JSON document",
                ) from exc
            try:
                output = ConnectorOutput.model_validate(raw_output)
            except ValidationError as exc:
                raise ConnectorProtocolError(
                    ConnectorErrorCode.INVALID_OUTPUT,
                    "connector result does not match the {items, stats} contract",
                    details={
                        "validation_errors": exc.errors(
                            include_url=False,
                            include_input=False,
                        )
                    },
                ) from exc
            try:
                audit_output = ConnectorOutput.model_validate(
                    connector_audit_projection(output)
                )
            except (ValidationError, ValueError) as exc:
                audit_details: dict[str, Any] = {
                    "reason": "audit_output_validation_failed"
                    if isinstance(exc, ValidationError)
                    else "audit_projection_failed",
                    "approx_bytes": None,
                    "node_count": None,
                    "depth": None,
                    "item_count": len(output.items),
                }
                if isinstance(exc, DiscoveryDataBoundsError):
                    audit_details.update(exc.details)
                if isinstance(exc, ValidationError):
                    audit_details["validation_errors"] = exc.errors(
                        include_url=False,
                        include_input=False,
                    )
                raise ConnectorProtocolError(
                    ConnectorErrorCode.INVALID_OUTPUT,
                    "connector output cannot be represented safely for audit",
                    details={"audit": audit_details},
                ) from exc
            output_sha256 = canonical_connector_digest(audit_output)
            try:
                attestation = _create_runtime_attestation(
                    execution=execution,
                    settings=self.settings,
                    image=image,
                    probe_nonce=probe_nonce,
                    started_nonce=started_nonce,
                    invocation_sha256=invocation_sha256,
                    output_sha256=output_sha256,
                )
            except (OSError, ValueError) as exc:
                raise self._runtime_error(
                    "host sandbox attestation keyring is unavailable or unsafe"
                ) from exc
            return SandboxExecutionResult(
                output=output,
                audit_invocation=audit_invocation,
                audit_output=audit_output,
                events=tuple(events),
                exit_code=ConnectorExitCode.SUCCESS,
                elapsed_seconds=time.monotonic() - started_at,
                attestation=attestation,
            )
        except subprocess.TimeoutExpired as exc:
            self._docker("kill", container_name, check=False, timeout=5.0)
            raise ConnectorProtocolError(
                ConnectorErrorCode.TIMEOUT,
                f"sandbox exceeded its {timeout_seconds:g} second wall-clock limit",
            ) from exc
        finally:
            with active.lock:
                active.container_name = None
                active.process = None
            try:
                container_cleanup = self._cleanup_resources_parallel(
                    (
                        ("container", container_name, ("rm", "-f", container_name)),
                        ("container", proxy_name, ("rm", "-f", proxy_name)),
                        (
                            "container",
                            artifact_loader_name,
                            ("rm", "-f", artifact_loader_name),
                        ),
                    )
                )
                # Networks and volumes depend on the containers being gone. Do
                # not race their deletion with endpoint/mount teardown.
                dependent_cleanup = self._cleanup_resources_parallel(
                    (
                        ("network", network_name, ("network", "rm", network_name)),
                        (
                            "volume",
                            artifact_volume_name,
                            ("volume", "rm", "-f", artifact_volume_name),
                        ),
                    )
                )
                active.cleanup_safe = (
                    active.cleanup_safe
                    and container_cleanup.get(("container", container_name), False)
                    and container_cleanup.get(("container", proxy_name), False)
                    and container_cleanup.get(("container", artifact_loader_name), False)
                    and dependent_cleanup.get(("network", network_name), False)
                    and dependent_cleanup.get(("volume", artifact_volume_name), False)
                )
            except Exception:  # noqa: BLE001 - retain capacity on cleanup orchestration failure
                active.cleanup_safe = False
                logger.exception("sandbox cleanup orchestration failed job_id=%s", execution.job_id)

    def _collect_proxy_denial_event(self, proxy_name: str) -> dict[str, Any] | None:
        """Aggregate trusted proxy policy and upstream-connect evidence."""
        result = self._docker("logs", proxy_name, check=False, timeout=2.0)
        if result.returncode != 0:
            logger.warning("unable to read sandbox proxy denial evidence proxy=%s", proxy_name)
            return None
        counts: dict[tuple[str, int, str], int] = {}
        upstream_connection_failures = 0
        for raw_line in result.stdout.decode("utf-8", errors="replace").splitlines()[:200]:
            try:
                document = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(document, dict):
                continue
            if document.get("type") == "sandbox_upstream_connection_failed":
                upstream_connection_failures += 1
                continue
            if document.get("type") != "sandbox_egress_denied":
                continue
            hostname = str(document.get("hostname") or "").lower().rstrip(".")
            protocol = str(document.get("protocol") or "").lower()
            port = document.get("port")
            if (
                not hostname
                or len(hostname) > 253
                or protocol not in {"http", "https"}
                or type(port) is not int
                or port not in {80, 443}
                or not all(
                    label
                    and len(label) <= 63
                    and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                    for label in hostname.split(".")
                )
            ):
                continue
            key = (hostname, port, protocol)
            counts[key] = counts.get(key, 0) + 1
        if counts:
            denied_targets = [
                {
                    "hostname": hostname,
                    "port": port,
                    "protocol": protocol,
                    "count": count,
                }
                for (hostname, port, protocol), count in sorted(counts.items())[:50]
            ]
            return {
                "type": "connector_event",
                "event": "sandbox_egress_denied",
                "level": "warning",
                "message": "sandbox blocked undeclared public-domain requests",
                "denied_targets": denied_targets,
            }
        if upstream_connection_failures:
            return {
                "type": "connector_event",
                "event": "sandbox_upstream_connection_failed",
                "level": "warning",
                "message": "sandbox could not reach an approved upstream domain",
                "count": upstream_connection_failures,
            }
        return None

    def _communicate_streaming(
        self,
        process: subprocess.Popen[bytes],
        invocation: bytes,
        *,
        timeout_seconds: float,
        on_event: Callable[[dict[str, Any]], None] | None,
    ) -> tuple[bytes, bytes, list[dict[str, Any]]]:
        """Drain both pipes through EOF and emit JSON stderr lines as they arrive."""
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise self._runtime_error("sandbox process pipes were not created")
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        events: list[dict[str, Any]] = []
        stdout_done = Event()
        stderr_done = Event()
        reader_errors: list[BaseException] = []
        reader_errors_lock = Lock()

        def record_reader_error(exc: BaseException) -> None:
            with reader_errors_lock:
                reader_errors.append(exc)

        def read_stdout() -> None:
            try:
                while chunk := process.stdout.read(64 * 1024):
                    remaining = max(MAX_HOST_STDOUT_BYTES + 1 - len(stdout_buffer), 0)
                    if remaining:
                        stdout_buffer.extend(chunk[:remaining])
                    # Always continue draining. Bytes beyond the fixed buffer are
                    # discarded so a hostile child cannot block or grow host memory.
            except BaseException as exc:  # pipe failures must never yield partial JSON
                record_reader_error(exc)
            finally:
                stdout_done.set()

        def read_stderr() -> None:
            captured = 0
            parsing_stopped = False

            def emit_truncation(reason: str) -> None:
                nonlocal parsing_stopped
                if parsing_stopped:
                    return
                parsing_stopped = True
                payload = {
                    "type": "connector_event",
                    "event": "host_log_truncated",
                    "level": "warning",
                    "message": "sandbox stderr event processing was truncated by the host",
                    "reason": reason,
                    "max_stderr_bytes": MAX_HOST_STDERR_BYTES,
                    "max_events": MAX_HOST_EVENTS,
                }
                events.append(payload)
                if on_event is not None:
                    try:
                        on_event(payload)
                    except Exception:
                        pass

            try:
                while raw_line := process.stderr.readline(64 * 1024):
                    remaining = max(MAX_HOST_STDERR_BYTES - captured, 0)
                    if remaining:
                        accepted = raw_line[:remaining]
                        stderr_buffer.extend(accepted)
                        captured += len(accepted)
                    if parsing_stopped:
                        continue
                    if len(raw_line) > remaining:
                        emit_truncation("stderr_byte_limit")
                        continue
                    # Reserve the final slot for one aggregate truncation event.
                    if len(events) >= MAX_HOST_EVENTS - 1:
                        emit_truncation("event_count_limit")
                        continue
                    payload = self._parse_event_line(raw_line)
                    events.append(payload)
                    if on_event is not None:
                        try:
                            on_event(payload)
                        except Exception:
                            # UI/event persistence must not be able to stop pipe draining
                            # and deadlock an otherwise bounded connector process.
                            pass
            except BaseException as exc:  # pipe failures must be explicit
                record_reader_error(exc)
            finally:
                stderr_done.set()

        stdout_thread = threading.Thread(target=read_stdout, name="sandbox-stdout", daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, name="sandbox-stderr", daemon=True)
        communication_deadline = time.monotonic() + timeout_seconds
        stdout_thread.start()
        stderr_thread.start()
        process_error: BaseException | None = None
        try:
            process.stdin.write(invocation)
            process.stdin.close()
            process.wait(timeout=max(0.001, communication_deadline - time.monotonic()))
        except BrokenPipeError:
            try:
                process.wait(timeout=max(0.001, communication_deadline - time.monotonic()))
            except BaseException as exc:
                process_error = exc
        except BaseException as exc:
            process_error = exc

        if process_error is None:
            drain_deadline = min(
                communication_deadline,
                time.monotonic() + HOST_PIPE_DRAIN_TIMEOUT_SECONDS,
            )
            for completed in (stdout_done, stderr_done):
                completed.wait(max(0.0, drain_deadline - time.monotonic()))
            if not stdout_done.is_set() or not stderr_done.is_set():
                raise self._runtime_error(
                    "sandbox output pipes did not reach EOF after the container exited",
                    stdout_eof=stdout_done.is_set(),
                    stderr_eof=stderr_done.is_set(),
                )
            stdout_thread.join()
            stderr_thread.join()
            if reader_errors:
                raise self._runtime_error(
                    "sandbox output pipe could not be drained safely",
                    reader_error=type(reader_errors[0]).__name__,
                )
        else:
            raise process_error
        return bytes(stdout_buffer), bytes(stderr_buffer), events

    def _start_proxy(
        self,
        *,
        proxy_name: str,
        network_name: str,
        image: str,
        allowed_domains: tuple[str, ...],
        job_id: str,
        active: _ActiveExecution,
        deadline: float,
    ) -> str:
        port = self.settings.discovery_sandbox_proxy_port
        self._raise_if_cancelled(active, "before egress proxy creation")
        self._docker(
            "run",
            "--detach",
            "--pull=never",
            "--name",
            proxy_name,
            "--label",
            "osnews.discovery.sandbox=true",
            "--label",
            "osnews.discovery.role=egress-proxy",
            "--label",
            f"osnews.discovery.job={job_id}",
            "--network",
            "bridge",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=32",
            "--memory=128m",
            "--memory-swap=128m",
            "--cpus=0.25",
            "--env",
            f"SANDBOX_ALLOWED_DOMAINS={json.dumps(allowed_domains)}",
            "--env",
            f"SANDBOX_PROXY_PORT={port}",
            image,
            "python",
            "-m",
            "app.discovery.sandbox.egress_proxy",
            timeout=self._step_timeout(deadline, 20.0),
        )
        self._raise_if_cancelled(active, "after egress proxy creation")
        self._docker(
            "network",
            "connect",
            "--alias",
            "egress-proxy",
            network_name,
            proxy_name,
            timeout=self._step_timeout(deadline, 10.0),
        )
        self._raise_if_cancelled(active, "after egress proxy network attachment")
        self._wait_for_proxy(proxy_name, port, active, deadline=deadline)
        inspected = self._docker(
            "container",
            "inspect",
            proxy_name,
            timeout=self._step_timeout(deadline, 5.0),
        )
        try:
            payload = json.loads(inspected.stdout)
            address = payload[0]["NetworkSettings"]["Networks"][network_name]["IPAddress"]
            parsed_address = ipaddress.ip_address(address)
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise self._runtime_error(
                "sandbox egress proxy has no valid internal network address"
            ) from exc
        if parsed_address.version != 4 or not parsed_address.is_private:
            raise self._runtime_error(
                "sandbox egress proxy internal address is outside the private network"
            )
        return str(parsed_address)

    def _wait_for_proxy(
        self,
        proxy_name: str,
        port: int,
        active: _ActiveExecution,
        *,
        deadline: float,
    ) -> None:
        """Do not start untrusted code until the policy proxy is accepting sockets."""
        probe = (
            "import socket; "
            f"s=socket.create_connection(('127.0.0.1',{port}),1); s.close()"
        )
        for _ in range(20):
            self._raise_if_cancelled(active, "while waiting for egress proxy readiness")
            result = self._docker(
                "exec",
                proxy_name,
                "python",
                "-c",
                probe,
                check=False,
                timeout=self._step_timeout(deadline, 2.0),
            )
            if result.returncode == 0:
                return
            time.sleep(0.1)
        logs = self._docker(
            "logs",
            proxy_name,
            check=False,
            timeout=self._step_timeout(deadline, 5.0),
        )
        raise self._runtime_error(
            "sandbox egress proxy did not become ready",
            stderr=self._safe_text(logs.stderr or logs.stdout),
        )

    def _connector_command(
        self,
        execution: SandboxExecution,
        *,
        image: str,
        network_name: str,
        container_name: str,
        artifact_volume_name: str,
        proxy_address: str,
        timeout_seconds: float,
    ) -> list[str]:
        artifact = execution.artifact
        artifact_target = (
            f"/connectors/{execution.kind}/{artifact.manifest.connector_key}"
            f"/v{artifact.manifest.version}"
        )
        proxy_url = f"http://{proxy_address}:{self.settings.discovery_sandbox_proxy_port}"
        command = [
            self.settings.discovery_sandbox_docker_command,
            "create",
            "--interactive",
            "--pull=never",
            "--runtime=runsc",
            "--name",
            container_name,
            "--label",
            "osnews.discovery.sandbox=true",
            "--label",
            "osnews.discovery.role=connector",
            "--label",
            f"osnews.discovery.job={execution.job_id}",
            "--network",
            network_name,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            str(self.settings.discovery_sandbox_pids_limit),
            "--cpus",
            str(self.settings.discovery_sandbox_cpus),
            "--memory",
            self.settings.discovery_sandbox_memory,
            "--memory-swap",
            self.settings.discovery_sandbox_memory,
            "--user",
            "65532:65532",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--tmpfs",
            "/workspace:rw,nosuid,nodev,size=128m",
            "--mount",
            f"type=volume,src={artifact_volume_name},dst=/connectors,readonly",
            "--env",
            f"HTTP_PROXY={proxy_url}",
            "--env",
            f"HTTPS_PROXY={proxy_url}",
            "--env",
            f"http_proxy={proxy_url}",
            "--env",
            f"https_proxy={proxy_url}",
            "--workdir",
            "/workspace",
            image,
            "python",
            "-m",
            "app.discovery.plugin.runner",
            "--connector-root",
            "/connectors",
            "--kind",
            execution.kind,
            "--connector-key",
            artifact.manifest.connector_key,
            "--version",
            str(artifact.manifest.version),
            "--timeout-seconds",
            str(timeout_seconds),
            "--expected-checksum",
            execution.expected_checksum,
            "--expected-signature",
            execution.expected_signature,
        ]
        return command

    def _parse_event_line(self, raw_line: bytes) -> dict[str, Any]:
        text = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        # Connector and runner share one process. No stderr payload can be
        # authenticated against arbitrary plugin code, so every line is data.
        return {
            "type": "connector_event",
            "event": "plugin_runtime_log",
            "level": "warning",
            "message": text[:16_384],
        }

    def _raise_process_error(
        self,
        returncode: int,
        stderr: bytes,
        *,
        events: Sequence[dict[str, Any]],
    ) -> None:
        try:
            exit_code = ConnectorExitCode(returncode)
        except ValueError:
            exit_code = ConnectorExitCode.RUNTIME_ERROR
        code_by_exit = {
            ConnectorExitCode.TIMEOUT: ConnectorErrorCode.TIMEOUT,
            ConnectorExitCode.CANCELLED: ConnectorErrorCode.CANCELLED,
            ConnectorExitCode.INVALID_OUTPUT: ConnectorErrorCode.INVALID_OUTPUT,
            ConnectorExitCode.PLUGIN_LOAD_ERROR: ConnectorErrorCode.PLUGIN_LOAD_ERROR,
            ConnectorExitCode.PLUGIN_EXECUTION_ERROR: ConnectorErrorCode.PLUGIN_EXECUTION_ERROR,
            ConnectorExitCode.NETWORK_ERROR: ConnectorErrorCode.NETWORK_ERROR,
        }
        raise ConnectorProtocolError(
            code_by_exit.get(exit_code, ConnectorErrorCode.RUNTIME_ERROR),
            "connector container failed",
            details={
                "returncode": returncode,
                "stderr": self._safe_text(stderr),
                "tool_events": list(events[:50]),
            },
        )

    def _docker(
        self,
        *args: str,
        check: bool = True,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                [self.settings.discovery_sandbox_docker_command, *args],
                capture_output=True,
                check=False,
                timeout=timeout,
                env=self._docker_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if check:
                raise self._runtime_error(f"Docker command failed: {args[0]}") from exc
            return subprocess.CompletedProcess(args, 1, b"", str(exc).encode())
        if check and result.returncode != 0:
            raise self._runtime_error(
                f"Docker command failed: {args[0]}",
                stderr=self._safe_text(result.stderr),
            )
        return result

    def _docker_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["DOCKER_HOST"] = f"unix://{self.settings.discovery_sandbox_docker_socket}"
        environment.pop("DOCKER_CONTEXT", None)
        environment.pop("DOCKER_TLS_VERIFY", None)
        environment.pop("DOCKER_CERT_PATH", None)
        return environment

    def cleanup_stale_resources(
        self,
        *,
        protected_job_ids: set[str] | None = None,
    ) -> tuple[dict[str, str], ...]:
        """Recovery seam for startup: remove resources carrying the sandbox label."""
        self._assert_local_docker_endpoint()
        failures: list[dict[str, str]] = []
        protected: dict[str, set[str]] = {
            "container": set(),
            "network": set(),
            "volume": set(),
        }
        for job_id in protected_job_ids or set():
            if not job_id or _SAFE_JOB_ID.sub("", job_id) != job_id:
                return ({"resource_type": "sandbox", "resource": "protected-job", "error": "unsafe_job_id"},)
            label = f"label=osnews.discovery.job={job_id}"
            protected_queries = (
                ("container", ("ps", "-aq", "--filter", label)),
                ("network", ("network", "ls", "-q", "--filter", label)),
                ("volume", ("volume", "ls", "-q", "--filter", label)),
            )
            for kind, query in protected_queries:
                result = self._docker(*query, check=False, timeout=10.0)
                if result.returncode != 0:
                    # Fail closed: do not run the broad stale-resource deletion
                    # if a live formal owner's resources cannot be identified.
                    return (
                        {
                            "resource_type": kind,
                            "resource": "protected-query",
                            "error": self._safe_text(result.stderr),
                        },
                    )
                protected[kind].update(
                    result.stdout.decode("utf-8", errors="replace").split()
                )
        queries = (
            ("container", ("ps", "-aq", "--filter", "label=osnews.discovery.sandbox=true")),
            ("network", ("network", "ls", "-q", "--filter", "label=osnews.discovery.sandbox=true")),
            ("volume", ("volume", "ls", "-q", "--filter", "label=osnews.discovery.sandbox=true")),
        )
        for kind, query in queries:
            result = self._docker(*query, check=False, timeout=10.0)
            if result.returncode != 0:
                failures.append({"resource_type": kind, "resource": "query", "error": self._safe_text(result.stderr)})
                continue
            resources = [
                resource
                for resource in result.stdout.decode("utf-8", errors="replace").split()
                if resource not in protected[kind]
            ]
            cleanup_requests = tuple(
                (
                    kind,
                    resource,
                    ("rm", "-f", resource)
                    if kind == "container"
                    else ("network", "rm", resource)
                    if kind == "network"
                    else ("volume", "rm", "-f", resource),
                )
                for resource in resources
            )
            cleanup = self._cleanup_resources_parallel(cleanup_requests)
            for (resource_type, resource), succeeded in cleanup.items():
                if not succeeded:
                    failures.append(
                        {
                            "resource_type": resource_type,
                            "resource": resource,
                            "error": "cleanup_failed",
                        }
                    )
        if not failures:
            with self._active_lock:
                retained = tuple(self._retained_capacity)
                self._retained_capacity.clear()
            for job_id in retained:
                self.capacity_queue.release(job_id)
        return tuple(failures)

    def cleanup_job_resources(self, job_id: str) -> tuple[dict[str, str], ...]:
        """Remove only Docker resources carrying the exact persisted job label."""
        if not job_id or _SAFE_JOB_ID.sub("", job_id) != job_id:
            raise ValueError("unsafe sandbox job id")
        self._assert_local_docker_endpoint()
        failures: list[dict[str, str]] = []
        label = f"label=osnews.discovery.job={job_id}"
        queries = (
            ("container", ("ps", "-aq", "--filter", label)),
            ("network", ("network", "ls", "-q", "--filter", label)),
            ("volume", ("volume", "ls", "-q", "--filter", label)),
        )
        for kind, query in queries:
            result = self._docker(*query, check=False, timeout=10.0)
            if result.returncode != 0:
                failures.append(
                    {"resource_type": kind, "resource": "query", "error": self._safe_text(result.stderr)}
                )
                continue
            requests = tuple(
                (
                    kind,
                    resource,
                    ("rm", "-f", resource)
                    if kind == "container"
                    else ("network", "rm", resource)
                    if kind == "network"
                    else ("volume", "rm", "-f", resource),
                )
                for resource in result.stdout.decode("utf-8", errors="replace").split()
            )
            for (resource_type, resource), succeeded in self._cleanup_resources_parallel(requests).items():
                if not succeeded:
                    failures.append(
                        {"resource_type": resource_type, "resource": resource, "error": "cleanup_failed"}
                    )
        return tuple(failures)

    def _cleanup_resources_parallel(
        self,
        resources: tuple[tuple[str, str, tuple[str, ...]], ...],
    ) -> dict[tuple[str, str], bool]:
        if not resources:
            return {}
        with ThreadPoolExecutor(max_workers=len(resources), thread_name_prefix="sandbox-cleanup") as pool:
            futures = {
                (kind, name): pool.submit(self._cleanup_resource, kind, name, args)
                for kind, name, args in resources
            }
            results: dict[tuple[str, str], bool] = {}
            for key, future in futures.items():
                try:
                    results[key] = future.result()
                except Exception as exc:  # noqa: BLE001 - cleanup must fail closed
                    results[key] = False
                    logger.exception(
                        "sandbox cleanup worker failed resource_type=%s resource=%s error=%s",
                        key[0],
                        key[1],
                        exc,
                    )
            return results

    def _cleanup_resource(self, kind: str, name: str, args: tuple[str, ...]) -> bool:
        for attempt in range(1, 3):
            result = self._docker(*args, check=False, timeout=1.0)
            error = self._safe_text(result.stderr)
            if result.returncode == 0 or "No such" in error or "not found" in error.lower():
                return True
            if attempt < 2:
                time.sleep(0.05)
        warning = {
            "event": "sandbox_cleanup_failed",
            "level": "warning",
            "resource_type": kind,
            "resource": name,
            "attempts": 2,
            "error": error,
        }
        logger.warning("%s", json.dumps(warning, ensure_ascii=False, separators=(",", ":")))
        return False

    @staticmethod
    def _step_timeout(deadline: float | None, cap_seconds: float) -> float:
        if deadline is None:
            return cap_seconds
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectorProtocolError(
                ConnectorErrorCode.TIMEOUT,
                "sandbox wall-clock deadline expired during setup",
            )
        return min(cap_seconds, remaining)

    @staticmethod
    def _safe_text(value: bytes) -> str:
        return value.decode("utf-8", errors="replace")[:4096]

    @staticmethod
    def _runtime_error(message: str, **details: Any) -> ConnectorProtocolError:
        return ConnectorProtocolError(ConnectorErrorCode.RUNTIME_ERROR, message, details=details)

    @staticmethod
    def _raise_if_cancelled(active: _ActiveExecution, stage: str) -> None:
        if active.cancel_event.is_set():
            raise ConnectorProtocolError(
                ConnectorErrorCode.CANCELLED,
                f"sandbox execution was cancelled {stage}",
            )


def _normalize_explore_domain(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("Explore domain must be a string")
    domain = raw.strip().rstrip(".").lower()
    if not domain or len(domain) > 253:
        raise ValueError("Explore domain is invalid")
    try:
        domain = domain.encode("idna").decode("ascii")
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    except UnicodeError as exc:
        raise ValueError("Explore domain is invalid") from exc
    else:
        raise ValueError("Explore domain must not be an IP address")
    labels = domain.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ValueError("Explore domain is invalid")
    return domain


def _initial_explore_allowed_domains(entry_url: str, allowed_domains: Sequence[str]) -> tuple[str, ...]:
    """Include the entry host plus its www/non-www redirect alias once."""
    try:
        parsed = urlsplit(entry_url)
    except ValueError as exc:
        raise ValueError("Explore entry URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Explore entry URL must be public HTTP(S)")
    entry_host = _normalize_explore_domain(parsed.hostname)
    values = [entry_host]
    alias = entry_host[4:] if entry_host.startswith("www.") else f"www.{entry_host}"
    try:
        alias = _normalize_explore_domain(alias)
    except ValueError:
        alias = ""
    if alias and alias not in values:
        values.append(alias)
    for value in allowed_domains:
        normalized = _normalize_explore_domain(value)
        if normalized not in values:
            raise ValueError(
                "initial Explore allowlist may contain only the entry host and its www/non-www alias"
            )
    return tuple(values)


def _bound_explore_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep host audit records bounded even if a broker implementation regresses."""
    def bound(item: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "[truncated]"
        if isinstance(item, str):
            return item[:32_768]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, Mapping):
            return {
                str(key)[:128]: bound(child, depth + 1)
                for key, child in list(item.items())[:200]
                if isinstance(key, str)
            }
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            return [bound(child, depth + 1) for child in item[:200]]
        return None
    projected = bound(value)
    return projected if isinstance(projected, dict) else {}


def _write_private_text(path: Path, text: str) -> None:
    """Atomically replace a read-only file in a host-owned Explore source directory."""
    encoded = text.encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        pending = memoryview(encoded)
        while pending:
            pending = pending[os.write(descriptor, pending):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    path.chmod(0o444)


def _remove_explore_resource_root(root: Path | None) -> bool:
    if root is None:
        return True
    try:
        if not root.is_absolute() or not root.name.startswith("osnews-explore-broker-"):
            return False
        shutil.rmtree(root)
        return True
    except OSError:
        logger.exception("Explore broker host resource cleanup failed root=%s", root)
        return False


_DYNAMIC_EXPLORE_PROXY_SOURCE = r'''
import asyncio
import json
import os
from contextlib import suppress

from app.discovery.sandbox.egress_proxy import MAX_HEADER_BYTES, handle_client


def _domains():
    with open(os.environ["SANDBOX_ALLOWED_DOMAINS_FILE"], "r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, list) or not value or len(value) > 32 or not all(isinstance(item, str) for item in value):
        raise ValueError("host Explore allowlist is invalid")
    return tuple(value)


async def _serve():
    port = int(os.environ.get("SANDBOX_PROXY_PORT", "8080"))
    async def handler(reader, writer):
        try:
            await handle_client(reader, writer, _domains())
        except Exception:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
    server = await asyncio.start_server(handler, host="0.0.0.0", port=port, limit=MAX_HEADER_BYTES + 1)
    async with server:
        await server.serve_forever()


asyncio.run(_serve())
'''.strip()


_EXPLORE_BROKER_SOURCE = r'''
import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx


MAX_BODY = 2 * 1024 * 1024
MAX_TEXT = 16_000
MAX_LINKS = 100
MAX_NETWORK = 50
MAX_ACTIONS = 400
MAX_PAGES = 8
MAX_RECORDS = 20
MAX_RECORD_TEXT = 1000
MAX_RECORD_LINK_CHARS = 512
PAGE_ID = re.compile(r"^page_[0-9a-f]{24}$")
SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")
SAFE_BASH = {"awk", "basename", "cat", "cut", "dirname", "find", "grep", "head", "jq", "ls", "printf", "sed", "sha256sum", "sort", "tail", "tr", "uniq", "wc"}


def _domains():
    with open(os.environ["SANDBOX_ALLOWED_DOMAINS_FILE"], "r", encoding="utf-8") as source:
        result = json.load(source)
    if not isinstance(result, list) or not result or len(result) > 32 or not all(isinstance(item, str) for item in result):
        raise ValueError("host Explore allowlist is invalid")
    return tuple(item.lower().rstrip(".") for item in result)


def _url(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or value != value.strip():
        raise ValueError("URL is invalid")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL must be public HTTP(S)")
    if parsed.hostname.lower().rstrip(".") not in _domains():
        raise ValueError("URL host is outside the current host allowlist")
    return value


def _read_body(response):
    digest, chunks, size, truncated = hashlib.sha256(), [], 0, False
    for chunk in response.iter_bytes():
        remain = MAX_BODY - size
        if remain <= 0:
            truncated = True
            break
        accepted = chunk[:remain]
        digest.update(accepted)
        chunks.append(accepted)
        size += len(accepted)
        if len(accepted) != len(chunk):
            truncated = True
            break
    return b"".join(chunks), size, digest.hexdigest(), truncated


_NON_VISIBLE_TAGS = {"script", "style", "template", "noscript", "svg", "canvas", "iframe", "object", "embed", "form", "input", "button", "select", "textarea"}


def _clean_text(values, limit=MAX_TEXT):
    """Return a compact, human-visible text sample from parser text nodes."""
    result = " ".join(" ".join(str(value).split()) for value in values if str(value).strip())
    return result[:limit]


def _drop_non_visible(root):
    for node in root.xpath(".//*"):
        tag = node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""
        if tag in _NON_VISIBLE_TAGS or str(node.get("aria-hidden") or "").lower() == "true":
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)


def _append_link(links, value, base):
    value = urljoin(base, str(value).strip())
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password and value not in links:
        links.append(value[:2048])


def _html_document(body, base):
    from lxml import html
    doc = html.fromstring(body)
    title = _clean_text(doc.xpath("//title[1]//text()"), limit=1000)
    _drop_non_visible(doc)
    headings = _clean_text(doc.xpath("//h1[1]//text()"), limit=1000)
    articles = doc.xpath("//article")
    if len(articles) == 1:
        candidates = articles
    else:
        candidates = doc.xpath("//main | //*[@role='main'] | //body")
    text = ""
    for candidate in candidates:
        text = _clean_text(candidate.itertext())
        if text:
            break
    if not text:
        text = _clean_text(doc.itertext())
    if headings and headings not in text:
        text = (headings + " " + text)[:MAX_TEXT]
    links = []
    for href in doc.xpath("//a[@href]/@href")[:MAX_LINKS * 3]:
        _append_link(links, href, base)
        if len(links) >= MAX_LINKS:
            break
    return title, text, links


def _xml_document(body, base):
    from lxml import etree
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(body, parser=parser)
    if root is None:
        return "", "", []
    _drop_non_visible(root)
    title_nodes = root.xpath("//*[local-name()='channel' or local-name()='feed']/*[local-name()='title'][1]")
    title = _clean_text(title_nodes[0].itertext(), limit=1000) if title_nodes else _clean_text(root.xpath("//*[local-name()='title'][1]//text()"), limit=1000)
    text = _clean_text(root.itertext())
    links = []
    for href in root.xpath("//@href") + root.xpath("//*[local-name()='link']/text()"):
        _append_link(links, href, base)
        if len(links) >= MAX_LINKS:
            break
    return title, text, links


def _document(body, base, content_type=""):
    """Project a bounded visible document sample without site-specific parsing."""
    try:
        normalized_type = str(content_type).lower()
        stripped = body.lstrip()
        looks_like_xml = (
            "xml" in normalized_type
            or "rss" in normalized_type
            or "atom" in normalized_type
            or stripped.startswith((b"<?xml", b"<rss", b"<feed"))
        )
        if looks_like_xml:
            return _xml_document(body, base)
        return _html_document(body, base)
    except Exception:
        return "", "", []


class Broker:
    def __init__(self):
        self.client = httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=False, headers={"User-Agent": "os-news-tracker/explore-broker"})
        self.pages, self.network, self.count = {}, {}, 0
        self.playwright = self.browser = None
        self.workspace = Path("/workspace/session")
        self.workspace.mkdir(parents=True, exist_ok=True)

    async def close(self):
        self.client.close()
        if self.browser is not None:
            await self.browser.close()
        if self.playwright is not None:
            await self.playwright.stop()

    async def act(self, action):
        self.count += 1
        if self.count > MAX_ACTIONS:
            raise ValueError("Explore session action limit reached")
        tool = action.get("tool")
        if tool == "http": return await self.http(action)
        if tool == "browser": return await self.browser_action(action)
        if tool == "workspace": return await self.workspace_action(action)
        raise ValueError("unsupported fixed Explore tool")

    async def http(self, action):
        method, current = action.get("method"), _url(action.get("url"))
        if method not in {"GET", "HEAD", "POST"}: raise ValueError("HTTP method is invalid")
        request_args = {"headers": action.get("headers") or {}}
        if "body" in action: request_args["content"] = action["body"].encode("utf-8")
        if "json" in action: request_args["json"] = action["json"]
        redirect_target = None
        for _ in range(6):
            request = self.client.build_request(method, current, **request_args)
            response = self.client.send(request, stream=True)
            try:
                final = str(response.url)
                _url(final)
                if 300 <= response.status_code < 400 and response.headers.get("location"):
                    redirect_target = urljoin(final, response.headers["location"])
                    try:
                        current = _url(redirect_target)
                    except ValueError:
                        return {"requested_url": action["url"], "final_url": final, "status": response.status_code, "redirect_target": redirect_target[:4096], "headers": {"location": response.headers["location"][:2048]}, "links": []}
                    continue
                body, size, digest, truncated = (b"", 0, hashlib.sha256(b"").hexdigest(), False) if method == "HEAD" else _read_body(response)
                content_type = response.headers.get("content-type", "")[:300]
                title, text, links = _document(body, final, content_type)
                capture = self.workspace / f"http-{self.count:04d}.body"
                capture.write_bytes(body)
                return {"requested_url": action["url"], "final_url": final, "status": response.status_code, "content_type": content_type, "title": title, "text_excerpt": text, "links": links, "body_size": size, "body_sha256": digest, "body_truncated": truncated, "workspace_path": str(capture.relative_to(self.workspace))}
            finally:
                response.close()
        raise ValueError("redirect limit exceeded")

    async def _launch(self):
        if self.browser is not None: return
        from playwright.async_api import async_playwright
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if not proxy: raise RuntimeError("browser proxy is unavailable")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True, proxy={"server": proxy})

    async def _page(self, page_id):
        if not isinstance(page_id, str) or not PAGE_ID.fullmatch(page_id) or page_id not in self.pages:
            raise ValueError("unknown broker page_id")
        return self.pages[page_id]

    async def _snapshot(self, page_id, page):
        links = await page.locator("a[href]").evaluate_all("els => els.slice(0, 200).map(e => e.href)")
        text = (await page.locator("body").inner_text())[:MAX_TEXT] if await page.locator("body").count() else ""
        return {"page_id": page_id, "final_url": page.url[:4096], "title": (await page.title())[:1000], "text_excerpt": text, "links": [str(v)[:2048] for v in links if isinstance(v, str)][:MAX_LINKS], "network": self.network.get(page_id, [])[-MAX_NETWORK:]}

    async def _record_elements(self, page, action):
        limit = min(int(action.get("limit", MAX_RECORDS)), MAX_RECORDS); loc = page.locator(action["selector"]); count = min(await loc.count(), limit)
        records = []
        for i in range(count):
            element = loc.nth(i)
            record = await element.evaluate("""element => {
                const read = node => {
                    const own = node.href || node.getAttribute('href') || '';
                    const nested = Array.from(node.querySelectorAll('a[href]'))
                        .slice(0, 4).map(link => link.href || link.getAttribute('href') || '');
                    return {
                        text: (node.innerText || '').trim(),
                        links: [...new Set([own, ...nested].filter(Boolean))],
                        html: node.outerHTML || '',
                    };
                };
                const direct = read(element);
                if (element.tagName !== 'A' && direct.text.length >= 160) return direct;
                let parent = element.parentElement;
                for (let depth = 0; parent && depth < 6; depth += 1, parent = parent.parentElement) {
                    const contextual = read(parent);
                    if (
                        contextual.text.length > direct.text.length
                        && contextual.text.length <= 1600
                        && contextual.links.length <= 6
                    ) return contextual;
                }
                return direct;
            }""")
            records.append({
                "text": str(record.get("text") or "")[:MAX_RECORD_TEXT],
                "html": str(record.get("html") or "")[:2_000],
                "links": list(dict.fromkeys(
                    str(value)[:MAX_RECORD_LINK_CHARS]
                    for value in record.get("links", [])
                    if isinstance(value, str) and value
                ))[:4],
            })
        return records

    async def browser_action(self, action):
        op = action.get("operation")
        if op == "open":
            if len(self.pages) >= MAX_PAGES: raise ValueError("Explore browser page limit reached")
            await self._launch(); page_id = "page_" + secrets.token_hex(12); page = await self.browser.new_page()
            self.pages[page_id], self.network[page_id] = page, []
            page.on("request", lambda req: self.network[page_id].append({"url": req.url[:2048], "method": req.method[:16]}))
            page.on("response", lambda res: self.network[page_id].append({"url": res.url[:2048], "status": res.status}))
            async def route(route):
                try: _url(route.request.url)
                except Exception: await route.abort()
                else: await route.continue_()
            await page.route("**/*", route)
            try:
                response = await page.goto(_url(action.get("url")), wait_until="domcontentloaded", timeout=20_000)
                result = await self._snapshot(page_id, page); result["requested_url"] = action["url"]; result["status"] = response.status if response else None
                return result
            except Exception:
                self.pages.pop(page_id, None); self.network.pop(page_id, None); await page.close()
                raise
        if op == "records" and "url" in action:
            await self._launch(); page = await self.browser.new_page()
            try:
                async def route(route):
                    try: _url(route.request.url)
                    except Exception: await route.abort()
                    else: await route.continue_()
                await page.route("**/*", route)
                response = await page.goto(_url(action["url"]), wait_until="domcontentloaded", timeout=20_000)
                return {
                    "requested_url": action["url"], "final_url": page.url[:4096],
                    "status": response.status if response else None,
                    "title": (await page.title())[:1000],
                    "records": await self._record_elements(page, action),
                }
            finally:
                await page.close()
        page = await self._page(action.get("page_id")); page_id = action["page_id"]
        if op == "click": await page.locator(action["selector"]).first.click(timeout=12_000)
        elif op == "fill": await page.locator(action["selector"]).first.fill(action["value"], timeout=12_000)
        elif op == "press": await page.locator(action["selector"]).first.press(action["key"], timeout=12_000)
        elif op == "scroll": await page.mouse.wheel(action.get("x", 0), action.get("y", 0))
        elif op == "text":
            text = (await page.locator(action["selector"]).first.inner_text(timeout=12_000))[:MAX_TEXT]
            cleaned_text = " ".join(text.split())
            return {
                "page_id": page_id,
                "text": text,
                "source_selector": action["selector"][:1024],
                "evidence_role": action.get("evidence_role"),
                "cleaned_text_chars": len(cleaned_text),
            }
        elif op == "attribute":
            text = await page.locator(action["selector"]).first.get_attribute(
                action["attribute"], timeout=12_000
            )
            if text is None:
                raise ValueError("browser attribute is missing")
            cleaned_text = " ".join(text.split())
            return {
                "page_id": page_id,
                "text": text[:MAX_TEXT],
                "source_selector": action["selector"][:1024],
                "source_attribute": action["attribute"][:128],
                "evidence_role": action.get("evidence_role"),
                "cleaned_text_chars": len(cleaned_text),
            }
        elif op == "query":
            limit = int(action.get("limit", 50)); loc = page.locator(action["selector"]); count = min(await loc.count(), limit)
            return {"page_id": page_id, "matches": [(await loc.nth(i).inner_text(timeout=12_000))[:2000] for i in range(count)]}
        elif op == "records":
            return {"page_id": page_id, "records": await self._record_elements(page, action)}
        elif op == "links":
            limit = int(action.get("limit", 50)); values = await page.locator(action["selector"]).evaluate_all(f"els => els.slice(0, {limit}).map(e => e.href || e.getAttribute('href') || '')")
            return {"page_id": page_id, "links": [str(v)[:2048] for v in values if isinstance(v, str)]}
        elif op == "network": return {"page_id": page_id, "network": self.network.get(page_id, [])[-int(action.get("limit", 50)): ]}
        else: raise ValueError("browser operation is invalid")
        return await self._snapshot(page_id, page)

    async def workspace_action(self, action):
        op = action.get("operation")
        path = action.get("path", "")
        if op == "bash":
            command = action.get("command")
            if not isinstance(command, str) or len(command) > 2048 or any(token in command for token in (";", "|", "&", "`", "$", "<", ">")):
                raise ValueError("workspace command is invalid")
            args = shlex.split(command)
            if not args or args[0] not in SAFE_BASH: raise ValueError("workspace command is not fixed-tool safe")
            process = await asyncio.create_subprocess_exec(*args, cwd=self.workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try: stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
            except asyncio.TimeoutError: process.kill(); await process.wait(); raise ValueError("workspace command timed out")
            return {"returncode": process.returncode, "stdout": stdout.decode("utf-8", "replace")[:MAX_TEXT], "stderr": stderr.decode("utf-8", "replace")[:4000]}
        if not isinstance(path, str) or not SAFE_PATH.fullmatch(path) or path.startswith("../"): raise ValueError("workspace path is invalid")
        target = (self.workspace / path).resolve()
        if self.workspace not in target.parents and target != self.workspace: raise ValueError("workspace path escaped")
        if op == "write":
            content = action.get("content")
            if not isinstance(content, str) or len(content.encode("utf-8")) > 256 * 1024: raise ValueError("workspace write content is invalid")
            target.parent.mkdir(parents=True, exist_ok=True); target.write_text(content, encoding="utf-8")
            return {"path": path, "bytes": len(content.encode("utf-8"))}
        if op == "read":
            return {"path": path, "text": target.read_text(encoding="utf-8", errors="replace")[:MAX_TEXT]}
        if op == "list":
            return {"path": path, "entries": [entry.name for entry in list(target.iterdir())[:200]]}
        raise ValueError("workspace operation is invalid")


async def main():
    broker = Broker()
    print(json.dumps({"type": "ready", "protocol": 1}), flush=True)
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not line: break
            try:
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("type") != "action" or message.get("protocol") != 1 or not isinstance(message.get("request_id"), str) or not isinstance(message.get("action"), dict):
                    raise ValueError("broker protocol request is invalid")
                action = message["action"]
                result = await broker.act(action)
                payload = {"type": "action_result", "request_id": message["request_id"], "tool": action.get("tool"), "operation": action.get("operation") or action.get("method"), "observation": result, "error": None}
            except Exception as exc:
                action = message.get("action") if isinstance(locals().get("message"), dict) else {}
                payload = {"type": "action_result", "request_id": message.get("request_id") if isinstance(locals().get("message"), dict) else "", "tool": action.get("tool") if isinstance(action, dict) else None, "operation": (action.get("operation") or action.get("method")) if isinstance(action, dict) else None, "observation": {}, "error": f"{type(exc).__name__}: {str(exc)[:1000]}"}
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)
    finally:
        await broker.close()


asyncio.run(main())
'''.strip()


@lru_cache
def get_formal_sandbox_runtime() -> SandboxRuntime:
    """One process-local controller sharing the application capacity queue."""
    return SandboxRuntime()
