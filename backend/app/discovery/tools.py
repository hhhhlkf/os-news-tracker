"""LangChain @tool 工具集：供 Explorer/Validator worker 自主调用的探查工具。

全部为只读探查动作（fetch/inspect/test），不写文件、不执行命令。Playwright 仅在
render_js=True 或 capture_network 时按需启动，模块加载零浏览器开销。
"""

from __future__ import annotations

import json
from urllib.parse import urljoin
from html.parser import HTMLParser

from langchain_core.tools import tool

from app.discovery.cancel import ensure_not_cancelled


@tool
def fetch_page(
    url: str,
    render_js: bool = False,
    transport: str = "httpx",
    impersonate: str | None = None,
    stealthy_headers: bool = True,
) -> dict:
    """抓取页面，返回 status/title/links。transport=scrapling 用浏览器指纹 HTTP 调取。"""
    import httpx
    ensure_not_cancelled()
    if transport == "scrapling":
        from scrapling.fetchers import Fetcher

        page = Fetcher.get(
            url,
            stealthy_headers=stealthy_headers,
            impersonate=impersonate or "chrome",
        )
        body = getattr(page, "body", b"")
        if isinstance(body, bytes):
            text = body.decode(getattr(page, "encoding", None) or "utf-8", errors="replace")
        else:
            text = str(getattr(page, "html_content", None) or body or "")
        return _page_summary(
            url=str(getattr(page, "url", None) or url),
            status=int(getattr(page, "status", 0) or 0),
            text=text,
            content_type=(getattr(page, "headers", {}) or {}).get("content-type", ""),
            transport="scrapling",
            impersonate=impersonate or "chrome",
            stealthy_headers=stealthy_headers,
        )
    if render_js:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            p = b.new_page()
            ensure_not_cancelled()
            p.goto(url, wait_until="networkidle")
            html = p.content()
            title = p.title()
            links = p.eval_on_selector_all("a[href]", "els=>els.map(e=>e.href)")
            b.close()
            out = _page_summary(
                url=url,
                status=200,
                text=html,
                content_type="text/html",
                transport="playwright",
                impersonate=None,
                stealthy_headers=False,
            )
            out["title"] = title
            out["links"] = links[:100]
            return out
    r = httpx.get(url, timeout=15, follow_redirects=True)
    return _page_summary(
        url=str(r.url),
        status=r.status_code,
        text=r.text,
        content_type=(getattr(r, "headers", {}) or {}).get("content-type", ""),
        transport="httpx",
        impersonate=None,
        stealthy_headers=False,
    )


def _page_summary(
    *,
    url: str,
    status: int,
    text: str,
    content_type: str,
    transport: str,
    impersonate: str | None,
    stealthy_headers: bool,
) -> dict:
    # 轻量 HTML 解析：提取 <title> 和 <a href>，避免引入 BeautifulSoup
    class _TitleLinkParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.title = ""
            self._in_title = False
            self.links: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag == "title":
                self._in_title = True
            if tag == "a":
                href = dict(attrs).get("href")
                if href:
                    self.links.append(urljoin(url, href))

        def handle_data(self, data):
            if self._in_title:
                self.title += data

        def handle_endtag(self, tag):
            if tag == "title":
                self._in_title = False

    parser = _TitleLinkParser()
    parser.feed(text)
    out = {
        "url": url,
        "status": status,
        "content_type": content_type,
        "transport": transport,
        "impersonate": impersonate,
        "stealthy_headers": stealthy_headers,
        "title": parser.title.strip(),
        "links": parser.links[:100],
        "html": text[:2000],
    }
    feed = _summarize_feed(text)
    if feed:
        out["feed"] = feed
    return out


def _summarize_feed(text: str) -> dict | None:
    import feedparser

    parsed = feedparser.parse(text)
    if parsed.bozo and not parsed.entries:
        return None
    if not parsed.entries:
        return None
    samples = []
    for entry in parsed.entries[:5]:
        samples.append({
            "title": str(entry.get("title") or "").strip(),
            "url": str(entry.get("link") or "").strip(),
            "published_at": str(entry.get("published") or entry.get("updated") or "").strip(),
        })
    return {
        "title": str(parsed.feed.get("title") or "").strip(),
        "item_count": len(parsed.entries),
        "sample_items": samples,
    }


@tool
def capture_network(url: str) -> list:
    """Playwright 抓页面加载时的 JSON XHR/Fetch 响应。"""
    from playwright.sync_api import sync_playwright
    ensure_not_cancelled()
    caps: list = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_page()

        def on_resp(resp):
            # 捕获 JSON XHR/Fetch 响应，body 截断防 token 爆炸
            try:
                body = resp.text()
                if body and len(body) < 500000:
                    post_data = getattr(resp.request, "post_data", None)
                    request_json_body = None
                    if post_data:
                        try:
                            request_json_body = json.loads(post_data)
                        except Exception:
                            request_json_body = None
                    caps.append({
                        "api_url": resp.url,
                        "method": resp.request.method,
                        "status": resp.status,
                        "request_json_body": request_json_body,
                        "parsed_json": json.loads(body),
                    })
            except Exception:
                pass

        p.on("response", on_resp)
        try:
            ensure_not_cancelled()
            p.goto(url, wait_until="networkidle", timeout=15000)
        except Exception:
            pass
        p.wait_for_timeout(1000)
        b.close()
    return caps


@tool
def probe_html_entries(
    url: str,
    item_selector: str,
    link_selector: str | None = None,
    title_selector: str | None = None,
    date_selector: str | None = None,
) -> dict:
    """按给定 HTML selector 抽样探测文章条目，返回 title/url/date 样本。"""
    from playwright.sync_api import sync_playwright

    ensure_not_cancelled()
    samples: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        elements = page.query_selector_all(item_selector)

        def _pick(el, selector: str | None):
            if selector in (None, "", "self"):
                return el
            return el.query_selector(selector)

        for el in elements[:5]:
            ensure_not_cancelled()
            link_el = _pick(el, link_selector)
            title_el = _pick(el, title_selector)
            date_el = _pick(el, date_selector)
            href = None
            if link_el is not None:
                href = link_el.evaluate("(node) => node.href || node.getAttribute('href') || ''")
            title = ""
            if title_el is not None:
                title = (title_el.inner_text() or "").strip()
            published_at = ""
            if date_el is not None:
                published_at = (date_el.inner_text() or "").strip()
            samples.append({
                "title": title,
                "url": href or "",
                "published_at": published_at,
            })
        browser.close()

    valid = [sample for sample in samples if sample.get("title") and sample.get("url")]
    return {
        "count": len(samples),
        "valid_count": len(valid),
        "looks_like_article_list": len(valid) >= 2,
        "samples": samples,
    }


def _decode_json_lenient(text: str):
    """按标准 JSON 解析；若响应拼接了多个 JSON，则只取第一个对象。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text)
        return obj


@tool
def inspect_item(api_url: str, method: str = "GET", json_body: dict | None = None) -> dict:
    """看 API 返回的 item 结构。"""
    import httpx
    ensure_not_cancelled()
    r = httpx.request(method, api_url, json=json_body, timeout=15)
    try:
        sample = r.json()
    except Exception:
        sample = _decode_json_lenient(r.text)
    return {"status": r.status_code, "sample": sample}


@tool
def test_url_template(template: str, id_field: str, sample_items: list[dict]) -> dict:
    """用真实 ID 填模板逐个请求验证。"""
    import httpx
    results = []
    for it in sample_items[:5]:  # 最多验证 5 个样本
        ensure_not_cancelled()
        url = template.replace("{id}", str(it.get(id_field, "")))
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True)
            # 判定为文章页：HTTP 200 且有 <title>（不依赖正文长度，SPA 正文 JS 渲染）
            is_article = r.status_code == 200 and "<title>" in r.text
            results.append({"url": url, "status": r.status_code, "is_article_page": is_article})
        except Exception as e:
            results.append({"url": url, "status": 0, "is_article_page": False, "error": str(e)})
    return {"results": results}


@tool
def test_path_join(base_url: str, path_field: str, sample_items: list[dict]) -> dict:
    """用 base_url + path_field 的真实值逐个请求验证。"""
    import httpx

    results = []
    for it in sample_items[:5]:
        ensure_not_cancelled()
        raw = it.get("raw") if isinstance(it.get("raw"), dict) else {}
        sample_value = raw.get(path_field)
        if sample_value is None:
            sample_value = it.get(path_field)
        url = urljoin(base_url, str(sample_value or ""))
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True)
            is_article = r.status_code == 200 and "<title>" in r.text
            results.append({
                "sample_value": sample_value,
                "url": url,
                "status": r.status_code,
                "is_article_page": is_article,
            })
        except Exception as e:
            results.append({
                "sample_value": sample_value,
                "url": url,
                "status": 0,
                "is_article_page": False,
                "error": str(e),
            })
    return {"results": results}


_DEFAULT_URL_PATTERNS: list[str] = [
    "/blog/{id}",
    "/blog/detail/{id}",
    "/post/{id}",
    "/posts/{id}",
    "/article/{id}",
    "/articles/{id}",
    "/news/{id}",
    "/p/{id}",
    "/{id}",
]


@tool
def probe_url_patterns(base_url: str, id_value: str, patterns: list[str] | None = None) -> list:
    """LLM 没头绪时批量试常见 URL pattern，返回每个 pattern 的验证结果，供 LLM 选规律用（灵感来源）。"""
    import httpx
    candidates = patterns if patterns is not None else _DEFAULT_URL_PATTERNS
    results: list = []
    for pat in candidates:
        ensure_not_cancelled()
        path = pat.replace("{id}", str(id_value))
        if not path.startswith("/"):
            path = "/" + path
        generated = urljoin(base_url, path)
        try:
            r = httpx.get(generated, timeout=15, follow_redirects=True)
            is_article = r.status_code == 200 and "<title>" in r.text
            results.append({
                "pattern": pat,
                "generated_url": generated,
                "status": r.status_code,
                "is_article_page": is_article,
            })
        except Exception as e:
            results.append({
                "pattern": pat,
                "generated_url": generated,
                "status": 0,
                "is_article_page": False,
                "error": str(e),
            })
    return results


TOOLS = [fetch_page, capture_network, inspect_item, probe_html_entries, test_url_template, test_path_join, probe_url_patterns]
