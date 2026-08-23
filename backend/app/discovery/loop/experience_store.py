"""Only admission boundary for trusted Discovery RAG experience."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.discovery.plugin.artifact import ConnectorArtifact, load_connector_artifact
from app.discovery.plugin.recipe import ResolvedPluginRecipe, resolve_plugin_recipe
from app.discovery.redaction import redact_discovery_data, validate_discovery_data_bounds
from app.models import CrawlMethod, CrawlMethodRun, DiscoveryExperience, DiscoveryRunEvent, SiteDiscoveryRun
from app.trends.embedding import build_embedding_version
from app.trends.embedding_client import request_embeddings
from app.trends.embedding_config import get_trend_embedding_settings


logger = logging.getLogger(__name__)
MAX_EXPERIENCE_BYTES = 64 * 1024
_PROHIBITED_CONTENT_KEYS = {"body", "content", "html", "article", "articles", "webpage", "raw_text"}


def admit_approved_connector_experience(
    session: Session,
    method: CrawlMethod,
) -> tuple[DiscoveryExperience, DiscoveryExperience]:
    """Persist separate Explore and Build knowledge from one reviewed artifact."""
    if method.review_status != "approved":
        raise ValueError("connector experience requires an approved CrawlMethod")
    from app.discovery.plugin.review import validate_plugin_activation

    validate_plugin_activation(method)
    return _admit_reviewed_method_experiences(session, method)


def backfill_legacy_approved_experiences(session: Session, *, limit: int = 200) -> int:
    """Split previously admitted rows without pretending to re-run their old audit."""
    legacy_rows = list(session.scalars(
        select(DiscoveryExperience)
        .where(
            DiscoveryExperience.experience_kind == "approved_connector",
            DiscoveryExperience.approved.is_(True),
        )
        .order_by(DiscoveryExperience.id)
        .limit(min(500, max(1, limit)))
    ))
    converted = 0
    for legacy in legacy_rows:
        method = session.get(CrawlMethod, legacy.source_method_id) if legacy.source_method_id else None
        if (
            method is None
            or method.review_status != "approved"
            or (legacy.technical_features or {}).get("provenance") != "human_reviewed_connector"
        ):
            continue
        try:
            with session.begin_nested():
                _admit_reviewed_method_experiences(
                    session,
                    method,
                    generate_embeddings=False,
                )
            converted += 1
        except Exception as exc:
            logger.warning("legacy RAG experience backfill skipped method_id=%s: %s", method.id, exc)
    return converted


def _admit_reviewed_method_experiences(
    session: Session,
    method: CrawlMethod,
    *,
    generate_embeddings: bool = True,
) -> tuple[DiscoveryExperience, DiscoveryExperience]:
    resolved, artifact = _load_reviewed_recipe_artifact(method)
    manifest = resolved.manifest
    if resolved.reviewed_signature != method.signature:
        raise ValueError("approved method signature does not match its connector artifact")
    exploration = _approved_exploration(session, method)
    source_profile = {
        **dict(exploration.get("technical_features") or {}),
        "provenance": "human_reviewed_connector",
        "schema_version": 2,
        "recipe_type": "python_plugin",
        "runtime_version": manifest.runtime_version,
        "allowed_domains": list(manifest.allowed_domains),
        "entry_host": urlsplit(manifest.entry).hostname,
        "connector_kind": resolved.connector_kind,
    }
    explore = _upsert_experience(
        session,
        experience_kind="explore_strategy",
        source_method_id=method.id,
        domain=urlsplit(manifest.entry).hostname,
        technical_features={
            **source_profile,
            "strategy": {
                "extraction": exploration.get("extraction_strategy"),
                "pagination": exploration.get("pagination_strategy"),
                "summary_validation": exploration.get("summary_validation"),
                "supports_pagination": exploration.get("supports_pagination"),
            },
        },
        summary=(
            f"已验证并人工批准的 Explore 规范：{manifest.connector_key} v{manifest.version}；"
            "包含来源特征、正文提取证据和分页状态转换证据。"
        ),
        failure_summary=None,
        repair_summary=None,
        generate_embedding=generate_embeddings,
    )
    build = _upsert_experience(
        session,
        experience_kind="build_pattern",
        source_method_id=method.id,
        domain=urlsplit(manifest.entry).hostname,
        technical_features={
            **source_profile,
            "verified_strategy": {
                "extraction": exploration.get("extraction_strategy"),
                "pagination": exploration.get("pagination_strategy"),
            },
            "implementation_pattern": _connector_implementation_pattern(artifact.connector_path),
            "connector_contract": {
                "entry_field": "request.entry",
                "default_target_count": 50,
                "result_shape": "items+stats",
                "required_stats": ["discovered_count"],
                "text_field_max_chars": 2000,
            },
        },
        summary=(
            f"已审核的 Build 模式：{manifest.connector_key} v{manifest.version}；"
            "记录真实 Explore 策略如何落实为确定性 Python Connector。"
        ),
        failure_summary=None,
        repair_summary=None,
        generate_embedding=generate_embeddings,
    )
    legacy = session.scalar(select(DiscoveryExperience).where(
        DiscoveryExperience.experience_kind == "approved_connector",
        DiscoveryExperience.source_method_id == method.id,
    ))
    if legacy is not None:
        session.delete(legacy)
        session.flush()
    return explore, build


def confirm_repair_experience(
    session: Session,
    *,
    run: SiteDiscoveryRun,
    method: CrawlMethod,
    confirmed_by: str,
) -> DiscoveryExperience:
    """Admit a repair only at the human approval boundary and with explicit provenance."""
    if not confirmed_by.strip() or run.trigger_type != "repair" or run.resulting_method_id != method.id:
        raise ValueError("repair experience lacks explicit human-confirmed repair provenance")
    if method.review_status != "approved":
        raise ValueError("repair experience requires an approved repaired method")
    from app.discovery.plugin.review import validate_plugin_activation

    validate_plugin_activation(method)
    resolved, _artifact = _load_reviewed_recipe_artifact(method)
    manifest = resolved.manifest
    failures = list(session.scalars(
        select(CrawlMethodRun)
        .where(CrawlMethodRun.method_id == run.repair_method_id, CrawlMethodRun.status == "failed")
        .order_by(CrawlMethodRun.id.desc())
        .limit(3)
    )) if run.repair_method_id else []
    change_event = session.scalar(
        select(DiscoveryRunEvent)
        .where(DiscoveryRunEvent.run_id == run.id, DiscoveryRunEvent.event_type == "connector_written")
        .order_by(DiscoveryRunEvent.sequence.desc())
        .limit(1)
    )
    failure_evidence = [
        _knowledge_projection(item.failure_evidence or {"message": item.error_message})
        for item in reversed(failures)
    ]
    change_payload = dict(change_event.payload or {}) if change_event is not None else {}
    return _upsert_experience(
        session,
        experience_kind="confirmed_repair",
        source_method_id=method.id,
        domain=urlsplit(manifest.entry).hostname,
        technical_features={
            "provenance": "human_confirmed_repair",
            "recipe_type": "python_plugin",
            "runtime_version": manifest.runtime_version,
            "repair_run_id": run.id,
            "failure_signature": failure_evidence,
            "solution": {
                "action_summary": change_event.summary if change_event is not None else "",
                "change_summary": change_payload.get("change_summary"),
            },
            "verification": {
                "sandbox_trials": 2,
                "deterministic_evaluation": "passed",
                "human_confirmed": True,
            },
        },
        summary=f"人工确认的 connector 修复：{manifest.connector_key} v{manifest.version}。",
        failure_summary=json.dumps(failure_evidence, ensure_ascii=False, default=str)[:8000],
        repair_summary=(
            (change_event.summary if change_event is not None else "修复生成的新版本")
            + "；通过两次沙箱试跑、确定性验收及人工审核。"
        )[:4000],
    )


def experience_has_valid_provenance(
    session: Session,
    experience: DiscoveryExperience,
) -> bool:
    """Read-side defense: an arbitrary approved boolean never grants RAG trust."""
    if not experience.approved or experience.experience_kind not in {
        "explore_strategy", "build_pattern", "confirmed_repair"
    } or experience.source_method_id is None:
        return False
    method = session.get(CrawlMethod, experience.source_method_id)
    if method is None or method.review_status != "approved":
        return False
    try:
        resolved, _artifact = _load_reviewed_recipe_artifact(method)
    except Exception:  # noqa: BLE001 - invalid provenance is simply excluded from RAG
        return False
    if resolved.reviewed_signature != method.signature:
        return False
    provenance = (experience.technical_features or {}).get("provenance")
    expected_provenance = (
        "human_confirmed_repair"
        if experience.experience_kind == "confirmed_repair"
        else "human_reviewed_connector"
    )
    if provenance != expected_provenance:
        return False
    if experience.experience_kind == "confirmed_repair":
        try:
            from app.discovery.plugin.review import validate_plugin_activation

            validate_plugin_activation(method)
        except Exception:
            return False
        repair_run = session.scalar(
            select(SiteDiscoveryRun).where(
                SiteDiscoveryRun.resulting_method_id == method.id,
                SiteDiscoveryRun.trigger_type == "repair",
                SiteDiscoveryRun.status == "completed",
            )
        )
        return repair_run is not None
    return True


def _approved_exploration(session: Session, method: CrawlMethod) -> dict[str, Any]:
    """Load the immutable approved run's bounded processing summary."""
    try:
        from app.discovery.checkpoints import CheckpointStore
        from app.discovery.plugin.review import parse_plugin_review_evidence

        evidence = parse_plugin_review_evidence(method)
        audit = evidence.get("method_audit") or {}
        trial = audit.get("trial_artifact") or audit.get("artifact") or {}
        run_id = trial.get("trial_run_id")
        if not isinstance(run_id, int):
            return {}
        run = session.get(SiteDiscoveryRun, run_id)
        if run is None or not run.checkpoint_path:
            return {}
        checkpoint = CheckpointStore().load(run.checkpoint_path, expected_run_id=run_id)
        return _knowledge_projection(checkpoint.processing_summary)
    except Exception as exc:  # older approved rows remain usable with a smaller profile
        logger.warning("approved Explore evidence unavailable method_id=%s: %s", method.id, exc)
        return {}


def _connector_implementation_pattern(path: Any) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports: set[str] = set()
    functions: list[str] = []
    request_keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name[:100])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in {
            "entry", "config", "target_count", "start_at", "end_at"
        }:
            request_keys.add(node.value)
    return {
        "imports": sorted(imports),
        "functions": functions[:50],
        "request_fields": sorted(request_keys),
        "source_lines": len(source.splitlines()),
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }


def _knowledge_projection(value: Any, *, depth: int = 0) -> Any:
    """Retain strategies and paths while excluding outputs, previews, and article text."""
    if depth > 10:
        return "[truncated]"
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {"items", "outputs", "body", "html", "article", "articles", "preview", "previews", "raw_text"}:
                continue
            projected[str(key)[:200]] = _knowledge_projection(item, depth=depth + 1)
        return projected
    if isinstance(value, list):
        return [_knowledge_projection(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:2000]
    return value


def _load_reviewed_recipe_artifact(
    method: CrawlMethod,
) -> tuple[ResolvedPluginRecipe, ConnectorArtifact]:
    """Resolve website and shared recipes without weakening either signature gate."""
    resolved = resolve_plugin_recipe(method.dsl_recipe)
    manifest = resolved.manifest
    if resolved.connector_kind == "shared" and manifest.connector_key == "wechat_sogou":
        from app.discovery.wechat_plugin import load_wechat_sogou_artifact

        artifact = load_wechat_sogou_artifact()
    else:
        artifact = load_connector_artifact(
            get_settings().discovery_connector_root,
            kind=resolved.connector_kind,
            connector_key=manifest.connector_key,
            version=manifest.version,
        )
    if artifact.manifest != manifest:
        raise ValueError("reviewed recipe Manifest does not match connector artifact")
    return resolved, artifact


def _upsert_experience(
    session: Session,
    *,
    experience_kind: str,
    source_method_id: int,
    domain: str | None,
    technical_features: dict[str, Any],
    summary: str,
    failure_summary: str | None,
    repair_summary: str | None,
    generate_embedding: bool = True,
) -> DiscoveryExperience:
    raw = {
        "technical_features": technical_features,
        "summary": summary,
        "failure_summary": failure_summary,
        "repair_summary": repair_summary,
    }
    _reject_prohibited_content(raw)
    validate_discovery_data_bounds(
        raw,
        max_string_chars=MAX_EXPERIENCE_BYTES,
        max_approx_bytes=MAX_EXPERIENCE_BYTES,
    )
    safe = redact_discovery_data(raw)
    text = " ".join(
        str(value) for value in (
            safe["summary"], safe["failure_summary"], safe["repair_summary"], safe["technical_features"]
        ) if value
    )
    embedding = None
    embedding_version = None
    if generate_embedding:
        try:
            embedding_version = build_embedding_version(get_trend_embedding_settings())
            embedding = request_embeddings([text], embedding_version=embedding_version)[0]
        except Exception as exc:  # indexing may be retried; approval itself remains available
            logger.warning("Discovery experience embedding deferred: %s", exc)
    row = session.scalar(
        select(DiscoveryExperience).where(
            DiscoveryExperience.experience_kind == experience_kind,
            DiscoveryExperience.source_method_id == source_method_id,
        )
    )
    if row is None:
        try:
            with session.begin_nested():
                row = DiscoveryExperience(
                    experience_kind=experience_kind,
                    source_method_id=source_method_id,
                    approved=True,
                    domain=domain,
                    technical_features={},
                    summary="",
                )
                session.add(row)
                session.flush()
        except IntegrityError:
            row = session.scalar(
                select(DiscoveryExperience).where(
                    DiscoveryExperience.experience_kind == experience_kind,
                    DiscoveryExperience.source_method_id == source_method_id,
                )
            )
            if row is None:
                raise
    row.domain = domain
    row.technical_features = safe["technical_features"]
    row.summary = safe["summary"]
    row.failure_summary = safe["failure_summary"]
    row.repair_summary = safe["repair_summary"]
    row.embedding = embedding
    row.embedding_version = embedding_version if embedding is not None else None
    row.approved = True
    session.flush()
    return row


def _reject_prohibited_content(value: Any, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            # A field mapping such as {"content": ".//article/body"} is a
            # reusable extraction path, not article content. Everywhere else
            # the raw-content names remain forbidden.
            if normalized in _PROHIBITED_CONTENT_KEYS and (not path or path[-1] != "field_mapping"):
                raise ValueError(f"Discovery experience may not store webpage/article content: {'.'.join((*path, normalized))}")
            _reject_prohibited_content(item, path=(*path, normalized))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_prohibited_content(item, path=(*path, str(index)))
