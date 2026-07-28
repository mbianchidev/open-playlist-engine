from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.api.owner_session import owner_session_authenticated
from app.settings import Settings, get_settings


def get_current_user_id(
    settings: Annotated[Settings, Depends(get_settings)],
    request: Request = None,
) -> str:
    if settings.is_hosted:
        raise HTTPException(
            status_code=501,
            detail="Hosted user authentication is not configured",
        )
    if settings.owner_auth_required:
        if not settings.sharing_enabled:
            raise HTTPException(status_code=503, detail=settings.sharing_disabled_reason)
        if request is None or not owner_session_authenticated(request, settings):
            raise HTTPException(status_code=401, detail="Owner authentication required")
    return "local"


CurrentUserId = Annotated[str, Depends(get_current_user_id)]
