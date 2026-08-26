import asyncio
import re
import html
from datetime import timezone
from urllib.parse import urlparse
import feedparser
import httpx
from lxml import html as lxml_html
from dateutil import parser as date_parser

MAX_CONTENT_LENGTH = 2000
DEFAULT_TARGET = 50
MAX_TARGET = 500
MIN_TARGET = 1


def clean_text(raw: str) -> str:
    if not raw:
        return ''
    try:
        doc = lxml_html.fromstring(raw)
        text = doc.text_content()
    except Exception:
        text = re.sub(r'<[^>]+>', ' ', raw)
        text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > MAX_CONTENT_LENGTH:
        text = text[:MAX_CONTENT_LENGTH]
    return text


async def fetch_detail(client, url, semaphore):
    async with semaphore:
        try:
            response = await client.get(url, timeout=10.0)
            if response.status_code != 200:
                return None
            content_type = response.headers.get('content-type', '')
            if 'html' not in content_type.lower():
                return None
            tree = lxml_html.fromstring(response.text)
            selectors = [
                'article', 'main', 'div.article-content', 'div.body', 'div.content',
                'div#content', 'div.post-content', 'div.entry-content', 'div.blog-post-content'
            ]
            content_node = None
            for selector in selectors:
                nodes = tree.cssselect(selector)
                if nodes:
                    content_node = nodes[0]
                    break
            if content_node is None:
                content_node = tree.body
            if content_node is None:
                return None
            text = content_node.text_content()
            text = clean_text(text)
            if len(text) >= 200:
                return text
            return None
        except Exception:
            return None


async def crawl(request, context):
    if 'entry' not in request or not request['entry']:
        raise ValueError('request.entry is required')

    entry_url = request['entry']
    target_count = int(request.get('target_count') or DEFAULT_TARGET)
    target_count = max(MIN_TARGET, min(MAX_TARGET, target_count))

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(entry_url)
            if response.status_code != 200:
                raise Exception(f'fetch error: HTTP {response.status_code} for {entry_url}')
            feed_text = response.text
            feed = feedparser.parse(feed_text)
            if not feed.entries:
                raise Exception('parse error: no entries found in feed')
    except Exception as e:
        raise Exception(f'fetch/parse error for {urlparse(entry_url).netloc}: {e}')

    items = []
    seen_urls = set()
    for entry in feed.entries:
        url = entry.get('link')
        title = entry.get('title')
        if not url or not title:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        published_raw = entry.get('published') or entry.get('updated')
        published_at = None
        if published_raw:
            try:
                dt = date_parser.parse(published_raw)
                published_at = dt.astimezone(timezone.utc).isoformat()
            except Exception:
                published_at = None
        summary_raw = entry.get('summary') or entry.get('description') or ''
        summary_clean = clean_text(summary_raw)
        items.append({
            'title': title,
            'url': url,
            'published_at': published_at,
            'summary': summary_clean,
            'content': None
        })
        if len(items) >= target_count:
            break

    detail_attempted_count = 0
    detail_success_count = 0
    detail_fallback_count = 0
    allowed_domains = context.get('allowed_domains', [])

    semaphore = asyncio.Semaphore(5)
    allowed_indices = []
    tasks = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        for i, item in enumerate(items):
            item_domain = urlparse(item['url']).netloc
            if item_domain in allowed_domains:
                allowed_indices.append(i)
                detail_attempted_count += 1
                tasks.append(fetch_detail(client, item['url'], semaphore))
            else:
                item['content'] = item['summary']
                detail_fallback_count += 1

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for idx, result in zip(allowed_indices, results):
                if isinstance(result, Exception):
                    items[idx]['content'] = items[idx]['summary']
                    detail_fallback_count += 1
                elif result:
                    items[idx]['content'] = result
                    detail_success_count += 1
                else:
                    items[idx]['content'] = items[idx]['summary']
                    detail_fallback_count += 1

    final_items = []
    for item in items:
        if not item.get('content'):
            item['content'] = item.get('summary', '')
        final_items.append({
            'title': item['title'],
            'url': item['url'],
            'published_at': item['published_at'],
            'content': item['content']
        })

    stats = {
        'discovered_count': len(final_items),
        'detail_attempted_count': detail_attempted_count,
        'detail_success_count': detail_success_count,
        'detail_fallback_count': detail_fallback_count
    }

    return {'items': final_items, 'stats': stats}
