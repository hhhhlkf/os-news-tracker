from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.mail.service import MailService, template_to_response
from app.schemas import MailTemplateCreateRequest, MailTemplateResponse

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
