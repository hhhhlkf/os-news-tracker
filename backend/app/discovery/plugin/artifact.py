"""Connector artifact loading, path validation, integrity checks and signing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from app.discovery.plugin.contracts import ConnectorManifest
from app.discovery.plugin.errors import ConnectorErrorCode, ConnectorProtocolError


MANIFEST_FILENAME = "manifest.json"
CONNECTOR_FILENAME = "crawler.py"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_CONNECTOR_BYTES = 2 * 1024 * 1024
HASH_CHUNK_BYTES = 64 * 1024
_CONNECTOR_KEY = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


@dataclass(frozen=True)
class ConnectorArtifact:
    """A validated, immutable connector version on the local filesystem."""

    directory: Path
    manifest_path: Path
    connector_path: Path
    manifest: ConnectorManifest
    signature: str


def compute_connector_checksum(content: bytes | Path) -> str:
    """Return the canonical SHA-256 digest for connector source bytes."""
    digest = hashlib.sha256()
    if isinstance(content, Path):
        total_bytes = 0
        with content.open("rb") as source:
            while chunk := source.read(HASH_CHUNK_BYTES):
                total_bytes += len(chunk)
                if total_bytes > MAX_CONNECTOR_BYTES:
                    raise _integrity_error(
                        "crawler.py grew beyond the artifact size limit while hashing",
                        maximum_bytes=str(MAX_CONNECTOR_BYTES),
                    )
                digest.update(chunk)
    else:
        digest.update(content)
    return digest.hexdigest()


def compute_connector_signature(manifest: ConnectorManifest) -> str:
    """Bind a normalized Manifest, including its source checksum, to one signature."""
    document = manifest.model_dump(mode="json")
    # Publication is the immutable legacy default.  Omitting its redundant
    # marker preserves pre-existing signatures; the only non-default meaning,
    # snapshot, is always included and therefore signature-bound.
    if manifest.time_semantics == "publication":
        document.pop("time_semantics", None)
    normalized = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def load_connector_artifact(
    connector_root: str | Path,
    *,
    kind: Literal["sites", "shared"],
    connector_key: str,
    version: int,
) -> ConnectorArtifact:
    """Resolve and verify one artifact below an explicitly trusted connector root."""
    if kind not in {"sites", "shared"}:
        raise _integrity_error("connector kind must be sites or shared")
    if not _CONNECTOR_KEY.fullmatch(connector_key):
        raise _integrity_error("connector_key is not a safe path segment")
    if version < 1:
        raise _integrity_error("connector version must be greater than zero")

    root_path = Path(connector_root)
    try:
        resolved_root = root_path.resolve(strict=True)
    except OSError as exc:
        raise _integrity_error("trusted connector root does not exist") from exc
    if not resolved_root.is_dir():
        raise _integrity_error("trusted connector root is not a directory")

    artifact_directory = resolved_root / kind / connector_key / f"v{version}"
    _reject_symlink_components(resolved_root, artifact_directory)
    try:
        resolved_directory = artifact_directory.resolve(strict=True)
    except OSError as exc:
        raise _integrity_error("connector artifact directory does not exist") from exc
    if not resolved_directory.is_dir() or not resolved_directory.is_relative_to(resolved_root):
        raise _integrity_error("connector artifact directory escapes the trusted connector root")

    manifest_path = resolved_directory / MANIFEST_FILENAME
    connector_path = resolved_directory / CONNECTOR_FILENAME
    for path in (manifest_path, connector_path):
        if path.is_symlink():
            raise _integrity_error(f"connector artifact file must not be a symlink: {path.name}")
        try:
            resolved_path = path.resolve(strict=True)
        except OSError as exc:
            raise _integrity_error(f"connector artifact file is missing: {path.name}") from exc
        if resolved_path.parent != resolved_directory or not resolved_path.is_file():
            raise _integrity_error(f"connector artifact path escapes its version directory: {path.name}")

    try:
        manifest_size = manifest_path.stat().st_size
        connector_size = connector_path.stat().st_size
    except OSError as exc:
        raise _integrity_error("connector artifact changed while validating file sizes") from exc
    if manifest_size > MAX_MANIFEST_BYTES:
        raise _integrity_error(
            "manifest.json exceeds the artifact size limit",
            maximum_bytes=str(MAX_MANIFEST_BYTES),
            actual_bytes=str(manifest_size),
        )
    if connector_size > MAX_CONNECTOR_BYTES:
        raise _integrity_error(
            "crawler.py exceeds the artifact size limit",
            maximum_bytes=str(MAX_CONNECTOR_BYTES),
            actual_bytes=str(connector_size),
        )

    try:
        with manifest_path.open("rb") as source:
            manifest_bytes = source.read(MAX_MANIFEST_BYTES + 1)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise _integrity_error("manifest.json grew beyond the artifact size limit while reading")
        raw_manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConnectorProtocolError(
            ConnectorErrorCode.INVALID_MANIFEST,
            "manifest.json is not valid UTF-8 JSON",
        ) from exc
    try:
        manifest = ConnectorManifest.model_validate(raw_manifest)
    except ValidationError as exc:
        raise ConnectorProtocolError(
            ConnectorErrorCode.INVALID_MANIFEST,
            "manifest.json does not match the connector contract",
            details={"validation_errors": exc.errors(include_url=False, include_input=False)},
        ) from exc

    if manifest.connector_key != connector_key or manifest.version != version:
        raise ConnectorProtocolError(
            ConnectorErrorCode.INVALID_MANIFEST,
            "Manifest connector_key/version does not match its artifact path",
            details={
                "path_connector_key": connector_key,
                "path_version": version,
                "manifest_connector_key": manifest.connector_key,
                "manifest_version": manifest.version,
            },
        )

    try:
        actual_checksum = compute_connector_checksum(connector_path)
    except ConnectorProtocolError:
        raise
    except OSError as exc:
        raise _integrity_error("crawler.py changed while computing its checksum") from exc
    if actual_checksum != manifest.checksum:
        raise _integrity_error(
            "crawler.py checksum does not match manifest.json",
            expected_checksum=manifest.checksum,
            actual_checksum=actual_checksum,
        )

    return ConnectorArtifact(
        directory=resolved_directory,
        manifest_path=manifest_path,
        connector_path=connector_path,
        manifest=manifest,
        signature=compute_connector_signature(manifest),
    )


def _integrity_error(message: str, **details: str) -> ConnectorProtocolError:
    return ConnectorProtocolError(
        ConnectorErrorCode.ARTIFACT_INTEGRITY_ERROR,
        message,
        details=details,
    )


def _reject_symlink_components(root: Path, artifact_directory: Path) -> None:
    """Reject symlinks at every artifact-owned level before resolving containment."""
    current = root
    for component in artifact_directory.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            raise _integrity_error(
                "connector artifact path contains a symlink",
                path=str(current),
            )
