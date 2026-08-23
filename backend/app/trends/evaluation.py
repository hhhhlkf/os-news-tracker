"""Template trend evaluation: window scoring, Agent review, and atomic publication."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Callable

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.client import LlmClient
from app.models import Item
from app.trends.cards import select_card_content
from app.trends.embedding import current_embedding_descriptor
from app.trends.models import (
    NewsExplanationCard,
    Storyline,
    StorylineMember,
    StorylineSnapshot,
    TrendResult,
    TrendResultItem,
    TrendRun,
)
from app.trends.prompts import (
    CARD_PROMPT_VERSION,
    TREND_EVALUATION_PROMPT_VERSION,
    build_trend_evaluation_prompt,
)
from app.trends.schemas import TrendEvaluationOutput, TrendRunStatusResponse
from app.trends.scoring import InfluenceEvidence, calculate_influence_score, calculate_trend_rank_score

logger = logging.getLogger(__name__)

MAX_TREND_EVALUATION_ATTEMPTS = 3
SHORT_WINDOW_CATEGORIES = ("emerging_trend", "hot_event", "unverified_change")
LONG_WINDOW_CATEGORIES = (
    "emerging_trend",
    "hot_event",
    "periodic_activity",
    "attention_declining",
    "unverified_change",
)


@dataclass(frozen=True)
class WindowMemberEvidence:
    card_id: str
    item_id: int
    at: date
    membership: str
    importance: str | None
    title: str
    news_actor: str | None
    action: str | None
    result: str | None
    potential_impact: str | None
    cause: str | None
    content: str


@dataclass(frozen=True)
class WindowScoredStoryline:
    storyline_id: str
    title: str
    overall_start_date: date
    overall_end_date: date
    overall_influence_score: float
    cluster_threshold: float
    cohesion_score: float
    decision: str
    agent_review: str
    verified_members: list[WindowMemberEvidence]
    window_members: list[WindowMemberEvidence]
    window_start_date: date
    window_end_date: date
    window_influence_score: float
    snapshots: list[dict[str, object]]


@dataclass(frozen=True)
class PreparedTrendResult:
    storyline_id: str
    overall_start_date: date
    overall_end_date: date
    window_start_date: date
    window_end_date: date
    overall_score: float
    window_score: float
    template_relevance_score: float
    trend_rank_score: float
    category: str
    direction: str
    topic: str | None
    trend_summary: str | None
    agent_review: str
    item_ids: list[int]


@dataclass(frozen=True)
class TrendEvaluationRuntimeState:
    run_id: str
    template_id: str
    is_running: bool
    status: str
    window_start_date: date
    window_end_date: date
    trend_count: int
    storyline_candidate_goal: int
    candidate_count: int
    completed_candidate_count: int
    result_count: int
    unverified_count: int
    reusable: bool
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    message: str | None


def window_day_count(*, start: date, end: date) -> int:
    return (end - start).days + 1


def allowed_categories_for_window(*, window_days: int, snapshot_count: int) -> tuple[list[str], bool]:
    long_term_allowed = window_days > 30 and snapshot_count >= 2
    if long_term_allowed:
        return list(LONG_WINDOW_CATEGORIES), True
    return list(SHORT_WINDOW_CATEGORIES), False


def _concise_error(exc: Exception) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
    if isinstance(exc, ValidationError):
        details = [
            f"{'.'.join(str(part) for part in item['loc']) or '输出'}: {item['msg']}"
            for item in exc.errors(include_url=False, include_input=False)
        ]
        return "字段校验失败：" + "；".join(details)
    return (str(exc).strip() or exc.__class__.__name__)[:700]


def _parse_evaluation(raw: str) -> TrendEvaluationOutput:
    parsed = json.loads(raw.strip())
    if not isinstance(parsed, dict):
        raise ValueError("JSON 顶层必须是对象")
    return TrendEvaluationOutput.model_validate(parsed)


def _validate_against_allowed(
    output: TrendEvaluationOutput, *, allowed: list[str], allowed_directions: list[str]
) -> None:
    if output.category not in allowed:
        raise ValueError(
            f"category={output.category} 不在当前窗口允许列表中：{', '.join(allowed)}"
        )
    if output.direction not in allowed_directions:
        raise ValueError(
            f"direction={output.direction} 不在身份模板允许方向中：{', '.join(allowed_directions)}"
        )


def score_storylines_for_window(
    db: Session,
    *,
    window_start: date,
    window_end: date,
) -> list[WindowScoredStoryline]:
    """Backfill storyline window fields and return candidates with window intersection."""

    storylines = list(
        db.scalars(select(Storyline).where(Storyline.status.in_(("active", "archived"))))
    )
    if not storylines:
        return []

    storyline_ids = [item.storyline_id for item in storylines]
    member_rows = list(
        db.execute(
            select(
                StorylineMember,
                NewsExplanationCard,
                Item,
            )
            .join(NewsExplanationCard, NewsExplanationCard.card_id == StorylineMember.card_id)
            .join(Item, Item.id == NewsExplanationCard.item_id)
            .where(StorylineMember.storyline_id.in_(storyline_ids))
            .order_by(StorylineMember.at.asc(), StorylineMember.card_id.asc())
        )
    )
    snapshots = list(
        db.scalars(
            select(StorylineSnapshot)
            .where(StorylineSnapshot.storyline_id.in_(storyline_ids))
            .order_by(StorylineSnapshot.at.asc(), StorylineSnapshot.snapshot_id.asc())
        )
    )
    members_by_storyline: dict[str, list[tuple[StorylineMember, NewsExplanationCard, Item]]] = {
        storyline_id: [] for storyline_id in storyline_ids
    }
    for member, card, item in member_rows:
        members_by_storyline[member.storyline_id].append((member, card, item))
    snapshots_by_storyline: dict[str, list[StorylineSnapshot]] = {
        storyline_id: [] for storyline_id in storyline_ids
    }
    for snapshot in snapshots:
        snapshots_by_storyline[snapshot.storyline_id].append(snapshot)

    scored: list[WindowScoredStoryline] = []
    for storyline in storylines:
        verified_members: list[WindowMemberEvidence] = []
        window_members: list[WindowMemberEvidence] = []
        for member, card, item in members_by_storyline.get(storyline.storyline_id, []):
            if member.membership == "duplicate":
                continue
            evidence = WindowMemberEvidence(
                card_id=member.card_id,
                item_id=item.id,
                at=member.at,
                membership=member.membership,
                importance=item.importance,
                title=(item.title_tldr or item.title or "").strip(),
                news_actor=card.news_actor,
                action=card.action,
                result=card.result,
                potential_impact=card.potential_impact,
                cause=card.cause,
                content="",
            )
            verified_members.append(evidence)
            if member.at < window_start or member.at > window_end:
                continue
            content, _source = select_card_content(
                clean_content=item.clean_content,
                summary=item.summary,
            )
            window_members.append(replace(evidence, content=(content or "").strip()[:3000]))
        if not window_members:
            storyline.window_start_date = None
            storyline.window_end_date = None
            storyline.window_influence_score = None
            continue
        local_start = min(member.at for member in window_members)
        local_end = max(member.at for member in window_members)
        score = calculate_influence_score(
            evidence=[
                InfluenceEvidence(
                    at=member.at,
                    membership=member.membership,
                    importance=member.importance,
                )
                for member in window_members
            ],
            cohesion_score=storyline.cohesion_score,
            recency_anchor=window_end,
        )
        storyline.window_start_date = local_start
        storyline.window_end_date = local_end
        storyline.window_influence_score = score
        scored.append(
            WindowScoredStoryline(
                storyline_id=storyline.storyline_id,
                title=storyline.title,
                overall_start_date=storyline.overall_start_date,
                overall_end_date=storyline.overall_end_date,
                overall_influence_score=storyline.overall_influence_score,
                cluster_threshold=storyline.cluster_threshold,
                cohesion_score=storyline.cohesion_score,
                decision=storyline.decision,
                agent_review=storyline.agent_review,
                verified_members=verified_members,
                window_members=window_members,
                window_start_date=local_start,
                window_end_date=local_end,
                window_influence_score=score,
                snapshots=_snapshot_payload(snapshots_by_storyline.get(storyline.storyline_id, [])),
            )
        )
    db.flush()
    scored.sort(
        key=lambda item: (-item.window_influence_score, item.storyline_id),
    )
    return scored


def _storyline_payload(candidate: WindowScoredStoryline) -> dict[str, object]:
    return {
        "storyline_id": candidate.storyline_id,
        "title": candidate.title,
        "overall_time_range": [
            candidate.overall_start_date.isoformat(),
            candidate.overall_end_date.isoformat(),
        ],
        "window_time_range": [
            candidate.window_start_date.isoformat(),
            candidate.window_end_date.isoformat(),
        ],
        "cluster": {
            "threshold": candidate.cluster_threshold,
            "cohesion_score": candidate.cohesion_score,
        },
        "overall_influence_score": candidate.overall_influence_score,
        "window_influence_score": candidate.window_influence_score,
        "decision": candidate.decision,
        "agent_review": candidate.agent_review,
        "verified_evidence": [
            {
                "card_id": member.card_id,
                "at": member.at.isoformat(),
                "membership": member.membership,
                "title": member.title,
                "news_actor": member.news_actor,
                "action": member.action,
                "result": member.result,
                "potential_impact": member.potential_impact,
                "cause": member.cause,
            }
            for member in candidate.verified_members
        ],
        "timeline": [
            {
                "card_id": member.card_id,
                "at": member.at.isoformat(),
                "membership": member.membership,
            }
            for member in candidate.window_members
        ],
    }


def _window_card_payload(candidate: WindowScoredStoryline) -> list[dict[str, object]]:
    return [
        {
            "card_id": member.card_id,
            "item_id": member.item_id,
            "at": member.at.isoformat(),
            "membership": member.membership,
            "title": member.title,
            "news_actor": member.news_actor,
            "action": member.action,
            "result": member.result,
            "potential_impact": member.potential_impact,
            "cause": member.cause,
            "content": member.content,
        }
        for member in candidate.window_members
    ]


def _snapshot_payload(snapshots: list[StorylineSnapshot]) -> list[dict[str, object]]:
    return [
        {
            "snapshot_id": snapshot.snapshot_id,
            "at": snapshot.at.isoformat(),
            "time_range": [snapshot.time_start_date.isoformat(), snapshot.time_end_date.isoformat()],
            "card_ids": snapshot.card_ids,
            "memberships": snapshot.memberships,
            "influence_score": snapshot.influence_score,
            "decision": snapshot.decision,
        }
        for snapshot in snapshots
    ]


def evaluate_candidate(
    *,
    candidate: WindowScoredStoryline,
    analysis_identity: str,
    allowed_directions: list[str],
    window_start: date,
    window_end: date,
    llm: LlmClient,
) -> PreparedTrendResult:
    window_days = window_day_count(start=window_start, end=window_end)
    allowed, long_term_allowed = allowed_categories_for_window(
        window_days=window_days,
        snapshot_count=len(candidate.snapshots),
    )
    previous_error: str | None = None
    for attempt in range(1, MAX_TREND_EVALUATION_ATTEMPTS + 1):
        prompt = build_trend_evaluation_prompt(
            analysis_identity=analysis_identity,
            allowed_directions=allowed_directions,
            window_start_date=window_start.isoformat(),
            window_end_date=window_end.isoformat(),
            window_days=window_days,
            allowed_categories=allowed,
            long_term_allowed=long_term_allowed,
            storyline=_storyline_payload(candidate),
            window_cards=_window_card_payload(candidate),
            snapshots=candidate.snapshots,
            window_influence_score=candidate.window_influence_score,
            attempt=attempt,
            previous_error=previous_error,
        )
        try:
            output = _parse_evaluation(llm.complete(prompt, response_format={"type": "json_object"}))
            _validate_against_allowed(
                output,
                allowed=allowed,
                allowed_directions=allowed_directions,
            )
            return PreparedTrendResult(
                storyline_id=candidate.storyline_id,
                overall_start_date=candidate.overall_start_date,
                overall_end_date=candidate.overall_end_date,
                window_start_date=candidate.window_start_date,
                window_end_date=candidate.window_end_date,
                overall_score=candidate.overall_influence_score,
                window_score=candidate.window_influence_score,
                template_relevance_score=float(output.template_relevance_score),
                trend_rank_score=calculate_trend_rank_score(
                    window_influence_score=candidate.window_influence_score,
                    template_relevance_score=float(output.template_relevance_score),
                ),
                category=output.category,
                direction=output.direction,
                topic=output.topic,
                trend_summary=output.trend_summary,
                agent_review=output.agent_review,
                item_ids=sorted({member.item_id for member in candidate.window_members}),
            )
        except Exception as exc:  # noqa: BLE001 - retry with concrete validation feedback
            previous_error = _concise_error(exc)
            logger.warning(
                "trend evaluation failed storyline=%s attempt=%s/%s error=%s",
                candidate.storyline_id,
                attempt,
                MAX_TREND_EVALUATION_ATTEMPTS,
                previous_error,
            )
    raise RuntimeError(previous_error or "趋势总结 Agent 输出校验失败。")


def publish_results(db: Session, *, run: TrendRun, prepared: list[PreparedTrendResult]) -> None:
    """Atomically replace nothing on failure; write the full result set on success."""

    for item in prepared:
        result = TrendResult(
            run_id=run.run_id,
            storyline_id=item.storyline_id,
            overall_start_date=item.overall_start_date,
            overall_end_date=item.overall_end_date,
            window_start_date=item.window_start_date,
            window_end_date=item.window_end_date,
            overall_score=item.overall_score,
            window_score=item.window_score,
            template_relevance_score=item.template_relevance_score,
            trend_rank_score=item.trend_rank_score,
            category=item.category,
            direction=item.direction,
            topic=item.topic,
            trend_summary=item.trend_summary,
            agent_review=item.agent_review,
        )
        db.add(result)
        db.flush()
        for item_id in item.item_ids:
            db.add(TrendResultItem(result_id=result.result_id, item_id=item_id))
    run.status = "succeeded"
    run.error_message = None
    run.completed_candidate_count = len(prepared)
    run.finished_at = datetime.now(timezone.utc)


def execute_trend_run(
    *,
    run_id: str,
    session_factory: Callable[[], Session],
    llm: LlmClient | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """Score the window, evaluate top 3X candidates, and publish only if all succeed."""

    client = llm or LlmClient()
    db = session_factory()
    candidates: list[WindowScoredStoryline] = []
    window_start: date
    window_end: date
    analysis_identity: str
    allowed_directions: list[str]
    try:
        run = db.get(TrendRun, run_id)
        if run is None:
            return
        run.status = "running"
        run.started_at = run.started_at or datetime.now(timezone.utc)
        run.error_message = None
        window_start = run.window_start_date
        window_end = run.window_end_date
        analysis_identity = run.analysis_identity_snapshot
        allowed_directions = list(run.direction_labels)
        if not allowed_directions:
            raise ValueError("该趋势运行未保存可用方向，无法执行评估。")
        scored = score_storylines_for_window(
            db,
            window_start=window_start,
            window_end=window_end,
        )
        limit = max(1, run.trend_count * 3)
        candidates = scored[:limit]
        run.candidate_storyline_ids = [item.storyline_id for item in candidates]
        run.candidate_count = len(candidates)
        run.completed_candidate_count = 0
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        failed = db.get(TrendRun, run_id)
        if failed is not None:
            failed.status = "failed"
            failed.error_message = _concise_error(exc)
            failed.finished_at = datetime.now(timezone.utc)
            db.commit()
        raise
    finally:
        db.close()

    prepared: list[PreparedTrendResult] = []
    try:
        for index, candidate in enumerate(candidates, start=1):
            prepared.append(
                evaluate_candidate(
                    candidate=candidate,
                    analysis_identity=analysis_identity,
                    allowed_directions=allowed_directions,
                    window_start=window_start,
                    window_end=window_end,
                    llm=client,
                )
            )
            if on_progress is not None:
                on_progress(index, len(candidates))
            progress_db = session_factory()
            try:
                progress_run = progress_db.get(TrendRun, run_id)
                if progress_run is not None:
                    progress_run.completed_candidate_count = index
                    progress_db.commit()
            finally:
                progress_db.close()
    except Exception as exc:  # noqa: BLE001 - incomplete runs must not publish
        fail_db = session_factory()
        try:
            failed = fail_db.get(TrendRun, run_id)
            if failed is not None:
                failed.status = "failed"
                failed.error_message = _concise_error(exc)
                failed.finished_at = datetime.now(timezone.utc)
                fail_db.commit()
        finally:
            fail_db.close()
        raise

    publish_db = session_factory()
    try:
        run = publish_db.get(TrendRun, run_id)
        if run is None:
            return
        publish_results(publish_db, run=run, prepared=prepared)
        publish_db.commit()
    except Exception as exc:  # noqa: BLE001
        publish_db.rollback()
        failed = publish_db.get(TrendRun, run_id)
        if failed is not None:
            failed.status = "failed"
            failed.error_message = _concise_error(exc)
            failed.finished_at = datetime.now(timezone.utc)
            publish_db.commit()
        raise
    finally:
        publish_db.close()


class TrendEvaluationController:
    """Per-template serial opinion-layer jobs with pollable status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_template: dict[str, TrendEvaluationRuntimeState] = {}

    def snapshot(self, template_id: str) -> TrendEvaluationRuntimeState | None:
        with self._lock:
            return self._by_template.get(template_id)

    def start(
        self,
        *,
        run: TrendRun,
        session_factory: Callable[[], Session],
        llm_factory: Callable[[], LlmClient] | None = None,
    ) -> TrendEvaluationRuntimeState:
        with self._lock:
            existing = self._by_template.get(run.template_id)
            if existing is not None and existing.is_running:
                return replace(existing, reusable=True, message="已有该模板的趋势运行正在执行，本次触发复用现有任务。")
            now = datetime.now(timezone.utc)
            state = TrendEvaluationRuntimeState(
                run_id=run.run_id,
                template_id=run.template_id,
                is_running=True,
                status="running",
                window_start_date=run.window_start_date,
                window_end_date=run.window_end_date,
                trend_count=run.trend_count,
                storyline_candidate_goal=run.storyline_candidate_goal,
                candidate_count=run.candidate_count,
                completed_candidate_count=0,
                result_count=0,
                unverified_count=0,
                reusable=False,
                started_at=run.started_at or now,
                finished_at=None,
                error_message=None,
                message="趋势总结任务已启动。",
            )
            self._by_template[run.template_id] = state
            threading.Thread(
                target=self._run,
                kwargs={
                    "run_id": run.run_id,
                    "template_id": run.template_id,
                    "session_factory": session_factory,
                    "llm_factory": llm_factory or LlmClient,
                },
                name=f"trend-evaluation-{run.template_id[:8]}",
                daemon=True,
            ).start()
            return state

    def _run(
        self,
        *,
        run_id: str,
        template_id: str,
        session_factory: Callable[[], Session],
        llm_factory: Callable[[], LlmClient],
    ) -> None:
        error_message: str | None = None
        status = "succeeded"
        result_count = 0
        unverified_count = 0
        candidate_count = 0
        completed = 0
        try:
            def on_progress(done: int, total: int) -> None:
                with self._lock:
                    current = self._by_template.get(template_id)
                    if current is None:
                        return
                    self._by_template[template_id] = replace(
                        current,
                        candidate_count=total,
                        completed_candidate_count=done,
                        message=f"正在评估候选故事线 {done}/{total}。",
                    )

            execute_trend_run(
                run_id=run_id,
                session_factory=session_factory,
                llm=llm_factory(),
                on_progress=on_progress,
            )
            db = session_factory()
            try:
                run = db.get(TrendRun, run_id)
                if run is not None:
                    status = run.status
                    candidate_count = run.candidate_count
                    completed = run.completed_candidate_count
                    error_message = run.error_message
                    results = list(db.scalars(select(TrendResult).where(TrendResult.run_id == run_id)))
                    result_count = len(results)
                    unverified_count = sum(1 for item in results if item.category == "unverified_change")
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.exception("trend evaluation run failed run_id=%s", run_id)
            status = "failed"
            error_message = _concise_error(exc)
        with self._lock:
            current = self._by_template.get(template_id)
            if current is None or current.run_id != run_id:
                return
            self._by_template[template_id] = replace(
                current,
                is_running=False,
                status=status,
                candidate_count=candidate_count or current.candidate_count,
                completed_candidate_count=completed or current.completed_candidate_count,
                result_count=result_count,
                unverified_count=unverified_count,
                finished_at=datetime.now(timezone.utc),
                error_message=error_message,
                message=(
                    "趋势总结已发布。"
                    if status == "succeeded"
                    else "趋势总结失败，未覆盖该模板最近成功结果。"
                ),
            )


def runtime_to_status_response(
    state: TrendEvaluationRuntimeState,
    *,
    message: str | None = None,
) -> TrendRunStatusResponse:
    return TrendRunStatusResponse(
        run_id=state.run_id,
        template_id=state.template_id,
        is_running=state.is_running,
        status=state.status,  # type: ignore[arg-type]
        window_start_date=state.window_start_date,
        window_end_date=state.window_end_date,
        trend_count=state.trend_count,
        storyline_candidate_goal=state.storyline_candidate_goal,
        candidate_count=state.candidate_count,
        completed_candidate_count=state.completed_candidate_count,
        result_count=state.result_count,
        unverified_count=state.unverified_count,
        reusable=state.reusable,
        started_at=state.started_at,
        finished_at=state.finished_at,
        error_message=state.error_message,
        retry_guidance=(
            "检查 LLM 服务与 JSON 输出契约后重新触发；失败运行不会替换最近成功发布。"
            if state.status == "failed" or state.error_message
            else None
        ),
        message=message if message is not None else state.message,
    )


def run_to_status_response(
    run: TrendRun,
    *,
    result_count: int = 0,
    unverified_count: int = 0,
    reusable: bool = False,
    message: str | None = None,
) -> TrendRunStatusResponse:
    return TrendRunStatusResponse(
        run_id=run.run_id,
        template_id=run.template_id,
        is_running=run.status in {"pending", "running"},
        status=run.status,  # type: ignore[arg-type]
        window_start_date=run.window_start_date,
        window_end_date=run.window_end_date,
        trend_count=run.trend_count,
        storyline_candidate_goal=run.storyline_candidate_goal,
        candidate_count=run.candidate_count,
        completed_candidate_count=run.completed_candidate_count,
        result_count=result_count,
        unverified_count=unverified_count,
        reusable=reusable,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error_message=run.error_message,
        retry_guidance=(
            "检查 LLM 服务与 JSON 输出契约后重新触发；失败运行不会替换最近成功发布。"
            if run.status == "failed" or run.error_message
            else None
        ),
        message=message,
    )


def build_trend_run(
    *,
    template_id: str,
    window_start: date,
    window_end: date,
    trend_count: int,
    storyline_candidate_goal: int,
    analysis_identity_snapshot: str,
    direction_labels: list[str],
) -> TrendRun:
    settings = get_settings()
    return TrendRun(
        template_id=template_id,
        window_start_date=window_start,
        window_end_date=window_end,
        trend_count=trend_count,
        storyline_candidate_goal=storyline_candidate_goal,
        embedding_version=current_embedding_descriptor().embedding_version,
        card_prompt_version=CARD_PROMPT_VERSION,
        trend_prompt_version=TREND_EVALUATION_PROMPT_VERSION,
        model_version=settings.llm_model,
        analysis_identity_snapshot=analysis_identity_snapshot,
        direction_labels=direction_labels,
        candidate_storyline_ids=[],
        status="pending",
        candidate_count=0,
        completed_candidate_count=0,
        started_at=datetime.now(timezone.utc),
    )


trend_evaluation_controller = TrendEvaluationController()
