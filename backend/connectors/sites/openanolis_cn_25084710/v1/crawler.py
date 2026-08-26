import json
import re
import html as html_lib
from datetime import timezone
import httpx
from lxml import html as lxml_html
from dateutil import parser as date_parser

def clean_html(raw_html, max_chars=2000):
    if not raw_html:
        return ""
    try:
        doc = lxml_html.fromstring(raw_html)
        text = doc.text_content()
    except Exception:
        text = re.sub(r'<[^>]+>', ' ', raw_html)
    text = html_lib.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > max_chars:
        text = text[:max_chars]
    return text

def parse_date(value):
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None

async def crawl(request, context):
    if 'entry' not in request or not request.get('entry'):
        raise ValueError('request.entry is required')
    entry = request['entry']
    config = request.get('config', {}) or {}
    target_count = int(request.get('target_count') or 50)
    target_count = max(1, min(target_count, 500))
    explicit_page = None
    if 'page' in config and config['page'] is not None:
        try:
            explicit_page = int(config['page'])
        except (TypeError, ValueError):
            pass
    base_url = 'https://openanolis.cn/api/blog/blogByCategoryPage.json'
    page_size = 10
    items = []
    seen_ids = set()
    page = explicit_page if explicit_page is not None else 1
    max_pages = 10
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            if len(items) >= target_count:
                break
            if explicit_page is None and page > max_pages:
                break
            url = f'{base_url}?categoryNo=&page={page}&pageSize={page_size}'
            try:
                resp = await client.get(url)
            except httpx.HTTPError as e:
                raise RuntimeError(f'fetch failed for {url}: {e}') from e
            if resp.status_code != 200:
                raise RuntimeError(f'fetch failed for {url}: HTTP status {resp.status_code}')
            try:
                data = resp.json()
            except ValueError as e:
                raise RuntimeError(f'parse failed for {url}: invalid JSON') from e
            payload = data.get('data', {})
            page_items = payload.get('items', [])
            if not page_items:
                break
            for item in page_items:
                no = item.get('no')
                if not no or no in seen_ids:
                    continue
                seen_ids.add(no)
                title = item.get('title') or ''
                url_article = f'https://openanolis.cn/blog#{no}'
                published_at = parse_date(item.get('publishTime') or item.get('gmtCreate'))
                summary = clean_html(item.get('summary') or '')
                content = clean_html(item.get('content') or '')
                if not content and summary:
                    content = summary
                    summary = ''
                if not content and not summary:
                    continue
                entry = {
                    'title': title,
                    'url': url_article,
                    'published_at': published_at,
                    'content': content,
                }
                if summary:
                    entry['summary'] = summary
                items.append(entry)
                if len(items) >= target_count:
                    break
            has_more = bool(payload.get('hasMore'))
            if explicit_page is not None:
                break
            if not has_more:
                break
            page += 1
    if len(items) > target_count:
        items = items[:target_count]
    return {
        'items': items,
        'stats': {
            'discovered_count': len(items)
        }
    }
