from __future__ import annotations

from typing import Any

from app.fetchers.api_adapters import ApiAdapterFetcher
from app.models import Source


class FakeTextRequester:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        return self.responses[url]


def test_configurable_json_probe_supports_title_templates_and_content_fallbacks() -> None:
    requester = FakeTextRequester(
        {
            "https://ubuntu.com/security/notices.json": """
            {
              "notices": [
                {
                  "id": "USN-1234-1",
                  "title": "Linux kernel vulnerabilities",
                  "summary": "Several kernel issues were fixed.",
                  "description": "Details about CVE-2026-1111.",
                  "published": "2026-06-16T21:02:47.040684"
                }
              ]
            }
            """
        }
    )
    source = Source(
        id=10,
        name="Ubuntu Security Notices",
        type="api",
        url="https://ubuntu.com/security/notices.json",
        adapter="ubuntu_security",
        api_config={
            "probe": {
                "mode": "json_list",
                "items_path": "notices",
                "fields": {
                    "title_template": "{id}: {title}",
                    "url_template": "https://ubuntu.com/security/notices/{id}",
                    "content": ["summary", "description"],
                    "published_at": "published",
                },
            }
        },
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert items[0].source_id == 10
    assert items[0].title == "USN-1234-1: Linux kernel vulnerabilities"
    assert items[0].url == "https://ubuntu.com/security/notices/USN-1234-1"
    assert "Several kernel issues were fixed." in (items[0].raw_content or "")
    assert items[0].published_at is not None


def test_configurable_json_probe_adds_query_params() -> None:
    requester = FakeTextRequester(
        {
            "https://ubuntu.com/security/cves.json?limit=20": """
            {
              "cves": [
                {
                  "id": "CVE-2026-1111",
                  "priority": "high",
                  "status": "active",
                  "description": "A parser vulnerability.",
                  "published": "2026-06-09T00:00:00"
                }
              ]
            }
            """
        }
    )
    source = Source(
        id=11,
        name="Ubuntu CVE",
        type="api",
        url="https://ubuntu.com/security/cves.json",
        adapter="ubuntu_cve",
        api_config={
            "probe": {
                "mode": "json_list",
                "query": {"limit": 20},
                "items_path": "cves",
                "fields": {
                    "title_template": "{id}: {priority} {status}",
                    "url_template": "https://ubuntu.com/security/{id}",
                    "content": "description",
                    "published_at": "published",
                },
            }
        },
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert requester.calls == ["https://ubuntu.com/security/cves.json?limit=20"]
    assert items[0].title == "CVE-2026-1111: high active"
    assert items[0].url == "https://ubuntu.com/security/CVE-2026-1111"
    assert items[0].raw_content == "A parser vulnerability."


def test_configurable_html_table_probe_filters_sort_links() -> None:
    requester = FakeTextRequester(
        {
            "https://repo.openeuler.org/": """
            <table>
              <tr><td class="link"><a href="openEuler-24.03-LTS-SP3/" title="openEuler-24.03-LTS-SP3">openEuler-24.03-LTS-SP3/</a></td><td class="size">-</td><td class="date">2026-Mar-06 14:04</td></tr>
              <tr><td class="link"><a href="?C=N&amp;O=A">File Name</a></td><td class="date"></td></tr>
            </table>
            """
        }
    )
    source = Source(
        id=12,
        name="openEuler Repo Metadata",
        type="api",
        url="https://repo.openeuler.org/",
        adapter="openeuler_repo",
        api_config={
            "probe": {
                "mode": "html_table",
                "fields": {
                    "title_template": "openEuler repo {cell[0]}",
                    "url_from_link": 0,
                    "strip_cell_suffix": "/",
                    "exclude_href_contains": ["?"],
                    "content_template": "openEuler repository entry {cell[0]}; modified {cell[2]}",
                    "published_at_cell": 2,
                },
            }
        },
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert items[0].title == "openEuler repo openEuler-24.03-LTS-SP3"
    assert items[0].url == "https://repo.openeuler.org/openEuler-24.03-LTS-SP3/"
    assert "modified 2026-Mar-06 14:04" in (items[0].raw_content or "")
    assert items[0].published_at is not None


def test_configurable_html_table_probe_can_require_link_suffix() -> None:
    requester = FakeTextRequester(
        {
            "https://security-metadata.canonical.com/oval/": """
            <table>
              <tr>
                <td>CVE</td>
                <td>jammy</td>
                <td><a href="/oval/com.ubuntu.jammy.cve.oval.xml.bz2">com.ubuntu.jammy.cve.oval.xml.bz2</a></td>
                <td>2026-06-16 09:37:13</td>
                <td>1M</td>
              </tr>
            </table>
            """
        }
    )
    source = Source(
        id=13,
        name="Canonical Security Metadata",
        type="api",
        url="https://security-metadata.canonical.com/",
        adapter="canonical_security_meta",
        api_config={
            "probe": {
                "mode": "html_table",
                "url": "https://security-metadata.canonical.com/oval/",
                "fields": {
                    "title_template": "Ubuntu OVAL {cell[1]}",
                    "url_from_link": 2,
                    "include_href_suffix": ".bz2",
                    "content_template": "Ubuntu OVAL metadata file {cell[2]}; release {cell[1]}; modified {cell[3]}",
                    "published_at_cell": 3,
                },
            }
        },
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert items[0].title == "Ubuntu OVAL jammy"
    assert items[0].url == "https://security-metadata.canonical.com/oval/com.ubuntu.jammy.cve.oval.xml.bz2"
    assert "com.ubuntu.jammy.cve.oval.xml.bz2" in (items[0].raw_content or "")


def test_configurable_text_probe_maps_repository_readme() -> None:
    requester = FakeTextRequester(
        {
            "https://raw.githubusercontent.com/canonical/ubuntu-security-notices/main/README.md": """
            # Ubuntu Vulnerability Data

            This repository contains Ubuntu Vulnerability Data in 3 different JSON formats.
            OSV JSON format is one of the supported formats.
            """
        }
    )
    source = Source(
        id=14,
        name="Ubuntu OSV Security Notices",
        type="api",
        url="https://github.com/canonical/ubuntu-security-notices",
        adapter="ubuntu_osv",
        api_config={
            "probe": {
                "mode": "text",
                "url": "https://raw.githubusercontent.com/canonical/ubuntu-security-notices/main/README.md",
            }
        },
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert items[0].title == "Ubuntu Vulnerability Data"
    assert items[0].url == "https://github.com/canonical/ubuntu-security-notices"
    assert "OSV JSON format" in (items[0].raw_content or "")


def test_configurable_json_probe_maps_new_source_without_python_adapter() -> None:
    requester = FakeTextRequester(
        {
            "https://example.com/releases.json?limit=3": """
            {
              "items": [
                {
                  "id": "v1",
                  "name": "ExampleOS 1.0",
                  "body": "Release notes for ExampleOS.",
                  "date": "2026-06-17"
                }
              ]
            }
            """
        }
    )
    source = Source(
        id=15,
        name="Example Releases",
        type="api",
        url="https://example.com/releases.json",
        adapter="anything_new",
        api_config={
            "probe": {
                "mode": "json_list",
                "query": {"limit": 3},
                "items_path": "items",
                "fields": {
                    "title": "name",
                    "url_template": "https://example.com/releases/{id}",
                    "content": "body",
                    "published_at": "date",
                },
            }
        },
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert requester.calls == ["https://example.com/releases.json?limit=3"]
    assert items[0].title == "ExampleOS 1.0"
    assert items[0].url == "https://example.com/releases/v1"
    assert items[0].raw_content == "Release notes for ExampleOS."
    assert items[0].published_at is not None


def test_configurable_html_table_probe_maps_new_source_without_python_adapter() -> None:
    requester = FakeTextRequester(
        {
            "https://example.com/index/": """
            <table>
              <tr>
                <td>release</td>
                <td><a href="example-os-2/">ExampleOS 2</a></td>
                <td>2026-06-17 10:30</td>
              </tr>
            </table>
            """
        }
    )
    source = Source(
        id=16,
        name="Example HTML Index",
        type="api",
        url="https://example.com/index/",
        adapter="another_new_adapter",
        api_config={
            "probe": {
                "mode": "html_table",
                "fields": {
                    "title_template": "Example {cell[1]}",
                    "url_from_link": 1,
                    "content_template": "kind={cell[0]}; modified={cell[2]}",
                    "published_at_cell": 2,
                },
            }
        },
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert items[0].title == "Example ExampleOS 2"
    assert items[0].url == "https://example.com/index/example-os-2/"
    assert items[0].raw_content == "kind=release; modified=2026-06-17 10:30"


# ── 分页 probe：模拟 openanolis blogByCategoryPage 这类「page=1&pageSize=10」
#    被探测固化后，运行侧应能翻页抓取多页内容。 ──────────────────────────
class _PageResponseRequester:
    """按 URL 的 page query 参数返回不同页面的 requester。

    每页返回 2 条文章，第 1、2 页 hasMore=true，第 3 页 hasMore=false。
    记录所有被请求的 URL，供断言翻页行为。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        from urllib.parse import parse_qs, urlparse

        self.calls.append(url)
        page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        if page == 1:
            items = [
                {"title": "Post A", "no": "a", "summary": "body a"},
                {"title": "Post B", "no": "b", "summary": "body b"},
            ]
            has_more = True
        elif page == 2:
            items = [
                {"title": "Post C", "no": "c", "summary": "body c"},
                {"title": "Post D", "no": "d", "summary": "body d"},
            ]
            has_more = True
        else:
            items = [{"title": "Post E", "no": "e", "summary": "body e"}]
            has_more = False
        import json

        return json.dumps({"data": {"items": items, "hasMore": has_more}})


def test_configurable_json_probe_paginates_across_pages_via_has_more() -> None:
    """probe 带 pagination.has_more_path 时应循环翻页直到 hasMore=false。"""
    requester = _PageResponseRequester()
    source = Source(
        id=20,
        name="OpenAnolis Blog",
        type="api",
        url="https://openanolis.cn/api/blog/blogByCategoryPage.json",
        api_config={
            "probe": {
                "mode": "json_list",
                "method": "GET",
                "url": "https://openanolis.cn/api/blog/blogByCategoryPage.json?categoryNo=",
                "items_path": "data.items",
                "fields": {
                    "title": "title",
                    "url_template": "https://openanolis.cn/blog/{no}",
                    "content": "summary",
                },
                "pagination": {
                    "page_param": "page",
                    "size_param": "pageSize",
                    "size": 10,
                    "start_page": 1,
                    "max_pages": 5,
                    "has_more_path": "data.hasMore",
                },
            }
        },
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    # 3 页（2+2+1）= 5 条
    assert len(items) == 5
    assert [i.title for i in items] == ["Post A", "Post B", "Post C", "Post D", "Post E"]
    # 应当在 hasMore=false 的第 3 页后停止，而不是抓满 max_pages=5
    assert len(requester.calls) == 3
    # 每一页都注入了 page 和 pageSize 参数
    assert "page=1" in requester.calls[0] and "pageSize=10" in requester.calls[0]
    assert "page=2" in requester.calls[1] and "pageSize=10" in requester.calls[1]
    assert "page=3" in requester.calls[2] and "pageSize=10" in requester.calls[2]


class _TotalResponseRequester:
    """按 page 参数返回页面；用 data.total 控制终止（total=7, size=2 → 4 页）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        from urllib.parse import parse_qs, urlparse
        import json

        self.calls.append(url)
        page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        # 第 4 页只有 1 条（7 = 2+2+2+1）
        if page <= 3:
            items = [
                {"title": f"Post {page}-1", "id": f"{page}-1"},
                {"title": f"Post {page}-2", "id": f"{page}-2"},
            ]
        else:
            items = [{"title": f"Post {page}-1", "id": f"{page}-1"}]
        return json.dumps({"data": {"records": items, "total": 7}})


def test_configurable_json_probe_paginates_via_total_path() -> None:
    """没有 has_more_path、只有 total_path + size 时，应按 total/size 推算终止。"""
    requester = _TotalResponseRequester()
    source = Source(
        id=21,
        name="Paged via total",
        type="api",
        url="https://example.com/api/articles",
        api_config={
            "probe": {
                "mode": "json_list",
                "method": "GET",
                "url": "https://example.com/api/articles",
                "items_path": "data.records",
                "fields": {
                    "title": "title",
                    "url_template": "https://example.com/a/{id}",
                },
                "pagination": {
                    "page_param": "page",
                    "size_param": "size",
                    "size": 2,
                    "start_page": 1,
                    "max_pages": 10,
                    "total_path": "data.total",
                },
            }
        },
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    # total=7, size=2 → 第 4 页后 (4*2=8 >= 7) 停止，共 7 条
    assert len(items) == 7
    assert len(requester.calls) == 4
    assert "size=2" in requester.calls[0]


def test_configurable_json_probe_pagination_respects_max_pages() -> None:
    """max_pages 应作为硬上限，即使 hasMore 一直为 true 也要停下。"""

    class _AlwaysMoreRequester:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, url: str) -> str:
            from urllib.parse import parse_qs, urlparse
            import json

            self.calls.append(url)
            page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
            return json.dumps(
                {"data": {"items": [{"title": f"P{page}", "no": str(page)}], "hasMore": True}}
            )

    requester = _AlwaysMoreRequester()
    source = Source(
        id=22,
        name="Always more",
        type="api",
        url="https://example.com/api/inf",
        api_config={
            "probe": {
                "mode": "json_list",
                "method": "GET",
                "url": "https://example.com/api/inf",
                "items_path": "data.items",
                "fields": {
                    "title": "title",
                    "url_template": "https://example.com/inf/{no}",
                },
                "pagination": {
                    "page_param": "page",
                    "start_page": 1,
                    "max_pages": 3,
                    "has_more_path": "data.hasMore",
                },
            }
        },
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 3
    assert len(requester.calls) == 3


def test_configurable_json_probe_without_pagination_stays_single_request() -> None:
    """无 pagination 字段时保持原有单请求行为，不引入翻页。"""
    requester = FakeTextRequester(
        {
            "https://example.com/one.json": """
            { "items": [ { "title": "Only", "url": "https://example.com/only" } ] }
            """
        }
    )
    source = Source(
        id=23,
        name="No pagination",
        type="api",
        url="https://example.com/one.json",
        api_config={
            "probe": {
                "mode": "json_list",
                "items_path": "items",
                "fields": {"title": "title", "url": "url"},
            }
        },
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert requester.calls == ["https://example.com/one.json"]
