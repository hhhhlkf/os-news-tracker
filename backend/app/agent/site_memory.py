"""站点记忆模块 — 缓存 Agent 爬取的质量评估结果。

- keep 记录 7 天后过期，内容可能已更新，需重新评估
- discard 记录永久有效，除非管理员手动清除
- PlanAgent 使用 should_skip() 跳过已知低质量 URL
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.schemas import QualityResult
from app.models import AgentSiteMemory

logger = logging.getLogger(__name__)

# keep 记录的有效期（天）
_KEEP_TTL_DAYS = 7


def _ensure_aware(dt: datetime) -> datetime:
    """将 naive datetime 转为 UTC-aware；已是 aware 则直接返回。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class SiteMemory:
    """agent_site_memory 表的读写接口。

    使用方式：
        memory = SiteMemory()
        cached = memory.get(db, source_id, url)       # 查询缓存
        memory.upsert(db, source_id, url, result)      # 写入评估结果
        if memory.should_skip(db, source_id, url):     # 是否应跳过
            ...
    """

    def get(self, db: Session, source_id: int, url: str) -> AgentSiteMemory | None:
        """查询指定 URL 的缓存记录。

        返回 None 的情况：
        - 无记录
        - keep 记录已超过 TTL（需重新评估）

        discard 记录无论多久都会返回。
        """
        record = db.scalars(
            select(AgentSiteMemory)
            .where(
                AgentSiteMemory.source_id == source_id,
                AgentSiteMemory.url_pattern == url,
            )
        ).first()

        if record is None:
            return None

        # keep 记录需要检查 TTL
        if record.verdict == "keep":
            last_seen = _ensure_aware(record.last_seen_at)
            age = datetime.now(timezone.utc) - last_seen
            if age > timedelta(days=_KEEP_TTL_DAYS):
                logger.debug(
                    "site_memory: keep 记录已过期 %s，将重新评估", url
                )
                return None

        return record

    def upsert(
        self, db: Session, source_id: int, url: str, result: QualityResult
    ) -> None:
        """插入或更新一条站点记忆记录。

        如果 URL 已存在则更新评分和状态；否则创建新记录。
        """
        record = db.scalars(
            select(AgentSiteMemory)
            .where(
                AgentSiteMemory.source_id == source_id,
                AgentSiteMemory.url_pattern == url,
            )
        ).first()

        if record is None:
            # 新记录
            record = AgentSiteMemory(
                source_id=source_id,
                url_pattern=url,
                quality_score=result.score,
                quality_reason=result.reason,
                verdict=result.verdict,
                relevant_topic=result.relevant_topic,
            )
            db.add(record)
        else:
            # 更新已有记录
            record.quality_score = result.score
            record.quality_reason = result.reason
            record.verdict = result.verdict
            record.relevant_topic = result.relevant_topic
            record.last_seen_at = datetime.now(timezone.utc)
            record.seen_count += 1

        db.commit()
        logger.info(
            "site_memory: upsert %s → %s (score=%s)",
            url, result.verdict, result.score,
        )

    def should_skip(self, db: Session, source_id: int, url: str) -> bool:
        """判断 PlanAgent 是否应跳过此 URL。

        规则：discard 且 seen_count >= 2 的 URL 直接跳过，
        避免反复抓取已知低质量页面。
        """
        record = db.scalars(
            select(AgentSiteMemory)
            .where(
                AgentSiteMemory.source_id == source_id,
                AgentSiteMemory.url_pattern == url,
            )
        ).first()

        if record is None:
            return False

        return record.verdict == "discard" and record.seen_count >= 2
