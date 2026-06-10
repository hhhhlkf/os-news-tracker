import hashlib

from app.config import get_settings


class LlmClient:
    def __init__(self, http=None, model: str | None = None):
        self._settings = get_settings()
        self._model = model or self._settings.llm_model
        self._http = http or self._make_http()
        self._cache: dict[str, str] = {}

    def _make_http(self):
        import httpx

        return httpx.Client(timeout=60.0)

    def _key(self, prompt: str) -> str:
        return hashlib.sha256(f"{self._model}:{prompt}".encode("utf-8")).hexdigest()

    def complete(self, prompt: str, *, temperature: float = 0.2) -> str:
        key = self._key(prompt)
        if key in self._cache:
            return self._cache[key]
        resp = self._http.post(
            f"{self._settings.llm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
            json={
                "model": self._model,
                "temperature": temperature,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        self._cache[key] = content
        return content
