"""Agent review orchestration and durable storyline updates for the fact layer."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Callable, Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.client import LlmClient
from app.models import Item
from app.trends.models import NewsExplanationCard, Storyline, StorylineMember, StorylineSnapshot, TrendStorylineReview
from app.trends.prompts import STORYLINE_REVIEW_PROMPT_VERSION, build_storyline_review_prompt
from app.trends.repository import StorylineReviewCandidate
from app.trends.schemas import StorylineReviewOutput, TrendStorylineStageStatusResponse
from app.trends.scoring import InfluenceEvidence, calculate_influence_score
from app.trends.clusters import AGENT_CONTEXT_CARD_LIMIT

logger = logging.getLogger(__name__)

MAX_STORYLINE_REVIEW_ATTEMPTS = 3
ReviewOutcome = Literal["accepted", "split", "rejected", "failed"]
CONTINUATION_TOKEN = re.compile(r"^\[\[storyline:([0-9a-f-]{36})\]\]\s*")


class ForcedRejectError(ValueError):
    """A valid Agent response that violates a non-negotiable storage boundary."""


@dataclass(frozen=True)
class StorylineReviewTask:
    review_id: str
    candidate: StorylineReviewCandidate


@dataclass(frozen=True)
class StorylineRuntimeState:
    is_running: bool
    start_date: date
    end_date: date
    embedding_version: str
    pending_count: int
    accepted_count: int
    split_count: int
    rejected_count: int
    failed_count: int
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None


@dataclass(frozen=True)
class _ReviewInput:
    cards: list[dict[str, object]]
    active_continuations: list[dict[str, object]]
    archived_continuations: list[dict[str, object]]
    continuation_members: dict[str, set[str]]
    history_owner: dict[str, str]
    card_dates: dict[str, date]


def review_key_for_candidate(candidate: StorylineReviewCandidate) -> str:
    payload = "|".join(
        (
            STORYLINE_REVIEW_PROMPT_VERSION,
            candidate.embedding_version,
            *sorted(card.card_id for card in candidate.cards),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def _parse_review(raw: str) -> StorylineReviewOutput:
    parsed = json.loads(raw.strip())
    if not isinstance(parsed, dict):
        raise ValueError("JSON 顶层必须是对象")
    return StorylineReviewOutput.model_validate(parsed)


def _continuation_target(title: str) -> tuple[str | None, str]:
    """Read the stable ID tunnelled through the allowed title field."""
    match = CONTINUATION_TOKEN.match(title.strip())
    if match is None:
        return None, title.strip()
    rendered = title.strip()[match.end():].strip()
    if not rendered:
        raise ValueError("continuation_token 后必须包含中文故事线标题")
    return match.group(1), rendered


def _validate_output_against_candidate(
    *, candidate: StorylineReviewCandidate, output: StorylineReviewOutput
) -> None:
    review_input = _build_review_input(candidate)
    candidate_ids = {card.card_id for card in candidate.cards}
    allowed_ids = {str(card["card_id"]) for card in review_input.cards}
    assigned: list[str] = []
    for line in output.storylines:
        line_ids = [member.card_id for member in line.members]
        if len(set(line_ids)) != len(line_ids):
            raise ValueError("同一 storyline 的 card_id 不能重复")
        independent = sum(member.membership in {"core", "supporting"} for member in line.members)
        if independent < 2:
            raise ForcedRejectError("accept 或 split 后每条故事线至少包含两张 core/supporting 卡片")
        assigned.extend(line_ids)
    if len(set(assigned)) != len(assigned):
        raise ValueError("同一候选簇卡片不能分配给多条故事线")
    removed = set(output.removed_card_ids)
    all_referenced = set(assigned) | removed
    unknown = all_referenced - allowed_ids
    if unknown:
        raise ValueError(f"输出引用了候选簇外 card_id：{', '.join(sorted(unknown))}")
    if set(assigned) & removed:
        raise ValueError("卡片不能同时作为故事线成员和 removed_card_ids")
    if output.decision != "reject" and not candidate_ids <= all_referenced:
        missing = candidate_ids - all_referenced
        raise ValueError(f"每张输入卡必须被分配或移除，缺少：{', '.join(sorted(missing))}")
    if removed - candidate_ids:
        raise ValueError("续接故事线的历史成员不得放入 removed_card_ids")
    if len(candidate.cards) == 3 and len(removed) > 1:
        raise ForcedRejectError("三卡候选簇最多移除一张；本次结果必须按 reject 处理")
    if len(candidate.cards) == 3:
        remaining_independent = sum(
            member.card_id in candidate_ids and member.membership in {"core", "supporting"}
            for line in output.storylines for member in line.members
        )
        if output.decision != "reject" and remaining_independent < 2:
            raise ForcedRejectError("三卡候选簇留下的 core/supporting 卡片不足两张，必须按 reject 处理")
    if output.decision == "reject" and output.storylines:
        raise ValueError("reject 时不得输出故事线")
    known_continuations = {
        str(item["storyline_id"])
        for item in review_input.active_continuations + review_input.archived_continuations
    }
    targets: list[str] = []
    for line in output.storylines:
        target, _ = _continuation_target(line.title)
        if target is not None and target not in known_continuations:
            raise ValueError("title 中的 continuation_token 不属于本次召回的故事线")
        if target is not None:
            targets.append(target)
    if len(set(targets)) != len(targets):
        raise ValueError("同一既有故事线只能被一条输出故事线作为续接或拆分目标")
    line_targets = [_continuation_target(line.title)[0] for line in output.storylines]
    if output.decision == "split":
        active_ids = {str(item["storyline_id"]) for item in review_input.active_continuations}
        archived_ids = {str(item["storyline_id"]) for item in review_input.archived_continuations}
        for target in targets:
            if target in archived_ids:
                raise ForcedRejectError("已归档故事线只能通过 accept 明确重新激活，不允许在本轮 split 中重分配")
            if target not in active_ids:
                continue
            required = review_input.continuation_members[target]
            assigned_ids = set(assigned)
            if not required <= assigned_ids:
                raise ForcedRejectError("既有故事线拆分时必须将其全部历史成员恰好分配到输出故事线")
    split_targets = set(targets) if output.decision == "split" else set()
    for index, line in enumerate(output.storylines):
        for member in line.members:
            owner = review_input.history_owner.get(member.card_id)
            if owner is None:
                continue
            if owner in split_targets:
                # Full-split validation above has already guaranteed complete,
                # unique reallocation for this old storyline.
                continue
            if line_targets[index] != owner:
                raise ValueError("既有故事线历史成员只能归入其对应 continuation_token 的故事线")


def _structured_cards(candidate: StorylineReviewCandidate) -> list[dict[str, object]]:
    return [
        {
            "card_id": card.card_id,
            "at": card.at.isoformat(),
            "title": card.title,
            "news_actor": card.news_actor,
            "action": card.action,
            "result": card.result,
            "potential_impact": card.potential_impact,
            "cause": card.cause,
            "content": card.content,
        }
        for card in candidate.cards
    ]


def _build_review_input(candidate: StorylineReviewCandidate) -> _ReviewInput:
    """Fit candidate and complete continuation evidence into the 12-card hard cap.

    A continuation is either included with every known member card or omitted.
    That makes an Agent-confirmed split safe: it can reassign every historical
    member exactly once instead of the backend guessing where partial evidence
    belongs.
    """
    cards = _structured_cards(candidate)
    if len(cards) > AGENT_CONTEXT_CARD_LIMIT:
        raise ValueError("候选簇超过故事线审查的 12 张卡片硬上限，拒绝生成 Prompt。")
    card_dates = {card.card_id: card.at for card in candidate.cards}
    seen_ids = set(card_dates)
    remaining = AGENT_CONTEXT_CARD_LIMIT - len(cards)
    continuation_members: dict[str, set[str]] = {}
    history_owner: dict[str, str] = {}
    selected_active: list[dict[str, object]] = []
    selected_archived: list[dict[str, object]] = []

    continuation_sources = [(item, selected_active) for item in candidate.active_continuations]
    continuation_sources.extend((item, selected_archived) for item in candidate.archived_continuations)
    for continuation, destination in continuation_sources:
        evidence_ids = {str(item["card_id"]) for item in continuation.evidence}
        if any(card_id in history_owner and history_owner[card_id] != continuation.storyline_id for card_id in evidence_ids):
            continue
        new_evidence = [item for item in continuation.evidence if str(item["card_id"]) not in seen_ids]
        if len(new_evidence) > remaining:
            continue
        for item in new_evidence:
            card_id = str(item["card_id"])
            at = date.fromisoformat(str(item["at"]))
            cards.append({
                "card_id": card_id,
                "at": at.isoformat(),
                "title": str(item["title"]),
                "news_actor": str(item["news_actor"]),
                "action": str(item["action"]),
                "result": str(item["result"]),
                "potential_impact": "无",
                "cause": str(item["cause"]),
                "content": "",
            })
            card_dates[card_id] = at
            seen_ids.add(card_id)
        remaining -= len(new_evidence)
        continuation_members[continuation.storyline_id] = evidence_ids
        for card_id in evidence_ids:
            history_owner[card_id] = continuation.storyline_id
        destination.append({
            "storyline_id": continuation.storyline_id,
            "continuation_token": f"[[storyline:{continuation.storyline_id}]]",
            "title": continuation.title,
            "last_member_at": continuation.last_member_at.isoformat(),
            "similarity": continuation.similarity,
            "member_card_ids": sorted(evidence_ids),
        })
    return _ReviewInput(
        cards=cards,
        active_continuations=selected_active,
        archived_continuations=selected_archived,
        continuation_members=continuation_members,
        history_owner=history_owner,
        card_dates=card_dates,
    )


def _generate_review(candidate: StorylineReviewCandidate, llm: LlmClient) -> tuple[StorylineReviewOutput | None, int, str | None]:
    previous_error: str | None = None
    review_input = _build_review_input(candidate)
    for attempt in range(1, MAX_STORYLINE_REVIEW_ATTEMPTS + 1):
        prompt = build_storyline_review_prompt(
            cards=review_input.cards,
            threshold=candidate.threshold,
            cohesion_score=candidate.cohesion_score,
            active_continuations=review_input.active_continuations,
            archived_continuations=review_input.archived_continuations,
            attempt=attempt,
            previous_error=previous_error,
        )
        try:
            output = _parse_review(llm.complete(prompt, response_format={"type": "json_object"}))
            _validate_output_against_candidate(candidate=candidate, output=output)
            return output, attempt, None
        except ForcedRejectError as exc:
            return (
                StorylineReviewOutput(
                    decision="reject",
                    storylines=[],
                    removed_card_ids=[],
                    agent_review=f"系统按故事线硬校验拒绝该候选簇：{exc}",
                ),
                attempt,
                None,
            )
        except Exception as exc:  # noqa: BLE001 - each invalid cluster remains independently retryable
            previous_error = _concise_error(exc)
            logger.warning(
                "storyline review failed candidate=%s attempt=%s/%s error=%s",
                candidate.candidate_cluster_id,
                attempt,
                MAX_STORYLINE_REVIEW_ATTEMPTS,
                previous_error,
            )
    return None, MAX_STORYLINE_REVIEW_ATTEMPTS, previous_error or "故事线审查失败。"


def _refresh_storyline(db: Session, storyline: Storyline, *, review_id: str) -> None:
    rows = list(
        db.execute(
            select(StorylineMember, Item.importance)
            .join(NewsExplanationCard, NewsExplanationCard.card_id == StorylineMember.card_id)
            .join(Item, Item.id == NewsExplanationCard.item_id)
            .where(StorylineMember.storyline_id == storyline.storyline_id)
        )
    )
    members = [row[0] for row in rows]
    if not members:
        return
    storyline.overall_start_date = min(member.at for member in members)
    storyline.overall_end_date = max(member.at for member in members)
    storyline.last_member_at = max(member.at for member in members)
    storyline.overall_influence_score = calculate_influence_score(
        evidence=[
            InfluenceEvidence(at=member.at, membership=member.membership, importance=importance)
            for member, importance in rows
        ],
        cohesion_score=storyline.cohesion_score,
    )
    db.add(
        StorylineSnapshot(
            storyline_id=storyline.storyline_id,
            review_id=review_id,
            at=date.today(),
            time_start_date=storyline.overall_start_date,
            time_end_date=storyline.overall_end_date,
            card_ids=[member.card_id for member in sorted(members, key=lambda item: (item.at, item.card_id))],
            memberships={member.card_id: member.membership for member in members},
            influence_score=storyline.overall_influence_score,
            decision=storyline.decision,
        )
    )


def _store_review_result(db: Session, task: StorylineReviewTask, output: StorylineReviewOutput, attempts: int) -> ReviewOutcome:
    review = db.get(TrendStorylineReview, task.review_id)
    if review is None:
        raise ValueError("故事线审查记录不存在，可能已被清理")
    review.attempt_count = attempts
    review.removed_card_ids = output.removed_card_ids
    review.agent_review = output.agent_review
    review.error_message = None
    review.decision = output.decision
    review.completed_at = datetime.now(timezone.utc)
    if output.decision == "reject":
        review.status = "rejected"
        return "rejected"

    review_input = _build_review_input(task.candidate)
    card_dates = review_input.card_dates
    active_ids = {item.storyline_id for item in task.candidate.active_continuations}
    archived_ids = {item.storyline_id for item in task.candidate.archived_continuations}
    line_targets = [_continuation_target(line.title)[0] for line in output.storylines]
    assigned_ids = {member.card_id for line in output.storylines for member in line.members}
    owner_rows = db.execute(
        select(StorylineMember.card_id, StorylineMember.storyline_id)
        .where(StorylineMember.card_id.in_(assigned_ids))
    )
    owners_by_card: dict[str, set[str]] = {}
    for card_id, storyline_id in owner_rows:
        owners_by_card.setdefault(card_id, set()).add(storyline_id)
    active_split_targets = {
        target for target in line_targets
        if output.decision == "split" and target is not None and target in active_ids
    }
    for line, target in zip(output.storylines, line_targets, strict=True):
        for member in line.members:
            owners = owners_by_card.get(member.card_id, set())
            if not owners:
                continue
            if output.decision == "split" and owners <= active_split_targets:
                continue
            if target is not None and owners == {target}:
                continue
            # This can occur when a real owner was omitted from the bounded
            # continuation context. Never create a second membership silently.
            review.status = "rejected"
            review.agent_review = (
                "系统拒绝本次候选簇：存在未被本轮明确续接或完整拆分的既有故事线成员，"
                "已保留为待匹配证据。"
            )
            return "rejected"
    if output.decision == "split":
        # The stable token is the Agent's explicit semantic split signal. This
        # target is only available when all of its members fit the Prompt, and
        # validation requires the Agent to reassign every one before deleting
        # the obsolete line and its cascading snapshots.
        for line in output.storylines:
            target_id, _ = _continuation_target(line.title)
            if target_id is None or target_id not in active_ids:
                continue
            existing = db.get(Storyline, target_id)
            if existing is None:
                continue
            db.delete(existing)
        db.flush()
    used_existing_ids: set[str] = set()
    for line in output.storylines:
        line_card_ids = {member.card_id for member in line.members}
        target_id, title = _continuation_target(line.title)
        existing = None if output.decision == "split" or target_id not in active_ids else db.get(Storyline, target_id)
        if existing is not None and existing.storyline_id in used_existing_ids:
            existing = None
        if existing is None and target_id in archived_ids:
            # The exact archived title is an explicit confirmation signal from
            # the Agent under the fixed JSON contract; high similarity alone
            # never reactivates a line.
            existing = db.get(Storyline, target_id)
        if existing is None:
            storyline = Storyline(
                title=title,
                overall_start_date=min(card_dates[card_id] for card_id in line_card_ids),
                overall_end_date=max(card_dates[card_id] for card_id in line_card_ids),
                cluster_threshold=task.candidate.threshold,
                cohesion_score=task.candidate.cohesion_score,
                overall_influence_score=0.0,
                decision=output.decision,
                agent_review=line.agent_review,
                review_id=review.review_id,
                status="active",
                last_member_at=max(card_dates[card_id] for card_id in line_card_ids),
            )
            db.add(storyline)
            db.flush()
        else:
            storyline = existing
            storyline.title = title
            storyline.cluster_threshold = task.candidate.threshold
            storyline.cohesion_score = task.candidate.cohesion_score
            storyline.decision = output.decision
            storyline.agent_review = line.agent_review
            storyline.review_id = review.review_id
            storyline.status = "active"
            used_existing_ids.add(storyline.storyline_id)
        for member in line.members:
            stored = db.get(StorylineMember, (storyline.storyline_id, member.card_id))
            if stored is None:
                db.add(
                    StorylineMember(
                        storyline_id=storyline.storyline_id,
                        card_id=member.card_id,
                        at=card_dates[member.card_id],
                        membership=member.membership,
                    )
                )
            else:
                stored.at = card_dates[member.card_id]
                stored.membership = member.membership
        db.flush()
        _refresh_storyline(db, storyline, review_id=review.review_id)
    review.status = "accepted" if output.decision == "accept" else "split"
    return "accepted" if output.decision == "accept" else "split"


def process_storyline_review(*, task: StorylineReviewTask, session_factory: Callable[[], Session], llm: LlmClient) -> ReviewOutcome:
    """Generate, validate and atomically store one isolated candidate review."""
    db = session_factory()
    try:
        review = db.get(TrendStorylineReview, task.review_id)
        if review is None:
            return "failed"
        review.status = "running"
        db.commit()
    finally:
        db.close()

    output, attempts, error = _generate_review(task.candidate, llm)
    db = session_factory()
    try:
        if output is None:
            review = db.get(TrendStorylineReview, task.review_id)
            if review is None:
                return "failed"
            review.status = "failed"
            review.attempt_count = attempts
            review.error_message = error
            review.completed_at = datetime.now(timezone.utc)
            db.commit()
            return "failed"
        outcome = _store_review_result(db, task, output, attempts)
        db.commit()
        return outcome
    except Exception as exc:  # noqa: BLE001 - do not let one cluster poison accepted work
        db.rollback()
        logger.exception("failed to persist storyline review candidate=%s", task.candidate.candidate_cluster_id)
        failed = db.get(TrendStorylineReview, task.review_id)
        if failed is not None:
            failed.status = "failed"
            failed.error_message = _concise_error(exc)
            failed.completed_at = datetime.now(timezone.utc)
            db.commit()
        return "failed"
    finally:
        db.close()


class StorylineReviewController:
    """One process-wide serial fact-layer review job with pollable counters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: StorylineRuntimeState | None = None

    def snapshot(self) -> StorylineRuntimeState | None:
        with self._lock:
            return self._state

    def start(
        self,
        *,
        tasks: list[StorylineReviewTask],
        start_date: date,
        end_date: date,
        embedding_version: str,
        accepted_count: int,
        split_count: int,
        rejected_count: int,
        process: Callable[[StorylineReviewTask], ReviewOutcome],
    ) -> StorylineRuntimeState:
        with self._lock:
            if self._state is not None and self._state.is_running:
                return self._state
            now = datetime.now(timezone.utc)
            self._state = StorylineRuntimeState(
                is_running=bool(tasks), start_date=start_date, end_date=end_date,
                embedding_version=embedding_version, pending_count=len(tasks), accepted_count=accepted_count,
                split_count=split_count, rejected_count=rejected_count, failed_count=0,
                started_at=now if tasks else None, finished_at=None if tasks else now, error_message=None,
            )
            if tasks:
                threading.Thread(
                    target=self._run, kwargs={"tasks": tasks, "process": process},
                    name="trend-storyline-review", daemon=True,
                ).start()
            return self._state

    def _run(self, *, tasks: list[StorylineReviewTask], process: Callable[[StorylineReviewTask], ReviewOutcome]) -> None:
        last_error: str | None = None
        for task in tasks:
            try:
                outcome = process(task)
            except Exception as exc:  # noqa: BLE001
                logger.exception("unhandled storyline review failure")
                outcome = "failed"
                last_error = _concise_error(exc)
            with self._lock:
                assert self._state is not None
                updates: dict[str, object] = {"pending_count": self._state.pending_count - 1}
                if outcome == "accepted":
                    updates["accepted_count"] = self._state.accepted_count + 1
                elif outcome == "split":
                    updates["split_count"] = self._state.split_count + 1
                elif outcome == "rejected":
                    updates["rejected_count"] = self._state.rejected_count + 1
                else:
                    updates["failed_count"] = self._state.failed_count + 1
                self._state = replace(self._state, **updates)
        with self._lock:
            assert self._state is not None
            self._state = replace(
                self._state, is_running=False, finished_at=datetime.now(timezone.utc), error_message=last_error
            )


storyline_review_controller = StorylineReviewController()
