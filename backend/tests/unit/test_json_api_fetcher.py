from datetime import timezone
from typing import Any

from app.fetchers.json_api import GenericJsonApiFetcher
from app.models import Source


class FakeRequester:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(
        self, url: str, params: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        self.calls.append((url, params, headers))
        return self.responses.pop(0)


def test_generic_json_api_fetcher_maps_items_and_cleans_html() -> None:
    requester = FakeRequester(
        [
            {
                "data": {
                    "items": [
                        {
                            "no": "123",
                            "title": "Kernel news",
                            "happenDate": "2026-06-10T16:00:00.000+0000",
                            "content": "<p>Hello <b>kernel</b></p>",
                        }
                    ],
                    "hasMore": False,
                }
            }
        ]
    )
    source = Source(
        id=7,
        name="JSON source",
        type="api",
        url="https://example.com/news.json",
        api_config={
            "items_path": "data.items",
            "headers": {"Referer": "https://example.com/news/"},
            "params": {"currentPage": 1},
            "pagination": {
                "page_param": "currentPage",
                "start_page": 1,
                "has_more_path": "data.hasMore",
                "max_pages": 1,
            },
            "fields": {
                "title": "title",
                "url_template": "https://example.com/news/{no}",
                "published_at": "happenDate",
                "content": "content",
            },
            "content_format": "html",
            "min_content_len": 5,
        },
    )

    items = GenericJsonApiFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert items[0].source_id == 7
    assert items[0].title == "Kernel news"
    assert items[0].url == "https://example.com/news/123"
    assert items[0].raw_content == "Hello kernel"
    assert items[0].published_at is not None
    assert items[0].published_at.tzinfo == timezone.utc
    assert requester.calls == [
        (
            "https://example.com/news.json",
            {"currentPage": 1},
            {"Referer": "https://example.com/news/"},
        )
    ]


def test_generic_json_api_fetcher_uses_field_fallbacks_and_filters_short_content() -> None:
    requester = FakeRequester(
        [
            {
                "data": {
                    "items": [
                        {
                            "no": "short",
                            "titleEn": "Image-only post",
                            "happenDate": "2026-06-10",
                            "content": "",
                            "abstractsEn": "tiny",
                        },
                        {
                            "no": "long",
                            "titleEn": "Fallback title",
                            "happenDate": "2026-06-11",
                            "content": "",
                            "contentEn": "A sufficiently detailed body about operating systems.",
                        },
                    ]
                }
            }
        ]
    )
    source = Source(
        id=8,
        name="JSON fallback source",
        type="api",
        url="https://example.com/news.json",
        api_config={
            "items_path": "data.items",
            "fields": {
                "title": ["title", "titleEn"],
                "url_template": "https://example.com/news/{no}",
                "published_at": "happenDate",
                "content": ["content", "contentEn", "abstracts", "abstractsEn"],
            },
            "min_content_len": 20,
        },
    )

    items = GenericJsonApiFetcher(requester=requester).fetch(source)

    assert [item.title for item in items] == ["Fallback title"]
    assert items[0].url == "https://example.com/news/long"
    assert items[0].raw_content == "A sufficiently detailed body about operating systems."


def test_generic_json_api_fetcher_paginates_until_has_more_is_false() -> None:
    requester = FakeRequester(
        [
            {
                "data": {
                    "items": [
                        {
                            "id": "1",
                            "title": "First",
                            "published": "2026-06-10",
                            "body": "first body",
                        }
                    ],
                    "hasMore": True,
                }
            },
            {
                "data": {
                    "items": [
                        {
                            "id": "2",
                            "title": "Second",
                            "published": "2026-06-11",
                            "body": "second body",
                        }
                    ],
                    "hasMore": False,
                }
            },
        ]
    )
    source = Source(
        id=9,
        name="Paged JSON source",
        type="api",
        url="https://example.com/news.json",
        api_config={
            "params": {"page": 1, "fixed": "yes"},
            "pagination": {
                "page_param": "page",
                "start_page": 1,
                "has_more_path": "data.hasMore",
                "max_pages": 5,
            },
            "items_path": "data.items",
            "fields": {
                "title": "title",
                "url_template": "https://example.com/news/{id}",
                "published_at": "published",
                "content": "body",
            },
            "min_content_len": 1,
        },
    )

    items = GenericJsonApiFetcher(requester=requester).fetch(source)

    assert [item.url for item in items] == [
        "https://example.com/news/1",
        "https://example.com/news/2",
    ]
    assert [call[1]["page"] for call in requester.calls] == [1, 2]
    assert all(call[1]["fixed"] == "yes" for call in requester.calls)


def test_generic_json_api_fetcher_fetches_multiple_configured_endpoints() -> None:
    requester = FakeRequester(
        [
            {
                "data": {
                    "items": [
                        {
                            "no": "a",
                            "title": "Category A",
                            "date": "2026-06-10",
                            "body": "category a body",
                        }
                    ]
                }
            },
            {
                "data": {
                    "items": [
                        {
                            "no": "b",
                            "title": "Category B",
                            "date": "2026-06-11",
                            "body": "category b body",
                        }
                    ]
                }
            },
        ]
    )
    source = Source(
        id=10,
        name="Multi endpoint JSON source",
        type="api",
        url="https://example.com/categories",
        api_config={
            "endpoints": [
                {"url": "https://example.com/category-a.json"},
                {"url": "https://example.com/category-b.json"},
            ],
            "items_path": "data.items",
            "fields": {
                "title": "title",
                "url_template": "https://example.com/news/{no}",
                "published_at": "date",
                "content": "body",
            },
            "min_content_len": 1,
        },
    )

    items = GenericJsonApiFetcher(requester=requester).fetch(source)

    assert [item.title for item in items] == ["Category A", "Category B"]
    assert [call[0] for call in requester.calls] == [
        "https://example.com/category-a.json",
        "https://example.com/category-b.json",
    ]
