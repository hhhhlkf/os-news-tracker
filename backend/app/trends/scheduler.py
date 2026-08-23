"""Global scheduled trend runs.

All schedule business logic lives here: weekly rule parsing, the persistent
per-instant dedupe claim, serial fact-layer catch-up and the workbench status
projection. The existing ``app.scheduler`` only calls the public start entry.

The rule is interpreted in Beijing wall-clock time, matching the mail and
morning-crawl schedulers, and every planned instant is claimed in
``trend_schedule_runs`` before any work starts so a process restart cannot run
the same instant twice.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, TypeVar

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.trends.models import TrendScheduleRun
from app.trends.repository import TrendRepository
from app.trends.schemas import (
    TrendRunTriggerRequest,
    TrendScheduleRunResponse,
    TrendScheduleStatusResponse,
)
from app.trends.service import TrendService

logger = logging.getLogger(__name__)

BEIJING_TZ = timezone(timedelta(hours=8))
TREND_SCHEDULE_TICK_INTERVAL_MINUTES = 1
STAGE_POLL_SECONDS = 3.0
STAGE_TIMEOUT_SECONDS = 4 * 60 * 60

WEEKDAY_INDEXES: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
WEEKDAY_LABELS: dict[str, str] = {
    "monday": "每周一",
    "tuesday": "每周二",
    "wednesday": "每周三",
    "thursday": "每周四",
    "friday": "每周五",
    "saturday": "每周六",
    "sunday": "每周日",
}
_WEEKLY_RULE_PATTERN = re.compile(rf"^weekly_({'|'.join(WEEKDAY_INDEXES)})_(\d{{2}}):(\d{{2}})$")
_DAILY_RULE_PATTERN = re.compile(r"^daily_(\d{2}):(\d{2})$")

STAGE_CARDS = "新闻卡片"
STAGE_VECTORS = "事件核心向量"
STAGE_CLUSTERS = "候选簇"
STAGE_STORYLINES = "故事线审查"
STAGE_TREND_RUN = "模板趋势运行"

T = TypeVar("T")


def beijing_now() -> datetime:
    """Current Beijing time as a naive wall-clock value."""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


class TrendFactLayerError(Exception):
    """The fact layer could not be completed, so no template run is created."""


@dataclass(frozen=True)
class WeeklyScheduleRule:
    weekday_name: str
    weekday: int
    hour: int
    minute: int

    @property
    def label(self) -> str:
        return f"{WEEKDAY_LABELS[self.weekday_name]} {self.hour:02d}:{self.minute:02d}（北京时间）"


@dataclass(frozen=True)
class DailyScheduleRule:
    hour: int
    minute: int

    @property
    def label(self) -> str:
        return f"每日 {self.hour:02d}:{self.minute:02d}（北京时间）"


ScheduleRule = WeeklyScheduleRule | DailyScheduleRule


@dataclass(frozen=True)
class ScheduledRunOutcome:
    status: str
    stage: str
    detail: str | None
    trend_run_id: str | None = None


@dataclass(frozen=True)
class TrendScheduleRuntimeState:
    schedule_run_id: str
    template_id: str
    scheduled_for: datetime
    is_running: bool
    stage: str


def parse_schedule_rule(rule: str | None) -> ScheduleRule | None:
    """Parse the daily or weekly rule produced by the settings UI."""
    normalized = (rule or "").strip()
    weekly_match = _WEEKLY_RULE_PATTERN.match(normalized)
    daily_match = _DAILY_RULE_PATTERN.match(normalized)
    if weekly_match is None and daily_match is None:
        return None
    if weekly_match is not None:
        weekday_name, hour_text, minute_text = weekly_match.groups()
    else:
        hour_text, minute_text = daily_match.groups()
    hour, minute = int(hour_text), int(minute_text)
    if hour > 23 or minute > 59:
        return None
    if daily_match is not None:
        return DailyScheduleRule(hour=hour, minute=minute)
    return WeeklyScheduleRule(
        weekday_name=weekday_name,
        weekday=WEEKDAY_INDEXES[weekday_name],
        hour=hour,
        minute=minute,
    )


def resolve_due_slot(rule: ScheduleRule, *, now: datetime) -> datetime | None:
    """Return the planned instant this tick belongs to, or None when not due.

    Catch-up stays inside the planned weekday so an outage during the exact
    minute still runs once later that day, while the persistent claim keeps the
    instant single-shot.
    """
    if isinstance(rule, WeeklyScheduleRule) and now.weekday() != rule.weekday:
        return None
    if (now.hour, now.minute) < (rule.hour, rule.minute):
        return None
    return now.replace(hour=rule.hour, minute=rule.minute, second=0, microsecond=0)


def resolve_next_run_at(rule: ScheduleRule, *, now: datetime) -> datetime:
    candidate = now.replace(hour=rule.hour, minute=rule.minute, second=0, microsecond=0)
    if isinstance(rule, WeeklyScheduleRule):
        candidate += timedelta(days=(rule.weekday - now.weekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7 if isinstance(rule, WeeklyScheduleRule) else 1)
    return candidate


def _concise_error(exc: Exception) -> str:
    return (str(exc).strip() or exc.__class__.__name__)[:700]


def _with_service(session_factory: Callable[[], Session], action: Callable[[TrendService], T]) -> T:
    db = session_factory()
    try:
        return action(TrendService(db))
    finally:
        db.close()


def _wait_while(is_running: Callable[[], bool], *, stage: str) -> None:
    deadline = time.monotonic() + STAGE_TIMEOUT_SECONDS
    while is_running():
        if time.monotonic() > deadline:
            raise TrendFactLayerError(f"{stage} 等待超时，本次定时趋势运行中止。")
        time.sleep(STAGE_POLL_SECONDS)


def prepare_fact_layer(
    *,
    session_factory: Callable[[], Session],
    on_stage: Callable[[str], None],
) -> list[str]:
    """Reuse or sequentially complete the fact layer before a template run.

    Every step goes through the existing public ``TrendService`` methods, so the
    review idempotency of stage five and the atomic publication of stage six are
    preserved. A step already running is waited on rather than started again.
    """
    notes: list[str] = []

    on_stage(STAGE_CARDS)
    cards = _with_service(session_factory, lambda service: service.get_card_stage_status())
    if cards.is_running:
        notes.append("新闻卡片补齐任务已在执行，本次定时任务复用并等待。")
    elif cards.pending_count > 0:
        _with_service(session_factory, lambda service: service.start_card_backfill())
    _wait_while(
        lambda: _with_service(session_factory, lambda service: service.get_card_stage_status()).is_running,
        stage=STAGE_CARDS,
    )
    cards = _with_service(session_factory, lambda service: service.get_card_stage_status())
    if cards.failed_count:
        notes.append(f"新闻卡片仍有 {cards.failed_count} 条失败，已按现有成功卡片继续。")

    on_stage(STAGE_VECTORS)
    vectors = _with_service(session_factory, lambda service: service.get_vector_stage_status())
    if vectors.is_running:
        notes.append("向量补齐任务已在执行，本次定时任务复用并等待。")
    elif vectors.pending_count > 0:
        _with_service(session_factory, lambda service: service.start_vector_backfill())
    _wait_while(
        lambda: _with_service(session_factory, lambda service: service.get_vector_stage_status()).is_running,
        stage=STAGE_VECTORS,
    )
    vectors = _with_service(session_factory, lambda service: service.get_vector_stage_status())
    if vectors.pending_count or vectors.failed_count:
        raise TrendFactLayerError(
            f"向量补齐未完成（待生成 {vectors.pending_count} 条，失败 {vectors.failed_count} 条）；"
            "候选聚类不会使用不完整向量，本次定时趋势运行跳过。"
        )

    on_stage(STAGE_CLUSTERS)
    clusters = _with_service(session_factory, lambda service: service.get_cluster_stage_status())
    if clusters.candidate_cluster_count == 0 or clusters.pending_match_count > 0:
        try:
            clusters = _with_service(session_factory, lambda service: service.run_candidate_clustering())
        except ValueError as exc:
            raise TrendFactLayerError(_concise_error(exc)) from exc
    else:
        notes.append("候选簇已就绪，本次定时任务直接复用。")

    on_stage(STAGE_STORYLINES)
    storylines = _with_service(session_factory, lambda service: service.get_storyline_stage_status())
    if storylines.is_running:
        notes.append("故事线审查任务已在执行，本次定时任务复用并等待。")
    elif storylines.pending_count > 0:
        _with_service(session_factory, lambda service: service.start_storyline_review())
    _wait_while(
        lambda: _with_service(
            session_factory, lambda service: service.get_storyline_stage_status()
        ).is_running,
        stage=STAGE_STORYLINES,
    )
    storylines = _with_service(session_factory, lambda service: service.get_storyline_stage_status())
    if storylines.failed_count:
        notes.append(f"故事线审查有 {storylines.failed_count} 个候选簇失败，已按既有故事线继续。")
    return notes


def execute_scheduled_trend_run(
    *,
    template_id: str,
    session_factory: Callable[[], Session],
    on_stage: Callable[[str], None],
) -> ScheduledRunOutcome:
    """Complete the fact layer, then run the template trend evaluation once."""
    reached = {"stage": STAGE_CARDS}

    def track_stage(stage: str) -> None:
        reached["stage"] = stage
        on_stage(stage)

    try:
        notes = prepare_fact_layer(session_factory=session_factory, on_stage=track_stage)
    except TrendFactLayerError as exc:
        return ScheduledRunOutcome(status="skipped", stage=reached["stage"], detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - a broken fact layer must not publish
        logger.exception("scheduled trend fact layer failed template_id=%s", template_id)
        return ScheduledRunOutcome(
            status="failed",
            stage=reached["stage"],
            detail=f"事实层补齐失败：{_concise_error(exc)}；未覆盖该模板最近成功发布。",
        )

    on_stage(STAGE_TREND_RUN)
    active_run_id = _with_service(
        session_factory, lambda service: service.get_active_trend_run_id(template_id)
    )
    if active_run_id is not None:
        return ScheduledRunOutcome(
            status="reused",
            stage=STAGE_TREND_RUN,
            detail=_join_notes(notes, "该模板已有运行中的趋势任务，本次定时触发复用现有运行，未重复创建。"),
            trend_run_id=active_run_id,
        )

    try:
        started = _with_service(
            session_factory,
            lambda service: service.start_trend_run(TrendRunTriggerRequest(template_id=template_id)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("scheduled trend run could not start template_id=%s", template_id)
        return ScheduledRunOutcome(
            status="failed",
            stage=STAGE_TREND_RUN,
            detail=_join_notes(notes, f"趋势运行未能启动：{_concise_error(exc)}"),
        )

    run_id = started.run_id
    if run_id is None:
        return ScheduledRunOutcome(
            status="failed",
            stage=STAGE_TREND_RUN,
            detail=_join_notes(notes, "趋势运行未返回 run_id，本次定时任务未发布结果。"),
        )
    if started.reusable:
        return ScheduledRunOutcome(
            status="reused",
            stage=STAGE_TREND_RUN,
            detail=_join_notes(notes, started.message or "复用该模板正在执行的趋势运行。"),
            trend_run_id=run_id,
        )

    _wait_while(
        lambda: _with_service(session_factory, lambda service: service.get_trend_run(run_id)).is_running,
        stage=STAGE_TREND_RUN,
    )
    final = _with_service(session_factory, lambda service: service.get_trend_run(run_id))
    if final.status == "succeeded":
        return ScheduledRunOutcome(
            status="succeeded",
            stage=STAGE_TREND_RUN,
            detail=_join_notes(notes, f"已发布 {final.result_count} 条趋势结果。"),
            trend_run_id=run_id,
        )
    return ScheduledRunOutcome(
        status="failed",
        stage=STAGE_TREND_RUN,
        detail=_join_notes(
            notes,
            final.error_message or "趋势运行未成功完成；最近成功发布保持不变。",
        ),
        trend_run_id=run_id,
    )


def _join_notes(notes: list[str], message: str) -> str:
    return " ".join([*notes, message]).strip()


class TrendScheduleController:
    """Single background instance so scheduled runs never overlap."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: TrendScheduleRuntimeState | None = None

    def snapshot(self) -> TrendScheduleRuntimeState | None:
        with self._lock:
            return self._state

    def is_running(self) -> bool:
        state = self.snapshot()
        return state is not None and state.is_running

    def start(
        self,
        *,
        schedule_run_id: str,
        template_id: str,
        scheduled_for: datetime,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> bool:
        with self._lock:
            if self._state is not None and self._state.is_running:
                return False
            self._state = TrendScheduleRuntimeState(
                schedule_run_id=schedule_run_id,
                template_id=template_id,
                scheduled_for=scheduled_for,
                is_running=True,
                stage=STAGE_CARDS,
            )
            threading.Thread(
                target=self._run,
                kwargs={
                    "schedule_run_id": schedule_run_id,
                    "template_id": template_id,
                    "session_factory": session_factory,
                },
                name="trend-schedule-run",
                daemon=True,
            ).start()
            return True

    def _set_stage(self, stage: str) -> None:
        with self._lock:
            if self._state is None:
                return
            self._state = replace(self._state, stage=stage)

    def _run(
        self,
        *,
        schedule_run_id: str,
        template_id: str,
        session_factory: Callable[[], Session],
    ) -> None:
        try:
            outcome = execute_scheduled_trend_run(
                template_id=template_id,
                session_factory=session_factory,
                on_stage=self._set_stage,
            )
        except Exception as exc:  # noqa: BLE001 - never leave the slot stuck as running
            logger.exception("scheduled trend run crashed schedule_run_id=%s", schedule_run_id)
            outcome = ScheduledRunOutcome(
                status="failed",
                stage=STAGE_TREND_RUN,
                detail=f"定时趋势任务异常终止：{_concise_error(exc)}",
            )
        _finalize_schedule_run(
            schedule_run_id=schedule_run_id,
            outcome=outcome,
            session_factory=session_factory,
        )
        logger.info(
            "scheduled trend run finished schedule_run_id=%s status=%s stage=%s",
            schedule_run_id,
            outcome.status,
            outcome.stage,
        )
        with self._lock:
            if self._state is not None and self._state.schedule_run_id == schedule_run_id:
                self._state = replace(self._state, is_running=False, stage=outcome.stage)


trend_schedule_controller = TrendScheduleController()


def _finalize_schedule_run(
    *,
    schedule_run_id: str,
    outcome: ScheduledRunOutcome,
    session_factory: Callable[[], Session],
) -> None:
    db = session_factory()
    try:
        row = TrendRepository(db).get_schedule_run(schedule_run_id)
        if row is None:
            return
        row.status = outcome.status
        row.stage = outcome.stage
        row.detail = outcome.detail
        row.trend_run_id = outcome.trend_run_id
        row.finished_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:  # noqa: BLE001 - bookkeeping must not mask the run result
        db.rollback()
        logger.exception("failed to persist scheduled trend run outcome id=%s", schedule_run_id)
    finally:
        db.close()


def resolve_skip_reason(
    *,
    trigger_mode: str,
    rule: ScheduleRule | None,
    scheduled_template_id: str | None,
) -> str | None:
    if trigger_mode != "scheduled":
        return "触发方式为手动，全局定时趋势任务未启用。"
    if rule is None:
        return "定时规则缺失或格式不合法，无法解析每日或每周执行时刻，定时任务不会运行。"
    if not scheduled_template_id:
        return "未选择当前定时模板，定时任务不会运行。"
    return None


@dataclass(frozen=True)
class ClaimedScheduleSlot:
    schedule_run_id: str
    template_id: str
    scheduled_for: datetime


def claim_due_slot(session_factory: Callable[[], Session]) -> ClaimedScheduleSlot | None:
    """Persist the claim for the currently due instant, or return None.

    The unique ``(schedule_rule, scheduled_for)`` constraint is the dedupe: a
    losing insert means this planned instant already ran, including across a
    process restart within the same minute.
    """
    db = session_factory()
    try:
        service = TrendService(db)
        repository = TrendRepository(db)
        settings = service.get_settings()
        rule = parse_schedule_rule(settings.schedule_rule)
        skip_reason = resolve_skip_reason(
            trigger_mode=settings.trigger_mode,
            rule=rule,
            scheduled_template_id=settings.scheduled_template_id,
        )
        if rule is None or settings.trigger_mode != "scheduled":
            logger.debug("trend schedule tick skipped: %s", skip_reason)
            return None
        slot = resolve_due_slot(rule, now=beijing_now())
        if slot is None:
            return None
        if trend_schedule_controller.is_running():
            logger.info("trend schedule slot %s reused: a scheduled run is still executing", slot)
            return None

        template_id = settings.scheduled_template_id
        window: tuple[date, date] | None = None
        if template_id:
            try:
                window = service.resolve_active_window()
            except ValueError as exc:
                skip_reason = f"无法解析全局日期窗口：{_concise_error(exc)}"
        now_utc = datetime.now(timezone.utc)
        claimed = TrendScheduleRun(
            schedule_rule=settings.schedule_rule or rule.label,
            scheduled_for=slot,
            template_id=template_id,
            status="skipped" if skip_reason else "running",
            stage=None if skip_reason else STAGE_CARDS,
            detail=skip_reason,
            window_start_date=window[0] if window else None,
            window_end_date=window[1] if window else None,
            trend_count=settings.trend_count,
            storyline_candidate_goal=settings.storyline_candidate_goal,
            started_at=None if skip_reason else now_utc,
            finished_at=now_utc if skip_reason else None,
        )
        repository.add_schedule_run(claimed)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
        if skip_reason or template_id is None:
            logger.info("trend schedule slot %s skipped: %s", slot, skip_reason)
            return None
        return ClaimedScheduleSlot(
            schedule_run_id=claimed.schedule_run_id,
            template_id=template_id,
            scheduled_for=slot,
        )
    finally:
        db.close()


def run_trend_schedule_tick(session_factory: Callable[[], Session] = SessionLocal) -> None:
    """Patrol the single global rule once per minute and start a due instant."""
    claim = claim_due_slot(session_factory)
    if claim is None:
        return
    if not trend_schedule_controller.start(
        schedule_run_id=claim.schedule_run_id,
        template_id=claim.template_id,
        scheduled_for=claim.scheduled_for,
        session_factory=session_factory,
    ):
        _finalize_schedule_run(
            schedule_run_id=claim.schedule_run_id,
            outcome=ScheduledRunOutcome(
                status="reused",
                stage=STAGE_TREND_RUN,
                detail="已有定时趋势任务在执行，本次计划时刻复用现有任务。",
            ),
            session_factory=session_factory,
        )
        return
    logger.info(
        "trend schedule slot %s claimed for template %s",
        claim.scheduled_for,
        claim.template_id,
    )


def reclaim_stale_schedule_runs(session_factory: Callable[[], Session] = SessionLocal) -> int:
    """Fail schedule rows left ``running`` by a crashed or restarted process."""
    db = session_factory()
    try:
        rows = TrendRepository(db).list_running_schedule_runs()
        for row in rows:
            row.status = "failed"
            row.detail = (
                row.detail
                or "定时趋势任务在进程重启后被回收；未发布不完整结果，最近成功发布保持不变。"
            )
            row.finished_at = datetime.now(timezone.utc)
        if rows:
            db.commit()
        return len(rows)
    except Exception:  # noqa: BLE001 - startup must not fail on schedule bookkeeping
        db.rollback()
        logger.exception("failed to reclaim stale trend schedule runs")
        return 0
    finally:
        db.close()


def _schedule_run_response(
    row: TrendScheduleRun, *, template_name: str | None
) -> TrendScheduleRunResponse:
    return TrendScheduleRunResponse(
        schedule_run_id=row.schedule_run_id,
        schedule_rule=row.schedule_rule,
        scheduled_for=row.scheduled_for,
        template_id=row.template_id,
        template_name=template_name,
        trend_run_id=row.trend_run_id,
        status=row.status,  # type: ignore[arg-type]
        stage=row.stage,
        detail=row.detail,
        window_start_date=row.window_start_date,
        window_end_date=row.window_end_date,
        trend_count=row.trend_count,
        storyline_candidate_goal=row.storyline_candidate_goal,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def build_schedule_status(db: Session) -> TrendScheduleStatusResponse:
    """Project the saved rule, its next instant and its last outcome."""
    service = TrendService(db)
    repository = TrendRepository(db)
    settings = service.get_settings()
    rule = parse_schedule_rule(settings.schedule_rule)
    skip_reason = resolve_skip_reason(
        trigger_mode=settings.trigger_mode,
        rule=rule,
        scheduled_template_id=settings.scheduled_template_id,
    )
    enabled = skip_reason is None
    scheduled_template = (
        repository.get_identity_template(settings.scheduled_template_id)
        if settings.scheduled_template_id
        else None
    )
    last_row = repository.get_latest_schedule_run(schedule_rule=settings.schedule_rule)
    if last_row is None:
        last_row = repository.get_latest_schedule_run()
    last_template = (
        repository.get_identity_template(last_row.template_id)
        if last_row is not None and last_row.template_id
        else None
    )
    runtime = trend_schedule_controller.snapshot()
    is_running = runtime is not None and runtime.is_running
    return TrendScheduleStatusResponse(
        enabled=enabled,
        trigger_mode=settings.trigger_mode,  # type: ignore[arg-type]
        schedule_rule=settings.schedule_rule,
        schedule_rule_label=rule.label if rule else None,
        scheduled_template_id=settings.scheduled_template_id,
        scheduled_template_name=scheduled_template.name if scheduled_template else None,
        is_running=is_running,
        current_stage=runtime.stage if is_running and runtime else None,
        next_run_at=resolve_next_run_at(rule, now=beijing_now()) if enabled and rule else None,
        skip_reason=skip_reason,
        last_run=(
            _schedule_run_response(last_row, template_name=last_template.name if last_template else None)
            if last_row is not None
            else None
        ),
        message=(
            "定时趋势任务正在执行。"
            if is_running
            else "已启用全局定时趋势任务。"
            if enabled
            else skip_reason
        ),
    )


def register_trend_schedule_jobs(scheduler: BackgroundScheduler) -> None:
    scheduler.add_job(
        run_trend_schedule_tick,
        IntervalTrigger(minutes=TREND_SCHEDULE_TICK_INTERVAL_MINUTES),
        id="trend-schedule-tick",
        replace_existing=True,
    )


def start_trend_schedule_scheduler() -> BackgroundScheduler:
    """Public start entry the existing app scheduler registers."""
    reclaim_stale_schedule_runs()
    scheduler = BackgroundScheduler()
    register_trend_schedule_jobs(scheduler)
    scheduler.start()
    return scheduler
