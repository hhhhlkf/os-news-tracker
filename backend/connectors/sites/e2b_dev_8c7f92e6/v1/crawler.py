import asyncio
import html as html_lib
import re
from datetime import timezone
from urllib.parse import urljoin, urlparse

import httpx
import lxml.html
from dateutil import parser as date_parser


def clean_text(text_or_element, max_chars=2000):
    if text_or_element is None:
        return ''
    if hasattr(text_or_element, 'text_content'):
        for bad in text_or_element.xpath('.//script | .//style'):
            bad.getparent().remove(bad)
        text = text_or_element.text_content()
    else:
        text = str(text_or_element)
    text = html_lib.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def parse_date_utc(date_str):
    if not date_str:
        return None
    m = re.search(r'([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})', date_str)
    if not m:
        try:
            dt = date_parser.parse(date_str)
        except Exception:
            return None
    else:
        date_part = m.group(1)
        try:
            dt = date_parser.parse(date_part)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def extract_all_items(html_text, base_url):
    doc = lxml.html.fromstring(html_text)
    item_nodes = doc.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' blog-collection-item ')]")
    items = []
    for node in item_nodes:
        title_a = node.xpath(".//a[contains(concat(' ', normalize-space(@class), ' '), ' h3-blog-item ')]")
        if not title_a:
            continue
        title_el = title_a[0]
        title = clean_text(title_el.text_content()) if title_el.text_content() else ''
        href = title_el.get('href')
        if not href:
            continue
        url = urljoin(base_url, href)
        pub_el = node.xpath(".//*[contains(concat(' ', normalize-space(@class), ' '), ' blog-featured-date ')]")
        pub_date_text = pub_el[0].text_content() if pub_el else ''
        published_at = parse_date_utc(pub_date_text)
        cat_el = node.xpath(".//div[contains(concat(' ', normalize-space(@class), ' '), ' tag primary ')]//div[contains(concat(' ', normalize-space(@class), ' '), ' label tertiary ')]")
        category = clean_text(cat_el[0].text_content()) if cat_el else ''
        items.append({
            'title': title,
            'url': url,
            'published_at': published_at,
            'category': category,
            'summary': None,
            'content': None,
        })
    return items


async def fetch_detail(client, item, sem):
    url = item['url']
    async with sem:
        try:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code != 200:
                raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
            doc = lxml.html.fromstring(resp.text)
            meta_desc = doc.xpath("//meta[@name='description']/@content")
            summary = meta_desc[0].strip() if meta_desc else ''
            content_el = doc.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' rtb-blog-detail ') and contains(concat(' ', normalize-space(@class), ' '), ' w-richtext ')]")
            if not content_el:
                content_el = doc.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' blog-detail-content ')]")
            content = ''
            if content_el:
                content = clean_text(content_el[0])
            detail_pub_el = doc.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' blog-featured-date-vertical ')]//div[contains(concat(' ', normalize-space(@class), ' '), ' label ')]")
            if detail_pub_el:
                detail_date_text = detail_pub_el[0].text_content()
                detail_pub = parse_date_utc(detail_date_text)
                if detail_pub:
                    item['published_at'] = detail_pub
            if not summary and content:
                summary = content[:200]
            item['summary'] = clean_text(summary) if summary else None
            item['content'] = content if content else None
            if not item.get('summary') and not item.get('content'):
                item['summary'] = item['title']
            return True, 'success'
        except Exception:
            item['summary'] = item['title']
            item['content'] = None
            return False, 'fallback'


async def crawl(request, context):
    if 'entry' not in request:
        raise ValueError('request.entry is required')
    entry = request['entry']
    target_count = int(request.get('target_count') or 50)
    target_count = max(1, min(500, target_count))
    config = request.get('config') or {}
    page_param = config.get('page')
    page_num = None
    if page_param is not None:
        try:
            page_num = int(page_param)
        except Exception:
            page_num = None
    domain = urlparse(entry).netloc
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        try:
            resp = await client.get(entry)
            if resp.status_code != 200:
                raise ValueError(f"fetch failed for {domain}: HTTP {resp.status_code}")
        except ValueError:
            raise
        except Exception as e:
            status = getattr(e, 'response', None)
            status_code = status.status_code if status else 'unknown'
            raise ValueError(f"fetch failed for {domain}: HTTP {status_code}") from e
        html_text = resp.text
        try:
            items_all = extract_all_items(html_text, entry)
        except Exception as e:
            raise ValueError(f"parse failed for {domain}: {str(e)}") from e
        items_per_page = 12
        if page_num is not None:
            start = (page_num - 1) * items_per_page
            end = start + items_per_page
            selected_items = items_all[start:end]
        else:
            selected_items = []
            total_pages = min(10, (len(items_all) + items_per_page - 1) // items_per_page)
            for p in range(1, total_pages + 1):
                start = (p - 1) * items_per_page
                end = start + items_per_page
                page_items = items_all[start:end]
                selected_items.extend(page_items)
                if len(selected_items) >= target_count:
                    break
            selected_items = selected_items[:target_count]
        sem = asyncio.Semaphore(5)
        tasks = []
        detail_attempted_count = 0
        detail_success_count = 0
        detail_fallback_count = 0
        for item in selected_items:
            detail_attempted_count += 1
            tasks.append(fetch_detail(client, item, sem))
        if tasks:
            results = await asyncio.gather(*tasks)
            for success, _ in results:
                if success:
                    detail_success_count += 1
                else:
                    detail_fallback_count += 1
        for item in selected_items:
            if not item.get('content') and not item.get('summary'):
                item['summary'] = item['title']
        items = selected_items
        stats = {
            'discovered_count': len(items),
            'detail_attempted_count': detail_attempted_count,
            'detail_success_count': detail_success_count,
            'detail_fallback_count': detail_fallback_count,
        }
        return {'items': items, 'stats': stats}