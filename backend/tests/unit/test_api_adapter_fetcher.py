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


def test_ubuntu_security_adapter_maps_notices_to_raw_items() -> None:
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
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert items[0].source_id == 10
    assert items[0].title == "USN-1234-1: Linux kernel vulnerabilities"
    assert items[0].url == "https://ubuntu.com/security/notices/USN-1234-1"
    assert "Several kernel issues were fixed." in (items[0].raw_content or "")
    assert items[0].published_at is not None


def test_ubuntu_cve_adapter_maps_cves_to_raw_items() -> None:
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
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert requester.calls == ["https://ubuntu.com/security/cves.json?limit=20"]
    assert items[0].title == "CVE-2026-1111: high active"
    assert items[0].url == "https://ubuntu.com/security/CVE-2026-1111"
    assert items[0].raw_content == "A parser vulnerability."


def test_openeuler_repo_adapter_maps_html_index_rows() -> None:
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
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert items[0].title == "openEuler repo openEuler-24.03-LTS-SP3"
    assert items[0].url == "https://repo.openeuler.org/openEuler-24.03-LTS-SP3/"
    assert "modified 2026-Mar-06 14:04" in (items[0].raw_content or "")
    assert items[0].published_at is not None


def test_canonical_security_meta_adapter_maps_oval_index_rows() -> None:
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
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert items[0].title == "Ubuntu OVAL jammy"
    assert items[0].url == "https://security-metadata.canonical.com/oval/com.ubuntu.jammy.cve.oval.xml.bz2"
    assert "com.ubuntu.jammy.cve.oval.xml.bz2" in (items[0].raw_content or "")


def test_ubuntu_osv_adapter_maps_repository_readme() -> None:
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
    )

    items = ApiAdapterFetcher(requester=requester).fetch(source)

    assert len(items) == 1
    assert items[0].title == "Ubuntu Vulnerability Data"
    assert items[0].url == "https://github.com/canonical/ubuntu-security-notices"
    assert "OSV JSON format" in (items[0].raw_content or "")
