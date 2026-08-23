"""Review workflow for newly discovered crawl methods."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import logging
from threading import RLock
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.discovery.quality_audit import calculate_overall_score
from app.mail.service import build_default_mail_provider, resolve_sender
from app.models import (
    CrawlMethod,
    CrawlMethodDomain,
    CrawlMethodReviewReminderConfig,
    DiscussionSourceRule,
    MailDelivery,
    MorningCrawlRunMethod,
    SiteDiscoveryRun,
    Source,
)

if TYPE_CHECKING:
    from app.discovery.plugin.artifact import ConnectorArtifact

REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
DEFAULT_REVIEW_REMINDER_INTERVAL_MINUTES = 1440
_APPROVAL_LOCK = RLock()
logger = logging.getLogger(__name__)


def list_review_methods(db: Session, *, review_status: str) -> list[CrawlMethod]:
    """按审核状态列出待审核的爬取方法（按创建时间倒序）。

    功能：查询指定 review_status 的 CrawlMethod，供审核列表展示。
    谁会调用：discovery_routes 的审核列表接口调用。
    直接调用：
    - SQLAlchemy select(...)：构造并执行查询。
    输入与结果：输入数据库会话与审核状态；返回 CrawlMethod 列表。
    副作用：只读查询数据库。
    """
    return list(
        db.scalars(
            select(CrawlMethod)
            .where(
                CrawlMethod.review_status == review_status,
                CrawlMethod.status != "packaging",
            )
            .order_by(CrawlMethod.created_at.desc(), CrawlMethod.id.desc())
        )
    )


def approve_methods(
    db: Session,
    method_ids: list[int],
    *,
    reviewer: str | None = None,
    low_frequency_exception_reason: str | None = None,
) -> int:
    with _APPROVAL_LOCK:
        return _approve_methods_locked(
            db,
            method_ids,
            reviewer=reviewer,
            low_frequency_exception_reason=low_frequency_exception_reason,
        )


def _approve_methods_locked(
    db: Session,
    method_ids: list[int],
    *,
    reviewer: str | None = None,
    low_frequency_exception_reason: str | None = None,
) -> int:
    if not method_ids:
        return 0
    requested_domains = list(
        db.scalars(select(CrawlMethod.domain).where(CrawlMethod.id.in_(method_ids)))
    )
    if len(requested_domains) != len(set(requested_domains)):
        raise ValueError("one approval batch cannot contain multiple versions of the same domain")
    db.rollback()
    from app.discovery.domain_transition import domain_transition_locks

    with domain_transition_locks(db, sorted(set(requested_domains))):
        return _approve_methods_under_domain_locks(
            db,
            method_ids,
            reviewer=reviewer,
            low_frequency_exception_reason=low_frequency_exception_reason,
        )


def _approve_methods_under_domain_locks(
    db: Session,
    method_ids: list[int],
    *,
    reviewer: str | None = None,
    low_frequency_exception_reason: str | None = None,
) -> int:
    """批量批准处于 pending 状态的爬取方法。

    功能：查出指定 id 中仍为 pending 的方法，置为 approved 并记录审核时间与审核人，提交后返回实际批准数量。
    谁会调用：discovery_routes 的批准接口调用。
    直接调用：
    - SQLAlchemy select(...)：查出目标方法。
    - db.commit(...)：提交状态变更。
    输入与结果：输入 db、method_ids 与可选 reviewer；返回批准的方法数量。
    副作用：更新 CrawlMethod 审核状态并落库。
    """
    if db.scalar(
        select(CrawlMethod.id).where(
            CrawlMethod.id.in_(method_ids),
            CrawlMethod.status == "packaging",
        )
    ) is not None:
        raise ValueError("method packaging is not available for review")
    now = datetime.now(timezone.utc)
    methods = list(
        db.scalars(
            select(CrawlMethod).where(
                CrawlMethod.id.in_(method_ids),
                CrawlMethod.review_status == REVIEW_PENDING,
                CrawlMethod.status != "packaging",
            ).with_for_update()
        )
    )
    for method in methods:
        source = db.get(Source, method.source_id)
        if source is None:
            raise LookupError("pending method source is missing")
        is_plugin = (method.dsl_recipe or {}).get("recipe_type") == "python_plugin"
        if is_plugin:
            from app.discovery.plugin.review import validate_plugin_approval

            validate_plugin_approval(
                method,
                session=db,
                exception_reason=low_frequency_exception_reason,
                reviewer=reviewer or "system_admin",
            )
        mapping = db.scalar(
            select(CrawlMethodDomain)
            .where(CrawlMethodDomain.domain == method.domain)
            .with_for_update()
        )
        if mapping is None:
            db.add(CrawlMethodDomain(domain=method.domain, method_id=method.id))
        else:
            previous_method = db.get(CrawlMethod, mapping.method_id)
            mapping.method_id = method.id
            if previous_method is not None and previous_method.id != method.id:
                previous_method.status = "inactive"
                previous_source = db.get(Source, previous_method.source_id)
                if previous_source is not None:
                    remaining_active_mappings = db.scalar(
                        select(func.count(CrawlMethodDomain.id))
                        .join(CrawlMethod, CrawlMethodDomain.method_id == CrawlMethod.id)
                        .where(
                            CrawlMethodDomain.domain != method.domain,
                            CrawlMethod.source_id == previous_source.id,
                            CrawlMethod.status == "active",
                        )
                    )
                    if not remaining_active_mappings:
                        previous_source.enabled = False
        method.status = "active"
        source.enabled = True
        method.review_status = REVIEW_APPROVED
        method.reviewed_at = now
        method.reviewed_by = reviewer
        if not is_plugin:
            method.review_note = None
        if is_plugin:
            from app.discovery.loop.experience_store import (
                admit_approved_connector_experience,
                confirm_repair_experience,
            )

            admit_approved_connector_experience(db, method)
            repair_run = db.scalar(
                select(SiteDiscoveryRun).where(
                    SiteDiscoveryRun.resulting_method_id == method.id,
                    SiteDiscoveryRun.trigger_type == "repair",
                    SiteDiscoveryRun.status == "completed",
                )
            )
            if repair_run is not None and reviewer:
                confirm_repair_experience(
                    db,
                    run=repair_run,
                    method=method,
                    confirmed_by=reviewer,
                )
    db.commit()
    return len(methods)


def delete_method(db: Session, method_id: int) -> bool:
    """删除单个爬取方法，并清理其关联记录。

    功能：先解除 SiteDiscoveryRun、MorningCrawlRunMethod 对该方法的引用，再删 CrawlMethodDomain 与 CrawlMethod 本身。
    谁会调用：delete_methods 批量删除、discovery_routes 删除接口调用。
    直接调用：
    - db.get/execute/update/delete/commit：清理关联并提交。
    输入与结果：输入 db 与 method_id；返回是否成功删除（不存在则返回 False）。
    副作用：删除方法及其关联记录并提交数据库。
    """
    observed = db.get(CrawlMethod, method_id)
    if observed is None:
        db.rollback()
        return False
    expected_review_status = observed.review_status
    db.rollback()
    with _APPROVAL_LOCK:
        return _delete_methods_locked(
            db,
            [method_id],
            require_all=True,
            expected_review_status={method_id: expected_review_status},
            allow_approved=True,
        ) == 1


def _delete_method_rows(db: Session, method: CrawlMethod) -> None:
    method_id = method.id
    source_id = method.source_id
    current_mapping = db.scalar(
        select(CrawlMethodDomain)
        .where(CrawlMethodDomain.method_id == method_id)
        .with_for_update()
    )
    fallback_method = None
    if current_mapping is not None:
        fallback_method = db.scalar(
            select(CrawlMethod)
            .where(
                CrawlMethod.id != method_id,
                CrawlMethod.domain == method.domain,
                CrawlMethod.review_status == REVIEW_APPROVED,
                CrawlMethod.status == "inactive",
            )
            .order_by(CrawlMethod.reviewed_at.desc().nullslast(), CrawlMethod.id.desc())
            .with_for_update()
            .limit(1)
        )
        if fallback_method is not None:
            current_mapping.method_id = fallback_method.id
            fallback_method.status = "active"
            fallback_source = db.get(Source, fallback_method.source_id)
            if fallback_source is not None:
                fallback_source.enabled = True
        else:
            db.delete(current_mapping)
    db.execute(
        update(SiteDiscoveryRun)
        .where(SiteDiscoveryRun.resulting_method_id == method_id)
        .values(resulting_method_id=None)
    )
    db.execute(
        update(SiteDiscoveryRun)
        .where(SiteDiscoveryRun.repair_method_id == method_id)
        .values(repair_method_id=None)
    )
    db.execute(
        update(MorningCrawlRunMethod)
        .where(MorningCrawlRunMethod.method_id == method_id)
        .values(method_id=None)
    )
    db.delete(method)
    db.flush()
    source = db.get(Source, source_id)
    if source is not None:
        active_mapped = db.scalar(
            select(func.count(CrawlMethodDomain.id))
            .join(CrawlMethod, CrawlMethodDomain.method_id == CrawlMethod.id)
            .where(
                CrawlMethod.source_id == source_id,
                CrawlMethod.status == "active",
            )
        )
        source.enabled = bool(active_mapped)


def delete_methods(db: Session, method_ids: list[int]) -> int:
    """批量删除处于 pending 状态的爬取方法。

    功能：逐个检查并删除 pending 状态的方法（跳过已审核或不存在的），返回实际删除数量。
    谁会调用：discovery_routes 的批量删除接口调用。
    直接调用：
    - delete_method(...)：执行单条删除与关联清理。
    输入与结果：输入 db 与 method_ids；返回删除的方法数量。
    副作用：经 delete_method 删除并落库。
    """
    with _APPROVAL_LOCK:
        return _delete_methods_locked(
            db,
            method_ids,
            require_all=False,
            expected_review_status={method_id: REVIEW_PENDING for method_id in method_ids},
            allow_approved=False,
        )


def _delete_methods_locked(
    db: Session,
    method_ids: list[int],
    *,
    require_all: bool,
    expected_review_status: dict[int, str],
    allow_approved: bool,
) -> int:
    ids = sorted(set(method_ids))
    if not ids:
        return 0
    candidates = list(db.execute(
        select(CrawlMethod.id, CrawlMethod.domain).where(CrawlMethod.id.in_(ids))
    ))
    if require_all and len(candidates) != len(ids):
        db.rollback()
        return 0
    domains = sorted({domain for _, domain in candidates})
    db.rollback()
    from app.discovery.domain_transition import domain_transition_locks

    with domain_transition_locks(db, domains):
        return _delete_methods_under_domain_locks(
            db,
            ids,
            require_all=require_all,
            expected_review_status=expected_review_status,
            allow_approved=allow_approved,
        )


def _delete_methods_under_domain_locks(
    db: Session,
    method_ids: list[int],
    *,
    require_all: bool,
    expected_review_status: dict[int, str],
    allow_approved: bool,
) -> int:
    ids = sorted(set(method_ids))
    methods = list(db.scalars(
        select(CrawlMethod)
        .where(CrawlMethod.id.in_(ids))
        .order_by(CrawlMethod.domain, CrawlMethod.id)
        .with_for_update()
    ))
    deletable = []
    for method in methods:
        from app.discovery.migration import method_deletion_blocker

        migration_blocker = method_deletion_blocker(db, method.id)
        if migration_blocker is not None:
            if require_all:
                db.rollback()
                raise ValueError(migration_blocker)
            continue
        expected = expected_review_status.get(method.id)
        state_matches = method.review_status == expected
        pending_allowed = (
            method.review_status == REVIEW_PENDING
            and method.status not in {"packaging", "active"}
        )
        approved_allowed = (
            allow_approved
            and method.review_status == REVIEW_APPROVED
            and method.status != "packaging"
        )
        if state_matches and (pending_allowed or approved_allowed):
            deletable.append(method)
    if any(method.status == "packaging" for method in methods):
        db.rollback()
        raise ValueError("method packaging is not deletable")
    if require_all and len(deletable) != len(ids):
        db.rollback()
        raise ValueError("method review state changed or is not deletable")
    cleanup_artifacts: list[tuple[ConnectorArtifact, str]] = []
    for method in deletable:
        artifact = _pending_site_artifact_for_cleanup(method)
        if artifact is not None:
            cleanup_artifacts.append((artifact, method.domain))
        _delete_method_rows(db, method)
    db.commit()
    for artifact, domain in cleanup_artifacts:
        _cleanup_pending_artifact(db, artifact, domain=domain)
    return len(deletable)


def _pending_site_artifact_for_cleanup(method: CrawlMethod) -> ConnectorArtifact | None:
    """Capture a validated pending site artifact; shared/approved artifacts are retained."""
    if method.review_status != REVIEW_PENDING or (method.dsl_recipe or {}).get("recipe_type") != "python_plugin":
        return None
    try:
        from app.discovery.plugin.review import load_validated_review_artifact

        validated = load_validated_review_artifact(method)
        return validated.artifact if validated.resolved.connector_kind == "sites" else None
    except Exception as exc:
        logger.warning("retaining invalid pending artifact during method deletion: %s", exc)
        return None


def _cleanup_pending_artifact(db: Session, artifact: ConnectorArtifact, *, domain: str) -> None:
    try:
        from app.discovery.loop.artifacts import remove_unreferenced_pending_site_artifact
        from app.discovery.domain_transition import domain_transition_lock

        with domain_transition_lock(db, domain):
            approved_reference = db.scalar(
                select(CrawlMethod.id).where(
                    CrawlMethod.review_status == REVIEW_APPROVED,
                    CrawlMethod.signature == artifact.signature,
                )
            )
            if approved_reference is None:
                remove_unreferenced_pending_site_artifact(db, artifact=artifact)
            db.commit()
    except Exception as exc:
        from app.discovery.domain_transition import MigrationBusyError

        if isinstance(exc, MigrationBusyError):
            db.rollback()
            logger.warning("pending artifact cleanup deferred because domain is busy: %s", exc)
            return
        # DB deletion is authoritative. Retaining an immutable orphan is safer
        # than broad filesystem cleanup and can be handled by an operator.
        logger.exception("pending plugin DB row deleted but artifact cleanup was retained")


def get_or_create_reminder_config(db: Session) -> CrawlMethodReviewReminderConfig:
    """获取或创建审核提醒配置（固定 id=1）。

    功能：若已存在则直接返回；否则新建一条默认关闭、间隔 1440 分钟的提醒配置并落库。
    谁会调用：discovery_routes 配置接口、send_review_reminder_if_due 读取配置时调用。
    直接调用：
    - db.get(...)：查现有配置。
    - CrawlMethodReviewReminderConfig(...)：构造默认配置；db.add/commit 落库。
    输入与结果：输入 db；返回配置对象。
    副作用：可能新建配置记录并提交。
    """
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
    """判断审核提醒是否到了发送时间。

    功能：配置未启用、无收件人或距上次发送不足间隔时返回 False；从未发送过则视为到期。
    谁会调用：send_review_reminder_if_due 在决定是否发送前调用。
    直接调用：无（仅时间比较）。
    输入与结果：输入配置对象与可选 now；返回布尔。
    副作用：无。
    """
    if not config.enabled:
        return False
    if not config.recipients_json:
        return False
    now = now or datetime.now(timezone.utc)
    if config.last_sent_at is None:
        return True
    return config.last_sent_at <= now - timedelta(minutes=max(1, config.interval_minutes))


def send_review_reminder_if_due(db: Session, *, force: bool = False) -> dict[str, Any]:
    """若到时间（或 force）则发送待审核提醒邮件，否则跳过。

    功能：统计 pending 的爬取方法与邮件列表讨论源规则，渲染 HTML 邮件并发送，记录 MailDelivery 与配置的最后发送时间；无待审则直接返回。
    谁会调用：scheduler 定时任务、discovery_routes 强制发送接口调用。
    直接调用：
    - get_or_create_reminder_config(...)：读取提醒配置。
    - list_review_methods(...)：列出待审方法。
    - reminder_due(...)：判断是否到时间。
    - render_review_reminder_html(...)：渲染邮件正文。
    - build_default_mail_provider(...)/resolve_sender(...)：构建邮件发送方。
    - MailDelivery(...)/db.commit(...)：记录发送结果。
    输入与结果：输入 db 与 force；返回含 sent/reason/count 等的结果字典。
    副作用：查询并写入数据库（MailDelivery、配置），发送邮件（网络）。
    """
    config = get_or_create_reminder_config(db)
    pending_methods = list_review_methods(db, review_status=REVIEW_PENDING)
    pending_source_rules = list(db.execute(
        select(Source, DiscussionSourceRule)
        .select_from(DiscussionSourceRule)
        .join(Source, Source.id == DiscussionSourceRule.source_id)
        .where(Source.stream == "discussion", DiscussionSourceRule.review_status == REVIEW_PENDING)
        .order_by(DiscussionSourceRule.created_at.desc(), DiscussionSourceRule.id.desc()),
    ))
    pending_source_ids = {rule.source_id for _, rule in pending_source_rules}
    pending_source_count = len(pending_source_ids)
    total_pending = len(pending_methods) + pending_source_count
    if not total_pending:
        return {"sent": False, "reason": "no_pending_sources", "count": 0}
    if not force and not reminder_due(config):
        return {"sent": False, "reason": "not_due", "count": total_pending}

    now = datetime.now(timezone.utc)
    subject_parts = []
    if pending_methods:
        subject_parts.append(f"{len(pending_methods)} 个爬取方式")
    if pending_source_count:
        subject_parts.append(f"{pending_source_count} 个邮件列表")
    subject = f"[OS News Tracker] 有 {'、'.join(subject_parts)}待审核"
    html = render_review_reminder_html(pending_methods, pending_source_rules)
    delivery = MailDelivery(
        schedule_id=None,
        template_id=None,
        trigger_type="crawl_method_review_reminder",
        status="pending",
        item_count=total_pending,
        subject=subject,
        recipients_json=list(config.recipients_json or []),
        filter_snapshot_json={"review_status": REVIEW_PENDING, "includes_discussion_sources": True},
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
        return {"sent": True, "reason": "sent", "count": total_pending, "delivery_id": delivery.id}
    except Exception as exc:
        delivery.status = "failed"
        delivery.finished_at = datetime.now(timezone.utc)
        delivery.error_message = str(exc)
        config.last_result_status = "failed"
        config.last_error = str(exc)
        db.commit()
        return {"sent": False, "reason": "send_failed", "count": total_pending, "error": str(exc)}


def render_review_reminder_html(
    methods: list[CrawlMethod],
    discussion_source_rules: list[tuple[Source, DiscussionSourceRule]] | None = None,
) -> str:
    """渲染审核提醒邮件的 HTML 正文（含爬取方法与邮件列表两部分）。

    功能：把待审爬取方法与讨论源规则分别组装成 HTML 表格，拼成完整邮件页面。
    谁会调用：send_review_reminder_if_due 在发送邮件前调用。
    直接调用：
    - _render_method_row(...)：渲染方法表格行。
    - _render_discussion_source_row(...)：渲染讨论源表格行。
    输入与结果：输入方法列表与讨论源规则；返回完整 HTML 字符串。
    副作用：无。
    """
    method_section = ""
    if methods:
        rows = "\n".join(_render_method_row(method) for method in methods)
        method_section = f"""
    <h2 style="margin:0 0 8px;font-size:20px;">有 {len(methods)} 个爬取方式待审核</h2>
    <p style="margin:0 0 16px;color:#667085;font-size:13px;">以下是智能探查生成但尚未进入正式爬取方式库的方法。</p>
    <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #d0d5dd;border-radius:8px;overflow:hidden;">
      <thead><tr style="background:#f2f4f7;color:#475467;font-size:12px;text-align:left;"><th style="padding:10px;border-bottom:1px solid #d0d5dd;">来源</th><th style="padding:10px;border-bottom:1px solid #d0d5dd;">入口</th><th style="padding:10px;border-bottom:1px solid #d0d5dd;">综合</th><th style="padding:10px;border-bottom:1px solid #d0d5dd;">质量</th><th style="padding:10px;border-bottom:1px solid #d0d5dd;">密度</th><th style="padding:10px;border-bottom:1px solid #d0d5dd;">状态</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""
    rules_by_source: dict[int, tuple[str, list[DiscussionSourceRule]]] = {}
    for source, rule in discussion_source_rules or []:
        rules_by_source.setdefault(source.id, (source.name, []))[1].append(rule)
    discussion_section = ""
    if rules_by_source:
        discussion_rows = "\n".join(
            _render_discussion_source_row(name, rules)
            for name, rules in rules_by_source.values()
        )
        discussion_section = f"""
    <h2 style=\"margin:24px 0 8px;font-size:20px;\">有 {len(rules_by_source)} 个邮件列表待审核</h2>
    <p style=\"margin:0 0 16px;color:#667085;font-size:13px;\">通过后，这些列表才会参与专用邮箱的技术讨论收取。</p>
    <table style=\"width:100%;border-collapse:collapse;background:#fff;border:1px solid #d0d5dd;border-radius:8px;overflow:hidden;\">
      <thead><tr style=\"background:#f2f4f7;color:#475467;font-size:12px;text-align:left;\"><th style=\"padding:10px;border-bottom:1px solid #d0d5dd;\">邮件列表</th><th style=\"padding:10px;border-bottom:1px solid #d0d5dd;\">识别规则</th></tr></thead>
      <tbody>{discussion_rows}</tbody>
    </table>"""
    return f"""<!doctype html>
<html>
<body style="margin:0;background:#f7f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#101828;">
  <div style="max-width:960px;margin:0 auto;padding:24px;">
    {method_section}{discussion_section}
  </div>
</body>
</html>"""


def _render_method_row(method: CrawlMethod) -> str:
    """渲染单条爬取方法的 HTML 表格行（来源/入口/综合分/质量/密度/状态）。

    功能：计算该方法的综合分（质量分与密度分加权或取 overall_score）并转义输出，组成一行表格。
    谁会调用：render_review_reminder_html 在组装方法表格时调用。
    直接调用：
    - calculate_overall_score(...)：计算综合分。
    - escape(...)：转义 HTML 特殊字符。
    输入与结果：输入 method；返回 HTML 表格行字符串。
    副作用：无。
    """
    score = ""
    if method.quality_score is not None:
        density_score = method.density_score if method.density_score is not None else method.quality_score
        score = calculate_overall_score(method.quality_score, density_score)
    elif method.overall_score is not None:
        score = method.overall_score
    quality = method.quality_score if method.quality_score is not None else ""
    density = method.density_score if method.density_score is not None else ""
    audit_status = method.quality_audit_status or "未审计"
    if (method.dsl_recipe or {}).get("recipe_type") == "python_plugin":
        from app.discovery.plugin.review import plugin_review_summary

        summary = plugin_review_summary(method) or {}
        if summary.get("method_audit_status") == "low_frequency_exception_required":
            audit_status = "低频例外待确认"
        elif summary.get("artifact_status") == "invalid":
            audit_status = "制品校验失败"
    return f"""
        <tr style="font-size:12px;">
          <td style="padding:10px;border-bottom:1px solid #eaecf0;font-weight:700;">{escape(method.domain)}</td>
          <td style="padding:10px;border-bottom:1px solid #eaecf0;word-break:break-all;color:#475467;">{escape(method.entry_url)}</td>
          <td style="padding:10px;border-bottom:1px solid #eaecf0;">{score}</td>
          <td style="padding:10px;border-bottom:1px solid #eaecf0;">{quality}</td>
          <td style="padding:10px;border-bottom:1px solid #eaecf0;">{density}</td>
          <td style="padding:10px;border-bottom:1px solid #eaecf0;">{escape(audit_status)}</td>
        </tr>"""


def _render_discussion_source_row(name: str, rules: list[DiscussionSourceRule]) -> str:
    """渲染单条讨论源（邮件列表）识别规则的 HTML 表格行。

    功能：把该来源名与其识别规则（类型:值）拼接成一行表格，输出前转义。
    谁会调用：render_review_reminder_html 在组装讨论源表格时调用。
    直接调用：
    - escape(...)：转义 HTML 特殊字符。
    输入与结果：输入来源名与规则列表；返回 HTML 表格行字符串。
    副作用：无。
    """
    rule_text = " · ".join(f"{rule.rule_type}: {rule.match_value}" for rule in rules)
    return f"""<tr style="font-size:12px;"><td style="padding:10px;border-bottom:1px solid #eaecf0;font-weight:700;">{escape(name)}</td><td style="padding:10px;border-bottom:1px solid #eaecf0;color:#475467;word-break:break-all;">{escape(rule_text)}</td></tr>"""


def method_source_name(db: Session, method: CrawlMethod) -> str:
    """取爬取方法对应的来源显示名（查不到则退回 domain）。

    功能：按 method.source_id 查 Source 名称，用于审核列表/详情展示友好的来源名。
    谁会调用：discovery_routes 方法详情接口调用。
    直接调用：
    - db.get(...)：查询 Source 记录。
    输入与结果：输入 db 与 method；返回来源名或 method.domain。
    副作用：只读查询数据库。
    """
    source = db.get(Source, method.source_id)
    return source.name if source else method.domain
