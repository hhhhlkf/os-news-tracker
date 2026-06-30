"""CrawlOutputIngester：把 DSL 产出 JSON 转成 RawItem，接入现有 pipeline。"""

from __future__ import annotations

from datetime import datetime

from app.schemas import RawItem


class CrawlOutputIngester:
    def to_raw_items(self, output: dict, *, source_id: int) -> list[RawItem]:
        """DSL 产出 items → RawItem 列表。丢弃无 url/title 的条目。"""
        raws: list[RawItem] = []
        for it in output.get("items", []):
            url = it.get("url")
            title = it.get("title")
            if not url or not title:
                continue  # 缺关键字段，丢弃
            pub = it.get("published_at")
            # ISO8601 Z 后缀转 +00:00 以兼容 fromisoformat
            published_at = datetime.fromisoformat(pub.replace("Z", "+00:00")) if pub else None
            raws.append(RawItem(
                source_id=source_id, title=str(title), url=str(url),
                raw_content=it.get("content") or it.get("summary"),
                published_at=published_at,
            ))
        return raws
