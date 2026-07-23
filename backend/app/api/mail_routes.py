from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.mail.service import (
    MailScheduleNotFoundError,
    MailService,
    MailTemplateNotFoundError,
    delivery_to_log,
    notice_config_to_response,
    schedule_to_response,
    template_to_response,
)
from app.schemas import (
    MailDeliveryLog,
    MailImmediatePreviewRequest,
    MailImmediateSendResponse,
    MailNoticeConfigResponse,
    MailNoticeConfigUpdateRequest,
    MailPreviewResponse,
    MailScheduleCreateRequest,
    MailScheduleResponse,
    MailScheduleUpdateRequest,
    MailTemplateActionRequest,
    MailTemplateCreateRequest,
    MailTemplateResponse,
    MailTemplateUpdateRequest,
)

router = APIRouter(prefix="/mail", tags=["mail"])


@router.get("/notice-config")
def get_mail_notice_config(db: Session = Depends(get_db)) -> MailNoticeConfigResponse:
    service = MailService(db)
    return notice_config_to_response(service.get_notice_config())


@router.put("/notice-config")
def update_mail_notice_config(
    request: MailNoticeConfigUpdateRequest,
    db: Session = Depends(get_db),
) -> MailNoticeConfigResponse:
    service = MailService(db)
    return notice_config_to_response(service.update_notice_config(request))


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


@router.get("/templates/{template_id}")
def get_mail_template(template_id: int, db: Session = Depends(get_db)) -> MailTemplateResponse:
    service = MailService(db)
    try:
        return template_to_response(service.get_template(template_id))
    except MailTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/templates/{template_id}")
def update_mail_template(
    template_id: int,
    request: MailTemplateUpdateRequest,
    db: Session = Depends(get_db),
) -> MailTemplateResponse:
    service = MailService(db)
    try:
        return template_to_response(service.update_template(template_id, request))
    except MailTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/templates/{template_id}", status_code=204)
def delete_mail_template(template_id: int, db: Session = Depends(get_db)) -> None:
    service = MailService(db)
    try:
        service.delete_template(template_id)
    except MailTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/templates/{template_id}/preview")
def preview_mail_template(
    template_id: int,
    request: MailTemplateActionRequest | None = Body(default=None),
    db: Session = Depends(get_db),
) -> MailPreviewResponse:
    service = MailService(db)
    provider = request.provider if request else None
    try:
        return service.preview_template(template_id, provider=provider)
    except MailTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/templates/{template_id}/send")
def send_mail_template(
    template_id: int,
    request: MailTemplateActionRequest | None = Body(default=None),
    db: Session = Depends(get_db),
) -> MailImmediateSendResponse:
    service = MailService(db)
    provider = request.provider if request else None
    try:
        return service.send_template_once(template_id, provider=provider)
    except MailTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/templates/{template_id}/schedules")
def list_template_schedules(
    template_id: int, db: Session = Depends(get_db)
) -> list[MailScheduleResponse]:
    service = MailService(db)
    try:
        schedules = service.list_schedules_for_template(template_id)
    except MailTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [schedule_to_response(schedule) for schedule in schedules]


@router.get("/schedules")
def list_mail_schedules(db: Session = Depends(get_db)) -> list[MailScheduleResponse]:
    service = MailService(db)
    return [schedule_to_response(schedule) for schedule in service.list_schedules()]


@router.post("/schedules", status_code=201)
def create_mail_schedule(
    request: MailScheduleCreateRequest,
    db: Session = Depends(get_db),
) -> MailScheduleResponse:
    service = MailService(db)
    return schedule_to_response(service.create_schedule(request))


@router.get("/schedules/{schedule_id}")
def get_mail_schedule(schedule_id: int, db: Session = Depends(get_db)) -> MailScheduleResponse:
    service = MailService(db)
    try:
        return schedule_to_response(service.get_schedule(schedule_id))
    except MailScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/schedules/{schedule_id}")
def update_mail_schedule(
    schedule_id: int,
    request: MailScheduleUpdateRequest,
    db: Session = Depends(get_db),
) -> MailScheduleResponse:
    service = MailService(db)
    try:
        return schedule_to_response(service.update_schedule(schedule_id, request))
    except MailScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_mail_schedule(schedule_id: int, db: Session = Depends(get_db)) -> None:
    service = MailService(db)
    try:
        service.delete_schedule(schedule_id)
    except MailScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/schedules/{schedule_id}/pause")
def pause_mail_schedule(schedule_id: int, db: Session = Depends(get_db)) -> MailScheduleResponse:
    service = MailService(db)
    try:
        return schedule_to_response(service.set_schedule_enabled(schedule_id, False))
    except MailScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/schedules/{schedule_id}/resume")
def resume_mail_schedule(schedule_id: int, db: Session = Depends(get_db)) -> MailScheduleResponse:
    service = MailService(db)
    try:
        return schedule_to_response(service.set_schedule_enabled(schedule_id, True))
    except MailScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/schedules/{schedule_id}/send-now")
def send_now_mail_schedule(schedule_id: int, db: Session = Depends(get_db)) -> MailImmediateSendResponse:
    service = MailService(db)
    try:
        return service.send_schedule_now(schedule_id)
    except MailScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/schedules/{schedule_id}/logs")
def list_mail_schedule_logs(schedule_id: int, db: Session = Depends(get_db)) -> list[MailDeliveryLog]:
    service = MailService(db)
    try:
        return [delivery_to_log(delivery) for delivery in service.list_schedule_logs(schedule_id)]
    except MailScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
        provider=request.provider,
    )


@router.post("/immediate/send")
def send_mail_immediate(
    request: MailImmediatePreviewRequest,
    db: Session = Depends(get_db),
) -> MailImmediateSendResponse:
    service = MailService(db)
    return service.send_immediate(request)
