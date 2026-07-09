"""主分类读取助手：优先读 DB（main_categories 表），异常/空时回退到内置常量。"""
from __future__ import annotations

import logging

from app.enums import MAIN_CATEGORIES as _DEFAULT_MAIN_CATEGORIES

logger = logging.getLogger(__name__)


def get_main_category_names(session=None) -> list[str]:
    """按 sort_order 返回主分类名称列表；DB 不可用或为空时回退到内置默认。

    传入 session 则复用；否则自开自关一个短会话（供 enricher 等无 session 处调用）。
    """
    own = session is None
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import MainCategory

        if own:
            session = SessionLocal()
        try:
            rows = session.scalars(
                select(MainCategory).order_by(MainCategory.sort_order, MainCategory.id)
            ).all()
            names = [r.name for r in rows if r.name and r.name.strip()]
            if names:
                return names
        finally:
            if own and session is not None:
                session.close()
    except Exception:  # noqa: BLE001 - 主分类读取失败不能阻塞富集/校验
        logger.debug("get_main_category_names fell back to defaults", exc_info=True)
    return list(_DEFAULT_MAIN_CATEGORIES)
