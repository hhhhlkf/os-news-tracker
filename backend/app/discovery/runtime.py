from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from sqlalchemy import func, or_, select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models import SiteDiscoveryRun


_start_lock = Lock()


@dataclass
class DiscoveryCapacityExceeded(Exception):
    active_count: int
    max_concurrent: int

    def __str__(self) -> str:
        """返回并发超限的可读错误信息，供日志与接口响应使用。

        功能：把当前活跃数与上限拼成一句中文提示。
        谁会调用：异常被抛出后由调用方/日志框架读取。
        直接调用：无。
        输入与结果：无参数；返回格式化的错误字符串。
        副作用：无。
        """
        return f"智能探查并发已满（{self.active_count}/{self.max_concurrent}），请稍后再试"


def count_active_discovery_runs(db) -> int:
    """统计当前处于 running 状态的探查运行数量。

    功能：对 SiteDiscoveryRun 表按 status='running' 计数，用于并发上限判断。
    谁会调用：create_discovery_run_or_raise 在创建新运行前检查并发；亦被运行管理逻辑直接查询。
    直接调用：
    - SQLAlchemy select/func.count(...)：构造并执行的计数查询（db.scalar）。
    输入与结果：输入数据库会话 db；返回当前活跃运行数（整数）。
    副作用：只读查询数据库，无写入。
    """
    return int(
        db.scalar(
            select(func.count())
            .select_from(SiteDiscoveryRun)
            .where(SiteDiscoveryRun.status.in_(("running", "repairing")))
        )
        or 0
    )


def create_discovery_run_or_raise(
    site_url: str,
    *,
    source_kind: str = "website",
    trigger_type: str = "manual",
    repair_method_id: int | None = None,
    runtime_version: str | None = None,
) -> int:
    """在并发上限内创建一条 running 状态的探查运行记录。

    功能：先加锁统计活跃运行数，若已达 discovery_max_concurrent_runs 上限则抛出 DiscoveryCapacityExceeded，
    否则新建 SiteDiscoveryRun 记录并落库，返回新记录 id。
    谁会调用：website_workflow.start_website_discovery_run、multi_graph.start_multi_discovery_run 在启动探查时调用。
    直接调用：
    - count_active_discovery_runs(...)：获取当前活跃运行数。
    - get_settings(...)：读取并发上限配置。
    - SessionLocal(...)：获取数据库会话并执行 db.add/db.commit。
    输入与结果：输入站点 URL；返回新建运行的 run_id（整数），超并发时抛 DiscoveryCapacityExceeded。
    副作用：向 SiteDiscoveryRun 表写入一条 running 记录并提交；修改全局并发计数。
    """
    settings = get_settings()
    max_concurrent = max(1, int(settings.discovery_max_concurrent_runs or 3))
    with _start_lock:
        db = SessionLocal()
        try:
            active_count = count_active_discovery_runs(db)
            if active_count >= max_concurrent:
                raise DiscoveryCapacityExceeded(active_count=active_count, max_concurrent=max_concurrent)
            if source_kind not in {"website", "wechat", "internal_forum", "unknown"}:
                raise ValueError("invalid Discovery source kind")
            run = SiteDiscoveryRun(
                site_url=site_url,
                source_kind=source_kind,
                status="repairing" if trigger_type == "repair" else "running",
                trigger_type=trigger_type,
                phase="context",
                round=0,
                repair_method_id=repair_method_id,
                runtime_version=runtime_version,
            )
            db.add(run)
            db.commit()
            return run.id
        finally:
            db.close()


def finish_discovery_run(
    run_id: int,
    *,
    status: str,
    resulting_method_id: int | None = None,
    node_trace: list | None = None,
    error_message: str | None = None,
    llm_token_usage: int | None = None,
    phase: str | None = None,
    round_number: int | None = None,
    checkpoint_path: str | None = None,
    runtime_version: str | None = None,
) -> None:
    """收尾一条探查运行：更新最终状态、产物方法、追踪、错误与 LLM 用量。

    功能：按 run_id 取回运行记录，写入最终 status、产出的方法 id、节点追踪、错误信息与 LLM token 用量，
    并打上结束时间后提交；记录不存在时静默返回。
    谁会调用：website_workflow、multi_graph 在探查流程成功/失败结束处调用。
    直接调用：
    - SessionLocal(...)：获取数据库会话，db.get 取记录，db.commit 提交更新。
    输入与结果：输入 run_id 与若干可选收尾字段；无返回值。
    副作用：更新 SiteDiscoveryRun 表对应记录并提交。
    """
    db = SessionLocal()
    try:
        values: dict[str, object] = {
            "status": status,
            "resulting_method_id": resulting_method_id,
            "error_message": error_message,
            "ended_at": datetime.now(timezone.utc),
        }
        if node_trace is not None:
            values["node_trace"] = node_trace
        if llm_token_usage is not None:
            values["llm_token_usage"] = llm_token_usage
        if phase is not None:
            values["phase"] = phase
        if round_number is not None:
            values["round"] = round_number
        if checkpoint_path is not None:
            values["checkpoint_path"] = checkpoint_path
        if runtime_version is not None:
            values["runtime_version"] = runtime_version
        # One conditional UPDATE closes the read/update race with cancellation.
        # Terminal states may be updated idempotently, never crossed.
        db.execute(
            update(SiteDiscoveryRun)
            .where(
                SiteDiscoveryRun.id == run_id,
                or_(
                    SiteDiscoveryRun.status.notin_(("cancelled", "completed")),
                    SiteDiscoveryRun.status == status,
                ),
            )
            .values(**values)
        )
        db.commit()
    finally:
        db.close()
