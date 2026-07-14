"""Review workflow for newly discovered crawl methods."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.mail.service import build_default_mail_provider, resolve_sender
from app.models import (
    CrawlMethod,
    CrawlMethodDomain,
    CrawlMethodReviewReminderConfig,
    MailDelivery,
    MorningCrawlRunMethod,
    SiteDiscoveryRun,
    Source,
)

REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
DEFAULT_REVIEW_REMINDER_INTERVAL_MINUTES = 1440


def list_review_methods(db: Session, *, review_status: str) -> list[CrawlMethod]:
    return list(
        db.scalars(
            select(CrawlMethod)
            .where(CrawlMethod.review_status == review_status)
            .order_by(CrawlMethod.created_at.desc(), CrawlMethod.id.desc())
        )
    )


def approve_methods(db: Session, method_ids: list[int], *, reviewer: str | None = None) -> int:
    if not method_ids:
        return 0
    now = datetime.now(timezone.utc)
    methods = list(
        db.scalars(
            select(CrawlMethod).where(
                CrawlMethod.id.in_(method_ids),
                CrawlMethod.review_status == REVIEW_PENDING,
            )
        )
    )
    for method in methods:
        method.review_status = REVIEW_APPROVED
        method.reviewed_at = now
        method.reviewed_by = reviewer
        method.review_note = None
    db.commit()
    return len(methods)


def delete_method(db: Session, method_id: int) -> bool:
    method = db.get(CrawlMethod, method_id)
    if method is None:
        return False
    db.execute(
        update(SiteDiscoveryRun)
        .where(SiteDiscoveryRun.resulting_method_id == method_id)
        .values(resulting_method_id=None)
    )
    db.execute(
        update(MorningCrawlRunMethod)
        .where(MorningCrawlRunMethod.method_id == method_id)
        .values(method_id=None)
    )
    db.query(CrawlMethodDomain).filter_by(method_id=method_id).delete()
    db.delete(method)
    db.commit()
    return True


def delete_methods(db: Session, method_ids: list[int]) -> int:
    deleted = 0
    for method_id in method_ids:
        method = db.get(CrawlMethod, method_id)
        if method is None or method.review_status != REVIEW_PENDING:
            continue
        if delete_method(db, method_id):
            deleted += 1
    return deleted


def get_or_create_reminder_config(db: Session) -> CrawlMethodReviewReminderConfig:
    config = db.get(CrawlMethodReviewReminderConfig, 1)
    if config is not None:
        return config
    config = CrawlMethodReviewReminderConfig(
        id=1,
        enabled=False,
        interval_minutes=DEFAULT_REVIEW_REMINDER_INTERVAL_MINUTES,
        recipients_json=[],
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


def reminder_due(config: CrawlMethodReviewReminderConfig, *, now: datetime | None = None) -> bool:
    if not config.enabled:
        return False
    if not config.recipients_json:
        return False
    now = now or datetime.now(timezone.utc)
    if config.last_sent_at is None:
        return True
    return config.last_sent_at <= now - timedelta(minutes=max(1, config.interval_minutes))


def send_review_reminder_if_due(db: Session, *, force: bool = False) -> dict[str, Any]:
    config = get_or_create_reminder_config(db)
    pending_methods = list_review_methods(db, review_status=REVIEW_PENDING)
    if not pending_methods:
        return {"sent": False, "reason": "no_pending_methods", "count": 0}
    if not force and not reminder_due(config):
        return {"sent": False, "reason": "not_due", "count": len(pending_methods)}

    now = datetime.now(timezone.utc)
    subject = f"[OS News Tracker] 有 {len(pending_methods)} 个爬取方式待审核"
    html = render_review_reminder_html(pending_methods)
    delivery = MailDelivery(
        schedule_id=None,
        template_id=None,
        trigger_type="crawl_method_review_reminder",
        status="pending",
        item_count=len(pending_methods),
        subject=subject,
        recipients_json=list(config.recipients_json or []),
        filter_snapshot_json={"review_status": REVIEW_PENDING},
        started_at=now,
    )
    db.add(delivery)
    db.flush()
    try:
        provider = build_default_mail_provider("tof4")
        from_email, from_name = resolve_sender("tof4")
        provider.send(
            subject=subject,
            html=html,
            recipients=list(config.recipients_json or []),
            from_email=from_email,
            from_name=from_name,
        )
        delivery.status = "sent"
        delivery.finished_at = datetime.now(timezone.utc)
        config.last_sent_at = delivery.finished_at
        config.last_result_status = "sent"
        config.last_error = None
        db.commit()
        return {"sent": True, "reason": "sent", "count": len(pending_methods), "delivery_id": delivery.id}
    except Exception as exc:
        delivery.status = "failed"
        delivery.finished_at = datetime.now(timezone.utc)
        delivery.error_message = str(exc)
        config.last_result_status = "failed"
        config.last_error = str(exc)
        db.commit()
        return {"sent": False, "reason": "send_failed", "count": len(pending_methods), "error": str(exc)}


def render_review_reminder_html(methods: list[CrawlMethod]) -> str:
    rows = "\n".join(_render_method_row(method) for method in methods)
    return f"""<!doctype html>
<html>
<body style="margin:0;background:#f7f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#101828;">
  <div style="max-width:960px;margin:0 auto;padding:24px;">
    <h2 style="margin:0 0 8px;font-size:20px;">有 {len(methods)} 个爬取方式待审核</h2>
    <p style="margin:0 0 16px;color:#667085;font-size:13px;">以下是智能探查生成但尚未进入正式爬取方式库的方法。</p>
    <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #d0d5dd;border-radius:8px;overflow:hidden;">
      <thead>
        <tr style="background:#f2f4f7;color:#475467;font-size:12px;text-align:left;">
          <th style="padding:10px;border-bottom:1px solid #d0d5dd;">来源</th>
          <th style="padding:10px;border-bottom:1px solid #d0d5dd;">入口</th>
          <th style="padding:10px;border-bottom:1px solid #d0d5dd;">综合</th>
          <th style="padding:10px;border-bottom:1px solid #d0d5dd;">质量</th>
          <th style="padding:10px;border-bottom:1px solid #d0d5dd;">密度</th>
          <th style="padding:10px;border-bottom:1px solid #d0d5dd;">状态</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</body>
</html>"""


def _render_method_row(method: CrawlMethod) -> str:
    score = method.overall_score if method.overall_score is not None else ""
    quality = method.quality_score if method.quality_score is not None else ""
    density = method.density_score if method.density_score is not None else ""
    return f"""
        <tr style="font-size:12px;">
          <td style="padding:10px;border-bottom:1px solid #eaecf0;font-weight:700;">{escape(method.domain)}</td>
          <td style="padding:10px;border-bottom:1px solid #eaecf0;word-break:break-all;color:#475467;">{escape(method.entry_url)}</td>
          <td style="padding:10px;border-bottom:1px solid #eaecf0;">{score}</td>
          <td style="padding:10px;border-bottom:1px solid #eaecf0;">{quality}</td>
          <td style="padding:10px;border-bottom:1px solid #eaecf0;">{density}</td>
          <td style="padding:10px;border-bottom:1px solid #eaecf0;">{escape(method.quality_audit_status or '未审计')}</td>
        </tr>"""


def method_source_name(db: Session, method: CrawlMethod) -> str:
    source = db.get(Source, method.source_id)
    return source.name if source else method.domain
