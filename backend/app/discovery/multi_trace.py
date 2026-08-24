"""DEPRECATED / UNUSED: retired multi-graph trace writer.

No production module imports this file.  Active Discovery persists ordered
events through ``discovery.events`` and checkpoints through
``discovery.checkpoints``.  The implementation is intentionally retained for
now as historical reference and must not receive new callers.

旧功能：把多来源探查阶段转换成前端可展示的轨迹，并持久化到运行记录。
当前调用者：无。
会调用谁：``SiteDiscoveryRun`` 与数据库会话；不负责路由、抓取或保存方式。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def append_run_trace(run_id: int, trace: list[dict[str, Any]], step: str, summary: dict[str, Any]) -> None:
    """DEPRECATED / UNUSED: append a trace in the retired node-trace format.

    功能：把阶段名称、完成状态、时间和摘要追加到内存轨迹；运行仍进行中时写回数据库。
    当前调用者：无；新路径使用持久化 Discovery events。
    直接调用：``SessionLocal`` 获取会话，``SiteDiscoveryRun`` 保存最新轨迹。
    输入与结果：输入运行 ID、轨迹列表、阶段名称和摘要；无返回值。
    副作用：更新 ``SiteDiscoveryRun.node_trace`` 并提交数据库事务。
    """
    from app.db import SessionLocal
    from app.models import SiteDiscoveryRun

    trace.append({
        "step": step,
        "status": "done",
        "ts": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
    })
    db = SessionLocal()
    try:
        run = db.get(SiteDiscoveryRun, run_id)
        if run and run.status == "running":
            run.node_trace = list(trace)
            db.commit()
    finally:
        db.close()
