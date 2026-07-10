"""Child-process entry for discovery method fetch (killable by parent)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def execute_discovery_fetch(
    method_id: int,
    run_id: int,
    request_payload: dict[str, Any] | None,
    *,
    log_queue: Any | None = None,
    db: Any | None = None,
) -> dict[str, Any]:
    """Run one crawl-method fetch + pipeline ingest. Used by killable fetch jobs.

    When *log_queue* is provided (child process), log events are forwarded to the
    parent via the queue so the UI ring buffer (in the API process) stays updated.
    """
    from app.api.discovery_routes import (
        _apply_fetch_limits,
        _prepare_fetch_recipe,
        run_method,
    )
    from app.db import SessionLocal
    from app.discovery.ingester import CrawlOutputIngester
    from app.discovery.fetch_runs import finish_method_fetch_run
    from app.manual_news_run import _build_not_stored_log_fields
    from app.models import CrawlMethod, Source
    from app.pipeline import Pipeline
    from app.processing.enricher import Enricher
    from app.run_logs import append_run_log
    from app.schemas import ManualNewsRunRequest

    def _log(
        stage: str,
        message: str,
        *,
        source: str | None = None,
        level: str = "info",
        **fields: Any,
    ) -> None:
        fields.setdefault("run_id", run_id)
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

    request = ManualNewsRunRequest(**request_payload) if request_payload else None
    owns_session = db is None
    db = db or SessionLocal()
    try:
        m = db.get(CrawlMethod, method_id)
        if not m:
            raise ValueError(f"method {method_id} not found")
        recipe = _prepare_fetch_recipe(m.dsl_recipe, request)
        _log(
            "抓方式",
            "开始抓取爬取方式",
            source=m.domain,
            method_id=m.id,
            run_id=run_id,
            entry_url=m.entry_url,
            status=m.status,
            time_mode=request.time_mode if request else None,
            relative_range=request.relative_range if request else None,
            start_at=request.start_at.isoformat() if request and request.start_at else None,
            end_at=request.end_at.isoformat() if request and request.end_at else None,
            target_count=request.target_count if request else None,
        )
        stored = 0
        discovered_count = 0
        try:
            def _log_fetch_progress(event: str, payload: dict[str, Any]) -> None:
                if event == "wechat_search_started":
                    _log(
                        "抓方式",
                        "开始执行微信搜索 DSL",
                        source=m.domain,
                        method_id=m.id,
                        query=payload.get("query"),
                        max_pages=payload.get("max_pages"),
                    )
                elif event == "wechat_search_page_started":
                    _log(
                        "抓方式",
                        "微信搜索开始抓取分页",
                        source=m.domain,
                        method_id=m.id,
                        query=payload.get("query"),
                        page_no=payload.get("page_no"),
                        total_pages=payload.get("total_pages"),
                        fetched_count=payload.get("fetched_count"),
                        transport=payload.get("transport"),
                    )
                elif event == "wechat_search_page_finished":
                    _log(
                        "抓方式",
                        "微信搜索分页抓取完成",
                        source=m.domain,
                        method_id=m.id,
                        query=payload.get("query"),
                        page_no=payload.get("page_no"),
                        page_items=payload.get("page_items"),
                        fetched_count=payload.get("fetched_count"),
                        transport=payload.get("transport"),
                    )
                elif event in {
                    "wechat_search_page_empty",
                    "wechat_search_rate_limited",
                    "wechat_search_captcha_required",
                    "wechat_search_failed",
                    "wechat_search_finished",
                }:
                    message_map = {
                        "wechat_search_page_empty": "微信搜索当前分页未提取到结果",
                        "wechat_search_rate_limited": "微信搜索触发限流",
                        "wechat_search_captcha_required": "微信搜索触发验证码",
                        "wechat_search_failed": "微信搜索执行失败",
                        "wechat_search_finished": "微信搜索执行完成",
                    }
                    _log(
                        "抓方式",
                        message_map[event],
                        source=m.domain,
                        method_id=m.id,
                        **payload,
                    )
                elif event == "wechat_enrich_started":
                    _log(
                        "抓方式",
                        "开始补抓微信文章内容",
                        source=m.domain,
                        method_id=m.id,
                        total_items=payload.get("total_items"),
                        max_items=payload.get("max_items"),
                        fetch_content=payload.get("fetch_content"),
                    )
                elif event == "wechat_enrich_item_started":
                    _log(
                        "抓方式",
                        "微信文章补抓进行中",
                        source=m.domain,
                        method_id=m.id,
                        attempted_count=payload.get("attempted_count"),
                        max_items=payload.get("max_items"),
                        title=payload.get("title"),
                        url=payload.get("url"),
                    )
                elif event == "wechat_enrich_item_finished":
                    _log(
                        "抓方式",
                        "微信文章补抓完成",
                        source=m.domain,
                        method_id=m.id,
                        attempted_count=payload.get("attempted_count"),
                        enriched_count=payload.get("enriched_count"),
                        title=payload.get("title"),
                        url=payload.get("url"),
                        status=payload.get("status"),
                    )
                elif event == "wechat_enrich_finished":
                    _log(
                        "抓方式",
                        "微信文章补抓阶段完成",
                        source=m.domain,
                        method_id=m.id,
                        status=payload.get("status"),
                        attempted_count=payload.get("attempted_count"),
                        enriched_count=payload.get("enriched_count"),
                        total_items=payload.get("total_items"),
                    )

            output = run_method(recipe, progress_callback=_log_fetch_progress)
            raw_items = list(output.get("items", []))
            _log(
                "抓方式",
                "DSL 执行完成",
                source=m.domain,
                method_id=m.id,
                raw_count=len(raw_items),
                stats_count=output.get("stats", {}).get("discovered_count"),
            )
            filtered_items = _apply_fetch_limits(raw_items, request)
            output["items"] = filtered_items
            _log(
                "抓方式",
                "抓取限制已应用",
                source=m.domain,
                method_id=m.id,
                input_count=len(raw_items),
                kept_count=len(filtered_items),
                dropped_count=max(len(raw_items) - len(filtered_items), 0),
                limit_applied=bool(request),
                time_mode=request.time_mode if request else None,
                target_count=request.target_count if request else None,
            )
            raws = CrawlOutputIngester().to_raw_items(output, source_id=m.source_id)
            discovered_count = len(raws)
            source = db.get(Source, m.source_id)
            pipeline = Pipeline(session=db, extractor=None, enricher=Enricher())
            _log(
                "process",
                "开始处理抓取结果",
                source=m.domain,
                method_id=m.id,
                count=len(raws),
            )
            processed_count = 0
            for raw in raws:
                processed_count += 1
                result = pipeline.process_item_result(source, raw)
                if result.stored:
                    stored += 1
                    _log(
                        "process",
                        "候选已新增入库",
                        source=source.name,
                        method_id=m.id,
                        title=raw.title,
                        url=raw.url,
                    )
                else:
                    _log(
                        "process",
                        "候选未入库",
                        source=source.name,
                        method_id=m.id,
                        title=raw.title,
                        url=raw.url,
                        **_build_not_stored_log_fields(result),
                    )
            last_run_status = "ok" if stored > 0 else "empty"
            _log(
                "process",
                "抓取结果处理完成",
                source=m.domain,
                method_id=m.id,
                processed_count=processed_count,
                saved_count=stored,
                rejected_count=max(processed_count - stored, 0),
            )
            _log(
                "抓方式",
                "爬取方式抓取完成",
                source=m.domain,
                method_id=m.id,
                discovered_count=len(raws),
                stored_count=stored,
                last_run_status=last_run_status,
                summary=(
                    f"抓取 {len(raws)} 条，入库 {stored} 条"
                    if stored > 0
                    else f"抓取 {len(raws)} 条，未入库（可能重复或被富化拒绝）"
                ),
            )
        except Exception as exc:
            _log(
                "抓方式",
                f"爬取方式抓取失败 · {exc}",
                source=m.domain,
                method_id=m.id,
                level="error",
                error_type=type(exc).__name__,
            )
            db.rollback()
            finish_method_fetch_run(
                run_id,
                "failed",
                discovered_count=discovered_count,
                stored_count=stored,
                error_message=str(exc),
                db=db,
            )
            m.last_run_at = datetime.now(timezone.utc)
            m.last_run_status = "failed"
            db.commit()
            raise
        m.last_run_at = datetime.now(timezone.utc)
        m.last_run_status = "ok" if stored > 0 else "empty"
        finish_method_fetch_run(
            run_id,
            m.last_run_status,
            discovered_count=len(raws),
            stored_count=stored,
            db=db,
        )
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
        if owns_session:
            db.close()
