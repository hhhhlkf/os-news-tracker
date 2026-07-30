"""Versioned vector generation contract reserved for news explanation cards.

Card generation, clustering and storylines are not implemented yet; this module
only fixes the seam they will use: one batch belongs to exactly one
``embedding_version`` and always yields 1024-dim L2 normalized vectors, or the
whole batch fails and nothing is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol, Sequence

from app.trends.embedding import current_embedding_descriptor
from app.trends.embedding_client import (
    EmbeddingWorkerRejectedError,
    EmbeddingWorkerUnavailableError,
    request_embeddings,
)
from app.trends.embedding_state import read_state

_EVENT_CORE_SEPARATOR = "；"


class EmbeddingVersionMismatchError(ValueError):
    """A batch was submitted against a vector space that is no longer current."""

    def __init__(self, requested: str, current: str) -> None:
        super().__init__(f"请求的 embedding_version {requested} 与当前版本 {current} 不一致")
        self.requested = requested
        self.current = current


class EmbeddingNotReadyError(RuntimeError):
    """The model is not usable yet; callers must retry instead of writing vectors."""

    def __init__(self, message: str, *, remedy: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy


@dataclass(frozen=True)
class CardEmbeddingRequestItem:
    card_id: str
    event_core_text: str


@dataclass(frozen=True)
class CardEmbeddingResult:
    card_id: str
    vector: list[float]
    provider: str
    model_id: str
    model_version: str | None
    embedding_version: str
    dimension: int
    normalized: bool
    generated_on: date


class CardVectorSink(Protocol):
    """Storage seam implemented once card vectors get their own table."""

    def write_card_vectors(self, results: Sequence[CardEmbeddingResult]) -> None: ...


def current_embedding_version() -> str:
    return current_embedding_descriptor().embedding_version


def build_event_core_text(
    *,
    news_actor: str,
    action: str,
    result: str,
    cause: str,
    potential_impact: str,
) -> str:
    """新闻主角 + 动作 + 结果 + 形成原因 + 后续影响；空值和“无”不拼接。"""
    parts = [news_actor, action, result, cause, potential_impact]
    return _EVENT_CORE_SEPARATOR.join(
        value
        for part in parts
        if (value := part.strip()) and value != "无"
    )


def generate_card_vectors(
    items: Sequence[CardEmbeddingRequestItem],
    *,
    embedding_version: str,
) -> list[CardEmbeddingResult]:
    """Embed one same-version batch; any failure aborts the whole batch."""
    descriptor = current_embedding_descriptor()
    if embedding_version != descriptor.embedding_version:
        raise EmbeddingVersionMismatchError(embedding_version, descriptor.embedding_version)
    if not items:
        return []

    blank = [item.card_id for item in items if not item.event_core_text.strip()]
    if blank:
        raise ValueError(f"事件核心文本为空的卡片不能生成向量：{', '.join(blank)}")

    state = read_state()
    if state.status == "failed":
        raise EmbeddingNotReadyError(
            state.error_message or "Embedding 模型处于失败状态。",
            remedy=state.remedy,
        )

    try:
        vectors = request_embeddings(
            [item.event_core_text for item in items],
            embedding_version=descriptor.embedding_version,
        )
    except EmbeddingWorkerUnavailableError as exc:
        raise EmbeddingNotReadyError(exc.message, remedy=exc.remedy) from exc
    except EmbeddingWorkerRejectedError as exc:
        raise EmbeddingNotReadyError(
            exc.message,
            remedy="Worker 拒绝了本批任务；等待当前任务结束或对齐版本配置后重试，不要写入部分结果。",
        ) from exc

    if len(vectors) != len(items):
        raise EmbeddingNotReadyError(
            f"Embedding 返回 {len(vectors)} 条向量，与输入 {len(items)} 条不一致。",
            remedy="该批向量已丢弃；请重新触发生成，不要写入部分结果。",
        )

    generated_on = datetime.now(timezone.utc).date()
    model_version = read_state().model_version
    return [
        CardEmbeddingResult(
            card_id=item.card_id,
            vector=vector,
            provider=descriptor.provider,
            model_id=descriptor.model_id,
            model_version=model_version,
            embedding_version=descriptor.embedding_version,
            dimension=descriptor.dimension,
            normalized=True,
            generated_on=generated_on,
        )
        for item, vector in zip(items, vectors)
    ]
