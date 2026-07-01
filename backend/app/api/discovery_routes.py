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
from app.discovery.ingester import CrawlOutputIngester
from app.discovery.interpreter import DslInterpreter
from app.models import CrawlMethod, CrawlMethodDomain, SiteDiscoveryRun

router = APIRouter(prefix="/discovery", tags=["discovery"])


class DiscoverRequest(BaseModel):
    url: HttpUrl
    force: bool = False  # true=跳过去重检查/覆盖同 domain 旧范式
    name: str | None = None  # 站点别名（选填，不填自动用域名）


def run_method(recipe: DslRecipe) -> dict:
    """运行命执行核心：按 DSL Recipe 纯确定性抓取。供 discovery_fetch 调用 + 测试 mock。"""
    return DslInterpreter().run(recipe)


@router.post("/run")
def discover_run(body: DiscoverRequest, db: Session = Depends(get_db)):
    """生成命：force=false 先查重，重复返回 duplicate；无重复/force=true 异步启动，返回 run_id 供轮询。"""
    from urllib.parse import urlparse
    site_url = str(body.url)
    if not body.force:
        existing = check_existing_method(site_url, db)
        if existing:
            return {"status": "duplicate", "existing_method": existing}
    # 别名：前端选填，不填自动用域名（复用 _domain_name 同款逻辑）
    name = body.name or urlparse(site_url).netloc.removeprefix("www.")
    run_id = start_discovery_run(site_url, force=body.force, name=name)
    return {"status": "started", "run_id": run_id, "name": name}


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
    """单 run 详情/轮询：含 node_trace 逐步轨迹 + current_step（前端高亮"进行到哪一步了"）。"""
    r = db.get(SiteDiscoveryRun, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    current_step = r.node_trace[-1]["step"] if r.node_trace else None
    return {"id": r.id, "site_url": r.site_url, "status": r.status,
            "resulting_method_id": r.resulting_method_id, "llm_token_usage": r.llm_token_usage,
            "node_trace": r.node_trace, "retry_count": r.retry_count,
            "current_step": current_step,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "error_message": r.error_message}


class MethodPatch(BaseModel):
    status: str | None = None  # active | disabled | failed


@router.get("/methods")
def list_methods(db: Session = Depends(get_db)):
    """列出所有已发现的爬取方式（摘要，不含完整 DSL Recipe）。"""
    ms = db.scalars(select(CrawlMethod).order_by(CrawlMethod.id.desc())).all()
    return [{"id": m.id, "domain": m.domain, "entry_url": m.entry_url, "status": m.status,
             "signature": m.signature, "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None,
             "last_run_status": m.last_run_status} for m in ms]


@router.get("/methods/{method_id}")
def get_method(method_id: int, db: Session = Depends(get_db)):
    """单方法详情，含完整 DSL Recipe（前端可展示/编辑）。"""
    m = db.get(CrawlMethod, method_id)
    if not m:
        raise HTTPException(404, "method not found")
    return {"id": m.id, "domain": m.domain, "entry_url": m.entry_url, "status": m.status,
            "dsl_recipe": m.dsl_recipe, "signature": m.signature,
            "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None}


@router.patch("/methods/{method_id}")
def patch_method(method_id: int, body: MethodPatch, db: Session = Depends(get_db)):
    """禁用/启用方法（改 status）。"""
    m = db.get(CrawlMethod, method_id)
    if not m:
        raise HTTPException(404, "method not found")
    if body.status:
        m.status = body.status
    db.commit()
    return {"id": m.id, "status": m.status}


@router.delete("/methods/{method_id}", status_code=204)
def delete_method(method_id: int, db: Session = Depends(get_db)):
    """删除方法 + 级联清 crawl_method_domains 映射。"""
    m = db.get(CrawlMethod, method_id)
    if not m:
        raise HTTPException(404, "method not found")
    db.query(CrawlMethodDomain).filter_by(method_id=method_id).delete()  # 级联清映射
    db.delete(m); db.commit()


@router.post("/methods/{method_id}/fetch")
def discovery_fetch(method_id: int, db: Session = Depends(get_db)):
    """运行命：按 DSL Recipe 抓取 + 接现有 pipeline 入 items（走 LLM Enricher 富化+打分）。"""
    from app.models import Source
    from app.pipeline import Pipeline
    from app.processing.enricher import Enricher
    m = db.get(CrawlMethod, method_id)
    if not m:
        raise HTTPException(404, "method not found")
    recipe = DslRecipe(**m.dsl_recipe)
    output = run_method(recipe)  # 纯确定性执行（可被测试 mock）
    # 转 RawItem → 走正常 pipeline 路径（调 Enricher LLM 富化：category/tags/summary/importance）
    raws = CrawlOutputIngester().to_raw_items(output, source_id=m.source_id)
    source = db.get(Source, m.source_id)
    pipeline = Pipeline(session=db, extractor=None, enricher=Enricher())
    stored = 0
    for raw in raws:
        if pipeline.process_item(source, raw):
            stored += 1
    m.last_run_at = datetime.now(timezone.utc)
    m.last_run_status = "ok" if stored > 0 else "empty"
    db.commit()
    return {
        "discovered_count": len(raws),
        "stored_count": stored,
        "items": output.get("items", []),
        "stats": output.get("stats", {}),
        "message": f"抓取 {len(raws)} 条，入库 {stored} 条" if stored > 0
                   else f"抓取 {len(raws)} 条，未入库（可能重复或被富化拒绝）",
    }
