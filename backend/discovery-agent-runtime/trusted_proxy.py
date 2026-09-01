"""Trusted website egress proxy plus bounded OpenAI-compatible LLM relay."""

from __future__ import annotations

import asyncio
import hmac
import http.client
import ipaddress
import json
import os
import socket
import ssl
import sys
import tempfile
from contextlib import suppress
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from app.discovery.sandbox.egress_proxy import MAX_HEADER_BYTES, handle_client
from app.discovery.sandbox.network_policy import (
    NetworkPolicyError,
    is_package_repository,
    normalize_hostname,
    validate_proxy_target,
    validate_public_addresses,
)


SECRET_PATH = "/run/secrets/llm-relay.json"
ALLOWLIST_PATH = "/run/config/allowed-domains.json"
secret = json.loads(open(SECRET_PATH, encoding="utf-8").read())
proxy_port = int(os.environ.get("SANDBOX_PROXY_PORT", "8080"))
requests_used = 0
trusted_usage_total = 0
request_lock = asyncio.Lock()
relay_slots = asyncio.Semaphore(1)
MAX_VERIFY_BODY_BYTES = 2 * 1024 * 1024
MAX_VERIFY_LINKS = 2_000
URL_KEYS = {"url", "href", "src", "action", "link", "links", "current_url", "final_url", "redirect_url"}


def _bounded_secret_text(name: str, *, maximum: int) -> str:
    value = secret.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"LLM relay {name} is invalid")
    if any(character in value for character in "\r\n\x00"):
        raise ValueError(f"LLM relay {name} contains a forbidden control character")
    return value


def _provider_endpoint() -> tuple[str, str, int, str]:
    parsed = urlsplit(str(secret["base_url"]))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise ValueError("configured LLM provider base URL is not an exact supported endpoint")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname, port, "/v1/chat/completions"


PROVIDER_SCHEME, PROVIDER_HOST, PROVIDER_PORT, PROVIDER_PATH = _provider_endpoint()
PROVIDER_API_KEY = _bounded_secret_text("api_key", maximum=16_384)
PROVIDER_MODEL = _bounded_secret_text("model", maximum=512)
RELAY_TOKEN = _bounded_secret_text("relay_token", maximum=512)


def _domains() -> tuple[str, ...]:
    value = json.loads(open(ALLOWLIST_PATH, encoding="utf-8").read())
    if not isinstance(value, list) or not value or len(value) > 32:
        raise ValueError("host OpenHands allowlist is invalid")
    if not all(isinstance(item, str) for item in value):
        raise ValueError("host OpenHands allowlist contains a non-string")
    return tuple(value)


class _URLAttributeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() in {"href", "src", "action"} and isinstance(value, str):
                if len(self.values) < MAX_VERIFY_LINKS:
                    self.values.append(value)


def _candidate_is_public(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("candidate hostname is invalid")
    candidate = normalize_hostname(value)
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise ValueError("candidate IP literals are forbidden")
    if is_package_repository(candidate):
        raise ValueError("package repositories are not Discovery evidence")
    validate_public_addresses(candidate, 443)
    return candidate


def _fetch_pinned(source_url: str, allowed: tuple[str, ...]) -> tuple[int, dict[str, str], bytes]:
    parsed = urlsplit(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source URL is invalid")
    if len(source_url) > 16_384 or any(ord(char) < 32 for char in source_url):
        raise ValueError("source URL exceeds bound")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if (parsed.scheme == "https" and port != 443) or (parsed.scheme == "http" and port != 80):
        raise ValueError("source URL uses a forbidden port")
    hostname, _port, addresses = validate_proxy_target(
        f"{parsed.hostname}:{port}",
        allowed_domains=allowed,
    )
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    last_error: OSError | None = None
    for address in addresses[:8]:
        raw: socket.socket | None = None
        wrapped: socket.socket | None = None
        try:
            raw = socket.create_connection((address, port), timeout=10)
            if parsed.scheme == "https":
                wrapped = ssl.create_default_context().wrap_socket(raw, server_hostname=hostname)
                raw = None
            else:
                wrapped = raw
                raw = None
            request = (
                f"GET {target} HTTP/1.1\r\nHost: {hostname}\r\n"
                "User-Agent: OSNews-Discovery-Policy/1\r\nAccept: text/html,application/json;q=0.9,*/*;q=0.1\r\n"
                "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            wrapped.sendall(request)
            response = http.client.HTTPResponse(wrapped)
            response.begin()
            headers = {name.lower(): value for name, value in response.getheaders()}
            body = response.read(MAX_VERIFY_BODY_BYTES + 1)
            if len(body) > MAX_VERIFY_BODY_BYTES:
                raise ValueError("source verification body exceeded bound")
            return response.status, headers, body
        except OSError as exc:
            last_error = exc
        finally:
            if wrapped is not None:
                with suppress(OSError):
                    wrapped.close()
            if raw is not None:
                with suppress(OSError):
                    raw.close()
    raise OSError(f"source verification fetch failed: {type(last_error).__name__ if last_error else 'unreachable'}")


def _url_has_candidate(source_url: str, value: object, candidate: str) -> bool:
    if not isinstance(value, str) or len(value) > 16_384:
        return False
    try:
        resolved = urlsplit(urljoin(source_url, value))
        host = normalize_hostname(resolved.hostname or "")
    except (NetworkPolicyError, UnicodeError, ValueError):
        return False
    return resolved.scheme in {"http", "https"} and host == candidate


def _json_has_candidate(
    source_url: str,
    value: object,
    candidate: str,
    depth: int = 0,
    url_context: bool = False,
) -> bool:
    if depth > 6:
        return False
    if isinstance(value, dict):
        for key, child in list(value.items())[:500]:
            normalized = str(key).lower()
            if normalized in URL_KEYS or normalized.endswith("_url"):
                if _json_has_candidate(source_url, child, candidate, depth + 1, True):
                    return True
            elif isinstance(child, (dict, list)) and _json_has_candidate(
                source_url, child, candidate, depth + 1, False
            ):
                return True
    elif isinstance(value, list):
        return any(
            _json_has_candidate(source_url, child, candidate, depth + 1, url_context)
            for child in value[:500]
        )
    else:
        return url_context and _url_has_candidate(source_url, value, candidate)
    return False


def _verify_candidate(source_url: str, candidate: str) -> str:
    allowed = _domains()
    source_host = normalize_hostname(urlsplit(source_url).hostname or "")
    if source_host not in allowed:
        raise ValueError("source hostname is not currently allowed")
    candidate = _candidate_is_public(candidate)
    status, headers, body = _fetch_pinned(source_url, allowed)
    location = headers.get("location")
    if 300 <= status < 400 and _url_has_candidate(source_url, location, candidate):
        return "redirect_location"
    if not 200 <= status < 300:
        raise ValueError("source verification returned a non-success status")
    content_type = headers.get("content-type", "").lower()
    if "html" in content_type:
        parser = _URLAttributeParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        if any(_url_has_candidate(source_url, value, candidate) for value in parser.values):
            return "html_url_attribute"
    if "json" in content_type:
        try:
            value = json.loads(body)
        except (UnicodeError, json.JSONDecodeError):
            value = None
        if _json_has_candidate(source_url, value, candidate):
            return "json_url_field"
    raise ValueError("candidate was not independently present in the source response")


def _verify_and_update() -> int:
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request) != {"source_url", "candidate_host"}:
        raise ValueError("verify request shape is invalid")
    source_url = request["source_url"]
    candidate = _candidate_is_public(request["candidate_host"])
    proof = _verify_candidate(source_url, candidate)
    allowed = _domains()
    combined = list(dict.fromkeys((*allowed, candidate)))
    if len(combined) > 32:
        raise ValueError("OpenHands allowlist exceeds 32 domains")
    descriptor, temporary = tempfile.mkstemp(prefix=".allowed-", dir="/run/config")
    try:
        os.write(descriptor, json.dumps(combined, separators=(",", ":")).encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(temporary, 0o444)
    os.replace(temporary, ALLOWLIST_PATH)
    print(json.dumps({"accepted": True, "candidate_host": candidate, "proof": proof}, separators=(",", ":")))
    return 0


async def proxy_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await handle_client(reader, writer, _domains())


def _provider_addresses() -> tuple[str, ...]:
    values: list[str] = []
    for _family, _socktype, _protocol, _canonical, sockaddr in socket.getaddrinfo(
        PROVIDER_HOST, PROVIDER_PORT, type=socket.SOCK_STREAM
    ):
        address = str(sockaddr[0])
        if address not in values:
            values.append(address)
    if not values:
        raise OSError("configured LLM provider did not resolve")
    return tuple(values[:8])


def _forward(body: bytes) -> tuple[int, str, bytes]:
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("LLM body must be an object")
    payload["model"] = PROVIDER_MODEL
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    host_header = PROVIDER_HOST
    if PROVIDER_PORT != (443 if PROVIDER_SCHEME == "https" else 80):
        host_header = f"{PROVIDER_HOST}:{PROVIDER_PORT}"
    request = (
        f"POST {PROVIDER_PATH} HTTP/1.1\r\nHost: {host_header}\r\n"
        f"Authorization: Bearer {PROVIDER_API_KEY}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(encoded)}\r\nAccept: application/json, text/event-stream\r\n"
        "Connection: close\r\n\r\n"
    ).encode("utf-8") + encoded
    last_error: OSError | None = None
    for address in _provider_addresses():
        raw: socket.socket | None = None
        wrapped: socket.socket | None = None
        try:
            raw = socket.create_connection((address, PROVIDER_PORT), timeout=180)
            if PROVIDER_SCHEME == "https":
                wrapped = ssl.create_default_context().wrap_socket(raw, server_hostname=PROVIDER_HOST)
                raw = None
            else:
                wrapped = raw
                raw = None
            wrapped.sendall(request)
            response = http.client.HTTPResponse(wrapped)
            response.begin()
            result = response.read(int(secret["max_response_bytes"]) + 1)
            if len(result) > int(secret["max_response_bytes"]):
                raise ValueError("LLM relay response exceeded its bound")
            if 300 <= response.status < 400:
                raise ValueError("LLM provider redirect is forbidden")
            return response.status, response.getheader("content-type", "application/json"), result
        except OSError as exc:
            last_error = exc
        finally:
            if wrapped is not None:
                with suppress(OSError):
                    wrapped.close()
            if raw is not None:
                with suppress(OSError):
                    raw.close()
    raise OSError(f"LLM provider connection failed: {type(last_error).__name__ if last_error else 'unreachable'}")


def _response_usage(content_type: str, body: bytes) -> dict[str, int] | None:
    payloads: list[object] = []
    if "text/event-stream" in content_type.lower():
        for line in body.splitlines():
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if not data or data == b"[DONE]":
                continue
            try:
                payloads.append(json.loads(data))
            except json.JSONDecodeError:
                continue
    else:
        try:
            payloads.append(json.loads(body))
        except json.JSONDecodeError:
            return None
    for payload in reversed(payloads):
        usage = payload.get("usage") if isinstance(payload, dict) else None
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        if isinstance(total, int) and total >= 0:
            prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
            completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
            return {
                "prompt_tokens": prompt if isinstance(prompt, int) and prompt >= 0 else 0,
                "completion_tokens": completion if isinstance(completion, int) and completion >= 0 else 0,
                "total_tokens": total,
            }
    return None


async def relay_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global requests_used, trusted_usage_total
    try:
        headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        if len(headers) > MAX_HEADER_BYTES:
            raise ValueError("relay headers exceed bound")
        lines = headers[:-4].split(b"\r\n")
        method, path, _version = lines[0].decode("ascii").split(" ", 2)
        if method != "POST" or path != "/v1/chat/completions":
            raise ValueError("relay endpoint is not allowed")
        values: dict[bytes, bytes] = {}
        for raw in lines[1:]:
            name, sep, value = raw.partition(b":")
            if not sep:
                raise ValueError("malformed relay header")
            values[name.strip().lower()] = value.strip()
        expected = ("Bearer " + RELAY_TOKEN).encode()
        if not hmac.compare_digest(values.get(b"authorization", b""), expected):
            raise ValueError("relay credential rejected")
        length = int(values.get(b"content-length", b"0"))
        if length < 1 or length > int(secret["max_body_bytes"]):
            raise ValueError("relay body exceeds bound")
        async with request_lock:
            if requests_used >= int(secret["max_requests"]):
                raise ValueError("relay request budget exhausted")
            requests_used += 1
            sequence = requests_used
        body = await asyncio.wait_for(reader.readexactly(length), timeout=15)
        async with relay_slots:
            status, content_type, response = await asyncio.to_thread(_forward, body)
        response_usage = _response_usage(content_type, response)
        response_tokens = response_usage["total_tokens"] if response_usage is not None else None
        if response_tokens is not None:
            trusted_usage_total += response_tokens
        print(json.dumps({
            "type": "llm_relay",
            "sequence": sequence,
            "status": status,
            "usage_status": "exact" if response_usage is not None else "unknown",
            "prompt_tokens": response_usage["prompt_tokens"] if response_usage is not None else None,
            "completion_tokens": response_usage["completion_tokens"] if response_usage is not None else None,
            "response_tokens": response_tokens,
            "trusted_cumulative_tokens": trusted_usage_total if response_usage is not None else None,
        }), flush=True)
        writer.write(
            f"HTTP/1.1 {status} OK\r\nConnection: close\r\nContent-Type: {content_type}\r\nContent-Length: {len(response)}\r\n\r\n".encode()
            + response
        )
    except Exception as exc:
        print(json.dumps({"type": "llm_relay_error", "error_type": type(exc).__name__}), flush=True)
        body = b"LLM relay rejected request\n"
        writer.write(
            b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Type: text/plain\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
    finally:
        with suppress(Exception):
            await writer.drain()
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


async def main() -> None:
    proxy = await asyncio.start_server(proxy_handler, "0.0.0.0", proxy_port, limit=MAX_HEADER_BYTES + 1)
    relay = await asyncio.start_server(relay_handler, "0.0.0.0", 8081, limit=MAX_HEADER_BYTES + 1)
    async with proxy, relay:
        await asyncio.gather(proxy.serve_forever(), relay.serve_forever())


if __name__ == "__main__" and sys.argv[1:] == ["verify-and-update"]:
    try:
        raise SystemExit(_verify_and_update())
    except Exception as exc:
        print(json.dumps({"accepted": False, "error_type": type(exc).__name__}, separators=(",", ":")))
        raise SystemExit(2) from None
if __name__ == "__main__" and not sys.argv[1:]:
    asyncio.run(main())
elif __name__ == "__main__":
    raise SystemExit("unsupported trusted proxy command")
