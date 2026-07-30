"""Intranet HTTP service of the dedicated embedding worker container.

Runs as its own image so only this container carries torch/transformers; the
web container stays slim and never imports them. This process is a thin
dispatcher: every job still runs in a spawned child process, so the model is
loaded on demand and its memory is returned to the OS when the batch ends.

The service listens on the compose network only and has no auth of its own; do
not publish its port outside the internal network.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.trends.embedding import current_embedding_descriptor
from app.trends.embedding_config import get_trend_embedding_settings
from app.trends.embedding_state import probe_cache_installed, report_cache_state
from app.trends.embedding_worker import (
    EmbeddingWorkerBusyError,
    EmbeddingWorkerError,
    generate_embeddings,
    is_worker_active,
    prepare_model_async,
)
from app.trends.startup import register_trend_startup

logger = logging.getLogger(__name__)


class EmbeddingWorkerHealth(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    provider: str
    model_id: str
    embedding_version: str
    dimension: int
    cache_dir: str
    cache_installed: bool
    model_version: str | None
    status: str
    worker_active: bool


class EmbedRequest(BaseModel):
    embedding_version: str = Field(min_length=1)
    texts: list[str]


class EmbedResponse(BaseModel):
    embedding_version: str
    vectors: list[list[float]]


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    register_trend_startup()
    yield


def create_worker_app() -> FastAPI:
    app = FastAPI(title="OS News Tracker Embedding Worker", lifespan=_lifespan)

    @app.get("/health")
    def health() -> EmbeddingWorkerHealth:
        descriptor = current_embedding_descriptor()
        installed, model_version, cache_dir = probe_cache_installed()
        # This container is the only one that mounts the cache, so it owns the
        # cache half of the persisted state machine.
        state = report_cache_state(installed=installed, model_version=model_version)
        return EmbeddingWorkerHealth(
            provider=descriptor.provider,
            model_id=descriptor.model_id,
            embedding_version=descriptor.embedding_version,
            dimension=descriptor.dimension,
            cache_dir=cache_dir,
            cache_installed=installed,
            model_version=model_version or state.model_version,
            status=state.status,
            worker_active=is_worker_active(),
        )

    @app.post("/model/prepare", status_code=202)
    def prepare_model() -> dict[str, str]:
        try:
            prepare_model_async()
        except EmbeddingWorkerBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "accepted"}

    @app.post("/embed")
    def embed(payload: EmbedRequest) -> EmbedResponse:
        descriptor = current_embedding_descriptor()
        if payload.embedding_version != descriptor.embedding_version:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Worker 当前向量版本为 {descriptor.embedding_version}，"
                    f"与请求的 {payload.embedding_version} 不一致；请统一后端与 Worker 的 Embedding 配置。"
                ),
            )
        try:
            vectors = generate_embeddings(payload.texts)
        except EmbeddingWorkerBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except EmbeddingWorkerError as exc:
            detail = exc.message if not exc.remedy else f"{exc.message} {exc.remedy}"
            raise HTTPException(status_code=503, detail=detail) from exc
        return EmbedResponse(embedding_version=descriptor.embedding_version, vectors=vectors)

    return app


logging.basicConfig(level=logging.INFO)
logger.info("embedding worker settings: %s", get_trend_embedding_settings().model_dump(exclude={"api_key"}))
app = create_worker_app()
