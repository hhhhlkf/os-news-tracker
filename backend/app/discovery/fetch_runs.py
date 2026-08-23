from __future__ import annotations

from collections.abc import Callable
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import CrawlMethod, CrawlMethodRun


logger = logging.getLogger(__name__)


class ActiveMethodFetchError(RuntimeError):
    def __init__(self, method_id: int, active_run_id: int | None) -> None:
        """构造「该方法已有活跃抓取任务」异常，附带冲突的 run_id 便于定位。

        功能：记录 method_id 与正在运行的 run_id，并生成可读错误信息。
        谁会调用：start_method_fetch_run 在数据库唯一冲突时抛出。
        直接调用：super().__init__(...) 设置异常消息。
        输入与结果：输入 method_id 与 active_run_id；无返回值（设置实例属性）。
        副作用：无。
        """
        self.method_id = method_id
        self.active_run_id = active_run_id
        suffix = f" (run_id={active_run_id})" if active_run_id is not None else ""
        super().__init__(f"method {method_id} already has an active fetch job{suffix}")


def _with_session(db: Session | None, fn: Callable[[Session], Any]) -> Any:
    """统一会话管理：有传入会话则复用，否则新建并在结束时关闭。

    功能：让读取/写入函数不必关心会话来源，外部传入时复用，否则临时开一个 SessionLocal。
    谁会调用：get_active_method_fetch_run、start_method_fetch_run、finish_method_fetch_run 内部调用。
    直接调用：
    - SessionLocal(...)：在需要新会话时创建。
    输入与结果：输入可选 db 与会话处理函数；返回 fn 的执行结果。
    副作用：可能新建并关闭数据库会话。
    """
    if db is not None:
        return fn(db)
    session = SessionLocal()
    try:
        return fn(session)
    finally:
        session.close()


def get_active_method_fetch_run(method_id: int, db: Session | None = None) -> CrawlMethodRun | None:
    """查询某抓取方法当前正在运行（running）的抓取记录。

    功能：按 method_id + status='running' 取最新一条抓取运行，供状态/取消判断与并发冲突检测。
    谁会调用：discovery_routes.py 的状态/取消接口、start_method_fetch_run 冲突检测时调用。
    直接调用：
    - _with_session(...)：统一会话。
    - SQLAlchemy select(...)：构造并执行的查询。
    输入与结果：输入 method_id 与可选 db；返回 CrawlMethodRun 或 None。
    副作用：只读查询数据库。
    """
    def _get(session: Session) -> CrawlMethodRun | None:
        """在给定会话里按 method_id + running 取最新一条抓取运行。

        功能：用 select 构造「method_id 匹配且 status=running」的查询，按时间倒序取第一条。
        谁会调用：get_active_method_fetch_run 通过 _with_session 执行。
        直接调用：
        - select(...)/session.scalar(...)：构造并执行查询（sqlalchemy）。
        输入与结果：输入数据库会话；返回 CrawlMethodRun 或 None。
        副作用：无（只读查询）。
        """
        return session.scalar(
            select(CrawlMethodRun)
            .where(CrawlMethodRun.method_id == method_id, CrawlMethodRun.status == "running")
            .order_by(CrawlMethodRun.started_at.desc(), CrawlMethodRun.id.desc())
        )

    return _with_session(db, _get)


def start_method_fetch_run(
    method_id: int,
    request_payload: dict[str, Any] | None,
    db: Session | None = None,
    owner_id: str | None = None,
    owner_pid: int | None = None,
    owner_start_token: str | None = None,
    sandbox_job_id: str | None = None,
) -> int:
    """创建一条 running 状态的抓取运行记录（并发冲突时抛异常）。

    功能：新建 CrawlMethodRun 并落库；若已有同方法活跃任务触发唯一冲突，回滚后查活并报 ActiveMethodFetchError。
    谁会调用：fetch_jobs.run_killable_fetch（内联与子进程两条路径）在抓取开始前调用。
    直接调用：
    - _with_session(...)：统一会话。
    - get_active_method_fetch_run(...)：冲突时查活跃任务。
    - CrawlMethodRun(...)：构造记录；session.add/commit 落库。
    输入与结果：输入 method_id、请求负载与可选 db；返回新建 run_id，冲突时抛 ActiveMethodFetchError。
    副作用：向 CrawlMethodRun 表写入一条运行记录并提交。
    """
    def _start(session: Session) -> int:
        """在会话里新建一条 running 的抓取运行记录，冲突时回滚并报错。

        功能：构造 CrawlMethodRun 并 add/commit；若触发唯一冲突（已有同方法活跃任务），
        回滚后查活跃 run 并抛 ActiveMethodFetchError。
        谁会调用：start_method_fetch_run 通过 _with_session 执行。
        直接调用：
        - CrawlMethodRun(...)：构造记录。
        - session.add(...)/session.commit(...)：落库。
        - get_active_method_fetch_run(...)：冲突时查活跃任务。
        输入与结果：输入数据库会话；返回新建 run_id，冲突时抛 ActiveMethodFetchError。
        副作用：向 CrawlMethodRun 表写入一条记录并提交。
        """
        from app.discovery.domain_transition import active_formal_run_id, domain_transition_lock
        from app.discovery.migration import assert_formal_method_current

        observed = session.get(CrawlMethod, method_id)
        if observed is None:
            raise ValueError(f"method {method_id} not found")
        domain = observed.domain
        with domain_transition_lock(session, domain):
            method = session.scalar(
                select(CrawlMethod).where(CrawlMethod.id == method_id).with_for_update()
            )
            if method is None:
                raise ValueError(f"method {method_id} not found")
            assert_formal_method_current(session, method)
            active_run_id = active_formal_run_id(session, (method_id,))
            if active_run_id is not None:
                raise ActiveMethodFetchError(method_id, active_run_id)
            run = CrawlMethodRun(
                method_id=method_id,
                status="running",
                request_payload=request_payload,
                owner_id=owner_id,
                owner_pid=owner_pid,
                owner_start_token=owner_start_token,
                owner_heartbeat_at=(datetime.now(timezone.utc) if owner_id is not None else None),
                sandbox_job_id=sandbox_job_id,
            )
            session.add(run)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                active = get_active_method_fetch_run(method_id, session)
                raise ActiveMethodFetchError(
                    method_id,
                    active.id if active is not None else None,
                ) from exc
            session.refresh(run)
            return run.id

    return _with_session(db, _start)


def finish_method_fetch_run(
    run_id: int,
    status: str,
    *,
    discovered_count: int = 0,
    stored_count: int = 0,
    error_message: str | None = None,
    failure_evidence: dict[str, Any] | None = None,
    owner_id: str | None = None,
    db: Session | None = None,
) -> str | None:
    """收尾一条抓取运行：写最终状态、发现/入库计数、错误与完成时间。

    功能：按 run_id 取回运行记录，若仍为 running 则更新状态与统计并打上完成时间后提交；不存在或非 running 则忽略。
    谁会调用：fetch_jobs._worker、run_killable_fetch、cancel_fetch_job、discovery_routes.py 取消接口、runner 等处调用。
    直接调用：
    - _with_session(...)：统一会话。
    - session.get(...)：取运行记录；session.commit 提交。
    输入与结果：输入 run_id、status 与若干统计字段；无返回值。
    副作用：更新 CrawlMethodRun 表对应记录并提交。
    """
    def _finish(session: Session) -> str | None:
        """在会话里按 run_id 收尾一条抓取运行：更新状态、统计与完成时间。

        功能：取回运行记录，若仍为 running 则写入状态/发现数/入库数/错误/完成时间后提交；
        不存在或已非 running 则忽略。
        谁会调用：finish_method_fetch_run 通过 _with_session 执行。
        直接调用：
        - session.get(...)/session.commit(...)：取记录并提交（sqlalchemy）。
        输入与结果：输入数据库会话；无返回值。
        副作用：更新 CrawlMethodRun 表对应记录并提交。
        """
        from app.discovery.domain_transition import domain_transition_lock

        observed = session.get(CrawlMethodRun, run_id)
        if observed is None:
            return None
        method = session.get(CrawlMethod, observed.method_id)
        if method is None:
            return None
        domain = method.domain
        with domain_transition_lock(session, domain):
            now = datetime.now(timezone.utc)
            conditions = [CrawlMethodRun.id == run_id, CrawlMethodRun.status == "running"]
            if owner_id is not None:
                conditions.append(CrawlMethodRun.owner_id == owner_id)
            if status != "cancelled":
                conditions.append(CrawlMethodRun.cancel_requested_at.is_(None))
            changed = session.execute(
                update(CrawlMethodRun)
                .where(*conditions)
                .values(
                    status=status,
                    discovered_count=discovered_count,
                    stored_count=stored_count,
                    error_message=error_message,
                    failure_evidence=failure_evidence,
                    completed_at=now,
                    cancel_acknowledged_at=(now if status == "cancelled" else None),
                )
            )
            actual_status = status if changed.rowcount == 1 else None
            # A persisted cancellation request always wins a concurrent success/error
            # finish. Only the recorded owner may acknowledge and terminalize it.
            if changed.rowcount != 1 and status != "cancelled" and owner_id is not None:
                cancelled = session.execute(
                    update(CrawlMethodRun)
                    .where(
                        CrawlMethodRun.id == run_id,
                        CrawlMethodRun.status == "running",
                        CrawlMethodRun.owner_id == owner_id,
                        CrawlMethodRun.cancel_requested_at.is_not(None),
                    )
                    .values(
                        status="cancelled",
                        discovered_count=discovered_count,
                        stored_count=stored_count,
                        error_message="fetch cancelled",
                        completed_at=now,
                        cancel_acknowledged_at=now,
                    )
                )
                if cancelled.rowcount == 1:
                    actual_status = "cancelled"
            if actual_status is not None:
                from app.discovery.migration import record_formal_terminal

                record_formal_terminal(session, run_id, actual_status, completed_at=now)
            session.commit()
            if actual_status is None:
                existing = session.get(CrawlMethodRun, run_id)
                if existing is not None and existing.status != "running":
                    actual_status = existing.status
            return actual_status

    return _with_session(db, _finish)


def request_method_fetch_cancel(run_id: int, db: Session | None = None) -> bool:
    """Persist a cancellation request without pretending the owner acknowledged it."""
    def _request(session: Session) -> bool:
        changed = session.execute(
            update(CrawlMethodRun)
            .where(CrawlMethodRun.id == run_id, CrawlMethodRun.status == "running")
            .values(cancel_requested_at=datetime.now(timezone.utc))
        )
        session.commit()
        return changed.rowcount == 1
    return bool(_with_session(db, _request))


def is_method_fetch_cancel_requested(run_id: int, db: Session | None = None) -> bool:
    def _check(session: Session) -> bool:
        run = session.get(CrawlMethodRun, run_id)
        return bool(run is not None and run.status == "running" and run.cancel_requested_at is not None)
    return bool(_with_session(db, _check))


def watch_method_fetch_owner(
    *,
    run_id: int,
    owner_id: str,
    sandbox_job_id: str,
    runtime: Any,
    cancel_event: Event,
    stop_event: Event,
) -> None:
    """Broker durable cross-worker cancellation and owner heartbeats."""
    next_heartbeat = 0.0
    cancellation_sent = False
    while not stop_event.wait(0.1):
        if cancel_event.is_set() and not cancellation_sent:
            runtime.cancel(sandbox_job_id)
            cancellation_sent = True
        try:
            now = time.monotonic()
            if now >= next_heartbeat:
                if not heartbeat_method_fetch_owner(run_id, owner_id):
                    cancel_event.set()
                    runtime.cancel(sandbox_job_id)
                    return
                next_heartbeat = now + 1.0
            if is_method_fetch_cancel_requested(run_id):
                cancel_event.set()
                if not cancellation_sent:
                    runtime.cancel(sandbox_job_id)
                    cancellation_sent = True
        except Exception:
            logger.exception("formal owner broker poll failed run_id=%s", run_id)


def process_start_token(pid: int | None = None) -> str:
    """Return Linux's immutable process start tick used to disambiguate PID reuse."""
    actual_pid = pid if pid is not None else os.getpid()
    tail = Path(f"/proc/{actual_pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1]
    return tail.strip().split()[19]


def heartbeat_method_fetch_owner(run_id: int, owner_id: str, db: Session | None = None) -> bool:
    """Refresh a formal run lease only while the same owner still owns a running row."""
    def _heartbeat(session: Session) -> bool:
        changed = session.execute(
            update(CrawlMethodRun)
            .where(
                CrawlMethodRun.id == run_id,
                CrawlMethodRun.status == "running",
                CrawlMethodRun.owner_id == owner_id,
            )
            .values(owner_heartbeat_at=datetime.now(timezone.utc))
        )
        session.commit()
        return changed.rowcount == 1
    return bool(_with_session(db, _heartbeat))


def _owner_is_alive(run: CrawlMethodRun) -> bool:
    if run.owner_pid is None or not run.owner_start_token:
        return False
    heartbeat = run.owner_heartbeat_at
    if heartbeat is None:
        return False
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - heartbeat > timedelta(seconds=30):
        return False
    try:
        return process_start_token(run.owner_pid) == run.owner_start_token
    except (OSError, ValueError, IndexError):
        return False


def get_live_method_fetch_sandbox_job_ids(db: Session | None = None) -> set[str]:
    """Return formal sandbox labels whose persisted PID identity is still alive."""
    def _get(session: Session) -> set[str]:
        runs = session.scalars(
            select(CrawlMethodRun).where(
                CrawlMethodRun.status == "running",
                CrawlMethodRun.owner_id.is_not(None),
                CrawlMethodRun.sandbox_job_id.is_not(None),
            )
        )
        return {str(run.sandbox_job_id) for run in runs if _owner_is_alive(run)}
    return set(_with_session(db, _get))


def has_live_scheduled_fetch_owner(
    morning_run_id: int,
    db: Session | None = None,
) -> bool:
    """Prove whether another worker still owns this morning run's formal fetch."""
    def _has(session: Session) -> bool:
        runs = session.scalars(
            select(CrawlMethodRun).where(
                CrawlMethodRun.status == "running",
                CrawlMethodRun.sandbox_job_id.like(
                    f"formal-scheduled-{morning_run_id}-%"
                ),
            )
        )
        return any(_owner_is_alive(run) for run in runs)

    return bool(_with_session(db, _has))


def recover_dead_method_fetch_runs(
    db: Session | None = None,
    *,
    run_id: int | None = None,
    cleanup_runtime: Any | None = None,
) -> int:
    """Converge dead formal owners after proving PID/start-token death and cleanup."""
    def _recover(session: Session) -> int:
        from app.discovery.domain_transition import domain_transition_lock
        from app.discovery.migration import record_formal_terminal

        query = select(CrawlMethodRun.id).where(
            CrawlMethodRun.status == "running",
            CrawlMethodRun.owner_id.is_not(None),
        )
        if run_id is not None:
            query = query.where(CrawlMethodRun.id == run_id)
        candidate_ids = list(session.scalars(query.order_by(CrawlMethodRun.id)))
        session.rollback()
        recovered = 0
        runtime = cleanup_runtime
        for candidate_id in candidate_ids:
            observed = session.get(CrawlMethodRun, candidate_id)
            if observed is None:
                continue
            method = session.get(CrawlMethod, observed.method_id)
            if method is None:
                session.rollback()
                continue
            domain = method.domain
            session.rollback()
            with domain_transition_lock(session, domain):
                run = session.scalar(
                    select(CrawlMethodRun)
                    .where(CrawlMethodRun.id == candidate_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if run is None or run.status != "running" or _owner_is_alive(run):
                    session.commit()
                    continue
                if run.sandbox_job_id:
                    if runtime is None:
                        from app.discovery.sandbox.runtime import get_formal_sandbox_runtime

                        runtime = get_formal_sandbox_runtime()
                    failures = runtime.cleanup_job_resources(run.sandbox_job_id)
                    if failures:
                        session.commit()
                        continue
                now = datetime.now(timezone.utc)
                requested = run.cancel_requested_at is not None
                terminal_status = "cancelled" if requested else "failed"
                changed = session.execute(
                    update(CrawlMethodRun)
                    .where(
                        CrawlMethodRun.id == run.id,
                        CrawlMethodRun.status == "running",
                        CrawlMethodRun.owner_id == run.owner_id,
                    )
                    .values(
                        status=terminal_status,
                        error_message=(
                            "fetch cancelled after owner process exited"
                            if requested
                            else "formal fetch owner process exited before completion"
                        ),
                        completed_at=now,
                        cancel_acknowledged_at=now if requested else None,
                    )
                )
                if changed.rowcount == 1:
                    # Terminal CAS and migration accounting commit together;
                    # no crash window can expose an unrecorded terminal run.
                    record_formal_terminal(
                        session,
                        run.id,
                        terminal_status,
                        completed_at=now,
                    )
                recovered += int(changed.rowcount == 1)
                session.commit()
        return recovered
    return int(_with_session(db, _recover))
