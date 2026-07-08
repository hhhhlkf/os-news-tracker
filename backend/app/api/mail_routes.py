from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.mail.service import MailService, template_to_response
from app.schemas import (
    MailImmediatePreviewRequest,
    MailImmediateSendResponse,
    MailPreviewResponse,
    MailTemplateCreateRequest,
    MailTemplateResponse,
)

router = APIRouter(prefix="/mail", tags=["mail"])


@router.get("/templates")
def list_mail_templates(db: Session = Depends(get_db)) -> list[MailTemplateResponse]:
    service = MailService(db)
    return [template_to_response(template) for template in service.list_templates()]


@router.post("/templates", status_code=201)
def create_mail_template(
    request: MailTemplateCreateRequest,
    db: Session = Depends(get_db),
) -> MailTemplateResponse:
    service = MailService(db)
    template = service.create_template(request)
    return template_to_response(template)


@router.post("/immediate/preview")
def preview_mail_immediate(
    request: MailImmediatePreviewRequest,
    db: Session = Depends(get_db),
) -> MailPreviewResponse:
    service = MailService(db)
    return service.preview_immediate_send(
        filter_snapshot=request.filter_snapshot.model_dump(mode="json"),
        subject=request.subject,
        recipients=request.recipients,
    )


@router.post("/immediate/send")
def send_mail_immediate(
    request: MailImmediatePreviewRequest,
    db: Session = Depends(get_db),
) -> MailImmediateSendResponse:
    service = MailService(db)
    return service.send_immediate(request)
