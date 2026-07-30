"""Embedding configuration owned by the trends module.

Kept separate from ``app.config`` so the trend fact layer can change providers,
cache location or batch limits without touching the chat LLM settings.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

TrendEmbeddingProviderName = Literal["local_qwen", "openai_compatible"]

# Bump when the event-core text template, dimension or normalization changes:
# it is part of the embedding version and invalidates stored vectors.
EVENT_CORE_TEXT_TEMPLATE_VERSION = "event_core_v2"

NORMALIZATION_L2 = "l2"


class TrendEmbeddingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TRENDS_EMBEDDING_",
        extra="ignore",
        protected_namespaces=(),
    )

    provider: TrendEmbeddingProviderName = "local_qwen"
    model_id: str = "Qwen/Qwen3-Embedding-0.6B"
    model_revision: str = "main"
    dimension: int = 1024
    batch_size: int = 16
    max_input_tokens: int = 1024
    cache_dir: str = "/var/lib/os-news-tracker/embedding-models"
    torch_threads: int = 4
    job_timeout_seconds: int = 7200
    # Intranet address of the dedicated worker container. The web process only
    # ever talks to it over HTTP and never loads a model itself.
    worker_base_url: str = "http://embedding-worker:8100"
    worker_status_timeout_seconds: float = 5.0
    worker_embed_timeout_seconds: float = 1800.0
    # Only used by the openai_compatible provider; the gateway must be verified
    # against POST /embeddings before it is selected.
    api_base_url: str | None = None
    api_key: str | None = None
    api_model: str | None = None
    api_timeout_seconds: float = 60.0


@lru_cache
def get_trend_embedding_settings() -> TrendEmbeddingSettings:
    return TrendEmbeddingSettings()
