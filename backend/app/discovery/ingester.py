"""CrawlOutputIngester：把 DSL 产出 JSON 转成 RawItem，接入现有 pipeline。"""

from __future__ import annotations

from datetime import datetime, timezone

from dateutil import parser as dateparser

from app.schemas import RawItem


def parse_published_at(value: object) -> datetime | None:
    """把发布时间解析为 UTC 感知的 datetime。

    功能：支持 ISO8601 与 RSS RFC2822（如 arXiv pubDate）等格式；非法或为空时返回 None；无时区则补 UTC。
    谁会调用：CrawlOutputIngester.to_raw_items、recipe_prepare.py、quality_audit.py 在规整条目时间时调用。
    直接调用：
    - dateutil.parser.parse(...)：解析多种日期字符串。
    输入与结果：输入任意对象（None/空/字符串/datetime）；返回 UTC datetime 或 None。
    副作用：无。
    """
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
        """把 DSL 产出的 JSON 转为可入库的 RawItem 列表。

        功能：遍历产出 items，丢弃缺 url 或 title 的条目，用 parse_published_at 规整时间，
        并把 content/summary 或 title 作为 raw_content，交给正常 pipeline（含 LLM Enricher 富化）。
        谁会调用：runner.py 在方法试跑/正式抓取拿到产出后调用，用于接入现有入库流程。
        直接调用：
        - parse_published_at(...)：规整每条条目的发布时间。
        - RawItem(...)：构造 schema 对象。
        输入与结果：输入产出 dict 与 source_id；返回 RawItem 列表（仅含关键字段齐全的条目）。
        副作用：无（仅构造内存对象，不写库）。
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
