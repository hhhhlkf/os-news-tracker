"""CrawlOutputIngester 单元测试 — DSL 产出 JSON → RawItem 转换 + 字段过滤。"""

from app.discovery.ingester import CrawlOutputIngester


def test_ingest_converts_to_raw_items():
    output = {"items": [
        {"title": "A", "url": "https://x.com/1", "published_at": "2026-06-01T00:00:00Z", "content": "body A"},
        {"title": "B", "url": "https://x.com/2"},
    ], "stats": {"discovered_count": 2}}
    raws = CrawlOutputIngester().to_raw_items(output, source_id=10)
    assert len(raws) == 2
    assert raws[0].source_id == 10
    assert raws[0].url == "https://x.com/1"
    assert raws[0].title == "A"
    assert raws[0].raw_content == "body A"
    assert raws[0].published_at is not None


def test_ingest_drops_item_without_url():
    output = {"items": [{"title": "no url"}], "stats": {"discovered_count": 0}}
    raws = CrawlOutputIngester().to_raw_items(output, source_id=10)
    assert raws == []


def test_ingest_drops_item_without_title():
    output = {"items": [{"url": "https://x.com/1"}], "stats": {"discovered_count": 0}}
    raws = CrawlOutputIngester().to_raw_items(output, source_id=10)
    assert raws == []


def test_ingest_falls_back_to_summary_field():
    output = {"items": [{"title": "A", "url": "https://x.com/1", "summary": "sum A"}], "stats": {}}
    raws = CrawlOutputIngester().to_raw_items(output, source_id=7)
    assert raws[0].raw_content == "sum A"
