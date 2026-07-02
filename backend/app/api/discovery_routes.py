"""新端点：/discovery/run（生成命）+ /discovery/methods/{id}/fetch（运行命）。

纯增量：不替换旧 /sources/discover，不接入 agent_crawl 主流程。
"""

from datetime import datetime, timedelta, timezone

import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, HttpUrl
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.discovery.dsl import DslRecipe
from app.discovery.cancel import request_cancel
from app.discovery.graph import check_existing_method, start_discovery_run
from app.discovery.ingester import CrawlOutputIngester
from app.discovery.interpreter import DslInterpreter
from app.llm.client import LlmClient
from app.models import CrawlMethod, CrawlMethodDomain, SiteDiscoveryRun
from app.schemas import ManualNewsRunRequest

router = APIRouter(prefix="/discovery", tags=["discovery"])
logger = logging.getLogger(__name__)
SUGGEST_NAME_LLM_TIMEOUT_SECONDS = 5.0
MAX_SUGGEST_NAME_LENGTH = 20
_RELATIVE_RANGE_TO_DELTA = {
    "24h": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


class DiscoverRequest(BaseModel):
    url: HttpUrl
    force: bool = False  # true=跳过去重检查/覆盖同 domain 旧范式
    name: str | None = None  # 站点别名（选填，不填自动用域名）


def run_method(recipe: DslRecipe) -> dict:
    """运行命执行核心：按 DSL Recipe 纯确定性抓取。供 discovery_fetch 调用 + 测试 mock。"""
    return DslInterpreter().run(recipe)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _apply_fetch_limits(items: list[dict], request: ManualNewsRunRequest | None) -> list[dict]:
    if request is None:
        return items

    now = datetime.now(timezone.utc)
    filtered: list[tuple[datetime, dict]] = []
    for item in items:
        published_at_raw = item.get("published_at")
        if not isinstance(published_at_raw, str):
            continue
        try:
            published_at = datetime.fromisoformat(published_at_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        published_at = _as_utc(published_at)
        if request.time_mode == "relative":
            lower_bound = now - _RELATIVE_RANGE_TO_DELTA[request.relative_range]
            if published_at < lower_bound:
                continue
        else:
            start_at = _as_utc(request.start_at)
            end_at = _as_utc(request.end_at)
            if published_at < start_at or published_at > end_at:
                continue
        filtered.append((published_at, item))

    filtered.sort(key=lambda entry: entry[0], reverse=True)
    return [item for _, item in filtered[:request.target_count]]


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


@router.post("/runs/{run_id}/cancel")
def cancel_discovery_run(run_id: int, db: Session = Depends(get_db)):
    """手动取消生成命：停止后续节点/LLM/API 调用，并把状态标为 cancelled。"""
    r = db.get(SiteDiscoveryRun, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    if r.status != "running":
        current_step = r.node_trace[-1]["step"] if r.node_trace else None
        return {"id": r.id, "site_url": r.site_url, "status": r.status,
                "resulting_method_id": r.resulting_method_id, "llm_token_usage": r.llm_token_usage,
                "node_trace": r.node_trace, "retry_count": r.retry_count,
                "current_step": current_step,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "error_message": r.error_message}
    request_cancel(run_id)
    r.status = "cancelled"
    r.error_message = "已手动取消"
    r.ended_at = datetime.now(timezone.utc)
    db.commit()
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
            "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None,
            "last_run_status": m.last_run_status}


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
    db.execute(
        update(SiteDiscoveryRun)
        .where(SiteDiscoveryRun.resulting_method_id == method_id)
        .values(resulting_method_id=None)
    )
    db.query(CrawlMethodDomain).filter_by(method_id=method_id).delete()  # 级联清映射
    db.delete(m); db.commit()


@router.post("/methods/{method_id}/fetch")
def discovery_fetch(
    method_id: int,
    request: ManualNewsRunRequest | None = Body(default=None),
    db: Session = Depends(get_db),
):
    """运行命：按 DSL Recipe 抓取 + 接现有 pipeline 入 items（走 LLM Enricher 富化+打分）。"""
    from app.models import Source
    from app.pipeline import Pipeline
    from app.processing.enricher import Enricher
    from app.run_logs import append_run_log
    m = db.get(CrawlMethod, method_id)
    if not m:
        raise HTTPException(404, "method not found")
    recipe = DslRecipe(**m.dsl_recipe)
    append_run_log(
        "抓方式",
        "开始抓取爬取方式",
        source=m.domain,
        method_id=m.id,
        entry_url=m.entry_url,
        status=m.status,
    )
    stored = 0
    try:
        output = run_method(recipe)  # 纯确定性执行（可被测试 mock）
        output["items"] = _apply_fetch_limits(list(output.get("items", [])), request)
        append_run_log(
            "抓方式",
            "DSL 执行完成，准备入库",
            source=m.domain,
            method_id=m.id,
            discovered_count=len(output.get("items", [])),
            limit_applied=bool(request),
        )
        # 转 RawItem → 走正常 pipeline 路径（调 Enricher LLM 富化：category/tags/summary/importance）
        raws = CrawlOutputIngester().to_raw_items(output, source_id=m.source_id)
        source = db.get(Source, m.source_id)
        pipeline = Pipeline(session=db, extractor=None, enricher=Enricher())
        for raw in raws:
            if pipeline.process_item(source, raw):
                stored += 1
        append_run_log(
            "抓方式",
            "爬取方式抓取完成",
            source=m.domain,
            method_id=m.id,
            discovered_count=len(raws),
            stored_count=stored,
            summary=(
                f"抓取 {len(raws)} 条，入库 {stored} 条"
                if stored > 0
                else f"抓取 {len(raws)} 条，未入库（可能重复或被富化拒绝）"
            ),
        )
    except Exception as exc:
        append_run_log(
            "抓方式",
            f"爬取方式抓取失败 · {exc}",
            source=m.domain,
            method_id=m.id,
            level="error",
        )
        raise
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


class SuggestNameRequest(BaseModel):
    url: HttpUrl


def _extract_title(html: str) -> str | None:
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title or None


def _normalize_site_name(value: str | None) -> str | None:
    import re

    if not value:
        return None
    cleaned = value.strip().strip("'\"“”‘’`")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[。！？!?,，；;：:]+$", "", cleaned).strip()
    cleaned = cleaned[:MAX_SUGGEST_NAME_LENGTH].strip()
    return cleaned or None


def _suggest_name_with_llm(site_url: str, domain: str, title: str | None) -> str | None:
    prompt = (
        "你是一个网站命名助手。"
        "请根据给定的网站信息，生成一个适合作为站点名称的短标题。"
        "要求：\n"
        f"1. 最终结果不超过{MAX_SUGGEST_NAME_LENGTH}个字符；\n"
        "2. 可以是中文、英文或中英文混合短语；\n"
        "3. 像站点名，不要写解释；\n"
        "4. 只输出名称本身。\n\n"
        f"URL: {site_url}\n"
        f"域名: {domain}\n"
        f"页面标题: {title or '(无标题)'}\n"
    )
    result = LlmClient().complete(
        prompt,
        temperature=0.1,
        timeout=SUGGEST_NAME_LLM_TIMEOUT_SECONDS,
    )
    return _normalize_site_name(result)


@router.post("/suggest-name")
def suggest_name(body: SuggestNameRequest):
    """优先用 LLM 生成站点短名；失败时回退<title>，再回退域名。"""
    from urllib.parse import urlparse
    import httpx
    site_url = str(body.url)
    domain = urlparse(site_url).netloc.removeprefix("www.")
    title = None
    try:
        logger.info("suggest-name title fetch start url=%s", site_url)
        resp = httpx.get(site_url, timeout=8.0, follow_redirects=True,
                         headers={"User-Agent": "os-news-tracker/discovery"})
        if resp.status_code < 400:
            title = _extract_title(resp.text)
        logger.info(
            "suggest-name title fetch done url=%s status=%s title=%s",
            site_url,
            resp.status_code,
            (title or "")[:80],
        )
    except Exception:
        logger.exception("suggest-name title fetch failed url=%s", site_url)

    try:
        logger.info(
            "suggest-name llm start url=%s domain=%s has_title=%s timeout=%s",
            site_url,
            domain,
            bool(title),
            SUGGEST_NAME_LLM_TIMEOUT_SECONDS,
        )
        llm_name = _suggest_name_with_llm(site_url, domain, title)
        if llm_name:
            logger.info("suggest-name llm success url=%s name=%s", site_url, llm_name)
            return {"name": llm_name}
    except Exception:
        logger.exception("suggest-name llm failed url=%s", site_url)

    fallback = _normalize_site_name(title) or _normalize_site_name(domain) or domain[:MAX_SUGGEST_NAME_LENGTH]
    logger.info("suggest-name fallback url=%s name=%s", site_url, fallback)
    return {"name": fallback}
