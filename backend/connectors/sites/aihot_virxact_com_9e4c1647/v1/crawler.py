import re
import html
from urllib.parse import urlparse
import dateutil.parser
import feedparser
import httpx

async def crawl(request, context):
    entry = request.get('entry')
    if not entry:
        raise ValueError('request.entry is required')

    target_count = int(request.get('target_count') or 50)
    target_count = max(1, min(500, target_count))

    # RSS feed has no pagination, config.page ignored
    # request.get('config', {}).get('page', 1) not used

    parsed_url = urlparse(entry)
    domain = parsed_url.netloc

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        try:
            response = await client.get(entry)
        except httpx.HTTPError as e:
            raise Exception(f"fetch failed for {domain}: {e}") from e

    if response.status_code != 200:
        raise Exception(f"fetch failed for {domain}: HTTP {response.status_code}")

    feed = feedparser.parse(response.text)
    if feed.bozo and not feed.entries:
        raise Exception(f"parse failed for {domain}: {feed.bozo_exception}")

    items = []
    seen_urls = set()

    def clean_text(raw):
        if not raw:
            return ''
        raw = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r'<[^>]+>', ' ', raw)
        raw = html.unescape(raw)
        raw = raw.replace('\xa0', ' ')
        raw = re.sub(r'\s+', ' ', raw).strip()
        return raw

    def get_entry_link(entry):
        link = entry.get('link')
        if isinstance(link, str) and link:
            return link
        if isinstance(link, dict) and link.get('href'):
            return link['href']
        if isinstance(link, list):
            for item_link in link:
                if isinstance(item_link, dict) and item_link.get('href'):
                    return item_link['href']
        guid = entry.get('id')
        if isinstance(guid, str) and guid.startswith('http'):
            return guid
        return ''

    for entry in feed.entries:
        if len(items) >= target_count:
            break

        link = get_entry_link(entry)
        if not link:
            continue
        if link in seen_urls:
            continue
        seen_urls.add(link)

        title = (entry.get('title') or '').strip()

        published_raw = entry.get('published') or entry.get('updated') or entry.get('created') or entry.get('issued')
        published_at = None
        if published_raw:
            try:
                published_at = dateutil.parser.parse(str(published_raw)).isoformat()
            except (ValueError, OverflowError):
                published_at = None

        # Prefer content, fallback summary
        raw_content = ''
        if entry.get('content'):
            for content_part in entry['content']:
                raw_content += content_part.get('value', '')
        raw_summary = entry.get('summary') or entry.get('description') or ''
        raw_text = raw_content if raw_content else raw_summary
        cleaned_text = clean_text(raw_text)
        if not cleaned_text:
            continue

        if raw_content:
            items.append({
                'title': title,
                'url': link,
                'published_at': published_at,
                'content': cleaned_text[:2000],
            })
        else:
            items.append({
                'title': title,
                'url': link,
                'published_at': published_at,
                'summary': cleaned_text[:2000],
            })

    return {
        'items': items,
        'stats': {'discovered_count': len(items)},
    }
