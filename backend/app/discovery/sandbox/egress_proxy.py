"""Minimal per-job HTTP CONNECT proxy enforcing Manifest domains and public IPs.

This process is trusted Runtime infrastructure. Connector containers have an
internal-only Docker network and can reach the internet only through this proxy.
Each upstream socket is opened against a validated resolved IP, preventing DNS
rebinding between policy evaluation and connection establishment.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from contextlib import suppress
from urllib.parse import urlsplit

from app.discovery.sandbox.network_policy import (
    NetworkPolicyError,
    domain_is_allowed,
    is_package_repository,
    normalize_hostname,
    validate_public_addresses,
    validate_proxy_target,
)


MAX_HEADER_BYTES = 64 * 1024
MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
COPY_CHUNK_BYTES = 64 * 1024
IDLE_TIMEOUT_SECONDS = 30.0
MAX_CONCURRENT_CONNECTIONS = 16
MAX_DENIED_TARGET_EVENTS = 200
_denied_target_events = 0


def _record_denied_public_target(
    method: str | None,
    target: str | None,
    allowed_domains: tuple[str, ...],
) -> None:
    """Log a bounded host-only denial without revealing paths, queries, or headers."""
    global _denied_target_events
    if not method or not target or _denied_target_events >= MAX_DENIED_TARGET_EVENTS:
        return
    try:
        if method == "CONNECT":
            parsed = urlsplit(f"//{target}")
            protocol = "https"
            port = parsed.port or 443
        else:
            parsed = urlsplit(target)
            protocol = parsed.scheme
            port = parsed.port or (443 if protocol == "https" else 80)
        if protocol not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return
        hostname = normalize_hostname(parsed.hostname)
        if port not in {80, 443} or domain_is_allowed(hostname, allowed_domains):
            return
        if is_package_repository(hostname):
            return
        try:
            ipaddress.ip_address(hostname)
            return
        except ValueError:
            pass
        validate_public_addresses(hostname, port)
    except (NetworkPolicyError, UnicodeError, ValueError):
        return
    _denied_target_events += 1
    print(
        json.dumps(
            {
                "type": "sandbox_egress_denied",
                "hostname": hostname,
                "port": port,
                "protocol": protocol,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        flush=True,
    )


async def _validated_tls_client_hello(
    reader: asyncio.StreamReader,
    expected_hostname: str,
) -> bytes:
    """Read one TLS ClientHello record and require SNI to equal the CONNECT host."""
    header = await reader.readexactly(5)
    if header[0] != 22:
        raise NetworkPolicyError("CONNECT port 443 requires a TLS ClientHello")
    record_length = int.from_bytes(header[3:5], "big")
    if record_length < 4 or record_length > 64 * 1024:
        raise NetworkPolicyError("invalid TLS ClientHello record length")
    payload = await reader.readexactly(record_length)
    if payload[0] != 1 or len(payload) < 42:
        raise NetworkPolicyError("CONNECT stream did not contain a TLS ClientHello")
    handshake_length = int.from_bytes(payload[1:4], "big")
    hello = payload[4 : 4 + handshake_length]
    if len(hello) != handshake_length or len(hello) < 38:
        raise NetworkPolicyError("truncated TLS ClientHello")
    offset = 34
    session_length = hello[offset]
    offset += 1 + session_length
    if offset + 2 > len(hello):
        raise NetworkPolicyError("truncated TLS session data")
    cipher_length = int.from_bytes(hello[offset : offset + 2], "big")
    offset += 2 + cipher_length
    if offset >= len(hello):
        raise NetworkPolicyError("truncated TLS cipher data")
    compression_length = hello[offset]
    offset += 1 + compression_length
    if offset + 2 > len(hello):
        raise NetworkPolicyError("TLS ClientHello omitted SNI")
    extensions_length = int.from_bytes(hello[offset : offset + 2], "big")
    offset += 2
    extensions_end = offset + extensions_length
    if extensions_end > len(hello):
        raise NetworkPolicyError("truncated TLS extensions")
    server_name: str | None = None
    while offset + 4 <= extensions_end:
        extension_type = int.from_bytes(hello[offset : offset + 2], "big")
        extension_length = int.from_bytes(hello[offset + 2 : offset + 4], "big")
        extension = hello[offset + 4 : offset + 4 + extension_length]
        offset += 4 + extension_length
        if extension_type != 0 or len(extension) < 5:
            continue
        names_length = int.from_bytes(extension[:2], "big")
        names = extension[2 : 2 + names_length]
        name_type = names[0]
        name_length = int.from_bytes(names[1:3], "big")
        if name_type == 0 and len(names) >= 3 + name_length:
            server_name = names[3 : 3 + name_length].decode("ascii")
            break
    if server_name is None or normalize_hostname(server_name) != expected_hostname:
        raise NetworkPolicyError("TLS SNI does not match the approved CONNECT hostname")
    return header + payload


async def _read_headers(reader: asyncio.StreamReader) -> bytes:
    headers = await reader.readuntil(b"\r\n\r\n")
    if len(headers) > MAX_HEADER_BYTES:
        raise NetworkPolicyError("proxy request headers exceed 64 KiB")
    return headers


async def _connect_pinned(addresses: tuple[str, ...], port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    last_error: OSError | None = None
    for address in addresses:
        try:
            return await asyncio.wait_for(asyncio.open_connection(address, port), timeout=10.0)
        except (OSError, TimeoutError) as exc:
            last_error = exc if isinstance(exc, OSError) else OSError(str(exc))
    raise NetworkPolicyError(f"all approved upstream addresses failed: {last_error}")


async def _copy(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
    try:
        while chunk := await asyncio.wait_for(
            source.read(COPY_CHUNK_BYTES), timeout=IDLE_TIMEOUT_SECONDS
        ):
            destination.write(chunk)
            await destination.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with suppress(Exception):
            destination.write_eof()


async def _handle_connect(
    target: str,
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    allowed_domains: tuple[str, ...],
) -> None:
    hostname, port, addresses = validate_proxy_target(target, allowed_domains=allowed_domains)
    if port != 443:
        raise NetworkPolicyError("CONNECT is restricted to TLS port 443")
    client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await client_writer.drain()
    client_hello = await asyncio.wait_for(
        _validated_tls_client_hello(client_reader, hostname),
        timeout=IDLE_TIMEOUT_SECONDS,
    )
    upstream_reader, upstream_writer = await _connect_pinned(addresses, port)
    try:
        upstream_writer.write(client_hello)
        await upstream_writer.drain()
        pumps = {
            asyncio.create_task(_copy(client_reader, upstream_writer)),
            asyncio.create_task(_copy(upstream_reader, client_writer)),
        }
        _done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
    finally:
        upstream_writer.close()
        with suppress(Exception):
            await upstream_writer.wait_closed()


async def _handle_http(
    method: str,
    target: str,
    version: str,
    raw_headers: list[bytes],
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    allowed_domains: tuple[str, ...],
) -> None:
    hostname, port, addresses = validate_proxy_target(target, allowed_domains=allowed_domains)
    parsed = urlsplit(target)
    if parsed.scheme != "http":
        raise NetworkPolicyError("HTTPS requests must use CONNECT so TLS SNI can be enforced")
    origin_target = parsed.path or "/"
    if parsed.query:
        origin_target += f"?{parsed.query}"
    filtered_headers: list[bytes] = []
    content_length = 0
    content_length_count = 0
    transfer_encoding_count = 0
    host_headers: list[str] = []
    for header in raw_headers:
        if header.startswith((b" ", b"\t")):
            raise NetworkPolicyError("obsolete folded headers are forbidden")
        name, separator, value = header.partition(b":")
        if not separator:
            raise NetworkPolicyError("malformed proxy request header")
        if name != name.strip() or any(byte <= 32 or byte >= 127 for byte in name):
            raise NetworkPolicyError("invalid whitespace or bytes in header name")
        normalized_name = name.strip().lower()
        if normalized_name == b"transfer-encoding":
            transfer_encoding_count += 1
            if value.strip().lower() != b"identity":
                raise NetworkPolicyError("chunked request bodies are not supported by the egress proxy")
        if normalized_name == b"content-length":
            content_length_count += 1
            try:
                content_length = int(value.strip())
            except ValueError as exc:
                raise NetworkPolicyError("invalid request Content-Length") from exc
        if normalized_name == b"host":
            raw_host = value.strip().decode("ascii")
            if raw_host.startswith("[") or raw_host.count(":") > 1:
                raise NetworkPolicyError("IP-literal Host headers are forbidden")
            submitted_host, separator, submitted_port = raw_host.rpartition(":")
            if separator:
                try:
                    if int(submitted_port) != port:
                        raise NetworkPolicyError("HTTP Host port does not match the target port")
                except ValueError as exc:
                    raise NetworkPolicyError("HTTP Host port is invalid") from exc
                raw_host = submitted_host
            host_headers.append(
                raw_host
            )
            continue
        if normalized_name not in {b"proxy-authorization", b"proxy-connection", b"connection"}:
            filtered_headers.append(header)
    if content_length_count > 1 or transfer_encoding_count > 1:
        raise NetworkPolicyError("duplicate framing headers are forbidden")
    if content_length_count and transfer_encoding_count:
        raise NetworkPolicyError("Content-Length with Transfer-Encoding is forbidden")
    if len(host_headers) != 1 or normalize_hostname(host_headers[0]) != hostname:
        raise NetworkPolicyError("HTTP Host header does not match the approved target hostname")
    canonical_host = hostname if port == 80 else f"{hostname}:{port}"
    filtered_headers.append(f"Host: {canonical_host}".encode("ascii"))
    if content_length < 0 or content_length > MAX_REQUEST_BODY_BYTES:
        raise NetworkPolicyError("proxy request body exceeds 4 MiB")
    body = (
        await asyncio.wait_for(
            client_reader.readexactly(content_length),
            timeout=IDLE_TIMEOUT_SECONDS,
        )
        if content_length
        else b""
    )
    request_head = (
        f"{method} {origin_target} {version}\r\n".encode("ascii")
        + b"\r\n".join(filtered_headers)
        + b"\r\nConnection: close\r\n\r\n"
    )
    upstream_reader, upstream_writer = await _connect_pinned(addresses, port)
    try:
        upstream_writer.write(request_head + body)
        await upstream_writer.drain()
        await _copy(upstream_reader, client_writer)
    finally:
        upstream_writer.close()
        with suppress(Exception):
            await upstream_writer.wait_closed()


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    allowed_domains: tuple[str, ...],
) -> None:
    method: str | None = None
    target: str | None = None
    try:
        headers = await asyncio.wait_for(
            _read_headers(client_reader), timeout=IDLE_TIMEOUT_SECONDS
        )
        lines = headers[:-4].split(b"\r\n")
        method_bytes, target_bytes, version_bytes = lines[0].split(b" ", 2)
        method = method_bytes.decode("ascii").upper()
        target = target_bytes.decode("ascii")
        version = version_bytes.decode("ascii")
        if method == "CONNECT":
            await _handle_connect(target, client_reader, client_writer, allowed_domains)
        else:
            await _handle_http(
                method,
                target,
                version,
                lines[1:],
                client_reader,
                client_writer,
                allowed_domains,
            )
    except (NetworkPolicyError, UnicodeError, ValueError, asyncio.IncompleteReadError) as exc:
        _record_denied_public_target(method, target, allowed_domains)
        body = f"egress policy denied request: {exc}\n".encode("utf-8", errors="replace")
        client_writer.write(
            b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Type: text/plain\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            + body
        )
        with suppress(Exception):
            await client_writer.drain()
    except Exception:
        client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
        with suppress(Exception):
            await client_writer.drain()
    finally:
        client_writer.close()
        with suppress(Exception):
            await client_writer.wait_closed()


async def serve() -> None:
    raw_domains = json.loads(os.environ["SANDBOX_ALLOWED_DOMAINS"])
    allowed_domains = tuple(str(domain) for domain in raw_domains)
    port = int(os.environ.get("SANDBOX_PROXY_PORT", "8080"))
    active_connections = 0

    async def limited_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal active_connections
        if active_connections >= MAX_CONCURRENT_CONNECTIONS:
            writer.write(
                b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            )
            with suppress(Exception):
                await writer.drain()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            return
        active_connections += 1
        try:
            await handle_client(reader, writer, allowed_domains)
        finally:
            active_connections -= 1

    server = await asyncio.start_server(
        limited_handler,
        host="0.0.0.0",
        port=port,
        limit=MAX_HEADER_BYTES + 1,
    )
    async with server:
        await server.serve_forever()


def main() -> int:
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
