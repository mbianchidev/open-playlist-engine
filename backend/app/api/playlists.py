"""Source playlist browsing (phase 1-2)."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.models import PlaylistRef

router = APIRouter(prefix="/api/playlists", tags=["playlists"])


@router.get("", response_model=list[PlaylistRef])
async def list_playlists(provider: str, account_id: str) -> list[PlaylistRef]:
    # TODO: stream from adapter.iter_playlists(cred) for the given account.
    return []
