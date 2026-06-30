"""新端点：/discovery/run（生成命）+ /discovery/methods/{id}/fetch（运行命）。

纯增量：不替换旧 /sources/discover，不接入 agent_crawl 主流程。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.discovery.dsl import DslRecipe
from app.discovery.graph import check_existing_method, start_discovery_run
from app.discovery.interpreter import DslInterpreter
from app.models import CrawlMethod, SiteDiscoveryRun

router = APIRouter(prefix="/discovery", tags=["discovery"])


class DiscoverRequest(BaseModel):
    url: HttpUrl
    force: bool = False  # true=跳过去重检查/覆盖同 domain 旧范式


def run_method(recipe: DslRecipe) -> dict:
    """运行命执行核心：按 DSL Recipe 纯确定性抓取。供 discovery_fetch 调用 + 测试 mock。"""
    return DslInterpreter().run(recipe)


@router.post("/run")
def discover_run(body: DiscoverRequest, db: Session = Depends(get_db)):
    """生成命：force=false 先查重，重复返回 duplicate 不跑；无重复/force=true 异步启动，返回 run_id 供轮询。"""
    site_url = str(body.url)
    if not body.force:
        existing = check_existing_method(site_url, db)
        if existing:
            return {"status": "duplicate", "existing_method": existing}
    run_id = start_discovery_run(site_url, force=body.force)
    return {"status": "started", "run_id": run_id}


@router.get("/runs")
def list_discovery_runs(limit: int = 20, db: Session = Depends(get_db)):
    """列出生成命历史（最近 limit 条），按 started_at 倒序。"""
    runs = db.scalars(
        select(SiteDiscoveryRun).order_by(SiteDiscoveryRun.started_at.desc()).limit(limit)
    ).all()
    return [{"id": r.id, "site_url": r.site_url, "status": r.status,
             "resulting_method_id": r.resulting_method_id, "llm_token_usage": r.llm_token_usage,
             "started_at": r.started_at.isoformat() if r.started_at else None,
             "ended_at": r.ended_at.isoformat() if r.ended_at else None,
             "error_message": r.error_message} for r in runs]


@router.get("/runs/{run_id}")
def get_discovery_run(run_id: int, db: Session = Depends(get_db)):
    """单 run 详情/轮询：含 node_trace 审计。前端轮询此端点看 status。"""
    r = db.get(SiteDiscoveryRun, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    return {"id": r.id, "site_url": r.site_url, "status": r.status,
            "resulting_method_id": r.resulting_method_id, "llm_token_usage": r.llm_token_usage,
            "node_trace": r.node_trace, "retry_count": r.retry_count,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "error_message": r.error_message}


@router.post("/methods/{method_id}/fetch")
def discovery_fetch(method_id: int, db: Session = Depends(get_db)):
    """运行命：按已存的 DSL Recipe 执行抓取，零 LLM。"""
    m = db.get(CrawlMethod, method_id)
    if not m:
        raise HTTPException(404, "method not found")
    recipe = DslRecipe(**m.dsl_recipe)
    output = run_method(recipe)  # 纯确定性执行（可被测试 mock）
    m.last_run_at = datetime.now(timezone.utc)
    db.commit()
    return output
