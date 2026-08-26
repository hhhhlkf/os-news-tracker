import asyncio
from dateutil import parser as date_parser
from datetime import timezone, timedelta
import httpx
from lxml import etree, html as lxml_html

LIST_ENDPOINT = 'https://www.oschina.net/action/api/news_list'
DETAIL_ENDPOINT = 'https://www.oschina.net/action/api/news_detail'
PAGE_SIZE = 20
MAX_PAGES = 10
CHINA_TZ = timezone(timedelta(hours=8))

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Referer': 'https://www.oschina.net/',
    'Connection': 'keep-alive',
}

def clean_text(raw):
    if not raw:
        return ''
    try:
        doc = lxml_html.fromstring(raw)
        text = doc.text_content()
    except Exception:
        text = raw
    text = ' '.join(text.split())
    return text[:2000]

def parse_date(value):
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CHINA_TZ)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None

def parse_list_items(xml_text):
    root = etree.fromstring(xml_text.encode('utf-8'))
    items = []
    for news in root.findall('.//news'):
        id_node = news.find('id')
        item_id = id_node.text.strip() if id_node is not None and id_node.text else None
        title_node = news.find('title')
        title = title_node.text.strip() if title_node is not None and title_node.text else ''
        url_node = news.find('url')
        url = url_node.text.strip() if url_node is not None and url_node.text else ''
        if not url and item_id:
            url = 'https://www.oschina.net/news/' + item_id
        if not url:
            continue
        body_node = news.find('body')
        summary_raw = body_node.text.strip() if body_node is not None and body_node.text else ''
        pub_node = news.find('pubDate')
        pub_date_raw = pub_node.text.strip() if pub_node is not None and pub_node.text else ''
        items.append({
            'id': item_id,
            'title': title,
            'url': url,
            'published_at': parse_date(pub_date_raw),
            'summary': clean_text(summary_raw),
        })
    return items

async def fetch_detail(client, item):
    if not item.get('id'):
        return item, False
    detail_url = DETAIL_ENDPOINT + '?id=' + item['id']
    try:
        resp = await client.get(detail_url)
        if resp.status_code == 200:
            content_type = resp.headers.get('content-type', '')
            if 'xml' in content_type or 'text/xml' in content_type:
                root = etree.fromstring(resp.content)
                body_node = root.find('.//news/body')
                if body_node is not None and body_node.text:
                    content_clean = clean_text(body_node.text)
                    if content_clean:
                        item['content'] = content_clean
                        return item, True
    except Exception:
        pass
    return item, False

async def crawl(request, context):
    if not request.get('entry'):
        raise ValueError('request.entry is required')
    target_count = int(request.get('target_count') or 50)
    target_count = max(1, min(target_count, 500))
    config = request.get('config') or {}
    has_page = 'page' in config and config.get('page') is not None
    page_number = None
    if has_page:
        try:
            page_number = int(config.get('page')) - 1
            if page_number < 0:
                page_number = 0
        except (TypeError, ValueError):
            has_page = False

    items = []
    seen_urls = set()
    detail_attempted = 0
    detail_success = 0
    detail_fallback = 0

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
        if has_page:
            page_index = page_number
            list_url = LIST_ENDPOINT + '?pageIndex=' + str(page_index) + '&pageSize=' + str(PAGE_SIZE)
            resp = await client.get(list_url)
            if resp.status_code != 200:
                raise Exception(f"fetch list page failed: url={list_url}, status={resp.status_code}, domain=www.oschina.net")
            try:
                page_items = parse_list_items(resp.text)
            except Exception as e:
                raise Exception(f"parse list page failed: url={list_url}, domain=www.oschina.net, error={str(e)}")
            for it in page_items:
                if it['url'] not in seen_urls:
                    seen_urls.add(it['url'])
                    items.append(it)
        else:
            for page_index in range(MAX_PAGES):
                list_url = LIST_ENDPOINT + '?pageIndex=' + str(page_index) + '&pageSize=' + str(PAGE_SIZE)
                resp = await client.get(list_url)
                if resp.status_code != 200:
                    raise Exception(f"fetch list page failed: url={list_url}, status={resp.status_code}, domain=www.oschina.net")
                try:
                    page_items = parse_list_items(resp.text)
                except Exception as e:
                    raise Exception(f"parse list page failed: url={list_url}, domain=www.oschina.net, error={str(e)}")
                if not page_items:
                    break
                for it in page_items:
                    if it['url'] not in seen_urls:
                        seen_urls.add(it['url'])
                        items.append(it)
                if len(page_items) < PAGE_SIZE:
                    break
                if len(items) >= target_count:
                    break

        if items:
            sem = asyncio.Semaphore(5)
            async def fetch_with_sem(item):
                nonlocal detail_attempted, detail_success, detail_fallback
                if not item.get('id'):
                    return item
                async with sem:
                    detail_attempted += 1
                    updated_item, success = await fetch_detail(client, item)
                    if success:
                        detail_success += 1
                    else:
                        detail_fallback += 1
                    return updated_item
            items = await asyncio.gather(*[fetch_with_sem(it) for it in items])

        final_items = []
        for it in items:
            entry = {
                'title': it.get('title', ''),
                'url': it.get('url', ''),
                'published_at': it.get('published_at'),
                'summary': it.get('summary', ''),
            }
            if it.get('content'):
                entry['content'] = it['content']
            final_items.append(entry)

    stats = {
        'discovered_count': len(final_items),
        'detail_attempted_count': detail_attempted,
        'detail_success_count': detail_success,
        'detail_fallback_count': detail_fallback,
    }
    return {'items': final_items, 'stats': stats}
