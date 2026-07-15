"""Deterministic playlist snapshots, diffs, and mirror capability checks."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.core.adapter import MirrorProviderAdapter
from app.core.capabilities import Capability
from app.core.migration_state import normalize_text
from app.core.models import Playlist, PlaylistKind, Track


class SyncMode(StrEnum):
    ADD_ONLY = "add_only"
    MIRROR = "mirror"


@dataclass(frozen=True)
class SyncDiff:
    added_positions: list[int]
    removed_count: int
    reordered_count: int

    @property
    def changed_count(self) -> int:
        return len(self.added_positions) + self.removed_count + self.reordered_count


def build_playlist_snapshot(playlist: Playlist, *, provider: str) -> dict[str, Any]:
    occurrences: Counter[str] = Counter()
    entries: list[dict[str, Any]] = []
    unsupported_count = 0
    for index, track in enumerate(playlist.tracks):
        if not track.is_migratable:
            unsupported_count += 1
            continue
        provider_uri = (track.provider_uris or {}).get(provider)
        identity = _track_identity(track, provider_uri=provider_uri)
        occurrence = occurrences[identity]
        occurrences[identity] += 1
        token = f"{identity}#{occurrence}"
        entries.append(
            {
                "token": token,
                "identity": identity,
                "occurrence": occurrence,
                "index": index,
                "position": track.position if track.position is not None else index,
                "provider_uri": provider_uri,
                "track": track.model_dump(mode="json"),
            }
        )
    snapshot = {
        "playlist_id": playlist.id,
        "name": playlist.name,
        "kind": playlist.kind.value,
        "provider": provider,
        "provider_snapshot_id": playlist.snapshot_id,
        "unsupported_count": unsupported_count,
        "tracks": entries,
    }
    snapshot["fingerprint"] = _snapshot_fingerprint(snapshot)
    return snapshot


def diff_snapshots(previous: dict[str, Any], current: dict[str, Any]) -> SyncDiff:
    previous_entries = _entries(previous)
    current_entries = _entries(current)
    previous_counts = Counter(entry["identity"] for entry in previous_entries)
    current_counts = Counter(entry["identity"] for entry in current_entries)

    seen: Counter[str] = Counter()
    added_positions: list[int] = []
    for entry in current_entries:
        identity = entry["identity"]
        occurrence = seen[identity]
        seen[identity] += 1
        if occurrence >= previous_counts[identity]:
            added_positions.append(int(entry["position"]))

    removed_count = sum(
        max(0, count - current_counts[identity])
        for identity, count in previous_counts.items()
    )
    shared_tokens = {
        f"{identity}#{occurrence}"
        for identity in previous_counts.keys() | current_counts.keys()
        for occurrence in range(min(previous_counts[identity], current_counts[identity]))
    }
    previous_order = [
        entry["token"] for entry in previous_entries if entry["token"] in shared_tokens
    ]
    current_order = [
        entry["token"] for entry in current_entries if entry["token"] in shared_tokens
    ]
    reordered_count = sum(
        previous_token != current_token
        for previous_token, current_token in zip(previous_order, current_order, strict=True)
    )
    return SyncDiff(
        added_positions=added_positions,
        removed_count=removed_count,
        reordered_count=reordered_count,
    )


def carry_target_mappings(
    previous: dict[str, Any],
    current: dict[str, Any],
    mappings: dict[str, str | None],
) -> dict[str, str | None]:
    previous_tokens = {entry["token"] for entry in _entries(previous)}
    carried: dict[str, str | None] = {}
    for entry in _entries(current):
        token = entry["token"]
        if token in previous_tokens and token in mappings:
            carried[token] = mappings[token]
    return carried


def target_uri_sequence(snapshot: dict[str, Any]) -> list[str]:
    return [
        str(entry["provider_uri"])
        for entry in _entries(snapshot)
        if entry.get("provider_uri")
    ]


def mirror_unavailable_reason(
    adapter: object,
    *,
    kind: PlaylistKind = PlaylistKind.STANDARD,
) -> str | None:
    info = getattr(adapter, "info", None)
    display_name = getattr(info, "display_name", "Target")
    if kind is not PlaylistKind.STANDARD:
        return "Mirror mode is only available for standard playlists"
    caps = getattr(info, "capabilities", None)
    if not caps or not (
        caps.can(Capability.REMOVE_TRACKS) and caps.can(Capability.REORDER)
    ):
        return f"{display_name} cannot remove and reorder playlist tracks"
    if not isinstance(adapter, MirrorProviderAdapter):
        return f"{display_name} does not implement ordered playlist replacement"
    return None


def next_run_after(now: datetime, *, cadence_minutes: int) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduled timestamps must be timezone-aware")
    if cadence_minutes <= 0:
        raise ValueError("cadence_minutes must be positive")
    return now + timedelta(minutes=cadence_minutes)


def snapshot_track(snapshot: dict[str, Any], token: str) -> Track:
    for entry in _entries(snapshot):
        if entry["token"] == token:
            return Track.model_validate(entry["track"])
    raise KeyError(token)


def _entries(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    tracks = snapshot.get("tracks")
    if not isinstance(tracks, list):
        return []
    return [entry for entry in tracks if isinstance(entry, dict)]


def _track_identity(track: Track, *, provider_uri: str | None) -> str:
    if track.source_item_id:
        return f"source-item:{track.source_item_id.lower()}"
    if provider_uri:
        return f"provider-uri:{provider_uri.strip().lower()}"
    if track.id:
        return f"track-id:{track.id.lower()}"
    if track.isrc:
        return f"isrc:{track.isrc.upper()}"
    title = normalize_text(track.title)
    artist = normalize_text(track.artist)
    album = normalize_text(track.album)
    duration = round(track.duration_s / 5) * 5 if track.duration_s else ""
    return f"signature:{title}|{artist}|{album}|{duration}"


def _snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    payload = {key: value for key, value in snapshot.items() if key != "fingerprint"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
