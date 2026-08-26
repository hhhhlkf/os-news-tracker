import asyncio
import re
from urllib.parse import urljoin, urlparse

import httpx
from lxml import html, etree
from dateutil import parser as date_parser

BASE_SITE_SUFFIX = " - LMSYS Org"

async def fetch_url(client, url, stage, domain):
    try:
        resp = await client.get(url, timeout=20.0)
    except Exception as e:
        raise Exception(f"{stage} failed for {domain}: network error: {e}")
    if resp.status_code != 200:
        raise Exception(f"{stage} failed for {domain}: HTTP {resp.status_code}")
    return resp

def parse_sitemap(content, domain):
    try:
        root = etree.fromstring(content.encode('utf-8'))
    except Exception as e:
        raise Exception(f"parse failed for {domain}: sitemap XML parse error: {e}")
    urls = []
    for url_elem in root.iter():
        if url_elem.tag.endswith('}url') or url_elem.tag == 'url':
            loc = None
            lastmod = None
            for child in url_elem:
                if child.tag.endswith('}loc') or child.tag == 'loc':
                    loc = child.text.strip() if child.text else None
                elif child.tag.endswith('}lastmod') or child.tag == 'lastmod':
                    lastmod = child.text.strip() if child.text else None
            if loc:
                urls.append((loc, lastmod))
    return urls

def extract_date_from_slug(url):
    m = re.search(r'/blog/(\d{4}-\d{2}-\d{2})', url)
    if m:
        return m.group(1)
    return None

def clean_text(text):
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def extract_content(tree):
    for elem in tree.xpath('//script|//style|//nav|//header|//footer'):
        elem.getparent().remove(elem)
    candidates = []
    for selector in ['//main', '//article', "//div[contains(@class,'prose')]", "//div[contains(@class,'content')]"]:
        elems = tree.xpath(selector)
        for e in elems:
            text = clean_text(e.text_content())
            if text:
                candidates.append(text)
    if candidates:
        return max(candidates, key=len)
    body = tree.xpath('//body')
    if body:
        return clean_text(body[0].text_content())
    return ''

async def crawl(request, context):
    entry = request.get('entry')
    if not entry:
        raise ValueError('request.entry is required')
    domain = urlparse(entry).netloc
    if not domain:
        domain = 'www.lmsys.org'
    target_count = int(request.get('target_count', 10))
    start_at = request.get('start_at')
    end_at = request.get('end_at')
    start_dt = None
    end_dt = None
    if start_at:
        try:
            start_dt = date_parser.parse(start_at)
        except Exception:
            start_dt = None
    if end_at:
        try:
            end_dt = date_parser.parse(end_at)
        except Exception:
            end_dt = None

    async with httpx.AsyncClient(follow_redirects=True) as client:
        sitemap_url = urljoin(entry, '/sitemap.xml')
        sitemap_resp = await fetch_url(client, sitemap_url, 'fetch', domain)
        sitemap_entries = parse_sitemap(sitemap_resp.text, domain)
        blog_entries = []
        for loc, lastmod in sitemap_entries:
            path = urlparse(loc).path
            if path.startswith('/blog/') and not path == '/blog/':
                slug = path[len('/blog/'):]
                if slug and not slug.endswith('/'):
                    date_str = extract_date_from_slug(loc)
                    if date_str:
                        try:
                            pub_dt = date_parser.parse(date_str)
                        except Exception:
                            pub_dt = None
                    elif lastmod:
                        try:
                            pub_dt = date_parser.parse(lastmod)
                        except Exception:
                            pub_dt = None
                    else:
                        pub_dt = None
                    if start_dt and pub_dt and pub_dt < start_dt:
                        continue
                    if end_dt and pub_dt and pub_dt > end_dt:
                        continue
                    blog_entries.append((loc, pub_dt))

        dated = [e for e in blog_entries if e[1] is not None]
        undated = [e for e in blog_entries if e[1] is None]
        dated.sort(key=lambda x: x[1], reverse=True)
        blog_entries = dated + undated
        entries_to_fetch = blog_entries[:target_count]

        async def process_entry(loc, pub_dt):
            post_domain = urlparse(loc).netloc or domain
            post_resp = await fetch_url(client, loc, 'fetch', post_domain)
            try:
                tree = html.fromstring(post_resp.text)
            except Exception as e:
                raise Exception(f"parse failed for {post_domain}: HTML parse error: {e}")
            title_elem = tree.xpath('//title')
            if title_elem:
                title = clean_text(title_elem[0].text_content())
                title = re.sub(r'\s*-\s*LMSYS Org\s*$', '', title, flags=re.IGNORECASE).strip()
                if not title:
                    title = None
            else:
                title = None
            if not title:
                h1_elems = tree.xpath('//h1')
                if h1_elems:
                    title = clean_text(h1_elems[0].text_content())
            if not title:
                slug = urlparse(loc).path.split('/')[-1]
                title = slug.replace('-', ' ').title()
            content = extract_content(tree)
            content = content[:2000]
            published_at = pub_dt.isoformat() if pub_dt else None
            return {
                'title': title,
                'url': loc,
                'published_at': published_at,
                'content': content,
            }

        items = []
        if entries_to_fetch:
            sem = asyncio.Semaphore(5)
            async def sem_process(entry):
                async with sem:
                    return await process_entry(*entry)
            tasks = [sem_process(entry) for entry in entries_to_fetch]
            items = await asyncio.gather(*tasks)
            items = [it for it in items if it is not None]

        if start_dt or end_dt:
            filtered = []
            for it in items:
                if it.get('published_at'):
                    try:
                        dt = date_parser.parse(it['published_at'])
                        if start_dt and dt < start_dt:
                            continue
                        if end_dt and dt > end_dt:
                            continue
                    except Exception:
                        pass
                filtered.append(it)
            items = filtered

        items = items[:target_count]
        return {
            'items': items,
            'stats': {'discovered_count': len(items)}
        }
