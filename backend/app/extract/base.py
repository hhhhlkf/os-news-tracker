from typing import Protocol, runtime_checkable

from app.schemas import ExtractedDoc


@runtime_checkable
class ContentExtractor(Protocol):
    def extract(self, url: str) -> ExtractedDoc: ...
