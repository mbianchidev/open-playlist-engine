"""``GET /providers`` — the capability matrix the frontend renders.

The UI uses ``can_source`` / ``can_target`` to populate the source and target
pickers and surfaces ``warning`` inline.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.capabilities import Capability
from app.core.registry import all_info
from app.imports import LOCAL_FILE_PROVIDER

router = APIRouter(prefix="/api", tags=["providers"])


class ProviderView(BaseModel):
    name: str
    display_name: str
    auth_kind: str
    official: bool
    stability: str
    has_isrc: bool
    can_source: bool
    can_target: bool
    warning: str | None = None


@router.get("/providers", response_model=list[ProviderView])
async def list_providers() -> list[ProviderView]:
    views: list[ProviderView] = [
        ProviderView(
            name=LOCAL_FILE_PROVIDER,
            display_name="Local playlist file",
            auth_kind="upload",
            official=True,
            stability="stable",
            has_isrc=True,
            can_source=True,
            can_target=False,
        )
    ]
    for info in all_info():
        caps = info.capabilities
        views.append(
            ProviderView(
                name=info.name,
                display_name=info.display_name,
                auth_kind=info.auth_kind.value,
                official=caps.official,
                stability=caps.stability.value,
                has_isrc=caps.has_isrc,
                can_source=caps.can(Capability.READ_TRACKS),
                can_target=caps.can(Capability.CREATE_PLAYLIST) and caps.can(Capability.ADD_TRACKS),
                warning=caps.warning,
            )
        )
    return views
