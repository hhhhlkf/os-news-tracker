from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from sqlalchemy import String, case, cast, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.discovery.quality_audit import calculate_overall_score
from app.models import (
    CrawlMethod,
    DiscussionGroup,
    DiscussionGroupThread,
    DiscussionMessage,
    Entity,
    Item,
    ItemEntity,
    ItemSource,
    ItemTag,
    MailDelivery,
    MailNoticeConfig,
    MailSchedule,
    MailTemplate,
    Source,
    Tag,
)
from app.discussions.visibility import visible_item_clause
from app.schemas import (
    MailFilterSnapshot,
    MailImmediatePreviewRequest,
    MailNoticeBlock,
    MailNoticeConfigResponse,
    MailNoticeConfigUpdateRequest,
    MailProviderKind,
    MailImmediateSendResponse,
    MailPreviewItem,
    MailPreviewResponse,
    MailTrendDirectionGroup,
    MailTrendPreviewRequest,
    MailTrendSummary,
    MailScheduleCreateRequest,
    MailScheduleResponse,
    MailScheduleUpdateRequest,
    MailDeliveryLog,
    MailTemplateCreateRequest,
    MailTemplateResponse,
    MailTemplateUpdateRequest,
)
from app.trends.service import TrendService
from app.trends.models import TrendIdentityTemplate
from app.trends.palette import trend_direction_sort_key


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
EMPTY_SCHEDULE_RETRY_DELAY = timedelta(hours=6)


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


def _compute_next_run(
    *, send_time: str, frequency: str, weekly_day: int | None, reference: datetime
) -> datetime | None:
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
        target_day = weekly_day if weekly_day is not None else reference.weekday()
        while candidate.weekday() != target_day:
            candidate = candidate + timedelta(days=1)
    return candidate

from .provider import MailProvider
from .rendering import build_mail_preview_context, render_mail_html, render_trend_mail_html
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


def get_or_create_notice_config(db: Session) -> MailNoticeConfig:
    config = db.scalar(select(MailNoticeConfig).order_by(MailNoticeConfig.id).limit(1))
    if config is not None:
        return config
    now = beijing_now()
    config = MailNoticeConfig(
        doc_text="",
        website_url="",
        include_on_send=False,
        include_on_template=False,
        created_at=now,
        updated_at=now,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


def notice_config_to_response(config: MailNoticeConfig) -> MailNoticeConfigResponse:
    return MailNoticeConfigResponse(
        doc_text=config.doc_text or "",
        website_url=config.website_url or "",
        include_on_send=bool(config.include_on_send),
        include_on_template=bool(config.include_on_template),
    )


def _notice_payload(doc_text: str, website_url: str, *, include: bool) -> dict:
    return {
        "include": include,
        "doc_text": (doc_text or "").strip(),
        "website_url": (website_url or "").strip(),
    }


def _active_notice_block(raw: dict | None) -> MailNoticeBlock | None:
    if not isinstance(raw, dict) or not raw.get("include"):
        return None
    doc_text = str(raw.get("doc_text") or "").strip()
    website_url = str(raw.get("website_url") or "").strip()
    if not doc_text and not website_url:
        return None
    return MailNoticeBlock(doc_text=doc_text, website_url=website_url)


def _snapshot_notice_from_config(config: MailNoticeConfig, *, include: bool) -> dict:
    return _notice_payload(config.doc_text or "", config.website_url or "", include=include)


def _website_url_for_item_links(
    *,
    stored_notice: dict | None,
    apply_send_config: bool,
    db: Session,
) -> str:
    """Return the site root used to turn in-mail relative links into URLs."""
    if isinstance(stored_notice, dict):
        return str(stored_notice.get("website_url") or "").strip()
    if not apply_send_config:
        return ""
    return str(get_or_create_notice_config(db).website_url or "").strip()


def _mail_item_source_url(*, item_url: str | None, item_id: int, website_url: str) -> str:
    """Make an item link usable outside the site, including in email clients."""
    candidate = (item_url or f"/?item={item_id}").strip()
    parsed_item_url = urlparse(candidate)
    if parsed_item_url.scheme or parsed_item_url.netloc:
        return candidate

    parsed_website_url = urlparse(website_url)
    if parsed_website_url.scheme not in {"http", "https"} or not parsed_website_url.netloc:
        return candidate

    # Treat the configured website URL as the application root.  Stripping
    # both boundaries prevents `//` when the setting and item path both carry
    # a slash, while still retaining a configured deployment subpath.
    website_root = f"{website_url.rstrip('/')}/"
    return urljoin(website_root, candidate.lstrip("/"))


class MailService:
    def __init__(self, db: Session, provider: MailProvider | None = None) -> None:
        self._db = db
        self._provider = provider

    def normalize_filter_snapshot(self, raw: dict) -> dict:
        snapshot = MailFilterSnapshot.model_validate(raw or {})
        return snapshot.model_dump(mode="json")

    def get_notice_config(self) -> MailNoticeConfig:
        return get_or_create_notice_config(self._db)

    def update_notice_config(self, payload: MailNoticeConfigUpdateRequest) -> MailNoticeConfig:
        config = get_or_create_notice_config(self._db)
        if payload.doc_text is not None:
            config.doc_text = payload.doc_text.strip()[:4000]
        if payload.website_url is not None:
            config.website_url = payload.website_url.strip()[:2048]
        if payload.include_on_send is not None:
            config.include_on_send = payload.include_on_send
        if payload.include_on_template is not None:
            config.include_on_template = payload.include_on_template
        now = beijing_now()
        config.updated_at = now

        # Notice content is a delivery snapshot so that preview and scheduled
        # delivery need no additional configuration lookup.  Keep every
        # existing snapshot aligned when the singleton changes: otherwise old
        # templates and schedules continue to render stale header content.
        snapshot = _snapshot_notice_from_config(
            config,
            include=bool(config.include_on_template),
        )
        for template in self._db.scalars(select(MailTemplate)):
            template.notice_json = snapshot.copy()
            template.updated_at = now
        for schedule in self._db.scalars(select(MailSchedule)):
            schedule.notice_json = snapshot.copy()
            schedule.updated_at = now

        self._db.commit()
        self._db.refresh(config)
        return config

    def _resolve_notice(
        self,
        *,
        stored: dict | None,
        apply_send_config: bool,
    ) -> MailNoticeBlock | None:
        if stored is not None:
            return _active_notice_block(stored)
        if not apply_send_config:
            return None
        config = get_or_create_notice_config(self._db)
        if not config.include_on_send:
            return None
        return _active_notice_block(_snapshot_notice_from_config(config, include=True))

    def list_templates(self) -> list[MailTemplate]:
        return list(self._db.scalars(select(MailTemplate).order_by(MailTemplate.updated_at.desc(), MailTemplate.id.desc())))

    def create_template(self, payload: MailTemplateCreateRequest) -> MailTemplate:
        now = beijing_now()
        config = get_or_create_notice_config(self._db)
        trend_identity_template_id: str | None = None
        if payload.content_type == "trend_distribution":
            trend_identity_template_id = payload.trend_identity_template_id
            if not trend_identity_template_id:
                raise ValueError("趋势分发模板必须选择身份模板")
            if self._db.get(TrendIdentityTemplate, trend_identity_template_id) is None:
                raise ValueError("趋势身份模板不存在")
        template = MailTemplate(
            name=payload.name.strip(),
            subject=payload.subject.strip(),
            recipients_json=list(payload.recipients),
            filter_snapshot_json=self.normalize_filter_snapshot(payload.filter_snapshot.model_dump()),
            content_type=payload.content_type,
            trend_identity_template_id=trend_identity_template_id,
            notice_json=_snapshot_notice_from_config(config, include=bool(config.include_on_template)),
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
        stored_notice = template.notice_json if isinstance(template.notice_json, dict) else None
        if template.content_type == "trend_distribution":
            if not template.trend_identity_template_id:
                raise ValueError("趋势分发模板缺少身份模板")
            return self.preview_trend_distribution(
                MailTrendPreviewRequest(
                    template_id=template.trend_identity_template_id,
                    subject=template.subject,
                    recipients=list(template.recipients_json or []),
                    provider=provider,
                ),
                stored_notice=stored_notice,
                apply_send_config=False,
            )
        snapshot = MailFilterSnapshot.model_validate(template.filter_snapshot_json or {})
        return self.preview_immediate_send(
            filter_snapshot=snapshot.model_dump(mode="json"),
            subject=template.subject,
            recipients=list(template.recipients_json or []),
            provider=provider,
            stored_notice=stored_notice,
            apply_send_config=False,
        )

    def send_template_once(
        self, template_id: int, provider: MailProviderKind | None = None
    ) -> MailImmediateSendResponse:
        template = self.get_template(template_id)
        preview = self.preview_template(template_id, provider=provider)
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

    def _resolve_datetime_boundaries(
        self,
        *,
        after_mode: str,
        after_value: str | None,
        after_date_value: str | None,
        before_mode: str,
        before_value: str | None,
        before_date_value: str | None,
    ) -> tuple[datetime | None, datetime | None]:
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
        if after_mode == "absolute" and after_date_value:
            after_date = date.fromisoformat(after_date_value)
            after = datetime(after_date.year, after_date.month, after_date.day, tzinfo=timezone.utc)
        elif after_mode == "relative":
            delta = _relative_delta(after_value)
            if delta is not None:
                after = now - delta

        before: datetime | None = None
        if before_mode == "absolute" and before_date_value:
            before_date = date.fromisoformat(before_date_value)
            before = datetime(before_date.year, before_date.month, before_date.day, tzinfo=timezone.utc) + timedelta(days=1)
        elif before_mode == "relative":
            delta = _relative_delta(before_value)
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
            normalized = value.casefold()
            if not value or normalized in seen:
                continue
            seen.add(normalized)
            values.append(value)
        return values

    @staticmethod
    def _search_terms(snapshot: MailFilterSnapshot) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()
        for candidate in [snapshot.q, *snapshot.keywords]:
            value = (candidate or "").strip()
            normalized = value.casefold()
            if not value or normalized in seen:
                continue
            seen.add(normalized)
            terms.append(value)
        return terms

    @staticmethod
    def _item_matches_search_term(term: str):
        source_matches = or_(
            Source.name.icontains(term, autoescape=True),
            Source.url.icontains(term, autoescape=True),
            Source.vendor.icontains(term, autoescape=True),
        )
        return or_(
            Item.title.icontains(term, autoescape=True),
            Item.title_tldr.icontains(term, autoescape=True),
            Item.summary.icontains(term, autoescape=True),
            Item.url.icontains(term, autoescape=True),
            Item.main_category.icontains(term, autoescape=True),
            Item.info_type.icontains(term, autoescape=True),
            Item.importance.icontains(term, autoescape=True),
            cast(Item.key_points, String).icontains(term, autoescape=True),
            Item.raw_content.icontains(term, autoescape=True),
            Item.clean_content.icontains(term, autoescape=True),
            Item.why_it_matters.icontains(term, autoescape=True),
            Item.os_insight.icontains(term, autoescape=True),
            Item.source_id.in_(select(Source.id).where(source_matches)),
            Item.id.in_(
                select(ItemSource.item_id)
                .join(Source, Source.id == ItemSource.source_id)
                .where(or_(source_matches, ItemSource.url.icontains(term, autoescape=True)))
            ),
            Item.id.in_(
                select(ItemEntity.item_id)
                .join(Entity, Entity.id == ItemEntity.entity_id)
                .where(or_(Entity.name.icontains(term, autoescape=True), Entity.type.icontains(term, autoescape=True)))
            ),
            Item.id.in_(
                select(DiscussionGroup.item_id)
                .join(DiscussionGroupThread, DiscussionGroupThread.group_id == DiscussionGroup.id)
                .join(DiscussionMessage, DiscussionMessage.thread_id == DiscussionGroupThread.thread_id)
                .where(DiscussionMessage.subject.icontains(term, autoescape=True))
            ),
            Item.id.in_(
                select(ItemTag.item_id)
                .join(Tag, Tag.id == ItemTag.tag_id)
                .where(or_(Tag.name.icontains(term, autoescape=True), Tag.kind.icontains(term, autoescape=True)))
            ),
        )

    @staticmethod
    def _item_matches_title_term(term: str):
        return or_(
            Item.title.icontains(term, autoescape=True),
            Item.title_tldr.icontains(term, autoescape=True),
            Item.id.in_(
                select(DiscussionGroup.item_id)
                .join(DiscussionGroupThread, DiscussionGroupThread.group_id == DiscussionGroup.id)
                .join(DiscussionMessage, DiscussionMessage.thread_id == DiscussionGroupThread.thread_id)
                .where(DiscussionMessage.subject.icontains(term, autoescape=True))
            ),
        )

    def _build_item_stmt(self, snapshot: MailFilterSnapshot):
        stmt = select(Item).where(visible_item_clause())
        if snapshot.item_kind:
            stmt = stmt.where(Item.item_kind == snapshot.item_kind)
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
        source_ids = [
            int(value)
            for value in self._split_filter_values(snapshot.source_id)
            if value.isdigit()
        ]
        if source_ids:
            stmt = stmt.where(
                or_(
                    Item.source_id.in_(source_ids),
                    Item.id.in_(
                        select(ItemSource.item_id)
                        .where(ItemSource.source_id.in_(source_ids))
                    ),
                )
            )
        search_terms = self._search_terms(snapshot)
        if search_terms:
            matcher = self._item_matches_title_term if snapshot.strict_title else self._item_matches_search_term
            stmt = stmt.where(or_(*(matcher(term) for term in search_terms)))

        published_after, published_before = self._resolve_datetime_boundaries(
            after_mode=snapshot.published_after_mode,
            after_value=snapshot.published_after_value,
            after_date_value=snapshot.published_after,
            before_mode=snapshot.published_before_mode,
            before_value=snapshot.published_before_value,
            before_date_value=snapshot.published_before,
        )
        if published_after is not None:
            stmt = stmt.where(Item.published_at >= published_after)
        if published_before is not None:
            stmt = stmt.where(Item.published_at < published_before)

        fetched_after, fetched_before = self._resolve_datetime_boundaries(
            after_mode=snapshot.fetched_after_mode,
            after_value=snapshot.fetched_after_value,
            after_date_value=snapshot.fetched_after,
            before_mode=snapshot.fetched_before_mode,
            before_value=snapshot.fetched_before_value,
            before_date_value=snapshot.fetched_before,
        )
        if fetched_after is not None:
            stmt = stmt.where(Item.fetched_at >= fetched_after)
        if fetched_before is not None:
            stmt = stmt.where(Item.fetched_at < fetched_before)
        return stmt

    def _fetch_items_for_snapshot(self, snapshot: MailFilterSnapshot, *, limit: int = 100) -> list[Item]:
        stmt = self._build_item_stmt(snapshot)
        sort_col = {
            "published_at": Item.published_at,
            "fetched_at": Item.fetched_at,
            "last_activity_at": func.coalesce(Item.last_activity_at, Item.published_at),
        }[snapshot.sort_by]
        time_order_clause = sort_col.desc() if snapshot.sort_dir == "desc" else sort_col.asc()
        if snapshot.sort_by in {"published_at", "last_activity_at"}:
            time_order_clause = time_order_clause.nullslast()
        importance_order_clause = case(
            (Item.importance == "高", 0),
            (Item.importance == "中", 1),
            (Item.importance == "低", 2),
            else_=3,
        )
        return list(
            self._db.scalars(
                stmt.order_by(importance_order_clause, time_order_clause, Item.id.desc()).limit(limit)
            )
        )

    def _extract_hotspots(self, item: Item) -> list[str]:
        hotspots = [tag.name for tag in item.tags if getattr(tag, "kind", None) == "sub_tag"]
        deduped: list[str] = []
        for hotspot in hotspots:
            if hotspot not in deduped:
                deduped.append(hotspot)
        return deduped[:5]

    @staticmethod
    def _method_overall_score(method: CrawlMethod | None) -> int | None:
        if method is None:
            return None
        if method.quality_score is None:
            return method.overall_score
        density_score = method.density_score if method.density_score is not None else method.quality_score
        return calculate_overall_score(method.quality_score, density_score)

    @staticmethod
    def _grade_for_score(score: int | None) -> str | None:
        if score is None:
            return None
        if score >= 85:
            return "A"
        if score >= 70:
            return "B"
        if score >= 50:
            return "C"
        return "D"

    def _source_quality_for_item(self, item: Item) -> dict[str, str | int | None]:
        source = item.source or self._db.get(Source, item.source_id)
        method = self._db.scalar(
            select(CrawlMethod)
            .where(CrawlMethod.source_id == item.source_id)
            .order_by(CrawlMethod.id.desc())
            .limit(1)
        )
        score = self._method_overall_score(method)
        return {
            "source_name": source.name if source is not None else None,
            "source_quality_score": score,
            "source_quality_grade": self._grade_for_score(score),
            "source_quality_status": method.quality_audit_status if method is not None else None,
        }

    def _build_preview_items(self, items: list[Item], *, website_url: str = "") -> list[MailPreviewItem]:
        preview_items: list[MailPreviewItem] = []
        for item in items:
            source_quality = self._source_quality_for_item(item)
            preview_items.append(
                MailPreviewItem(
                    # 与主界面 ItemCard 一致：优先展示中文标题 title_tldr，缺失时回退原文 title
                    title=item.title_tldr or item.title,
                    item_kind=item.item_kind,
                    main_category=item.main_category,
                    reason=item.why_it_matters or item.summary or item.title_tldr,
                    summary=item.summary,
                    importance=item.importance,
                    key_points=[str(point) for point in (item.key_points or [])],
                    hotspots=self._extract_hotspots(item),
                    source_url=_mail_item_source_url(
                        item_url=item.url,
                        item_id=item.id,
                        website_url=website_url,
                    ),
                    published_at=item.published_at.isoformat() if item.published_at else None,
                    **source_quality,
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
        stored_notice: dict | None = None,
        apply_send_config: bool = True,
    ) -> MailPreviewResponse:
        normalized = MailFilterSnapshot.model_validate(filter_snapshot or {})
        items = self._fetch_items_for_snapshot(normalized)
        website_url = _website_url_for_item_links(
            stored_notice=stored_notice,
            apply_send_config=apply_send_config,
            db=self._db,
        )
        preview_items = self._build_preview_items(items, website_url=website_url)
        dated_subject = with_send_date_suffix(subject)
        notice = self._resolve_notice(stored=stored_notice, apply_send_config=apply_send_config)
        notice_dict = notice.model_dump() if notice is not None else None
        context = build_mail_preview_context(
            filters=normalized.model_dump(mode="json"),
            items=[item.model_dump(mode="json") for item in preview_items],
            subject=dated_subject,
            notice=notice_dict,
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
            notice=notice,
        )

    def preview_trend_distribution(
        self,
        payload: MailTrendPreviewRequest,
        *,
        stored_notice: dict | None = None,
        apply_send_config: bool = True,
    ) -> MailPreviewResponse:
        """Build one mail from the same per-direction result sets as the carousel."""

        trend_service = TrendService(self._db)
        all_results = trend_service.get_trend_carousel(template_id=payload.template_id)
        website_url = _website_url_for_item_links(
            stored_notice=stored_notice,
            apply_send_config=apply_send_config,
            db=self._db,
        )
        groups: list[MailTrendDirectionGroup] = []
        flattened_sources: list[MailPreviewItem] = []
        for direction in sorted(all_results.directions, key=trend_direction_sort_key):
            direction_results = trend_service.get_trend_carousel(
                template_id=payload.template_id,
                direction=direction,
            )
            trends: list[MailTrendSummary] = []
            for result in direction_results.items:
                source_items = list(
                    self._db.scalars(select(Item).where(Item.id.in_(result.item_ids)))
                )
                source_by_id = {item.id: item for item in source_items}
                sources = self._build_preview_items(
                    [source_by_id[item_id] for item_id in result.item_ids if item_id in source_by_id],
                    website_url=website_url,
                )
                flattened_sources.extend(sources)
                trends.append(
                    MailTrendSummary(
                        result_id=result.result_id,
                        title=result.topic,
                        summary=result.trend_summary,
                        category=result.category,
                        category_label=result.category_label,
                        sources=sources,
                    )
                )
            if trends:
                groups.append(MailTrendDirectionGroup(direction=direction, trends=trends))
        dated_subject = with_send_date_suffix(payload.subject)
        notice = self._resolve_notice(stored=stored_notice, apply_send_config=apply_send_config)
        notice_dict = notice.model_dump() if notice is not None else None
        rendered_html = render_trend_mail_html(
            subject=dated_subject,
            trend_groups=[group.model_dump(mode="json") for group in groups],
            notice=notice_dict,
        )
        return MailPreviewResponse(
            subject=dated_subject,
            filter_snapshot=MailFilterSnapshot(),
            recipients=payload.recipients,
            provider=resolve_mail_provider_name(payload.provider),
            item_count=sum(len(group.trends) for group in groups),
            items=flattened_sources,
            rendered_html=rendered_html,
            notice=notice,
            trend_groups=groups,
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

        if preview.item_count <= 0:
            delivery.status = "查询空"
            delivery.error_message = "当前筛选没有匹配到条目，未发送邮件。"
            delivery.finished_at = beijing_now()
            return delivery

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

    def send_trend_distribution(self, payload: MailTrendPreviewRequest) -> MailImmediateSendResponse:
        preview = self.preview_trend_distribution(payload)
        delivery = self._dispatch_delivery(
            preview=preview,
            provider=payload.provider,
            trigger_type="trend_distribution",
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
        notice_json: dict | None = None
        content_type = "news"
        trend_identity_template_id: str | None = None
        if payload.template_id is not None:
            template = self.get_template(payload.template_id)
            notice_json = template.notice_json if isinstance(template.notice_json, dict) else None
            content_type = template.content_type
            trend_identity_template_id = template.trend_identity_template_id
        else:
            config = get_or_create_notice_config(self._db)
            notice_json = _snapshot_notice_from_config(config, include=bool(config.include_on_send))
        schedule = MailSchedule(
            template_id=payload.template_id,
            name=payload.name.strip() or "未命名预定",
            subject=payload.subject.strip() or "未命名预定",
            recipients_json=list(payload.recipients),
            filter_snapshot_json=self.normalize_filter_snapshot(payload.filter_snapshot.model_dump()),
            content_type=content_type,
            trend_identity_template_id=trend_identity_template_id,
            notice_json=notice_json,
            frequency=payload.frequency,
            weekly_day=payload.weekly_day if payload.frequency == "weekly" else None,
            send_time=payload.send_time,
            enabled=payload.enabled,
            next_run_at=_compute_next_run(
                send_time=payload.send_time,
                frequency=payload.frequency,
                weekly_day=payload.weekly_day,
                reference=now,
            ),
            last_sent_marker_date=now.date().isoformat() if payload.enabled else None,
            last_result_status="跳过" if payload.enabled else None,
            last_result_count=0 if payload.enabled else None,
            patrol_status="跳过" if payload.enabled else None,
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
        if payload.weekly_day is not None:
            schedule.weekly_day = payload.weekly_day
        if schedule.frequency == "weekly" and schedule.weekly_day is None:
            raise ValueError("每周预定必须选择发送星期")
        if schedule.frequency != "weekly":
            schedule.weekly_day = None
        if payload.send_time is not None:
            schedule.send_time = payload.send_time
        if payload.enabled is not None:
            schedule.enabled = payload.enabled
        schedule.updated_at = beijing_now()
        schedule.next_run_at = _compute_next_run(
            send_time=schedule.send_time,
            frequency=schedule.frequency,
            weekly_day=schedule.weekly_day,
            reference=beijing_now(),
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
                send_time=schedule.send_time,
                frequency=schedule.frequency,
                weekly_day=schedule.weekly_day,
                reference=beijing_now(),
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
        stored_notice = schedule.notice_json if isinstance(schedule.notice_json, dict) else None
        if schedule.content_type == "trend_distribution":
            if not schedule.trend_identity_template_id:
                raise ValueError("趋势分发预定缺少身份模板")
            preview = self.preview_trend_distribution(
                MailTrendPreviewRequest(
                    template_id=schedule.trend_identity_template_id,
                    subject=schedule.subject,
                    recipients=list(schedule.recipients_json or []),
                    provider=None,
                ),
                stored_notice=stored_notice,
                apply_send_config=stored_notice is None,
            )
        else:
            snapshot = MailFilterSnapshot.model_validate(schedule.filter_snapshot_json or {})
            preview = self.preview_immediate_send(
                filter_snapshot=snapshot.model_dump(mode="json"),
                subject=schedule.subject,
                recipients=list(schedule.recipients_json or []),
                provider=None,
                stored_notice=stored_notice,
                apply_send_config=stored_notice is None,
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
            schedule.patrol_status = "正常"
        elif delivery.status in ("查询空", "skipped_empty"):
            schedule.patrol_status = "查询空"
            schedule.next_run_at = now + EMPTY_SCHEDULE_RETRY_DELAY
        else:
            schedule.next_run_at = _compute_next_run(
                send_time=schedule.send_time,
                frequency=schedule.frequency,
                weekly_day=schedule.weekly_day,
                reference=now,
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
        content_type="trend_distribution" if template.content_type == "trend_distribution" else "news",
        trend_identity_template_id=template.trend_identity_template_id,
        notice=_active_notice_block(template.notice_json if isinstance(template.notice_json, dict) else None),
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
        content_type="trend_distribution" if schedule.content_type == "trend_distribution" else "news",
        trend_identity_template_id=schedule.trend_identity_template_id,
        frequency=schedule.frequency if schedule.frequency in ("daily", "weekly") else "daily",
        weekly_day=schedule.weekly_day,
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
