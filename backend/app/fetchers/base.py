from typing import Protocol, runtime_checkable

from app.models import Source
from app.schemas import RawItem


@runtime_checkable
class Fetcher(Protocol):
    def fetch(self, source: Source) -> list[RawItem]: ...
