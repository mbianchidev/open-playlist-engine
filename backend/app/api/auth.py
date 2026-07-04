"""Account connection flow — collapses every provider into 3 challenge shapes."""

from __future__ import annotations

import html
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import (
    AccessDenied,
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
    delete_account,
    list_accounts,
    load_credential,
    load_fresh_credential,
    save_credential,
)
from app.settings import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)


class AccountView(BaseModel):
    id: str
    provider: str
    provider_user_id: str | None = None
    display_name: str | None = None


class ConnectionView(BaseModel):
    status: str
    provider: str
    account: AccountView


class ConnectionTestView(BaseModel):
    status: str
    provider: str
    account_id: str
    message: str


def _provider_error(exc: ProviderError) -> HTTPException:
    if isinstance(exc, AuthExpired):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, RateLimited):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    if isinstance(exc, AccessDenied):
        return HTTPException(status_code=403, detail=str(exc))
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
    check: Annotated[bool, Query()] = False,
) -> list[AccountView]:
    rows = await list_accounts(session, user_id=user_id, provider=provider)
    if not check:
        return [_account_view(account) for account in rows]

    valid_accounts = []
    removed = False
    for account in rows:
        try:
            adapter = get(account.provider)
            credential, _ = await load_fresh_credential(
                session,
                account_id=account.id,
                adapter=adapter,
                provider=account.provider,
            )
            await adapter.test_connection(credential)
            valid_accounts.append(account)
        except KeyError:
            valid_accounts.append(account)
        except AuthExpired:
            removed = True
            logger.info(
                "removing expired provider account provider=%s account_id=%s",
                account.provider,
                account.id,
            )
            await delete_account(session, account_id=account.id, user_id=user_id)
        except ProviderError:
            valid_accounts.append(account)
    if removed:
        await session.commit()
    return [_account_view(account) for account in valid_accounts]


@router.post("/accounts/{account_id}/test", response_model=ConnectionTestView)
async def test_account_connection(
    account_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: str = "local",
) -> ConnectionTestView:
    try:
        _, account = await load_credential(session, account_id=account_id)
        if account.user_id != user_id:
            raise AccountNotFound(account_id)
        adapter = get(account.provider)
        credential, account = await load_fresh_credential(
            session,
            account_id=account_id,
            adapter=adapter,
            provider=account.provider,
        )
        await adapter.test_connection(credential)
        await session.commit()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AccountNotFound, CredentialNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AuthExpired as exc:
        await delete_account(session, account_id=account_id, user_id=user_id)
        await session.commit()
        raise HTTPException(
            status_code=401,
            detail=f"{exc}; account was disconnected",
        ) from exc
    except ProviderError as exc:
        raise _provider_error(exc) from exc
    return ConnectionTestView(
        status="ok",
        provider=account.provider,
        account_id=account.id,
        message=f"{account.provider} connection is working",
    )


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
