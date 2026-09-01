"""Declarative Explore actions and host-owned observation projections.

Explore never accepts Agent-generated Python, parsers, or site-specific DSLs.
The model can only request a small declarative action.  The matching
``SandboxExploreSession`` is opened by the Engine once per Discovery run and
executes those actions in a retained gVisor container.  This module is the
host-side request boundary; it intentionally does not contain transport or
browser implementation code.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit


MAX_EXPLORE_ACTIONS_PER_DECISION = 6
MAX_EXPLORE_ACTION_URL_CHARS = 4_096
MAX_EXPLORE_ALLOWED_DOMAINS = 32
MAX_EXPLORE_HEADERS = 32
MAX_EXPLORE_HEADER_CHARS = 2_048
MAX_EXPLORE_BODY_BYTES = 256 * 1024
MAX_EXPLORE_BROWSER_SELECTOR_CHARS = 1_024
MAX_EXPLORE_BROWSER_VALUE_CHARS = 32_768
MAX_EXPLORE_BROWSER_RECORDS = 20
MAX_EXPLORE_WORKSPACE_PATH_CHARS = 512
MAX_EXPLORE_WORKSPACE_TEXT_BYTES = 256 * 1024
MAX_EXPLORE_BASH_CHARS = 2_048
MAX_EXPLORE_OBSERVATION_TEXT_CHARS = 32_768
MAX_EXPLORE_OBSERVATION_ITEMS = 200
MAX_AGENT_LINKS_PER_RECORD = 60
MAX_AGENT_LINKS_PER_DECISION = 60
MAX_AGENT_EXCERPT_CHARS = 4_000
MAX_AGENT_PROJECTION_BYTES = 128 * 1024

_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PAGE_ID = re.compile(r"^page_[0-9a-f]{24}$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_HTML_ATTRIBUTE_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_.:-]{0,127}$")
_SAFE_WORKSPACE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")
_BASH_FIRST_TOKEN = frozenset({
    "awk", "basename", "cat", "cut", "dirname", "find", "grep", "head",
    "jq", "ls", "printf", "sed", "sha256sum", "sort", "tail", "tr", "uniq", "wc",
})
_BASH_FORBIDDEN = re.compile(
    r"(?:^|[\s;&|(){}])(?:python(?:[0-9.]*)?|node|perl|php|ruby|lua|java|go|curl|wget|nc|ncat|socat|ssh|bash|sh)(?:\s|$)|[;&|`$<>]"
)


def normalize_allowed_domains(allowed_domains: Collection[str]) -> tuple[str, ...]:
    """Canonicalise exact public-domain allowlist entries for one session."""
    if isinstance(allowed_domains, (str, bytes)):
        raise ValueError("allowed_domains must be a collection of public domains")
    normalized: list[str] = []
    for raw_domain in allowed_domains:
        if not isinstance(raw_domain, str):
            raise ValueError("allowed_domains must contain only strings")
        domain = _normalize_domain(raw_domain)
        if domain not in normalized:
            normalized.append(domain)
    if not normalized:
        raise ValueError("allowed_domains cannot be empty")
    if len(normalized) > MAX_EXPLORE_ALLOWED_DOMAINS:
        raise ValueError(f"Explore permits at most {MAX_EXPLORE_ALLOWED_DOMAINS} allowed domains")
    return tuple(normalized)


def validate_explore_actions(raw: Any, *, allowed_domains: Collection[str]) -> list[dict[str, Any]]:
    """Validate generic, bounded Explore tool requests before the gVisor broker sees them."""
    domains = normalize_allowed_domains(allowed_domains)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("Explore actions must be an array")
    if not 1 <= len(raw) <= MAX_EXPLORE_ACTIONS_PER_DECISION:
        raise ValueError(f"Explore requires between 1 and {MAX_EXPLORE_ACTIONS_PER_DECISION} actions per decision")
    actions: list[dict[str, Any]] = []
    for index, candidate in enumerate(raw):
        if not isinstance(candidate, Mapping):
            raise ValueError(f"Explore action {index + 1} must be an object")
        tool = candidate.get("tool")
        if tool == "http":
            actions.append(_validate_http_action(candidate, domains))
        elif tool == "browser":
            actions.append(_validate_browser_action(candidate, domains))
        elif tool == "workspace":
            actions.append(_validate_workspace_action(candidate))
        else:
            raise ValueError(f"Explore action {index + 1} tool must be http, browser, or workspace")
    return actions


def explore_action_fingerprint(actions: Sequence[Mapping[str, Any]]) -> str:
    """Return a stable digest used only to recognize accidental repeat plans."""
    encoded = json.dumps(list(actions), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def explore_action_summary(actions: Sequence[Mapping[str, Any]]) -> str:
    """Provide a public-safe tool summary without exposing request payload contents."""
    counts: dict[str, int] = {}
    hosts: set[str] = set()
    for action in actions:
        tool = str(action.get("tool") or "unknown")
        operation = str(action.get("operation") or action.get("method") or "")
        label = f"{tool}:{operation}" if operation else tool
        counts[label] = counts.get(label, 0) + 1
        url = action.get("url")
        if isinstance(url, str) and (host := urlsplit(url).hostname):
            hosts.add(host.lower().rstrip("."))
    detail = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
    return f"Explore retained session executes {len(actions)} fixed actions ({detail or 'none'}) across {len(hosts)} allowlisted host(s)."


def project_explore_observation(raw: Any) -> dict[str, Any]:
    """Bound a broker response before it becomes durable Agent-visible evidence.

    The full host audit may retain bounded raw broker observations.  The Agent
    ledger gets only representative links and excerpts, so a 6-action batch
    cannot inject multi-megabyte context into a later decision.
    """
    candidates = raw.get("records") if isinstance(raw, Mapping) else None
    records: list[dict[str, Any]] = []
    remaining_links = MAX_AGENT_LINKS_PER_DECISION
    truncated = False
    if isinstance(candidates, list):
        for candidate in candidates[:MAX_EXPLORE_ACTIONS_PER_DECISION]:
            if not isinstance(candidate, Mapping):
                continue
            record, used_links = _project_record(candidate, remaining_links)
            trial = {
                "records": [*records, record],
                "record_count": len(records) + 1,
                "success_count": sum(1 for item in [*records, record] if item["error"] is None),
                "representative_link_count": MAX_AGENT_LINKS_PER_DECISION - remaining_links + used_links,
            }
            if len(json.dumps(trial, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_AGENT_PROJECTION_BYTES:
                truncated = True
                break
            remaining_links -= used_links
            records.append(record)
    return {
        "records": records,
        "record_count": len(records),
        "success_count": sum(1 for record in records if record["error"] is None),
        "representative_link_count": MAX_AGENT_LINKS_PER_DECISION - remaining_links,
        "truncated": truncated,
    }


def _validate_http_action(raw: Mapping[str, Any], domains: Collection[str]) -> dict[str, Any]:
    _assert_exact_fields(raw, {"tool", "method", "url", "headers", "body", "json"})
    method = raw.get("method")
    if not isinstance(method, str) or method.upper() not in {"GET", "HEAD", "POST"}:
        raise ValueError("HTTP Explore method must be GET, HEAD, or POST")
    normalized: dict[str, Any] = {
        "tool": "http", "method": method.upper(),
        "url": _validate_public_url(raw.get("url"), domains, field_name="HTTP Explore URL"),
        "headers": _validate_headers(raw.get("headers", {})),
    }
    body, json_body = raw.get("body"), raw.get("json")
    if body is not None and json_body is not None:
        raise ValueError("HTTP Explore action may provide body or json, not both")
    if body is not None:
        if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_EXPLORE_BODY_BYTES:
            raise ValueError("HTTP Explore body exceeds the bounded UTF-8 limit")
        normalized["body"] = body
    if json_body is not None:
        if len(json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_EXPLORE_BODY_BYTES:
            raise ValueError("HTTP Explore JSON body exceeds the bounded limit")
        normalized["json"] = json_body
    if method.upper() in {"GET", "HEAD"} and (body is not None or json_body is not None):
        raise ValueError("GET and HEAD Explore actions cannot include a request body")
    return normalized


def _validate_browser_action(raw: Mapping[str, Any], domains: Collection[str]) -> dict[str, Any]:
    _assert_exact_fields(raw, {
        "tool", "operation", "url", "page_id", "selector", "value", "key",
        "x", "y", "limit", "attribute", "evidence_role",
    })
    operation = raw.get("operation")
    supported = {
        "open", "click", "fill", "press", "scroll", "query", "records",
        "text", "attribute", "links", "network",
    }
    if not isinstance(operation, str) or operation not in supported:
        raise ValueError("browser Explore operation is unsupported")
    normalized: dict[str, Any] = {"tool": "browser", "operation": operation}
    if operation == "open":
        normalized["url"] = _validate_public_url(raw.get("url"), domains, field_name="browser Explore URL")
        return normalized
    page_id = raw.get("page_id")
    if operation == "records" and raw.get("url") is not None:
        if page_id is not None:
            raise ValueError("browser records requires exactly one of page_id or url")
        normalized["url"] = _validate_public_url(raw.get("url"), domains, field_name="browser records URL")
    else:
        if not isinstance(page_id, str) or not _PAGE_ID.fullmatch(page_id):
            if operation == "records":
                raise ValueError("browser records requires exactly one of a broker-minted page_id or URL")
            raise ValueError("browser Explore action requires a broker-minted page_id")
        normalized["page_id"] = page_id
    if operation in {"click", "fill", "press", "query", "records", "text", "attribute", "links"}:
        selector = raw.get("selector")
        if not isinstance(selector, str) or not selector or len(selector) > MAX_EXPLORE_BROWSER_SELECTOR_CHARS:
            raise ValueError("browser Explore selector is missing or exceeds its limit")
        normalized["selector"] = selector
    evidence_role = raw.get("evidence_role")
    if evidence_role is not None:
        if operation not in {"text", "attribute"} or evidence_role not in {"content", "published_at"}:
            raise ValueError(
                "browser evidence_role is allowed only for text/attribute and must be content or published_at"
            )
        normalized["evidence_role"] = evidence_role
    if operation == "attribute":
        attribute = raw.get("attribute")
        if not isinstance(attribute, str) or not _HTML_ATTRIBUTE_NAME.fullmatch(attribute):
            raise ValueError("browser attribute name is missing or invalid")
        normalized["attribute"] = attribute
    if operation == "fill":
        value = raw.get("value")
        if not isinstance(value, str) or len(value) > MAX_EXPLORE_BROWSER_VALUE_CHARS:
            raise ValueError("browser fill value is missing or exceeds its limit")
        normalized["value"] = value
    if operation == "press":
        key = raw.get("key")
        if not isinstance(key, str) or not key or len(key) > 128:
            raise ValueError("browser press key is missing or exceeds its limit")
        normalized["key"] = key
    if operation == "scroll":
        for axis in ("x", "y"):
            value = raw.get(axis, 0)
            if isinstance(value, bool) or not isinstance(value, int) or not -20_000 <= value <= 20_000:
                raise ValueError("browser scroll coordinates must be bounded integers")
            normalized[axis] = value
    if operation in {"query", "records", "links", "network"}:
        maximum = MAX_EXPLORE_BROWSER_RECORDS if operation == "records" else MAX_EXPLORE_OBSERVATION_ITEMS
        limit = raw.get("limit", maximum if operation == "records" else 50)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
            raise ValueError(f"browser {operation} limit must be between 1 and {maximum}")
        normalized["limit"] = limit
    return normalized


def _validate_workspace_action(raw: Mapping[str, Any]) -> dict[str, Any]:
    _assert_exact_fields(raw, {"tool", "operation", "path", "content", "command"})
    operation = raw.get("operation")
    if operation not in {"bash", "read", "write", "list"}:
        raise ValueError("workspace Explore operation must be bash, read, write, or list")
    normalized: dict[str, Any] = {"tool": "workspace", "operation": operation}
    if operation == "bash":
        command = raw.get("command")
        if not isinstance(command, str) or not command or len(command) > MAX_EXPLORE_BASH_CHARS:
            raise ValueError("workspace bash command is missing or exceeds its limit")
        if _BASH_FORBIDDEN.search(command):
            raise ValueError("workspace bash command contains a blocked interpreter, network client, or shell operator")
        if command.split(maxsplit=1)[0] not in _BASH_FIRST_TOKEN:
            raise ValueError("workspace bash command is not an approved bounded inspection command")
        normalized["command"] = command
        return normalized
    path = raw.get("path")
    if not isinstance(path, str) or not _SAFE_WORKSPACE_PATH.fullmatch(path) or path.startswith("../"):
        raise ValueError("workspace path must be a bounded relative path")
    normalized["path"] = path
    if operation == "write":
        content = raw.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_EXPLORE_WORKSPACE_TEXT_BYTES:
            raise ValueError("workspace write content exceeds its bounded limit")
        normalized["content"] = content
    return normalized


def _validate_headers(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping) or len(raw) > MAX_EXPLORE_HEADERS:
        raise ValueError("HTTP Explore headers must be an object with at most 32 entries")
    headers: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _HEADER_NAME.fullmatch(key):
            raise ValueError("HTTP Explore header name is invalid")
        if key.lower() in {"host", "proxy-authorization", "connection", "transfer-encoding"}:
            raise ValueError("HTTP Explore header is controlled by the sandbox")
        if not isinstance(value, str) or len(value) > MAX_EXPLORE_HEADER_CHARS or "\r" in value or "\n" in value:
            raise ValueError("HTTP Explore header value is invalid or exceeds its limit")
        headers[key] = value
    return headers


def _project_record(raw: Mapping[str, Any], remaining_links: int) -> tuple[dict[str, Any], int]:
    sequence = raw.get("sequence")
    record_id = raw.get("record_id")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        sequence = 0
    if not isinstance(record_id, str) or not re.fullmatch(r"record_[0-9a-f]{24}", record_id):
        record_id = ""
    observation = _agent_observation(raw.get("observation"))
    links = observation.get("links")
    used_links = 0
    if isinstance(links, list):
        retained = _representative_links(
            [link for link in links if isinstance(link, str)],
            limit=min(MAX_AGENT_LINKS_PER_RECORD, remaining_links),
        )
        observation["links"] = retained
        observation["links_total"] = len(links)
        used_links = len(retained)
    for excerpt_key in ("text", "text_excerpt", "body_excerpt", "stdout", "content"):
        if isinstance(observation.get(excerpt_key), str):
            observation[excerpt_key] = observation[excerpt_key][:MAX_AGENT_EXCERPT_CHARS]
    return ({
        "sequence": sequence, "record_id": record_id,
        "tool": _bounded_text(raw.get("tool"), 32),
        "operation": _bounded_text(raw.get("operation"), 32),
        "observation": observation,
        "error": _bounded_text(raw.get("error"), 2_000) or None,
    }, used_links)


def _representative_links(links: list[str], *, limit: int) -> list[str]:
    """Keep navigation, body, and tail links instead of a page-header prefix.

    HTML documents often put global navigation before article and next-page
    links.  A small, deterministic spread gives the Agent evidence from the
    entire observed document without treating any URL pattern as special.
    """
    if limit <= 0:
        return []
    unique = list(dict.fromkeys(links))
    if len(unique) <= limit:
        return unique
    head = min(4, limit)
    tail = min(4, max(0, limit - head))
    selected = set(range(head))
    if tail:
        selected.update(range(len(unique) - tail, len(unique)))
    middle_slots = limit - len(selected)
    if middle_slots:
        start, end = head, len(unique) - tail - 1
        if start <= end:
            for slot in range(middle_slots):
                position = round(start + (end - start) * (slot + 1) / (middle_slots + 1))
                selected.add(position)
    for index in range(len(unique)):
        if len(selected) >= limit:
            break
        selected.add(index)
    return [unique[index] for index in sorted(selected)[:limit]]


def _agent_observation(raw: Any) -> dict[str, Any]:
    """Select the finite broker fields that are useful for the next decision."""
    if not isinstance(raw, Mapping):
        return {}
    scalar_fields = {
        "requested_url", "final_url", "redirect_target", "content_type", "title",
        "workspace_path", "page_id", "body_sha256", "status", "body_size",
        "body_truncated", "returncode", "bytes", "path", "record_selector",
        "source_selector", "source_attribute", "evidence_role", "cleaned_text_chars",
    }
    result = {
        field: _bound_value(raw[field], depth=0)
        for field in scalar_fields
        if field in raw
    }
    for field in ("text", "text_excerpt", "stdout", "stderr"):
        if isinstance(raw.get(field), str):
            result[field] = raw[field][:MAX_AGENT_EXCERPT_CHARS]
    if isinstance(raw.get("matches"), list):
        result["matches"] = [
            item[:1_000] for item in raw["matches"][:20] if isinstance(item, str)
        ]
    if isinstance(raw.get("records"), list):
        records: list[dict[str, Any]] = []
        for item in raw["records"][:20]:
            if not isinstance(item, Mapping):
                continue
            text = item.get("text")
            links = item.get("links")
            html = item.get("html")
            records.append({
                "text": text[:1_000] if isinstance(text, str) else "",
                "html": html[:1_000] if isinstance(html, str) else "",
                "links": [link[:512] for link in links[:4] if isinstance(link, str)]
                if isinstance(links, list) else [],
            })
        result["records"] = records
    if isinstance(raw.get("entries"), list):
        result["entries"] = [item[:256] for item in raw["entries"][:50] if isinstance(item, str)]
    if isinstance(raw.get("network"), list):
        network: list[dict[str, Any]] = []
        for item in raw["network"][-20:]:
            if not isinstance(item, Mapping):
                continue
            network.append({
                key: _bound_value(value, depth=0)
                for key, value in item.items()
                if key in {"url", "method", "status"}
            })
        result["network"] = network
    if isinstance(raw.get("links"), list):
        result["links"] = [item[:1_024] for item in raw["links"] if isinstance(item, str)]
    return result


def _bound_value(value: Any, *, depth: int) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, str):
        return value[:MAX_EXPLORE_OBSERVATION_TEXT_CHARS]
    if value is None or isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        return {key: _bound_value(item, depth=depth + 1) for key, item in list(value.items())[:MAX_EXPLORE_OBSERVATION_ITEMS] if isinstance(key, str) and len(key) <= 128}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_bound_value(item, depth=depth + 1) for item in value[:MAX_EXPLORE_OBSERVATION_ITEMS]]
    return None


def _bounded_text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _assert_exact_fields(raw: Mapping[str, Any], permitted: set[str]) -> None:
    unexpected = set(raw) - permitted
    if unexpected:
        raise ValueError(f"Explore action has unsupported fields: {', '.join(sorted(map(str, unexpected)))}")


def _normalize_domain(raw_domain: str) -> str:
    value = raw_domain.strip().rstrip(".").lower()
    if not value or len(value) > 253:
        raise ValueError("allowed_domains contains an invalid domain")
    try:
        value = value.encode("idna").decode("ascii")
        ipaddress.ip_address(value)
    except ValueError:
        pass
    except UnicodeError as exc:
        raise ValueError("allowed_domains contains an invalid domain") from exc
    else:
        raise ValueError("allowed_domains must not contain an IP address")
    labels = value.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise ValueError("allowed_domains contains an invalid public domain")
    return value


def _validate_public_url(raw_url: Any, allowed_domains: Collection[str], *, field_name: str) -> str:
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > MAX_EXPLORE_ACTION_URL_CHARS:
        raise ValueError(f"{field_name} must be an absolute URL within its limit")
    if raw_url != raw_url.strip() or any(character.isspace() for character in raw_url):
        raise ValueError(f"{field_name} must not contain whitespace")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} has an invalid port") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field_name} must be absolute public HTTP(S)")
    if parsed.username or parsed.password or (port is not None and not 1 <= port <= 65535):
        raise ValueError(f"{field_name} is not public")
    host = _normalize_domain(parsed.hostname)
    if host not in allowed_domains:
        raise ValueError(f"{field_name} host is outside allowed_domains: {host}")
    return raw_url
