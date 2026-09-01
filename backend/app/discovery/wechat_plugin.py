"""Deterministic Discovery flow for the one shared anonymous Sogou connector."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select, text, update

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
from app.discovery.events import append_discovery_event
from app.discovery.loop.evaluator import evaluate_connector_outputs
from app.discovery.loop.artifacts import write_trial_artifact
from app.discovery.plugin.artifact import ConnectorArtifact, load_connector_artifact
from app.discovery.plugin.contracts import (
    ConnectorContext,
    ConnectorInvocation,
    ConnectorOutput,
    ConnectorRequest,
)
from app.discovery.plugin.recipe import (
    ConfiguredPluginRecipe,
    WechatCheckpointOutput,
    WechatSogouConfig,
    configured_plugin_config_hash,
    configured_plugin_signature,
    resolve_plugin_recipe,
)
from app.discovery.plugin.review import (
    artifact_evidence,
    attach_plugin_review_evidence,
    bind_packaged_artifact_evidence,
    quality_audit_from_evidence,
)
from app.discovery.quality_audit import audit_plugin_trial_quality
from app.discovery.redaction import redact_discovery_data, redact_discovery_text
from app.discovery.runtime import create_discovery_run_or_raise, finish_discovery_run
from app.discovery.sandbox.capacity import SandboxJobPriority
from app.discovery.sandbox.runtime import SandboxExecution, SandboxExecutionResult, SandboxRuntime
from app.enums import SourceType, Stream
from app.models import CrawlMethod, CrawlMethodRun, SiteDiscoveryRun, Source


logger = logging.getLogger(__name__)
SHARED_CONNECTOR_KEY = "wechat_sogou"
SHARED_CONNECTOR_VERSION = 2
REVIEW_PENDING = "pending"
WECHAT_PHASES = ("context", "execute", "evaluate", "package")
WECHAT_EXECUTION_STATE_MAX_BYTES = 3 * 1024 * 1024
_WECHAT_SQLITE_PACKAGE_LOCK = threading.RLock()
_WECHAT_DOMAIN_LOCKS_GUARD = threading.Lock()
_WECHAT_DOMAIN_LOCKS: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()
_WECHAT_URL_VERIFIER_SOURCE = r'''
from datetime import datetime, timezone
from urllib.parse import urlsplit
import httpx
import json

async def crawl(request, context):
    del context
    urls = request.get("config", {}).get("urls") or []
    items = []
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers={"User-Agent": "os-news-tracker/wechat-url-verifier"}) as client:
        for raw_url in urls[:10]:
            try:
                if urlsplit(raw_url).hostname != "mp.weixin.qq.com":
                    continue
                response = await client.get(raw_url)
                if response.status_code < 400 and urlsplit(str(response.url)).hostname == "mp.weixin.qq.com":
                    items.append({
                        "title": "reachable",
                        "url": raw_url,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "summary": "reachable " * 30,
                    })
            except Exception as exc:
                print(json.dumps({"event": "wechat_url_check_failed", "error": str(exc)[:300]}), file=__import__("sys").stderr)
    return {"items": items, "stats": {"discovered_count": len(items)}}
'''.strip()


class _ExecutionBudget:
    """Thirty-minute budget whose clock pauses for every sandbox capacity wait."""

    def __init__(self, maximum_seconds: float, *, already_elapsed: float = 0.0) -> None:
        self.maximum_seconds = min(1800.0, max(0.1, maximum_seconds))
        self.already_elapsed = max(0.0, already_elapsed)
        self.started_at: float | None = time.monotonic() if self.already_elapsed > 0 else None
        self.excluded_queue_seconds = 0.0
        self.pending_queue_started_at: float | None = None
        self._lock = threading.Lock()

    def capacity_queued(self) -> float:
        """Mark a capacity wait before submitting the sandbox job."""
        with self._lock:
            queued_at = time.monotonic()
            self.pending_queue_started_at = queued_at
            return queued_at

    def capacity_acquired(self, queued_at: float) -> float:
        now = time.monotonic()
        with self._lock:
            queue_started_at = self.pending_queue_started_at or queued_at
            queue_wait_seconds = max(0.0, now - queue_started_at)
            if self.started_at is None:
                self.started_at = now
            else:
                self.excluded_queue_seconds += queue_wait_seconds
            self.pending_queue_started_at = None
            return queue_wait_seconds

    def elapsed(self) -> float:
        with self._lock:
            if self.started_at is None:
                return self.already_elapsed
            pending_queue_seconds = (
                max(0.0, time.monotonic() - self.pending_queue_started_at)
                if self.pending_queue_started_at is not None
                else 0.0
            )
            return self.already_elapsed + max(
                0.0,
                time.monotonic() - self.started_at - self.excluded_queue_seconds - pending_queue_seconds,
            )

    def remaining(self) -> float:
        return max(0.0, self.maximum_seconds - self.elapsed())

    def check(self) -> None:
        if self.remaining() <= 0:
            raise TimeoutError("WeChat Discovery 已达到 30 分钟执行预算")


def _domain_package_lock(domain: str) -> threading.RLock:
    """Return a live per-domain lock without retaining an unbounded key map."""
    with _WECHAT_DOMAIN_LOCKS_GUARD:
        lock = _WECHAT_DOMAIN_LOCKS.get(domain)
        if lock is None:
            lock = threading.RLock()
            _WECHAT_DOMAIN_LOCKS[domain] = lock
        return lock


def bundled_connector_root() -> Path:
    """Return the immutable connector tree shipped with the backend application."""
    return Path(__file__).resolve().parents[2] / "connectors"


def load_wechat_sogou_artifact(settings: Settings | None = None) -> ConnectorArtifact:
    """Prefer the deployed registry; use the reviewed bundled copy only when absent."""
    settings = settings or get_settings()
    configured = Path(settings.discovery_connector_root)
    expected = configured / "shared" / SHARED_CONNECTOR_KEY / f"v{SHARED_CONNECTOR_VERSION}"
    root = configured if expected.exists() else bundled_connector_root()
    return load_connector_artifact(
        root,
        kind="shared",
        connector_key=SHARED_CONNECTOR_KEY,
        version=SHARED_CONNECTOR_VERSION,
    )


def public_wechat_config(
    value: str,
    *,
    input_type: str,
    hints: dict[str, Any] | None,
) -> WechatSogouConfig:
    """Translate legacy route inputs to the strict public-only shared configuration."""
    hints = hints or {}
    if input_type not in {"wechat_search", "wechat_history", "wechat_history_url", "wechat_account"}:
        raise ValueError("shared WeChat connector received an unsupported route type")
    allowed_hint_keys = {
        "source_kind",
        "account_name",
        "keywords",
        "max_pages",
        "limit",
        # Accepted only for HTTP contract compatibility; the public connector
        # always fetches article bodies and has no template/auth mode.
        "fetch_content",
        "template_variant",
    }
    forbidden = {
        str(key) for key in hints
        if any(part in str(key).lower() for part in ("cookie", "token", "authorization", "auth_ref", "password", "secret"))
    }
    if forbidden:
        raise ValueError(f"authenticated WeChat configuration is deprecated: {', '.join(sorted(forbidden))}")
    unknown = {str(key) for key in hints if str(key) not in allowed_hint_keys}
    if unknown:
        raise ValueError(f"unsupported WeChat public configuration: {', '.join(sorted(unknown))}")
    raw_keywords = hints.get("keywords") or ()
    if isinstance(raw_keywords, str):
        raw_keywords = (raw_keywords,)
    if input_type == "wechat_search":
        account_name = str(hints.get("account_name") or "")
        keywords = tuple(raw_keywords) or (value,)
    elif urlsplit(value).scheme in {"http", "https"}:
        account_name = str(hints.get("account_name") or "")
        keywords = tuple(raw_keywords) or (value,)
    else:
        account_name = str(hints.get("account_name") or value)
        keywords = tuple(raw_keywords)
    return WechatSogouConfig(
        account_name=account_name,
        keywords=keywords,
        max_pages=int(hints.get("max_pages") or 6),
        limit=int(hints.get("limit") or 120),
    )


class WechatDiscoveryEngine:
    """Program-owned phases for configuring and validating the shared connector."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        sandbox: SandboxRuntime | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.sandbox = sandbox or SandboxRuntime(settings=self.settings)
        self.checkpoints = checkpoint_store or CheckpointStore(self.settings)
        self._jobs: dict[int, set[str]] = {}
        self._phase_started_elapsed: dict[int, tuple[str, float]] = {}
        self._budgets: dict[int, _ExecutionBudget] = {}
        self._lock = threading.Lock()

    def start(
        self,
        value: str,
        *,
        input_type: str,
        force: bool,
        name: str,
        hints: dict[str, Any] | None,
        trigger_type: str = "manual",
        repair_method_id: int | None = None,
        configured_recipe: WechatSogouConfig | None = None,
        repair_evidence: list[dict[str, Any]] | None = None,
    ) -> int:
        config = configured_recipe or public_wechat_config(value, input_type=input_type, hints=hints)
        run_id = create_discovery_run_or_raise(
            value,
            source_kind="wechat",
            trigger_type=trigger_type,
            repair_method_id=repair_method_id,
            runtime_version=self.settings.discovery_runtime_version,
        )
        if trigger_type == "repair":
            try:
                append_discovery_event(
                    run_id,
                    event_type="formal_failure_evidence",
                    summary="已载入连续三次正式连接器失败的脱敏证据。",
                    phase="context",
                    round_number=0,
                    payload={"failures": list(repair_evidence or ())[:3]},
                )
            except Exception as exc:
                finish_discovery_run(
                    run_id,
                    status="failed",
                    error_message=redact_discovery_text(str(exc))[:4000],
                )
                raise
        register_run(run_id)
        threading.Thread(
            target=self.run,
            kwargs={
                "run_id": run_id,
                "value": value,
                "input_type": input_type,
                "force": force,
                "name": name,
                "config": config,
            },
            daemon=True,
            name=f"wechat-discovery-{run_id}",
        ).start()
        return run_id

    def start_repair(self, method_id: int) -> int:
        """Revalidate a failed shared configuration and package it for review."""
        db = SessionLocal()
        try:
            method = db.get(CrawlMethod, method_id)
            if method is None:
                raise ValueError(f"method {method_id} not found")
            if method.review_status != "approved" or method.status != "active":
                raise ValueError("automatic repair requires an approved active method")
            resolved = resolve_plugin_recipe(method.dsl_recipe)
            if resolved.connector_kind != "shared" or resolved.manifest.connector_key != SHARED_CONNECTOR_KEY:
                raise ValueError("WeChat repair requires the reviewed shared connector")
            artifact = load_wechat_sogou_artifact(self.settings)
            if (
                artifact.manifest != resolved.manifest
                or method.signature != resolved.reviewed_signature
            ):
                raise ValueError("shared repair source no longer matches review evidence")
            config = WechatSogouConfig.model_validate(resolved.config)
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
            repair_evidence = [
                dict(item.failure_evidence or {}) for item in reversed(failures)
            ]
            source = db.get(Source, method.source_id)
            if source is None:
                raise ValueError("WeChat repair source is missing")
            value, name = source.url, source.name
            input_type = (
                "wechat_search" if method.domain.startswith("wechat_search_") else "wechat_account"
            )
        finally:
            db.close()
        return self.start(
            value,
            input_type=input_type,
            force=True,
            name=name,
            hints=None,
            trigger_type="repair",
            repair_method_id=method_id,
            configured_recipe=config,
            repair_evidence=repair_evidence,
        )

    def resume(self, run_id: int, checkpoint: DiscoveryCheckpoint) -> None:
        summary = checkpoint.processing_summary
        if checkpoint.token_usage != 0:
            raise ValueError("WeChat Discovery checkpoint must not contain LLM token usage")
        if checkpoint.round != 1:
            raise ValueError("WeChat Discovery checkpoint round is invalid")
        if checkpoint.phase not in WECHAT_PHASES:
            raise ValueError("WeChat Discovery checkpoint phase is invalid")
        config = WechatSogouConfig.model_validate(summary.get("wechat_config") or {})
        register_run(run_id)
        threading.Thread(
            target=self.run,
            kwargs={
                "run_id": run_id,
                "value": str(summary.get("wechat_input") or ""),
                "input_type": str(summary.get("input_type") or "wechat_search"),
                "force": True,
                "name": str(summary.get("display_name") or "微信公众号"),
                "config": config,
                "resume_checkpoint": checkpoint,
            },
            daemon=True,
            name=f"wechat-discovery-resume-{run_id}",
        ).start()

    def cancel(self, run_id: int) -> bool:
        with self._lock:
            jobs = tuple(self._jobs.get(run_id, ()))
        return any(self.sandbox.cancel(job_id) for job_id in jobs)

    def run(
        self,
        *,
        run_id: int,
        value: str,
        input_type: str,
        force: bool,
        name: str,
        config: WechatSogouConfig,
        resume_checkpoint: DiscoveryCheckpoint | None = None,
    ) -> None:
        token = activate_run(run_id)
        trace = self._load_trace(run_id) if resume_checkpoint is not None else []
        budget = _ExecutionBudget(
            self.settings.discovery_loop_max_seconds,
            already_elapsed=resume_checkpoint.elapsed_seconds if resume_checkpoint else 0.0,
        )
        with self._lock:
            self._budgets[run_id] = budget
        evidence: list[dict[str, Any]] = []
        execution_state: dict[str, Any] = dict(
            resume_checkpoint.execution_result or {}
        ) if resume_checkpoint else {}
        try:
            budget.check()
            artifact = load_wechat_sogou_artifact(self.settings)
            self._validate_checkpoint_artifact(resume_checkpoint, artifact)
            if self._can_resume_package(resume_checkpoint):
                self._phase(run_id, "package", trace, "从已通过验收的微信检查点继续封装。", budget=budget)
                budget.check()
                self._package(
                    run_id=run_id,
                    artifact=artifact,
                    value=value,
                    input_type=input_type,
                    name=name,
                    config=config,
                    force=force,
                    budget=budget,
                    trace=trace,
                    plugin_review=(resume_checkpoint.evaluation_result or {}).get("plugin_review") or {},
                )
                return
            if resume_checkpoint is None or resume_checkpoint.phase == "context":
                self._phase(run_id, "context", trace, "准备共享无登录搜狗微信采集器配置。", budget=budget)
            if resume_checkpoint is not None and resume_checkpoint.phase == "evaluate":
                # A failed completed evaluation gets a fresh deterministic trial,
                # but retains its consumed budget and prior diagnosis.
                execution_state = {}
            trial_snapshots = [
                WechatCheckpointOutput.model_validate(raw)
                for raw in execution_state.get("trial_outputs") or []
            ][:2]
            outputs = [snapshot.output for snapshot in trial_snapshots if snapshot.complete and snapshot.output is not None]
            trial_attestations = list(execution_state.get("trial_attestations") or [])[:len(outputs)]
            auxiliary_proofs = list(execution_state.get("auxiliary_proofs") or [])
            self._phase(run_id, "execute", trace, "通过 gVisor 对共享采集器独立试运行两次。", budget=budget)
            page_one_config = {**config.model_dump(mode="json"), "page": 1, "max_pages": 1}
            for trial in range(len(outputs) + 1, 3):
                result = self._execute(
                    run_id,
                    artifact,
                    config,
                    suffix=f"trial-{trial}",
                    override_config=page_one_config,
                    budget=budget,
                )
                outputs.append(result.audit_output)
                trial_attestations.append(result.attestation.as_dict())
                evidence.extend(result.events)
                execution_state = self._execution_state(
                    artifact, outputs, None, set(), trial_attestations,
                    auxiliary_proofs,
                )
                self._checkpoint(
                    run_id, phase="execute", config=config, value=value,
                    input_type=input_type, name=name, evidence=evidence,
                    evaluation={"passed": False, "pending": "independent_trials"},
                    execution_result=execution_state, artifact=artifact,
                    elapsed_seconds=budget.elapsed(),
                )
            raw_page_output = execution_state.get("page_output")
            page_snapshot = WechatCheckpointOutput.model_validate(raw_page_output) if raw_page_output else None
            page_output = page_snapshot.output if page_snapshot is not None and page_snapshot.complete else None
            if config.max_pages > 1 and page_output is None:
                page_config = {**config.model_dump(mode="json"), "page": 2, "max_pages": 1}
                result = self._execute(
                    run_id,
                    artifact,
                    config,
                    suffix="page-2",
                    override_config=page_config,
                    budget=budget,
                )
                page_output = result.audit_output
                auxiliary_proofs.append(sandbox_execution_proof(result))
                evidence.extend(result.events)
                execution_state = self._execution_state(
                    artifact, outputs, page_output, set(), trial_attestations,
                    auxiliary_proofs,
                )
                self._checkpoint(
                    run_id, phase="execute", config=config, value=value,
                    input_type=input_type, name=name, evidence=evidence,
                    evaluation={"passed": False, "pending": "url_verification"},
                    execution_result=execution_state, artifact=artifact,
                    elapsed_seconds=budget.elapsed(),
                )
            # Summary-only WeChat discovery validates the Sogou result payload
            # itself. It deliberately does not open mp.weixin.qq.com article
            # pages, because a CAPTCHA is not a connector defect and must not
            # be bypassed or treated as an acceptance failure.
            reachable_urls: set[str] = set()
            execution_state = self._execution_state(
                artifact, outputs, page_output, reachable_urls, trial_attestations,
                auxiliary_proofs,
            )
            persisted_trials = [
                WechatCheckpointOutput.model_validate(raw)
                for raw in execution_state.get("trial_outputs") or []
            ]
            if len(persisted_trials) != 2 or not all(snapshot.complete for snapshot in persisted_trials):
                raise ValueError("complete WeChat trial outputs do not fit durable review evidence")
            persisted_page = execution_state.get("page_output")
            if page_output is not None and (
                not isinstance(persisted_page, dict)
                or WechatCheckpointOutput.model_validate(persisted_page).complete is not True
            ):
                raise ValueError("complete WeChat pagination output does not fit durable review evidence")
            self._phase(run_id, "evaluate", trace, "执行搜狗摘要字段、去重、分页与双试跑验收。", budget=budget)
            budget.check()
            evaluation = evaluate_connector_outputs(
                outputs,
                reachable_urls=reachable_urls,
                supports_pagination=config.max_pages > 1,
                pagination_output=page_output,
                require_url_accessibility=False,
                enforce_fixed_listing_page=False,
                enforce_text_coverage=False,
            )
            evaluation_document = evaluation.as_dict()
            method_audit = audit_plugin_trial(
                evaluation=evaluation_document,
                outputs=outputs,
                artifact_evidence={
                    **artifact_evidence(artifact, kind="shared"),
                    "trial_run_id": run_id,
                    "trial_config_hash": configured_plugin_config_hash(config),
                    "config_signature": configured_plugin_signature(
                        ConfiguredPluginRecipe(manifest=artifact.manifest, config=config)
                    ),
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
                    method_audit["passed"] or method_audit["low_frequency_exception_eligible"]
                ),
            }
            evaluation_document["plugin_review"] = plugin_review
            append_discovery_event(
                run_id,
                event_type="wechat_evaluation_completed",
                summary="微信共享采集器确定性验收完成。",
                phase="evaluate",
                round_number=1,
                level="info" if evaluation_document.get("passed") else "warning",
                payload={
                    "passed": bool(evaluation_document.get("passed")),
                    "failures": list(evaluation_document.get("failures") or [])[:12],
                    "item_count": sum(len(output.items) for output in outputs),
                    "trial_count": len(outputs),
                    "reachable_count": len(reachable_urls),
                },
            )
            self._checkpoint(
                run_id,
                phase="evaluate",
                config=config,
                value=value,
                input_type=input_type,
                name=name,
                evidence=evidence,
                evaluation=evaluation_document,
                execution_result=execution_state,
                artifact=artifact,
                elapsed_seconds=budget.elapsed(),
            )
            if not plugin_review["package_eligible"]:
                raise ValueError(f"shared WeChat connector acceptance failed: {evaluation.failures}")
            budget.check()
            self._phase(run_id, "package", trace, "保存每公众号配置并进入人工审核。", budget=budget)
            budget.check()
            self._package(
                run_id=run_id,
                artifact=artifact,
                value=value,
                input_type=input_type,
                name=name,
                config=config,
                force=force,
                budget=budget,
                trace=trace,
                plugin_review=plugin_review,
            )
        except DiscoveryCancelled as exc:
            finish_discovery_run(run_id, status="cancelled", error_message=str(exc), node_trace=trace)
        except Exception as exc:
            safe_error = redact_discovery_text(str(exc))[:4000]
            logger.exception("shared WeChat Discovery failed run_id=%s", run_id)
            try:
                phase = str(trace[-1].get("step") if trace else "context")
                self._checkpoint(
                    run_id,
                    phase=phase,
                    config=config,
                    value=value,
                    input_type=input_type,
                    name=name,
                    evidence=evidence,
                    evaluation={
                        "passed": False,
                        "failures": [{"check": "wechat_shared_execution", "error": safe_error}],
                    },
                    execution_result=execution_state,
                    artifact=artifact if "artifact" in locals() else None,
                    elapsed_seconds=budget.elapsed(),
                )
            except Exception:
                logger.exception("failed to save WeChat Discovery failure checkpoint run_id=%s", run_id)
            append_discovery_event(
                run_id,
                event_type="run_failed",
                summary="微信 Single Agent Loop 失败并停止。",
                phase=str(trace[-1].get("step") if trace else "context"),
                round_number=1,
                level="error",
                payload={"error": safe_error, **self._current_timing_payload(run_id, budget=budget)},
            )
            finish_discovery_run(run_id, status="failed", error_message=safe_error, node_trace=trace, llm_token_usage=0)
        finally:
            with self._lock:
                self._jobs.pop(run_id, None)
                self._phase_started_elapsed.pop(run_id, None)
                self._budgets.pop(run_id, None)
            deactivate_run(token)
            unregister_run(run_id)

    def _execute(
        self,
        run_id: int,
        artifact: ConnectorArtifact,
        config: WechatSogouConfig,
        *,
        suffix: str,
        override_config: dict[str, Any] | None = None,
        budget: _ExecutionBudget,
    ) -> SandboxExecutionResult:
        ensure_not_cancelled()
        job_id = f"discovery-{run_id}-wechat-{suffix}"
        invocation = ConnectorInvocation(
            request=ConnectorRequest(entry=artifact.manifest.entry, config=override_config or config.model_dump(mode="json")),
            context=ConnectorContext(
                run_id=run_id,
                connector_key=artifact.manifest.connector_key,
                connector_version=artifact.manifest.version,
                allowed_domains=artifact.manifest.allowed_domains,
            ),
        )
        budget.check()
        execution = SandboxExecution(
            job_id=job_id,
            artifact=artifact,
            invocation=invocation,
            kind="shared",
            priority=self._sandbox_priority(run_id),
            expected_checksum=artifact.manifest.checksum,
            expected_signature=artifact.signature,
            timeout_seconds=max(0.1, min(budget.remaining(), self.settings.discovery_sandbox_timeout_seconds)),
            purpose=(
                "primary_trial" if suffix in {"trial-1", "trial-2"}
                else "pagination" if suffix == "page-2"
                else "other"
            ),
            trial_index=(int(suffix[-1]) if suffix in {"trial-1", "trial-2"} else None),
        )
        return self._run_sandbox_execution(
            run_id,
            execution,
            budget=budget,
            phase="execute",
            round_number=1,
        )

    def _verify_output_urls(
        self,
        *,
        run_id: int,
        outputs: list[ConnectorOutput],
        budget: _ExecutionBudget,
    ) -> tuple[set[str], list[dict[str, Any]], list[dict[str, Any]]]:
        verifier = write_trial_artifact(
            run_id=run_id,
            site_url="https://mp.weixin.qq.com/",
            source=_WECHAT_URL_VERIFIER_SOURCE,
            allowed_domains=["mp.weixin.qq.com"],
            runtime_version=self.settings.discovery_runtime_version,
            connector_key="wechat_url_verifier",
            version=1,
            settings=self.settings,
        )
        reachable: set[str] = set()
        evidence: list[dict[str, Any]] = []
        proofs: list[dict[str, Any]] = []
        for index, output in enumerate(outputs, start=1):
            sample_urls = [
                item.url for item in output.items[:10]
                if urlsplit(item.url).scheme in {"http", "https"}
                and urlsplit(item.url).hostname == "mp.weixin.qq.com"
            ]
            invocation = ConnectorInvocation(
                request=ConnectorRequest(
                    entry=verifier.manifest.entry,
                    config={"urls": sample_urls},
                ),
                context=ConnectorContext(
                    run_id=run_id,
                    connector_key=verifier.manifest.connector_key,
                    connector_version=verifier.manifest.version,
                    allowed_domains=verifier.manifest.allowed_domains,
                ),
            )
            execution = SandboxExecution(
                job_id=f"discovery-{run_id}-wechat-url-verify-{index}",
                artifact=verifier,
                invocation=invocation,
                kind="sites",
                priority=self._sandbox_priority(run_id),
                expected_checksum=verifier.manifest.checksum,
                expected_signature=verifier.signature,
                timeout_seconds=max(
                    0.1,
                    min(budget.remaining(), self.settings.discovery_sandbox_timeout_seconds),
                ),
                purpose="url_verifier",
            )
            result = self._run_sandbox_execution(
                run_id,
                execution,
                budget=budget,
                phase="evaluate",
                round_number=1,
            )
            reachable.update(item.url for item in result.audit_output.items)
            evidence.extend(result.events)
            proofs.append(sandbox_execution_proof(result))
        return reachable, evidence, proofs

    def _run_sandbox_execution(
        self,
        run_id: int,
        execution: SandboxExecution,
        *,
        budget: _ExecutionBudget,
        phase: str,
        round_number: int,
    ) -> SandboxExecutionResult:
        budget.check()
        job_id = execution.job_id
        with self._lock:
            self._jobs.setdefault(run_id, set()).add(job_id)
        elapsed_snapshot = budget.elapsed()
        queued_at = budget.capacity_queued()
        append_discovery_event(
            run_id,
            event_type="sandbox_capacity_queued",
            summary="微信沙箱任务已进入容量队列，执行预算暂停计时。",
            phase=phase,
            round_number=round_number,
            payload={
                "maximum_seconds": budget.maximum_seconds,
                "elapsed_snapshot": elapsed_snapshot,
                "active_execution_elapsed_seconds": round(elapsed_snapshot, 3),
            },
        )
        self._set_status(run_id, "queued")
        try:
            def mark_started() -> None:
                queue_wait_seconds = round(budget.capacity_acquired(queued_at), 3)
                self._set_status(run_id, "running")
                append_discovery_event(
                    run_id,
                    event_type="sandbox_capacity_acquired",
                    summary="已获得 gVisor 沙箱容量，三十分钟总执行预算继续计时。",
                    phase=phase,
                    round_number=round_number,
                    payload={
                        "maximum_seconds": budget.maximum_seconds,
                        "already_elapsed_seconds": budget.elapsed(),
                        "active_execution_elapsed_seconds": round(budget.elapsed(), 3),
                        "queue_wait_seconds": queue_wait_seconds,
                    },
                )

            result = self.sandbox.execute(execution, on_started=mark_started)
            ensure_not_cancelled()
            budget.check()
            append_discovery_event(
                run_id,
                event_type="sandbox_tool_evidence",
                summary="微信共享采集器沙箱执行摘要已持久化。",
                phase=phase,
                round_number=round_number,
                payload={
                    "job_id": job_id,
                    "event_count": len(result.events),
                    "events": [
                        {
                            key: redact_discovery_data(event[key])
                            for key in ("event", "type", "level")
                            if isinstance(event, dict) and event.get(key) is not None
                        }
                        for event in result.events[:8]
                    ],
                },
            )
            return result
        finally:
            with self._lock:
                self._jobs.get(run_id, set()).discard(job_id)

    @staticmethod
    def _set_status(run_id: int, status: str) -> None:
        db = SessionLocal()
        try:
            run = db.get(SiteDiscoveryRun, run_id)
            if run is None:
                return
            target = "repairing" if run.trigger_type == "repair" else status
            db.execute(
                update(SiteDiscoveryRun)
                .where(
                    SiteDiscoveryRun.id == run_id,
                    SiteDiscoveryRun.status.in_(("queued", "running", "repairing")),
                )
                .values(status=target)
            )
            db.commit()
        finally:
            db.close()

    def _phase(
        self,
        run_id: int,
        phase: str,
        trace: list[dict[str, Any]],
        summary: str,
        *,
        budget: _ExecutionBudget,
    ) -> None:
        if phase not in WECHAT_PHASES:
            raise ValueError("invalid WeChat Discovery phase")
        active_elapsed = round(budget.elapsed(), 3)
        timing_payload: dict[str, Any] = {
            "source_kind": "wechat",
            "active_execution_elapsed_seconds": active_elapsed,
        }
        with self._lock:
            previous = self._phase_started_elapsed.get(run_id)
        if previous is not None:
            previous_phase, started_elapsed = previous
            completed_elapsed = max(0.0, round(active_elapsed - started_elapsed, 3))
            timing_payload.update({
                "completed_phase": previous_phase,
                "completed_phase_elapsed_seconds": completed_elapsed,
                "phase_elapsed_seconds": completed_elapsed,
                "timed_phase": previous_phase,
            })
        trace.append({"step": phase, "round": 1, "summary": summary, "source_kind": "wechat"})
        db = SessionLocal()
        try:
            changed = db.execute(
                update(SiteDiscoveryRun)
                .where(
                    SiteDiscoveryRun.id == run_id,
                    SiteDiscoveryRun.status.in_(("queued", "running", "repairing")),
                )
                .values(phase=phase, round=1, node_trace=list(trace))
            )
            if changed.rowcount != 1:
                raise DiscoveryCancelled("Discovery run 已取消")
            append_discovery_event(
                run_id,
                event_type="phase_changed",
                summary=summary,
                phase=phase,
                round_number=1,
                payload=timing_payload,
                session=db,
            )
            db.commit()
        finally:
            db.close()
        with self._lock:
            self._phase_started_elapsed[run_id] = (phase, round(budget.elapsed(), 3))

    def _current_timing_payload(
        self,
        run_id: int,
        *,
        budget: _ExecutionBudget,
    ) -> dict[str, Any]:
        active_elapsed = round(budget.elapsed(), 3)
        payload: dict[str, Any] = {
            "active_execution_elapsed_seconds": active_elapsed,
        }
        with self._lock:
            current = self._phase_started_elapsed.get(run_id)
        if current is not None:
            phase, started_elapsed = current
            payload.update({
                "timed_phase": phase,
                "phase_elapsed_seconds": max(0.0, round(active_elapsed - started_elapsed, 3)),
            })
        return payload

    def current_timing_payload(self, run_id: int) -> dict[str, Any]:
        """Return a bounded live timing snapshot for a user cancellation event."""
        with self._lock:
            budget = self._budgets.get(run_id)
        # A cancellation before the worker starts has no active execution time
        # and must not be approximated from wall time.
        return self._current_timing_payload(run_id, budget=budget) if budget is not None else {}

    @staticmethod
    def _sandbox_priority(run_id: int) -> SandboxJobPriority:
        db = SessionLocal()
        try:
            run = db.get(SiteDiscoveryRun, run_id)
            return (
                SandboxJobPriority.REPAIR
                if run is not None and run.trigger_type == "repair"
                else SandboxJobPriority.DISCOVERY
            )
        finally:
            db.close()

    def _checkpoint(
        self,
        run_id: int,
        *,
        phase: str,
        config: WechatSogouConfig,
        value: str,
        input_type: str,
        name: str,
        evidence: list[dict[str, Any]],
        evaluation: dict[str, Any],
        execution_result: dict[str, Any],
        artifact: ConnectorArtifact | None,
        elapsed_seconds: float,
    ) -> None:
        checkpoint = DiscoveryCheckpoint(
            run_id=run_id,
            phase=phase,
            round=1,
            processing_summary={
                "discovery_kind": "wechat",
                "wechat_input": value,
                "input_type": input_type,
                "display_name": name,
                "wechat_config": config.model_dump(mode="json"),
                "artifact": self._artifact_state(artifact) if artifact is not None else None,
            },
            tool_evidence=evidence[:100],
            execution_result=execution_result,
            evaluation_result=evaluation,
            elapsed_seconds=elapsed_seconds,
            runtime_version=self.settings.discovery_runtime_version,
        )
        db = SessionLocal()
        try:
            self.checkpoints.save(checkpoint, session=db)
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _artifact_state(artifact: ConnectorArtifact) -> dict[str, Any]:
        return {
            "connector_key": artifact.manifest.connector_key,
            "version": artifact.manifest.version,
            "checksum": artifact.manifest.checksum,
            "signature": artifact.signature,
            "runtime_version": artifact.manifest.runtime_version,
            "kind": "shared",
        }

    @classmethod
    def _execution_state(
        cls,
        artifact: ConnectorArtifact,
        outputs: list[ConnectorOutput],
        page_output: ConnectorOutput | None,
        reachable_urls: set[str],
        trial_attestations: list[dict[str, Any]],
        auxiliary_proofs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        trial_snapshots = [cls._checkpoint_output(output) for output in outputs[:2]]
        page_snapshot = cls._checkpoint_output(page_output) if page_output is not None else None
        state = {
            "artifact": cls._artifact_state(artifact),
            "trial_outputs": [snapshot.model_dump(mode="json") for snapshot in trial_snapshots],
            "trial_count": sum(snapshot.complete for snapshot in trial_snapshots),
            "page_one_request": {"page": 1, "max_pages": 1},
            "page_output": page_snapshot.model_dump(mode="json") if page_snapshot is not None else None,
            "page_two_request": {"page": 2, "max_pages": 1} if page_snapshot is not None else None,
            "reachable_urls": sorted(reachable_urls)[:30],
            "requires_url_verification": False,
            "trial_attestations": list(trial_attestations[:2]),
            "auxiliary_proofs": list(auxiliary_proofs or [])[:4],
        }
        candidates: list[tuple[str, int]] = []
        if page_snapshot is not None and page_snapshot.complete:
            candidates.append(("page", 0))
        candidates.extend(
            ("trial", index)
            for index, snapshot in reversed(list(enumerate(trial_snapshots)))
            if snapshot.complete
        )
        while cls._state_size(state) > WECHAT_EXECUTION_STATE_MAX_BYTES and candidates:
            kind, index = candidates.pop(0)
            incomplete = WechatCheckpointOutput(
                complete=False,
                item_count=(
                    page_snapshot.item_count if kind == "page" and page_snapshot is not None
                    else trial_snapshots[index].item_count
                ),
                incomplete_reason="complete output exceeded the checkpoint aggregate size budget; rerun required",
            ).model_dump(mode="json")
            if kind == "page":
                state["page_output"] = incomplete
            else:
                state["trial_outputs"][index] = incomplete
            state["trial_count"] = sum(
                WechatCheckpointOutput.model_validate(raw).complete
                for raw in state["trial_outputs"]
            )
        return state

    @staticmethod
    def _checkpoint_output(output: ConnectorOutput) -> WechatCheckpointOutput:
        item_count = len(output.items)
        if item_count > 50:
            return WechatCheckpointOutput(
                complete=False,
                item_count=item_count,
                incomplete_reason=f"connector returned {item_count} items; maximum restorable count is 50",
            )
        try:
            return WechatCheckpointOutput(
                complete=True,
                item_count=item_count,
                output=output,
            )
        except ValueError as exc:
            return WechatCheckpointOutput(
                complete=False,
                item_count=item_count,
                incomplete_reason=f"output is not safely restorable: {str(exc)[:350]}",
            )

    @staticmethod
    def _state_size(state: dict[str, Any]) -> int:
        return len(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    @classmethod
    def _validate_checkpoint_artifact(
        cls,
        checkpoint: DiscoveryCheckpoint | None,
        artifact: ConnectorArtifact,
    ) -> None:
        if checkpoint is None:
            return
        expected = checkpoint.processing_summary.get("artifact")
        if expected is None:
            expected = (checkpoint.execution_result or {}).get("artifact")
        if expected is None and checkpoint.phase == "context":
            return
        if expected != cls._artifact_state(artifact):
            raise ValueError("WeChat Discovery checkpoint artifact no longer matches the reviewed shared version")

    @staticmethod
    def _can_resume_package(checkpoint: DiscoveryCheckpoint | None) -> bool:
        return bool(
            checkpoint
            and checkpoint.phase in {"evaluate", "package"}
            and (
                (checkpoint.evaluation_result or {}).get("passed") is True
                or ((checkpoint.evaluation_result or {}).get("plugin_review") or {}).get(
                    "package_eligible"
                ) is True
            )
        )

    @staticmethod
    def _load_trace(run_id: int) -> list[dict[str, Any]]:
        db = SessionLocal()
        try:
            run = db.get(SiteDiscoveryRun, run_id)
            return list(run.node_trace or []) if run is not None else []
        finally:
            db.close()

    def _package(
        self,
        *,
        run_id: int,
        artifact: ConnectorArtifact,
        value: str,
        input_type: str,
        name: str,
        config: WechatSogouConfig,
        force: bool,
        budget: _ExecutionBudget,
        trace: list[dict[str, Any]],
        plugin_review: dict[str, Any],
    ) -> int:
        """Atomically create/reuse one pending configuration and complete its run."""
        recipe = ConfiguredPluginRecipe(manifest=artifact.manifest, config=config)
        signature = configured_plugin_signature(recipe)
        domain = _wechat_domain(value, input_type)
        del force  # force never mutates an active rollback version.

        db = SessionLocal()
        dialect = db.get_bind().dialect.name
        # SQLite has a database-wide writer, while PostgreSQL and other
        # backends serialize only equal domains so unrelated packaging stays
        # parallel. The weak map releases inactive domain keys automatically.
        process_lock = (
            _WECHAT_SQLITE_PACKAGE_LOCK
            if dialect == "sqlite"
            else _domain_package_lock(domain)
        )
        acquired = False
        try:
            while not process_lock.acquire(
                timeout=min(0.05, max(0.001, budget.remaining()))
            ):
                ensure_not_cancelled()
                budget.check()
            acquired = True
            ensure_not_cancelled()
            budget.check()
            try:
                if dialect == "sqlite":
                    # Cross-process SQLite writers serialize before inspecting
                    # candidates, so a second process observes the first commit.
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                elif dialect == "postgresql":
                    while not bool(
                        db.scalar(
                            select(
                                text("pg_try_advisory_xact_lock(hashtext(:domain))")
                            ).params(domain=domain)
                        )
                    ):
                        ensure_not_cancelled()
                        budget.check()
                        time.sleep(min(0.05, budget.remaining()))

                locked_run = db.scalar(
                    select(SiteDiscoveryRun)
                    .where(SiteDiscoveryRun.id == run_id)
                    .with_for_update()
                )
                if locked_run is None or locked_run.status not in {"queued", "running", "repairing"}:
                    raise DiscoveryCancelled("Discovery run 已取消或不再允许封装")
                ensure_not_cancelled()
                budget.check()

                method = None
                pending_candidates = list(
                    db.scalars(
                        select(CrawlMethod)
                        .where(
                            CrawlMethod.domain == domain,
                            CrawlMethod.signature == signature,
                            CrawlMethod.status == "pending",
                            CrawlMethod.review_status == REVIEW_PENDING,
                        )
                        .order_by(CrawlMethod.id)
                        .with_for_update()
                    )
                )
                for candidate in pending_candidates:
                    stored = candidate.dsl_recipe or {}
                    if (
                        stored.get("recipe_type") == "python_plugin"
                        and stored.get("connector_kind") == "shared"
                        and (stored.get("manifest") or {}).get("connector_key") == SHARED_CONNECTOR_KEY
                    ):
                        method = candidate
                        break
                if method is None:
                    source = Source(
                        name=name[:200],
                        type=SourceType.DISCOVERY.value,
                        url=value,
                        main_category="OS跟踪来源",
                        stream=Stream.NEWS.value,
                        enabled=False,
                    )
                    db.add(source)
                    db.flush()
                    method = CrawlMethod(
                        domain=domain,
                        entry_url=value,
                        source_id=source.id,
                        dsl_recipe=recipe.model_dump(mode="json"),
                        signature=signature,
                        status="pending",
                        review_status=REVIEW_PENDING,
                    )
                    db.add(method)
                    db.flush()

                attach_plugin_review_evidence(
                    method,
                    method_audit=bind_packaged_artifact_evidence(
                        dict(plugin_review.get("method_audit") or {}),
                        trial=artifact,
                        packaged=artifact,
                        kind="shared",
                        packaging_run_id=run_id,
                    ),
                    quality_audit=quality_audit_from_evidence(
                        dict(plugin_review.get("quality_audit") or {})
                    ),
                    quality_trial_evidence=list(plugin_review.get("quality_trials") or []),
                )
                db.flush()

                ensure_not_cancelled()
                budget.check()
                completed = db.execute(
                    update(SiteDiscoveryRun)
                    .where(
                        SiteDiscoveryRun.id == run_id,
                        SiteDiscoveryRun.status.in_(("queued", "running", "repairing")),
                    )
                    .values(
                        status="completed",
                        resulting_method_id=method.id,
                        node_trace=trace,
                        phase="package",
                        round=1,
                        runtime_version=artifact.manifest.runtime_version,
                        llm_token_usage=0,
                        error_message=None,
                        ended_at=datetime.now(timezone.utc),
                    )
                )
                if completed.rowcount != 1:
                    raise DiscoveryCancelled("Discovery run 在封装前已取消")
                append_discovery_event(
                    run_id,
                    event_type="wechat_plugin_packaged",
                    summary="共享采集器配置已完成双试跑证据并进入待审核。",
                    phase="package",
                    round_number=1,
                    payload={
                        "method_id": method.id,
                        "connector_key": SHARED_CONNECTOR_KEY,
                        "review_status": REVIEW_PENDING,
                        **self._current_timing_payload(run_id, budget=budget),
                    },
                    session=db,
                )
                ensure_not_cancelled()
                budget.check()
                db.commit()
                return method.id
            except BaseException:
                db.rollback()
                raise
        finally:
            db.close()
            if acquired:
                process_lock.release()


def _wechat_domain(value: str, input_type: str) -> str:
    if input_type == "wechat_search":
        return f"wechat_search_{hashlib.sha256(value.encode()).hexdigest()[:16]}"
    if not urlsplit(value).scheme and len(value) <= 240:
        return f"wechat_mp_{value}"
    return f"wechat_mp_{hashlib.sha256(value.encode()).hexdigest()[:16]}"


_engine: WechatDiscoveryEngine | None = None
_engine_lock = threading.Lock()


def get_wechat_discovery_engine() -> WechatDiscoveryEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = WechatDiscoveryEngine()
        return _engine


def start_wechat_discovery_run(
    value: str,
    *,
    input_type: str,
    force: bool,
    name: str,
    hints: dict[str, Any] | None,
) -> int:
    return get_wechat_discovery_engine().start(
        value,
        input_type=input_type,
        force=force,
        name=name,
        hints=hints,
    )


def start_wechat_repair_run(method_id: int) -> int:
    """Start one bounded deterministic repair for an approved WeChat configuration."""
    return get_wechat_discovery_engine().start_repair(method_id)


def cancel_wechat_discovery_run(run_id: int) -> bool:
    with _engine_lock:
        engine = _engine
    return engine.cancel(run_id) if engine is not None else False


def wechat_discovery_timing_payload(run_id: int) -> dict[str, Any]:
    """Read the live queue-excluding timing snapshot without creating an engine."""
    with _engine_lock:
        engine = _engine
    return engine.current_timing_payload(run_id) if engine is not None else {}
