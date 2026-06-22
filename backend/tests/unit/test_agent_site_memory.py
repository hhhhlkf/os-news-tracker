from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.agent.site_memory import SiteMemory
from app.models import AgentSiteMemory


def _make_db():
    """构造一个 mock 数据库会话，默认不返回任何记录。"""
    db = MagicMock()
    db.scalars.return_value.first.return_value = None
    return db


def test_get_returns_none_when_no_record():
    """无记录时 get 应返回 None。"""
    mem = SiteMemory()
    assert mem.get(db=_make_db(), source_id=1, url="https://example.com") is None


def test_get_returns_record_within_keep_ttl():
    """keep 记录在 7 天 TTL 内应正常返回。"""
    record = AgentSiteMemory(
        source_id=1, url_pattern="https://example.com",
        verdict="keep", quality_score=8, quality_reason="good",
        last_seen_at=datetime.now(timezone.utc),
        seen_count=1,
    )
    db = _make_db()
    db.scalars.return_value.first.return_value = record
    mem = SiteMemory()
    result = mem.get(db=db, source_id=1, url="https://example.com")
    assert result is not None
    assert result.verdict == "keep"


def test_get_returns_none_for_stale_keep_record():
    """keep 记录超过 7 天 TTL 应返回 None，触发重新评估。"""
    record = AgentSiteMemory(
        source_id=1, url_pattern="https://example.com",
        verdict="keep", quality_score=8, quality_reason="good",
        last_seen_at=datetime.now(timezone.utc) - timedelta(days=8),
        seen_count=3,
    )
    db = _make_db()
    db.scalars.return_value.first.return_value = record
    mem = SiteMemory()
    result = mem.get(db=db, source_id=1, url="https://example.com")
    assert result is None   # 过期 keep → 重新评估


def test_get_returns_discard_regardless_of_age():
    """discard 记录应永久保留，不论经过多久都应返回。"""
    record = AgentSiteMemory(
        source_id=1, url_pattern="https://example.com",
        verdict="discard", quality_score=2, quality_reason="bad",
        last_seen_at=datetime.now(timezone.utc) - timedelta(days=100),
        seen_count=5,
    )
    db = _make_db()
    db.scalars.return_value.first.return_value = record
    mem = SiteMemory()
    result = mem.get(db=db, source_id=1, url="https://example.com")
    assert result is not None   # discard 永久有效
    assert result.verdict == "discard"
