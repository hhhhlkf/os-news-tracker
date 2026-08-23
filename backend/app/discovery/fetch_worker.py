"""Child-process entry for discovery method fetch (killable by parent)."""

from __future__ import annotations

from typing import Any


def execute_discovery_fetch(
    method_id: int,
    run_id: int,
    request_payload: dict[str, Any] | None,
    *,
    log_queue: Any | None = None,
    db: Any | None = None,
) -> dict[str, Any]:
    """子进程抓取入口：转发到 runner 真正执行一次抓取。

    功能：在子进程里被调用，仅做转发，把实际抓取逻辑委托给 runner.execute_discovery_fetch。
    谁会调用：fetch_jobs._worker 在子进程中调用。
    直接调用：
    - runner.execute_discovery_fetch(...)：真正执行配方抓取并产出结果。
    输入与结果：输入 method_id、run_id、请求负载、日志队列与可选 db；返回抓取结果 dict。
    副作用：由 runner 执行实际抓取（网络、日志、入库等）。
    """
    from app.discovery.runner import execute_discovery_fetch as _execute_discovery_fetch

    return _execute_discovery_fetch(
        method_id,
        run_id,
        request_payload,
        log_queue=log_queue,
        db=db,
    )
