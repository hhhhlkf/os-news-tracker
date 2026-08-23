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
import uuid
import multiprocessing as mp
from dataclasses import dataclass
from multiprocessing.context import SpawnProcess
from queue import Empty
from threading import Event, Lock, Thread
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_lock = Lock()
# method_id -> live process/run pair
@dataclass(frozen=True)
class _ProcessFetchJob:
    proc: SpawnProcess
    run_id: int


@dataclass(frozen=True)
class _PluginFetchJob:
    runtime: Any
    sandbox_job_id: str
    run_id: int
    done: Event
    cancelled: Event
    owner_id: str


_jobs: dict[int, _ProcessFetchJob | _PluginFetchJob] = {}
_ctx = mp.get_context("spawn")


class FetchCancelled(RuntimeError):
    """Fetch job was force-killed by user cancel."""

    def __init__(self, message: str, run_id: int | None = None) -> None:
        """构造「抓取任务被强制终止」异常，并记录关联的 run_id 便于定位。

        功能：调用父类构造设置错误信息，并把 run_id 存为实例属性。
        谁会调用：run_killable_fetch / cancel_fetch_job 在强制终止抓取任务时 raise。
        直接调用：
        - super().__init__(...)：设置异常消息。
        输入与结果：输入错误信息与可选 run_id；无返回值（设置实例属性）。
        副作用：无。
        """
        super().__init__(message)
        self.run_id = run_id


def _should_run_inline() -> bool:
    """判断是否走进程内同步抓取（测试或显式同步模式）。

    功能：当环境变量 DISCOVERY_FETCH_SYNC=1 或处于 pytest 环境时返回 True，退化为旧的内联路径，便于 mock。
    谁会调用：run_killable_fetch 在决定子进程还是内联执行时调用。
    直接调用：
    - os.environ.get(...)：读取环境变量。
    输入与结果：无参数；返回布尔。
    副作用：无。
    """
    # Tests and explicit sync mode keep the old in-process path (easier to mock).
    if os.environ.get("DISCOVERY_FETCH_SYNC") == "1":
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return False


def _drain_log_queue(log_queue: mp.Queue) -> None:
    """把子进程日志事件转发到父进程的运行日志环形缓冲。

    功能：从子进程日志队列取事件，转成父进程的 append_run_log 调用，使跨进程的抓取日志能在同一处可见。
    谁会调用：run_killable_fetch 在主循环与收尾处反复调用。
    直接调用：
    - append_run_log(...)：把事件写入父进程运行日志缓冲。
    输入与结果：输入子进程日志队列；无返回值。
    副作用：写入父进程运行日志（进程级全局缓冲）。
    """
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
    """子进程入口：执行抓取并把结果与日志回传父进程。

    功能：调用 fetch_worker 执行实际抓取，把结果放进 result_queue；失败时收尾抓取运行并把错误写入日志与结果队列。
    谁会调用：run_killable_fetch 通过 _ctx.Process(target=_worker) 在子进程启动。
    直接调用：
    - execute_discovery_fetch（fetch_worker）：真正执行抓取。
    - finish_method_fetch_run(...)：失败时收尾运行记录。
    - log_queue.put(...)/result_queue.put(...)：回传日志与结果。
    输入与结果：输入 method_id、run_id、请求负载与两个队列；无返回值（经队列回传）。
    副作用：在子进程中执行抓取（网络/日志/入库），并写队列与失败记录。
    """
    try:
        from app.discovery.fetch_worker import execute_discovery_fetch

        result = execute_discovery_fetch(method_id, run_id, request_payload, log_queue=log_queue)
        result_queue.put({"ok": True, "result": result})
    except Exception as exc:  # noqa: BLE001 - surface any worker failure to parent
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
    """强制终止某抓取方法的活跃子进程（若正在运行）。

    功能：找到 method 对应的子进程，先 terminate 再 join，仍存活则 kill；结束后把抓取运行标记为 cancelled 并清理 _jobs。
    谁会调用：discovery_routes.py 的取消接口、run_killable_fetch 收尾兜底时调用。
    直接调用：
    - finish_method_fetch_run(...)：把运行标记为 cancelled。
    输入与结果：输入 method_id；返回是否成功终止（无活跃任务则返回 False）。
    副作用：终止子进程、更新抓取运行状态、修改全局 _jobs 字典。
    """
    from app.discovery.fetch_runs import finish_method_fetch_run

    with _lock:
        job = _jobs.get(method_id)
    if job is None:
        return False
    if isinstance(job, _PluginFetchJob):
        job.cancelled.set()
        job.runtime.cancel(job.sandbox_job_id)
        if not job.done.wait(timeout=10.0):
            logger.error("sandbox cancellation did not converge method_id=%s", method_id)
            return False
        with _lock:
            if _jobs.get(method_id) is job:
                _jobs.pop(method_id, None)
        finish_method_fetch_run(
            job.run_id,
            "cancelled",
            error_message="fetch cancelled",
            owner_id=job.owner_id,
        )
        return True
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
    """返回某方法活跃抓取子进程对应的 run_id（无或已退出则返回 None）。

    功能：用于上游取消/状态接口确认当前是否在跑以及对应的运行记录。
    谁会调用：discovery_routes.py 的状态/取消接口调用。
    直接调用：无（仅查 _jobs 字典与 proc.is_alive）。
    输入与结果：输入 method_id；返回 run_id 或 None。
    副作用：无。
    """
    with _lock:
        job = _jobs.get(method_id)
    if job is None:
        return None
    if isinstance(job, _PluginFetchJob):
        return None if job.done.is_set() else job.run_id
    if not job.proc.is_alive():
        return None
    return job.run_id


def run_killable_fetch(
    method_id: int,
    request_payload: dict[str, Any] | None,
    *,
    db: Session | None = None,
) -> dict[str, Any]:
    """在可终止的子进程里执行一次抓取（测试环境退化为进程内执行）。

    功能：先开一条 running 抓取记录；测试/同步模式下直接内联执行，否则 spawn 子进程跑 _worker，
    主进程边轮询边转发日志，支持在取消时强制 terminate 子进程，避免卡在微信/网络 IO。
    谁会调用：discovery_routes.py 的抓取接口在处理单次方法抓取时调用。
    直接调用：
    - _should_run_inline(...)：判断是否内联。
    - start_method_fetch_run(...)：创建抓取运行记录。
    - execute_discovery_fetch（fetch_worker）：内联路径真正执行。
    - _worker(...)：经 Process 在子进程执行。
    - _drain_log_queue(...)：转发子进程日志。
    - cancel_fetch_job(...)：收尾兜底终止。
    输入与结果：输入 method_id、请求负载与可选 db；返回抓取结果 dict，失败抛 RuntimeError/FetchCancelled。
    副作用：启动/管理子进程，写入抓取运行记录，转发并产出运行日志。
    """
    from app.discovery.fetch_runs import (
        finish_method_fetch_run,
        start_method_fetch_run,
    )

    method = _load_fetch_method(method_id, db)
    from app.discovery.migration import assert_formal_method_current

    if db is not None:
        assert_formal_method_current(db, method)
    else:
        from app.db import SessionLocal
        from app.models import CrawlMethod

        gate_session = SessionLocal()
        try:
            gated_method = gate_session.get(CrawlMethod, method_id)
            if gated_method is None:
                raise ValueError(f"method {method_id} not found")
            assert_formal_method_current(gate_session, gated_method)
        finally:
            gate_session.close()
    if (method.dsl_recipe or {}).get("recipe_type") == "python_plugin":
        if method.review_status != "approved" or method.status != "active":
            raise RuntimeError("python_plugin method is not approved and active")
        return _run_plugin_fetch(method_id, request_payload, db=db)

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
    job = _ProcessFetchJob(proc=proc, run_id=run_id)
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


def _load_fetch_method(method_id: int, db: Session | None) -> Any:
    from app.db import SessionLocal
    from app.models import CrawlMethod

    if db is not None:
        method = db.get(CrawlMethod, method_id)
        if method is None:
            raise ValueError(f"method {method_id} not found")
        return method
    session = SessionLocal()
    try:
        method = session.get(CrawlMethod, method_id)
        if method is None:
            raise ValueError(f"method {method_id} not found")
        session.expunge(method)
        return method
    finally:
        session.close()


def _run_plugin_fetch(
    method_id: int,
    request_payload: dict[str, Any] | None,
    *,
    db: Session | None,
) -> dict[str, Any]:
    """Run reviewed plugin in this process so cancellation targets its sandbox."""
    from app.discovery.fetch_runs import (
        process_start_token,
        start_method_fetch_run,
        watch_method_fetch_owner,
    )
    from app.discovery.runner import execute_discovery_fetch
    from app.discovery.sandbox.runtime import get_formal_sandbox_runtime

    runtime = get_formal_sandbox_runtime()
    done = Event()
    cancelled = Event()
    owner_pid = os.getpid()
    owner_start_token = process_start_token(owner_pid)
    owner_id = f"{owner_pid}-{owner_start_token}-{uuid.uuid4().hex}"
    with _lock:
        existing = _jobs.get(method_id)
        if existing is not None:
            raise RuntimeError(f"method {method_id} already has an active fetch job")
        # Commit and local registration share one critical section. A remote
        # cancellation may arrive after commit; the watcher observes it.
        provisional_job_id = f"formal-manual-{method_id}-{owner_id}"
        run_id = start_method_fetch_run(
            method_id,
            request_payload,
            db,
            owner_id=owner_id,
            owner_pid=owner_pid,
            owner_start_token=owner_start_token,
            sandbox_job_id=provisional_job_id,
        )
        sandbox_job_id = provisional_job_id
        job = _PluginFetchJob(runtime, sandbox_job_id, run_id, done, cancelled, owner_id)
        _jobs[method_id] = job
    watcher_stop = Event()

    watcher = Thread(
        target=watch_method_fetch_owner,
        kwargs={
            "run_id": run_id,
            "owner_id": owner_id,
            "sandbox_job_id": sandbox_job_id,
            "runtime": runtime,
            "cancel_event": cancelled,
            "stop_event": watcher_stop,
        },
        name=f"formal-cancel-{run_id}",
        daemon=True,
    )
    watcher.start()
    try:
        return execute_discovery_fetch(
            method_id,
            run_id,
            request_payload,
            db=db,
            sandbox_runtime=runtime,
            sandbox_job_id=sandbox_job_id,
            cancel_event=cancelled,
            owner_id=owner_id,
        )
    except Exception as exc:
        if cancelled.is_set():
            raise FetchCancelled(f"method {method_id} fetch cancelled", run_id=run_id) from exc
        raise
    finally:
        watcher_stop.set()
        watcher.join(timeout=1.0)
        done.set()
        with _lock:
            if _jobs.get(method_id) is job:
                _jobs.pop(method_id, None)
