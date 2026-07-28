from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

from app.core.capabilities import Capability, CapabilityDescriptor
from app.core.models import Playlist, PlaylistKind, PlaylistRef, Track

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


class OrganizerIntent(StrEnum):
    REMOVE = "remove"
    DELETE = "delete"
    REMOVE_TRACKS = "remove_tracks"


class OrganizerAction(StrEnum):
    UNFOLLOW_PLAYLIST = "unfollow_playlist"
    DELETE_PLAYLIST = "delete_playlist"
    REMOVE_TRACKS = "remove_tracks"


class PlaylistActionSelection(BaseModel):
    playlist_id: str
    intent: OrganizerIntent = OrganizerIntent.REMOVE


class TrackSelection(BaseModel):
    position: int
    source_item_id: str | None = None


class PlaylistTrackSelection(BaseModel):
    playlist_id: str
    tracks: list[TrackSelection] = Field(default_factory=list)


class OrganizerSelection(BaseModel):
    playlist_actions: list[PlaylistActionSelection] = Field(default_factory=list)
    track_removals: list[PlaylistTrackSelection] = Field(default_factory=list)


class ResolvedOrganizerItem(BaseModel):
    playlist: PlaylistRef
    action: OrganizerAction
    destructive: bool
    recovery: str
    selected_track_count: int = 0
    request_payload: dict = Field(default_factory=dict)


class UnsupportedOrganizerItem(BaseModel):
    playlist_id: str
    playlist_name: str
    intent: OrganizerIntent
    reason: str


class OrganizerResolution(BaseModel):
    items: list[ResolvedOrganizerItem] = Field(default_factory=list)
    unsupported: list[UnsupportedOrganizerItem] = Field(default_factory=list)
    confirmation_phrase: str | None = None


@dataclass(frozen=True)
class DuplicateCandidate:
    playlist_ids: tuple[str, str]
    playlist_names: tuple[str, str]
    normalized_name: str
    overlap_count: int
    overlap_ratio: float
    reasons: tuple[str, ...]


def resolve_playlist_action(
    intent: OrganizerIntent,
    capabilities: CapabilityDescriptor,
    playlist: PlaylistRef,
) -> OrganizerAction | None:
    if playlist.kind is PlaylistKind.LIKED_TRACKS:
        return None
    if intent is OrganizerIntent.REMOVE:
        return (
            OrganizerAction.UNFOLLOW_PLAYLIST
            if capabilities.can(Capability.UNFOLLOW_PLAYLIST)
            else None
        )
    if intent is OrganizerIntent.DELETE:
        if capabilities.can(Capability.DELETE_PLAYLIST) and playlist.is_owned is True:
            return OrganizerAction.DELETE_PLAYLIST
        return None
    if capabilities.can(Capability.REMOVE_TRACKS) and (
        playlist.is_owned is True or playlist.collaborative is True
    ):
        return OrganizerAction.REMOVE_TRACKS
    return None


def build_confirmation_phrase(
    *,
    delete_count: int,
    removed_track_count: int,
) -> str | None:
    parts: list[str] = []
    if delete_count:
        parts.append(f"DELETE {delete_count} {_plural(delete_count, 'PLAYLIST')}")
    if removed_track_count:
        parts.append(f"REMOVE {removed_track_count} {_plural(removed_track_count, 'SONG')}")
    return " AND ".join(parts) or None


def playlist_sequence_hash(tracks: list[Track]) -> str:
    encoded = json.dumps(
        [_track_token(track) for track in tracks],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def detect_duplicate_candidates(playlists: list[Playlist]) -> list[DuplicateCandidate]:
    grouped: dict[str, list[Playlist]] = {}
    for playlist in playlists:
        if playlist.id:
            grouped.setdefault(normalize_playlist_name(playlist.name), []).append(playlist)

    candidates: list[DuplicateCandidate] = []
    for normalized_name, group in grouped.items():
        if not normalized_name or len(group) < 2:
            continue
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                if not _owners_compatible(left, right):
                    continue
                left_tracks = {_track_token(track) for track in left.tracks}
                right_tracks = {_track_token(track) for track in right.tracks}
                if not left_tracks or not right_tracks:
                    continue
                overlap_count = len(left_tracks & right_tracks)
                overlap_ratio = overlap_count / min(len(left_tracks), len(right_tracks))
                if overlap_ratio < 0.5:
                    continue
                reasons = ["normalized name"]
                if _same_known_owner(left, right):
                    reasons.append("same owner")
                reasons.append(f"{overlap_count} overlapping tracks")
                candidates.append(
                    DuplicateCandidate(
                        playlist_ids=(left.id or "", right.id or ""),
                        playlist_names=(left.name, right.name),
                        normalized_name=normalized_name,
                        overlap_count=overlap_count,
                        overlap_ratio=round(overlap_ratio, 4),
                        reasons=tuple(reasons),
                    )
                )
    return candidates


def normalize_playlist_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return _NON_ALPHANUMERIC.sub(" ", normalized.casefold()).strip()


def _track_token(track: Track) -> str:
    provider_uris = [uri for _, uri in sorted(track.provider_uris.items()) if uri]
    if provider_uris:
        return f"uri:{provider_uris[0]}"
    if track.id:
        return f"id:{track.id}"
    return "meta:" + "|".join(
        [
            normalize_playlist_name(track.title),
            normalize_playlist_name(track.artist),
            normalize_playlist_name(track.album or ""),
            str(track.duration_s or ""),
        ]
    )


def _owners_compatible(left: Playlist, right: Playlist) -> bool:
    if left.owner_id and right.owner_id:
        return left.owner_id == right.owner_id
    if left.owner_name and right.owner_name:
        return normalize_playlist_name(left.owner_name) == normalize_playlist_name(
            right.owner_name
        )
    return True


def _same_known_owner(left: Playlist, right: Playlist) -> bool:
    if left.owner_id and right.owner_id:
        return left.owner_id == right.owner_id
    if left.owner_name and right.owner_name:
        return normalize_playlist_name(left.owner_name) == normalize_playlist_name(
            right.owner_name
        )
    return False


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else f"{noun}S"
