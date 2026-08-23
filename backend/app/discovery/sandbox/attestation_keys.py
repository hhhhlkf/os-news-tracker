"""Persistent host-only keyring for sandbox runtime attestations."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import uuid
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings


KEYRING_SCHEMA_VERSION = 1
MINIMUM_KEY_BYTES = 32


def sign_host_attestation(
    document: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> tuple[str, str]:
    settings = settings or get_settings()
    keyring = load_or_create_attestation_keyring(settings=settings)
    key_id = keyring["active_key_id"]
    key = _decode_key(keyring["keys"][key_id])
    return key_id, hmac.new(key, _canonical(document), hashlib.sha256).hexdigest()


def verify_host_attestation(
    document: dict[str, Any],
    *,
    key_id: str,
    proof: str,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    try:
        keyring = load_attestation_keyring(settings=settings)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("sandbox attestation keyring cannot be read safely") from exc
    encoded = keyring["keys"].get(key_id)
    if encoded is None:
        raise ValueError("sandbox attestation signing key is not retained")
    expected = hmac.new(_decode_key(encoded), _canonical(document), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, proof):
        raise ValueError("sandbox runtime attestation host proof is invalid")


def rotate_attestation_keyring(*, settings: Settings | None = None) -> str:
    """Atomically activate a new key while retaining every historical verifier."""
    settings = settings or get_settings()
    path, directory = _validated_location(settings, create_parent=True)
    with _keyring_lock(path, directory):
        keyring = _load_existing(path)
        key_id, encoded = _new_key()
        keyring["keys"][key_id] = encoded
        keyring["active_key_id"] = key_id
        _atomic_replace(path, directory, keyring)
        return key_id


def load_or_create_attestation_keyring(*, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    path, directory = _validated_location(settings, create_parent=True)
    with _keyring_lock(path, directory):
        if path.exists():
            return _load_existing(path)
        key_id, encoded = _new_key()
        document = {
            "schema_version": KEYRING_SCHEMA_VERSION,
            "active_key_id": key_id,
            "keys": {key_id: encoded},
        }
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            _write_all(descriptor, _serialize(document))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)
        _fsync_directory(directory)
        return _load_existing(path)


def load_attestation_keyring(*, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    path, _directory = _validated_location(settings, create_parent=False)
    if not path.exists():
        raise ValueError("sandbox attestation keyring is missing")
    return _load_existing(path)


def _validated_location(settings: Settings, *, create_parent: bool) -> tuple[Path, Path]:
    path = Path(settings.discovery_sandbox_attestation_keyring_path)
    if not path.is_absolute() or path.name != "keyring.json":
        raise ValueError("sandbox attestation keyring path must be an absolute keyring.json path")
    if (
        path.is_relative_to(Path("/tmp"))
        and not settings.discovery_sandbox_allow_ephemeral_attestation_keyring
    ):
        raise ValueError("ephemeral sandbox attestation keyring paths require explicit local/test opt-in")
    directory = path.parent
    if create_parent:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    current = Path(path.anchor)
    for component in directory.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ValueError("sandbox attestation keyring path contains a symlink")
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("sandbox attestation keyring directory is unavailable")
    directory_mode = stat.S_IMODE(directory.stat().st_mode)
    if directory_mode & 0o077:
        raise ValueError("sandbox attestation keyring directory permissions must be 0700 or stricter")
    if path.is_symlink():
        raise ValueError("sandbox attestation keyring must not be a symlink")
    return path, directory


class _keyring_lock:
    def __init__(self, path: Path, directory: Path) -> None:
        self.path = path.with_name(".keyring.lock")
        self.directory = directory
        self.descriptor: int | None = None

    def __enter__(self) -> None:
        self.descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        metadata = os.fstat(self.descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            os.close(self.descriptor)
            self.descriptor = None
            raise ValueError("sandbox attestation keyring lock permissions are unsafe")
        os.fchmod(self.descriptor, 0o600)
        os.fsync(self.descriptor)
        _fsync_directory(self.directory)
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)


def _load_existing(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > 64 * 1024
        ):
            raise ValueError("sandbox attestation keyring file permissions or size are invalid")
        chunks = bytearray()
        while chunk := os.read(descriptor, min(16 * 1024, metadata.st_size + 1 - len(chunks))):
            chunks.extend(chunk)
            if len(chunks) > metadata.st_size:
                raise ValueError("sandbox attestation keyring changed while reading")
        raw = bytes(chunks)
        if len(raw) != metadata.st_size:
            raise ValueError("sandbox attestation keyring changed while reading")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("sandbox attestation keyring JSON is invalid") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "active_key_id", "keys"}
        or document.get("schema_version") != KEYRING_SCHEMA_VERSION
        or not isinstance(document.get("active_key_id"), str)
        or not isinstance(document.get("keys"), dict)
        or document["active_key_id"] not in document["keys"]
        or not document["keys"]
    ):
        raise ValueError("sandbox attestation keyring schema is invalid")
    for key_id, encoded in document["keys"].items():
        key = _decode_key(encoded)
        if key_id != _key_id(key):
            raise ValueError("sandbox attestation key identifier is invalid")
    return document


def _new_key() -> tuple[str, str]:
    key = secrets.token_bytes(32)
    return _key_id(key), base64.b64encode(key).decode("ascii")


def _key_id(key: bytes) -> str:
    return hashlib.sha256(b"osnews-attestation-key-id-v1\0" + key).hexdigest()[:32]


def _decode_key(encoded: Any) -> bytes:
    if not isinstance(encoded, str):
        raise ValueError("sandbox attestation key encoding is invalid")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("sandbox attestation key encoding is invalid") from exc
    if len(key) < MINIMUM_KEY_BYTES:
        raise ValueError("sandbox attestation key is too weak")
    return key


def _atomic_replace(path: Path, directory: Path, document: dict[str, Any]) -> None:
    temporary = directory / f".keyring-{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            _write_all(descriptor, _serialize(document))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(directory)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _serialize(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_all(descriptor: int, content: bytes) -> None:
    pending = memoryview(content)
    while pending:
        pending = pending[os.write(descriptor, pending):]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
