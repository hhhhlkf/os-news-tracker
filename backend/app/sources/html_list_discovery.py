"""HTML news-list discovery for page_monitor sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from lxml import html as lxml_html

logger = logging.getLogger(__name__)

DATE_TEXT_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|June|Jul|July|Aug|Sep|Sept|Oct|Nov|Dec)"
    r"[a-z]*\s+\d{1,2},\s+\d{4}\b"
    r"|\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"
    r"|\b\d{4}年\d{1,2}月\d{1,2}日\b",
    re.IGNORECASE,
)

ARTICLE_PATH_HINTS = ("/news/", "/blog/", "/posts/", "/post/", "/article/", "/articles/")

HTML_LIST_DISCOVERY_PROMPT = """你是新闻列表页结构识别助手。目标是从一个 HTML 页面中找出稳定的 CSS 选择器，用于批量抓取新闻/博客列表。

你必须返回三个字段：
- link_selector：选择每篇文章链接的 CSS selector，必须尽量排除导航、页脚、语言切换、RSS、社交链接。
- title_selector：在每个链接元素内部或附近选择标题的 CSS selector；如果标题就是链接文字，可返回 null。
- date_selector：在每个链接元素内部、父级或最近公共卡片容器内选择发布日期的 CSS selector；必须尽量选择每条新闻自己的日期，不要选择页面生成时间。

判断标准：
1. 文章链接通常同域，路径类似 /news/slug、/blog/slug、/posts/slug，且会重复出现。
2. 标题通常在 h1/h2/h3/h4 或链接文字里。
3. 日期通常是类似 "June 18, 2026"、"2026-06-18"、"2026年6月18日" 的文本，常在 time、p、span、div 中。
4. selector 要能被 lxml/cssselect 使用，避免 :has()、:contains()、:nth-child() 这类脆弱或不兼容写法。
5. 如果无法可靠判断，字段填 null，不要编造。

页面 URL：{url}
候选链接摘要：
{candidates_json}

裁剪后的 HTML：
```html
{html_preview}
```

只输出 JSON，不要解释：
{{"link_selector":"a[href^=\\"/news/\\"]","title_selector":"h3","date_selector":"p.text-sm.text-muted-foreground","reason":"新闻卡片重复使用 /news/ 链接，标题在 h3，日期在同卡片的 p.text-sm.text-muted-foreground"}}
"""


@dataclass
class HtmlListSampleItem:
    title: str
    url: str
    date_text: str | None = None


@dataclass
class HtmlListDiscoveryResult:
    success: bool
    link_selector: str | None = None
    title_selector: str | None = None
    date_selector: str | None = None
    sample_items: list[HtmlListSampleItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    used_llm: bool = False
    prompt: str | None = None


def _fetch_html(url: str) -> str:
    response = httpx.get(url, timeout=20, follow_redirects=True)
    response.raise_for_status()
    return response.text


def _classes_selector(element) -> str:
    classes = [
        cls for cls in (element.get("class") or "").split()
        if re.fullmatch(r"[-_a-zA-Z0-9:]+", cls)
    ]
    if not classes:
        return element.tag
    escaped_classes = [cls.replace(":", r"\:") for cls in classes[:3]]
    return element.tag + "".join(f".{cls}" for cls in escaped_classes)


def _article_like_href(base_url: str, href: str) -> bool:
    absolute = urljoin(base_url, href)
    parsed_base = urlparse(base_url)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or parsed.netloc != parsed_base.netloc:
        return False
    path = parsed.path.rstrip("/")
    if not path or path == parsed_base.path.rstrip("/"):
        return False
    if any(hint in f"{path}/" for hint in ARTICLE_PATH_HINTS):
        return True
    slug = path.rsplit("/", 1)[-1]
    return "-" in slug and len(slug) >= 12


def _link_selector_from_hrefs(base_url: str, hrefs: list[str]) -> str | None:
    parsed_base = urlparse(base_url)
    base_path = parsed_base.path.rstrip("/")
    if base_path:
        prefix = f"{base_path}/"
        if sum(1 for href in hrefs if href.startswith(prefix)) >= 2:
            return f'a[href^="{prefix}"]'
    for hint in ARTICLE_PATH_HINTS:
        if sum(1 for href in hrefs if hint in urljoin(base_url, href)) >= 2:
            return f'a[href*="{hint}"]'
    return None


def _nearest_date_element(anchor):
    current = anchor
    for _ in range(5):
        if current is None:
            break
        for el in current.iter():
            if el is anchor:
                continue
            text = " ".join(el.text_content().split())
            if text and DATE_TEXT_RE.search(text):
                return el
        current = current.getparent()
    return None


def _extract_samples(tree, base_url: str, link_selector: str, title_selector: str | None, date_selector: str | None) -> list[HtmlListSampleItem]:
    samples: list[HtmlListSampleItem] = []
    seen: set[str] = set()
    for anchor in tree.cssselect(link_selector):
        href = (anchor.get("href") or "").strip()
        if not href or not _article_like_href(base_url, href):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        title = ""
        if title_selector:
            try:
                title_els = anchor.cssselect(title_selector)
                if title_els:
                    title = " ".join(title_els[0].text_content().split())
            except Exception:
                title = ""
        if not title:
            title = " ".join(anchor.text_content().split()) or absolute
        date_text = None
        if date_selector:
            try:
                date_els = anchor.cssselect(date_selector)
                parent = anchor.getparent()
                if not date_els and parent is not None:
                    date_els = parent.cssselect(date_selector)
                ancestor = parent.getparent() if parent is not None else None
                while not date_els and ancestor is not None:
                    date_els = ancestor.cssselect(date_selector)
                    ancestor = ancestor.getparent()
                if date_els:
                    date_text = " ".join(date_els[0].text_content().split())
            except Exception:
                date_text = None
        samples.append(HtmlListSampleItem(title=title, url=absolute, date_text=date_text))
        if len(samples) >= 5:
            break
    return samples


def _valid_sample_count(samples: list[HtmlListSampleItem]) -> int:
    return sum(
        1
        for sample in samples
        if sample.title and sample.url and sample.date_text and DATE_TEXT_RE.search(sample.date_text)
    )


def _deterministic_discover(url: str, html: str) -> HtmlListDiscoveryResult:
    tree = lxml_html.fromstring(html)
    anchors = tree.cssselect("a[href]")
    article_anchors = [
        anchor for anchor in anchors
        if _article_like_href(url, (anchor.get("href") or "").strip())
    ]
    hrefs = [(anchor.get("href") or "").strip() for anchor in article_anchors]
    link_selector = _link_selector_from_hrefs(url, hrefs)
    title_selector = None
    if article_anchors and sum(1 for a in article_anchors[:10] if a.cssselect("h3")) >= 2:
        title_selector = "h3"
    elif article_anchors and sum(1 for a in article_anchors[:10] if a.cssselect("h2")) >= 2:
        title_selector = "h2"

    date_elements = [_nearest_date_element(anchor) for anchor in article_anchors[:10]]
    date_elements = [el for el in date_elements if el is not None]
    date_selector = None
    if date_elements:
        selectors = [_classes_selector(el) for el in date_elements]
        date_selector = max(set(selectors), key=selectors.count)

    samples: list[HtmlListSampleItem] = []
    if link_selector:
        samples = _extract_samples(tree, url, link_selector, title_selector, date_selector)
    success = bool(link_selector and title_selector and date_selector and _valid_sample_count(samples) >= 2)
    return HtmlListDiscoveryResult(
        success=success,
        link_selector=link_selector,
        title_selector=title_selector,
        date_selector=date_selector,
        sample_items=samples,
        notes=["确定性识别完成"],
    )


def _candidate_summary(url: str, html: str) -> list[dict[str, str]]:
    tree = lxml_html.fromstring(html)
    candidates = []
    for anchor in tree.cssselect("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not _article_like_href(url, href):
            continue
        candidates.append({
            "href": href,
            "url": urljoin(url, href),
            "text": " ".join(anchor.text_content().split())[:160],
        })
        if len(candidates) >= 30:
            break
    return candidates


def _extract_json(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        return json.loads(brace.group(0))
    raise ValueError(f"No JSON found in html list discovery response: {text[:200]}")


def _llm_discover(url: str, html: str, llm=None) -> HtmlListDiscoveryResult:
    if llm is None:
        from app.llm.client import LlmClient
        llm = LlmClient()
    html_preview = html[:12000]
    prompt = HTML_LIST_DISCOVERY_PROMPT.format(
        url=url,
        candidates_json=json.dumps(_candidate_summary(url, html), ensure_ascii=False, indent=2),
        html_preview=html_preview,
    )
    raw = llm.complete(prompt)
    data = _extract_json(raw)
    link_selector = data.get("link_selector") if isinstance(data.get("link_selector"), str) else None
    title_selector = data.get("title_selector") if isinstance(data.get("title_selector"), str) else None
    date_selector = data.get("date_selector") if isinstance(data.get("date_selector"), str) else None
    tree = lxml_html.fromstring(html)
    samples = _extract_samples(tree, url, link_selector or "", title_selector, date_selector) if link_selector else []
    return HtmlListDiscoveryResult(
        success=bool(link_selector and title_selector and date_selector and _valid_sample_count(samples) >= 2),
        link_selector=link_selector,
        title_selector=title_selector,
        date_selector=date_selector,
        sample_items=samples,
        notes=[data.get("reason") or "LLM 兜底识别完成"],
        used_llm=True,
        prompt=prompt,
    )


def discover_html_list_source(url: str, *, html: str | None = None, llm=None) -> HtmlListDiscoveryResult:
    """Discover list-page selectors for a news/blog page."""
    page_html = html if html is not None else _fetch_html(url)
    deterministic = _deterministic_discover(url, page_html)
    if deterministic.success:
        return deterministic
    try:
        llm_result = _llm_discover(url, page_html, llm=llm)
    except Exception as exc:  # noqa: BLE001
        logger.warning("html list LLM discovery failed for %s: %s", url, exc)
        deterministic.notes.append(f"LLM 兜底失败: {exc}")
        return deterministic
    return llm_result if llm_result.success else deterministic
