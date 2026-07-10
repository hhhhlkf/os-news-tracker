"""Killable discovery method fetch jobs.

Manual/batch fetch runs in a child process so cancel can terminate() the worker
instead of waiting for cooperative abort (which often hangs on WeChat/network I/O).

Child-process run logs are forwarded to the parent via a multiprocessing Queue,
because app.run_logs is an in-memory ring buffer local to each process.
"""

from __future__ import annotations

import logging
import os
import time
import multiprocessing as mp
from dataclasses import dataclass
from multiprocessing.context import SpawnProcess
from queue import Empty
from threading import Lock
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_lock = Lock()
# method_id -> live process/run pair
@dataclass(frozen=True)
class _FetchJob:
    proc: SpawnProcess
    run_id: int


_jobs: dict[int, _FetchJob] = {}
_ctx = mp.get_context("spawn")


class FetchCancelled(RuntimeError):
    """Fetch job was force-killed by user cancel."""

    def __init__(self, message: str, run_id: int | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


def _should_run_inline() -> bool:
    # Tests and explicit sync mode keep the old in-process path (easier to mock).
    if os.environ.get("DISCOVERY_FETCH_SYNC") == "1":
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return False


def _drain_log_queue(log_queue: mp.Queue) -> None:
    """Pull child log events into the parent process run-log ring buffer."""
    from app.run_logs import append_run_log

    while True:
        try:
            # short timeout: mp.Queue feeder thread may lag behind put_nowait
            event = log_queue.get(timeout=0.05)
        except Empty:
            break
        except Exception:  # noqa: BLE001 - queue closed / broken pipe after kill
            break
        if not isinstance(event, dict):
            continue
        stage = str(event.get("stage") or "抓方式")
        message = str(event.get("message") or "")
        source = event.get("source")
        level = str(event.get("level") or "info")
        fields = {
            key: value
            for key, value in event.items()
            if key not in {"stage", "message", "source", "level", "id", "ts"}
        }
        append_run_log(stage, message, source=source, level=level, **fields)


def _worker(
    method_id: int,
    run_id: int,
    request_payload: dict[str, Any] | None,
    result_queue: mp.Queue,
    log_queue: mp.Queue,
) -> None:
    try:
        from app.discovery.fetch_worker import execute_discovery_fetch

        result = execute_discovery_fetch(method_id, run_id, request_payload, log_queue=log_queue)
        result_queue.put({"ok": True, "result": result})
    except Exception as exc:  # noqa: BLE001 - surface any worker failure to parent
        from app.discovery.fetch_runs import finish_method_fetch_run

        finish_method_fetch_run(run_id, "failed", error_message=str(exc))
        try:
            log_queue.put(
                {
                    "stage": "抓方式",
                    "message": f"爬取方式抓取失败 · {exc}",
                    "level": "error",
                    "method_id": method_id,
                    "run_id": run_id,
                    "error_type": type(exc).__name__,
                }
            )
        except Exception:  # noqa: BLE001
            pass
        result_queue.put(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )


def cancel_fetch_job(method_id: int) -> bool:
    """Force-kill the active fetch process for *method_id*, if any."""
    from app.discovery.fetch_runs import finish_method_fetch_run

    with _lock:
        job = _jobs.get(method_id)
    if job is None:
        return False
    proc = job.proc
    if not proc.is_alive():
        with _lock:
            _jobs.pop(method_id, None)
        return False

    logger.warning("force-killing discovery fetch process method_id=%s pid=%s", method_id, proc.pid)
    try:
        proc.terminate()
        proc.join(timeout=2.0)
    except Exception:  # noqa: BLE001
        logger.exception("terminate failed method_id=%s", method_id)
    if proc.is_alive():
        try:
            proc.kill()
            proc.join(timeout=1.0)
        except Exception:  # noqa: BLE001
            logger.exception("kill failed method_id=%s", method_id)
    with _lock:
        if _jobs.get(method_id) is job:
            _jobs.pop(method_id, None)
    finish_method_fetch_run(job.run_id, "cancelled", error_message="fetch cancelled")
    return True


def get_active_fetch_job_run_id(method_id: int) -> int | None:
    with _lock:
        job = _jobs.get(method_id)
    if job is None or not job.proc.is_alive():
        return None
    return job.run_id


def run_killable_fetch(
    method_id: int,
    request_payload: dict[str, Any] | None,
    *,
    db: Session | None = None,
) -> dict[str, Any]:
    """Run discovery fetch in a killable child process (or inline under tests)."""
    from app.discovery.fetch_runs import finish_method_fetch_run, start_method_fetch_run

    if _should_run_inline():
        from app.discovery.fetch_worker import execute_discovery_fetch

        run_id = start_method_fetch_run(method_id, request_payload, db)
        return execute_discovery_fetch(method_id, run_id, request_payload, db=db)

    with _lock:
        existing = _jobs.get(method_id)
        if existing is not None and existing.proc.is_alive():
            raise RuntimeError(f"method {method_id} already has an active fetch job")

    run_id = start_method_fetch_run(method_id, request_payload)
    result_queue: mp.Queue = _ctx.Queue()
    log_queue: mp.Queue = _ctx.Queue()
    proc: SpawnProcess = _ctx.Process(
        target=_worker,
        args=(method_id, run_id, request_payload, result_queue, log_queue),
        name=f"discovery-fetch-{method_id}",
        daemon=True,
    )
    job = _FetchJob(proc=proc, run_id=run_id)
    with _lock:
        _jobs[method_id] = job
    try:
        proc.start()
    except Exception as exc:
        with _lock:
            if _jobs.get(method_id) is job:
                _jobs.pop(method_id, None)
        finish_method_fetch_run(run_id, "failed", error_message=str(exc))
        raise
    try:
        while True:
            _drain_log_queue(log_queue)
            proc.join(timeout=0.35)
            if not proc.is_alive():
                break
            with _lock:
                # Another request called cancel_fetch_job and removed/replaced us.
                if _jobs.get(method_id) is not job:
                    raise FetchCancelled(f"method {method_id} fetch cancelled", run_id=run_id)
        # Final flush: child may have exited just after putting the last log/result.
        for _ in range(3):
            _drain_log_queue(log_queue)
            if not result_queue.empty():
                break
            time.sleep(0.05)
        try:
            payload = result_queue.get(timeout=0.2)
        except Empty:
            # Killed before result was queued.
            raise FetchCancelled(f"method {method_id} fetch cancelled", run_id=run_id) from None
        if payload.get("ok"):
            return payload["result"]
        raise RuntimeError(f"{payload.get('error_type')}: {payload.get('error')}")
    finally:
        _drain_log_queue(log_queue)
        with _lock:
            if _jobs.get(method_id) is job:
                _jobs.pop(method_id, None)
        if proc.is_alive():
            cancel_fetch_job(method_id)
