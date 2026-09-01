import hashlib
import json as jsonlib
import logging
import time
from threading import Event

from app.config import get_settings

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    """Rough mixed CN/EN estimate when the gateway omits usage."""
    return max(1, (len(text or "") + 3) // 4)


class LlmClient:
    def __init__(self, http=None, model: str | None = None):
        self._settings = get_settings()
        self._model = model or self._settings.llm_model
        self._http = http or self._make_http()
        self._cache: dict[str, str] = {}

    def _make_http(self):
        import httpx

        return httpx.Client(timeout=60.0)

    def _key(
        self,
        prompt: str,
        *,
        temperature: float,
        response_format: dict | None,
        max_tokens: int | None,
        thinking: bool | None,
    ) -> str:
        extra = jsonlib.dumps(
            {
                "response_format": response_format,
                "max_tokens": max_tokens,
                "thinking": thinking,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(
            f"{self._model}:{temperature}:{extra}:{prompt}".encode("utf-8")
        ).hexdigest()

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        response_format: dict | None = None,
        max_tokens: int | None = None,
        thinking: bool | None = None,
        timeout: float | None = None,
        _cancel_event: Event | None = None,
    ) -> str:
        return self.complete_messages(
            [{"role": "user", "content": prompt}],
            temperature=temperature,
            response_format=response_format,
            max_tokens=max_tokens,
            thinking=thinking,
            timeout=timeout,
            _cancel_event=_cancel_event,
        )

    def complete_messages(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.2,
        response_format: dict | None = None,
        max_tokens: int | None = None,
        thinking: bool | None = None,
        timeout: float | None = None,
        _cancel_event: Event | None = None,
    ) -> str:
        """Complete one stateful chat turn while callers own conversation history."""
        self._raise_if_cancelled(_cancel_event)
        normalized_messages = [
            {"role": str(message["role"]), "content": str(message["content"])}
            for message in messages
        ]
        conversation = jsonlib.dumps(normalized_messages, ensure_ascii=False, separators=(",", ":"))
        key = self._key(
            conversation,
            temperature=temperature,
            response_format=response_format,
            max_tokens=max_tokens,
            thinking=thinking,
        )
        if key in self._cache:
            content = self._cache[key]
            self._raise_if_cancelled(_cancel_event)
            self._record_usage(
                prompt=conversation,
                content=content,
                usage={},
                model=self._model,
            )
            return content
        payload = {
            "model": self._model,
            "temperature": temperature,
            "messages": normalized_messages,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload["max_tokens"] = max(1, int(max_tokens))
        if thinking is not None:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        deadline = time.monotonic() + timeout if timeout is not None else None
        candidate_payload = payload
        for _ in range(3):
            try:
                content = self._retry_post_complete(
                    candidate_payload,
                    deadline=deadline,
                    cancel_event=_cancel_event,
                )
                break
            except Exception as exc:
                retry_payload = dict(candidate_payload)
                if (
                    "response_format" in retry_payload
                    and self._should_retry_without_response_format(exc)
                ):
                    retry_payload.pop("response_format", None)
                elif "thinking" in retry_payload and self._should_retry_without_thinking(exc):
                    retry_payload.pop("thinking", None)
                else:
                    raise
                candidate_payload = retry_payload
        else:  # pragma: no cover - each fallback either returns or raises
            raise RuntimeError("LLM compatibility fallbacks were exhausted")
        self._raise_if_cancelled(_cancel_event)
        self._cache[key] = content
        return content

    def complete_tool_messages(
        self,
        messages: list[dict],
        *,
        tools: list[dict],
        temperature: float = 0.1,
        thinking: bool | None = None,
        timeout: float | None = None,
        _cancel_event: Event | None = None,
    ) -> dict:
        """Return one native assistant message containing a function tool call."""
        self._raise_if_cancelled(_cancel_event)
        payload = {
            "model": self._model,
            "temperature": temperature,
            "messages": messages,
            "tools": tools,
        }
        if thinking is not None:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        deadline = time.monotonic() + timeout if timeout is not None else None
        last_exc: Exception | None = None
        used_thinking_fallback = False
        for attempt in range(4):
            self._raise_if_cancelled(_cancel_event)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("LLM tool call exceeded its total deadline") from last_exc
            try:
                return self._post_tool_complete(
                    payload,
                    timeout=remaining,
                    cancel_event=_cancel_event,
                )
            except Exception as exc:
                last_exc = exc
                if (
                    not used_thinking_fallback
                    and "thinking" in payload
                    and self._should_retry_without_thinking(exc)
                ):
                    payload = dict(payload)
                    payload.pop("thinking", None)
                    used_thinking_fallback = True
                    continue
                if not self._is_retryable_transient_error(exc) or attempt == 3:
                    raise
                delay = min(float(attempt + 1), max(0.0, remaining or float(attempt + 1)))
                if delay <= 0:
                    raise TimeoutError("LLM tool call exceeded its total deadline") from exc
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def _retry_post_complete(
        self,
        payload: dict,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> str:
        if deadline is None and timeout is not None:
            deadline = time.monotonic() + timeout
        last_exc: Exception | None = None
        for attempt in range(4):
            self._raise_if_cancelled(cancel_event)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("LLM call exceeded its total deadline") from last_exc
            try:
                content = self._post_complete(payload, timeout=remaining, cancel_event=cancel_event)
                self._raise_if_cancelled(cancel_event)
                return content
            except Exception as exc:
                last_exc = exc
                if not self._is_retryable_transient_error(exc) or attempt == 3:
                    raise
                delay = 1.0 * (attempt + 1)
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                if delay <= 0:
                    raise TimeoutError("LLM call exceeded its total deadline") from exc
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _raise_if_cancelled(cancel_event: Event | None = None) -> None:
        from app.discovery.cancel import is_cancelled

        if (cancel_event is not None and cancel_event.is_set()) or is_cancelled():
            raise RuntimeError("LLM call cancelled")

    def cancel_inflight(self) -> None:
        """Cancellation is per-call; never close the shared transport."""

    def _post_complete(
        self,
        payload: dict,
        *,
        timeout: float | None = None,
        cancel_event: Event | None = None,
    ) -> str:
        import httpx

        try:
            request_kwargs = {
                "headers": {"Authorization": f"Bearer {self._settings.llm_api_key}"},
                "json": payload,
            }
            if timeout is not None:
                request_kwargs["timeout"] = timeout
            resp = self._http.post(
                f"{self._settings.llm_base_url}/chat/completions",
                **request_kwargs,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = self._response_error_text(getattr(exc, "response", None))
            if detail:
                raise RuntimeError(f"{exc} · response_body={detail}") from exc
            raise
        response_payload = resp.json()
        content = response_payload["choices"][0]["message"]["content"]
        prompt = ""
        messages = payload.get("messages") or []
        if messages:
            prompt = "\n".join(
                str(message.get("content") or "")
                for message in messages
                if isinstance(message, dict)
            )
        self._raise_if_cancelled(cancel_event)
        self._record_usage(
            prompt=prompt,
            content=content,
            usage=response_payload.get("usage") or {},
            model=response_payload.get("model") or payload.get("model"),
        )
        return content

    def _post_tool_complete(
        self,
        payload: dict,
        *,
        timeout: float | None = None,
        cancel_event: Event | None = None,
    ) -> dict:
        import httpx

        try:
            request_kwargs = {
                "headers": {"Authorization": f"Bearer {self._settings.llm_api_key}"},
                "json": payload,
            }
            if timeout is not None:
                request_kwargs["timeout"] = timeout
            response = self._http.post(
                f"{self._settings.llm_base_url}/chat/completions",
                **request_kwargs,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = self._response_error_text(getattr(exc, "response", None))
            if detail:
                raise RuntimeError(f"{exc} · response_body={detail}") from exc
            raise
        response_payload = response.json()
        raw_message = response_payload["choices"][0]["message"]
        if not isinstance(raw_message, dict):
            raise ValueError("LLM tool response message must be an object")
        raw_calls = raw_message.get("tool_calls") or []
        tool_calls: list[dict] = []
        if isinstance(raw_calls, list):
            for raw_call in raw_calls:
                function = raw_call.get("function") if isinstance(raw_call, dict) else None
                if not isinstance(function, dict):
                    continue
                tool_calls.append({
                    "id": str(raw_call.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or "{}"),
                    },
                })
        # Never retain gateway-specific reasoning fields in the Agent conversation.
        message = {
            "role": "assistant",
            "content": str(raw_message.get("content") or ""),
            "tool_calls": tool_calls,
        }
        self._raise_if_cancelled(cancel_event)
        prompt = "\n".join(
            str(item.get("content") or "")
            for item in payload.get("messages") or []
            if isinstance(item, dict)
        )
        completion = jsonlib.dumps(
            message.get("tool_calls") or message.get("content") or "",
            ensure_ascii=False,
            default=str,
        )
        self._record_usage(
            prompt=prompt,
            content=completion,
            usage=response_payload.get("usage") or {},
            model=response_payload.get("model") or payload.get("model"),
        )
        return message

    def _record_usage(
        self,
        *,
        prompt: str,
        content: str,
        usage: dict,
        model: str | None,
    ) -> None:
        from app.llm.usage import record_usage

        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        if not total_tokens and not prompt_tokens and not completion_tokens:
            prompt_tokens = _estimate_tokens(prompt)
            completion_tokens = _estimate_tokens(content)
            total_tokens = prompt_tokens + completion_tokens
            logger.info(
                "LLM usage missing from response; estimated prompt=%s completion=%s",
                prompt_tokens,
                completion_tokens,
            )
        record_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            model=model,
        )

    def _should_retry_without_response_format(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "response_format" in text and (
            "unavailable" in text
            or "unsupported" in text
            or "invalid_request_error" in text
        )

    def _should_retry_without_thinking(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "thinking" in text and any(token in text for token in (
            "unavailable",
            "unsupported",
            "unrecognized",
            "unknown field",
            "unknown parameter",
            "unexpected",
            "invalid_request_error",
            "extra fields",
            "additional properties",
            "not permitted",
        ))

    def _is_retryable_transient_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(token in text for token in (
            " 500",
            " 502",
            " 503",
            " 504",
            "internalservererror",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
            "api connection error",
            "timeout",
        ))

    def _response_error_text(self, response) -> str | None:
        if response is None:
            return None
        text = getattr(response, "text", None)
        if not text:
            return None
        text = str(text).strip()
        return text[:500] + ("...[truncated]" if len(text) > 500 else "")
