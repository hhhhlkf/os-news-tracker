from app.extract.base import ContentExtractor
from app.extract.scrapling_extractor import ScraplingExtractor


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
