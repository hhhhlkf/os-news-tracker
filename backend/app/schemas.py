from datetime import datetime
from pydantic import BaseModel, Field
from app.enums import InfoType, Importance, EntityType


class RawItem(BaseModel):
    source_id: int
    title: str
    url: str
    raw_content: str | None = None
    published_at: datetime | None = None


class ExtractedDoc(BaseModel):
    url: str
    title: str | None = None
    clean_content: str
    published_at: datetime | None = None


class EntityRef(BaseModel):
    type: EntityType
    name: str
    role: str | None = None


class EnrichedFields(BaseModel):
    title_tldr: str
    summary: str
    key_points: list[str] = Field(default_factory=list)
    info_type: InfoType
    importance: Importance
    why_it_matters: str
    main_category: str
    sub_tags: list[str] = Field(default_factory=list)
    entities: list[EntityRef] = Field(default_factory=list)
    confidence: float = 0.0


class NormalizedItem(BaseModel):
    source_id: int
    title: str
    url: str
    canonical_url: str
    clean_content: str
    published_at: datetime | None = None
