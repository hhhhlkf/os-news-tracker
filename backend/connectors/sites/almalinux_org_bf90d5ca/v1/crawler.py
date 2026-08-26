import asyncio
import re
import html as html_lib
from datetime import timezone
from urllib.parse import urlparse, urljoin
import httpx
import feedparser
from dateutil import parser as date_parser
from lxml import html as lxml_html

USER_AGENT = 'Mozilla/5.0 (compatible; OSNewsTracker/1.0)'

def clean_text(raw):
    if not raw:
        return ''
    raw = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', raw, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', raw)
    text = html_lib.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:2000]

def parse_date(value):
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()
    except Exception:
        return None

def extract_detail_text(html_text):
    if not html_text:
        return ''
    try:
        tree = lxml_html.fromstring(html_text)
        for bad in tree.xpath('//script|//style|//nav|//header|//footer|//aside|//form|//noscript'):
            bad.getparent().remove(bad)
        container = None
        for sel in ['//article', '//main']:
            nodes = tree.xpath(sel)
            if nodes:
                container = nodes[0]
                break
        if container is None:
            body = tree.xpath('//body')
            container = body[0] if body else tree
        raw_text = container.text_content()
        return clean_text(raw_text)
    except Exception:
        return ''

async def fetch_detail(client, url, sem):
    try:
        async with sem:
            resp = await client.get(url, follow_redirects=True, timeout=10.0)
            if resp.status_code == 200:
                ctype = resp.headers.get('content-type', '')
                if 'text/html' in ctype.lower():
                    text = extract_detail_text(resp.text)
                    if len(text) >= 200:
                        return text
            return None
    except Exception:
        return None

async def crawl(request, context):
    entry = request.get('entry')
    if not entry:
        raise ValueError('request.entry is required')
    target_count = int(request.get('target_count') or 50)
    if target_count < 1:
        target_count = 1
    elif target_count > 500:
        target_count = 500

    allowed_domains = set(context.get('allowed_domains', []))

    async with httpx.AsyncClient(timeout=15.0, headers={'User-Agent': USER_AGENT}, follow_redirects=True) as client:
        try:
            resp = await client.get(entry)
            if resp.status_code != 200:
                raise RuntimeError(f'fetch failed: {entry} status={resp.status_code}')
            feed = feedparser.parse(resp.text)
        except Exception as e:
            raise RuntimeError(f'fetch failed: {entry} error={e}') from e

        entries = []
        for e in feed.entries:
            title = e.get('title') or ''
            link = e.get('link') or ''
            published = e.get('published') or e.get('updated') or None
            summary_raw = e.get('summary') or e.get('description') or ''
            summary = clean_text(summary_raw)
            pub_at = parse_date(published)
            if not title or not link:
                continue
            if not urlparse(link).scheme:
                link = urljoin(entry, link)
            entries.append({
                'title': title,
                'url': link,
                'published_at': pub_at,
                'summary': summary,
                'content': None,
            })
            if len(entries) >= target_count:
                break

        detail_attempted = 0
        detail_success = 0
        detail_fallback = 0
        sem = asyncio.Semaphore(3)

        async def process_item(item):
            nonlocal detail_attempted, detail_success, detail_fallback
            url = item['url']
            domain = urlparse(url).netloc.lower()
            if domain not in allowed_domains:
                return item
            detail_attempted += 1
            text = await fetch_detail(client, url, sem)
            if text:
                item['content'] = text
                detail_success += 1
            else:
                detail_fallback += 1
            return item

        items = await asyncio.gather(*(process_item(item) for item in entries))

        final_items = []
        for item in items:
            final_items.append({
                'title': item['title'],
                'url': item['url'],
                'published_at': item['published_at'],
                'summary': item['summary'],
                'content': item['content'],
            })

        stats = {
            'discovered_count': len(final_items),
            'detail_attempted_count': detail_attempted,
            'detail_success_count': detail_success,
            'detail_fallback_count': detail_fallback,
        }

        return {'items': final_items, 'stats': stats}