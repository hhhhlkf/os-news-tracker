from __future__ import annotations

from pathlib import Path

import pytest

from app.sources.registry import _load_source_entries
from app.sources.seed_source_probe import (
    ProbeStatus,
    build_probe_fetcher,
    probe_routes_for_manifest,
    validate_sample_items,
)
from app.models import Source
from app.schemas import RawItem


SEED_PATH = (
    Path(__file__).resolve().parents[2] / "app" / "sources" / "seed_sources.yaml"
)


@pytest.mark.parametrize(
    "entry",
    _load_source_entries(SEED_PATH),
    ids=lambda entry: entry["name"],
)
def test_every_seed_source_has_deterministic_probe_route(entry):
    route = probe_routes_for_manifest(SEED_PATH)[entry["name"]]

    assert route.source_name == entry["name"]
    assert route.source_type == entry["type"]
    assert route.url == entry.get("url", "")
    assert route.status in {ProbeStatus.SUPPORTED, ProbeStatus.UNIMPLEMENTED}
    if route.status == ProbeStatus.UNIMPLEMENTED:
        assert route.reason


def test_probe_fetcher_builds_for_supported_news_adapters():
    rss = Source(name="rss", type="rss", url="https://example.com/feed.xml")
    api = Source(
        name="api",
        type="api",
        url="https://example.com/news.json",
        adapter="generic_json_list",
        stream="news",
    )

    assert build_probe_fetcher(rss) is not None
    assert build_probe_fetcher(api) is not None


def test_probe_fetcher_returns_none_for_unimplemented_adapter():
    source = Source(
        name="structured",
        type="api",
        url="https://example.com/cves.json",
        adapter="ubuntu_cve",
        stream="structured",
    )

    assert build_probe_fetcher(source) is None


def test_validate_sample_items_keeps_three_valid_unique_items():
    items = [
        RawItem(source_id=1, title="First", url="https://example.com/1", raw_content="body"),
        RawItem(source_id=1, title="", url="https://example.com/empty-title", raw_content="body"),
        RawItem(source_id=1, title="Duplicate", url="https://example.com/1", raw_content="body"),
        RawItem(source_id=1, title="No content", url="https://example.com/no-content"),
        RawItem(source_id=1, title="Second", url="https://example.com/2", raw_content="summary"),
        RawItem(source_id=1, title="Third", url="https://example.com/3", raw_content="text"),
        RawItem(source_id=1, title="Fourth", url="https://example.com/4", raw_content="text"),
    ]

    sample, errors = validate_sample_items(items)

    assert [item.url for item in sample] == [
        "https://example.com/1",
        "https://example.com/2",
        "https://example.com/3",
    ]
    assert errors == {
        "empty_title": 1,
        "duplicate_url": 1,
        "empty_content": 1,
    }
