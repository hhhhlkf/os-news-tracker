"""Inspect the public Google DeepMind News listing and prove its pagination."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any
from urllib.parse import urljoin

import httpx
from lxml import html


ENTRY_URL = "https://deepmind.google/blog/"
USER_AGENT = "Mozilla/5.0 (compatible; OSNewsTracker/1.0)"


def fetch(client: httpx.Client, url: str, attempts: int = 3) -> str:
    """Fetch one public page with bounded retries for transient TLS/proxy errors."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.get(url)
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    raise RuntimeError(f"fetch failed for {url}: {last_error}") from last_error


def extract_cards(document: str, page_url: str) -> list[dict[str, str]]:
    """Extract visible News cards from server-rendered HTML."""
    root = html.fromstring(document)
    items: list[dict[str, str]] = []
    for card in root.xpath("//article[contains(@class, 'card-blog')]"):
        href = card.xpath("string(.//a[contains(@class, 'card__overlay-link')]/@href)").strip()
        title = " ".join(card.xpath("string(.//h3[contains(@class, 'card__title')])").split())
        published = card.xpath("string(.//time/@datetime)").strip()
        if not href or not title or not published:
            continue
        items.append({
            "title": title,
            "url": urljoin(page_url, href),
            "published_text": published,
        })
    return items


def page_urls(root_url: str, pages: int) -> list[str]:
    return [root_url] + [urljoin(root_url, f"page/{number}/") for number in range(2, pages + 1)]


def inspect(pages: int) -> dict[str, Any]:
    """Fetch adjacent listing pages and prove that page 2 contains new article URLs."""
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=10.0),
        http2=False,
    ) as client:
        results: list[dict[str, Any]] = []
        all_urls: list[set[str]] = []
        for url in page_urls(ENTRY_URL, pages):
            items = extract_cards(fetch(client, url), url)
            all_urls.append({item["url"] for item in items})
            results.append({"page_url": url, "item_count": len(items), "items": items})

    first = all_urls[0] if all_urls else set()
    later = set().union(*all_urls[1:]) if len(all_urls) > 1 else set()
    return {
        "entry_url": ENTRY_URL,
        "pagination": {
            "mechanism": "server-rendered path pages",
            "checked_pages": pages,
            "new_item_count_after_page_1": len(later - first),
            "supports_pagination": bool(later - first),
        },
        "pages": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.pages <= 10:
        raise SystemExit("--pages must be between 1 and 10")
    print(json.dumps(inspect(args.pages), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
