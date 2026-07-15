"""``GET /providers`` — the capability matrix the frontend renders.

The UI uses ``can_source`` / ``can_target`` to populate the source and target
pickers and surfaces ``warning`` inline.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.capabilities import Capability
from app.core.registry import all_adapters
from app.core.sync import mirror_unavailable_reason

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
    can_mirror: bool
    mirror_unavailable_reason: str | None = None
    warning: str | None = None


@router.get("/providers", response_model=list[ProviderView])
async def list_providers() -> list[ProviderView]:
    views: list[ProviderView] = []
    for adapter in all_adapters():
        info = adapter.info
        caps = info.capabilities
        mirror_reason = mirror_unavailable_reason(adapter)
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
                can_mirror=mirror_reason is None,
                mirror_unavailable_reason=mirror_reason,
                warning=caps.warning,
            )
        )
    return views
