"""一次性脚本：把 items.why_it_matters 全部重写成 1 句话。

运行：cd backend && python -m scripts.rewrite_reasons
（DATABASE_URL / LLM_* 走项目现有 .env 配置）
"""

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Item
from app.processing.reason import generate_recommendation_reason


def main() -> None:
    db = SessionLocal()
    try:
        items = db.scalars(
            select(Item).where(Item.why_it_matters.is_not(None))
        ).all()
        print(f"待重写条目：{len(items)}")
        for i, item in enumerate(items, 1):
            try:
                item.why_it_matters = generate_recommendation_reason(item)
                db.commit()
                print(f"[{i}/{len(items)}] id={item.id} ok")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"[{i}/{len(items)}] id={item.id} 失败: {exc}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
