"""System-owned Explore tool adapter executed only by SandboxRuntime."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from app.discovery.loop.agent import AgentExploreDecision


MAX_OBSERVATION_CHARS = 64_000
MAX_PROBE_SOURCE_CHARS = 60_000

_PROBE_ALLOWED_IMPORT_ROOTS = frozenset({
    "asyncio", "collections", "datetime", "decimal", "functools", "hashlib",
    "html", "httpx", "itertools", "json", "lxml", "math", "re", "statistics",
    "time", "typing", "urllib", "xml", "feedparser", "dateutil", "yaml",
    "playwright", "pydantic",
})

EXPLORE_TOOL_SOURCE = r'''
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timezone
import httpx
from lxml import html

def _browser_launch_options():
    proxy_server = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy_server:
        raise RuntimeError("sandbox browser proxy is not configured")
    return {"headless": True, "proxy": {"server": proxy_server}}

def _safe_path(root, raw):
    value = str(raw or "").replace("\\", "/")
    parts = Path(value).parts
    if not value or value.startswith("/") or ".." in parts:
        raise ValueError("unsafe workspace path")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("workspace path escaped")
    return path

def _relative_workspace_arg(raw):
    value = str(raw or "").replace("\\", "/")
    parts = value.split("/")
    if not value or value.startswith(("/", "~", "-")) or ".." in parts or parts[0] in {"proc","sys","dev","etc"} or any(token in value for token in ("$","`",";","|","&","<",">","\x00","\n","\r")):
        raise ValueError("bash path must stay inside /workspace")
    return value

def _validate_bash_argv(raw):
    argv = [str(value) for value in (raw or [])]
    if argv in (["ls"], ["ls", "-la"]):
        return argv
    if len(argv) == 6 and argv[:2] == ["find", "."] and argv[2] == "-maxdepth" and argv[4:] == ["-type", "f"] and argv[3].isdigit() and 1 <= int(argv[3]) <= 5:
        return argv
    if len(argv) == 5 and argv[:2] == ["head", "-n"] and argv[2].isdigit() and 1 <= int(argv[2]) <= 200 and argv[3] == "--":
        _relative_workspace_arg(argv[4]); return argv
    if len(argv) == 5 and argv[:3] == ["grep", "-n", "--"] and 0 < len(argv[3]) <= 500 and not argv[3].startswith("-") and not any(token in argv[3] for token in ("$","`",";","|","&","<",">","/etc/","/proc/","/sys/","/dev/","\x00","\n","\r")):
        _relative_workspace_arg(argv[4]); return argv
    raise ValueError("bash argv does not match a controlled workspace command schema")

async def crawl(request, context):
    config = request.get("config", {})
    action = config.get("action")
    args = config.get("args") or {}
    observation = {"action": action}
    with tempfile.TemporaryDirectory(prefix="discovery-explore-") as temporary:
        root = Path(temporary)
        for name, content in (config.get("files") or {}).items():
            path = _safe_path(root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content)[:65536], encoding="utf-8")
        if action == "http":
            method = str(args.get("method") or "GET").upper()
            if method not in {"GET", "HEAD"}:
                raise ValueError("http method is not allowed")
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent":"os-news-tracker/discovery-explore"}) as client:
                response = await client.request(method, args["url"])
                response.raise_for_status()
            document = html.fromstring(response.content) if response.content else None
            observation.update({"url":str(response.url),"status":response.status_code,"content_type":response.headers.get("content-type", ""),"title":" ".join(document.xpath("string(//title)").split()) if document is not None else "","text":" ".join(document.xpath("string(//body)").split())[:16000] if document is not None else "","links":[httpx.URL(str(response.url)).join(href).__str__() for href in (document.xpath("//a[@href]/@href")[:200] if document is not None else [])]})
        elif action == "browser":
            from playwright.async_api import async_playwright
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(**_browser_launch_options())
                page = await browser.new_page()
                response = await page.goto(args["url"], wait_until="domcontentloaded", timeout=20000)
                observation.update({"url":page.url,"status":response.status if response else None,"title":await page.title(),"text":(await page.locator("body").inner_text())[:16000],"links":(await page.locator("a").evaluate_all("els => els.slice(0,200).map(e => e.href)"))})
                await browser.close()
        elif action == "bash":
            argv = _validate_bash_argv(args.get("argv"))
            result = subprocess.run(argv, cwd=root, shell=False, capture_output=True, text=True, timeout=15, env={"PATH":"/usr/local/bin:/usr/bin:/bin"})
            observation.update({"argv":argv,"returncode":result.returncode,"stdout":result.stdout[:16000],"stderr":result.stderr[:8000]})
        elif action == "file":
            operation = args.get("operation")
            path = _safe_path(root, args.get("path"))
            if operation == "write":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(args.get("content") or "")[:65536], encoding="utf-8")
                observation.update({"operation":"write","path":str(path.relative_to(root)),"bytes":path.stat().st_size})
            elif operation == "read":
                observation.update({"operation":"read","path":str(path.relative_to(root)),"content":path.read_text(encoding="utf-8")[:16000]})
            elif operation == "list":
                observation.update({"operation":"list","path":str(path.relative_to(root)),"entries":[str(item.relative_to(root)) for item in path.iterdir()][:200]})
            else:
                raise ValueError("file operation is not allowed")
        else:
            raise ValueError("explore action is not allowed")
        files = {}
        for path in root.rglob("*"):
            if path.is_file() and len(files) < 20 and path.stat().st_size <= 65536:
                files[str(path.relative_to(root))] = path.read_text(encoding="utf-8", errors="replace")
    marker = datetime.now(timezone.utc).isoformat()
    return {"items":[{"title":"explore observation","url":request.get("entry"),"published_at":marker,"summary":"controlled sandbox observation"}],"stats":{"discovered_count":1,"observation":observation,"files":files}}
'''.strip()


PROBE_RUNTIME_SUPPORT = r'''
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlsplit

_PROBE_MAX_REQUESTS = 50
_PROBE_MAX_NETWORK_EVENTS = 100
_PROBE_MAX_BODY_CHARS = 65536
_PROBE_MAX_EVIDENCE_EVENTS = 50
_PROBE_MAX_SCROLLS = 10
_PROBE_MAX_TRACE_EVENTS = 200
_PROBE_MAX_RESOURCE_CHARS = 8 * 1024 * 1024
_PROBE_MAX_RESOURCE_TOTAL_CHARS = 32 * 1024 * 1024
_PROBE_MAX_RESOURCE_URLS = 40
_PROBE_MAX_SEARCH_PATTERNS = 30
_PROBE_MAX_SEARCH_MATCHES = 50

def _probe_text(value, limit=16000):
    return str(value or "")[:limit]

def _probe_json(value, limit=65536):
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) <= limit:
        return value
    return {"truncated": True, "preview": encoded[:limit]}

def _probe_url(url, allowed_domains):
    value = str(url or "")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ValueError("probe URL must be absolute public HTTP(S)")
    if host not in allowed_domains:
        raise ValueError("probe URL host is outside allowed_domains")
    return value

def _probe_selector(value):
    selector = str(value or "")
    if not selector or len(selector) > 1000 or "\x00" in selector:
        raise ValueError("probe selector is invalid")
    return selector

def _state_item_hashes(values):
    result = []
    for value in list(values or [])[:500]:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("state item keys must be strings or integers")
        text = str(value).strip()
        if not text or len(text) > 4000:
            raise ValueError("state item key is invalid")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest not in result:
            result.append(digest)
    return result

def _state_scalar_entries(value, limit=2000):
    """Flatten opaque evidence without assuming parameter or response field names."""
    stack = [("$", value, 0)]
    entries = []
    while stack and len(entries) < limit:
        path, current, depth = stack.pop()
        if depth > 12:
            continue
        if isinstance(current, dict):
            for key, nested in list(current.items())[:500]:
                stack.append((f"{path}.{str(key)[:120]}", nested, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            for index, nested in enumerate(list(current)[:500]):
                stack.append((f"{path}[{index}]", nested, depth + 1))
            continue
        if current is None or isinstance(current, bool):
            continue
        text = str(current).strip()
        if not 2 <= len(text) <= 1000:
            continue
        entries.append((text, path))
        if text.startswith(("http://", "https://")):
            try:
                for key, nested in parse_qsl(urlsplit(text).query, keep_blank_values=True)[:200]:
                    stack.append((f"{path}.query.{key[:120]}", nested, depth + 1))
            except ValueError:
                pass
        elif text[:1] in {"{", "["}:
            try:
                stack.append((f"{path}.json", json.loads(text), depth + 1))
            except (TypeError, ValueError):
                pass
    return entries

def _state_safe_records(values):
    return [_probe_json(item, 8000) for item in list(values or [])[:50]]

class _StateTransitions:
    def __init__(self, trace):
        self.trace = trace

    def snapshot(self, item_keys, requests=None, responses=None):
        snapshot = {
            "item_keys": _state_item_hashes(item_keys),
            "requests": _state_safe_records(requests),
            "responses": _state_safe_records(responses),
        }
        self.trace.record(
            "state.snapshot",
            item_count=len(snapshot["item_keys"]),
            request_count=len(requests or []),
            response_count=len(responses or []),
        )
        return snapshot

    def compare(self, before, after):
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError("state comparison requires two snapshots")
        first_keys = _state_item_hashes(before.get("item_keys") or [])
        next_keys = _state_item_hashes(after.get("item_keys") or [])
        # Snapshot keys are already hashes. Avoid hashing them a second time.
        if all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in before.get("item_keys") or []):
            first_keys = list(dict.fromkeys(before.get("item_keys") or []))[:500]
        if all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in after.get("item_keys") or []):
            next_keys = list(dict.fromkeys(after.get("item_keys") or []))[:500]
        new_keys = sorted(set(next_keys) - set(first_keys))

        response_values = {}
        for value, path in _state_scalar_entries(before.get("responses") or []):
            response_values.setdefault(value, []).append(path)
        before_requests = {
            hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            for item in list(before.get("requests") or [])[:100]
        }
        new_request_items = []
        for item in list(after.get("requests") or [])[:100]:
            digest = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            if digest not in before_requests:
                new_request_items.append((digest, item))
        request_values = {}
        for value, path in _state_scalar_entries([item for _digest, item in new_request_items]):
            request_values.setdefault(value, []).append(path)
        transfers = []
        for value in sorted(set(response_values) & set(request_values), key=lambda item: (len(item), item)):
            # Low-entropy constants are not useful state. The rule is based on
            # shape only and has no knowledge of pagination names or values.
            if len(value) < 3 or (value.isdigit() and len(value) < 4) or (value.isalpha() and len(value) < 5):
                continue
            transfers.append({
                "value_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                "response_paths": response_values[value][:5],
                "request_paths": request_values[value][:5],
            })
            if len(transfers) >= 100:
                break

        next_request_records = []
        for digest, item in new_request_items:
            next_request_records.append({
                "request_sha256": digest,
                "method": _probe_text(item.get("method"), 20) if isinstance(item, dict) else None,
                "url": _probe_text(item.get("url"), 4000) if isinstance(item, dict) else None,
            })
        result = {
            "follow_up_produced_new_items": bool(new_keys),
            "first_item_keys": first_keys,
            "next_item_keys": next_keys,
            "new_item_keys": new_keys,
            "new_item_count": len(new_keys),
            "new_requests": next_request_records[:50],
            "state_transfers": transfers,
        }
        self.trace.record(
            "state.compare",
            first_count=len(first_keys),
            next_count=len(next_keys),
            new_count=len(new_keys),
            new_request_count=len(next_request_records),
            state_transfer_count=len(transfers),
        )
        return result

class _Trace:
    def __init__(self):
        self.events = []

    def record(self, operation, **details):
        if len(self.events) >= _PROBE_MAX_TRACE_EVENTS:
            return
        safe = {str(key)[:80]: _probe_json(value, 4000) for key, value in details.items()}
        event = {"sequence":len(self.events) + 1,"operation":_probe_text(operation, 100),"details":safe}
        self.events.append(event)
        # The host treats stderr as untrusted data, then validates and projects
        # this bounded marker before persisting it as a public progress event.
        if event["sequence"] <= 40:
            os.write(2, ("PROBE_STEP " + json.dumps(event, ensure_ascii=False, default=str) + "\n").encode("utf-8")[:16384])

class _Evidence:
    def __init__(self, trace):
        self.events = []
        self.trace = trace

    def emit(self, kind, data):
        if len(self.events) >= _PROBE_MAX_EVIDENCE_EVENTS:
            raise ValueError("probe evidence event limit exceeded")
        safe_kind = _probe_text(kind, 100)
        safe_data = _probe_json(data)
        self.events.append({"kind":safe_kind,"data":safe_data})
        self.trace.record("evidence.emit", kind=safe_kind, data=safe_data)

    def api_candidate(self, data):
        self.emit("api_candidate", data)

    def pagination(self, data):
        self.emit("pagination", data)

    def warning(self, message):
        self.emit("warning", {"message": _probe_text(message, 2000)})

class _Http:
    def __init__(self, allowed_domains, trace):
        self.allowed_domains = allowed_domains
        self.trace = trace
        self.count = 0

    def _claim_request(self):
        self.count += 1
        if self.count > _PROBE_MAX_REQUESTS:
            raise ValueError("probe HTTP request limit exceeded")

    def _headers(self, headers):
        result = {"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36","Accept":"*/*"}
        for raw_name, raw_value in dict(headers or {}).items():
            name = str(raw_name).strip(); value = str(raw_value).strip()
            if not name or len(name) > 100 or len(value) > 4000 or "\n" in name + value or "\r" in name + value:
                raise ValueError("probe HTTP header is invalid")
            if name.lower() in {"host", "authorization", "proxy-authorization", "cookie", "set-cookie"}:
                raise ValueError("probe HTTP credential or routing header is not allowed")
            result[name] = value
        if len(result) > 32:
            raise ValueError("probe HTTP header count exceeded")
        return result

    async def request(self, method, url, json_body=None, headers=None):
        self._claim_request()
        verb = str(method or "GET").upper()
        if verb not in {"GET", "HEAD", "POST"}:
            raise ValueError("probe HTTP method is not allowed")
        target = _probe_url(url, self.allowed_domains)
        if json_body is not None and len(json.dumps(json_body, default=str)) > 65536:
            raise ValueError("probe HTTP JSON body is too large")
        self.trace.record("http.request", method=verb, url=target)
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=self._headers(headers)) as client:
            response = await client.request(verb, target, json=json_body)
        body = response.text[:_PROBE_MAX_BODY_CHARS] if verb != "HEAD" else ""
        result = {"url":str(response.url),"status":response.status_code,"content_type":response.headers.get("content-type", ""),"text":body}
        if "json" in result["content_type"].lower():
            try: result["json"] = _probe_json(response.json())
            except Exception: pass
        self.trace.record("http.response", method=verb, url=str(response.url), status=response.status_code, content_type=result["content_type"], body_chars=len(body))
        return result

    async def get(self, url, headers=None): return await self.request("GET", url, headers=headers)
    async def head(self, url): return await self.request("HEAD", url)
    async def post_json(self, url, body, headers=None): return await self.request("POST", url, json_body=body, headers=headers)

    async def read_text(self, url, max_chars=524288, headers=None):
        self._claim_request()
        target = _probe_url(url, self.allowed_domains)
        limit = min(max(int(max_chars), 1), _PROBE_MAX_RESOURCE_CHARS)
        self.trace.record("http.read_text", url=target, max_chars=limit)
        chunks = []; chars = 0; truncated = False
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=self._headers(headers)) as client:
            async with client.stream("GET", target) as response:
                response.raise_for_status()
                async for chunk in response.aiter_text():
                    remaining = limit - chars
                    if remaining <= 0:
                        truncated = True; break
                    chunks.append(chunk[:remaining]); chars += len(chunks[-1])
                    if len(chunk) > remaining:
                        truncated = True; break
                result = {"url":str(response.url),"status":response.status_code,"content_type":response.headers.get("content-type", ""),"text":"".join(chunks),"truncated":truncated}
        self.trace.record("http.read_text_result", url=result["url"], status=result["status"], chars=len(result["text"]), truncated=truncated)
        return result

    async def search_text(self, urls, patterns, regex=False, ignore_case=True, context_chars=240, max_matches=50, headers=None):
        values = [urls] if isinstance(urls, str) else list(urls or [])
        needles = [str(value) for value in ([patterns] if isinstance(patterns, str) else list(patterns or []))]
        if not values or len(values) > _PROBE_MAX_RESOURCE_URLS:
            raise ValueError("probe resource URL count must be between 1 and 40")
        if not needles or len(needles) > _PROBE_MAX_SEARCH_PATTERNS or any(not value or len(value) > 1000 for value in needles):
            raise ValueError("probe search patterns are invalid")
        context_limit = min(max(int(context_chars), 0), 2000)
        match_limit = min(max(int(max_matches), 1), _PROBE_MAX_SEARCH_MATCHES)
        flags = re.IGNORECASE if ignore_case else 0
        compiled = [re.compile(value if regex else re.escape(value), flags) for value in needles]
        matches = []; searched = []; total_chars = 0
        for raw_url in values:
            remaining = _PROBE_MAX_RESOURCE_TOTAL_CHARS - total_chars
            if remaining <= 0 or len(matches) >= match_limit: break
            try:
                resource = await self.read_text(raw_url, max_chars=min(_PROBE_MAX_RESOURCE_CHARS, remaining), headers=headers)
            except Exception as exc:
                searched.append({"url":_probe_text(raw_url, 4000),"error":type(exc).__name__})
                continue
            text = resource["text"]; total_chars += len(text)
            searched.append({"url":resource["url"],"status":resource["status"],"content_type":resource["content_type"],"chars":len(text),"truncated":resource["truncated"]})
            for pattern, matcher in zip(needles, compiled):
                for found in matcher.finditer(text):
                    start = max(0, found.start() - context_limit); end = min(len(text), found.end() + context_limit)
                    matches.append({"url":resource["url"],"pattern":pattern,"start":found.start(),"snippet":text[start:end]})
                    if len(matches) >= match_limit: break
                if len(matches) >= match_limit: break
        self.trace.record("http.search_text", resource_count=len(searched), pattern_count=len(needles), match_count=len(matches), searched_chars=total_chars)
        return {"searched":searched,"matches":matches,"searched_chars":total_chars,"truncated":len(values) > len(searched) or any(item["truncated"] for item in searched)}

    async def verify_candidate(self, url, method="GET", json_body=None, required_paths=None, headers=None):
        response = await self.request(method, url, json_body=json_body, headers=headers)
        payload = response.get("json"); paths = [str(value) for value in (required_paths or [])][:30]; found = {}
        for path in paths:
            current = payload; exists = True
            for part in path.split("."):
                if isinstance(current, dict) and part in current: current = current[part]
                elif isinstance(current, list) and part.isdigit() and int(part) < len(current): current = current[int(part)]
                else: exists = False; break
            found[path] = {"exists":exists,"type":type(current).__name__ if exists else None,"preview":_probe_text(current, 1000) if exists and not isinstance(current, (dict, list)) else None}
        result = {"url":response["url"],"status":response["status"],"content_type":response["content_type"],"is_json":payload is not None,"paths":found,"json":payload,"text_preview":response["text"][:4000]}
        self.trace.record("http.verify_candidate", url=result["url"], status=result["status"], is_json=result["is_json"], required_paths=paths)
        return result

class _ProbePage:
    def __init__(self, owner, page):
        self.owner = owner
        self.page = page
        self.scroll_count = 0

    async def click(self, selector):
        selector = _probe_selector(selector); self.owner.trace.record("browser.click", selector=selector); await self.page.locator(selector).first.click(timeout=10000)
    async def fill(self, selector, value):
        selector = _probe_selector(selector); self.owner.trace.record("browser.fill", selector=selector, value_chars=len(str(value or ""))); await self.page.locator(selector).first.fill(_probe_text(value, 4000), timeout=10000)
    async def press(self, selector, key):
        selector = _probe_selector(selector); key = _probe_text(key, 100); self.owner.trace.record("browser.press", selector=selector, key=key); await self.page.locator(selector).first.press(key, timeout=10000)
    async def select(self, selector, value):
        selector = _probe_selector(selector); self.owner.trace.record("browser.select", selector=selector); return await self.page.locator(selector).first.select_option(_probe_text(value, 500), timeout=10000)
    async def check(self, selector):
        selector = _probe_selector(selector); self.owner.trace.record("browser.check", selector=selector); await self.page.locator(selector).first.check(timeout=10000)
    async def hover(self, selector):
        selector = _probe_selector(selector); self.owner.trace.record("browser.hover", selector=selector); await self.page.locator(selector).first.hover(timeout=10000)
    async def scroll_into_view(self, selector):
        selector = _probe_selector(selector); self.owner.trace.record("browser.scroll_into_view", selector=selector); await self.page.locator(selector).first.scroll_into_view_if_needed(timeout=10000)

    async def scroll(self, direction="bottom", amount=1200):
        self.scroll_count += 1
        if self.scroll_count > _PROBE_MAX_SCROLLS:
            raise ValueError("probe scroll limit exceeded")
        self.owner.trace.record("browser.scroll", direction=direction, amount=amount, scroll_number=self.scroll_count)
        if direction == "bottom": await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "top": await self.page.evaluate("window.scrollTo(0, 0)")
        elif direction == "down": await self.page.evaluate("n => window.scrollBy(0, n)", min(max(int(amount), 1), 5000))
        else: raise ValueError("probe scroll direction is invalid")

    async def wait(self, seconds):
        seconds = min(max(float(seconds), 0), 5); self.owner.trace.record("browser.wait", seconds=seconds); await self.page.wait_for_timeout(seconds * 1000)
    async def wait_for_selector(self, selector):
        selector = _probe_selector(selector); self.owner.trace.record("browser.wait_for_selector", selector=selector); await self.page.locator(selector).first.wait_for(timeout=10000)
    async def wait_for_url(self, pattern):
        pattern = _probe_text(pattern, 1000); self.owner.trace.record("browser.wait_for_url", pattern=pattern); await self.page.wait_for_url(pattern, timeout=10000)
    async def wait_for_network_idle(self):
        self.owner.trace.record("browser.wait_for_network_idle"); await self.page.wait_for_load_state("networkidle", timeout=10000)
    async def title(self):
        value = _probe_text(await self.page.title(), 2000); self.owner.trace.record("browser.title", chars=len(value)); return value
    async def current_url(self): self.owner.trace.record("browser.current_url", url=self.page.url); return self.page.url
    async def text(self, selector="body"):
        selector = _probe_selector(selector); value = _probe_text(await self.page.locator(selector).first.inner_text(), 16000); self.owner.trace.record("browser.text", selector=selector, chars=len(value)); return value
    async def attr(self, selector, name):
        selector = _probe_selector(selector); name = _probe_text(name, 100); value = _probe_text(await self.page.locator(selector).first.get_attribute(name), 4000); self.owner.trace.record("browser.attr", selector=selector, name=name, chars=len(value)); return value
    async def links(self, selector="a"):
        selector = _probe_selector(selector); value = await self.page.locator(selector).evaluate_all("els => els.slice(0,200).map(e => ({text:(e.innerText||'').slice(0,500),href:e.href||''}))"); self.owner.trace.record("browser.links", selector=selector, count=len(value)); return value
    async def query(self, selector):
        loc = self.page.locator(_probe_selector(selector)).first
        result = {"count":await self.page.locator(_probe_selector(selector)).count(),"text":_probe_text(await loc.inner_text(), 4000) if await loc.count() else ""}; self.owner.trace.record("browser.query", selector=selector, count=result["count"]); return result
    async def query_all(self, selector):
        selector = _probe_selector(selector); value = await self.page.locator(selector).evaluate_all("els => els.slice(0,100).map(e => ({tag:e.tagName,text:(e.innerText||'').slice(0,1000),href:e.href||'',class:e.className||''}))"); self.owner.trace.record("browser.query_all", selector=selector, count=len(value)); return value
    async def html_fragment(self, selector):
        selector = _probe_selector(selector); value = _probe_text(await self.page.locator(selector).first.inner_html(), 16000); self.owner.trace.record("browser.html_fragment", selector=selector, chars=len(value)); return value
    async def resource_entries(self, resource_type=None):
        value = await self.page.evaluate("""kind => performance.getEntriesByType('resource').filter(e => !kind || e.initiatorType === kind).slice(0,500).map(e => ({url:e.name,resource_type:e.initiatorType,duration_ms:Math.round(e.duration),transfer_size:e.transferSize||0}))""", resource_type)
        value = [item for item in value if urlsplit(str(item.get("url") or "")).hostname in self.owner.allowed_domains]
        self.owner.trace.record("browser.resource_entries", resource_type=resource_type, count=len(value)); return value
    async def script_resources(self):
        dom = await self.page.locator("script").evaluate_all("""els => els.slice(0,300).map(e => ({url:e.src||'',type:e.type||'',async:!!e.async,defer:!!e.defer,integrity:e.integrity||'',inline_chars:e.src?0:(e.textContent||'').length,inline_preview:e.src?'':(e.textContent||'').slice(0,2000)}))""")
        perf = await self.resource_entries("script"); by_url = {}; inline = []
        for item in dom:
            if item.get("url"):
                try: _probe_url(item["url"], self.owner.allowed_domains)
                except ValueError: continue
                by_url[item["url"]] = item
            elif item.get("inline_chars"): inline.append(item)
        for item in perf:
            by_url.setdefault(item["url"], {"url":item["url"],"type":"","async":False,"defer":False,"integrity":"","inline_chars":0,"inline_preview":""})["performance"] = item
        value = list(by_url.values())[:300] + inline[:50]
        self.owner.trace.record("browser.script_resources", external_count=len(by_url), inline_count=len(inline)); return value
    async def read_script(self, url, max_chars=524288):
        result = await self.owner.http.read_text(url, max_chars=max_chars)
        self.owner.trace.record("browser.read_script", url=result["url"], chars=len(result["text"]), truncated=result["truncated"]); return result
    async def search_scripts(self, patterns, regex=False, ignore_case=True, context_chars=240, max_matches=50):
        resources = await self.script_resources(); urls = [item["url"] for item in resources if item.get("url")][:_PROBE_MAX_RESOURCE_URLS]
        result = await self.owner.http.search_text(urls, patterns, regex=regex, ignore_case=ignore_case, context_chars=context_chars, max_matches=max_matches) if urls else {"searched":[],"matches":[],"searched_chars":0,"truncated":False}
        inline_matches = []; flags = re.IGNORECASE if ignore_case else 0
        needles = [str(value) for value in ([patterns] if isinstance(patterns, str) else list(patterns or []))]
        if not needles or len(needles) > _PROBE_MAX_SEARCH_PATTERNS or any(not value or len(value) > 1000 for value in needles): raise ValueError("probe search patterns are invalid")
        context_limit = min(max(int(context_chars), 0), 2000); match_limit = min(max(int(max_matches), 1), _PROBE_MAX_SEARCH_MATCHES)
        compiled = [re.compile(value if regex else re.escape(value), flags) for value in needles]
        for script_index, item in enumerate(resources):
            if len(result["matches"]) + len(inline_matches) >= match_limit: break
            text = item.get("inline_preview") or ""
            for pattern, matcher in zip(needles, compiled):
                if len(result["matches"]) + len(inline_matches) >= match_limit: break
                for found in matcher.finditer(text):
                    inline_matches.append({"url":f"inline:{script_index}","pattern":pattern,"start":found.start(),"snippet":text[max(0,found.start()-context_limit):found.end()+context_limit]})
                    if len(result["matches"]) + len(inline_matches) >= match_limit: break
        result["matches"].extend(inline_matches)
        self.owner.trace.record("browser.search_scripts", script_count=len(resources), match_count=len(result["matches"])); return result
    async def requests(self): value = list(self.owner.network_requests); self.owner.trace.record("browser.requests", count=len(value)); return value
    async def responses(self):
        await asyncio.sleep(0.1)
        value = list(self.owner.network_responses); self.owner.trace.record("browser.responses", count=len(value)); return value
    async def response_json(self, contains):
        needle = _probe_text(contains, 1000)
        value = [item for item in self.owner.network_responses if needle in item.get("url", "") and "json" in item]; self.owner.trace.record("browser.response_json", url_contains=needle, count=len(value)); return value
    async def screenshot(self):
        data = await self.page.screenshot(type="png")
        result = {"sha256":hashlib.sha256(data).hexdigest(),"bytes":len(data)}; self.owner.trace.record("browser.screenshot", bytes=len(data)); return result
    async def console_logs(self): value = list(self.owner.console_events); self.owner.trace.record("browser.console_logs", count=len(value)); return value
    async def page_errors(self): value = list(self.owner.page_errors); self.owner.trace.record("browser.page_errors", count=len(value)); return value

class _Browser:
    def __init__(self, allowed_domains, trace, http):
        self.allowed_domains = allowed_domains
        self.trace = trace
        self.http = http
        self.capture_enabled = False
        self.capture_types = {"xhr", "fetch"}
        self.network_requests = []
        self.network_responses = []
        self.console_events = []
        self.page_errors = []
        self.open_count = 0
        self._playwright = None
        self._browser = None

    async def capture_network(self, types=None):
        self.capture_enabled = True
        self.capture_types = set(types or ["xhr", "fetch"])
        self.trace.record("browser.capture_network", resource_types=sorted(self.capture_types))

    async def open(self, url):
        self.open_count += 1
        if self.open_count > 10:
            raise ValueError("probe browser page limit exceeded")
        target = _probe_url(url, self.allowed_domains)
        self.trace.record("browser.open", url=target, page_number=self.open_count)
        from playwright.async_api import async_playwright
        if self._playwright is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(**_browser_launch_options())
        page = await self._browser.new_page()
        page.on("request", self._on_request)
        page.on("response", lambda response: asyncio.create_task(self._on_response(response)))
        page.on("console", lambda message: self.console_events.append({"type":message.type,"text":_probe_text(message.text, 2000)}) if len(self.console_events) < 50 else None)
        page.on("pageerror", lambda error: self.page_errors.append(_probe_text(error, 2000)) if len(self.page_errors) < 20 else None)
        response = await page.goto(target, wait_until="domcontentloaded", timeout=20000)
        wrapper = _ProbePage(self, page)
        wrapper.open_status = response.status if response else None
        self.trace.record("browser.opened", url=page.url, status=wrapper.open_status, title=_probe_text(await page.title(), 500))
        return wrapper

    def _on_request(self, request):
        if not self.capture_enabled or request.resource_type not in self.capture_types or len(self.network_requests) >= _PROBE_MAX_NETWORK_EVENTS: return
        self.network_requests.append({"url":request.url,"method":request.method,"resource_type":request.resource_type,"post_data":_probe_text(request.post_data, 8000)})

    async def _on_response(self, response):
        request = response.request
        if not self.capture_enabled or request.resource_type not in self.capture_types or len(self.network_responses) >= _PROBE_MAX_NETWORK_EVENTS: return
        record = {"url":response.url,"status":response.status,"resource_type":request.resource_type,"content_type":response.headers.get("content-type", "")}
        if "json" in record["content_type"].lower():
            try: record["json"] = _probe_json(await response.json())
            except Exception: pass
        self.network_responses.append(record)

    async def close(self):
        if self._browser is not None: await self._browser.close()
        if self._playwright is not None: await self._playwright.stop()

class ProbeTools:
    def __init__(self, allowed_domains):
        self.trace = _Trace()
        self.http = _Http(allowed_domains, self.trace)
        self.browser = _Browser(allowed_domains, self.trace, self.http)
        self.evidence = _Evidence(self.trace)
        self.state = _StateTransitions(self.trace)
'''.strip()


PROBE_RUNTIME_ENTRY = r'''
async def crawl(request, context):
    tools = ProbeTools(set(context.get("allowed_domains") or []))
    try:
        result = await probe({"entry":request.get("entry"),"objective":request.get("config",{}).get("objective"),"limits":{"max_scrolls":_PROBE_MAX_SCROLLS,"max_requests":_PROBE_MAX_REQUESTS}}, tools)
        observation = _probe_json({"probe_result":result,"evidence":tools.evidence.events,"trace":tools.trace.events}, 512000)
    finally:
        await tools.browser.close()
    marker = datetime.now(timezone.utc).isoformat()
    return {"items":[{"title":"probe observation","url":request.get("entry"),"published_at":marker,"summary":"sandbox probe completed"}],"stats":{"discovered_count":1,"observation":observation,"files":{}}}
'''.strip()


def validate_explore_decision(decision: AgentExploreDecision, *, site_url: str) -> None:
    if decision.action == "finish":
        if not decision.exploration_summary:
            raise ValueError("finish Explore decision requires exploration_summary")
        return
    if decision.action in {"http", "browser"}:
        url = str(decision.args.get("url") or "")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Explore URL must be absolute HTTP(S)")
        if parsed.username or parsed.password or parsed.hostname.lower() not in decision.allowed_domains:
            raise ValueError("Explore URL host is not declared")
    elif decision.action == "probe":
        source = str(decision.args.get("probe_py") or "")
        validate_probe_source(source)
    elif decision.action == "bash":
        _validate_host_bash_argv(decision.args.get("argv"))
    elif decision.action == "file":
        operation = decision.args.get("operation")
        path = str(decision.args.get("path") or "")
        if operation not in {"read", "write", "list"} or not path or path.startswith("/") or ".." in path.replace("\\", "/").split("/"):
            raise ValueError("Explore file action is outside the controlled workspace")
        if len(str(decision.args.get("content") or "")) > 65536:
            raise ValueError("Explore file content exceeds its limit")


def build_explore_tool_source(decision: AgentExploreDecision) -> str:
    """Return the fixed tool connector or a validated SDK-backed probe connector."""
    if decision.action != "probe":
        return EXPLORE_TOOL_SOURCE
    source = str(decision.args.get("probe_py") or "")
    validate_probe_source(source)
    return "\n\n".join((EXPLORE_TOOL_SOURCE, PROBE_RUNTIME_SUPPORT, source, PROBE_RUNTIME_ENTRY))


def probe_source_summary(decision: AgentExploreDecision) -> dict[str, Any]:
    """Expose only bounded source metadata in persisted/public action events."""
    if decision.action != "probe":
        return decision.args
    source = str(decision.args.get("probe_py") or "")
    return {
        "probe_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "probe_chars": len(source),
        "objective": str(decision.args.get("objective") or "")[:1000],
    }


def explore_action_fingerprint(decision: AgentExploreDecision) -> str:
    """Identify materially identical actions before paying for another sandbox."""
    args = dict(decision.args)
    # Rephrasing the objective or changing comments must not bypass deduplication.
    args.pop("objective", None)
    if decision.action == "probe" and isinstance(args.get("probe_py"), str):
        try:
            args["probe_py"] = ast.dump(ast.parse(args["probe_py"]), include_attributes=False)
        except SyntaxError:
            pass  # validate_probe_source reports the actionable syntax error later.
    payload = {
        "action": decision.action,
        "args": args,
        "allowed_domains": sorted(decision.allowed_domains),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def explore_strategy_family(decision: AgentExploreDecision) -> str:
    """Coarsely classify an Explore action so stalled strategies can be blocked."""
    if decision.action != "probe":
        return decision.action
    source = str(decision.args.get("probe_py") or "").lower()
    if "verify_candidate" in source:
        return "candidate_verification"
    if "search_scripts" in source or "read_script" in source or "script_resources" in source:
        return "script_search"
    if "capture_network" in source or ".responses(" in source or ".requests(" in source:
        return "browser_network"
    if "tools.browser" in source:
        return "browser_dom"
    if "feedparser" in source or "lxml" in source or "tools.http" in source:
        return "document_parse"
    return "probe_other"


def explore_evidence_tokens(observation: dict[str, Any]) -> set[str]:
    """Return stable evidence tokens; volatile prose/timestamps do not count as progress."""
    tokens: set[str] = set()
    meaningful_keys = {
        "url", "status", "content_type", "detail_url_template", "content_path",
        "verified_content_chars", "total", "at_least_200", "kind",
        "error", "type", "code", "api_candidates", "matches", "response_count",
    }

    def visit(value: Any, key: str = "", depth: int = 0) -> None:
        if depth > 8 or len(tokens) >= 1000:
            return
        if isinstance(value, dict):
            for raw_key, child in value.items():
                child_key = str(raw_key).lower()
                visit(child, child_key, depth + 1)
            return
        if isinstance(value, list):
            for child in value[:200]:
                visit(child, key, depth + 1)
            return
        if key not in meaningful_keys and not key.endswith("_url"):
            return
        text = str(value).strip()
        if not text:
            return
        if key == "url" or key.endswith("_url"):
            text = text.split("#", 1)[0]
        tokens.add(f"{key}:{text[:1000]}")

    visit(observation)
    # URLs embedded in bounded evidence are useful even when their surrounding key varies.
    encoded = json.dumps(observation, ensure_ascii=False, default=str)
    for url in re.findall(r"https?://[^\s\"'<>]+", encoded, flags=re.IGNORECASE)[:300]:
        tokens.add(f"url:{url.rstrip('.,);]').split('#', 1)[0][:1000]}")
    return tokens


def validate_probe_source(source: str) -> None:
    """Validate only the runtime entry contract; gVisor/proxy are the security boundary."""
    if not source.strip() or len(source) > MAX_PROBE_SOURCE_CHARS:
        raise ValueError("probe.py must be non-empty and at most 60000 characters")
    try:
        tree = ast.parse(source, filename="probe.py")
    except SyntaxError as exc:
        raise ValueError(f"probe.py syntax error: {exc.msg}") from exc
    probes = [
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "probe"
    ]
    if len(probes) != 1:
        raise ValueError("probe.py must define one top-level async def probe(request, tools)")
    if [arg.arg for arg in probes[0].args.args] != ["request", "tools"]:
        raise ValueError("probe.py entrypoint parameters must be request, tools")
    for node in ast.walk(tree):
        imported: list[str] = []
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise ValueError("probe.py relative imports are not allowed")
            imported = [node.module or ""]
        for module in imported:
            root = module.split(".", 1)[0]
            if root not in _PROBE_ALLOWED_IMPORT_ROOTS:
                allowed = ", ".join(sorted(_PROBE_ALLOWED_IMPORT_ROOTS))
                raise ValueError(
                    f"probe.py import is not available in Runtime: {root}; allowed imports: {allowed}"
                )
        if isinstance(node, ast.Name) and node.id == "__import__":
            raise ValueError("probe.py dynamic __import__ is not allowed; use an allowed import statement")


def bounded_observation(value: dict[str, Any]) -> dict[str, Any]:
    """Bound the Agent feedback independently of event/checkpoint persistence bounds."""
    import json

    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) <= MAX_OBSERVATION_CHARS:
        return value
    return {"truncated": True, "preview": encoded[:MAX_OBSERVATION_CHARS]}


def _validate_host_bash_argv(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError("Explore bash argv must be a list")
    argv = [str(value) for value in raw]
    if argv in (["ls"], ["ls", "-la"]):
        return argv
    if (
        len(argv) == 6
        and argv[:2] == ["find", "."]
        and argv[2] == "-maxdepth"
        and argv[3].isdigit()
        and 1 <= int(argv[3]) <= 5
        and argv[4:] == ["-type", "f"]
    ):
        return argv
    if (
        len(argv) == 5
        and argv[:2] == ["head", "-n"]
        and argv[2].isdigit()
        and 1 <= int(argv[2]) <= 200
        and argv[3] == "--"
        and _safe_workspace_argument(argv[4])
    ):
        return argv
    if (
        len(argv) == 5
        and argv[:3] == ["grep", "-n", "--"]
        and 0 < len(argv[3]) <= 500
        and not argv[3].startswith("-")
        and all(token not in argv[3] for token in ("$", "`", ";", "|", "&", "<", ">", "/etc/", "/proc/", "/sys/", "/dev/", "\x00", "\n", "\r"))
        and _safe_workspace_argument(argv[4])
    ):
        return argv
    raise ValueError("Explore bash argv does not match a controlled workspace command schema")


def _safe_workspace_argument(value: str) -> bool:
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    return bool(
        normalized
        and not normalized.startswith(("/", "~", "-"))
        and ".." not in parts
        and parts[0] not in {"proc", "sys", "dev", "etc"}
        and all(token not in normalized for token in ("$", "`", ";", "|", "&", "<", ">", "\x00", "\n", "\r"))
    )
