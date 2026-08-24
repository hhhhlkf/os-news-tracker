"""DEPRECATED / DORMANT HTTP compatibility for WeChat MP authentication.

These authenticated endpoints remain registered so existing operators are not
broken, but the active WeChat Discovery and formal connector never use them.
Do not add new product dependencies; removal is a separately gated cleanup.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.api.deps import require_system_access
from app.config import get_settings
from app.wechat_auth import (
    logout_profile,
    profile_public_status,
    verify_profile,
    wechat_qr_login_manager,
)

router = APIRouter(prefix="/wechat-auth", tags=["wechat-auth"])


class WechatQrStartRequest(BaseModel):
    profile_name: str | None = Field(default=None, min_length=1, max_length=100)


def _profile_name(requested: str | None) -> str:
    expected = get_settings().wechat_mp_profile_name or "wechat_mp_default"
    resolved = requested.strip() if requested is not None else expected
    if resolved != expected:
        raise HTTPException(status_code=400, detail="Only the configured WeChat profile is supported")
    return resolved


@router.get("/profile", deprecated=True)
def get_wechat_auth_profile(
    response: Response,
    profile_name: str | None = None,
    _access: dict[str, Any] = Depends(require_system_access),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return profile_public_status(_profile_name(profile_name))


@router.post("/profile/verify", deprecated=True)
def verify_wechat_auth_profile(
    response: Response,
    profile_name: str | None = None,
    _access: dict[str, Any] = Depends(require_system_access),
) -> dict[str, Any]:
    """Check the stored credential against WeChat instead of trusting the DB status."""
    response.headers["Cache-Control"] = "no-store"
    return verify_profile(_profile_name(profile_name))


@router.post("/profile/logout", deprecated=True)
def logout_wechat_auth_profile(
    response: Response,
    profile_name: str | None = None,
    _access: dict[str, Any] = Depends(require_system_access),
) -> dict[str, Any]:
    """Clear the credential and the retained browser session so the next login rescans."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return logout_profile(_profile_name(profile_name))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/qr-sessions", status_code=202, deprecated=True)
def start_wechat_qr_session(
    request: WechatQrStartRequest,
    response: Response,
    _access: dict[str, Any] = Depends(require_system_access),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return wechat_qr_login_manager.start(_profile_name(request.profile_name))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/qr-sessions/{session_id}", deprecated=True)
def get_wechat_qr_session(
    session_id: str,
    response: Response,
    _access: dict[str, Any] = Depends(require_system_access),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    result = wechat_qr_login_manager.get(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="QR login session not found")
    return result


@router.post("/qr-sessions/{session_id}/cancel", deprecated=True)
def cancel_wechat_qr_session(
    session_id: str,
    response: Response,
    _access: dict[str, Any] = Depends(require_system_access),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    result = wechat_qr_login_manager.cancel(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="QR login session not found")
    return result
