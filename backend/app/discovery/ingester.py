"""CrawlOutputIngester：把 DSL 产出 JSON 转成 RawItem，接入现有 pipeline。"""

from __future__ import annotations

from datetime import datetime, timezone

from dateutil import parser as dateparser

from app.schemas import RawItem


def parse_published_at(value: object) -> datetime | None:
    """Parse published_at from ISO8601 or RSS RFC2822 (e.g. arXiv pubDate)."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = dateparser.parse(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class CrawlOutputIngester:
    def to_raw_items(self, output: dict, *, source_id: int) -> list[RawItem]:
        """DSL 产出 items → RawItem 列表。丢弃无 url/title 的条目。

        走正常 pipeline 路径（调 LLM Enricher 富化），不设 agent_item 旁路。
        raw_content 无 content/summary 时回退 title，给 enricher 至少有文本可富化。
        """
        raws: list[RawItem] = []
        for it in output.get("items", []):
            url = it.get("url")
            title = it.get("title")
            if not url or not title:
                continue  # 缺关键字段，丢弃
            published_at = parse_published_at(it.get("published_at"))
            raws.append(RawItem(
                source_id=source_id, title=str(title), url=str(url),
                raw_content=it.get("content") or it.get("summary") or str(title),
                published_at=published_at,
            ))
        return raws
