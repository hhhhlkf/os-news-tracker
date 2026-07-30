"""Single-instance CPU embedding job runner, used inside the worker container.

No model is ever loaded in a web process: this module runs behind
``embedding_service`` and every job goes to a spawned child process that
downloads (if needed), loads, embeds a batch and then exits, which returns the
model memory to the OS. Only one job may run at a time — enforced in-process by
a lock and across processes by the ``trend_embedding_model_state`` row claim.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import threading
from dataclasses import dataclass
from multiprocessing.context import SpawnProcess
from queue import Empty
from typing import Any, Sequence

from app.trends import embedding_state
from app.trends.embedding import EmbeddingProviderError, build_embedding_provider
from app.trends.embedding_config import get_trend_embedding_settings

logger = logging.getLogger(__name__)

_ctx = mp.get_context("spawn")
_lock = threading.Lock()
_active_process: SpawnProcess | None = None

_UNEXPECTED_EXIT_REMEDY = (
    "Worker 进程异常退出，常见原因是容器内存不足（建议上限 8 GB）或依赖缺失；"
    "查看 Worker 日志后重新触发任务。"
)


class EmbeddingWorkerBusyError(RuntimeError):
    """Another embedding worker is already running."""


class EmbeddingWorkerError(RuntimeError):
    """Worker job failed; the message is meant for the operator."""

    def __init__(self, message: str, *, remedy: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy


@dataclass(frozen=True)
class EmbeddingJob:
    kind: str  # "prepare" | "embed"
    texts: tuple[str, ...] = ()


def is_worker_active() -> bool:
    with _lock:
        return _active_process is not None and _active_process.is_alive()


def prepare_model_async() -> None:
    """Kick off download/preparation in the background and return immediately."""
    _claim_or_raise("downloading")
    thread = threading.Thread(
        target=_run_prepare_job,
        name="trend-embedding-prepare",
        daemon=True,
    )
    thread.start()


def _run_prepare_job() -> None:
    """Background preparation: the failure is already persisted for the workbench."""
    try:
        _run_claimed_job(EmbeddingJob(kind="prepare"))
    except EmbeddingWorkerError as exc:
        logger.warning("embedding model preparation failed: %s", exc.message)


def generate_embeddings(texts: Sequence[str]) -> list[list[float]]:
    """Run one batch in a fresh worker process and return the vectors."""
    if not texts:
        return []
    installed, _model_version, _cache_dir = embedding_state.probe_cache_installed()
    _claim_or_raise("downloading" if not installed else "loading")
    result = _run_claimed_job(EmbeddingJob(kind="embed", texts=tuple(texts)))
    return result or []


def _claim_or_raise(initial_status: embedding_state.TrendEmbeddingStatus) -> None:
    with _lock:
        if _active_process is not None and _active_process.is_alive():
            raise EmbeddingWorkerBusyError("Embedding Worker 正在运行，同一时刻只允许一个任务。")
        if not embedding_state.try_claim_worker(initial_status):
            raise EmbeddingWorkerBusyError("Embedding Worker 正在运行，同一时刻只允许一个任务。")


def _run_claimed_job(job: EmbeddingJob) -> list[list[float]] | None:
    """Run an already-claimed job in a child process and settle the state row."""
    global _active_process

    result_queue: mp.Queue = _ctx.Queue()
    process: SpawnProcess = _ctx.Process(
        target=_worker_entry,
        args=(job.kind, list(job.texts), result_queue),
        name="trend-embedding-worker",
        daemon=True,
    )
    with _lock:
        _active_process = process
    try:
        process.start()
    except Exception as exc:  # noqa: BLE001 - spawn failure must not leave an in-flight row
        with _lock:
            _active_process = None
        embedding_state.mark_failed(f"启动 Embedding Worker 进程失败：{exc}", remedy=_UNEXPECTED_EXIT_REMEDY)
        raise EmbeddingWorkerError(str(exc), remedy=_UNEXPECTED_EXIT_REMEDY) from exc

    embedding_state.set_worker_pid(process.pid)
    try:
        # Read before joining: a large vector payload would otherwise block the
        # child's queue feeder at exit while the parent waits in join().
        payload = _read_result(process, result_queue)
        process.join(timeout=5.0)
        if payload is None:
            message = f"Embedding Worker 进程异常退出（exitcode={process.exitcode}）。"
            embedding_state.mark_failed(message, remedy=_UNEXPECTED_EXIT_REMEDY)
            raise EmbeddingWorkerError(message, remedy=_UNEXPECTED_EXIT_REMEDY)
        if not payload.get("ok"):
            raise EmbeddingWorkerError(str(payload.get("error")), remedy=payload.get("remedy"))
        return payload.get("vectors")
    finally:
        with _lock:
            if _active_process is process:
                _active_process = None
        if process.is_alive():
            process.terminate()


def _read_result(process: SpawnProcess, result_queue: mp.Queue) -> dict[str, Any] | None:
    """Wait for the child result, giving up once the child is gone."""
    deadline_polls = 5
    while True:
        try:
            payload = result_queue.get(timeout=0.2)
            return payload if isinstance(payload, dict) else None
        except Empty:
            if process.is_alive():
                continue
            # The child may have exited right after queueing its last message.
            deadline_polls -= 1
            if deadline_polls <= 0:
                return None
        except Exception:  # noqa: BLE001 - queue broken after an abnormal child exit
            return None


def _worker_entry(kind: str, texts: list[str], result_queue: mp.Queue) -> None:
    """Child-process entry: download → load → embed → exit."""
    logging.basicConfig(level=logging.INFO)
    provider = None
    try:
        settings = get_trend_embedding_settings()
        provider = build_embedding_provider(settings)

        cache = provider.probe_cache()
        if not cache.installed:
            embedding_state.mark_status("downloading", pid=os.getpid())
            logger.info("downloading embedding model %s", settings.model_id)
            model_version = provider.ensure_downloaded()
        else:
            model_version = cache.model_version

        if kind == "prepare" or not texts:
            embedding_state.mark_ready(model_version=model_version)
            result_queue.put({"ok": True, "vectors": []})
            return

        embedding_state.mark_status("loading", pid=os.getpid())
        provider.load()
        embedding_state.mark_status("processing", pid=os.getpid())
        vectors = provider.embed(texts)
        embedding_state.mark_ready(model_version=model_version)
        result_queue.put({"ok": True, "vectors": vectors})
    except EmbeddingProviderError as exc:
        embedding_state.mark_failed(exc.message, remedy=exc.remedy)
        result_queue.put({"ok": False, "error": exc.message, "remedy": exc.remedy})
    except Exception as exc:  # noqa: BLE001 - never leave the row in-flight
        message = f"Embedding Worker 任务失败：{exc}"
        embedding_state.mark_failed(message, remedy=_UNEXPECTED_EXIT_REMEDY)
        result_queue.put({"ok": False, "error": message, "remedy": _UNEXPECTED_EXIT_REMEDY})
    finally:
        if provider is not None:
            provider.release()
