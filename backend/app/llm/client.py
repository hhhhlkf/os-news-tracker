import hashlib
import json as jsonlib
import logging
import time

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

    def _key(self, prompt: str, *, temperature: float, response_format: dict | None) -> str:
        extra = jsonlib.dumps(response_format, ensure_ascii=False, sort_keys=True) if response_format else ""
        return hashlib.sha256(
            f"{self._model}:{temperature}:{extra}:{prompt}".encode("utf-8")
        ).hexdigest()

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        response_format: dict | None = None,
        timeout: float | None = None,
    ) -> str:
        key = self._key(prompt, temperature=temperature, response_format=response_format)
        if key in self._cache:
            content = self._cache[key]
            self._record_usage(
                prompt=prompt,
                content=content,
                usage={},
                model=self._model,
            )
            return content
        payload = {
            "model": self._model,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if response_format is not None:
            payload["response_format"] = response_format
        try:
            content = self._retry_post_complete(payload, timeout=timeout)
        except Exception as exc:
            if response_format is not None and self._should_retry_without_response_format(exc):
                retry_payload = dict(payload)
                retry_payload.pop("response_format", None)
                content = self._retry_post_complete(retry_payload, timeout=timeout)
            else:
                raise
        self._cache[key] = content
        return content

    def _retry_post_complete(self, payload: dict, *, timeout: float | None = None) -> str:
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                return self._post_complete(payload, timeout=timeout)
            except Exception as exc:
                last_exc = exc
                if not self._is_retryable_transient_error(exc) or attempt == 3:
                    raise
                time.sleep(1.0 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    def _post_complete(self, payload: dict, *, timeout: float | None = None) -> str:
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
        if messages and isinstance(messages[0], dict):
            prompt = str(messages[0].get("content") or "")
        self._record_usage(
            prompt=prompt,
            content=content,
            usage=response_payload.get("usage") or {},
            model=response_payload.get("model") or payload.get("model"),
        )
        return content

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
