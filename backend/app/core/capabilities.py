"""Structured capability descriptors.

The duck review flagged that boolean caps ("can write") are not enough — the UI
and core scheduler need limits (batch size, ordering, quota cost, allowed
visibility values). Adapters advertise a :class:`CapabilityDescriptor`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Capability(StrEnum):
    READ_PLAYLISTS = "read_playlists"
    READ_TRACKS = "read_tracks"
    READ_LIBRARY = "read_library"
    WRITE_LIBRARY = "write_library"
    CREATE_PLAYLIST = "create_playlist"
    ADD_TRACKS = "add_tracks"
    REMOVE_TRACKS = "remove_tracks"
    UNFOLLOW_PLAYLIST = "unfollow_playlist"
    DELETE_PLAYLIST = "delete_playlist"
    REORDER = "reorder"
    SET_COVER = "set_cover"
    SET_DESCRIPTION = "set_description"


class SearchMode(StrEnum):
    ISRC = "isrc"
    TEXT = "text"


class OrderingGuarantee(StrEnum):
    PRESERVED = "preserved"
    BEST_EFFORT = "best_effort"
    NONE = "none"


class Stability(StrEnum):
    STABLE = "stable"
    BETA = "beta"
    EXPERIMENTAL = "experimental"
    UNAVAILABLE = "unavailable"


class CapabilityDescriptor(BaseModel):
    capabilities: set[Capability] = Field(default_factory=set)
    has_isrc: bool = False
    search_modes: list[SearchMode] = Field(default_factory=lambda: [SearchMode.TEXT])
    official: bool = True
    stability: Stability = Stability.STABLE

    # Write constraints
    max_add_batch: int = 100
    max_remove_batch: int = 100
    max_playlist_size: int | None = None
    supports_duplicates: bool = True
    ordering: OrderingGuarantee = OrderingGuarantee.PRESERVED
    description_max_len: int | None = None

    # Cost / pacing hints used by the central rate limiter
    search_quota_cost: int = 1
    write_quota_cost: int = 1
    daily_quota: int | None = None

    # Free-form caveat surfaced in the UI (e.g. "unofficial — may break").
    warning: str | None = None

    def can(self, cap: Capability) -> bool:
        return cap in self.capabilities
