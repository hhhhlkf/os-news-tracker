import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.db import SessionLocal
from app.discovery.recovery import (
    STALE_RUN_PATROL_INTERVAL_MINUTES,
    STALE_RUN_TIMEOUT_SECONDS,
    reclaim_stale_runs,
)
from app.enums import SourceType, Stream
from app.extract.scrapling_extractor import ScraplingExtractor
from app.fetchers.api_adapters import ApiAdapterFetcher
from app.fetchers.json_api import GenericJsonApiFetcher
from app.fetchers.page_monitor import PageMonitorFetcher
from app.fetchers.rss import RssFetcher
from app.fetchers.search import SearchFetcher
from app.models import Source
from app.pipeline import Pipeline
from app.processing.enricher import Enricher
from app.search.base import get_search_provider

logger = logging.getLogger(__name__)

SUPPORTED_NEWS_SOURCE_TYPES = (
    SourceType.RSS,
    SourceType.PAGE_MONITOR,
    SourceType.SEARCH,
    SourceType.API,
)


def build_fetcher(source: Source, extractor, search):
    """根据源类型构建对应的 Fetcher 实例。"""
    if source.type == SourceType.RSS:
        return RssFetcher()
    if source.type == SourceType.PAGE_MONITOR:
        return PageMonitorFetcher(extractor=extractor)
    if source.type == SourceType.SEARCH:
        return SearchFetcher(search=search, extractor=extractor)
    if source.type == SourceType.API:
        if source.api_config and isinstance(source.api_config.get("probe"), dict):
            return ApiAdapterFetcher()
        if source.adapter == "generic_json_list":
            return GenericJsonApiFetcher()
    raise ValueError(f"unknown source type {source.type}")


def list_enabled_news_sources(session) -> list[Source]:
    return list(
        session.scalars(
            select(Source).where(
                Source.enabled.is_(True),
                Source.stream == Stream.NEWS,
                Source.type.in_(SUPPORTED_NEWS_SOURCE_TYPES),
                (Source.type != SourceType.API)
                | (Source.adapter == "generic_json_list")
                | (Source.api_config.is_not(None) & Source.api_config["probe"].is_not(None)),
            )
        )
    )


def run_source_job(source_id: int):
    session = SessionLocal()
    try:
        source = session.get(Source, source_id)
        if not source or not source.enabled:
            return
        extractor = ScraplingExtractor(use_stealth=source.stealth)
        search = get_search_provider()
        fetcher = build_fetcher(
            source,
            extractor,
            search,
        )
        pipeline = Pipeline(session=session, extractor=extractor, enricher=Enricher())
        count = pipeline.run_source(source, fetcher=fetcher)
        logger.info("source %s produced %d new items", source.name, count)
    finally:
        session.close()


def run_startup_backfill() -> int:
    session = SessionLocal()
    try:
        source_ids = [source.id for source in list_enabled_news_sources(session)]
    finally:
        session.close()

    for source_id in source_ids:
        run_source_job(source_id)

    logger.info("startup backfill processed %d news sources", len(source_ids))
    return len(source_ids)


MAIL_SCHEDULE_TICK_INTERVAL_MINUTES = 1
MAIL_SCHEDULE_PATROL_INTERVAL_HOURS = 6
CRAWL_METHOD_REVIEW_REMINDER_TICK_INTERVAL_MINUTES = 1


def _schedule_due_now(schedule, *, now: datetime, today: str) -> bool:
    """判断某条启用中的预定发送任务此刻是否应触发。

    - 当天已发送（marker == today）则跳过
    - weekly 仅在与创建日相同的星期几触发
    - 当前时间需已到达/越过 send_time
    """
    if not schedule.enabled:
        return False
    if schedule.patrol_status in ("查询空", "empty_waiting_patrol"):
        return schedule.next_run_at is not None and now >= schedule.next_run_at
    if schedule.next_run_at is not None and now < schedule.next_run_at:
        return False
    if schedule.last_sent_marker_date == today:
        return False
    try:
        hour_str, minute_str = (schedule.send_time or "09:00").split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        return False
    if schedule.frequency == "weekly":
        anchor = schedule.created_at.weekday() if schedule.created_at else now.weekday()
        if now.weekday() != anchor:
            return False
    return (now.hour, now.minute) >= (hour, minute)


def _run_due_schedules(*, trigger_type: str, mark_today: bool, patrol: bool) -> None:
    from app.mail.service import MailService, beijing_now
    from app.models import MailSchedule

    session = SessionLocal()
    try:
        # 预定发送的判定统一按北京时间：send_time / marker 都是北京时间墙钟值
        now = beijing_now()
        today = now.date().isoformat()
        schedules = list(session.scalars(select(MailSchedule).where(MailSchedule.enabled.is_(True))))
        service = MailService(session)
        for schedule in schedules:
            if schedule.patrol_status in ("查询空", "empty_waiting_patrol") and not patrol:
                continue
            if not _schedule_due_now(schedule, now=now, today=today):
                continue
            try:
                service.run_schedule(schedule, trigger_type=trigger_type, mark_today=mark_today)
                if patrol:
                    schedule.patrol_status = "已补发"
                    session.commit()
                logger.info("mail schedule %s executed (%s)", schedule.id, trigger_type)
            except Exception:
                logger.exception("mail schedule %s failed (%s)", schedule.id, trigger_type)
    finally:
        session.close()


def run_mail_schedule_tick() -> None:
    """主任务：巡检启用中的预定发送任务，触发到点且当天未发送的任务。"""
    _run_due_schedules(trigger_type="scheduled_send", mark_today=True, patrol=False)


def run_mail_schedule_patrol() -> None:
    """巡检任务：兜底补发当天到点却漏发的任务。"""
    _run_due_schedules(trigger_type="patrol_resend", mark_today=True, patrol=True)


def run_crawl_method_review_reminder_tick() -> None:
    from app.discovery.review import send_review_reminder_if_due

    session = SessionLocal()
    try:
        result = send_review_reminder_if_due(session)
        if result.get("sent"):
            logger.info("crawl method review reminder sent: %s", result)
    except Exception:
        logger.exception("crawl method review reminder failed")
    finally:
        session.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    session = SessionLocal()
    try:
        for source in session.scalars(select(Source).where(
            Source.enabled.is_(True),
            Source.stream == Stream.NEWS.value,
            Source.type.in_([value.value for value in SUPPORTED_NEWS_SOURCE_TYPES]),
        )):
            cron = source.fetch_cron or "0 8 * * *"
            scheduler.add_job(
                run_source_job,
                CronTrigger.from_crontab(cron),
                args=[source.id],
                id=f"source-{source.id}",
            )
    finally:
        session.close()
    # 定时巡检：回收卡在 running 超过 STALE_RUN_TIMEOUT_SECONDS 的 discovery run
    # （进程活着但某条 run 的线程静默死掉时，启动回收够不到，靠这个兜底）
    scheduler.add_job(
        reclaim_stale_runs,
        IntervalTrigger(minutes=STALE_RUN_PATROL_INTERVAL_MINUTES),
        args=[STALE_RUN_TIMEOUT_SECONDS],
        id="discovery-reclaim-stale-runs",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler


def register_mail_schedule_jobs(scheduler: BackgroundScheduler) -> None:
    """在给定调度器上注册邮件预定发送的 tick / patrol 任务。

    统一巡检启用中的任务，不为每条 schedule 单独注册 job。
    """
    scheduler.add_job(
        run_mail_schedule_tick,
        IntervalTrigger(minutes=MAIL_SCHEDULE_TICK_INTERVAL_MINUTES),
        id="mail-schedule-tick",
        replace_existing=True,
    )
    scheduler.add_job(
        run_mail_schedule_patrol,
        IntervalTrigger(hours=MAIL_SCHEDULE_PATROL_INTERVAL_HOURS),
        id="mail-schedule-patrol",
        replace_existing=True,
    )
    scheduler.add_job(
        run_crawl_method_review_reminder_tick,
        IntervalTrigger(minutes=CRAWL_METHOD_REVIEW_REMINDER_TICK_INTERVAL_MINUTES),
        id="crawl-method-review-reminder-tick",
        replace_existing=True,
    )


def start_mail_scheduler() -> BackgroundScheduler:
    """轻量调度器：只跑邮件预定发送，不注册新闻源 cron / discovery 巡检。

    由独立开关 ENABLE_MAIL_SCHEDULER 控制，便于开发态只验证邮件定时功能。
    """
    scheduler = BackgroundScheduler()
    register_mail_schedule_jobs(scheduler)
    scheduler.start()
    return scheduler


MORNING_CRAWL_TICK_INTERVAL_MINUTES = 1
MORNING_CRAWL_PATROL_INTERVAL_HOURS = 3


def _run_system_morning_crawl(*, trigger_type: str, patrol: bool) -> None:
    from app.api.discussion_routes import start_discussion_pipeline_run
    from app.morning_crawl.service import (
        beijing_now,
        execute_morning_crawl,
        get_or_create_config,
        is_running,
        patrol_due_now,
        schedule_due_now,
    )

    session = SessionLocal()
    try:
        config = get_or_create_config(session)
        now = beijing_now()
        due = patrol_due_now(config, now=now) if patrol else schedule_due_now(config, now=now, today=now.date().isoformat())
        if not due:
            return
        if is_running(session):
            return
        try:
            # Technical discussions share the system schedule.  Run the
            # existing mail + GitHub pipeline to completion before collecting
            # papers and web sources, so the configured order is explicit.
            # Patrol runs retry only unfinished crawl methods and must not
            # repeatedly scan the discussion mailbox.
            if not patrol:
                start_discussion_pipeline_run(
                    session,
                    trigger_type="scheduled",
                    run_in_background=False,
                )
            execute_morning_crawl(session, trigger_type=trigger_type)
            logger.info("system morning crawl executed (%s)", trigger_type)
        except Exception:
            logger.exception("system morning crawl failed (%s)", trigger_type)
    finally:
        session.close()


def run_system_morning_crawl() -> None:
    """定时抓取主任务：到点且当天未成功、且无进行中的 run 时，执行全部 active discovery methods。"""
    _run_system_morning_crawl(trigger_type="scheduled", patrol=False)


def patrol_system_morning_crawl() -> None:
    """定时抓取巡检：兜底补跑当天到点却漏跑/未成功的定时抓取。"""
    _run_system_morning_crawl(trigger_type="patrol_resend", patrol=True)


def coordinate_discovery_migration_shadow() -> None:
    """Advance at most one no-ingestion site shadow per scheduler tick."""
    from app.discovery.migration import coordinate_next_migration_shadow

    try:
        result = coordinate_next_migration_shadow()
        if result is not None:
            logger.info("Discovery migration shadow coordinated: %s", result)
    except Exception:
        logger.exception("Discovery migration shadow coordination failed")


def register_morning_crawl_jobs(scheduler: BackgroundScheduler) -> None:
    scheduler.add_job(
        coordinate_discovery_migration_shadow,
        IntervalTrigger(minutes=MORNING_CRAWL_TICK_INTERVAL_MINUTES),
        id="discovery-migration-shadow-tick",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_system_morning_crawl,
        IntervalTrigger(minutes=MORNING_CRAWL_TICK_INTERVAL_MINUTES),
        id="system-morning-crawl-tick",
        replace_existing=True,
    )
    scheduler.add_job(
        patrol_system_morning_crawl,
        IntervalTrigger(hours=MORNING_CRAWL_PATROL_INTERVAL_HOURS),
        id="system-morning-crawl-patrol",
        replace_existing=True,
    )


def start_morning_crawl_scheduler() -> BackgroundScheduler:
    """轻量调度器：只跑系统定时抓取 tick / patrol，由独立开关 ENABLE_MORNING_CRAWL_SCHEDULER 控制。

    与 ENABLE_SCHEDULER（新闻源 cron）解耦，便于开发态单独验证定时抓取。
    """
    scheduler = BackgroundScheduler()
    register_morning_crawl_jobs(scheduler)
    scheduler.start()
    return scheduler


def start_trend_scheduler() -> BackgroundScheduler:
    """趋势定时任务调度器：只调用 trends 模块的公开启动入口。

    规则解析、去重、事实层编排等趋势业务判断全部留在 app/trends/scheduler.py，
    这里不承载任何趋势逻辑；由独立开关 ENABLE_TREND_SCHEDULER 控制。
    """
    from app.trends.scheduler import start_trend_schedule_scheduler

    return start_trend_schedule_scheduler()
