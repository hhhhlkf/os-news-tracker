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
def fetch_page(url: str, render_js: bool = False) -> dict:
    """抓取页面，返回 status/title/links。render_js=True 用 Playwright。"""
    import httpx
    ensure_not_cancelled()
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
            return {"url": url, "status": 200, "title": title, "links": links, "html": html[:2000]}
    r = httpx.get(url, timeout=15, follow_redirects=True)
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
    parser.feed(r.text)
    return {
        "url": str(r.url),
        "status": r.status_code,
        "title": parser.title.strip(),
        "links": parser.links[:100],
        "html": r.text[:2000],
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


TOOLS = [fetch_page, capture_network, inspect_item, test_url_template, test_path_join, probe_url_patterns]
