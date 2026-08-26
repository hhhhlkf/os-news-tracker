import asyncio
import html
import datetime
from dateutil import parser as date_parser
import httpx
from lxml import html as lxml_html
import feedparser

def clean_text(raw):
    if not raw:
        return ''
    try:
        doc = lxml_html.fromstring(raw)
        text = doc.text_content()
    except:
        text = raw
        while '<' in text and '>' in text:
            start = text.find('<')
            end = text.find('>', start)
            if end == -1:
                break
            text = text[:start] + ' ' + text[end+1:]
    text = html.unescape(text)
    text = ' '.join(text.split())
    return text[:2000]

def parse_published(value):
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc).isoformat()
    except:
        return None

def extract_detail_content(html_text):
    try:
        doc = lxml_html.fromstring(html_text)
    except:
        return ''
    candidates = doc.xpath('//article | //main')
    candidates.extend(doc.find_class('content'))
    candidates.extend(doc.find_class('article'))
    candidates.extend(doc.find_class('post-content'))
    if candidates:
        best = max(candidates, key=lambda el: len(el.text_content() or ''))
        return clean_text(best.text_content())
    body = doc.xpath('//body')
    if body:
        return clean_text(body[0].text_content())
    return clean_text(doc.text_content())

async def fetch_detail(client, url, sem, timeout=10.0):
    try:
        async with sem:
            resp = await client.get(url, timeout=timeout)
            if resp.status_code == 200:
                content_type = resp.headers.get('content-type', '')
                if 'text/html' in content_type or 'application/xhtml+xml' in content_type:
                    text = extract_detail_content(resp.text)
                    if text and len(text) > 50:
                        return text
            return None
    except Exception:
        return None

async def fetch_detail_and_update(client, item, sem):
    text = await fetch_detail(client, item['url'], sem)
    if text:
        item['content'] = text
        del item['summary']
        return True
    return False

async def crawl(request, context):
    entry = request.get('entry')
    if not entry:
        raise ValueError('request.entry is required')
    target_count = int(request.get('target_count') or 50)
    target_count = max(1, min(target_count, 500))

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        try:
            resp = await client.get(entry)
        except Exception as e:
            raise Exception(f'fetch failed for {entry}: {e}')
        if resp.status_code != 200:
            raise Exception(f'fetch failed for {entry} with HTTP {resp.status_code}')
        
        feed = feedparser.parse(resp.text)
        if not feed.entries:
            raise Exception(f'parse failed: no entries in feed {entry}')
        
        items = []
        for ent in feed.entries[:target_count]:
            title = ent.get('title', '').strip()
            link = ent.get('link', '').strip()
            if not title or not link:
                continue
            summary = ent.get('summary') or ent.get('description') or ''
            cleaned_summary = clean_text(summary)
            published = ent.get('published') or ent.get('updated')
            published_iso = parse_published(published)
            item = {
                'title': title,
                'url': link,
                'published_at': published_iso,
                'summary': cleaned_summary
            }
            items.append(item)
        
        if not items:
            raise Exception(f'parse failed: no valid items in feed {entry}')
        
        detail_attempted_count = len(items)
        sem = asyncio.Semaphore(5)
        results = await asyncio.gather(*(fetch_detail_and_update(client, item, sem) for item in items))
        detail_success_count = sum(1 for r in results if r)
        detail_fallback_count = len(results) - detail_success_count
        
        stats = {
            'discovered_count': len(items),
            'detail_attempted_count': detail_attempted_count,
            'detail_success_count': detail_success_count,
            'detail_fallback_count': detail_fallback_count
        }
        return {'items': items, 'stats': stats}
