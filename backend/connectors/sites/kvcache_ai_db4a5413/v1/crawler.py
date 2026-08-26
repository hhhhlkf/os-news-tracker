import asyncio
import re
from datetime import timezone
from urllib.parse import urljoin

import httpx
from lxml import html
from dateutil import parser as date_parser


def clean_text(raw: str) -> str:
    """Remove HTML/XML tags, script/style content, decode entities, normalize whitespace, truncate to 2000 chars."""
    if not raw:
        return ""
    try:
        doc = html.fromstring(raw)
        for elem in doc.xpath('//script | //style'):
            elem.getparent().remove(elem)
        text = doc.text_content()
    except Exception:
        text = raw
    text = ' '.join(text.split())
    return text[:2000]


def extract_text(element, class_name):
    """Return cleaned text of first descendant with given class."""
    matches = element.xpath(f'.//*[contains(concat(" ", normalize-space(@class), " "), " {class_name} ")]')
    if matches:
        return clean_text(matches[0].text_content() or '')
    return ''


def parse_date(date_text):
    """Parse date string to ISO8601 with UTC timezone."""
    if not date_text:
        return ''
    try:
        dt = date_parser.parse(date_text, fuzzy=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return date_text


def extract_article_content(html_text):
    """Extract main content from article HTML page."""
    try:
        doc = html.fromstring(html_text)
    except Exception:
        return ''
    # Remove non-content elements
    for bad in doc.xpath('//script | //style | //nav | //header | //footer | //aside | //*[contains(concat(" ", normalize-space(@class), " "), " hb-toc")] | //*[contains(concat(" ", normalize-space(@class), " "), " toc ")]'):
        bad.getparent().remove(bad)
    # Try to find main content container (main, article, post, content, article-content)
    candidates = doc.xpath('//main | //article | //div[contains(concat(" ", normalize-space(@class), " "), " post ") or contains(concat(" ", normalize-space(@class), " "), " content ") or contains(concat(" ", normalize-space(@class), " "), " article ")]')
    if candidates:
        best = max(candidates, key=lambda e: len(e.text_content()))
        text = clean_text(best.text_content() or '')
    else:
        text = clean_text(doc.text_content() or '')
    return text


async def fetch(client: httpx.AsyncClient, url: str, phase: str) -> str:
    """Fetch URL and return text; raise on network error or non-200 status."""
    try:
        resp = await client.get(url)
    except Exception as e:
        raise RuntimeError(f"fetch {phase} failed for {url}: {e}") from e
    if resp.status_code != 200:
        raise RuntimeError(f"fetch {phase} failed for {url}: HTTP {resp.status_code}")
    return resp.text


async def crawl(request, context):
    """Crawl kvcache.ai blog index and return items with full article content."""
    entry = request.get('entry')
    if not entry:
        raise ValueError('request.entry is required')

    config = request.get('config', {})
    # Pagination is not supported (static single page with all entries)
    # If config contains 'page', we still ignore it explicitly

    target_count = int(request.get('target_count') or 50)
    if target_count < 1:
        target_count = 1
    elif target_count > 500:
        target_count = 500

    async with httpx.AsyncClient(timeout=30) as client:
        index_html = await fetch(client, entry, 'index')
        tree = html.fromstring(index_html)
        cards = tree.xpath('//*[contains(concat(" ", normalize-space(@class), " "), " blog-item-full ")]')
        items = []
        seen_urls = set()

        for card in cards:
            # The card itself is usually an <a>; if not, find descendant <a>
            a_tag = card if card.tag == 'a' else card.xpath('.//a')[0] if card.xpath('.//a') else None
            if a_tag is None:
                a_tag = card
            href = a_tag.get('href') or ''
            if not href:
                continue
            url = urljoin(entry, href)
            if url in seen_urls:
                continue
            seen_urls.add(url)

            title = extract_text(card, 'blog-item-title')
            if not title:
                title = clean_text(card.text_content() or '')

            # Extract publication date from first span in .blog-item-meta
            published_at = ''
            meta_matches = card.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " blog-item-meta ")]')
            if meta_matches:
                spans = meta_matches[0].xpath('.//span')
                if spans:
                    date_text = spans[0].text_content().strip()
                    published_at = parse_date(date_text)

            # Fetch article page to get full content
            article_html = await fetch(client, url, 'article')
            content = extract_article_content(article_html)
            if not content:
                # Fallback to excerpt if article content extraction fails
                excerpt = extract_text(card, 'blog-item-excerpt')
                content = excerpt if excerpt else title

            items.append({
                'title': title,
                'url': url,
                'published_at': published_at,
                'content': content[:2000]
            })

            if len(items) >= target_count:
                break

    if not items:
        raise RuntimeError(f"parse failed for {entry}: no articles found")

    return {
        'items': items,
        'stats': {
            'discovered_count': len(items)
        }
    }
