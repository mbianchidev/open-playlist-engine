from typing import Annotated

from fastapi import Depends, HTTPException

from app.settings import Settings, get_settings


def get_current_user_id(
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    if settings.is_hosted:
        raise HTTPException(
            status_code=501,
            detail="Hosted user authentication is not configured",
        )
    return "local"


CurrentUserId = Annotated[str, Depends(get_current_user_id)]
