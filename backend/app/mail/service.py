from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Item, ItemTag, MailDelivery, MailTemplate, Tag
from app.schemas import (
    MailFilterSnapshot,
    MailImmediatePreviewRequest,
    MailImmediateSendResponse,
    MailPreviewItem,
    MailPreviewResponse,
    MailTemplateCreateRequest,
    MailTemplateResponse,
)

from .provider import MailProvider
from .rendering import build_mail_preview_context, render_mail_html
from .smtp_provider import SMTPConfig, SmtpMailProvider


def build_default_mail_provider() -> MailProvider:
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


class MailService:
    def __init__(self, db: Session, provider: MailProvider | None = None) -> None:
        self._db = db
        self._provider = provider or build_default_mail_provider()

    def normalize_filter_snapshot(self, raw: dict) -> dict:
        snapshot = MailFilterSnapshot.model_validate(raw or {})
        return snapshot.model_dump(mode="json")

    def list_templates(self) -> list[MailTemplate]:
        return list(self._db.scalars(select(MailTemplate).order_by(MailTemplate.updated_at.desc(), MailTemplate.id.desc())))

    def create_template(self, payload: MailTemplateCreateRequest) -> MailTemplate:
        now = datetime.utcnow()
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

    def _build_item_stmt(self, snapshot: MailFilterSnapshot):
        stmt = select(Item)
        if snapshot.main_category:
            stmt = stmt.where(Item.main_category == snapshot.main_category)
        if snapshot.info_type:
            stmt = stmt.where(Item.info_type == snapshot.info_type)
        if snapshot.importance:
            stmt = stmt.where(Item.importance == snapshot.importance)
        if snapshot.sub_tag:
            stmt = stmt.where(
                Item.id.in_(
                    select(ItemTag.item_id)
                    .join(Tag, Tag.id == ItemTag.tag_id)
                    .where(Tag.kind == "sub_tag", Tag.name == snapshot.sub_tag)
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
                    title=item.title,
                    reason=item.why_it_matters or item.summary or item.title_tldr,
                    summary=item.summary,
                    key_points=[str(point) for point in (item.key_points or [])],
                    hotspots=self._extract_hotspots(item),
                    source_url=item.url,
                    published_at=item.published_at.isoformat() if item.published_at else None,
                )
            )
        return preview_items

    def preview_immediate_send(self, *, filter_snapshot: dict, subject: str, recipients: list[str]) -> MailPreviewResponse:
        normalized = MailFilterSnapshot.model_validate(filter_snapshot or {})
        items = self._fetch_items_for_snapshot(normalized)
        preview_items = self._build_preview_items(items)
        context = build_mail_preview_context(
            filters=normalized.model_dump(mode="json"),
            items=[item.model_dump(mode="json") for item in preview_items],
            subject=subject.strip(),
        )
        rendered_html = render_mail_html(context)
        return MailPreviewResponse(
            subject=subject.strip(),
            filter_snapshot=normalized,
            recipients=recipients,
            item_count=len(preview_items),
            items=preview_items,
            rendered_html=rendered_html,
        )

    def send_immediate(self, payload: MailImmediatePreviewRequest) -> MailImmediateSendResponse:
        preview = self.preview_immediate_send(
            filter_snapshot=payload.filter_snapshot.model_dump(mode="json"),
            subject=payload.subject,
            recipients=payload.recipients,
        )
        delivery = MailDelivery(
            schedule_id=None,
            template_id=None,
            trigger_type="manual_send",
            status="pending",
            item_count=preview.item_count,
            subject=preview.subject,
            recipients_json=list(preview.recipients),
            filter_snapshot_json=preview.filter_snapshot.model_dump(mode="json"),
            started_at=datetime.utcnow(),
        )
        self._db.add(delivery)
        self._db.flush()

        settings = get_settings()
        try:
            self._provider.send(
                subject=preview.subject,
                html=preview.rendered_html,
                recipients=list(preview.recipients),
                from_email=settings.smtp_from_email,
                from_name=settings.smtp_from_name,
            )
            delivery.status = "sent"
            delivery.finished_at = datetime.utcnow()
            self._db.commit()
            self._db.refresh(delivery)
            return MailImmediateSendResponse(
                delivery_id=delivery.id,
                status=delivery.status,
                item_count=delivery.item_count,
                error_message=None,
            )
        except Exception as exc:
            delivery.status = "failed"
            delivery.error_message = str(exc)
            delivery.finished_at = datetime.utcnow()
            self._db.commit()
            self._db.refresh(delivery)
            return MailImmediateSendResponse(
                delivery_id=delivery.id,
                status=delivery.status,
                item_count=delivery.item_count,
                error_message=delivery.error_message,
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
