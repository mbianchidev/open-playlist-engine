"""Signed, expiring browser sessions for public share recipients."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, Response

from app.core.session_tokens import SessionTokenError, sign_session, verify_session
from app.settings import Settings

RECIPIENT_SESSION_COOKIE = "ope_share_recipient"
_RECIPIENT_SESSION_PURPOSE = "playlist-share-recipient"


def ensure_recipient_session(
    request: Request,
    response: Response,
    settings: Settings,
) -> str:
    existing = read_recipient_session(request, settings)
    if existing:
        return existing
    session_id = secrets.token_urlsafe(32)
    response.set_cookie(
        RECIPIENT_SESSION_COOKIE,
        sign_session(
            session_id,
            purpose=_RECIPIENT_SESSION_PURPOSE,
            secret=settings.secret_key,
        ),
        max_age=settings.share_recipient_session_ttl_s,
        httponly=True,
        secure=settings.secure_public_cookies,
        samesite="lax",
        path="/api/public/shares",
    )
    return session_id


def read_recipient_session(request: Request, settings: Settings) -> str | None:
    token = request.cookies.get(RECIPIENT_SESSION_COOKIE)
    if not token:
        return None
    try:
        return verify_session(
            token,
            purpose=_RECIPIENT_SESSION_PURPOSE,
            secret=settings.secret_key,
            max_age_s=settings.share_recipient_session_ttl_s,
        )
    except SessionTokenError:
        return None


def require_recipient_session(request: Request, settings: Settings) -> str:
    session_id = read_recipient_session(request, settings)
    if not session_id:
        raise HTTPException(status_code=401, detail="Recipient session required")
    return session_id


def recipient_user_id(share_id: str, session_id: str) -> str:
    return f"share-recipient:{share_id}:{session_id}"

