"""Backend-side client for the dedicated embedding worker container.

The web process reaches the worker only through this intranet HTTP client, so it
never imports a model runtime. Failures carry an operator-facing message plus a
remedy instead of falling back to another provider.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.trends.embedding_config import get_trend_embedding_settings

_UNREACHABLE_REMEDY = (
    "确认 embedding-worker 服务已启动（docker compose -f docker-compose.dev.yml up -d embedding-worker），"
    "或调整 TRENDS_EMBEDDING_WORKER_BASE_URL 指向正确的内网地址。"
)


class EmbeddingWorkerUnavailableError(RuntimeError):
    """The worker container could not be reached."""

    def __init__(self, message: str, *, remedy: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy or _UNREACHABLE_REMEDY


class EmbeddingWorkerRejectedError(RuntimeError):
    """The worker refused the request (busy or version mismatch)."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class EmbeddingWorkerHealth:
    provider: str
    model_id: str
    embedding_version: str
    dimension: int
    cache_dir: str
    cache_installed: bool
    model_version: str | None
    status: str
    worker_active: bool


def _base_url() -> str:
    return get_trend_embedding_settings().worker_base_url.rstrip("/")


def _detail(response: httpx.Response, fallback: str) -> str:
    try:
        body = response.json()
    except ValueError:
        return fallback
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) and detail else fallback


def fetch_worker_health() -> EmbeddingWorkerHealth:
    settings = get_trend_embedding_settings()
    try:
        with httpx.Client(timeout=settings.worker_status_timeout_seconds) as client:
            response = client.get(f"{_base_url()}/health")
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 - any transport failure means unavailable
        raise EmbeddingWorkerUnavailableError(f"无法连接 Embedding Worker：{exc}") from exc
    return EmbeddingWorkerHealth(
        provider=payload["provider"],
        model_id=payload["model_id"],
        embedding_version=payload["embedding_version"],
        dimension=payload["dimension"],
        cache_dir=payload["cache_dir"],
        cache_installed=payload["cache_installed"],
        model_version=payload.get("model_version"),
        status=payload["status"],
        worker_active=payload["worker_active"],
    )


def request_model_preparation() -> None:
    settings = get_trend_embedding_settings()
    try:
        with httpx.Client(timeout=settings.worker_status_timeout_seconds) as client:
            response = client.post(f"{_base_url()}/model/prepare")
    except Exception as exc:  # noqa: BLE001 - transport failure, not a worker verdict
        raise EmbeddingWorkerUnavailableError(f"无法连接 Embedding Worker：{exc}") from exc
    if response.status_code == 409:
        raise EmbeddingWorkerRejectedError(
            _detail(response, "Embedding Worker 正在运行，同一时刻只允许一个任务。"),
            status_code=409,
        )
    if response.status_code >= 400:
        raise EmbeddingWorkerUnavailableError(
            _detail(response, f"Embedding Worker 返回 HTTP {response.status_code}。")
        )


def request_embeddings(texts: list[str], *, embedding_version: str) -> list[list[float]]:
    settings = get_trend_embedding_settings()
    try:
        with httpx.Client(timeout=settings.worker_embed_timeout_seconds) as client:
            response = client.post(
                f"{_base_url()}/embed",
                json={"embedding_version": embedding_version, "texts": texts},
            )
    except Exception as exc:  # noqa: BLE001 - transport failure, not a worker verdict
        raise EmbeddingWorkerUnavailableError(f"无法连接 Embedding Worker：{exc}") from exc
    if response.status_code == 409:
        raise EmbeddingWorkerRejectedError(_detail(response, "Embedding Worker 拒绝了本批任务。"), status_code=409)
    if response.status_code >= 400:
        raise EmbeddingWorkerUnavailableError(
            _detail(response, f"Embedding Worker 返回 HTTP {response.status_code}。"),
            remedy="该批向量未写入；修复 Worker 报告的问题后重新触发生成。",
        )

    payload = response.json()
    if payload.get("embedding_version") != embedding_version:
        raise EmbeddingWorkerUnavailableError(
            f"Worker 返回的向量版本 {payload.get('embedding_version')} 与请求的 {embedding_version} 不一致。",
            remedy="统一后端与 Worker 的 Embedding 配置后重新生成，不要写入本批向量。",
        )
    return [[float(value) for value in vector] for vector in payload["vectors"]]
