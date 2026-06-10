import re
from html.parser import HTMLParser

from app.extract.base import ContentExtractor
from app.schemas import ExtractedDoc


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = None
        self._in_title = False
        self._in_skip = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in ("script", "style"):
            self._in_skip = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in ("script", "style"):
            self._in_skip = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title = text
        elif not self._in_skip:
            self.chunks.append(text)


def _default_fetcher():
    from scrapling.fetchers import Fetcher

    return Fetcher


class ScraplingExtractor(ContentExtractor):
    def __init__(self, fetcher=None):
        self._fetcher = fetcher or _default_fetcher()

    def extract(self, url: str) -> ExtractedDoc:
        page = self._fetcher.fetch(url)
        html = getattr(page, "html_content", None) or getattr(page, "body", "") or ""
        parser = _TextExtractor()
        parser.feed(html)
        clean = re.sub(r"\s+", " ", " ".join(parser.chunks)).strip()
        return ExtractedDoc(url=url, title=parser.title, clean_content=clean)
