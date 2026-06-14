"""Account connection flow — collapses every provider into 3 challenge shapes."""

from __future__ import annotations

import html
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import (
    AuthChallenge,
    AuthExpired,
    NotFound,
    ProviderError,
    RateLimited,
    Unsupported,
)
from app.core.registry import get
from app.db.base import get_session
from app.db.repositories import (
    AccountNotFound,
    CredentialNotFound,
    list_accounts,
    save_credential,
)
from app.settings import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class AccountView(BaseModel):
    id: str
    provider: str
    provider_user_id: str | None = None
    display_name: str | None = None


class ConnectionView(BaseModel):
    status: str
    provider: str
    account: AccountView


def _provider_error(exc: ProviderError) -> HTTPException:
    if isinstance(exc, AuthExpired):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, RateLimited):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, Unsupported):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _account_view(account) -> AccountView:
    return AccountView(
        id=account.id,
        provider=account.provider,
        provider_user_id=account.provider_user_id,
        display_name=account.display_name,
    )


@router.post("/{provider}/begin", response_model=AuthChallenge)
async def begin(provider: str, user_id: str = "local") -> AuthChallenge:
    try:
        adapter = get(provider)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return await adapter.auth.begin(user_id=user_id)
    except ProviderError as exc:
        raise _provider_error(exc) from exc


@router.get("/accounts", response_model=list[AccountView])
async def accounts(
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: str = "local",
    provider: Annotated[str | None, Query()] = None,
) -> list[AccountView]:
    accounts = await list_accounts(session, user_id=user_id, provider=provider)
    return [_account_view(account) for account in accounts]


@router.post("/{provider}/complete", response_model=ConnectionView)
async def complete(
    provider: str,
    callback: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: str = "local",
) -> ConnectionView:
    try:
        adapter = get(provider)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        credential = await adapter.auth.complete(user_id=user_id, callback=callback)
        account = await save_credential(
            session,
            user_id=user_id,
            provider=provider,
            provider_user_id=credential.account_id,
            display_name=credential.extra.get("display_name"),
            credential=credential,
        )
        await session.commit()
    except ProviderError as exc:
        raise _provider_error(exc) from exc
    except (AccountNotFound, CredentialNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ConnectionView(status="connected", provider=provider, account=_account_view(account))


@router.get("/{provider}/callback", response_class=HTMLResponse)
async def callback(
    provider: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: str = "local",
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> str:
    result = await complete(
        provider,
        {"code": code, "state": state, "error": error},
        session,
        user_id,
    )
    app_url = get_settings().frontend_url
    name = html.escape(
        result.account.display_name or result.account.provider_user_id or result.account.id
    )
    provider_name = html.escape(provider.title())
    app_href = html.escape(app_url, quote=True)
    return f"""
<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Open Playlist Engine</title></head>
  <body>
    <h1>{provider_name} connected</h1>
    <p>Connected account: {name}</p>
    <p>You can close this tab and return to Open Playlist Engine.</p>
    <p><a href="{app_href}">Return to app</a></p>
    <script>window.opener && window.close();</script>
  </body>
</html>
"""
