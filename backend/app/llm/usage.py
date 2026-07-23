"""Exact LLM token accounting for query and discovery runs."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any, Iterator

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)


@dataclass
class UsageScope:
    context_type: str
    trigger_type: str | None = None
    stage: str = "llm"
    discovery_run_id: int | None = None
    crawl_method_run_id: int | None = None
    morning_crawl_run_id: int | None = None
    morning_crawl_run_method_id: int | None = None
    method_id: int | None = None
    total_tokens: int = 0


_current_scope: ContextVar[UsageScope | None] = ContextVar("llm_usage_scope", default=None)


def activate_usage_scope(scope: UsageScope) -> Token[UsageScope | None]:
    return _current_scope.set(scope)


def deactivate_usage_scope(token: Token[UsageScope | None]) -> None:
    _current_scope.reset(token)


@contextmanager
def usage_scope(scope: UsageScope) -> Iterator[UsageScope]:
    token = _current_scope.set(scope)
    try:
        yield scope
    finally:
        _current_scope.reset(token)


@contextmanager
def usage_stage(stage: str) -> Iterator[UsageScope | None]:
    current = _current_scope.get()
    if current is None:
        yield None
        return
    staged = replace(current, stage=stage, total_tokens=current.total_tokens)
    token = _current_scope.set(staged)
    try:
        yield staged
    finally:
        current.total_tokens = staged.total_tokens
        _current_scope.reset(token)


def current_usage_total() -> int:
    scope = _current_scope.get()
    return scope.total_tokens if scope is not None else 0


def record_usage(
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    model: str | None = None,
    stage: str | None = None,
) -> None:
    scope = _current_scope.get()
    if scope is None:
        return
    prompt = max(int(prompt_tokens or 0), 0)
    completion = max(int(completion_tokens or 0), 0)
    total = max(int(total_tokens or prompt + completion), 0)
    if total == 0:
        return

    from app.db import SessionLocal
    from app.models import LlmUsageEvent

    session = SessionLocal()
    try:
        session.add(
            LlmUsageEvent(
                context_type=scope.context_type,
                trigger_type=scope.trigger_type,
                stage=stage or scope.stage,
                model=model,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                discovery_run_id=scope.discovery_run_id,
                crawl_method_run_id=scope.crawl_method_run_id,
                morning_crawl_run_id=scope.morning_crawl_run_id,
                morning_crawl_run_method_id=scope.morning_crawl_run_method_id,
                method_id=scope.method_id,
            )
        )
        session.commit()
        scope.total_tokens += total
    except Exception:
        session.rollback()
        logger.exception("failed to persist LLM token usage")
    finally:
        session.close()


def _usage_from_message(message: Any) -> dict[str, Any]:
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict):
        return usage
    metadata = getattr(message, "response_metadata", None)
    if isinstance(metadata, dict):
        token_usage = metadata.get("token_usage")
        if isinstance(token_usage, dict):
            return token_usage
    return {}


class UsageCallbackHandler(BaseCallbackHandler):
    """LangChain callback that records provider-reported usage once per invocation."""

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("token_usage") if isinstance(llm_output, dict) else None
        model = llm_output.get("model_name") if isinstance(llm_output, dict) else None
        generations = getattr(response, "generations", None) or []
        message = generations[0][0].message if generations and generations[0] else None
        if not isinstance(usage, dict) or not any(
            key in usage for key in ("total_tokens", "prompt_tokens", "input_tokens")
        ):
            usage = _usage_from_message(message)
        metadata = getattr(message, "response_metadata", None) or {}
        model = model or metadata.get("model_name")
        if not isinstance(usage, dict):
            return
        record_usage(
            prompt_tokens=usage.get("prompt_tokens", usage.get("input_tokens")),
            completion_tokens=usage.get("completion_tokens", usage.get("output_tokens")),
            total_tokens=usage.get("total_tokens"),
            model=model,
        )
