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
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable, Iterator, Literal

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
    purpose: Literal["primary_trial", "pagination", "url_verifier", "explore_tool", "formal", "other"]
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
        self._active_lock = Lock()
        self._retained_capacity: set[str] = set()

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
        if active is None:
            return False
        active.cancel_event.set()
        self.capacity_queue.cancel_waiting(job_id)
        with active.lock:
            container_name = active.container_name
        if container_name:
            self._docker("kill", container_name, check=False, timeout=5.0)
            self._cleanup_resource("container", container_name, ("rm", "-f", container_name))
        return True

    def queue_position(self, job_id: str) -> int | None:
        return self.capacity_queue.queue_position(job_id)

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
        """Aggregate trusted proxy denials into one bounded Agent observation event."""
        result = self._docker("logs", proxy_name, check=False, timeout=2.0)
        if result.returncode != 0:
            logger.warning("unable to read sandbox proxy denial evidence proxy=%s", proxy_name)
            return None
        counts: dict[tuple[str, int, str], int] = {}
        for raw_line in result.stdout.decode("utf-8", errors="replace").splitlines()[:200]:
            try:
                document = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(document, dict) or document.get("type") != "sandbox_egress_denied":
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
        if not counts:
            return None
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
        return [
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


@lru_cache
def get_formal_sandbox_runtime() -> SandboxRuntime:
    """One process-local controller sharing the application capacity queue."""
    return SandboxRuntime()
