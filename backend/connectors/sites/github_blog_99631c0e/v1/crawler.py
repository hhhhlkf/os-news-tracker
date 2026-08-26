import re
import html
from datetime import timezone
from typing import Any, Dict, List

import httpx
import feedparser
from dateutil import parser as date_parser


def clean_text(raw: str) -> str:
    if not raw:
        return ''
    raw = re.sub('(?is)<script.*?</script>', '', raw)
    raw = re.sub('(?is)<style.*?</style>', '', raw)
    text = re.sub('<[^>]+>', '', raw)
    text = html.unescape(text)
    text = ' '.join(text.split())
    return text[:2000]


def parse_date(value: str) -> str:
    if not value:
        return ''
    try:
        dt = date_parser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, OverflowError):
        return value


async def crawl(request: dict, context: dict) -> dict:
    if not request.get('entry'):
        raise ValueError('request.entry is required')
    entry_url = request['entry']
    config = request.get('config', {}) or {}
    # page from config is not used because this feed is not paginated
    # page = config.get('page', 1)
    domain = 'github.blog'
    timeout = httpx.Timeout(15.0)
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; OSNewsTracker/1.0)'}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        try:
            resp = await client.get(entry_url)
        except httpx.HTTPError as e:
            raise Exception(f'fetch error for domain {domain}: {e}') from e
        if resp.status_code != 200:
            raise Exception(f'fetch error for domain {domain}: HTTP {resp.status_code}')
        parsed = feedparser.parse(resp.content)
        if not parsed.entries:
            raise Exception(f'parse error for domain {domain}: HTTP {resp.status_code}, no entries found')
        items = []
        for entry in parsed.entries:
            title = entry.get('title', '').strip()
            link = entry.get('link', '').strip()
            if not title or not link:
                continue
            published_raw = entry.get('published', entry.get('updated', ''))
            published_at = parse_date(published_raw)
            content_raw = ''
            if entry.get('content'):
                try:
                    content_raw = entry.content[0].value
                except (IndexError, AttributeError, TypeError):
                    content_raw = ''
            if not content_raw:
                content_raw = entry.get('summary', entry.get('description', ''))
            content = clean_text(content_raw)
            if not content:
                content = clean_text(entry.get('summary', entry.get('description', '')))
            items.append({
                'title': title,
                'url': link,
                'published_at': published_at,
                'content': content,
            })
        return {'items': items, 'stats': {'discovered_count': len(items)}}
