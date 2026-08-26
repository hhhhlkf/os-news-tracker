import asyncio
import re
import html as html_lib
from datetime import timezone
from dateutil import parser as date_parser
import feedparser
import httpx
from lxml import html as lxml_html

def clean_html(raw_html):
    if not raw_html:
        return ''
    try:
        doc = lxml_html.fromstring(raw_html)
        text = doc.text_content()
    except Exception:
        text = re.sub(r'<[^>]+>', '', raw_html)
    text = html_lib.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:2000]

def extract_article_text(html_content):
    try:
        doc = lxml_html.fromstring(html_content)
        for selector in ['article', 'main', '[role="main"]', '.post-content', '.article-content', '#content']:
            elements = doc.cssselect(selector)
            if elements:
                text = elements[0].text_content()
                if len(text.strip()) > 100:
                    return clean_html(text)
        for bad in doc.cssselect('script, style, nav, header, footer, aside'):
            bad.drop_tree()
        body = doc.cssselect('body')
        if body:
            text = body[0].text_content()
            return clean_html(text)
        return clean_html(doc.text_content())
    except Exception:
        return None

async def fetch_detail(url, client, timeout=5.0):
    try:
        resp = await client.get(url, timeout=timeout)
        if resp.status_code == 200:
            content_type = resp.headers.get('content-type', '')
            if 'html' in content_type.lower():
                return extract_article_text(resp.text)
    except Exception:
        pass
    return None

async def crawl(request, context):
    if 'entry' not in request or not request['entry']:
        raise ValueError('request.entry is required')
    entry_url = request['entry']
    target_count = int(request.get('target_count') or 50)
    target_count = max(1, min(500, target_count))
    allowed_domains = context.get('allowed_domains', [])
    # RSS has no pagination, ignore config page

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            resp = await client.get(entry_url, timeout=10.0)
            if resp.status_code != 200:
                raise Exception(f'fetch failed for {entry_url}: HTTP {resp.status_code}')
            feed = feedparser.parse(resp.text)
        except Exception as e:
            raise Exception(f'fetch failed for {entry_url}: {str(e)}') from e

        if feed.bozo and not feed.entries:
            raise Exception(f'parse failed for {entry_url}: {feed.bozo_exception}')

        entries = feed.entries[:target_count]

        items = []
        detail_attempted = 0
        detail_success = 0

        for entry in entries:
            title = getattr(entry, 'title', '').strip()
            url = getattr(entry, 'link', '').strip()
            if not url:
                links = getattr(entry, 'links', [])
                if links:
                    url = links[0].get('href', '')
            published_raw = getattr(entry, 'published', None) or getattr(entry, 'updated', None)
            published_at = None
            if published_raw:
                try:
                    dt = date_parser.parse(published_raw)
                    published_at = dt.astimezone(timezone.utc).isoformat()
                except Exception:
                    published_at = None

            raw_summary = getattr(entry, 'description', '') or getattr(entry, 'summary', '') or ''
            summary = clean_html(raw_summary)
            if not summary:
                summary = title

            item = {
                'title': title,
                'url': url,
                'published_at': published_at,
                'summary': summary,
                'content': None
            }

            if url and any(domain in url for domain in allowed_domains):
                detail_attempted += 1
                content_text = await fetch_detail(url, client)
                if content_text:
                    item['content'] = content_text
                    detail_success += 1

            items.append(item)

        stats = {
            'discovered_count': len(items),
            'detail_attempted_count': detail_attempted,
            'detail_success_count': detail_success,
            'detail_fallback_count': len(items) - detail_success,
        }
        return {'items': items, 'stats': stats}