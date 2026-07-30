from __future__ import annotations

import base64
import fcntl
import logging
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import SessionLocal
from app.models import WechatAuthProfile

logger = logging.getLogger(__name__)

QR_TERMINAL_STATUSES = {"success", "expired", "failed", "cancelled"}
QR_SESSION_RETENTION_SECONDS = 900
QR_SESSION_MAX_RECORDS = 100
_SAFE_PROFILE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_profile_name(profile_name: str) -> str:
    return _SAFE_PROFILE_NAME.sub("_", profile_name).strip("._") or "default"


def profile_browser_data_dir(profile_name: str) -> Path:
    """Persistent Chromium profile the QR login reuses across attempts."""
    return Path(get_settings().wechat_browser_data_dir) / _safe_profile_name(profile_name)


def _login_lock_path(profile_name: str) -> Path:
    return Path(get_settings().wechat_browser_data_dir) / f".{_safe_profile_name(profile_name)}.login.lock"


def get_stored_profile(profile_name: str) -> WechatAuthProfile | None:
    db = SessionLocal()
    try:
        return db.scalar(
            select(WechatAuthProfile).where(WechatAuthProfile.profile_name == profile_name)
        )
    finally:
        db.close()


def resolve_profile(profile_name: str) -> dict[str, Any]:
    """Resolve the latest DB credential, falling back to env only before first save."""
    profile = get_stored_profile(profile_name)
    if profile is not None:
        if profile.status != "valid":
            return {
                "status": "auth_invalid",
                "auth_ref": profile_name,
                "reason": profile.last_error or f"stored profile status is {profile.status}",
            }
        return {
            "status": "ok",
            "auth_ref": profile_name,
            "cookie": profile.cookie,
            "token": profile.token,
        }

    settings = get_settings()
    expected = settings.wechat_mp_profile_name or "wechat_mp_default"
    if profile_name != expected:
        return {"status": "auth_invalid", "reason": "unknown auth_ref"}
    if not settings.wechat_mp_cookie or not settings.wechat_mp_token:
        return {
            "status": "pending_auth",
            "reason": "WeChat authentication has not been configured",
        }
    return {
        "status": "ok",
        "auth_ref": profile_name,
        "cookie": settings.wechat_mp_cookie,
        "token": settings.wechat_mp_token,
    }


def profile_public_status(profile_name: str) -> dict[str, Any]:
    profile = get_stored_profile(profile_name)
    if profile is not None:
        return {
            "profile_name": profile.profile_name,
            "status": profile.status,
            "configured": bool(profile.cookie and profile.token),
            "source": "database",
            "last_error": profile.last_error,
            "updated_at": _iso(profile.updated_at),
            "last_verified_at": _iso(profile.last_verified_at),
        }

    settings = get_settings()
    configured = bool(settings.wechat_mp_cookie and settings.wechat_mp_token)
    return {
        "profile_name": profile_name,
        "status": "valid" if configured else "unconfigured",
        "configured": configured,
        "source": "environment" if configured else "none",
        "last_error": None,
        "updated_at": None,
        "last_verified_at": None,
    }


def verify_profile(profile_name: str) -> dict[str, Any]:
    """Probe WeChat with the stored credential and persist the real verdict.

    This is the only place the reported status is backed by WeChat itself; the
    stored ``status`` is otherwise just bookkeeping from the last login or the
    last crawl. An ``unknown`` result leaves the stored status untouched on
    purpose, so a network blip never revokes a working credential.
    """
    from app.discovery.wechat_tools import probe_mp_session

    profile = get_stored_profile(profile_name)
    if profile is not None:
        # A stored row with no credential means logged out or expired, and it
        # deliberately shadows any env fallback: falling back would report a
        # stale env cookie as valid right after an explicit logout.
        if not (profile.cookie and profile.token):
            return {
                "result": "unconfigured",
                "reason": "当前没有已保存的微信登录态，请扫码登录。",
                "checked_at": _iso(utc_now()),
                "profile": profile_public_status(profile_name),
            }
        cookie, token = profile.cookie, profile.token
        persistable = True
    else:
        settings = get_settings()
        expected = settings.wechat_mp_profile_name or "wechat_mp_default"
        if (
            profile_name != expected
            or not settings.wechat_mp_cookie
            or not settings.wechat_mp_token
        ):
            return {
                "result": "unconfigured",
                "reason": "尚未配置微信登录态，请先扫码登录。",
                "checked_at": _iso(utc_now()),
                "profile": profile_public_status(profile_name),
            }
        cookie, token = settings.wechat_mp_cookie, settings.wechat_mp_token
        # An env-only credential has no row to update; report the live verdict
        # and let an expiry create the usual sentinel below.
        persistable = False

    result, reason = probe_mp_session(cookie=cookie, token=token)
    if result == "expired":
        mark_profile_expired(profile_name, reason or "WeChat MP login expired", token, cookie)
    elif result == "valid" and persistable:
        mark_profile_verified(profile_name, token, cookie)
    logger.info("wechat_auth_verify profile=%s result=%s", profile_name, result)
    return {
        "result": result,
        "reason": reason,
        "checked_at": _iso(utc_now()),
        "profile": profile_public_status(profile_name),
    }


def logout_profile(profile_name: str) -> dict[str, Any]:
    """Drop the stored credential and the retained browser session.

    Clearing the Chromium profile is what makes the next login a real scan: the
    QR flow reuses a persistent profile, and while that profile still holds a
    WeChat session it logs straight back in as the same account without ever
    showing a code.
    """
    lock_path = _login_lock_path(profile_name)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("扫码会话正在进行中，请先取消该会话再退出登录。") from exc
        try:
            browser_data_dir = profile_browser_data_dir(profile_name)
            browser_data_cleared = browser_data_dir.exists()
            if browser_data_cleared:
                shutil.rmtree(browser_data_dir, ignore_errors=True)
                browser_data_cleared = not browser_data_dir.exists()
            _clear_stored_credential(profile_name)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()

    logger.info(
        "wechat_auth_logout profile=%s browser_data_cleared=%s",
        profile_name,
        browser_data_cleared,
    )
    return {
        "browser_data_cleared": browser_data_cleared,
        "profile": profile_public_status(profile_name),
    }


def _clear_stored_credential(profile_name: str) -> None:
    """Blank the row rather than delete it, so it keeps shadowing the env fallback."""
    for attempt in range(2):
        db = SessionLocal()
        try:
            profile = db.scalar(
                select(WechatAuthProfile)
                .where(WechatAuthProfile.profile_name == profile_name)
                .with_for_update()
            )
            now = utc_now()
            if profile is None:
                profile = WechatAuthProfile(
                    profile_name=profile_name,
                    cookie="",
                    token="",
                    status="unconfigured",
                    updated_at=now,
                )
                db.add(profile)
            else:
                profile.cookie = ""
                profile.token = ""
                profile.status = "unconfigured"
                profile.last_error = None
                profile.last_verified_at = None
                profile.updated_at = now
            db.commit()
            return
        except IntegrityError:
            db.rollback()
            if attempt > 0:
                raise
            # A concurrent writer inserted the row first; retry under the lock.
        finally:
            db.close()


def mark_profile_verified(profile_name: str, credential_token: str, credential_cookie: str) -> None:
    """Record a confirmed-live credential; heals a status an earlier probe soured."""
    db = SessionLocal()
    try:
        profile = db.scalar(
            select(WechatAuthProfile)
            .where(WechatAuthProfile.profile_name == profile_name)
            .with_for_update()
        )
        if profile is None:
            return
        # A verdict about an older credential must not bless a renewed one.
        if profile.token != credential_token or profile.cookie != credential_cookie:
            return
        profile.status = "valid"
        profile.last_error = None
        profile.last_verified_at = utc_now()
        db.commit()
    finally:
        db.close()


def save_profile(profile_name: str, cookie: str, token: str) -> None:
    for attempt in range(2):
        db = SessionLocal()
        try:
            profile = db.scalar(
                select(WechatAuthProfile)
                .where(WechatAuthProfile.profile_name == profile_name)
                .with_for_update()
            )
            now = utc_now()
            if profile is None:
                profile = WechatAuthProfile(
                    profile_name=profile_name,
                    cookie=cookie,
                    token=token,
                    status="valid",
                    last_verified_at=now,
                )
                db.add(profile)
            else:
                profile.cookie = cookie
                profile.token = token
                profile.status = "valid"
                profile.last_error = None
                profile.last_verified_at = now
                profile.updated_at = now
            db.commit()
            return
        except IntegrityError:
            db.rollback()
            if attempt > 0:
                raise
            # A concurrent expiry sentinel may have been inserted. Retry and
            # replace it under a row lock with the successful login credential.
        finally:
            db.close()


def mark_profile_expired(
    profile_name: str,
    reason: str,
    credential_token: str | None,
    credential_cookie: str | None,
) -> None:
    """Invalidate only a stored profile; never persist or log credential material."""
    for attempt in range(2):
        db = SessionLocal()
        try:
            profile = db.scalar(
                select(WechatAuthProfile)
                .where(WechatAuthProfile.profile_name == profile_name)
                .with_for_update()
            )
            now = utc_now()
            if profile is None:
                # Create an invalid sentinel so an expired env fallback is not retried forever.
                profile = WechatAuthProfile(
                    profile_name=profile_name,
                    cookie="",
                    token="",
                    status="expired",
                    last_error=reason[:1000],
                    updated_at=now,
                )
                db.add(profile)
            else:
                # An old in-flight request must not invalidate a freshly renewed credential.
                if (
                    credential_token is not None
                    and credential_cookie is not None
                    and (profile.token != credential_token or profile.cookie != credential_cookie)
                ):
                    return
                profile.status = "expired"
                profile.last_error = reason[:1000]
                profile.updated_at = now
            db.commit()
            return
        except IntegrityError:
            db.rollback()
            if attempt > 0:
                logger.warning(
                    "wechat_auth_expiry_sentinel_conflict profile=%s",
                    profile_name,
                )
                return
            # A concurrent request inserted the sentinel (or renewed credentials).
            # Retry under a row lock and apply the credential-version guard.
        finally:
            db.close()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class QrLoginSession:
    session_id: str
    profile_name: str
    status: str = "pending"
    qr_image_data_url: str | None = None
    message: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    lock_file: Any = field(default=None, repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "profile_name": self.profile_name,
            "status": self.status,
            "qr_image_data_url": self.qr_image_data_url,
            "message": self.message,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "expires_at": _iso(self.expires_at),
        }


class WechatQrLoginManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, QrLoginSession] = {}
        self._profile_sessions: dict[str, str] = {}

    def start(self, profile_name: str) -> dict[str, Any]:
        with self._lock:
            self._prune_locked()
            previous_id = self._profile_sessions.get(profile_name)
            previous = self._sessions.get(previous_id or "")
            if previous is not None and previous.thread is not None and previous.thread.is_alive():
                if previous.status not in QR_TERMINAL_STATUSES:
                    raise RuntimeError("A QR login session is already active for this profile")
                raise RuntimeError("The previous QR login session is still cleaning up")

            settings = get_settings()
            lock_path = _login_lock_path(profile_name)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = lock_path.open("a+")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                lock_file.close()
                raise RuntimeError("A QR login session is already active for this profile") from exc

            if previous is not None:
                self._sessions.pop(previous.session_id, None)

            timeout = max(60, settings.wechat_qr_session_timeout_seconds)
            session = QrLoginSession(
                session_id=str(uuid.uuid4()),
                profile_name=profile_name,
                expires_at=datetime.fromtimestamp(time.time() + timeout, tz=timezone.utc),
                lock_file=lock_file,
            )
            thread = threading.Thread(
                target=self._run,
                args=(session, timeout),
                name=f"wechat-qr-{session.session_id[:8]}",
                daemon=True,
            )
            session.thread = thread
            self._sessions[session.session_id] = session
            self._profile_sessions[profile_name] = session.session_id
            try:
                thread.start()
            except Exception:
                self._sessions.pop(session.session_id, None)
                self._profile_sessions.pop(profile_name, None)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
                raise
            return session.public_dict()

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(session_id)
            return session.public_dict() if session is not None else None

    def cancel(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.status not in QR_TERMINAL_STATUSES:
                session.cancel_event.set()
                self._update_locked(
                    session,
                    status="cancelled",
                    qr_image_data_url=None,
                    message="扫码登录已取消",
                )
            return session.public_dict()

    def shutdown_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            for session in sessions:
                session.cancel_event.set()
        deadline = time.monotonic() + 25
        for session in sessions:
            thread = session.thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    logger.warning(
                        "wechat_qr_thread_did_not_stop profile=%s",
                        session.profile_name,
                    )

    def _prune_locked(self) -> None:
        cutoff = time.time() - QR_SESSION_RETENTION_SECONDS
        removable = [
            session_id
            for session_id, session in self._sessions.items()
            if session.status in QR_TERMINAL_STATUSES
            and (session.thread is None or not session.thread.is_alive())
            and session.updated_at.timestamp() < cutoff
        ]
        for session_id in removable:
            session = self._sessions.pop(session_id)
            if self._profile_sessions.get(session.profile_name) == session_id:
                self._profile_sessions.pop(session.profile_name, None)

        if len(self._sessions) <= QR_SESSION_MAX_RECORDS:
            return
        terminal = sorted(
            (
                session
                for session in self._sessions.values()
                if session.status in QR_TERMINAL_STATUSES
                and (session.thread is None or not session.thread.is_alive())
            ),
            key=lambda item: item.updated_at,
        )
        for session in terminal[: max(0, len(self._sessions) - QR_SESSION_MAX_RECORDS)]:
            self._sessions.pop(session.session_id, None)
            if self._profile_sessions.get(session.profile_name) == session.session_id:
                self._profile_sessions.pop(session.profile_name, None)

    def _update(self, session: QrLoginSession, **changes: Any) -> None:
        with self._lock:
            if session.status in QR_TERMINAL_STATUSES:
                return
            self._update_locked(session, **changes)

    @staticmethod
    def _update_locked(session: QrLoginSession, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(session, key, value)
        session.updated_at = utc_now()

    def _run(self, session: QrLoginSession, timeout: int) -> None:
        playwright = None
        context = None
        try:
            from playwright.sync_api import sync_playwright

            user_data_dir = profile_browser_data_dir(session.profile_name)
            user_data_dir.mkdir(parents=True, exist_ok=True)

            playwright = sync_playwright().start()
            context = playwright.chromium.launch_persistent_context(
                str(user_data_dir),
                headless=True,
                viewport={"width": 1180, "height": 820},
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                timeout=10_000,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://mp.weixin.qq.com/", wait_until="domcontentloaded", timeout=10_000)
            if self._save_authenticated_context(session, page.url, context):
                return
            self._publish_screenshot(session, page, "请使用微信扫描二维码并在手机上确认")

            deadline = time.monotonic() + timeout
            last_screenshot = 0.0
            while time.monotonic() < deadline:
                if session.cancel_event.wait(1):
                    return

                if self._save_authenticated_context(session, page.url, context):
                    return

                body_text = ""
                try:
                    body_text = page.locator("body").inner_text(timeout=2_000)
                except Exception:
                    pass
                if any(marker in body_text for marker in ("扫描成功", "手机上确认", "点击确认登录")):
                    self._update(session, status="scanned", message="已扫码，请在手机上确认登录")

                if time.monotonic() - last_screenshot >= 8:
                    self._publish_screenshot(
                        session,
                        page,
                        "已扫码，请在手机上确认登录"
                        if session.status == "scanned"
                        else "请使用微信扫描二维码并在手机上确认",
                    )
                    last_screenshot = time.monotonic()

            self._update(
                session,
                status="expired",
                qr_image_data_url=None,
                message="二维码已过期，请重新发起扫码",
            )
        except Exception as exc:
            logger.error(
                "wechat_qr_login_failed profile=%s error_type=%s",
                session.profile_name,
                type(exc).__name__,
            )
            self._update(
                session,
                status="failed",
                qr_image_data_url=None,
                message=f"扫码登录失败：{type(exc).__name__}",
            )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    logger.warning("wechat_qr_context_close_failed")
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    logger.warning("wechat_qr_playwright_stop_failed")
            if session.lock_file is not None:
                try:
                    fcntl.flock(session.lock_file.fileno(), fcntl.LOCK_UN)
                    session.lock_file.close()
                except Exception:
                    logger.warning("wechat_qr_lock_release_failed")
                session.lock_file = None

    def _save_authenticated_context(self, session: QrLoginSession, url: str, context: Any) -> bool:
        token = _token_from_url(url)
        if not token:
            return False
        cookies = context.cookies(["https://mp.weixin.qq.com/"])
        cookie = "; ".join(
            f"{item['name']}={item['value']}"
            for item in cookies
            if item.get("name") and item.get("value")
        )
        if not cookie:
            return False
        # Linearize success with cancel: whichever acquires the manager lock first wins.
        with self._lock:
            if session.cancel_event.is_set() or session.status in QR_TERMINAL_STATUSES:
                return False
            save_profile(session.profile_name, cookie, token)
            self._update_locked(
                session,
                status="success",
                qr_image_data_url=None,
                message="登录成功，认证信息已安全保存并立即生效",
            )
        logger.info("wechat_qr_login_success profile=%s", session.profile_name)
        return True

    def _publish_screenshot(self, session: QrLoginSession, page: Any, message: str) -> None:
        if session.cancel_event.is_set():
            return
        try:
            qr_locator = page.locator(
                "img.login__type__container__scan__qrcode,"
                " img[src*='scanloginqrcode'], img[src*='qrcode'],"
                " canvas"
            ).first
            if not qr_locator.is_visible(timeout=1_500):
                return
            raw = qr_locator.screenshot(type="png")
        except Exception:
            return
        encoded = base64.b64encode(raw).decode("ascii")
        with self._lock:
            if session.cancel_event.is_set() or session.status in QR_TERMINAL_STATUSES:
                return
            status = session.status if session.status == "scanned" else "qr_ready"
            self._update_locked(
                session,
                status=status,
                qr_image_data_url=f"data:image/png;base64,{encoded}",
                message=message,
            )


def _token_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.hostname != "mp.weixin.qq.com":
        return None
    values = parse_qs(parsed.query).get("token")
    token = values[0].strip() if values else ""
    return token if token.isdigit() else None


wechat_qr_login_manager = WechatQrLoginManager()
