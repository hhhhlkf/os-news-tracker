"""Atomic and resumable Discovery Loop checkpoints under a dedicated host root."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.discovery.redaction import redact_discovery_data, validate_discovery_data_bounds
from app.models import SiteDiscoveryRun


class DiscoveryCheckpoint(BaseModel):
    """Durable state saved after one deterministic write/execute/repair round."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_version: Literal[1] = 1
    run_id: int = Field(gt=0)
    phase: str = Field(min_length=1, max_length=40)
    round: int = Field(ge=0)
    connector_draft_path: str | None = None
    manifest_path: str | None = None
    rag_references: list[dict[str, Any]] = Field(default_factory=list)
    tool_evidence: list[dict[str, Any]] = Field(default_factory=list)
    processing_summary: dict[str, Any] = Field(default_factory=dict)
    execution_result: dict[str, Any] | None = None
    evaluation_result: dict[str, Any] | None = None
    code_diff: str | None = None
    error: dict[str, Any] | str | None = None
    elapsed_seconds: float = Field(default=0.0, ge=0)
    token_usage: int = Field(default=0, ge=0)
    runtime_version: str | None = Field(default=None, max_length=200)
    saved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("connector_draft_path", "manifest_path")
    @classmethod
    def _reject_unsafe_artifact_paths(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if (
            not value.strip()
            or path.is_absolute()
            or PureWindowsPath(value).is_absolute()
            or "\\" in value
            or ".." in path.parts
            or "\x00" in value
        ):
            raise ValueError("checkpoint artifact path contains traversal")
        return value


class CheckpointStore:
    """Writes bounded JSON atomically and rejects every path outside its root."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        configured_root = Path(self.settings.discovery_checkpoint_root)
        if not configured_root.is_absolute():
            raise ValueError("discovery_checkpoint_root must be absolute")
        root_descriptor = self._open_absolute_directory(configured_root, create=True)
        os.close(root_descriptor)
        self.root = configured_root
        configured_workspace_root = Path(self.settings.discovery_workspace_root)
        if not configured_workspace_root.is_absolute():
            raise ValueError("discovery_workspace_root must be absolute")
        workspace_descriptor = self._open_absolute_directory(configured_workspace_root, create=True)
        os.close(workspace_descriptor)
        self.workspace_root = configured_workspace_root

    def save(
        self,
        checkpoint: DiscoveryCheckpoint,
        *,
        session: Session | None = None,
    ) -> Path:
        """Write an immutable file through no-follow dirfds and flush its relative DB key."""
        raw_document = checkpoint.model_dump(mode="json")
        validate_discovery_data_bounds(
            raw_document,
            max_string_chars=self.settings.discovery_checkpoint_max_bytes,
            max_approx_bytes=self.settings.discovery_checkpoint_max_bytes,
        )
        redacted = DiscoveryCheckpoint.model_validate(
            redact_discovery_data(raw_document)
        )
        for artifact_path in (redacted.connector_draft_path, redacted.manifest_path):
            if artifact_path is not None:
                self.resolve_workspace_path(redacted.run_id, artifact_path)
        document = redacted.model_dump_json(indent=2).encode("utf-8")
        if len(document) > self.settings.discovery_checkpoint_max_bytes:
            raise ValueError("Discovery checkpoint exceeds configured size limit")
        run = None
        if session is not None:
            run = session.get(SiteDiscoveryRun, redacted.run_id)
            if run is None:
                raise LookupError(f"Discovery run {redacted.run_id} does not exist")
        run_name = f"run-{redacted.run_id}"
        destination_name = f"round-{redacted.round:02d}-{uuid.uuid4().hex}.json"
        temporary_name = f".checkpoint-{uuid.uuid4().hex}.tmp"
        root_descriptor = self._open_absolute_directory(self.root, create=False)
        run_descriptor = -1
        try:
            try:
                os.mkdir(run_name, mode=0o700, dir_fd=root_descriptor)
                os.fsync(root_descriptor)
            except FileExistsError:
                pass
            run_descriptor = os.open(
                run_name,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=run_descriptor,
            )
            try:
                pending = memoryview(document)
                while pending:
                    written = os.write(descriptor, pending)
                    pending = pending[written:]
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o400)
            finally:
                os.close(descriptor)
            os.link(
                temporary_name,
                destination_name,
                src_dir_fd=run_descriptor,
                dst_dir_fd=run_descriptor,
                follow_symlinks=False,
            )
            os.unlink(temporary_name, dir_fd=run_descriptor)
            os.fsync(run_descriptor)
        except BaseException:
            if run_descriptor >= 0:
                try:
                    os.unlink(temporary_name, dir_fd=run_descriptor)
                except FileNotFoundError:
                    pass
            raise
        finally:
            if run_descriptor >= 0:
                os.close(run_descriptor)
            os.close(root_descriptor)
        checkpoint_key = Path(run_name) / destination_name
        if run is not None:
            changed = session.execute(
                update(SiteDiscoveryRun)
                .where(
                    SiteDiscoveryRun.id == redacted.run_id,
                    SiteDiscoveryRun.status.in_(("queued", "running", "repairing")),
                )
                .values(
                    phase=redacted.phase,
                    round=redacted.round,
                    checkpoint_path=checkpoint_key.as_posix(),
                    runtime_version=redacted.runtime_version,
                )
            )
            if changed.rowcount != 1:
                raise RuntimeError("Discovery run is no longer active for checkpoint persistence")
        return checkpoint_key

    def load(self, checkpoint_path: str | Path, *, expected_run_id: int | None = None) -> DiscoveryCheckpoint:
        """Load one bounded regular file after containment and schema validation."""
        run_name, filename = self._parse_checkpoint_key(checkpoint_path)
        root_descriptor = self._open_absolute_directory(self.root, create=False)
        run_descriptor = -1
        descriptor = -1
        try:
            run_descriptor = os.open(
                run_name,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
            descriptor = os.open(
                filename,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=run_descriptor,
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("checkpoint path must be a regular non-symlink file")
            if metadata.st_size > self.settings.discovery_checkpoint_max_bytes:
                raise ValueError("Discovery checkpoint exceeds configured size limit")
            chunks: list[bytes] = []
            remaining = self.settings.discovery_checkpoint_max_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if run_descriptor >= 0:
                os.close(run_descriptor)
            os.close(root_descriptor)
        if len(raw) > self.settings.discovery_checkpoint_max_bytes:
            raise ValueError("Discovery checkpoint exceeds configured size limit")
        decoded = json.loads(raw)
        validate_discovery_data_bounds(
            decoded,
            max_string_chars=self.settings.discovery_checkpoint_max_bytes,
            max_approx_bytes=self.settings.discovery_checkpoint_max_bytes,
        )
        checkpoint = DiscoveryCheckpoint.model_validate(decoded)
        if expected_run_id is not None and checkpoint.run_id != expected_run_id:
            raise ValueError("checkpoint run_id does not match the requested run")
        for artifact_path in (checkpoint.connector_draft_path, checkpoint.manifest_path):
            if artifact_path is not None:
                self.resolve_workspace_path(checkpoint.run_id, artifact_path)
        return checkpoint

    def resolve_workspace_path(self, run_id: int, relative_path: str) -> Path:
        """Resolve one run-relative artifact path without following any workspace symlink."""
        if run_id < 1:
            raise ValueError("workspace run_id must be positive")
        # Reuse the checkpoint schema's cross-platform relative-path validation.
        validated = DiscoveryCheckpoint._reject_unsafe_artifact_paths(relative_path)
        if validated is None:
            raise ValueError("workspace path is required")
        run_name = f"run-{run_id}"
        parts = Path(validated).parts
        root_descriptor = self._open_absolute_directory(self.workspace_root, create=False)
        current_descriptor = root_descriptor
        try:
            for index, part in enumerate((run_name, *parts)):
                try:
                    metadata = os.stat(part, dir_fd=current_descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    break
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError("workspace path contains a symlink")
                if index < len(parts) and not stat.S_ISDIR(metadata.st_mode):
                    raise ValueError("workspace path traverses a non-directory")
                if stat.S_ISDIR(metadata.st_mode):
                    next_descriptor = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=current_descriptor,
                    )
                    if current_descriptor != root_descriptor:
                        os.close(current_descriptor)
                    current_descriptor = next_descriptor
        finally:
            if current_descriptor != root_descriptor:
                os.close(current_descriptor)
            os.close(root_descriptor)
        return self.workspace_root / run_name / validated

    @staticmethod
    def _open_absolute_directory(path: Path, *, create: bool) -> int:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                try:
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=descriptor,
                    )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _parse_checkpoint_key(checkpoint_path: str | Path) -> tuple[str, str]:
        raw = str(checkpoint_path)
        path = Path(raw)
        if path.is_absolute() or PureWindowsPath(raw).is_absolute() or "\\" in raw:
            raise ValueError("checkpoint database key must be root-relative")
        if len(path.parts) != 2 or ".." in path.parts:
            raise ValueError("checkpoint database key has an invalid shape")
        run_name, filename = path.parts
        if not re.fullmatch(r"run-[1-9][0-9]*", run_name) or not re.fullmatch(
            r"round-[0-9]{2}-[0-9a-f]{32}\.json", filename
        ):
            raise ValueError("checkpoint database key has an invalid name")
        return run_name, filename


def cleanup_orphaned_checkpoints(session: Session) -> int:
    """Delete only immutable/temp checkpoint files absent from every committed DB pointer."""
    store = CheckpointStore()
    referenced = {
        value
        for value in session.scalars(
            select(SiteDiscoveryRun.checkpoint_path).where(
                SiteDiscoveryRun.checkpoint_path.is_not(None)
            )
        )
        if value
    }
    removed = 0
    root_descriptor = store._open_absolute_directory(store.root, create=False)
    try:
        for run_name in os.listdir(root_descriptor):
            if not re.fullmatch(r"run-[1-9][0-9]*", run_name):
                continue
            try:
                run_descriptor = os.open(
                    run_name,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=root_descriptor,
                )
            except OSError:
                continue
            try:
                for filename in os.listdir(run_descriptor):
                    is_checkpoint = re.fullmatch(
                        r"round-[0-9]{2}-[0-9a-f]{32}\.json", filename
                    )
                    is_temporary = re.fullmatch(r"\.checkpoint-[0-9a-f]{32}\.tmp", filename)
                    key = f"{run_name}/{filename}"
                    if not (is_checkpoint or is_temporary) or key in referenced:
                        continue
                    metadata = os.stat(filename, dir_fd=run_descriptor, follow_symlinks=False)
                    if stat.S_ISREG(metadata.st_mode):
                        os.unlink(filename, dir_fd=run_descriptor)
                        removed += 1
                if removed:
                    os.fsync(run_descriptor)
            finally:
                os.close(run_descriptor)
    finally:
        os.close(root_descriptor)
    return removed


def save_discovery_checkpoint(checkpoint: DiscoveryCheckpoint) -> Path:
    """Persist both the atomic checkpoint file and its run pointer in one service call."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        path = CheckpointStore().save(checkpoint, session=db)
        db.commit()
        return path
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
