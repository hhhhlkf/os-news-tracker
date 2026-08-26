import asyncio
import re
import html as html_std
import httpx
import feedparser
from datetime import timezone
from dateutil import parser as date_parser
from lxml import html as lxml_html
from urllib.parse import urljoin, urlparse


def clean_text(text):
    """Clean HTML/XML to plain text, decode entities, collapse whitespace, truncate to 2000 chars."""
    if not text:
        return ""
    try:
        doc = lxml_html.fromstring(f"<div>{text}</div>")
        clean = doc.text_content()
    except Exception:
        clean = re.sub(r'<[^>]+>', '', text)
        clean = html_std.unescape(clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean[:2000]


def parse_date(date_str):
    """Parse date string to UTC ISO 8601; return None if parsing fails."""
    if not date_str:
        return None
    try:
        dt = date_parser.parse(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def extract_main_content(html_text):
    """Extract readable text from article/main or common content containers."""
    try:
        doc = lxml_html.fromstring(html_text)
        selectors = [
            'article',
            'main',
            'div.article-content',
            'div.post-content',
            'div.entry-content',
            'div.content',
            'div#content',
            'div.post',
            'div.blog-post',
        ]
        for sel in selectors:
            elements = doc.cssselect(sel)
            if elements:
                raw = elements[0].text_content()
                return clean_text(raw)
        return None
    except Exception:
        return None


async def fetch_detail(client, sem, url):
    """Fetch a detail page and extract content; returns None on failure or no content."""
    async with sem:
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            content = extract_main_content(resp.text)
            return content if content else None
        except Exception:
            return None


async def crawl(request, context):
    if 'entry' not in request or not request['entry']:
        raise ValueError('request.entry is required')

    entry_url = request['entry']
    target_count = int(request.get('target_count') or 50)
    target_count = max(1, min(500, target_count))

    allowed_domains = context.get('allowed_domains', [])
    if not allowed_domains:
        allowed_domains = [urlparse(entry_url).netloc]

    # Fetch RSS feed (failure is fatal)
    async with httpx.AsyncClient(timeout=15.0) as rss_client:
        try:
            rss_resp = await rss_client.get(entry_url)
        except Exception as e:
            raise RuntimeError(f"fetch failed for {urlparse(entry_url).netloc}: {e}") from e
        if rss_resp.status_code != 200:
            raise RuntimeError(f"fetch failed for {urlparse(entry_url).netloc}: HTTP {rss_resp.status_code}")

        feed = feedparser.parse(rss_resp.text)
        if not feed.entries:
            raise RuntimeError(f"parse failed for {urlparse(entry_url).netloc}: no entries found")

        entries = feed.entries[:target_count]

    # Build initial items with cleaned summaries
    items = []
    for entry in entries:
        title = entry.get('title', '').strip()
        link = entry.get('link', '')
        if link:
            link = urljoin(entry_url, link)
        published_at = parse_date(entry.get('published') or entry.get('updated'))

        raw_summary = ''
        if entry.get('summary'):
            raw_summary = entry.get('summary')
        elif entry.get('description'):
            raw_summary = entry.get('description')
        elif entry.get('content'):
            content_list = entry.get('content', [])
            if content_list:
                raw_summary = content_list[0].get('value', '')
        summary = clean_text(raw_summary)

        items.append({
            'title': title,
            'url': link,
            'published_at': published_at,
            'summary': summary,
        })

    # Best-effort detail fetching
    detail_attempted_count = 0
    detail_success_count = 0
    detail_fallback_count = 0

    urls_to_fetch = []
    for item in items:
        if not item['url']:
            continue
        parsed = urlparse(item['url'])
        if parsed.scheme in ('http', 'https') and parsed.netloc in allowed_domains:
            urls_to_fetch.append(item['url'])

    if urls_to_fetch:
        detail_attempted_count = len(urls_to_fetch)
        sem = asyncio.Semaphore(5)
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as detail_client:
            tasks = [fetch_detail(detail_client, sem, url) for url in urls_to_fetch]
            results = await asyncio.gather(*tasks)
        success_contents = {}
        for url, content in zip(urls_to_fetch, results):
            if content:
                success_contents[url] = content
        detail_success_count = len(success_contents)
        detail_fallback_count = detail_attempted_count - detail_success_count
        for item in items:
            if item['url'] in success_contents:
                item['content'] = success_contents[item['url']]

    stats = {
        'discovered_count': len(items),
        'detail_attempted_count': detail_attempted_count,
        'detail_success_count': detail_success_count,
        'detail_fallback_count': detail_fallback_count,
    }
    return {'items': items, 'stats': stats}
