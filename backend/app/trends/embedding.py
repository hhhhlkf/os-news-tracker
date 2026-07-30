"""Replaceable embedding providers for the trend fact layer.

Business code only depends on :class:`EmbeddingProvider`: a batch text-in /
normalized-vector-out contract plus a descriptor that pins the vector space.
The default provider is the local ``Qwen/Qwen3-Embedding-0.6B`` model; an
OpenAI-compatible provider is kept as an explicit opt-in extension point.

Heavy runtime dependencies (torch/transformers/huggingface_hub) are imported
lazily inside the provider methods so importing this module from the web
process never pulls a model into memory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from app.trends.embedding_config import (
    EVENT_CORE_TEXT_TEMPLATE_VERSION,
    NORMALIZATION_L2,
    TrendEmbeddingSettings,
    get_trend_embedding_settings,
)

_WEIGHT_FILENAMES = ("model.safetensors", "pytorch_model.bin", "model.safetensors.index.json")


class EmbeddingProviderError(RuntimeError):
    """Embedding failure that must be shown to an operator, never swallowed."""

    def __init__(self, message: str, *, remedy: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy


@dataclass(frozen=True)
class EmbeddingDescriptor:
    """Identity of the vector space a batch of vectors belongs to."""

    provider: str
    model_id: str
    model_revision: str
    dimension: int
    normalization: str
    embedding_version: str


@dataclass(frozen=True)
class EmbeddingCacheState:
    installed: bool
    cache_dir: str
    model_version: str | None


class EmbeddingProvider(Protocol):
    """Batch embedding contract shared by local and remote providers."""

    def describe(self) -> EmbeddingDescriptor: ...

    def probe_cache(self) -> EmbeddingCacheState:
        """Report availability without importing runtime deps or loading weights."""

    def ensure_downloaded(self) -> str | None:
        """Materialize model weights into the persistent cache; return model version."""

    def load(self) -> None:
        """Load the model into this process. Never call from the web process."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return same-order, same-length L2 normalized vectors."""

    def release(self) -> None:
        """Drop references to loaded weights."""


def build_embedding_version(settings: TrendEmbeddingSettings) -> str:
    """Stable id of the current vector space; mixing versions is forbidden."""
    model_slug = settings.model_id.split("/")[-1].lower()
    return f"{settings.provider}:{model_slug}:{settings.dimension}:{EVENT_CORE_TEXT_TEMPLATE_VERSION}"


def _descriptor(settings: TrendEmbeddingSettings) -> EmbeddingDescriptor:
    return EmbeddingDescriptor(
        provider=settings.provider,
        model_id=settings.model_id,
        model_revision=settings.model_revision,
        dimension=settings.dimension,
        normalization=NORMALIZATION_L2,
        embedding_version=build_embedding_version(settings),
    )


class LocalQwenEmbeddingProvider:
    """Local CPU FP32 Qwen embedding provider running inside the worker process."""

    def __init__(self, settings: TrendEmbeddingSettings) -> None:
        self._settings = settings
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._snapshot_dir: str | None = None

    def describe(self) -> EmbeddingDescriptor:
        return _descriptor(self._settings)

    def probe_cache(self) -> EmbeddingCacheState:
        cache_dir = self._settings.cache_dir
        snapshot_dir, revision = self._resolve_cached_snapshot()
        return EmbeddingCacheState(
            installed=snapshot_dir is not None,
            cache_dir=cache_dir,
            model_version=revision,
        )

    def ensure_downloaded(self) -> str | None:
        cached = self.probe_cache()
        if cached.installed:
            self._snapshot_dir, _ = self._resolve_cached_snapshot()
            return cached.model_version

        snapshot_download = self._import_snapshot_download()
        os.makedirs(self._settings.cache_dir, exist_ok=True)
        try:
            snapshot_dir = snapshot_download(
                repo_id=self._settings.model_id,
                revision=self._settings.model_revision,
                cache_dir=self._settings.cache_dir,
                allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "*.py"],
            )
        except Exception as exc:  # noqa: BLE001 - any hub failure must stay actionable
            raise EmbeddingProviderError(
                f"下载模型 {self._settings.model_id} 失败：{exc}",
                remedy=(
                    "确认 Worker 容器可访问 Hugging Face（或配置 HF_ENDPOINT 镜像），"
                    f"并确保缓存目录 {self._settings.cache_dir} 可写且剩余空间大于 2 GB，然后重新点击“准备模型”。"
                ),
            ) from exc

        self._snapshot_dir = snapshot_dir
        _, revision = self._resolve_cached_snapshot()
        return revision or os.path.basename(snapshot_dir)

    def load(self) -> None:
        if self._model is not None:
            return
        snapshot_dir = self._snapshot_dir
        if snapshot_dir is None:
            snapshot_dir, _ = self._resolve_cached_snapshot()
        if snapshot_dir is None:
            raise EmbeddingProviderError(
                f"模型 {self._settings.model_id} 未在缓存目录中找到。",
                remedy="先在趋势工作台点击“准备模型”完成下载，再重试向量生成。",
            )

        torch, auto_model, auto_tokenizer = self._import_runtime()
        if self._settings.torch_threads > 0:
            torch.set_num_threads(self._settings.torch_threads)
        try:
            self._tokenizer = auto_tokenizer.from_pretrained(snapshot_dir, padding_side="left")
            self._model = auto_model.from_pretrained(
                snapshot_dir,
                torch_dtype=torch.float32,
            ).eval()
        except Exception as exc:  # noqa: BLE001 - surface load failure with remedy
            raise EmbeddingProviderError(
                f"加载模型 {self._settings.model_id} 失败：{exc}",
                remedy=(
                    "检查缓存文件是否完整（可删除缓存目录后重新准备模型），"
                    "并确认 Worker 容器内存上限不低于 8 GB。"
                ),
            ) from exc
        self._snapshot_dir = snapshot_dir

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model is None or self._tokenizer is None:
            raise EmbeddingProviderError(
                "模型尚未加载。",
                remedy="向量生成必须先调用 load()；请重新触发一次批量任务。",
            )

        torch, _, _ = self._import_runtime()
        vectors: list[list[float]] = []
        batch_size = max(1, self._settings.batch_size)
        for start in range(0, len(texts), batch_size):
            batch = list(texts[start : start + batch_size])
            encoded = self._tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=self._settings.max_input_tokens,
                return_tensors="pt",
            )
            with torch.no_grad():
                output = self._model(**encoded)
            pooled = _last_token_pool(torch, output.last_hidden_state, encoded["attention_mask"])
            normalized = torch.nn.functional.normalize(pooled, p=2, dim=1)
            vectors.extend([[float(value) for value in row] for row in normalized.tolist()])

        _validate_dimension(vectors, self._settings.dimension)
        return vectors

    def release(self) -> None:
        self._model = None
        self._tokenizer = None

    def _repo_cache_root(self) -> str:
        folder = "models--" + self._settings.model_id.replace("/", "--")
        return os.path.join(self._settings.cache_dir, folder)

    def _resolve_cached_snapshot(self) -> tuple[str | None, str | None]:
        """Locate a complete snapshot in the Hugging Face cache layout, no imports."""
        repo_root = self._repo_cache_root()
        snapshots_root = os.path.join(repo_root, "snapshots")
        if not os.path.isdir(snapshots_root):
            return None, None

        candidates: list[str] = []
        ref_path = os.path.join(repo_root, "refs", self._settings.model_revision)
        if os.path.isfile(ref_path):
            try:
                with open(ref_path, encoding="utf-8") as handle:
                    candidates.append(handle.read().strip())
            except OSError:
                pass
        candidates.extend(sorted(os.listdir(snapshots_root)))

        for revision in candidates:
            if not revision:
                continue
            snapshot_dir = os.path.join(snapshots_root, revision)
            if _is_complete_snapshot(snapshot_dir):
                return snapshot_dir, revision
        return None, None

    @staticmethod
    def _import_snapshot_download() -> Any:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise EmbeddingProviderError(
                "缺少本地 Embedding 下载依赖 huggingface_hub。",
                remedy='在 Embedding Worker 镜像中安装 `pip install "os-news-tracker[embedding]"` 后重试。',
            ) from exc
        return snapshot_download

    @staticmethod
    def _import_runtime() -> tuple[Any, Any, Any]:
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise EmbeddingProviderError(
                "缺少本地 Embedding 运行依赖 torch / transformers。",
                remedy='在 Embedding Worker 镜像中安装 `pip install "os-news-tracker[embedding]"` 后重试。',
            ) from exc
        return torch, AutoModel, AutoTokenizer


class OpenAiCompatibleEmbeddingProvider:
    """Extension point for a gateway that exposes POST /embeddings.

    Disabled unless explicitly configured; the module never falls back to it
    when the local model is unavailable.
    """

    def __init__(self, settings: TrendEmbeddingSettings) -> None:
        self._settings = settings

    def describe(self) -> EmbeddingDescriptor:
        return _descriptor(self._settings)

    def probe_cache(self) -> EmbeddingCacheState:
        return EmbeddingCacheState(
            installed=self._is_configured(),
            cache_dir="",
            model_version=self._settings.api_model,
        )

    def ensure_downloaded(self) -> str | None:
        self._require_configured()
        return self._settings.api_model

    def load(self) -> None:
        self._require_configured()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self._require_configured()
        if not texts:
            return []

        import httpx

        headers = {"Content-Type": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        base_url = (self._settings.api_base_url or "").rstrip("/")

        vectors: list[list[float]] = []
        batch_size = max(1, self._settings.batch_size)
        try:
            with httpx.Client(timeout=self._settings.api_timeout_seconds) as client:
                for start in range(0, len(texts), batch_size):
                    batch = list(texts[start : start + batch_size])
                    response = client.post(
                        f"{base_url}/embeddings",
                        headers=headers,
                        json={"model": self._settings.api_model, "input": batch},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    rows = payload.get("data") or []
                    if len(rows) != len(batch):
                        raise EmbeddingProviderError(
                            f"Embedding 网关返回 {len(rows)} 条向量，与输入 {len(batch)} 条不一致。",
                            remedy="要求网关按 OpenAI Embeddings 契约同序返回等量向量后再启用该提供方。",
                        )
                    vectors.extend([_l2_normalize([float(v) for v in row["embedding"]]) for row in rows])
        except EmbeddingProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - network/contract failures stay actionable
            raise EmbeddingProviderError(
                f"调用 Embedding 网关失败：{exc}",
                remedy="确认网关已提供标准 POST /embeddings 接口与可用模型 ID，或切回本地 Qwen 提供方。",
            ) from exc

        _validate_dimension(vectors, self._settings.dimension)
        return vectors

    def release(self) -> None:
        return None

    def _is_configured(self) -> bool:
        return bool(self._settings.api_base_url and self._settings.api_model)

    def _require_configured(self) -> None:
        if self._is_configured():
            return
        raise EmbeddingProviderError(
            "openai_compatible 提供方未配置。",
            remedy=(
                "先按调研文档验证网关的 POST /embeddings 契约，再设置 "
                "TRENDS_EMBEDDING_API_BASE_URL 与 TRENDS_EMBEDDING_API_MODEL。"
            ),
        )


def build_embedding_provider(settings: TrendEmbeddingSettings | None = None) -> EmbeddingProvider:
    settings = settings or get_trend_embedding_settings()
    if settings.provider == "local_qwen":
        return LocalQwenEmbeddingProvider(settings)
    if settings.provider == "openai_compatible":
        return OpenAiCompatibleEmbeddingProvider(settings)
    raise EmbeddingProviderError(
        f"未知的 Embedding 提供方 {settings.provider}。",
        remedy="TRENDS_EMBEDDING_PROVIDER 只能是 local_qwen 或 openai_compatible。",
    )


def current_embedding_descriptor() -> EmbeddingDescriptor:
    return _descriptor(get_trend_embedding_settings())


def _is_complete_snapshot(snapshot_dir: str) -> bool:
    if not os.path.isdir(snapshot_dir):
        return False
    if not os.path.exists(os.path.join(snapshot_dir, "config.json")):
        return False
    # os.path.exists follows the blob symlinks, so a partial download fails here.
    return any(os.path.exists(os.path.join(snapshot_dir, name)) for name in _WEIGHT_FILENAMES)


def _last_token_pool(torch: Any, last_hidden_states: Any, attention_mask: Any) -> Any:
    left_padding = attention_mask[:, -1].sum() == attention_mask.shape[0]
    if left_padding:
        return last_hidden_states[:, -1]
    sequence_lengths = attention_mask.sum(dim=1) - 1
    batch_size = last_hidden_states.shape[0]
    return last_hidden_states[torch.arange(batch_size, device=last_hidden_states.device), sequence_lengths]


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = sum(value * value for value in vector) ** 0.5
    if norm == 0:
        raise EmbeddingProviderError(
            "Embedding 返回了零向量。",
            remedy="零向量无法用于余弦相似度；请检查提供方输出后重试，不要写入该批向量。",
        )
    return [value / norm for value in vector]


def _validate_dimension(vectors: Sequence[Sequence[float]], expected: int) -> None:
    for vector in vectors:
        if len(vector) != expected:
            raise EmbeddingProviderError(
                f"Embedding 维度为 {len(vector)}，期望 {expected}。",
                remedy="维度不一致意味着向量空间已变化；请更新 TRENDS_EMBEDDING_DIMENSION 并重建受影响向量。",
            )
