"""Source playlist browsing (phase 1-2)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapter import AuthExpired, NotFound, ProviderError, RateLimited
from app.core.models import PlaylistRef
from app.core.registry import get
from app.db.base import get_session
from app.db.repositories import AccountNotFound, CredentialNotFound, load_fresh_credential

router = APIRouter(prefix="/api/playlists", tags=["playlists"])


@router.get("", response_model=list[PlaylistRef])
async def list_playlists(
    provider: str,
    account_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[PlaylistRef]:
    try:
        adapter = get(provider)
        credential, _ = await load_fresh_credential(
            session, account_id=account_id, adapter=adapter, provider=provider
        )
        playlists = [playlist async for playlist in adapter.iter_playlists(credential)]
        await session.commit()
        return playlists
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (AccountNotFound, CredentialNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AuthExpired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
