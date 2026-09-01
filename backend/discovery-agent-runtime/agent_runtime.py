"""Fixed stdio entrypoint for the OpenHands Discovery Agent Runtime image."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from openhands.sdk import Conversation, LLM
from openhands.sdk.context.condenser import LLMSummarizingCondenser
from openhands.tools.preset.default import get_default_agent
from pydantic import SecretStr


PROTOCOL_VERSION = 1
WORKSPACE = Path("/workspace")
MAX_SOURCE = 2 * 1024 * 1024
MAX_MANIFEST = 128 * 1024
MAX_EVENT_HOSTS = 32
MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]{0,500}\]\((https?://[^\s)]+)\)", re.IGNORECASE)
HREF_PATTERN = re.compile(r"\bhref\s*=\s*[\"'](https?://[^\"']+)[\"']", re.IGNORECASE)
NAVIGATION_RESULT_PATTERN = re.compile(
    r"(?:navigated|redirected|current\s+url|final\s+url)\s*(?:to|:)?\s*(https?://\S+)",
    re.IGNORECASE,
)
STRUCTURED_URL_KEYS = {"url", "href", "link", "links", "current_url", "final_url", "redirect_url", "redirected_url"}


def _bounded_env_int(name: str, *, minimum: int, maximum: int, step: int = 1) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum or (value - minimum) % step:
        raise RuntimeError(f"{name} is outside the supported range")
    return value

for distribution in ("openhands-sdk", "openhands-tools", "openhands-agent-server"):
    actual = importlib.metadata.version(distribution)
    if actual != "1.43.1":
        raise RuntimeError(f"{distribution} version mismatch: {actual}")

_stdout = sys.stdout
_emit_lock = threading.Lock()
_request_id = ""
_event_sequence = 0
_browser_url: str | None = None
_action_urls: dict[str, str] = {}
_action_operations: dict[str, str] = {}
_turn_events: list[dict[str, object]] = []


def _emit(payload: dict[str, object]) -> None:
    with _emit_lock:
        _stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
        _stdout.flush()


def _public_url_host(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 16_384:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        return parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except (UnicodeError, ValueError):
        return None


def _observed_url_hosts(text: str, *, allow_structured_state: bool) -> list[str]:
    """Extract only explicit Browser link/navigation fields, never plain text URLs."""
    hosts: list[str] = []

    def add(value: object) -> None:
        host = _public_url_host(value)
        if host and host not in hosts:
            hosts.append(host)

    def walk(value: object, key: str | None = None, depth: int = 0) -> None:
        if depth > 6 or len(hosts) >= MAX_EVENT_HOSTS:
            return
        if isinstance(value, dict):
            for child_key, child in list(value.items())[:200]:
                normalized = str(child_key).lower()
                if normalized in STRUCTURED_URL_KEYS or normalized.endswith("_url"):
                    walk(child, normalized, depth + 1)
                elif isinstance(child, (dict, list)):
                    walk(child, normalized, depth + 1)
        elif isinstance(value, list):
            for child in value[:200]:
                walk(child, key, depth + 1)
        elif key in STRUCTURED_URL_KEYS or (key or "").endswith("_url"):
            add(value)

    bounded = text[:256_000]
    if allow_structured_state:
        try:
            walk(json.loads(bounded))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    for pattern in (MARKDOWN_LINK_PATTERN, HREF_PATTERN, NAVIGATION_RESULT_PATTERN):
        for match in pattern.finditer(bounded):
            add(match.group(1).rstrip(".,;:!?"))
            if len(hosts) >= MAX_EVENT_HOSTS:
                return hosts
    return hosts


def _safe_action_parameters(action: object, tool_name: str) -> dict[str, object]:
    if action is None:
        return {}
    data = action.model_dump(mode="python") if hasattr(action, "model_dump") else {}
    if not isinstance(data, dict):
        return {}
    tool = tool_name.lower()
    if "browser" in tool:
        host = _public_url_host(data.get("url"))
        return {"operation": type(action).__name__[:80], "target_host": host}
    if "file" in tool or "editor" in tool:
        path = data.get("path")
        return {
            "operation": str(data.get("command") or type(action).__name__)[:80],
            "path": str(path)[:240] if isinstance(path, str) else None,
        }
    if "terminal" in tool:
        command = data.get("command")
        encoded = command.encode("utf-8", errors="replace") if isinstance(command, str) else b""
        return {
            "operation": type(action).__name__[:80],
            "command_bytes": len(encoded),
            "command_sha256": hashlib.sha256(encoded).hexdigest()[:16],
        }
    return {"operation": type(action).__name__[:80], "parameter_names": sorted(str(k)[:80] for k in data)[:30]}


def _on_event(event: object) -> None:
    """Project SDK events without thoughts, raw commands, bodies, or results."""
    global _event_sequence, _browser_url
    name = type(event).__name__
    tool_name = str(getattr(event, "tool_name", "") or "")[:120]
    tool_call_id = str(getattr(event, "tool_call_id", "") or "")[:160]
    projected: dict[str, object] | None = None
    if name == "ActionEvent":
        action = getattr(event, "action", None)
        params = _safe_action_parameters(action, tool_name)
        target_host = params.get("target_host")
        raw_action_url = None
        if action is not None and hasattr(action, "model_dump"):
            raw_action_url = action.model_dump(mode="python").get("url")
        if isinstance(target_host, str) and isinstance(raw_action_url, str) and tool_call_id:
            _action_urls[tool_call_id] = raw_action_url[:16_384]
        if "browser" in tool_name.lower() and tool_call_id:
            _action_operations[tool_call_id] = str(params.get("operation") or "")
        projected = {
            "status": "started",
            "event_type": name,
            "tool": tool_name,
            "summary": str(getattr(event, "summary", "") or f"{tool_name} action")[:500],
            "parameters": params,
        }
    elif name == "ObservationEvent":
        observation = getattr(event, "observation", None)
        is_error = bool(getattr(observation, "is_error", False))
        text = str(getattr(observation, "text", "") or "")
        source_url = _browser_url
        action_url = _action_urls.pop(tool_call_id, None)
        action_operation = _action_operations.pop(tool_call_id, "")
        candidate_hosts: list[str] = []
        if "browser" in tool_name.lower() and not is_error:
            candidate_hosts = _observed_url_hosts(
                text,
                allow_structured_state=any(
                    marker in action_operation
                    for marker in ("Navigate", "GetState", "Click", "ListTabs", "SwitchTab", "GoBack")
                ),
            )
            if action_url:
                _browser_url = action_url
            source_url = _browser_url
        projected = {
            "status": "error" if is_error else "finished",
            "event_type": name,
            "tool": tool_name,
            "summary": f"{tool_name} {'failed' if is_error else 'completed'}",
            "result": {
                "result_type": type(observation).__name__[:120],
                "is_error": is_error,
                "text_bytes": len(text.encode("utf-8", errors="replace")),
                "text_sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16],
            },
            "browser_source_url": source_url,
            "browser_candidate_hosts": candidate_hosts,
        }
    elif name in {"AgentErrorEvent", "UserRejectObservation"}:
        projected = {
            "status": "error",
            "event_type": name,
            "tool": tool_name,
            "summary": f"OpenHands {name}",
        }
    if projected is None:
        return
    _event_sequence += 1
    projected["sequence"] = _event_sequence
    if len(_turn_events) < 100:
        _turn_events.append(projected)
    _emit({"type": "event", "protocol": PROTOCOL_VERSION, "request_id": _request_id, "event": projected})


context_memory = _bounded_env_int("OPENHANDS_CONTEXT_MEMORY", minimum=20, maximum=80)
tool_kb = _bounded_env_int("OPENHANDS_TOOL_KB", minimum=8, maximum=64)
max_iterations = _bounded_env_int("OPENHANDS_MAX_ITERATIONS", minimum=20, maximum=150)
token_budget = _bounded_env_int(
    "OPENHANDS_TOKEN_BUDGET",
    minimum=50_000,
    maximum=200_000,
    step=10_000,
)
llm = LLM(
    usage_id="discovery-agent",
    model=os.environ["OPENHANDS_LLM_MODEL"],
    base_url=os.environ["OPENHANDS_LLM_BASE_URL"],
    api_key=SecretStr(os.environ["OPENHANDS_LLM_API_KEY"]),
    max_message_chars=tool_kb * 1024,
)
finish_schema = {
    "type": "object",
    "properties": {"outcome_summary": {"type": "string"}},
    "required": ["outcome_summary"],
    "additionalProperties": False,
}
agent = get_default_agent(llm=llm, cli_mode=False, finish_tool_response_schema=finish_schema)
agent = agent.model_copy(update={
    "condenser": LLMSummarizingCondenser(
        llm=llm.model_copy(update={"usage_id": "condenser"}),
        max_size=context_memory,
        max_tokens=token_budget,
        keep_first=4,
    ),
})
conversation = Conversation(
    agent=agent,
    workspace=str(WORKSPACE),
    callbacks=[_on_event],
    visualizer=None,
    max_iteration_per_run=max_iterations,
)


def _read_candidate() -> tuple[str, object]:
    source = (WORKSPACE / "crawler.py").read_bytes()
    manifest = (WORKSPACE / "manifest-draft.json").read_bytes()
    if len(source) > MAX_SOURCE or len(manifest) > MAX_MANIFEST:
        raise ValueError("OpenHands candidate file exceeds its bound")
    return source.decode("utf-8"), json.loads(manifest)


_emit({"type": "ready", "protocol": PROTOCOL_VERSION, "openhands_sdk": "1.43.1"})
for line in sys.stdin.buffer:
    request: dict[str, object] = {}
    _turn_events.clear()
    try:
        request = json.loads(line)
        if request.get("type") != "turn" or request.get("protocol") != PROTOCOL_VERSION:
            raise ValueError("invalid host turn protocol")
        _request_id = str(request.get("request_id") or "")
        initial_source = request.get("initial_source")
        if initial_source is not None and not (WORKSPACE / "crawler.py").exists():
            if not isinstance(initial_source, str) or len(initial_source.encode()) > MAX_SOURCE:
                raise ValueError("initial connector source is invalid")
            (WORKSPACE / "crawler.py").write_text(initial_source, encoding="utf-8")
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or not prompt or len(prompt.encode()) > 512 * 1024:
            raise ValueError("host prompt is invalid")
        conversation.send_message(prompt)
        with contextlib.redirect_stdout(sys.stderr):
            conversation.run()
        crawler_py, manifest_draft = _read_candidate()
        response = {
            "type": "turn_result",
            "request_id": _request_id,
            "crawler_py": crawler_py,
            "manifest_draft": manifest_draft,
            "tool_events": _turn_events,
            "error": None,
        }
    except BaseException as exc:
        response = {
            "type": "turn_result",
            "request_id": str(request.get("request_id") or ""),
            "tool_events": _turn_events,
            "error": f"{type(exc).__name__}: {str(exc)[:3000]}",
        }
    _emit(response)
conversation.close()
