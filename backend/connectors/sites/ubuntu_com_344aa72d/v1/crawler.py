import asyncio
import re
import html
from datetime import timezone
from urllib.parse import urlparse

import httpx
import feedparser
from dateutil import parser as date_parser
from lxml import html as lxml_html


def clean_text(raw: str) -> str:
    """Remove HTML tags and scripts, decode entities, collapse whitespace, and truncate."""
    if not raw:
        return ""
    # Remove script and style blocks
    raw = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', raw, flags=re.DOTALL | re.IGNORECASE)
    # Remove remaining HTML tags
    text = re.sub(r'<[^>]+>', ' ', raw)
    # Decode HTML entities
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:2000]


def parse_date(value: str):
    """Parse date to UTC ISO 8601 string, or return None on failure."""
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def extract_content(html_text: str):
    """Extract readable main content from a detail page."""
    try:
        tree = lxml_html.fromstring(html_text)
    except Exception:
        return None
    selectors = ['article', 'main', '[class*="content"]', '[class*="post"]', '[class*="article"]']
    for sel in selectors:
        elems = tree.cssselect(sel)
        if elems:
            text = elems[0].text_content()
            cleaned = clean_text(text)
            if len(cleaned) > 200:
                return cleaned
    body = tree.find('body')
    if body is not None:
        text = body.text_content()
        cleaned = clean_text(text)
        if len(cleaned) > 200:
            return cleaned
    return None


async def crawl(request, context):
    entry = request.get('entry')
    if not entry:
        raise ValueError('request.entry is required')
    target_count = int(request.get('target_count') or 50)
    target_count = max(1, min(target_count, 500))
    config = request.get('config', {})
    # No pagination for this RSS feed; config page is ignored
    allowed_domains = context.get('allowed_domains', [])

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        try:
            resp = await client.get(entry, headers={'User-Agent': 'Mozilla/5.0 (compatible; OSNewsCrawler/1.0)'})
        except Exception as e:
            raise Exception(f"fetch failed for {entry}: {e}") from e
        if resp.status_code != 200:
            raise Exception(f"fetch failed for {entry}: HTTP {resp.status_code}")

        feed = feedparser.parse(resp.text)
        if not feed.entries:
            raise Exception(f"parse failed for {entry}: no entries found")

        entries = []
        seen_urls = set()
        for e in feed.entries:
            link = e.get('link')
            if not link:
                continue
            if link in seen_urls:
                continue
            seen_urls.add(link)
            title = e.get('title', '').strip()
            summary_raw = e.get('summary', '') or e.get('description', '')
            summary = clean_text(summary_raw)
            published = parse_date(e.get('published', '') or e.get('updated', ''))
            entries.append({
                'title': title,
                'url': link,
                'published_at': published,
                'summary': summary,
            })
            if len(entries) >= target_count:
                break

        semaphore = asyncio.Semaphore(5)
        detail_attempted = 0
        detail_success = 0
        detail_fallback = 0

        async def process_entry(item):
            nonlocal detail_attempted, detail_success, detail_fallback
            url = item['url']
            domain = urlparse(url).netloc
            if allowed_domains and domain not in allowed_domains:
                return item
            if not url.startswith(('http://', 'https://')):
                return item
            detail_attempted += 1
            async with semaphore:
                try:
                    detail_resp = await client.get(url, timeout=5.0, headers={'User-Agent': 'Mozilla/5.0 (compatible; OSNewsCrawler/1.0)'})
                    if detail_resp.status_code != 200:
                        raise Exception(f"status {detail_resp.status_code}")
                    content = extract_content(detail_resp.text)
                    if content:
                        detail_success += 1
                        item['content'] = content
                        return item
                except Exception:
                    pass
            detail_fallback += 1
            return item

        items = await asyncio.gather(*[process_entry(item) for item in entries])

        stats = {
            'discovered_count': len(items),
            'detail_attempted_count': detail_attempted,
            'detail_success_count': detail_success,
            'detail_fallback_count': detail_fallback,
        }
        return {'items': items, 'stats': stats}
