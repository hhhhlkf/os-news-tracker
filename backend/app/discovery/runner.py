"""Unified runner for stored discovery crawl methods."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select


def execute_discovery_fetch(
    method_id: int,
    run_id: int,
    request_payload: dict[str, Any] | None,
    *,
    log_queue: Any | None = None,
    db: Any | None = None,
) -> dict[str, Any]:
    """Run one crawl method from recipe execution through normal item pipeline."""
    from app.db import SessionLocal
    from app.discovery.execution import run_method
    from app.discovery.fetch_runs import finish_method_fetch_run
    from app.discovery.ingester import CrawlOutputIngester
    from app.discovery.progress import log_discovery_progress
    from app.discovery.recipe_prepare import (
        apply_fetch_limits,
        attach_wechat_skip_keys,
        prepare_fetch_recipe,
    )
    from app.extract.scrapling_extractor import ScraplingExtractor
    from app.models import CrawlMethod, Item, Source
    from app.pipeline import Pipeline
    from app.processing.enricher import Enricher
    from app.run_logs import append_run_log, build_not_stored_log_fields
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
        method = db.get(CrawlMethod, method_id)
        if not method:
            raise ValueError(f"method {method_id} not found")

        recipe = prepare_fetch_recipe(method.dsl_recipe, request)
        existing_urls = list(db.scalars(select(Item.url).where(Item.source_id == method.source_id)))
        recipe = attach_wechat_skip_keys(recipe, existing_urls)
        _log(
            "抓方式",
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
        )

        stored = 0
        discovered_count = 0
        try:
            def _log_fetch_progress(event: str, payload: dict[str, Any]) -> None:
                log_discovery_progress(
                    event,
                    payload,
                    emit=_log,
                    stage="抓方式",
                    source=method.domain,
                    method_id=method.id,
                )

            output = run_method(recipe, progress_callback=_log_fetch_progress)
            raw_items = list(output.get("items", []))
            _log(
                "抓方式",
                "DSL 执行完成",
                source=method.domain,
                method_id=method.id,
                raw_count=len(raw_items),
                stats_count=output.get("stats", {}).get("discovered_count"),
            )

            filtered_items = apply_fetch_limits(raw_items, request)
            output["items"] = filtered_items
            _log(
                "抓方式",
                "抓取限制已应用",
                source=method.domain,
                method_id=method.id,
                input_count=len(raw_items),
                kept_count=len(filtered_items),
                dropped_count=max(len(raw_items) - len(filtered_items), 0),
                limit_applied=bool(request),
                time_mode=request.time_mode if request else None,
                target_count=request.target_count if request else None,
            )

            raws = CrawlOutputIngester().to_raw_items(output, source_id=method.source_id)
            discovered_count = len(raws)
            source = db.get(Source, method.source_id)
            pipeline = Pipeline(session=db, extractor=ScraplingExtractor(), enricher=Enricher())
            _log(
                "process",
                "开始处理抓取结果",
                source=method.domain,
                method_id=method.id,
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
                        method_id=method.id,
                        title=raw.title,
                        url=raw.url,
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
                    )

            last_run_status = "ok" if stored > 0 else "empty"
            _log(
                "process",
                "抓取结果处理完成",
                source=method.domain,
                method_id=method.id,
                processed_count=processed_count,
                saved_count=stored,
                rejected_count=max(processed_count - stored, 0),
            )
            _log(
                "抓方式",
                "爬取方式抓取完成",
                source=method.domain,
                method_id=method.id,
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
                source=method.domain,
                method_id=method.id,
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
            method.last_run_at = datetime.now(timezone.utc)
            method.last_run_status = "failed"
            db.commit()
            raise

        method.last_run_at = datetime.now(timezone.utc)
        method.last_run_status = "ok" if stored > 0 else "empty"
        finish_method_fetch_run(
            run_id,
            method.last_run_status,
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
