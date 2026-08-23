"""Program-controlled Single Agent Loop for ordinary website Discovery."""

from __future__ import annotations

import difflib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from itertools import count
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import select, update

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
from app.discovery.loop.agent import WebsiteConnectorAgent
from app.discovery.loop.artifacts import (
    activate_packaging_method,
    create_packaging_method,
    discard_packaging_method,
    discard_staged_artifact,
    publish_staged_artifact,
    stage_pending_artifact,
    write_trial_artifact,
)
from app.discovery.loop.evaluator import EvaluationResult, evaluate_connector_outputs
from app.discovery.loop.explore_tools import (
    bounded_observation,
    build_explore_tool_source,
    explore_action_fingerprint,
    explore_evidence_tokens,
    explore_strategy_family,
    probe_source_summary,
    validate_explore_decision,
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
    redact_discovery_data,
    redact_discovery_text,
    validate_discovery_data_bounds,
)
from app.discovery.runtime import finish_discovery_run
from app.discovery.sandbox.capacity import SandboxJobPriority
from app.discovery.sandbox.runtime import SandboxExecution, SandboxExecutionResult, SandboxRuntime
from app.llm.usage import UsageScope, activate_usage_scope, deactivate_usage_scope
from app.models import CrawlMethod, CrawlMethodRun, SiteDiscoveryRun


logger = logging.getLogger(__name__)
PHASES = ("context", "explore", "build", "execute", "evaluate", "repair", "package")
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
        self.maximum_seconds = min(1200.0, max(0.1, maximum_seconds))
        self.consumed_seconds = max(0.0, consumed_seconds)
        self.deadline: float | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self.deadline is None:
                remaining = self.maximum_seconds - self.consumed_seconds
                if remaining <= 0:
                    raise TimeoutError("Discovery Loop 已达到 15 分钟执行预算。")
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
            raise TimeoutError("Discovery Loop 已达到 15 分钟执行预算。")

    def elapsed(self) -> float:
        with self._lock:
            if self.deadline is None:
                return self.consumed_seconds
            return min(self.maximum_seconds, self.maximum_seconds - max(0.0, self.deadline - time.monotonic()))


class WebsiteLoopEngine:
    """One Agent, deterministic phases, bounded rounds, durable evidence and review gate."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        sandbox: SandboxRuntime | None = None,
        agent: WebsiteConnectorAgent | None = None,
        retriever: DiscoveryExperienceRetriever | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.sandbox = sandbox or SandboxRuntime(settings=self.settings)
        self.agent = agent or WebsiteConnectorAgent()
        self.retriever = retriever or DiscoveryExperienceRetriever()
        self.checkpoints = checkpoint_store or CheckpointStore(self.settings)
        self._run_jobs: dict[int, set[str]] = {}
        self._deadlines: dict[int, _LoopDeadline] = {}
        self._job_lock = threading.Lock()

    def start(self, site_url: str, *, force: bool = True, name: str | None = None) -> int:
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

    def run(
        self,
        *,
        run_id: int,
        site_url: str,
        force: bool,
        name: str | None,
        resume_checkpoint: DiscoveryCheckpoint | None = None,
        initial_source: str | None = None,
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
                initial_error=initial_error,
                usage=usage,
            )
        except DiscoveryCancelled as exc:
            finish_discovery_run(
                run_id,
                status="cancelled",
                error_message=str(exc),
                llm_token_usage=usage.total_tokens,
            )
        except Exception as exc:  # initialization failures converge here
            terminal_error = _structured_error(exc)
            terminal_error["stage"] = "initialization"
            safe_error = terminal_error["message"]
            logger.exception("website Discovery initialization failed run_id=%s", run_id)
            self._terminal_event(
                run_id, "run_failed", "Single Agent Loop 初始化失败并停止。",
                "context", 0, {"error": terminal_error}, level="error",
            )
            finish_discovery_run(
                run_id,
                status="failed",
                error_message=safe_error,
                llm_token_usage=usage.total_tokens,
            )
        finally:
            with self._job_lock:
                self._run_jobs.pop(run_id, None)
                self._deadlines.pop(run_id, None)
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
        initial_error: dict[str, Any] | None,
        usage: UsageScope,
    ) -> None:
        trace: list[dict[str, Any]] = []
        deadline = _LoopDeadline(
            self.settings.discovery_loop_max_seconds,
            consumed_seconds=float(resume_checkpoint.elapsed_seconds if resume_checkpoint else 0.0),
        )
        with self._job_lock:
            self._deadlines[run_id] = deadline
        if resume_checkpoint is not None and resume_checkpoint.elapsed_seconds > 0:
            deadline.start()
        previous_source = initial_source or self._load_resume_source(resume_checkpoint)
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
        last_error: dict[str, Any] | None = initial_error
        last_tool_evidence: list[dict[str, Any]] = []
        last_evaluation: EvaluationResult | None = None
        try:
            if resume_package and resume_checkpoint is not None:
                trial = self._load_resume_trial(resume_checkpoint)
                self._mark_sandbox_started(run_id)
                self._phase(
                    run_id,
                    "package",
                    resume_checkpoint.round,
                    trace,
                    "从已通过确定性验收的检查点继续封装。",
                )
                self._package_trial(
                    run_id=run_id,
                    trial=trial,
                    display_name=name or (urlsplit(site_url).hostname or "website"),
                    force=force,
                    round_number=resume_checkpoint.round,
                    trace=trace,
                    token_usage=usage.total_tokens,
                    deadline=deadline,
                    plugin_review=previous_evaluation.get("plugin_review") or {},
                )
                return
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

            if not exploration.get("sandbox_probe") or not exploration.get("pagination_strategy"):
                self._phase(run_id, "explore", start_round - 1, trace, "在 gVisor 沙箱中探查公开入口。")
                exploration.update(self._explore(
                    run_id=run_id,
                    site_url=site_url,
                    round_number=start_round - 1,
                    rag_references=explore_references,
                    deadline=deadline,
                ))

            # Rounds are intentionally unbounded. The shared monotonic Loop
            # deadline is the only execution limit, so every retry still
            # consumes the same 20-minute budget (capacity waits excluded).
            for round_number in count(start_round):
                ensure_not_cancelled()
                self._guard_active(run_id, deadline)
                phase = "repair" if previous_source or last_error else "build"
                self._phase(run_id, phase, round_number, trace,
                            "根据真实执行与确定性校验反馈修复采集器。" if previous_source else "编写首版 Python 采集器。")
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
                            "extraction_strategy": exploration.get("extraction_strategy"),
                            "pagination_strategy": exploration.get("pagination_strategy"),
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
                try:
                    draft = self.agent.build_or_repair(
                        site_url=site_url,
                        runtime_version=self.settings.discovery_runtime_version,
                        round_number=round_number,
                        exploration=exploration,
                        rag_references=stage_references,
                        previous_source=previous_source,
                        evaluation_failures=list(previous_evaluation.get("failures") or []),
                        execution_error=(
                            {"error": last_error, "tool_evidence": last_tool_evidence}
                            if last_error or last_tool_evidence else None
                        ),
                        timeout_seconds=deadline.remaining(),
                        session_id=run_id,
                    )
                    _validate_draft_extraction_strategy(
                        draft.crawler_py,
                        exploration.get("extraction_strategy"),
                    )
                    _validate_draft_pagination_strategy(
                        draft.crawler_py,
                        draft.supports_pagination,
                        exploration.get("pagination_strategy"),
                    )
                except Exception as exc:  # malformed model output is repair evidence, not success
                    if isinstance(exc, (TimeoutError, DiscoveryCancelled)):
                        raise
                    last_error = {
                        "type": type(exc).__name__,
                        "message": redact_discovery_text(str(exc))[:4000],
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
                    self._guard_active(run_id, deadline)
                    continue
                self._guard_active(run_id, deadline)
                try:
                    trial = write_trial_artifact(
                        run_id=run_id,
                        site_url=site_url,
                        source=draft.crawler_py,
                        allowed_domains=draft.allowed_domains,
                        runtime_version=self.settings.discovery_runtime_version,
                        version=round_number,
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
                try:
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
                    if draft.supports_pagination:
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

                    self._phase(run_id, "evaluate", round_number, trace, "程序执行第 9 节确定性验收。")
                    self._guard_active(run_id, deadline)
                    last_evaluation = evaluate_connector_outputs(
                        outputs,
                        reachable_urls=reachable,
                        supports_pagination=draft.supports_pagination,
                        pagination_output=pagination_output,
                        require_url_accessibility=False,
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
                                evaluation_dict, level="info" if evaluation_dict["passed"] else "warning")
                    last_tool_evidence = [bounded_observation(event) for event in round_tool_evidence[:50]]
                except (ConnectorProtocolError, TimeoutError, RuntimeError, ValueError) as exc:
                    if isinstance(exc, TimeoutError):
                        raise
                    if isinstance(exc, ConnectorProtocolError):
                        if exc.code == ConnectorErrorCode.CANCELLED:
                            raise DiscoveryCancelled("用户已取消智能探查") from exc
                        if exc.code == ConnectorErrorCode.RUNTIME_ERROR:
                            raise
                    last_error = _structured_error(exc)
                    last_tool_evidence = [bounded_observation(event) for event in round_tool_evidence[:50]]
                    if not isinstance(exc, ConnectorProtocolError):
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
                    previous_evaluation = {"passed": False, "failures": [
                        {"check": "sandbox_execution", "passed": False, **last_error}
                    ]}
                    self._event(run_id, "sandbox_execution_failed", "沙箱真实执行失败。", "execute", round_number,
                                last_error, level="error")

                exploration["technical_features"] = draft.technical_features
                exploration["supports_pagination"] = draft.supports_pagination
                self._save_round_checkpoint(
                    run_id=run_id,
                    phase=(
                        "evaluate"
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
                    execution_result={
                        "successful_runs": len(outputs),
                        "trial_outputs": [output.model_dump(mode="json") for output in outputs],
                        "pagination_output": (
                            pagination_output.model_dump(mode="json")
                            if pagination_output is not None else None
                        ),
                        "reachable_urls": sorted(reachable),
                        "requires_url_verification": False,
                        "supports_pagination": draft.supports_pagination,
                        "auxiliary_proofs": auxiliary_proofs,
                    },
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
                    self._phase(run_id, "package", round_number, trace, "固化新版本并进入待审核；不自动发布。")
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
                    )
                    return
                previous_source = draft.crawler_py
                self._guard_active(run_id, deadline)

        except DiscoveryCancelled as exc:
            self._save_initial_failure_checkpoint(run_id, usage.total_tokens, deadline.elapsed(), str(exc))
            finish_discovery_run(run_id, status="cancelled", node_trace=trace,
                                 error_message=str(exc), llm_token_usage=usage.total_tokens)
        except Exception as exc:  # noqa: BLE001 - terminal state must always converge
            terminal_error = _structured_error(exc)
            terminal_error["stage"] = "repair" if previous_source else "explore"
            safe_error = terminal_error["message"]
            logger.exception("website Discovery Loop failed run_id=%s", run_id)
            self._save_initial_failure_checkpoint(run_id, usage.total_tokens, deadline.elapsed(), safe_error)
            self._terminal_event(run_id, "run_failed", "Single Agent Loop 失败并停止。",
                                 "repair" if previous_source else "context", max(0, start_round - 1),
                                 {"error": terminal_error}, level="error")
            finish_discovery_run(run_id, status="failed", node_trace=trace,
                                 error_message=safe_error, llm_token_usage=usage.total_tokens)
        finally:
            with self._job_lock:
                self._run_jobs.pop(run_id, None)
                self._deadlines.pop(run_id, None)
            self.agent.end_session(run_id)

    def _explore(
        self,
        *,
        run_id: int,
        site_url: str,
        round_number: int,
        rag_references: list[dict[str, Any]],
        deadline: _LoopDeadline,
    ) -> dict[str, Any]:
        """Let the same Agent choose bounded tools; execute every action in gVisor."""
        observations: list[dict[str, Any]] = []
        files: dict[str, str] = {}
        executed_actions = 0
        action_fingerprints: set[str] = set()
        evidence_tokens: set[str] = set()
        consecutive_no_gain = 0
        blocked_family: str | None = None
        for action_number in count(1):
            self._guard_active(run_id, deadline)
            decide = getattr(self.agent, "decide_explore_native", self.agent.decide_explore)
            try:
                decision = decide(
                    site_url=site_url,
                    runtime_version=self.settings.discovery_runtime_version,
                    rag_references=rag_references,
                    observations=observations,
                    remaining_actions=None,
                    timeout_seconds=deadline.remaining(),
                    session_id=run_id,
                )
                validate_explore_decision(decision, site_url=site_url)
            except (ValueError, ValidationError) as exc:
                observation = {
                    "action_number": action_number,
                    "action": "agent_tool_call",
                    "error": {
                        "type": type(exc).__name__,
                        "message": redact_discovery_text(str(exc))[:4000],
                        "stage": "explore_decision",
                    },
                    "repair_hint": "修正工具名、参数或 probe.py；此错误不会终止 Explore。",
                }
                observations.append(observation)
                self._event(
                    run_id,
                    "explore_tool_observation",
                    "Explore 工具调用格式错误，已反馈 Agent 继续修正。",
                    "explore",
                    round_number,
                    observation,
                    level="warning",
                )
                continue
            if decision.action != "finish":
                fingerprint = explore_action_fingerprint(decision)
                family = explore_strategy_family(decision)
                rejection: str | None = None
                if fingerprint in action_fingerprints:
                    rejection = "完全相同的 Explore 动作已经执行过；必须复用已有证据并更换方法。"
                elif blocked_family == family:
                    rejection = (
                        f"{family} 连续没有产生新证据；本次不再启动沙箱。"
                        "请切换到另一策略（文档解析、脚本搜索或候选 API 验证）。"
                    )
                if rejection is not None:
                    observation = {
                        "action_number": action_number,
                        "action": decision.action,
                        "strategy_family": family,
                        "error": {
                            "type": "ExploreStrategyRejected",
                            "message": rejection,
                            "stage": "explore",
                        },
                    }
                    observations.append(observation)
                    self._event(run_id, "explore_tool_observation", "重复或无增益的探查已被系统阻止。",
                                "explore", round_number, observation, level="warning")
                    continue
                action_fingerprints.add(fingerprint)
            self._event(
                run_id,
                "explore_action_selected",
                decision.action_summary,
                "explore",
                round_number,
                {"action": decision.action, "args": probe_source_summary(decision),
                 "allowed_domains": decision.allowed_domains, "action_number": action_number},
            )
            if decision.action == "finish":
                if executed_actions == 0:
                    rejection = "Explore cannot finish before a real sandbox tool action"
                    observations.append({
                        "action_number": action_number,
                        "action": "finish",
                        "error": {"type": "ExploreDecisionRejected", "message": rejection, "stage": "explore"},
                    })
                    self._event(run_id, "explore_tool_observation", "Explore 结束条件不足，继续探查。",
                                "explore", round_number, observations[-1], level="warning")
                    continue
                extraction_strategy, strategy_errors = _latest_extraction_strategy(observations)
                pagination_strategy, pagination_errors = _latest_pagination_strategy(observations)
                if pagination_strategy is None:
                    rejection = "Explore cannot finish without a verified pagination_strategy"
                    observations.append({
                        "action_number": action_number,
                        "action": "finish",
                        "error": {
                            "type": "ExploreDecisionRejected",
                            "message": rejection,
                            "stage": "explore",
                        },
                        "strategy_validation_errors": pagination_errors[-3:],
                        "repair_hint": (
                            "用 probe 真实验证第二页、游标、next、滚动新增，或确认固定单页后返回 "
                            "pagination_strategy；格式错误不会终止 Loop。"
                        ),
                    })
                    self._event(run_id, "explore_tool_observation", "分页机制尚未验证，继续探查。",
                                "explore", round_number, observations[-1], level="warning")
                    continue
                summary_only = decision.technical_features.get("summary_only") is True
                summary_validation = None
                if summary_only and extraction_strategy is None:
                    try:
                        summary_validation = _latest_summary_validation(observations)
                        _validate_summary_only_threshold(summary_validation)
                    except ValueError as exc:
                        rejection = str(exc)
                        observations.append({
                            "action_number": action_number,
                            "action": "finish",
                            "error": {
                                "type": "ExploreDecisionRejected",
                                "message": rejection,
                                "stage": "explore",
                            },
                            "repair_hint": "继续探查详情正文，或重新用 probe 统计摘要覆盖率。",
                        })
                        self._event(run_id, "explore_tool_observation", "summary_only 条件不足，继续探查。",
                                    "explore", round_number, observations[-1], level="warning")
                        continue
                if extraction_strategy is None and not summary_only:
                    rejection = (
                        "summary coverage is below 70% and no valid verified extraction_strategy was provided"
                    )
                    observations.append({
                        "action_number": action_number,
                        "action": "finish",
                        "error": {
                            "type": "ExploreDecisionRejected",
                            "message": rejection,
                            "stage": "explore",
                        },
                        "strategy_validation_errors": strategy_errors[-3:],
                        "repair_hint": (
                            "继续 Explore，并让 probe 返回 extraction_strategy 对象；"
                            "格式错误不能结束任务，也不会终止整个 Loop。"
                        ),
                    })
                    self._event(run_id, "explore_tool_observation", "详情提取策略尚未验证，继续探查。",
                                "explore", round_number, observations[-1], level="warning")
                    continue
                technical_features = {
                    **decision.technical_features,
                    "supports_pagination": pagination_strategy["supports_pagination"],
                }
                return {
                    "sandbox_probe": {"observations": observations},
                    "technical_features": technical_features,
                    "exploration_summary": decision.exploration_summary,
                    "extraction_strategy": extraction_strategy,
                    "pagination_strategy": pagination_strategy,
                    "summary_validation": summary_validation,
                    "tool_action_count": executed_actions,
                }
            artifact = write_trial_artifact(
                run_id=run_id,
                site_url=site_url,
                source=build_explore_tool_source(decision),
                allowed_domains=decision.allowed_domains,
                runtime_version=self.settings.discovery_runtime_version,
                connector_key=f"{_connector_base_key(site_url)[:56]}_tools",
                version=action_number,
                settings=self.settings,
            )
            try:
                result = self._execute(
                    run_id,
                    "explore",
                    round_number,
                    artifact,
                    request=ConnectorRequest(
                        entry=site_url,
                        config={
                            "action": decision.action,
                            "args": decision.args if decision.action != "probe" else {},
                            "objective": str(decision.args.get("objective") or decision.action_summary)[:2000],
                            "files": files,
                        },
                    ),
                    suffix=f"tool-{action_number}",
                    deadline=deadline,
                )
                stats = result.audit_output.stats.model_dump(mode="json")
                raw_result = stats.get("observation")
                if isinstance(raw_result, dict):
                    trace = raw_result.get("trace")
                    if isinstance(trace, list):
                        # The first 40 steps stream from the sandbox while it is
                        # running. Persist only the remainder here to avoid duplicates.
                        for step in trace[40:200]:
                            if not isinstance(step, dict):
                                continue
                            operation = str(step.get("operation") or "probe.step")[:100]
                            self._event(
                                run_id,
                                "explore_probe_step",
                                f"Probe 执行：{operation}",
                                "explore",
                                round_number,
                                {
                                    "action_number": action_number,
                                    "sequence": step.get("sequence"),
                                    "operation": operation,
                                    "details": step.get("details") if isinstance(step.get("details"), dict) else {},
                                },
                            )
                observation = bounded_observation({
                    "action_number": action_number,
                    "action": decision.action,
                    "result": stats.get("observation"),
                    "sandbox_events": list(result.events),
                })
                raw_files = stats.get("files") or {}
                if isinstance(raw_files, dict):
                    files = {str(key): str(value)[:65536] for key, value in list(raw_files.items())[:20]}
            except ConnectorProtocolError as exc:
                if exc.code == ConnectorErrorCode.CANCELLED:
                    raise DiscoveryCancelled("用户已取消智能探查") from exc
                tool_error = _structured_error(exc)
                tool_error["stage"] = "explore"
                self._event(
                    run_id,
                    "sandbox_execution_failed",
                    "Explore 沙箱工具执行失败。",
                    "explore",
                    round_number,
                    {
                        "error": tool_error,
                        "action": decision.action,
                        "action_number": action_number,
                    },
                    level="error",
                )
                if exc.code == ConnectorErrorCode.RUNTIME_ERROR:
                    raise
                observation = {
                    "action_number": action_number,
                    "action": decision.action,
                    "error": tool_error,
                }
            observations.append(observation)
            executed_actions += 1
            new_tokens = explore_evidence_tokens(observation) - evidence_tokens
            evidence_tokens.update(new_tokens)
            family = explore_strategy_family(decision)
            if new_tokens:
                consecutive_no_gain = 0
                blocked_family = None
            else:
                consecutive_no_gain += 1
                if consecutive_no_gain >= 2:
                    blocked_family = family
                    observation["convergence_guard"] = {
                        "new_evidence_count": 0,
                        "consecutive_no_gain": consecutive_no_gain,
                        "blocked_strategy_family": family,
                        "required_next_step": "switch_strategy",
                    }
            self._event(run_id, "explore_tool_observation", "受控 Explore 工具返回真实观察。",
                        "explore", round_number, observation,
                        level="warning" if "error" in observation else "info")

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
                else "explore_tool" if suffix.startswith("tool-")
                else "other"
            ),
            trial_index=(int(suffix[-1]) if suffix in {"trial-1", "trial-2"} else None),
        )
        with self._job_lock:
            self._run_jobs.setdefault(run_id, set()).add(job_id)
        elapsed_snapshot = deadline.pause_for_capacity()
        append_discovery_event(
            run_id,
            event_type="sandbox_capacity_queued",
            summary="沙箱任务已进入容量队列，执行预算暂停计时。",
            phase=phase,
            round_number=round_number,
            payload={
                "maximum_seconds": deadline.maximum_seconds,
                "elapsed_snapshot": elapsed_snapshot,
            },
        )
        self._mark_waiting_for_sandbox(run_id)
        try:
            result = self.sandbox.execute(
                execution,
                on_event=(
                    lambda event: self._persist_live_probe_step(
                        run_id, round_number, event
                    )
                    if phase == "explore" and suffix.startswith("tool-")
                    else None
                ),
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
        finally:
            with self._job_lock:
                self._run_jobs.get(run_id, set()).discard(job_id)

    def _persist_live_probe_step(
        self,
        run_id: int,
        round_number: int,
        event: dict[str, Any],
    ) -> None:
        """Persist only the system SDK's bounded progress marker from stderr."""
        message = event.get("message")
        if not isinstance(message, str) or not message.startswith("PROBE_STEP "):
            return
        try:
            step = json.loads(message.removeprefix("PROBE_STEP "))
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if not isinstance(step, dict):
            return
        sequence = step.get("sequence")
        operation = step.get("operation")
        details = step.get("details")
        if not isinstance(sequence, int) or not 1 <= sequence <= 40:
            return
        if not isinstance(operation, str) or not operation or len(operation) > 100:
            return
        self._event(
            run_id,
            "explore_probe_step",
            f"Probe 执行：{operation}",
            "explore",
            round_number,
            {
                "sequence": sequence,
                "operation": operation,
                "details": details if isinstance(details, dict) else {},
            },
        )

    def _phase(self, run_id: int, phase: str, round_number: int,
               trace: list[dict[str, Any]], summary: str) -> None:
        self._check_registered_deadline(run_id)
        if phase not in PHASES:
            raise ValueError(f"invalid Loop phase: {phase}")
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
            append_discovery_event(run_id, event_type="phase_changed", summary=summary,
                                   phase=phase, round_number=round_number, session=db)
            db.commit()
        finally:
            db.close()
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

    @staticmethod
    def _terminal_event(
        run_id: int,
        event_type: str,
        summary: str,
        phase: str,
        round_number: int,
        payload: dict[str, Any] | None = None,
        level: str = "info",
    ) -> None:
        """Persist only the terminal diagnosis after timeout/cancel cleanup begins."""
        append_discovery_event(run_id, event_type=event_type, summary=summary, phase=phase,
                               round_number=round_number, payload=payload, level=level)

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
        append_discovery_event(
            run_id,
            event_type="sandbox_capacity_acquired",
            summary="已获得 gVisor 沙箱容量，二十分钟总执行预算继续计时。",
            phase=phase,
            round_number=round_number,
            payload={
                "maximum_seconds": deadline.maximum_seconds,
                "already_elapsed_seconds": deadline.elapsed(),
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
    ) -> None:
        """Package a previously evaluated trial; shared by round-5 resume."""
        staged = None
        method_id: int | None = None
        try:
            staged = stage_pending_artifact(trial=trial, settings=self.settings)
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
                    select(SiteDiscoveryRun).where(SiteDiscoveryRun.id == run_id).with_for_update()
                )
                if locked_run is None or locked_run.status not in {"running", "repairing"}:
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
            publish_staged_artifact(staged)
            self._guard_active(run_id, deadline)
            final_db = SessionLocal()
            try:
                locked_run = final_db.scalar(
                    select(SiteDiscoveryRun).where(SiteDiscoveryRun.id == run_id).with_for_update()
                )
                method = final_db.scalar(
                    select(CrawlMethod).where(CrawlMethod.id == method_id).with_for_update()
                )
                if (
                    locked_run is None
                    or locked_run.status not in {"running", "repairing"}
                    or method is None
                    or method.status != "packaging"
                ):
                    raise DiscoveryCancelled("Discovery run 在制品发布前已取消")
                activate_packaging_method(final_db, method)
                self._guard_active(run_id, deadline)
                completed = final_db.execute(
                    update(SiteDiscoveryRun)
                    .where(
                        SiteDiscoveryRun.id == run_id,
                        SiteDiscoveryRun.status.in_(("running", "repairing")),
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
                    payload={"method_id": method_id, "review_status": "pending"},
                    session=final_db,
                )
                self._guard_active(run_id, deadline)
                final_db.commit()
            except BaseException:
                final_db.rollback()
                raise
            finally:
                final_db.close()
        except BaseException:
            if method_id is not None and self._package_is_committed(run_id, method_id):
                return
            cleanup_db = SessionLocal()
            try:
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
            except BaseException:
                cleanup_db.rollback()
                logger.exception("failed to compensate package method_id=%s", method_id)
                raise
            finally:
                cleanup_db.close()
            raise

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
    ) -> None:
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
                "sandbox_probe": exploration.get("sandbox_probe"),
                "sandbox_probe_error": exploration.get("sandbox_probe_error"),
                "technical_features": exploration.get("technical_features") or {},
                "supports_pagination": bool(exploration.get("supports_pagination")),
                "extraction_strategy": exploration.get("extraction_strategy"),
                "pagination_strategy": exploration.get("pagination_strategy"),
                "summary_validation": exploration.get("summary_validation"),
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


def start_website_loop_run(site_url: str, *, force: bool = True, name: str | None = None) -> int:
    return get_website_loop_engine().start(site_url, force=force, name=name)


def cancel_website_loop_run(run_id: int) -> bool:
    # Cancellation must not bootstrap a new engine (or query its process DB) for
    # a run that belongs to another route/test session.
    with _engine_lock:
        engine = _engine
    return engine.cancel(run_id) if engine is not None else False


def _connector_base_key(site_url: str) -> str:
    from app.discovery.loop.artifacts import connector_key_for_url

    return connector_key_for_url(site_url)


def _host_allowed(url: str, allowed_domains: tuple[str, ...]) -> bool:
    try:
        host = (urlsplit(url).hostname or "").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return False
    return host in allowed_domains


def _code_diff(previous: str | None, current: str) -> str:
    return "\n".join(difflib.unified_diff(
        (previous or "").splitlines(), current.splitlines(),
        fromfile="previous/crawler.py", tofile="current/crawler.py", lineterm="",
    ))


def _normalize_extraction_strategy(raw: Any) -> dict[str, Any]:
    """Accept any bounded extraction evidence; runtime trials certify behavior."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("extraction_strategy must be a non-empty object")
    validate_discovery_data_bounds(
        raw,
        max_depth=12,
        max_nodes=2_000,
        max_string_chars=16_000,
        max_approx_bytes=128 * 1024,
    )
    return dict(raw)


def _find_extraction_strategies(value: Any, *, depth: int = 0) -> list[Any]:
    if depth > 10:
        return []
    if isinstance(value, dict):
        found = [value["extraction_strategy"]] if "extraction_strategy" in value else []
        for nested in value.values():
            found.extend(_find_extraction_strategies(nested, depth=depth + 1))
        return found
    if isinstance(value, list):
        found: list[Any] = []
        for nested in value:
            found.extend(_find_extraction_strategies(nested, depth=depth + 1))
        return found
    return []


def _latest_extraction_strategy(
    observations: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    for observation in reversed(observations):
        candidates = _find_extraction_strategies(observation)
        for candidate in reversed(candidates):
            if candidate is None:
                errors.append("extraction_strategy was null")
                continue
            try:
                return _normalize_extraction_strategy(candidate), errors
            except ValueError as exc:
                errors.append(str(exc))
    return None, errors


def _latest_summary_validation(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    def find(value: Any, depth: int = 0) -> list[Any]:
        if depth > 10:
            return []
        if isinstance(value, dict):
            found = [value["summary_validation"]] if "summary_validation" in value else []
            for nested in value.values():
                found.extend(find(nested, depth + 1))
            return found
        if isinstance(value, list):
            found: list[Any] = []
            for nested in value:
                found.extend(find(nested, depth + 1))
            return found
        return []

    for observation in reversed(observations):
        candidates = find(observation)
        if candidates:
            candidate = candidates[-1]
            return candidate if isinstance(candidate, dict) else None
    return None


def _validate_draft_extraction_strategy(source: str, raw_strategy: Any) -> None:
    """Keep the strategy bounded; behavior is certified only by sandbox execution."""
    del source
    if raw_strategy is None:
        return
    _normalize_extraction_strategy(raw_strategy)


def _normalize_pagination_strategy(raw: Any) -> dict[str, Any]:
    """Accept any bounded mechanism and enforce only cross-site outcome invariants."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("pagination_strategy must be a non-empty object")
    validate_discovery_data_bounds(
        raw,
        max_depth=12,
        max_nodes=2_000,
        max_string_chars=16_000,
        max_approx_bytes=128 * 1024,
    )
    supports_pagination = raw.get("supports_pagination")
    mechanism = raw.get("mechanism")
    request_sequence = raw.get("request_sequence")
    stop_conditions = raw.get("stop_conditions")
    proof = raw.get("proof")
    if not isinstance(supports_pagination, bool):
        raise ValueError("pagination_strategy.supports_pagination must be boolean")
    if not isinstance(mechanism, dict) or not mechanism:
        raise ValueError("pagination_strategy requires an open, non-empty mechanism object")
    if not isinstance(request_sequence, list) or not request_sequence:
        raise ValueError("pagination_strategy requires the real request_sequence")
    if supports_pagination and len(request_sequence) < 2:
        raise ValueError("pagination proof requires at least two consecutive real operations")
    if not isinstance(stop_conditions, list) or not stop_conditions:
        raise ValueError("pagination_strategy requires at least one bounded stop condition")
    if not isinstance(proof, dict):
        raise ValueError("pagination_strategy requires proof")

    first_keys = _bounded_item_keys(proof.get("first_item_keys"), "first_item_keys")
    next_keys = _bounded_item_keys(proof.get("next_item_keys"), "next_item_keys", allow_empty=True)
    if supports_pagination and not (set(next_keys) - set(first_keys)):
        raise ValueError("pagination proof must contain new item identities after the follow-up operation")
    return dict(raw)


def _bounded_item_keys(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty) or len(value) > 500:
        raise ValueError(f"pagination_strategy.proof.{field} is invalid")
    result: list[str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise ValueError(f"pagination_strategy.proof.{field} contains an invalid item key")
        key = str(item).strip()
        if not key or len(key) > 2_000:
            raise ValueError(f"pagination_strategy.proof.{field} contains an invalid item key")
        result.append(key)
    return result


def _find_pagination_strategies(value: Any, *, depth: int = 0) -> list[Any]:
    if depth > 10:
        return []
    if isinstance(value, dict):
        found = [value["pagination_strategy"]] if "pagination_strategy" in value else []
        for nested in value.values():
            found.extend(_find_pagination_strategies(nested, depth=depth + 1))
        return found
    if isinstance(value, list):
        found: list[Any] = []
        for nested in value:
            found.extend(_find_pagination_strategies(nested, depth=depth + 1))
        return found
    return []


def _latest_pagination_strategy(
    observations: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    for observation in reversed(observations):
        for candidate in reversed(_find_pagination_strategies(observation)):
            try:
                return _normalize_pagination_strategy(candidate), errors
            except ValueError as exc:
                errors.append(str(exc))
    return None, errors


def _validate_draft_pagination_strategy(
    source: str,
    supports_pagination: bool,
    raw_strategy: Any,
) -> None:
    """Check only the stable capability flag; runtime trials certify behavior."""
    del source
    strategy = _normalize_pagination_strategy(raw_strategy)
    expected_support = strategy["supports_pagination"]
    if supports_pagination is not expected_support:
        raise ValueError(
            "generated connector supports_pagination contradicts verified pagination_strategy"
        )


def _validate_summary_only_threshold(validation: dict[str, Any] | None) -> None:
    if not isinstance(validation, dict):
        raise ValueError(
            "summary_only requires probe evidence summary_validation with total and at_least_200"
        )
    total = validation.get("total")
    at_least_200 = validation.get("at_least_200")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total <= 0
        or isinstance(at_least_200, bool)
        or not isinstance(at_least_200, int)
        or not 0 <= at_least_200 <= total
    ):
        raise ValueError("summary_validation counts are invalid")
    rate = at_least_200 / total
    if rate < 0.70:
        raise ValueError(
            f"summary_only requires at least 70% summaries with 200 characters; actual={rate:.1%}"
        )


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
    return error
