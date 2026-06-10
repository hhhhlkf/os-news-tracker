from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from app.config import get_settings


class SearchResult(BaseModel):
    url: str
    title: str | None = None
    snippet: str | None = None


@runtime_checkable
class SearchProvider(Protocol):
    def search(self, query: str) -> list[SearchResult]: ...


class NullSearchProvider(SearchProvider):
    def search(self, query: str) -> list[SearchResult]:
        return []


def get_search_provider() -> SearchProvider:
    name = get_settings().search_provider
    if name == "internal":
        from app.search.internal_gateway import InternalGatewaySearch

        return InternalGatewaySearch()
    return NullSearchProvider()
