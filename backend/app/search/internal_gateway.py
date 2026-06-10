import httpx

from app.config import get_settings
from app.search.base import SearchProvider, SearchResult


class InternalGatewaySearch(SearchProvider):
    """Placeholder for the internal LLM-gateway web search."""

    def __init__(self, client: httpx.Client | None = None):
        self._settings = get_settings()
        self._client = client or httpx.Client(timeout=15.0)

    def search(self, query: str) -> list[SearchResult]:
        resp = self._client.post(
            f"{self._settings.llm_base_url}/web_search",
            headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
            json={"query": query},
        )
        resp.raise_for_status()
        data = resp.json()
        return [SearchResult(**r) for r in data.get("results", [])]
