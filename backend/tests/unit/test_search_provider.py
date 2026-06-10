from app.search.base import NullSearchProvider, SearchProvider, get_search_provider


def test_null_provider_returns_empty():
    p = NullSearchProvider()
    assert p.search("anything") == []
    assert isinstance(p, SearchProvider)


def test_get_search_provider_defaults_to_null(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER", "none")
    from app.config import get_settings

    get_settings.cache_clear()
    p = get_search_provider()
    assert isinstance(p, NullSearchProvider)
