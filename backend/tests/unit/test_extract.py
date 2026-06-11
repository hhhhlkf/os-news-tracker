import sys
import types

from app.extract.base import ContentExtractor
from app.extract.scrapling_extractor import ScraplingExtractor, _default_fetcher


class _FakeFetcher:
    def __init__(self, html):
        self._html = html

    def fetch(self, url, **kw):
        class _Page:
            def __init__(self, html):
                self.html_content = html

        return _Page(self._html)


def test_scrapling_extractor_extracts_text(monkeypatch):
    html = "<html><head><title>Hi</title></head><body><article><p>Hello world body</p></article></body></html>"
    ext = ScraplingExtractor(fetcher=_FakeFetcher(html))
    doc = ext.extract("https://x.com/a")
    assert doc.url == "https://x.com/a"
    assert "Hello world body" in doc.clean_content
    assert doc.title == "Hi"


def test_extractor_is_protocol_instance():
    assert isinstance(ScraplingExtractor(fetcher=_FakeFetcher("<html></html>")), ContentExtractor)


def test_default_fetcher_factory_instantiates_fetcher(monkeypatch):
    class _FetcherImpl:
        def fetch(self, url, **kw):
            class _Page:
                html_content = "<html><body>ok</body></html>"

            return _Page()

    module = types.ModuleType("scrapling.fetchers")
    module.Fetcher = _FetcherImpl
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", module)

    fetcher = _default_fetcher()
    assert isinstance(fetcher, _FetcherImpl)


def test_scrapling_extractor_supports_get_based_fetcher():
    class _GetFetcher:
        def get(self, url, **kw):
            class _Page:
                html_content = "<html><head><title>Hi</title></head><body>From get</body></html>"

            return _Page()

    doc = ScraplingExtractor(fetcher=_GetFetcher()).extract("https://x.com/a")
    assert doc.title == "Hi"
    assert "From get" in doc.clean_content
