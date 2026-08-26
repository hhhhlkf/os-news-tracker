import asyncio
import re
import html as html_mod
from datetime import timezone
from dateutil import parser as date_parser
from typing import Any, Dict, List, Optional

import httpx
import feedparser


async def crawl(request: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    entry = request.get('entry')
    if not entry:
        raise ValueError('request.entry is required')

    target_count = int(request.get('target_count') or 50)
    target_count = max(1, min(target_count, 500))

    config = request.get('config') or {}
    # pagination not supported; ignore config.get('page', 1)

    feed_url = entry
    timeout = 10.0

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(feed_url)
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"fetch list failed for {feed_url}: {e}") from e

        feed = feedparser.parse(resp.text)
        entries = feed.entries[:target_count]

        items: List[Dict[str, Any]] = []
        detail_attempted_count = 0
        detail_success_count = 0
        detail_fallback_count = 0

        async def fetch_detail_content(item_id: str) -> Optional[str]:
            nonlocal detail_attempted_count, detail_success_count, detail_fallback_count
            detail_attempted_count += 1
            detail_url = f'https://aihot.virxact.com/items/{item_id}/markdown'
            try:
                resp = await client.get(detail_url)
                resp.raise_for_status()
                markdown_text = resp.text
                # extract content after '## 正文' if present
                marker = '## 正文'
                if marker in markdown_text:
                    content_text = markdown_text.split(marker, 1)[1]
                else:
                    content_text = markdown_text
                # clean and truncate
                content_text = clean_text(content_text)
                if content_text:
                    detail_success_count += 1
                    return content_text[:2000]
                else:
                    detail_fallback_count += 1
                    return None
            except Exception:
                detail_fallback_count += 1
                return None

        for entry_obj in entries:
            title = entry_obj.get('title', '')
            link = entry_obj.get('link', '')
            guid = entry_obj.get('guid', '')
            pubDate = entry_obj.get('published', entry_obj.get('pubDate', ''))

            published_at = None
            if pubDate:
                try:
                    dt = date_parser.parse(pubDate)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    published_at = dt.astimezone(timezone.utc).isoformat()
                except Exception:
                    published_at = None

            summary_html = entry_obj.get('summary', entry_obj.get('description', ''))
            summary = clean_text(summary_html)[:2000] if summary_html else ''

            item: Dict[str, Any] = {
                'title': title,
                'url': link,
                'published_at': published_at,
            }

            detail_content = None
            if guid:
                detail_content = await fetch_detail_content(guid)
            if detail_content:
                item['content'] = detail_content
            else:
                item['summary'] = summary

            items.append(item)

    stats = {
        'discovered_count': len(items),
        'detail_attempted_count': detail_attempted_count,
        'detail_success_count': detail_success_count,
        'detail_fallback_count': detail_fallback_count,
    }
    return {'items': items, 'stats': stats}


def clean_text(raw: str) -> str:
    if not raw:
        return ''
    # remove script/style blocks
    raw = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', raw, flags=re.DOTALL | re.IGNORECASE)
    # remove remaining HTML tags
    raw = re.sub(r'<[^>]+>', ' ', raw)
    # decode HTML entities
    raw = html_mod.unescape(raw)
    # collapse whitespace
    raw = re.sub(r'\s+', ' ', raw).strip()
    return raw
