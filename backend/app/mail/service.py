from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MailTemplate
from app.schemas import MailFilterSnapshot, MailTemplateCreateRequest, MailTemplateResponse

from .provider import MailProvider
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
