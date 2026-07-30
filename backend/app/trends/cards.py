"""News explanation-card generation, validation and process-local task control."""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Callable, Literal, Sequence

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.client import LlmClient
from app.trends.prompts import CARD_PROMPT_VERSION, build_card_prompt
from app.trends.schemas import NewsExplanationContent, TrendCardStageStatusResponse

logger = logging.getLogger(__name__)

MIN_CARD_CONTENT_LENGTH = 200
CARD_CONTENT_CHAR_LIMIT = 3000
MAX_CARD_GENERATION_ATTEMPTS = 3

CardOutcomeStatus = Literal["ready", "failed", "skipped"]
CardContentSource = Literal["clean_content", "summary"]


@dataclass(frozen=True)
class CardGenerationCandidate:
    item_id: int
    title: str
    content: str | None
    at: date
    content_source: CardContentSource | None = "clean_content"
    key_points: list[str] | None = None


@dataclass(frozen=True)
class CardStageCounts:
    pending_count: int
    generated_count: int
    skipped_count: int
    failed_count: int


@dataclass(frozen=True)
class CardGenerationOutcome:
    status: CardOutcomeStatus
    content: NewsExplanationContent | None
    skip_reason: str | None
    error_message: str | None
    attempt_count: int


def select_card_content(
    *,
    clean_content: str | None,
    summary: str | None,
) -> tuple[str | None, CardContentSource | None]:
    """Choose the best stored input for one explanation card.

    A sufficiently long cleaned article body is the preferred evidence.  When
    it is unavailable or too short, the already stored summary remains useful
    evidence and deliberately has no minimum-length requirement.
    """

    cleaned_content = (clean_content or "").strip()
    if len(cleaned_content) >= MIN_CARD_CONTENT_LENGTH:
        return cleaned_content, "clean_content"

    stored_summary = (summary or "").strip()
    if stored_summary:
        return stored_summary, "summary"

    return None, None


@dataclass(frozen=True)
class _RuntimeState:
    is_running: bool
    start_date: date
    end_date: date
    pending_count: int
    generated_count: int
    skipped_count: int
    failed_count: int
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None


@dataclass(frozen=True)
class ActiveCardBackfill:
    """Read-only snapshot of work that has not yet been persisted."""

    start_date: date
    end_date: date
    pending_item_ids: frozenset[int]


def _concise_error(exc: Exception) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
    if isinstance(exc, ValidationError):
        details: list[str] = []
        for error in exc.errors(include_url=False, include_input=False):
            field = ".".join(str(part) for part in error["loc"]) or "输出"
            details.append(f"{field}: {error['msg']}")
        return "字段校验失败：" + "；".join(details)
    text = str(exc).strip() or exc.__class__.__name__
    return f"生成失败：{text[:500]}"


def _parse_card_content(raw: str) -> NewsExplanationContent:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    json_text = fenced.group(1) if fenced else brace.group(0) if brace else text
    payload = json.loads(json_text)
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层必须是对象")
    return NewsExplanationContent.model_validate(payload)


def generate_news_explanation_card(
    *,
    title: str,
    content: str | None,
    llm: LlmClient,
    content_source: CardContentSource | None = "clean_content",
    key_points: list[str] | None = None,
) -> CardGenerationOutcome:
    """Generate one card without reading or writing database state."""

    cleaned_content = (content or "").strip()
    if not cleaned_content or content_source is None:
        return CardGenerationOutcome(
            status="skipped",
            content=None,
            skip_reason=(
                "清洗后正文为空或不足 "
                f"{MIN_CARD_CONTENT_LENGTH} 个字符，且已存摘要为空，未调用 LLM。"
            ),
            error_message=None,
            attempt_count=0,
        )
    if content_source == "clean_content" and len(cleaned_content) < MIN_CARD_CONTENT_LENGTH:
        return CardGenerationOutcome(
            status="skipped",
            content=None,
            skip_reason=f"清洗后正文不足 {MIN_CARD_CONTENT_LENGTH} 个字符，未调用 LLM。",
            error_message=None,
            attempt_count=0,
        )

    previous_error: str | None = None
    for attempt in range(1, MAX_CARD_GENERATION_ATTEMPTS + 1):
        prompt = build_card_prompt(
            title=title,
            content=cleaned_content[:CARD_CONTENT_CHAR_LIMIT],
            content_source=content_source,
            key_points=key_points,
            attempt=attempt,
            previous_error=previous_error,
        )
        try:
            raw = llm.complete(
                prompt,
                response_format={"type": "json_object"},
            )
            card = _parse_card_content(raw)
        except Exception as exc:  # noqa: BLE001 - failure is persisted as retryable card state
            previous_error = _concise_error(exc)
            logger.warning(
                "news explanation card generation failed item_title=%r attempt=%s/%s error=%s",
                title[:120],
                attempt,
                MAX_CARD_GENERATION_ATTEMPTS,
                previous_error,
            )
            continue
        return CardGenerationOutcome(
            status="ready",
            content=card,
            skip_reason=None,
            error_message=None,
            attempt_count=attempt,
        )

    return CardGenerationOutcome(
        status="failed",
        content=None,
        skip_reason=None,
        error_message=previous_error or "新闻解释卡生成失败。",
        attempt_count=MAX_CARD_GENERATION_ATTEMPTS,
    )


class CardBackfillController:
    """Run one card backfill task per web process and expose pollable counters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: _RuntimeState | None = None
        self._thread: threading.Thread | None = None
        self._pending_item_ids: set[int] = set()

    def running_status(self) -> TrendCardStageStatusResponse | None:
        """Return the global running task without consulting persistent counts."""

        with self._lock:
            if self._state is None or not self._state.is_running:
                return None
            return self._response(self._state)

    def active_backfill(self) -> ActiveCardBackfill | None:
        """Return the active range and IDs still awaiting a durable outcome."""

        with self._lock:
            if self._state is None or not self._state.is_running:
                return None
            return ActiveCardBackfill(
                start_date=self._state.start_date,
                end_date=self._state.end_date,
                pending_item_ids=frozenset(self._pending_item_ids),
            )

    def status(
        self,
        *,
        start_date: date,
        end_date: date,
        fallback_counts: CardStageCounts,
    ) -> TrendCardStageStatusResponse:
        with self._lock:
            state = self._state
            if state is None or (
                not state.is_running and (state.start_date, state.end_date) != (start_date, end_date)
            ):
                return self._response_from_counts(
                    start_date=start_date,
                    end_date=end_date,
                    counts=fallback_counts,
                )
            return self._response(state)

    def start(
        self,
        *,
        start_date: date,
        end_date: date,
        candidates: Sequence[CardGenerationCandidate],
        counts: CardStageCounts,
        session_factory: Callable[[], Session],
        persist_outcome: Callable[[Session, CardGenerationCandidate, CardGenerationOutcome], None],
    ) -> TrendCardStageStatusResponse:
        with self._lock:
            if self._state is not None and self._state.is_running:
                response = self._response(self._state)
                return response.model_copy(update={"message": "已有新闻卡片补齐任务正在执行，本次触发复用现有任务。"})

            now = datetime.now(timezone.utc)
            self._state = _RuntimeState(
                is_running=bool(candidates),
                start_date=start_date,
                end_date=end_date,
                pending_count=len(candidates),
                generated_count=counts.generated_count,
                skipped_count=counts.skipped_count,
                failed_count=0,
                started_at=now if candidates else None,
                finished_at=None if candidates else now,
                error_message=None,
            )
            self._pending_item_ids = {candidate.item_id for candidate in candidates}
            if not candidates:
                return self._response(self._state).model_copy(
                    update={"message": "当前时间范围内没有缺失或失败的新闻解释卡。"}
                )

            self._thread = threading.Thread(
                target=self._run,
                kwargs={
                    "candidates": list(candidates),
                    "session_factory": session_factory,
                    "persist_outcome": persist_outcome,
                },
                name="trend-card-backfill",
                daemon=True,
            )
            self._thread.start()
            return self._response(self._state).model_copy(update={"message": "新闻解释卡补齐任务已启动。"})

    def _run(
        self,
        *,
        candidates: list[CardGenerationCandidate],
        session_factory: Callable[[], Session],
        persist_outcome: Callable[[Session, CardGenerationCandidate, CardGenerationOutcome], None],
    ) -> None:
        fatal_errors: list[str] = []
        try:
            llm = LlmClient()
            max_workers = max(1, get_settings().llm_max_concurrency)
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="trend-card-llm") as executor:
                future_map: dict[Future[CardGenerationOutcome], CardGenerationCandidate] = {
                    executor.submit(
                        generate_news_explanation_card,
                        title=candidate.title,
                        content=candidate.content,
                        content_source=candidate.content_source,
                        key_points=candidate.key_points,
                        llm=llm,
                    ): candidate
                    for candidate in candidates
                }
                for future in as_completed(future_map):
                    candidate = future_map[future]
                    try:
                        outcome = future.result()
                    except Exception as exc:  # defensive: generator normally converts failures to outcomes
                        outcome = CardGenerationOutcome(
                            status="failed",
                            content=None,
                            skip_reason=None,
                            error_message=_concise_error(exc),
                            attempt_count=MAX_CARD_GENERATION_ATTEMPTS,
                        )

                    db = session_factory()
                    try:
                        persist_outcome(db, candidate, outcome)
                        db.commit()
                    except Exception as exc:  # noqa: BLE001 - isolate one write from the rest of the range
                        db.rollback()
                        message = f"item_id={candidate.item_id} 卡片状态保存失败：{str(exc)[:300]}"
                        fatal_errors.append(message)
                        logger.exception(message)
                        continue
                    finally:
                        db.close()
                    self._record_outcome(candidate.item_id, outcome.status)
                    if outcome.status == "failed" and outcome.error_message:
                        fatal_errors.append(
                            f"item_id={candidate.item_id}：{outcome.error_message[:300]}"
                        )
        except Exception as exc:  # noqa: BLE001 - never leave the global stage stuck as running
            message = f"新闻解释卡后台任务异常终止：{str(exc)[:500]}"
            fatal_errors.insert(0, message)
            logger.exception(message)
        finally:
            with self._lock:
                assert self._state is not None
                self._state = replace(
                    self._state,
                    is_running=False,
                    finished_at=datetime.now(timezone.utc),
                    error_message="；".join(fatal_errors[:5]) or None,
                )

    def _record_outcome(self, item_id: int, outcome: CardOutcomeStatus) -> None:
        with self._lock:
            assert self._state is not None
            # The database commit has completed before this method is called.
            # Removing the overlay here makes the list immediately reflect the
            # durable final status, including a retry that finishes as failed.
            self._pending_item_ids.discard(item_id)
            pending = max(0, self._state.pending_count - 1)
            generated = self._state.generated_count
            skipped = self._state.skipped_count
            failed = self._state.failed_count
            if outcome == "ready":
                generated += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                failed += 1
            self._state = replace(
                self._state,
                pending_count=pending,
                generated_count=generated,
                skipped_count=skipped,
                failed_count=failed,
            )

    @staticmethod
    def _response_from_counts(
        *,
        start_date: date,
        end_date: date,
        counts: CardStageCounts,
    ) -> TrendCardStageStatusResponse:
        return TrendCardStageStatusResponse(
            is_running=False,
            start_date=start_date,
            end_date=end_date,
            pending_count=counts.pending_count,
            generated_count=counts.generated_count,
            skipped_count=counts.skipped_count,
            failed_count=counts.failed_count,
            card_prompt_version=CARD_PROMPT_VERSION,
            started_at=None,
            finished_at=None,
            error_message=None,
            retry_guidance=(
                "再次点击“补齐缺失卡片”可重试失败项；若持续失败，请检查 LLM 服务和输出格式。"
                if counts.failed_count
                else None
            ),
        )

    @staticmethod
    def _response(state: _RuntimeState) -> TrendCardStageStatusResponse:
        return TrendCardStageStatusResponse(
            is_running=state.is_running,
            start_date=state.start_date,
            end_date=state.end_date,
            pending_count=state.pending_count,
            generated_count=state.generated_count,
            skipped_count=state.skipped_count,
            failed_count=state.failed_count,
            card_prompt_version=CARD_PROMPT_VERSION,
            started_at=state.started_at,
            finished_at=state.finished_at,
            error_message=state.error_message,
            retry_guidance=(
                "再次点击“补齐缺失卡片”可重试失败项；若持续失败，请检查 LLM 服务和输出格式。"
                if state.failed_count or state.error_message
                else None
            ),
        )


card_backfill_controller = CardBackfillController()
