"""Account connection flow — collapses every provider into 3 challenge shapes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.adapter import AuthChallenge
from app.core.registry import get

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/{provider}/begin", response_model=AuthChallenge)
async def begin(provider: str, user_id: str = "local") -> AuthChallenge:
    try:
        adapter = get(provider)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await adapter.auth.begin(user_id=user_id)


@router.post("/{provider}/complete")
async def complete(provider: str, callback: dict, user_id: str = "local") -> dict:
    try:
        adapter = get(provider)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # TODO: persist encrypted credential; return the connected account summary.
    await adapter.auth.complete(user_id=user_id, callback=callback)
    return {"status": "connected", "provider": provider}
