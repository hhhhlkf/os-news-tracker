"""新端点：/discovery/run（生成命）+ /discovery/methods/{id}/fetch（运行命）。

纯增量：不替换旧 /sources/discover，不接入 agent_crawl 主流程。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, HttpUrl
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.discovery.dsl import DslRecipe
from app.discovery.graph import check_existing_method, run_discovery
from app.discovery.interpreter import DslInterpreter
from app.models import CrawlMethod

router = APIRouter(prefix="/discovery", tags=["discovery"])


class DiscoverRequest(BaseModel):
    url: HttpUrl
    force: bool = False  # true=跳过去重检查/覆盖同 domain 旧范式


def run_method(recipe: DslRecipe) -> dict:
    """运行命执行核心：按 DSL Recipe 纯确定性抓取。供 discovery_fetch 调用 + 测试 mock。"""
    return DslInterpreter().run(recipe)


@router.post("/run")
def discover_run(body: DiscoverRequest, db: Session = Depends(get_db)):
    """生成命：force=false 先查重，重复返回 duplicate 不跑；force=true 覆盖。

    第一子项目同步调用 run_discovery（Task 15 改异步）。
    """
    site_url = str(body.url)
    if not body.force:
        existing = check_existing_method(site_url, db)
        if existing:
            return {"status": "duplicate", "existing_method": existing}
    result = run_discovery(site_url, force=body.force)
    return {"status": "completed", **result}


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
