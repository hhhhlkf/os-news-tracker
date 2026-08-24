"""Unified runner for stored discovery crawl methods."""

from __future__ import annotations

from threading import Event
from typing import TYPE_CHECKING, Any, Callable

from sqlalchemy import select

if TYPE_CHECKING:
    from app.discovery.sandbox import SandboxJobPriority


def execute_discovery_fetch(
    method_id: int,
    run_id: int,
    request_payload: dict[str, Any] | None,
    *,
    log_queue: Any | None = None,
    db: Any | None = None,
    sandbox_runtime: Any | None = None,
    sandbox_job_id: str | None = None,
    cancel_event: Event | None = None,
    owner_id: str | None = None,
    sandbox_priority: SandboxJobPriority | None = None,
    log_stage: str = "抓方式",
    manage_usage_scope: bool = True,
    result_progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Run and terminalize one formal method exactly once through owner CAS."""
    from app.discovery.fetch_runs import finish_method_fetch_run
    from app.discovery.formal_repair import (
        formal_failure_evidence,
        trigger_repair_after_formal_result,
    )
    from app.discovery.plugin.errors import ConnectorErrorCode, ConnectorProtocolError
    from app.discovery.redaction import redact_discovery_text

    stage = "request"

    def _stage(value: str) -> None:
        nonlocal stage
        stage = value

    try:
        result = _execute_discovery_fetch_impl(
            method_id,
            run_id,
            request_payload,
            log_queue=log_queue,
            db=db,
            sandbox_runtime=sandbox_runtime,
            sandbox_job_id=sandbox_job_id,
            cancel_event=cancel_event,
            owner_id=owner_id,
            sandbox_priority=sandbox_priority,
            log_stage=log_stage,
            manage_usage_scope=manage_usage_scope,
            result_progress_callback=result_progress_callback,
            stage_callback=_stage,
        )
    except Exception as exc:
        cancelled = bool(
            (cancel_event is not None and cancel_event.is_set())
            or (
                isinstance(exc, ConnectorProtocolError)
                and exc.code == ConnectorErrorCode.CANCELLED
            )
        )
        counts = getattr(exc, "_formal_counts", (0, 0))
        discovered, stored = counts if isinstance(counts, tuple) and len(counts) == 2 else (0, 0)

        def _safe_count(value: object) -> int:
            try:
                return max(0, int(value))
            except BaseException:
                return 0

        evidence: dict[str, object] = {
            "stage": stage[:100],
            "code": type(exc).__name__[:200],
            "message": "formal fetch failed; evidence construction failed",
            "details": {},
            "details_truncated": True,
            "evidence_error": "formal_failure_evidence raised unexpectedly",
            "repairable": False,
        }
        try:
            evidence = formal_failure_evidence(exc, stage=stage)
        except BaseException:
            pass
        finally:
            finished_as = finish_method_fetch_run(
                run_id,
                "cancelled" if cancelled else "failed",
                discovered_count=_safe_count(discovered),
                stored_count=_safe_count(stored),
                error_message=str(evidence.get("message") or "formal fetch failed")[:4000],
                failure_evidence=None if cancelled else evidence,
                owner_id=owner_id,
                db=db,
            )
        terminal = "cancelled" if cancelled or finished_as == "cancelled" else "failed"
        if terminal == "failed":
            trigger_repair_after_formal_result(method_id, db=db)
        # Scheduled callers need a durable cancellation classification even
        # when their process-local morning-run event was not the requester.
        # Exception attributes preserve the public exception contract while
        # avoiding a second, racy database lookup after terminalization.
        try:
            setattr(exc, "_formal_cancelled", terminal == "cancelled")
            setattr(exc, "_formal_run_id", run_id)
        except BaseException:
            pass
        raise

    status = (
        "partial"
        if result.get("stats", {}).get("status") == "partial"
        else "ok" if int(result.get("stored_count") or 0) > 0 else "empty"
    )
    finished_as = finish_method_fetch_run(
        run_id,
        status,
        discovered_count=int(result.get("discovered_count") or 0),
        stored_count=int(result.get("stored_count") or 0),
        error_message=(
            redact_discovery_text(str(result.get("stats", {}).get("error")))[:4000]
            if status == "partial" and result.get("stats", {}).get("error")
            else None
        ),
        owner_id=owner_id,
        db=db,
    )
    if finished_as == "cancelled":
        if cancel_event is not None:
            cancel_event.set()
        cancelled_error = RuntimeError("formal fetch cancelled during completion")
        cancelled_error._formal_cancelled = True  # type: ignore[attr-defined]
        cancelled_error._formal_run_id = run_id  # type: ignore[attr-defined]
        raise cancelled_error
    if finished_as != status:
        raise RuntimeError(
            f"formal fetch completion lost owner CAS to terminal status {finished_as}"
        )
    trigger_repair_after_formal_result(method_id, db=db)
    return result


def _execute_discovery_fetch_impl(
    method_id: int,
    run_id: int,
    request_payload: dict[str, Any] | None,
    *,
    log_queue: Any | None = None,
    db: Any | None = None,
    sandbox_runtime: Any | None = None,
    sandbox_job_id: str | None = None,
    cancel_event: Event | None = None,
    owner_id: str | None = None,
    sandbox_priority: SandboxJobPriority | None = None,
    log_stage: str = "抓方式",
    manage_usage_scope: bool = True,
    result_progress_callback: Callable[[int, int], None] | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """执行一个已存爬取方法的完整抓取：配方准备 → 执行 → 限流 → 入库 → 收尾。

    功能：载入 CrawlMethod，补全配方、按已有 URL 去重，执行 DSL 拿到候选，应用时窗/条数限制，
    再经 CrawlOutputIngester 转 RawItem 走正常 pipeline 入库，最后更新方法与抓取运行状态。
    谁会调用：fetch_worker.execute_discovery_fetch、fetch_jobs.run_killable_fetch（内联路径）在子进程真正执行抓取时调用。
    直接调用：
    - prepare_fetch_recipe(...)：补全配方参数。
    - attach_wechat_skip_keys(...)：按已有 URL 注入去重键。
    - run_method(...)：真正执行配方返回候选。
    - apply_fetch_limits(...)：按时窗与条数裁剪。
    - CrawlOutputIngester().to_raw_items(...)：转 RawItem。
    - Pipeline.process_item_result(...)：走去重/富化/入库。
    - finish_method_fetch_run(...)：收尾抓取运行记录。
    输入与结果：输入 method_id、run_id、请求负载、日志队列与可选 db；返回含 run_id/discovered_count/stored_count/items 的 dict。
    副作用：读取并写入数据库（CrawlMethod、Item、Source），触发网络抓取与 LLM 富化，写运行日志，更新抓取运行状态。
    """
    from app.db import SessionLocal
    from app.discovery.execution import run_method
    from app.discovery.ingester import CrawlOutputIngester
    from app.discovery.partial_output import PartialExecutionError
    from app.discovery.progress import log_discovery_progress
    from app.discovery.redaction import redact_discovery_text
    from app.discovery.recipe_prepare import (
        apply_fetch_limits,
        attach_wechat_skip_keys,
        prepare_fetch_recipe,
    )
    from app.extract.scrapling_extractor import ScraplingExtractor
    from app.llm.usage import UsageScope, activate_usage_scope, deactivate_usage_scope
    from app.models import CrawlMethod, Item, Source
    from app.pipeline import Pipeline
    from app.processing.enricher import Enricher
    from app.run_logs import append_run_log, build_not_stored_log_fields
    from app.schemas import ManualNewsRunRequest
    from app.discovery.sandbox import SandboxJobPriority

    def _log(
        stage: str,
        message: str,
        *,
        source: str | None = None,
        level: str = "info",
        **fields: Any,
    ) -> None:
        """统一的运行日志输出：子进程经 log_queue 回传，父进程直接写本地缓冲。

        功能：为每条日志补上 run_id，再按运行环境选择队列转发或本地写入，供实时日志面板消费。
        谁会调用：execute_discovery_fetch 内部各阶段调用。
        直接调用：
        - log_queue.put(...)：子进程把日志事件回传父进程。
        - append_run_log(...)：父进程直接写入运行日志缓冲。
        输入与结果：输入 stage、message、source、level 与任意字段；无返回值。
        副作用：写运行日志（队列或本地缓冲）。
        """
        fields.setdefault("run_id", run_id)
        fields.setdefault("trigger_type", log_trigger_type)
        if log_queue is not None:
            log_queue.put(
                {
                    "stage": stage,
                    "message": message,
                    "source": source,
                    "level": level,
                    **fields,
                }
            )
            return
        append_run_log(stage, message, source=source, level=level, **fields)

    def _ensure_not_cancelled(stage: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError(f"formal fetch cancelled {stage}")

    request = ManualNewsRunRequest(**request_payload) if request_payload else None
    trigger_type = "manual_method"
    if request is not None and request.trigger_type in {"manual", "manual_method"}:
        trigger_type = request.trigger_type
    elif isinstance(request_payload, dict) and request_payload.get("trigger_type") in {
        "manual",
        "manual_method",
    }:
        trigger_type = str(request_payload["trigger_type"])
    log_trigger_type = "scheduled" if log_stage == "定时抓取" else trigger_type
    owns_session = db is None
    db = db or SessionLocal()
    usage_token = (
        activate_usage_scope(
            UsageScope(
                context_type="query",
                trigger_type=trigger_type,
                stage="query_enrichment",
                crawl_method_run_id=run_id,
                method_id=method_id,
            )
        )
        if manage_usage_scope
        else None
    )
    try:
        if stage_callback is not None:
            stage_callback("database")
        method = db.get(CrawlMethod, method_id)
        if not method:
            raise ValueError(f"method {method_id} not found")
        from app.discovery.migration import assert_formal_method_current

        assert_formal_method_current(db, method)

        _ensure_not_cancelled("before recipe preparation")
        if stage_callback is not None:
            stage_callback("connector_contract")
        recipe = prepare_fetch_recipe(method.dsl_recipe, request)
        existing_urls = list(db.scalars(select(Item.url).where(Item.source_id == method.source_id)))
        recipe = attach_wechat_skip_keys(recipe, existing_urls)
        _log(
            log_stage,
            "开始抓取爬取方式",
            source=method.domain,
            method_id=method.id,
            run_id=run_id,
            entry_url=method.entry_url,
            status=method.status,
            time_mode=request.time_mode if request else None,
            relative_range=request.relative_range if request else None,
            start_at=request.start_at.isoformat() if request and request.start_at else None,
            end_at=request.end_at.isoformat() if request and request.end_at else None,
            target_count=request.target_count if request else None,
            phase="prepare",
        )

        stored = 0
        discovered_count = 0
        final_status = "empty"
        partial_error: PartialExecutionError | None = None
        try:
            def _log_fetch_progress(event: str, payload: dict[str, Any]) -> None:
                """把 discovery 进度事件适配成统一运行日志。

                功能：作为 run_method 的 progress_callback，把抓取阶段进度事件转交 log_discovery_progress 并落到 _log。
                谁会调用：run_method 在执行配方过程中回调。
                直接调用：
                - log_discovery_progress(...)：翻译并挑选进度字段。
                - _log(...)：输出统一运行日志。
                输入与结果：输入事件名与负载；无返回值。
                副作用：经 _log 写运行日志。
                """
                log_discovery_progress(
                    event,
                    payload,
                    emit=_log,
                    stage=log_stage,
                    source=method.domain,
                    method_id=method.id,
                    extra_fields={"phase": "connector_sandbox"},
                )

            try:
                if stage_callback is not None:
                    stage_callback("connector_sandbox")
                output = run_method(
                    recipe,
                    progress_callback=_log_fetch_progress,
                    expected_signature=method.signature,
                    sandbox_runtime=sandbox_runtime,
                    sandbox_job_id=sandbox_job_id,
                    cancel_event=cancel_event,
                    sandbox_priority=(
                        sandbox_priority
                        if sandbox_priority is not None
                        else SandboxJobPriority.MANUAL
                    ),
                    allow_legacy_compatibility=(
                        recipe.get("recipe_type") != "python_plugin"
                    ),
                    request_parameters=(
                        request.model_dump(mode="json") if request is not None else None
                    ),
                )
                _ensure_not_cancelled("after sandbox")
                if stage_callback is not None:
                    stage_callback("connector_contract")
            except PartialExecutionError as exc:
                partial_error = exc
                output = {
                    "items": list(exc.items),
                    "stats": {
                        **(exc.stats or {}),
                        "status": "partial",
                        "error": str(exc),
                    },
                }
                _log(
                    log_stage,
                    "采集器执行部分完成，已保留已抓到候选继续处理 · "
                    f"{redact_discovery_text(str(exc))[:4000]}",
                    source=method.domain,
                    method_id=method.id,
                    level="warning",
                    raw_count=len(output.get("items", [])),
                    stats_count=output.get("stats", {}).get("discovered_count"),
                    error_type=type(exc).__name__,
                    phase="connector_sandbox",
                )
            raw_items = list(output.get("items", []))
            if partial_error is None and output.get("stats", {}).get("status") == "partial":
                partial_error = PartialExecutionError(
                    str(output.get("stats", {}).get("error") or "connector returned partial output"),
                    items=raw_items,
                    stats=output.get("stats", {}),
                )
            if partial_error is None:
                _log(
                    log_stage,
                    "采集器执行完成",
                    source=method.domain,
                    method_id=method.id,
                    raw_count=len(raw_items),
                    stats_count=output.get("stats", {}).get("discovered_count"),
                    phase="connector_sandbox",
                )

            filtered_items = apply_fetch_limits(raw_items, request)
            output["items"] = filtered_items
            _log(
                log_stage,
                "抓取限制已应用",
                source=method.domain,
                method_id=method.id,
                input_count=len(raw_items),
                kept_count=len(filtered_items),
                dropped_count=max(len(raw_items) - len(filtered_items), 0),
                limit_applied=bool(request),
                time_mode=request.time_mode if request else None,
                target_count=request.target_count if request else None,
                phase="output_filter",
            )

            _ensure_not_cancelled("before ingestion")
            raws = CrawlOutputIngester().to_raw_items(output, source_id=method.source_id)
            discovered_count = len(raws)
            if result_progress_callback is not None:
                result_progress_callback(discovered_count, stored)
            source = db.get(Source, method.source_id)
            pipeline = Pipeline(
                session=db,
                extractor=ScraplingExtractor(),
                enricher=Enricher(),
                cancel_check=lambda: _ensure_not_cancelled("before repository commit"),
            )
            _log(
                "process",
                "开始处理抓取结果",
                source=method.domain,
                method_id=method.id,
                count=len(raws),
                phase="pipeline",
            )
            processed_count = 0
            if stage_callback is not None:
                stage_callback("pipeline")
            for raw in raws:
                _ensure_not_cancelled("before pipeline item")
                processed_count += 1
                result = pipeline.process_item_result(source, raw)
                _ensure_not_cancelled("after pipeline item")
                if result.stored:
                    stored += 1
                    if result_progress_callback is not None:
                        result_progress_callback(discovered_count, stored)
                    _log(
                        "process",
                        "候选已新增入库",
                        source=source.name,
                        method_id=method.id,
                        title=raw.title,
                        url=raw.url,
                        phase="pipeline",
                    )
                else:
                    _log(
                        "process",
                        "候选未入库",
                        source=source.name,
                        method_id=method.id,
                        title=raw.title,
                        url=raw.url,
                        **build_not_stored_log_fields(result),
                        phase="pipeline",
                    )

            final_status = "partial" if partial_error is not None else ("ok" if stored > 0 else "empty")
            _log(
                "process",
                "抓取结果处理完成",
                source=method.domain,
                method_id=method.id,
                processed_count=processed_count,
                saved_count=stored,
                rejected_count=max(processed_count - stored, 0),
                phase="pipeline",
            )
            _log(
                log_stage,
                "爬取方式抓取完成",
                source=method.domain,
                method_id=method.id,
                discovered_count=len(raws),
                stored_count=stored,
                last_run_status=final_status,
                summary=(
                    f"部分抓取 {len(raws)} 条，入库 {stored} 条"
                    if partial_error is not None and stored > 0
                    else f"部分抓取 {len(raws)} 条，未入库（可能重复或被富化拒绝）"
                    if partial_error is not None
                    else f"抓取 {len(raws)} 条，入库 {stored} 条"
                    if stored > 0
                    else f"抓取 {len(raws)} 条，未入库（可能重复或被富化拒绝）"
                ),
                phase="complete",
            )
        except Exception as exc:
            _log(
                log_stage,
                f"爬取方式抓取失败 · {redact_discovery_text(str(exc))[:4000]}",
                source=method.domain,
                method_id=method.id,
                level="error",
                error_type=type(exc).__name__,
                phase="failed",
            )
            db.rollback()
            try:
                setattr(exc, "_formal_counts", (discovered_count, stored))
            except Exception:
                pass
            raise

        _ensure_not_cancelled("before successful completion")
        if stage_callback is not None:
            stage_callback("database")
        db.commit()
        return {
            "run_id": run_id,
            "discovered_count": len(raws),
            "stored_count": stored,
            "items": output.get("items", []),
            "stats": output.get("stats", {}),
            "message": (
                f"抓取 {len(raws)} 条，入库 {stored} 条"
                if stored > 0
                else f"抓取 {len(raws)} 条，未入库（可能重复或被富化拒绝）"
            ),
        }
    finally:
        if usage_token is not None:
            deactivate_usage_scope(usage_token)
        if owns_session:
            db.close()
