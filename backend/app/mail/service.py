from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Item, ItemTag, MailDelivery, MailSchedule, MailTemplate, Tag
from app.schemas import (
    MailFilterSnapshot,
    MailImmediatePreviewRequest,
    MailProviderKind,
    MailImmediateSendResponse,
    MailPreviewItem,
    MailPreviewResponse,
    MailScheduleCreateRequest,
    MailScheduleResponse,
    MailScheduleUpdateRequest,
    MailDeliveryLog,
    MailTemplateCreateRequest,
    MailTemplateResponse,
    MailTemplateUpdateRequest,
)


class MailTemplateNotFoundError(Exception):
    def __init__(self, template_id: int) -> None:
        super().__init__(f"mail template {template_id} not found")
        self.template_id = template_id


class MailScheduleNotFoundError(Exception):
    def __init__(self, schedule_id: int) -> None:
        super().__init__(f"mail schedule {schedule_id} not found")
        self.schedule_id = schedule_id


# 邮件预定发送的所有时间判定与展示统一使用北京时间（UTC+8），
# 存库的 send_time / next_run_at / last_sent_at / marker 均为北京时间墙钟值，
# 前端直接原样展示，不再做任何时区换算。
BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """当前北京时间（naive 墙钟值，便于与 send_time 字符串直接比较/入库）。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def with_send_date_suffix(subject: str, *, when: datetime | None = None) -> str:
    """Append ·yyyy-mm-dd (Beijing calendar day) for preview/send subjects.

    Templates/schedules keep the bare subject in DB; the date marks the send day
    and is applied only when rendering preview HTML or dispatching mail.
    """
    base = (subject or "").strip()
    day = (when or beijing_now()).strftime("%Y-%m-%d")
    suffix = f"·{day}"
    if base.endswith(suffix):
        return base
    # Replace a trailing ·yyyy-mm-dd from an earlier preview of another day.
    if len(base) >= 11 and base[-11] == "·" and base[-10:].replace("-", "").isdigit():
        base = base[:-11].rstrip()
    return f"{base}{suffix}" if base else suffix


def _compute_next_run(*, send_time: str, frequency: str, reference: datetime) -> datetime | None:
    """reference 应为北京时间墙钟值；返回的 next_run_at 同样是北京时间。"""
    try:
        hour_str, minute_str = send_time.split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        return None
    if reference.tzinfo is not None:
        reference = reference.astimezone(BEIJING_TZ).replace(tzinfo=None)
    candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= reference:
        candidate = candidate + timedelta(days=1)
    if frequency == "weekly":
        while candidate.weekday() != reference.weekday():
            candidate = candidate + timedelta(days=1)
    return candidate

from .provider import MailProvider
from .rendering import build_mail_preview_context, render_mail_html
from .smtp_provider import SMTPConfig, SmtpMailProvider
from .tof4_provider import Tof4Config, Tof4MailProvider


def _build_smtp_mail_provider() -> MailProvider:
    settings = get_settings()
    config: SMTPConfig = {
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_username": settings.smtp_username,
        "smtp_password": settings.smtp_password,
        "smtp_from_email": settings.smtp_from_email,
        "smtp_from_name": settings.smtp_from_name,
        "smtp_use_tls": settings.smtp_use_tls,
        "smtp_use_ssl": settings.smtp_use_ssl,
    }
    return SmtpMailProvider(config)


def _build_tof4_mail_provider() -> MailProvider:
    settings = get_settings()
    if not settings.tof4_paasid or not settings.tof4_token or not settings.tof4_url:
        raise ValueError("TOF4 mail provider is not configured")
    config: Tof4Config = {
        "paasid": settings.tof4_paasid,
        "token": settings.tof4_token,
        "url": settings.tof4_url,
        "from_email": settings.tof4_from_email or settings.smtp_from_email,
    }
    return Tof4MailProvider(config)


def resolve_mail_provider_name(requested: MailProviderKind | None = None) -> MailProviderKind:
    settings = get_settings()
    resolved = (requested or settings.mail_provider or "tof4").strip().lower()
    if resolved == "smtp":
        return "smtp"
    return "tof4"


def build_default_mail_provider(requested: MailProviderKind | None = None) -> MailProvider:
    provider_name = resolve_mail_provider_name(requested)
    if provider_name == "smtp":
        return _build_smtp_mail_provider()
    return _build_tof4_mail_provider()


def resolve_sender(provider_name: MailProviderKind) -> tuple[str, str | None]:
    settings = get_settings()
    if provider_name == "tof4":
        return (
            settings.tof4_from_email or settings.smtp_from_email,
            settings.tof4_from_name or settings.smtp_from_name,
        )
    return settings.smtp_from_email, settings.smtp_from_name


class MailService:
    def __init__(self, db: Session, provider: MailProvider | None = None) -> None:
        self._db = db
        self._provider = provider

    def normalize_filter_snapshot(self, raw: dict) -> dict:
        snapshot = MailFilterSnapshot.model_validate(raw or {})
        return snapshot.model_dump(mode="json")

    def list_templates(self) -> list[MailTemplate]:
        return list(self._db.scalars(select(MailTemplate).order_by(MailTemplate.updated_at.desc(), MailTemplate.id.desc())))

    def create_template(self, payload: MailTemplateCreateRequest) -> MailTemplate:
        now = beijing_now()
        template = MailTemplate(
            name=payload.name.strip(),
            subject=payload.subject.strip(),
            recipients_json=list(payload.recipients),
            filter_snapshot_json=self.normalize_filter_snapshot(payload.filter_snapshot.model_dump()),
            is_active=payload.is_active,
            created_at=now,
            updated_at=now,
        )
        self._db.add(template)
        self._db.commit()
        self._db.refresh(template)
        return template

    def get_template(self, template_id: int) -> MailTemplate:
        template = self._db.get(MailTemplate, template_id)
        if template is None:
            raise MailTemplateNotFoundError(template_id)
        return template

    def update_template(self, template_id: int, payload: MailTemplateUpdateRequest) -> MailTemplate:
        template = self.get_template(template_id)
        if payload.name is not None:
            template.name = payload.name.strip()
        if payload.subject is not None:
            template.subject = payload.subject.strip()
        if payload.recipients is not None:
            template.recipients_json = list(payload.recipients)
        if payload.filter_snapshot is not None:
            template.filter_snapshot_json = self.normalize_filter_snapshot(payload.filter_snapshot.model_dump())
        if payload.is_active is not None:
            template.is_active = payload.is_active
        template.updated_at = beijing_now()
        self._db.commit()
        self._db.refresh(template)
        return template

    def delete_template(self, template_id: int) -> None:
        template = self.get_template(template_id)
        # 断开投递记录与预定任务对该模版的外键引用，避免 FK 冲突；
        # 预定任务保留（filter_snapshot 已是独立快照），只是回退为「独立预定」。
        self._db.execute(
            update(MailDelivery).where(MailDelivery.template_id == template_id).values(template_id=None)
        )
        self._db.execute(
            update(MailSchedule).where(MailSchedule.template_id == template_id).values(template_id=None)
        )
        self._db.delete(template)
        self._db.commit()

    def preview_template(self, template_id: int, provider: MailProviderKind | None = None) -> MailPreviewResponse:
        template = self.get_template(template_id)
        snapshot = MailFilterSnapshot.model_validate(template.filter_snapshot_json or {})
        return self.preview_immediate_send(
            filter_snapshot=snapshot.model_dump(mode="json"),
            subject=template.subject,
            recipients=list(template.recipients_json or []),
            provider=provider,
        )

    def send_template_once(
        self, template_id: int, provider: MailProviderKind | None = None
    ) -> MailImmediateSendResponse:
        template = self.get_template(template_id)
        snapshot = MailFilterSnapshot.model_validate(template.filter_snapshot_json or {})
        preview = self.preview_immediate_send(
            filter_snapshot=snapshot.model_dump(mode="json"),
            subject=template.subject,
            recipients=list(template.recipients_json or []),
            provider=provider,
        )
        delivery = self._dispatch_delivery(
            preview=preview,
            provider=provider,
            trigger_type="manual_send",
            template_id=template.id,
            schedule_id=None,
        )
        template.last_send_at = delivery.finished_at
        template.last_send_status = delivery.status
        template.last_send_count = delivery.item_count
        self._db.commit()
        self._db.refresh(delivery)
        return MailImmediateSendResponse(
            delivery_id=delivery.id,
            provider=preview.provider,
            status=delivery.status,
            item_count=delivery.item_count,
            error_message=delivery.error_message,
        )

    def _resolve_datetime_boundaries(self, snapshot: MailFilterSnapshot) -> tuple[datetime | None, datetime | None]:
        now = datetime.now(timezone.utc)

        def _relative_delta(value: str | None) -> timedelta | None:
            if value == "24h":
                return timedelta(hours=24)
            if value == "7d":
                return timedelta(days=7)
            if value == "30d":
                return timedelta(days=30)
            return None

        after: datetime | None = None
        if snapshot.published_after_mode == "absolute" and snapshot.published_after:
            after_date = date.fromisoformat(snapshot.published_after)
            after = datetime(after_date.year, after_date.month, after_date.day, tzinfo=timezone.utc)
        elif snapshot.published_after_mode == "relative":
            delta = _relative_delta(snapshot.published_after_value)
            if delta is not None:
                after = now - delta

        before: datetime | None = None
        if snapshot.published_before_mode == "absolute" and snapshot.published_before:
            before_date = date.fromisoformat(snapshot.published_before)
            before = datetime(before_date.year, before_date.month, before_date.day, tzinfo=timezone.utc) + timedelta(days=1)
        elif snapshot.published_before_mode == "relative":
            delta = _relative_delta(snapshot.published_before_value)
            if delta is not None:
                before = now - delta

        return after, before

    @staticmethod
    def _split_filter_values(raw: str | None) -> list[str]:
        if not raw:
            return []
        values: list[str] = []
        seen: set[str] = set()
        for part in str(raw).split(","):
            value = part.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            values.append(value)
        return values

    def _build_item_stmt(self, snapshot: MailFilterSnapshot):
        stmt = select(Item)
        main_categories = self._split_filter_values(snapshot.main_category)
        if main_categories:
            stmt = stmt.where(Item.main_category.in_(main_categories))
        if snapshot.info_type:
            stmt = stmt.where(Item.info_type == snapshot.info_type)
        importances = self._split_filter_values(snapshot.importance)
        if importances:
            stmt = stmt.where(Item.importance.in_(importances))
        sub_tags = self._split_filter_values(snapshot.sub_tag)
        if sub_tags:
            stmt = stmt.where(
                Item.id.in_(
                    select(ItemTag.item_id)
                    .join(Tag, Tag.id == ItemTag.tag_id)
                    .where(Tag.kind == "sub_tag", Tag.name.in_(sub_tags))
                )
            )
        if snapshot.q:
            like = f"%{snapshot.q}%"
            stmt = stmt.where((Item.title.ilike(like)) | (Item.summary.ilike(like)))

        after, before = self._resolve_datetime_boundaries(snapshot)
        if after is not None:
            stmt = stmt.where(Item.published_at >= after)
        if before is not None:
            stmt = stmt.where(Item.published_at < before)
        return stmt

    def _fetch_items_for_snapshot(self, snapshot: MailFilterSnapshot, *, limit: int = 100) -> list[Item]:
        stmt = self._build_item_stmt(snapshot)
        sort_col = Item.published_at if snapshot.sort_by == "published_at" else Item.fetched_at
        order_clause = sort_col.desc() if snapshot.sort_dir == "desc" else sort_col.asc()
        if snapshot.sort_by == "published_at":
            order_clause = order_clause.nullslast()
        return list(self._db.scalars(stmt.order_by(order_clause).limit(limit)))

    def _extract_hotspots(self, item: Item) -> list[str]:
        hotspots = [tag.name for tag in item.tags if getattr(tag, "kind", None) == "sub_tag"]
        deduped: list[str] = []
        for hotspot in hotspots:
            if hotspot not in deduped:
                deduped.append(hotspot)
        return deduped[:5]

    def _build_preview_items(self, items: list[Item]) -> list[MailPreviewItem]:
        preview_items: list[MailPreviewItem] = []
        for item in items:
            preview_items.append(
                MailPreviewItem(
                    # 与主界面 ItemCard 一致：优先展示中文标题 title_tldr，缺失时回退原文 title
                    title=item.title_tldr or item.title,
                    reason=item.why_it_matters or item.summary or item.title_tldr,
                    summary=item.summary,
                    key_points=[str(point) for point in (item.key_points or [])],
                    hotspots=self._extract_hotspots(item),
                    source_url=item.url,
                    published_at=item.published_at.isoformat() if item.published_at else None,
                )
            )
        return preview_items

    def preview_immediate_send(
        self,
        *,
        filter_snapshot: dict,
        subject: str,
        recipients: list[str],
        provider: MailProviderKind | None = None,
    ) -> MailPreviewResponse:
        normalized = MailFilterSnapshot.model_validate(filter_snapshot or {})
        items = self._fetch_items_for_snapshot(normalized)
        preview_items = self._build_preview_items(items)
        dated_subject = with_send_date_suffix(subject)
        context = build_mail_preview_context(
            filters=normalized.model_dump(mode="json"),
            items=[item.model_dump(mode="json") for item in preview_items],
            subject=dated_subject,
        )
        rendered_html = render_mail_html(context)
        return MailPreviewResponse(
            subject=dated_subject,
            filter_snapshot=normalized,
            recipients=recipients,
            provider=resolve_mail_provider_name(provider),
            item_count=len(preview_items),
            items=preview_items,
            rendered_html=rendered_html,
        )

    def _dispatch_delivery(
        self,
        *,
        preview: MailPreviewResponse,
        provider: MailProviderKind | None,
        trigger_type: str,
        template_id: int | None,
        schedule_id: int | None,
    ) -> MailDelivery:
        delivery = MailDelivery(
            schedule_id=schedule_id,
            template_id=template_id,
            trigger_type=trigger_type,
            status="pending",
            item_count=preview.item_count,
            subject=preview.subject,
            recipients_json=list(preview.recipients),
            filter_snapshot_json=preview.filter_snapshot.model_dump(mode="json"),
            started_at=beijing_now(),
        )
        self._db.add(delivery)
        self._db.flush()

        mail_provider = build_default_mail_provider(provider)
        from_email, from_name = resolve_sender(preview.provider)
        try:
            mail_provider.send(
                subject=preview.subject,
                html=preview.rendered_html,
                recipients=list(preview.recipients),
                from_email=from_email,
                from_name=from_name,
            )
            delivery.status = "sent"
            delivery.error_message = None
        except Exception as exc:
            delivery.status = "failed"
            delivery.error_message = str(exc)
        delivery.finished_at = beijing_now()
        return delivery

    def send_immediate(self, payload: MailImmediatePreviewRequest) -> MailImmediateSendResponse:
        preview = self.preview_immediate_send(
            filter_snapshot=payload.filter_snapshot.model_dump(mode="json"),
            subject=payload.subject,
            recipients=payload.recipients,
            provider=payload.provider,
        )
        delivery = self._dispatch_delivery(
            preview=preview,
            provider=payload.provider,
            trigger_type="manual_send",
            template_id=None,
            schedule_id=None,
        )
        self._db.commit()
        self._db.refresh(delivery)
        return MailImmediateSendResponse(
            delivery_id=delivery.id,
            provider=preview.provider,
            status=delivery.status,
            item_count=delivery.item_count,
            error_message=delivery.error_message,
        )

    # --- Schedules ---

    def list_schedules(self) -> list[MailSchedule]:
        return list(
            self._db.scalars(select(MailSchedule).order_by(MailSchedule.updated_at.desc(), MailSchedule.id.desc()))
        )

    def get_schedule(self, schedule_id: int) -> MailSchedule:
        schedule = self._db.get(MailSchedule, schedule_id)
        if schedule is None:
            raise MailScheduleNotFoundError(schedule_id)
        return schedule

    def list_schedules_for_template(self, template_id: int) -> list[MailSchedule]:
        """列出关联到指定模版的所有预定任务；模版不存在则抛错。"""
        self.get_template(template_id)
        return list(
            self._db.scalars(
                select(MailSchedule)
                .where(MailSchedule.template_id == template_id)
                .order_by(MailSchedule.updated_at.desc(), MailSchedule.id.desc())
            )
        )

    def create_schedule(self, payload: MailScheduleCreateRequest) -> MailSchedule:
        now = beijing_now()
        schedule = MailSchedule(
            template_id=payload.template_id,
            name=payload.name.strip() or "未命名预定",
            subject=payload.subject.strip() or "未命名预定",
            recipients_json=list(payload.recipients),
            filter_snapshot_json=self.normalize_filter_snapshot(payload.filter_snapshot.model_dump()),
            frequency=payload.frequency,
            send_time=payload.send_time,
            enabled=payload.enabled,
            next_run_at=_compute_next_run(send_time=payload.send_time, frequency=payload.frequency, reference=now),
            created_at=now,
            updated_at=now,
        )
        self._db.add(schedule)
        self._db.commit()
        self._db.refresh(schedule)
        return schedule

    def update_schedule(self, schedule_id: int, payload: MailScheduleUpdateRequest) -> MailSchedule:
        schedule = self.get_schedule(schedule_id)
        if payload.name is not None:
            schedule.name = payload.name.strip()
        if payload.subject is not None:
            schedule.subject = payload.subject.strip()
        if payload.recipients is not None:
            schedule.recipients_json = list(payload.recipients)
        if payload.filter_snapshot is not None:
            schedule.filter_snapshot_json = self.normalize_filter_snapshot(payload.filter_snapshot.model_dump())
        if payload.frequency is not None:
            schedule.frequency = payload.frequency
        if payload.send_time is not None:
            schedule.send_time = payload.send_time
        if payload.enabled is not None:
            schedule.enabled = payload.enabled
        schedule.updated_at = beijing_now()
        schedule.next_run_at = _compute_next_run(
            send_time=schedule.send_time, frequency=schedule.frequency, reference=beijing_now()
        )
        self._db.commit()
        self._db.refresh(schedule)
        return schedule

    def set_schedule_enabled(self, schedule_id: int, enabled: bool) -> MailSchedule:
        schedule = self.get_schedule(schedule_id)
        schedule.enabled = enabled
        schedule.updated_at = beijing_now()
        if enabled:
            schedule.next_run_at = _compute_next_run(
                send_time=schedule.send_time, frequency=schedule.frequency, reference=beijing_now()
            )
        self._db.commit()
        self._db.refresh(schedule)
        return schedule

    def delete_schedule(self, schedule_id: int) -> None:
        schedule = self.get_schedule(schedule_id)
        # 先断开投递记录对该预定的外键引用，保留历史投递日志
        self._db.execute(
            update(MailDelivery).where(MailDelivery.schedule_id == schedule_id).values(schedule_id=None)
        )
        self._db.delete(schedule)
        self._db.commit()

    def run_schedule(self, schedule: MailSchedule, *, trigger_type: str, mark_today: bool) -> MailDelivery:
        snapshot = MailFilterSnapshot.model_validate(schedule.filter_snapshot_json or {})
        preview = self.preview_immediate_send(
            filter_snapshot=snapshot.model_dump(mode="json"),
            subject=schedule.subject,
            recipients=list(schedule.recipients_json or []),
            provider=None,
        )
        delivery = self._dispatch_delivery(
            preview=preview,
            provider=None,
            trigger_type=trigger_type,
            template_id=schedule.template_id,
            schedule_id=schedule.id,
        )
        now = beijing_now()
        schedule.last_sent_at = now
        schedule.last_result_status = delivery.status
        schedule.last_result_count = delivery.item_count
        if mark_today and delivery.status == "sent":
            schedule.last_sent_marker_date = now.date().isoformat()
        schedule.next_run_at = _compute_next_run(
            send_time=schedule.send_time, frequency=schedule.frequency, reference=now
        )
        self._db.commit()
        self._db.refresh(delivery)
        return delivery

    def send_schedule_now(self, schedule_id: int) -> MailImmediateSendResponse:
        schedule = self.get_schedule(schedule_id)
        delivery = self.run_schedule(schedule, trigger_type="manual_send", mark_today=True)
        return MailImmediateSendResponse(
            delivery_id=delivery.id,
            provider=resolve_mail_provider_name(None),
            status=delivery.status,
            item_count=delivery.item_count,
            error_message=delivery.error_message,
        )

    def list_schedule_logs(self, schedule_id: int, *, limit: int = 20) -> list[MailDelivery]:
        self.get_schedule(schedule_id)
        return list(
            self._db.scalars(
                select(MailDelivery)
                .where(MailDelivery.schedule_id == schedule_id)
                .order_by(MailDelivery.started_at.desc().nullslast(), MailDelivery.id.desc())
                .limit(limit)
            )
        )


def template_to_response(template: MailTemplate) -> MailTemplateResponse:
    return MailTemplateResponse(
        id=template.id,
        name=template.name,
        subject=template.subject,
        recipients=list(template.recipients_json or []),
        filter_snapshot=MailFilterSnapshot.model_validate(template.filter_snapshot_json or {}),
        is_active=template.is_active,
        last_send_at=template.last_send_at,
        last_send_status=template.last_send_status,
        last_send_count=template.last_send_count,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def schedule_to_response(schedule: MailSchedule) -> MailScheduleResponse:
    return MailScheduleResponse(
        id=schedule.id,
        template_id=schedule.template_id,
        template_name=schedule.template.name if schedule.template else None,
        name=schedule.name,
        subject=schedule.subject,
        recipients=list(schedule.recipients_json or []),
        filter_snapshot=MailFilterSnapshot.model_validate(schedule.filter_snapshot_json or {}),
        frequency=schedule.frequency if schedule.frequency in ("daily", "weekly") else "daily",
        send_time=schedule.send_time,
        enabled=schedule.enabled,
        last_sent_at=schedule.last_sent_at,
        last_result_status=schedule.last_result_status,
        last_result_count=schedule.last_result_count,
        last_sent_marker_date=schedule.last_sent_marker_date,
        next_run_at=schedule.next_run_at,
        patrol_status=schedule.patrol_status,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


def delivery_to_log(delivery: MailDelivery) -> MailDeliveryLog:
    return MailDeliveryLog(
        id=delivery.id,
        trigger_type=delivery.trigger_type,
        status=delivery.status,
        item_count=delivery.item_count,
        subject=delivery.subject,
        started_at=delivery.started_at,
        finished_at=delivery.finished_at,
        error_message=delivery.error_message,
    )
