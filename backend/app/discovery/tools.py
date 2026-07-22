"""LangChain @tool 工具集：供 Explorer/Validator worker 自主调用的探查工具。

全部为只读探查动作（fetch/inspect/test），不写文件、不执行命令。Playwright 仅在
render_js=True 或 capture_network 时按需启动，模块加载零浏览器开销。
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
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


@tool
def probe_article_content(
    article_url: str,
    sample_item: dict,
    list_url: str | None = None,
    id_field: str | None = None,
    min_content_chars: int = 300,
) -> dict:
    """验证文章详情正文获取策略；HTML 失败后探测详情 JSON API。"""
    import httpx
    from app.discovery.article_tools import fetch_article_page_content

    ensure_not_cancelled()
    html_result = fetch_article_page_content(article_url, timeout_seconds=8, content_char_limit=3000)
    html_content_chars = len(html_result.get("content") or "")
    if html_content_chars >= min_content_chars:
        return {
            "content_verified": True,
            "content_strategy": "html_page",
            "content_chars": html_content_chars,
            "html_status": html_result.get("status"),
            "article_url": html_result.get("resolved_url") or article_url,
        }

    id_value = _sample_id_value(sample_item, id_field)
    detail_candidates = _detail_api_candidates(article_url=article_url, list_url=list_url, id_value=id_value)
    for api_url in detail_candidates:
        ensure_not_cancelled()
        api_result = _probe_detail_api_url(api_url, min_content_chars=min_content_chars)
        if api_result.get("content_verified"):
            return {
                **api_result,
                "html_status": html_result.get("status"),
                "html_content_chars": html_content_chars,
                "article_url": article_url,
            }

    for cap in _capture_article_json_responses(article_url):
        ensure_not_cancelled()
        payload = cap.get("parsed_json")
        api_result = _probe_detail_api_payload(
            payload,
            api_url=cap.get("api_url"),
            min_content_chars=min_content_chars,
        )
        if api_result.get("content_verified"):
            return {
                **api_result,
                "html_status": html_result.get("status"),
                "html_content_chars": html_content_chars,
                "article_url": article_url,
            }

    return {
        "content_verified": False,
        "content_strategy": "none",
        "content_chars": 0,
        "html_status": html_result.get("status"),
        "html_content_chars": html_content_chars,
        "article_url": article_url,
        "detail_api_candidates": detail_candidates[:6],
    }


def _sample_id_value(sample_item: dict, id_field: str | None) -> str | None:
    if id_field:
        raw = sample_item.get("raw") if isinstance(sample_item.get("raw"), dict) else {}
        value = raw.get(id_field) or sample_item.get(id_field)
        if value is not None:
            return str(value)
    value = sample_item.get("id")
    return str(value) if value is not None else None


def _detail_api_candidates(*, article_url: str, list_url: str | None, id_value: str | None) -> list[str]:
    if not id_value:
        return []
    candidates: list[str] = []
    parsed_article = urlparse(article_url)
    origin = f"{parsed_article.scheme}://{parsed_article.netloc}"
    if list_url:
        parsed_list = urlparse(list_url)
        list_path = parsed_list.path
        parent = list_path.rsplit("/", 1)[0]
        if parent:
            for filename in (
                "detail.json",
                "getDetail.json",
                "getArticleDetail.json",
                "articleDetail.json",
                "getBlogDetail.json",
                "blogDetail.json",
            ):
                candidates.append(_url_with_query(urljoin(f"{parsed_list.scheme}://{parsed_list.netloc}", f"{parent}/{filename}"), {"no": id_value}))
                candidates.append(_url_with_query(urljoin(f"{parsed_list.scheme}://{parsed_list.netloc}", f"{parent}/{filename}"), {"id": id_value}))
        if "ByCategory" in list_path:
            detail_path = list_path.replace("blogByCategoryPage", "detail").replace("ByCategoryPage", "Detail")
            candidates.append(_url_with_query(urljoin(f"{parsed_list.scheme}://{parsed_list.netloc}", detail_path), {"no": id_value}))
    article_parts = [part for part in parsed_article.path.split("/") if part]
    if "detail" in article_parts:
        idx = article_parts.index("detail")
        api_parts = ["api", *article_parts[:idx], "detail.json"]
        candidates.append(_url_with_query(urljoin(origin, "/" + "/".join(api_parts)), {"no": id_value}))
        candidates.append(_url_with_query(urljoin(origin, "/" + "/".join(api_parts)), {"id": id_value}))
    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped[:12]


def _url_with_query(url: str, values: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(values)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _probe_detail_api_url(api_url: str, *, min_content_chars: int) -> dict:
    import httpx

    try:
        response = httpx.get(api_url, timeout=8, follow_redirects=True)
        content_type = response.headers.get("content-type", "")
        if response.status_code >= 400 or "json" not in content_type.lower():
            return {"content_verified": False}
        payload = response.json()
    except Exception:
        return {"content_verified": False}
    return _probe_detail_api_payload(payload, api_url=api_url, min_content_chars=min_content_chars)


def _probe_detail_api_payload(payload: object, *, api_url: str | None, min_content_chars: int) -> dict:
    content_path, content_text = _find_best_text_path(payload, ("content", "body", "html", "article"))
    if not content_path or len(_strip_tags(content_text)) < min_content_chars:
        return {"content_verified": False}
    field_paths = {
        "content": content_path,
        "title": _find_first_text_path(payload, ("title", "name", "subject")),
        "summary": _find_first_text_path(payload, ("summary", "description", "desc", "brief")),
        "published_at": _find_first_text_path(payload, ("publishTime", "published_at", "publishedAt", "date", "created_at", "gmtCreate")),
    }
    return {
        "content_verified": True,
        "content_strategy": "detail_api",
        "content_chars": len(_strip_tags(content_text)),
        "detail_api": {
            "url_template": _template_api_url(api_url),
            "fields": {k: v for k, v in field_paths.items() if v},
            "method": "GET",
        },
    }


def _capture_article_json_responses(article_url: str) -> list[dict]:
    from playwright.sync_api import sync_playwright

    caps: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(resp):
            try:
                content_type = resp.headers.get("content-type", "")
                if "json" not in content_type.lower():
                    return
                body = resp.text()
                if not body or len(body) > 500000:
                    return
                caps.append({"api_url": resp.url, "status": resp.status, "parsed_json": json.loads(body)})
            except Exception:
                return

        page.on("response", on_response)
        try:
            page.goto(article_url, wait_until="networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(1000)
        browser.close()
    return caps[:10]


def _find_best_text_path(payload: object, names: tuple[str, ...]) -> tuple[str | None, str]:
    best_path = None
    best_text = ""

    def walk(node: object, path: str) -> None:
        nonlocal best_path, best_text
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path}.{key}" if path else key
                if any(name.lower() in key.lower() for name in names) and isinstance(value, str):
                    text = _strip_tags(value)
                    if len(text) > len(best_text):
                        best_path = child_path
                        best_text = value
                walk(value, child_path)
        elif isinstance(node, list):
            for idx, value in enumerate(node[:5]):
                walk(value, f"{path}.{idx}" if path else str(idx))

    walk(payload, "")
    return best_path, best_text


def _find_first_text_path(payload: object, names: tuple[str, ...]) -> str | None:
    path, text = _find_best_text_path(payload, names)
    return path if text else None


def _strip_tags(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(value or "")).split())


def _template_api_url(api_url: str | None) -> str:
    if not api_url:
        return ""
    parsed = urlparse(api_url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    templated = [
        f"{key}={{item.{key}}}" if key in {"id", "no", "slug", "uuid"} else f"{key}={value}"
        for key, value in query
    ]
    return urlunparse(parsed._replace(query="&".join(templated)))


TOOLS = [
    fetch_page,
    capture_network,
    inspect_item,
    probe_html_entries,
    test_url_template,
    test_path_join,
    probe_url_patterns,
    probe_article_content,
]
