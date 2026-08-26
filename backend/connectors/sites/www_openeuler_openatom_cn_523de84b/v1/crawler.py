import asyncio
import re
import html as html_module
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from lxml import html as lxml_html
from playwright.async_api import async_playwright


MAX_CONTENT_CHARS = 2000
DETAIL_FETCH_CHUNK = 20
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'


def clean_text(raw: str) -> str:
    if not raw:
        return ''
    text = re.sub(r'<[^>]+>', ' ', raw)
    text = html_module.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:MAX_CONTENT_CHARS]


def extract_article_content(html_text: str) -> str | None:
    try:
        tree = lxml_html.fromstring(html_text)
    except Exception:
        return None
    for elem in tree.xpath('//script|//style'):
        elem.getparent().remove(elem)
    candidates = [
        '//article',
        '//main',
        '//div[contains(@class, "content")]',
        '//div[contains(@class, "article")]',
        '//div[contains(@class, "post")]',
        '//div[contains(@class, "entry")]',
    ]
    best = None
    best_len = 0
    for xpath in candidates:
        elems = tree.xpath(xpath)
        for elem in elems:
            text = clean_text(elem.text_content())
            if len(text) > best_len:
                best = text
                best_len = len(text)
    if best:
        return best[:MAX_CONTENT_CHARS]
    body = tree.xpath('//body')
    if body:
        return clean_text(body[0].text_content())[:MAX_CONTENT_CHARS]
    return None


def parse_list_item(text: str, href: str) -> dict:
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    if not lines:
        return None
    title = lines[0]
    date_str = None
    for line in lines:
        m = re.search(r'(\d{4}-\d{2}-\d{2})', line)
        if m:
            date_str = m.group(1)
            break
    summary = ''
    for line in lines:
        if line == title or (date_str and date_str in line):
            continue
        if len(line) > len(summary) and len(line) > 30:
            summary = line
    if not summary and len(lines) > 1:
        summary = lines[1]
    summary = clean_text(summary)
    published_at = None
    if date_str:
        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
            published_at = dt.replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            published_at = None
    return {
        'title': clean_text(title),
        'url': href,
        'published_at': published_at,
        'summary': summary,
    }


async def extract_list_items(page):
    js_code = '''
    () => {
      const links = Array.from(document.querySelectorAll('a[href*="/zh/blog/"]'));
      return links.map(a => ({ text: a.innerText.trim(), href: a.href }));
    }
    '''
    raw_items = await page.evaluate(js_code)
    items = []
    for raw in raw_items:
        item = parse_list_item(raw['text'], raw['href'])
        if item and item['title'] and item['url']:
            items.append(item)
    return items


async def get_page_signature(page) -> str:
    js_code = '''
    () => {
      const links = Array.from(document.querySelectorAll('a[href*="/zh/blog/"]'));
      const urls = links.map(a => a.href).sort();
      return JSON.stringify(urls);
    }
    '''
    return await page.evaluate(js_code)


async def click_page_number(page, page_number):
    loc = page.get_by_text(str(page_number), exact=True)
    if await loc.count() > 0:
        await loc.first.click()
        return True
    # fallback to broader text match
    loc = page.locator(f'text="{page_number}"')
    if await loc.count() > 0:
        await loc.first.click()
        return True
    return False


async def click_next_link(page):
    for text in ['下一页', 'next', 'Next', '>', '»']:
        loc = page.get_by_text(text, exact=False)
        if await loc.count() > 0:
            await loc.first.click()
            return True
    return False


async def wait_for_content_change(page, prev_signature, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        if await get_page_signature(page) != prev_signature:
            return True
        await asyncio.sleep(0.5)
    return False


async def crawl(request: dict, context: dict) -> dict:
    entry = request.get('entry')
    if not entry:
        raise ValueError('request.entry is required')

    target_count = int(request.get('target_count') or 50)
    target_count = max(1, min(500, target_count))

    config = request.get('config') or {}
    requested_page = config.get('page') if isinstance(config, dict) else None

    items = []
    seen_urls = set()
    max_pages = 10
    detail_attempted = 0
    detail_success = 0
    detail_fallback = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        try:
            await page.goto(entry, wait_until='networkidle', timeout=60000)
            await page.wait_for_selector('a[href*="/zh/blog/"]', timeout=30000)

            current_page = 1

            if requested_page is not None:
                target_page = int(requested_page)
                while current_page < target_page and current_page < max_pages:
                    prev_sig = await get_page_signature(page)
                    clicked = await click_page_number(page, current_page + 1)
                    if not clicked:
                        clicked = await click_next_link(page)
                    if not clicked:
                        separator = '&' if '?' in page.url else '?'
                        next_url = f"{entry}{separator}page={current_page + 1}"
                        await page.goto(next_url, wait_until='networkidle', timeout=60000)
                        changed = True
                    else:
                        changed = await wait_for_content_change(page, prev_sig, timeout=10)
                    if not changed:
                        break
                    current_page += 1

                page_items = await extract_list_items(page)
                for item in page_items:
                    if item['url'] not in seen_urls:
                        seen_urls.add(item['url'])
                        items.append(item)
                        if len(items) >= target_count:
                            break
            else:
                while len(items) < target_count and current_page <= max_pages:
                    page_items = await extract_list_items(page)
                    if not page_items:
                        break
                    new_added = 0
                    for item in page_items:
                        if item['url'] not in seen_urls:
                            seen_urls.add(item['url'])
                            items.append(item)
                            new_added += 1
                            if len(items) >= target_count:
                                break
                    if len(items) >= target_count:
                        break
                    if new_added == 0 and current_page > 1:
                        break
                    prev_sig = await get_page_signature(page)
                    clicked = await click_page_number(page, current_page + 1)
                    if not clicked:
                        clicked = await click_next_link(page)
                    if not clicked:
                        separator = '&' if '?' in page.url else '?'
                        next_url = f"{entry}{separator}page={current_page + 1}"
                        await page.goto(next_url, wait_until='networkidle', timeout=60000)
                        changed = True
                    else:
                        changed = await wait_for_content_change(page, prev_sig, timeout=10)
                    if not changed:
                        break
                    current_page += 1
        finally:
            await browser.close()

    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={'User-Agent': USER_AGENT}) as client:
        for item in items[:DETAIL_FETCH_CHUNK]:
            detail_attempted += 1
            try:
                response = await client.get(item['url'])
                if response.status_code == 200:
                    content = extract_article_content(response.text)
                    if content:
                        item['content'] = content
                        detail_success += 1
                    else:
                        item['content'] = item['summary']
                        detail_fallback += 1
                else:
                    item['content'] = item['summary']
                    detail_fallback += 1
            except Exception:
                item['content'] = item['summary']
                detail_fallback += 1

    for item in items:
        if 'content' not in item or not item['content']:
            item['content'] = item.get('summary', '')

    stats = {
        'discovered_count': len(items),
        'detail_attempted_count': detail_attempted,
        'detail_success_count': detail_success,
        'detail_fallback_count': detail_fallback,
    }
    return {'items': items, 'stats': stats}
