"""Exact LLM token accounting for query and discovery runs."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any, Iterator

from langchain_core.callbacks import BaseCallbackHandler
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

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
) -> bool:
    """Persist provider-reported usage and report whether it was accounted."""
    scope = _current_scope.get()
    if scope is None:
        return False
    prompt = max(int(prompt_tokens or 0), 0)
    completion = max(int(completion_tokens or 0), 0)
    total = max(int(total_tokens or prompt + completion), 0)

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
        return True
    except Exception:
        session.rollback()
        logger.exception("failed to persist LLM token usage")
        return False
    finally:
        session.close()


def record_trusted_relay_usage(
    *,
    relay_session_id: str,
    relay_sequence: int,
    total_tokens: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    model: str,
    stage: str,
    usage_status: str = "exact",
    session: Any | None = None,
) -> bool:
    """Atomically persist one trusted relay sequence without owning caller transactions.

    An ``unknown`` row is the durable, idempotent audit/ack fact and carries no
    estimated usage.  When a caller supplies a Session it owns commit/rollback;
    this function only flushes changes into that transaction.
    """
    scope = _current_scope.get()
    if (
        scope is None
        or scope.discovery_run_id is None
        or not relay_session_id
        or len(relay_session_id) > 64
        or relay_sequence < 1
        or isinstance(relay_sequence, bool)
        or total_tokens < 0
        or isinstance(total_tokens, bool)
        or prompt_tokens < 0
        or isinstance(prompt_tokens, bool)
        or completion_tokens < 0
        or isinstance(completion_tokens, bool)
        or usage_status not in {"exact", "unknown"}
        or (usage_status == "unknown" and any((total_tokens, prompt_tokens, completion_tokens)))
    ):
        return False

    from app.db import SessionLocal
    from app.models import LlmUsageEvent, SiteDiscoveryRun

    values = {
        "context_type": scope.context_type,
        "trigger_type": scope.trigger_type,
        "stage": stage,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "discovery_run_id": scope.discovery_run_id,
        "usage_provenance": "openhands_trusted_relay",
        "relay_session_id": relay_session_id,
        "relay_sequence": relay_sequence,
        "relay_usage_status": usage_status,
    }
    conflict_columns = [
        "discovery_run_id", "usage_provenance", "relay_session_id", "relay_sequence",
    ]
    own_session = session is None
    session = session or SessionLocal()
    try:
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = postgresql_insert(LlmUsageEvent).values(**values)
        elif dialect == "sqlite":
            statement = sqlite_insert(LlmUsageEvent).values(**values)
        else:
            raise RuntimeError("trusted relay usage requires conflict-safe SQL support")
        inserted_id = session.scalar(
            statement.on_conflict_do_nothing(index_elements=conflict_columns)
            .returning(LlmUsageEvent.id)
        )
        # Lock/synchronize the authoritative run counter.  Never derive the
        # next value from a process-local += because recovery workers may race.
        run_total = session.scalar(
            select(SiteDiscoveryRun.llm_token_usage)
            .where(SiteDiscoveryRun.id == scope.discovery_run_id)
            .with_for_update()
        )
        if run_total is None:
            raise LookupError("trusted relay Discovery run is missing")
        authoritative_base = max(int(run_total), int(scope.total_tokens))
        authoritative_next = authoritative_base + (total_tokens if inserted_id is not None else 0)
        session.execute(
            update(SiteDiscoveryRun)
            .where(SiteDiscoveryRun.id == scope.discovery_run_id)
            .values(llm_token_usage=authoritative_next)
        )
        session.flush()
        authoritative_total = session.scalar(
            select(func.coalesce(SiteDiscoveryRun.llm_token_usage, 0)).where(
                SiteDiscoveryRun.id == scope.discovery_run_id
            )
        )
        if authoritative_total is None:
            raise LookupError("trusted relay Discovery run is missing")
        if own_session:
            session.commit()
            scope.total_tokens = int(authoritative_total)
        return True
    except Exception:
        if own_session:
            session.rollback()
        logger.exception("failed to persist idempotent trusted relay usage")
        return False
    finally:
        if own_session:
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
