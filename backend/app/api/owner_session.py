"""Owner-session protection for internet-exposed self-hosted instances."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.core.rate_limit import rate_limiter
from app.core.session_tokens import SessionTokenError, sign_session, verify_session
from app.settings import Settings, get_settings

OWNER_SESSION_COOKIE = "ope_owner_session"
_OWNER_SESSION_PURPOSE = "self-host-owner"

router = APIRouter(prefix="/api/session", tags=["session"])


class OwnerSessionLogin(BaseModel):
    access_token: str


class OwnerSessionView(BaseModel):
    required: bool
    authenticated: bool
    sharing_enabled: bool
    sharing_disabled_reason: str


def owner_session_authenticated(request: Request, settings: Settings) -> bool:
    if not settings.owner_auth_required:
        return True
    token = request.cookies.get(OWNER_SESSION_COOKIE)
    if not token:
        return False
    try:
        subject = verify_session(
            token,
            purpose=_OWNER_SESSION_PURPOSE,
            secret=settings.secret_key,
            max_age_s=settings.owner_session_ttl_s,
        )
    except SessionTokenError:
        return False
    return subject == "local"


def owner_session_view(request: Request, settings: Settings) -> OwnerSessionView:
    return OwnerSessionView(
        required=settings.owner_auth_required,
        authenticated=owner_session_authenticated(request, settings),
        sharing_enabled=settings.sharing_enabled,
        sharing_disabled_reason=settings.sharing_disabled_reason,
    )


@router.get("", response_model=OwnerSessionView)
async def session_status(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> OwnerSessionView:
    return owner_session_view(request, settings)


@router.post("", response_model=OwnerSessionView)
async def login(
    body: OwnerSessionLogin,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> OwnerSessionView:
    if not settings.owner_auth_required:
        return owner_session_view(request, settings)
    if not settings.sharing_enabled:
        raise HTTPException(status_code=503, detail=settings.sharing_disabled_reason)

    host = request.client.host if request.client else "unknown"
    retry_after = await rate_limiter.try_consume(
        f"owner-login:{host}",
        capacity=5,
        refill_per_s=1 / 60,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many owner login attempts",
            headers={"Retry-After": str(max(1, round(retry_after)))},
        )
    if not hmac.compare_digest(body.access_token, settings.owner_access_token):
        raise HTTPException(status_code=401, detail="Invalid owner access token")

    token = sign_session(
        "local",
        purpose=_OWNER_SESSION_PURPOSE,
        secret=settings.secret_key,
    )
    response.set_cookie(
        OWNER_SESSION_COOKIE,
        token,
        max_age=settings.owner_session_ttl_s,
        httponly=True,
        secure=settings.secure_public_cookies,
        samesite="strict",
        path="/",
    )
    return OwnerSessionView(
        required=True,
        authenticated=True,
        sharing_enabled=True,
        sharing_disabled_reason="",
    )


@router.delete("", response_model=OwnerSessionView)
async def logout(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> OwnerSessionView:
    response.delete_cookie(OWNER_SESSION_COOKIE, path="/")
    return OwnerSessionView(
        required=settings.owner_auth_required,
        authenticated=not settings.owner_auth_required,
        sharing_enabled=settings.sharing_enabled,
        sharing_disabled_reason=settings.sharing_disabled_reason,
    )

