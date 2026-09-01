"""Connector execution entry with an explicit migration-only legacy seam."""

from __future__ import annotations

import uuid
from pathlib import Path
from threading import Event
from typing import Any, Callable

from app.config import get_settings
from app.discovery.plugin.artifact import load_connector_artifact
from app.discovery.plugin.contracts import (
    ConnectorContext,
    ConnectorInvocation,
    ConnectorRequest,
)
from app.discovery.plugin.recipe import resolve_plugin_recipe
from app.discovery.sandbox import SandboxExecution, SandboxJobPriority, SandboxRuntime
from app.discovery.sandbox.runtime import get_formal_sandbox_runtime

def run_method(
    recipe: dict[str, Any],
    *,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    sandbox_priority: SandboxJobPriority = SandboxJobPriority.MANUAL,
    expected_signature: str | None = None,
    sandbox_runtime: SandboxRuntime | None = None,
    sandbox_job_id: str | None = None,
    cancel_event: Event | None = None,
    allow_legacy_compatibility: bool = False,
    request_parameters: dict[str, Any] | None = None,
    execution_metadata_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    """Execute a plugin; legacy recipes require an audited migration caller."""
    if recipe.get("recipe_type") == "python_plugin":
        return _run_python_plugin(
            recipe,
            progress_callback=progress_callback,
            priority=sandbox_priority,
            expected_signature=expected_signature,
            runtime=sandbox_runtime,
            job_id=sandbox_job_id,
            cancel_event=cancel_event,
            request_parameters=request_parameters,
            execution_metadata_callback=execution_metadata_callback,
        )
    if not allow_legacy_compatibility:
        raise ValueError("website/WeChat DSL execution is migration-only")
    return _run_legacy_compatibility(recipe, progress_callback=progress_callback)


def _run_python_plugin(
    recipe: dict[str, Any],
    *,
    progress_callback: Callable[[str, dict[str, Any]], None] | None,
    priority: SandboxJobPriority,
    expected_signature: str | None,
    runtime: SandboxRuntime | None,
    job_id: str | None,
    cancel_event: Event | None,
    request_parameters: dict[str, Any] | None,
    execution_metadata_callback: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    """Execute an approved manifest deterministically in gVisor; never use DSL/LLM."""
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("formal connector execution cancelled before artifact loading")
    settings = get_settings()
    resolved = resolve_plugin_recipe(recipe)
    manifest = resolved.manifest
    configured_version = (
        Path(settings.discovery_connector_root)
        / resolved.connector_kind
        / manifest.connector_key
        / f"v{manifest.version}"
    )
    if configured_version.exists() or resolved.connector_kind != "shared":
        artifact = load_connector_artifact(
            settings.discovery_connector_root,
            kind=resolved.connector_kind,
            connector_key=manifest.connector_key,
            version=manifest.version,
        )
    else:
        from app.discovery.wechat_plugin import bundled_connector_root

        artifact = load_connector_artifact(
            bundled_connector_root(),
            kind="shared",
            connector_key=manifest.connector_key,
            version=manifest.version,
        )
    if expected_signature is None or resolved.reviewed_signature != expected_signature:
        raise ValueError("formal connector execution requires the reviewed recipe signature")
    if artifact.manifest != manifest:
        raise ValueError("connector artifact signature mismatch")
    supplied = dict(request_parameters or {})
    invocation = ConnectorInvocation(
        request=ConnectorRequest(
            entry=manifest.entry,
            config=resolved.config,
            target_count=supplied.get("target_count") or 50,
            start_at=supplied.get("start_at"),
            end_at=supplied.get("end_at"),
        ),
        context=ConnectorContext(
            run_id=None,
            connector_key=manifest.connector_key,
            connector_version=manifest.version,
            allowed_domains=manifest.allowed_domains,
        ),
    )
    execution = SandboxExecution(
        job_id=job_id or f"formal-{uuid.uuid4().hex}",
        artifact=artifact,
        invocation=invocation,
        kind=resolved.connector_kind,
        priority=priority,
        expected_checksum=manifest.checksum,
        expected_signature=artifact.signature,
        purpose="formal",
    )
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("formal connector execution cancelled before sandbox registration")
    if progress_callback is not None:
        progress_callback("sandbox_started", {"connector_version": manifest.version})
    result = (runtime or get_formal_sandbox_runtime()).execute(execution, cancel_event=cancel_event)
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("formal connector execution cancelled after sandbox completion")
    if progress_callback is not None:
        progress_callback(
            "sandbox_completed",
            {"connector_version": manifest.version, "elapsed_seconds": result.elapsed_seconds},
        )
    if execution_metadata_callback is not None:
        from app.discovery.sandbox.runtime import validate_runtime_attestation

        attestation = validate_runtime_attestation(result.attestation.as_dict())
        if (
            attestation.artifact_checksum != manifest.checksum
            or attestation.artifact_signature != artifact.signature
            or attestation.connector_key != manifest.connector_key
            or attestation.connector_version != manifest.version
            or attestation.purpose != "formal"
        ):
            raise ValueError("formal connector runtime attestation does not bind the manifest")
        execution_metadata_callback({
            "time_semantics": manifest.time_semantics,
            "host_observed_at": attestation.completed_at,
        })
    return result.output.model_dump(mode="json")


def _run_legacy_compatibility(
    recipe: dict[str, Any],
    *,
    progress_callback: Callable[[str, dict[str, Any]], None] | None,
) -> dict[str, Any]:
    """Lazy rollback/internal-forum seam; connector paths never import it."""
    from app.discovery.interpreter import DslExecutionPartialError
    from app.discovery.multi_interpreter import MultiDslInterpreter
    from app.discovery.partial_output import PartialExecutionError

    try:
        return MultiDslInterpreter().run(
            _as_multi_dsl_recipe(recipe),
            progress_callback=progress_callback,
        )
    except DslExecutionPartialError as exc:
        raise PartialExecutionError(
            str(exc),
            items=list(exc.items),
            stats=dict(exc.stats or {}),
        ) from exc


def _as_multi_dsl_recipe(recipe: dict[str, Any]) -> Any:
    """把旧版 dict / dsl 配方适配成 MultiDslRecipe（source_kind=website）。

    功能：已是 multi_dsl 类型则直接构造；否则用 DslRecipe 解析后映射为 MultiDslRecipe，
    把入口 URL、动作列表等字段填上，供统一解释器执行。
    谁会调用：run_method 在收到非 MultiDslRecipe 配方时调用。
    直接调用：
    - DslRecipe(**recipe)：解析旧版配方。
    - MultiDslRecipe(...)：构造统一配方对象。
    输入与结果：输入 dict；返回 MultiDslRecipe。
    副作用：无。
    """
    from app.discovery.dsl import DslRecipe
    from app.discovery.multi_dsl import MultiDslRecipe

    if recipe.get("recipe_type") == "multi_dsl":
        return MultiDslRecipe(**recipe)
    dsl = DslRecipe(**recipe)
    return MultiDslRecipe(
        recipe_type="multi_dsl",
        source_kind="website",
        entry=dsl.entry_url,
        auth_ref=None,
        requires_auth=False,
        actions=[action.model_dump(by_alias=True) for action in dsl.actions],
        notes=dsl.notes,
    )
