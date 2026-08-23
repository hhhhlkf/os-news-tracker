"""Immutable trial and pending-review artifact handling for website Discovery."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.discovery.method_keys import crawl_method_domain_key_for_input_url
from app.discovery.method_keys import (
    crawl_method_domain_key,
    is_feed_recipe,
    normalized_feed_domain_key,
)
from app.discovery.plugin.artifact import (
    CONNECTOR_FILENAME,
    MANIFEST_FILENAME,
    ConnectorArtifact,
    compute_connector_checksum,
    compute_connector_signature,
    load_connector_artifact,
)
from app.discovery.plugin.contracts import ConnectorManifest
from app.discovery.plugin.review import attach_plugin_review_evidence
from app.discovery.quality_audit import SourceQualityAudit
from app.enums import SourceType, Stream
from app.models import CrawlMethod, CrawlMethodDomain, Source


REVIEW_PENDING = "pending"


def connector_key_for_url(site_url: str) -> str:
    host = (urlsplit(site_url).hostname or "site").lower()
    stem = re.sub(r"[^a-z0-9]+", "_", host).strip("_")[:45]
    if len(stem) < 2:
        stem = f"site_{stem or 'web'}"
    return f"{stem}_{hashlib.sha256(host.encode()).hexdigest()[:8]}"[:64]


def write_trial_artifact(
    *,
    run_id: int,
    site_url: str,
    source: str,
    allowed_domains: list[str],
    runtime_version: str,
    connector_key: str | None = None,
    version: int = 1,
    settings: Settings | None = None,
) -> ConnectorArtifact:
    settings = settings or get_settings()
    key = connector_key or connector_key_for_url(site_url)
    workspace_root = _trusted_absolute_root(Path(settings.discovery_workspace_root))
    run_root = workspace_root / f"run-{run_id}"
    if run_root.exists() and run_root.is_symlink():
        raise ValueError("Discovery workspace run directory must not be a symlink")
    root = run_root / "trial"
    version_dir = root / "sites" / key / f"v{version}"
    _replace_private_directory(version_dir)
    source_bytes = source.encode("utf-8")
    manifest = ConnectorManifest(
        connector_key=key,
        version=version,
        entry=site_url,
        runtime_version=runtime_version,
        checksum=compute_connector_checksum(source_bytes),
        allowed_domains=tuple(allowed_domains),
    )
    _write_exclusive(version_dir / CONNECTOR_FILENAME, source_bytes)
    _write_exclusive(
        version_dir / MANIFEST_FILENAME,
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
    )
    return load_connector_artifact(root, kind="sites", connector_key=key, version=version)


def find_existing_method(site_url: str, session: Session) -> dict[str, object] | None:
    """Compatibility duplicate lookup without importing the legacy website workflow."""
    domain_key = crawl_method_domain_key_for_input_url(site_url)
    mapping = session.scalar(select(CrawlMethodDomain).where(CrawlMethodDomain.domain == domain_key))
    if mapping is None:
        return None
    method = session.get(CrawlMethod, mapping.method_id)
    if method is None:
        return None
    if domain_key != normalized_feed_domain_key(site_url) and is_feed_recipe(method.dsl_recipe):
        if crawl_method_domain_key(method.entry_url, method.dsl_recipe) != normalized_feed_domain_key(site_url):
            return None
    return {
        "method_id": method.id,
        "domain": method.domain,
        "signature": method.signature,
        "dsl_recipe": method.dsl_recipe,
        "last_run_at": method.last_run_at.isoformat() if method.last_run_at else None,
        "last_run_status": method.last_run_status,
    }


@dataclass(frozen=True)
class StagedConnectorArtifact:
    artifact: ConnectorArtifact
    root: Path
    staging_directory: Path
    final_directory: Path


def stage_pending_artifact(
    *,
    trial: ConnectorArtifact,
    settings: Settings | None = None,
) -> StagedConnectorArtifact:
    """Create a hidden, fsynced version; reserve its version under a process lock."""
    settings = settings or get_settings()
    root = _trusted_absolute_root(Path(settings.discovery_connector_root))
    key_root = root / "sites" / trial.manifest.connector_key
    key_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging_root = key_root / ".staging"
    staging_root.mkdir(mode=0o700, exist_ok=True)
    lock_path = key_root / ".version.lock"
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    staging_directory: Path | None = None
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        versions = [
            int(path.name[1:]) for path in key_root.iterdir()
            if path.is_dir() and re.fullmatch(r"v[1-9][0-9]*", path.name)
        ]
        for staged in staging_root.iterdir():
            if (
                staged.is_dir()
                and not staged.is_symlink()
                and re.fullmatch(r"[0-9a-f]{32}", staged.name)
                and staged.stat().st_mtime <= time.time() - 300.0
            ):
                shutil.rmtree(staged)
                continue
            manifest_path = staged / MANIFEST_FILENAME
            try:
                value = json.loads(manifest_path.read_text(encoding="utf-8")).get("version")
                if isinstance(value, int) and value > 0:
                    versions.append(value)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        version = max(versions, default=0) + 1
        staging_directory = staging_root / uuid.uuid4().hex
        staging_directory.mkdir(mode=0o700)
        final_directory = key_root / f"v{version}"
        source_bytes = trial.connector_path.read_bytes()
        manifest = trial.manifest.model_copy(update={
            "version": version,
            "checksum": compute_connector_checksum(source_bytes),
        })
        _write_exclusive(staging_directory / CONNECTOR_FILENAME, source_bytes)
        _write_exclusive(
            staging_directory / MANIFEST_FILENAME,
            json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
        )
        _fsync_directory(staging_directory)
        _fsync_directory(staging_root)
        # Validate while retaining the version lock. Startup orphan cleanup uses
        # the same lock and therefore cannot remove an actively-built stage.
        staged_root = staging_directory / "validation"
        formal_shape = staged_root / "sites" / manifest.connector_key / f"v{version}"
        formal_shape.mkdir(parents=True)
        for filename in (CONNECTOR_FILENAME, MANIFEST_FILENAME):
            os.link(staging_directory / filename, formal_shape / filename, follow_symlinks=False)
        artifact = load_connector_artifact(
            staged_root,
            kind="sites",
            connector_key=manifest.connector_key,
            version=version,
        )
        shutil.rmtree(staged_root)
        _fsync_directory(staging_directory)
        return StagedConnectorArtifact(
            artifact=ConnectorArtifact(
                directory=staging_directory,
                manifest_path=staging_directory / MANIFEST_FILENAME,
                connector_path=staging_directory / CONNECTOR_FILENAME,
                manifest=artifact.manifest,
                signature=artifact.signature,
            ),
            root=root,
            staging_directory=staging_directory,
            final_directory=final_directory,
        )
    except BaseException:
        if staging_directory is not None:
            shutil.rmtree(staging_directory, ignore_errors=True)
            _fsync_directory(staging_root)
        raise
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def create_packaging_method(
    session: Session,
    *,
    staged: StagedConnectorArtifact,
    display_name: str,
    force: bool,
    method_audit: dict[str, object],
    quality_audit: SourceQualityAudit,
    quality_trial_evidence: list[dict[str, object]],
) -> CrawlMethod:
    """Persist a non-runnable packaging record while the artifact remains hidden."""
    manifest = staged.artifact.manifest

    domain_key = crawl_method_domain_key_for_input_url(manifest.entry)
    mapping = session.scalar(select(CrawlMethodDomain).where(CrawlMethodDomain.domain == domain_key))
    if mapping is not None and not force:
        raise ValueError("a crawl method already exists for this website")
    source = Source(
        name=display_name[:200],
        type=SourceType.DISCOVERY.value,
        url=manifest.entry,
        main_category="OS跟踪来源",
        stream=Stream.NEWS.value,
        enabled=False,
    )
    session.add(source)
    session.flush()
    method = CrawlMethod(
        domain=domain_key,
        entry_url=manifest.entry,
        source_id=source.id,
        dsl_recipe=manifest.model_dump(mode="json"),
        signature=compute_connector_signature(manifest),
        status="packaging",
        review_status=REVIEW_PENDING,
    )
    session.add(method)
    attach_plugin_review_evidence(
        method,
        method_audit=method_audit,
        quality_audit=quality_audit,
        quality_trial_evidence=quality_trial_evidence,
    )
    session.flush()
    return method


def publish_staged_artifact(staged: StagedConnectorArtifact) -> ConnectorArtifact:
    """Atomically expose one unique version and fsync it before DB activation."""
    key_root = staged.final_directory.parent
    lock_descriptor = os.open(key_root / ".version.lock", os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        if staged.final_directory.exists():
            raise FileExistsError("reserved connector version already exists")
        os.rename(staged.staging_directory, staged.final_directory)
        for path in (staged.final_directory / CONNECTOR_FILENAME, staged.final_directory / MANIFEST_FILENAME):
            path.chmod(0o444)
        staged.final_directory.chmod(0o555)
        _fsync_directory(staged.final_directory)
        _fsync_directory(key_root)
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)
    return load_connector_artifact(
        staged.root,
        kind="sites",
        connector_key=staged.artifact.manifest.connector_key,
        version=staged.artifact.manifest.version,
    )


def activate_packaging_method(session: Session, method: CrawlMethod) -> None:
    """Expose a durable package to review, without making it executable."""
    changed = session.execute(
        update(CrawlMethod)
        .where(
            CrawlMethod.id == method.id,
            CrawlMethod.status == "packaging",
            CrawlMethod.review_status == REVIEW_PENDING,
        )
        .values(status="pending")
    )
    if changed.rowcount != 1:
        raise ValueError("method is not an inactive packaging record")
    session.expire(method)


def discard_staged_artifact(staged: StagedConnectorArtifact) -> None:
    """Remove only this invocation's UUID staging or unique final directory."""
    for path in (staged.staging_directory, staged.final_directory):
        if path.exists() and path.name in {staged.staging_directory.name, staged.final_directory.name}:
            if path == staged.staging_directory and not re.fullmatch(r"[0-9a-f]{32}", path.name):
                raise ValueError("refusing to remove an unowned staging path")
            shutil.rmtree(path)
            _fsync_directory(path.parent)


def remove_unreferenced_pending_site_artifact(
    session: Session,
    *,
    artifact: ConnectorArtifact,
) -> bool:
    """Delete only a never-approved, unreferenced site version.

    Approved versions and every shared connector are deliberately retained for
    rollback.  The caller must delete/commit the pending DB row first.
    """
    from app.discovery.plugin.review import plugin_artifact_is_referenced

    manifest = artifact.manifest
    if plugin_artifact_is_referenced(
        session,
        excluded_method_id=-1,
        connector_key=manifest.connector_key,
        version=manifest.version,
        kind="sites",
    ):
        return False
    key_root = artifact.directory.parent
    if artifact.directory.name != f"v{manifest.version}" or key_root.name != manifest.connector_key:
        raise ValueError("refusing to remove an artifact outside its canonical version path")
    descriptor = os.open(
        key_root / ".version.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if plugin_artifact_is_referenced(
            session,
            excluded_method_id=-1,
            connector_key=manifest.connector_key,
            version=manifest.version,
            kind="sites",
        ):
            return False
        current = load_connector_artifact(
            key_root.parents[1],
            kind="sites",
            connector_key=manifest.connector_key,
            version=manifest.version,
        )
        if current.signature != artifact.signature or current.directory != artifact.directory:
            raise ValueError("artifact changed before pending cleanup")
        artifact.directory.chmod(0o700)
        for path in artifact.directory.iterdir():
            if path.is_file() and not path.is_symlink():
                path.chmod(0o600)
        shutil.rmtree(artifact.directory)
        _fsync_directory(key_root)
        return True
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def discard_packaging_method(session: Session, method_id: int) -> None:
    method = session.get(CrawlMethod, method_id)
    if method is None:
        return
    if method.status != "packaging" or method.review_status != REVIEW_PENDING:
        raise ValueError("refusing to discard a method outside internal packaging")
    source_id = method.source_id
    session.query(CrawlMethodDomain).filter_by(method_id=method_id).delete()
    session.delete(method)
    session.flush()
    if session.scalar(select(CrawlMethod.id).where(CrawlMethod.source_id == source_id)) is None:
        source = session.get(Source, source_id)
        if source is not None:
            session.delete(source)
    session.flush()


def recover_stale_packaging_artifacts(session: Session, *, settings: Settings | None = None) -> int:
    """Compensate packaging records left by a process crash before final activation."""
    settings = settings or get_settings()
    root = _trusted_absolute_root(Path(settings.discovery_connector_root))
    methods = list(
        session.scalars(
            select(CrawlMethod).where(
                CrawlMethod.status == "packaging",
                CrawlMethod.review_status == REVIEW_PENDING,
            )
        )
    )
    recovered = 0
    for method in methods:
        try:
            manifest = ConnectorManifest.model_validate(method.dsl_recipe)
            key_root = root / "sites" / manifest.connector_key
            final = key_root / f"v{manifest.version}"
            if final.exists() and final.is_dir() and not final.is_symlink():
                shutil.rmtree(final)
            staging_root = key_root / ".staging"
            if staging_root.is_dir() and not staging_root.is_symlink():
                for candidate in staging_root.iterdir():
                    try:
                        staged_manifest = ConnectorManifest.model_validate_json(
                            (candidate / MANIFEST_FILENAME).read_text(encoding="utf-8")
                        )
                    except Exception:
                        continue
                    if (
                        staged_manifest.connector_key == manifest.connector_key
                        and staged_manifest.version == manifest.version
                        and re.fullmatch(r"[0-9a-f]{32}", candidate.name)
                    ):
                        shutil.rmtree(candidate)
                _fsync_directory(staging_root)
            if key_root.exists():
                _fsync_directory(key_root)
        finally:
            discard_packaging_method(session, method.id)
            recovered += 1
    recovered += _remove_orphan_staging_directories(root)
    recovered += _remove_unreferenced_final_site_artifacts(session, root)
    session.commit()
    return recovered


def _remove_unreferenced_final_site_artifacts(
    session: Session,
    root: Path,
    *,
    minimum_age_seconds: float = 300.0,
) -> int:
    """Reclaim validated final site versions left after deferred DB-first cleanup."""
    from app.discovery.plugin.review import plugin_artifact_is_referenced

    sites_root = root / "sites"
    if not sites_root.is_dir() or sites_root.is_symlink():
        return 0
    cutoff = time.time() - minimum_age_seconds
    removed = 0
    for key_root in sites_root.iterdir():
        if not key_root.is_dir() or key_root.is_symlink():
            continue
        for version_dir in key_root.iterdir():
            match = re.fullmatch(r"v([1-9][0-9]*)", version_dir.name)
            if match is None or not version_dir.is_dir() or version_dir.is_symlink():
                continue
            try:
                if version_dir.stat().st_mtime > cutoff:
                    continue
                artifact = load_connector_artifact(
                    root,
                    kind="sites",
                    connector_key=key_root.name,
                    version=int(match.group(1)),
                )
                manifest = artifact.manifest
                if plugin_artifact_is_referenced(
                    session,
                    excluded_method_id=-1,
                    connector_key=manifest.connector_key,
                    version=manifest.version,
                    kind="sites",
                ):
                    continue
                if remove_unreferenced_pending_site_artifact(session, artifact=artifact):
                    removed += 1
            except Exception:
                # Invalid or concurrently changing final versions are retained
                # for operator inspection; startup recovery must remain safe.
                continue
    return removed


def _remove_orphan_staging_directories(root: Path, *, minimum_age_seconds: float = 300.0) -> int:
    """Remove old UUID stages without DB provenance, never racing an active writer."""
    sites_root = root / "sites"
    if not sites_root.is_dir() or sites_root.is_symlink():
        return 0
    cutoff = time.time() - minimum_age_seconds
    removed = 0
    for key_root in sites_root.iterdir():
        staging_root = key_root / ".staging"
        if not key_root.is_dir() or key_root.is_symlink() or not staging_root.is_dir() or staging_root.is_symlink():
            continue
        lock_path = key_root / ".version.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            for candidate in staging_root.iterdir():
                if (
                    candidate.is_dir()
                    and not candidate.is_symlink()
                    and re.fullmatch(r"[0-9a-f]{32}", candidate.name)
                    and candidate.stat().st_mtime <= cutoff
                ):
                    shutil.rmtree(candidate)
                    removed += 1
            _fsync_directory(staging_root)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
    return removed


def _replace_private_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, mode=0o700)


def _write_exclusive(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        pending = memoryview(content)
        while pending:
            pending = pending[os.write(descriptor, pending):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _trusted_absolute_root(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("Discovery artifact roots must be absolute")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("Discovery artifact root contains a symlink")
    return path
