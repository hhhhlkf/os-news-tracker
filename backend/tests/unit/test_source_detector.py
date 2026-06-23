import pytest

from app.sources.detector import FetchResult, SourceDetectionError, detect_source


def test_detects_rss_feed():
    xml = """
    <rss version="2.0">
      <channel>
        <title>AlmaLinux Blog</title>
        <item><title>Release</title><link>https://example.com/release</link></item>
      </channel>
    </rss>
    """

    result = detect_source("https://example.com/feed.xml", fetcher=lambda _: FetchResult(xml))

    assert result.detected_type == "rss"
    assert result.name_suggestion == "AlmaLinux Blog"
    assert result.api_config is None
    assert "RSS/Atom" in result.notes[0]


def test_detects_json_api_with_common_items_path():
    payload = """
    {
      "items": [
        {
          "headline": "Kernel update",
          "html_url": "https://example.com/kernel",
          "created_at": "2026-06-22",
          "summary": "A useful summary"
        }
      ]
    }
    """

    result = detect_source("https://api.example.com/news", fetcher=lambda _: payload)

    assert result.detected_type == "api"
    assert result.name_suggestion == "api.example.com"
    assert result.api_config == {
        "probe": {
            "mode": "json_list",
            "items_path": "items",
            "fields": {
                "title": "headline",
                "url": "html_url",
                "published_at": "created_at",
                "content": ["summary"],
            },
        }
    }


def test_json_without_usable_fields_is_unrecognized():
    payload = """{"items": [{"id": 1, "value": "missing title and url"}]}"""

    with pytest.raises(SourceDetectionError):
        detect_source("https://api.example.com/news", fetcher=lambda _: payload)


def test_detects_html_page_monitor():
    html = "<!doctype html><html><head><title>Example News</title></head><body>Hello</body></html>"

    result = detect_source("https://www.example.com/news", fetcher=lambda _: html)

    assert result.detected_type == "page_monitor"
    assert result.name_suggestion == "Example News"
    assert result.api_config is None
    assert "page monitor" in result.notes[0]


def test_unrecognizable_content_raises():
    with pytest.raises(SourceDetectionError):
        detect_source("https://example.com/file.txt", fetcher=lambda _: "plain text")
