"""Provider-neutral playlist projection for connected accounts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from app.core.migration_state import normalize_text, track_keys
from app.core.models import Playlist, PlaylistKind, Track


@dataclass(frozen=True)
class ProviderPlaylist:
    provider: str
    account_id: str
    account_label: str
    playlist: Playlist

    @property
    def key(self) -> str:
        return _member_key(self.provider, self.account_id, self.playlist.id or "")


@dataclass(frozen=True)
class SyncLink:
    source_provider: str
    source_account_id: str
    source_playlist_id: str
    target_provider: str
    target_account_id: str
    target_playlist_id: str
    rule_id: str
    enabled: bool
    status: str

    @property
    def source_key(self) -> str:
        return _member_key(
            self.source_provider,
            self.source_account_id,
            self.source_playlist_id,
        )

    @property
    def target_key(self) -> str:
        return _member_key(
            self.target_provider,
            self.target_account_id,
            self.target_playlist_id,
        )


class UnifiedPlaylistMember(BaseModel):
    key: str
    provider: str
    account_id: str
    account_label: str
    playlist_id: str
    playlist_name: str
    track_count: int
    kind: PlaylistKind


class UnifiedTrackSource(BaseModel):
    provider: str
    account_id: str
    account_label: str
    playlist_id: str
    position: int | None = None
    uri: str | None = None


class UnifiedTrack(BaseModel):
    key: str
    title: str
    artist: str
    album: str | None = None
    duration_s: int | None = None
    isrc: str | None = None
    artwork_uri: str | None = None
    sources: list[UnifiedTrackSource] = Field(default_factory=list)


class UnifiedSyncLink(BaseModel):
    rule_id: str
    source_member_key: str
    target_member_key: str
    enabled: bool
    status: str


class UnifiedSyncAttempt(BaseModel):
    migration_job_id: str
    source_member_key: str
    target_provider: str
    target_account_id: str
    status: Literal["pending", "active", "failed"]
    sync_rule_id: str | None = None
    error: str | None = None


class UnifiedPlaylist(BaseModel):
    id: str
    name: str
    description: str | None = None
    photo: str | None = None
    kind: PlaylistKind
    alignment: Literal["single_provider", "aligned", "drifted"]
    canonical_member_key: str
    members: list[UnifiedPlaylistMember]
    tracks: list[UnifiedTrack]
    sync_rule_ids: list[str] = Field(default_factory=list)
    sync_links: list[UnifiedSyncLink] = Field(default_factory=list)
    sync_attempts: list[UnifiedSyncAttempt] = Field(default_factory=list)


def build_unified_playlists(
    playlists: list[ProviderPlaylist],
    *,
    sync_links: list[SyncLink] | None = None,
) -> list[UnifiedPlaylist]:
    """Group provider copies and merge their track availability."""

    by_key = {item.key: item for item in playlists if item.playlist.id}
    parents = {key: key for key in by_key}
    source_weights: dict[str, int] = {}
    component_rule_ids: dict[str, set[str]] = {}

    def find(key: str) -> str:
        parent = parents[key]
        if parent != key:
            parents[key] = find(parent)
        return parents[key]

    def union(left: str, right: str, *, allow_same_account: bool = False) -> str:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return left_root
        if not allow_same_account:
            left_accounts = {
                (by_key[key].provider, by_key[key].account_id)
                for key in by_key
                if find(key) == left_root
            }
            right_accounts = {
                (by_key[key].provider, by_key[key].account_id)
                for key in by_key
                if find(key) == right_root
            }
            if left_accounts & right_accounts:
                return left_root
        if left_root > right_root:
            left_root, right_root = right_root, left_root
        parents[right_root] = left_root
        component_rule_ids.setdefault(left_root, set()).update(
            component_rule_ids.pop(right_root, set())
        )
        return left_root

    for link in sync_links or []:
        if link.source_key not in by_key or link.target_key not in by_key:
            continue
        root = union(link.source_key, link.target_key, allow_same_account=True)
        component_rule_ids.setdefault(root, set()).add(link.rule_id)
        source_weights[link.source_key] = source_weights.get(link.source_key, 0) + 1

    candidates = sorted(by_key.values(), key=lambda item: item.key)
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if left.playlist.kind != right.playlist.kind:
                continue
            if normalize_text(left.playlist.name) != normalize_text(right.playlist.name):
                continue
            if (left.provider, left.account_id) == (right.provider, right.account_id):
                continue
            if not _playlists_overlap(left.playlist, right.playlist):
                continue
            union(left.key, right.key)

    components: dict[str, list[ProviderPlaylist]] = {}
    for key, item in by_key.items():
        components.setdefault(find(key), []).append(item)

    result = []
    for root, members in components.items():
        member_keys = {member.key for member in members}
        canonical = min(
            members,
            key=lambda member: (
                -source_weights.get(member.key, 0),
                -len(member.playlist.tracks),
                member.key,
            ),
        )
        ordered_members = [
            canonical,
            *sorted(
                (member for member in members if member.key != canonical.key),
                key=lambda member: member.key,
            ),
        ]
        rule_ids = sorted(
            {
                link.rule_id
                for link in sync_links or []
                if link.source_key in member_keys and link.target_key in member_keys
            }
            | component_rule_ids.get(root, set())
        )
        matching_links = sorted(
            (
                link
                for link in sync_links or []
                if link.source_key in member_keys and link.target_key in member_keys
            ),
            key=lambda link: link.rule_id,
        )
        result.append(
            UnifiedPlaylist(
                id=_unified_id(canonical.key),
                name=canonical.playlist.name,
                description=canonical.playlist.description,
                photo=canonical.playlist.photo,
                kind=canonical.playlist.kind,
                alignment=_alignment(members),
                canonical_member_key=canonical.key,
                members=[
                    UnifiedPlaylistMember(
                        key=member.key,
                        provider=member.provider,
                        account_id=member.account_id,
                        account_label=member.account_label,
                        playlist_id=member.playlist.id or "",
                        playlist_name=member.playlist.name,
                        track_count=len(member.playlist.tracks),
                        kind=member.playlist.kind,
                    )
                    for member in ordered_members
                ],
                tracks=_merge_tracks(ordered_members),
                sync_rule_ids=rule_ids,
                sync_links=[
                    UnifiedSyncLink(
                        rule_id=link.rule_id,
                        source_member_key=link.source_key,
                        target_member_key=link.target_key,
                        enabled=link.enabled,
                        status=link.status,
                    )
                    for link in matching_links
                ],
            )
        )

    return sorted(result, key=lambda item: (normalize_text(item.name), item.id))


def _playlists_overlap(left: Playlist, right: Playlist) -> bool:
    if not left.tracks or not right.tracks:
        return False
    return any(
        _tracks_match(left_track, right_track)
        for left_track in left.tracks
        for right_track in right.tracks
    )


def _portable_track_keys(track: Track) -> set[str]:
    return {key for key in track_keys(track) if key.startswith(("isrc:", "sig:", "song:"))}


def _alignment(members: list[ProviderPlaylist]) -> Literal["single_provider", "aligned", "drifted"]:
    if len(members) == 1:
        return "single_provider"
    reference = sorted(
        members[0].playlist.tracks,
        key=lambda track: track.position if track.position is not None else 10**9,
    )
    for member in members[1:]:
        candidate = sorted(
            member.playlist.tracks,
            key=lambda track: track.position if track.position is not None else 10**9,
        )
        if len(candidate) != len(reference):
            return "drifted"
        if any(
            not _tracks_match(left, right) for left, right in zip(reference, candidate, strict=True)
        ):
            return "drifted"
    return "aligned"


def _merge_tracks(members: list[ProviderPlaylist]) -> list[UnifiedTrack]:
    clusters: list[tuple[set[str], set[str], Track, list[UnifiedTrackSource]]] = []
    for member in members:
        matched_clusters: set[int] = set()
        for track in sorted(
            member.playlist.tracks,
            key=lambda item: item.position if item.position is not None else 10**9,
        ):
            keys = _portable_track_keys(track) or {_track_identity(track)}
            matching = next(
                (
                    index
                    for index, (cluster_keys, cluster_isrcs, _, _) in enumerate(clusters)
                    if index not in matched_clusters
                    and _cluster_matches(cluster_keys, cluster_isrcs, track)
                ),
                None,
            )
            source = UnifiedTrackSource(
                provider=member.provider,
                account_id=member.account_id,
                account_label=member.account_label,
                playlist_id=member.playlist.id or "",
                position=track.position,
                uri=track.provider_uris.get(member.provider)
                or next(iter(track.provider_uris.values()), None),
            )
            if matching is None:
                clusters.append((set(keys), _track_isrcs(track), track, [source]))
                matched_clusters.add(len(clusters) - 1)
                continue
            cluster_keys, cluster_isrcs, representative, sources = clusters[matching]
            cluster_keys.update(keys)
            cluster_isrcs.update(_track_isrcs(track))
            sources.append(source)
            clusters[matching] = (cluster_keys, cluster_isrcs, representative, sources)
            matched_clusters.add(matching)

    occurrence_counts: dict[str, int] = {}
    result = []
    for keys, _, representative, sources in clusters:
        base_key = _preferred_key(keys, representative)
        occurrence_counts[base_key] = occurrence_counts.get(base_key, 0) + 1
        occurrence = occurrence_counts[base_key]
        key = base_key if occurrence == 1 else f"{base_key}:occurrence:{occurrence}"
        result.append(
            UnifiedTrack(
                key=key,
                title=representative.title,
                artist=representative.artist,
                album=representative.album,
                duration_s=representative.duration_s,
                isrc=representative.isrc,
                artwork_uri=representative.artwork_uri,
                sources=sorted(
                    sources,
                    key=lambda source: (
                        source.position if source.position is not None else 10**9,
                        source.provider,
                        source.account_id,
                    ),
                ),
            )
        )
    return result


def _tracks_match(left: Track, right: Track) -> bool:
    left_isrcs = _track_isrcs(left)
    right_isrcs = _track_isrcs(right)
    if left_isrcs and right_isrcs:
        return bool(left_isrcs & right_isrcs)
    return bool(_portable_track_keys(left) & _portable_track_keys(right))


def _cluster_matches(cluster_keys: set[str], cluster_isrcs: set[str], track: Track) -> bool:
    track_isrcs = _track_isrcs(track)
    if cluster_isrcs and track_isrcs:
        return bool(cluster_isrcs & track_isrcs)
    return bool(cluster_keys & _portable_track_keys(track))


def _track_isrcs(track: Track) -> set[str]:
    return {track.isrc.upper()} if track.isrc else set()


def _preferred_key(keys: set[str], track: Track) -> str:
    for prefix in ("isrc:", "sig:", "song:"):
        matches = sorted(key for key in keys if key.startswith(prefix))
        if matches:
            return matches[0]
    return _track_identity(track)


def _track_identity(track: Track) -> str:
    portable = _portable_track_keys(track)
    if portable:
        return _preferred_key(portable, track)
    raw = "|".join(
        (
            normalize_text(track.title),
            normalize_text(track.artist),
            normalize_text(track.album),
        )
    )
    return f"fallback:{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def _member_key(provider: str, account_id: str, playlist_id: str) -> str:
    return f"{provider}:{account_id}:{playlist_id}"


def _unified_id(canonical_member_key: str) -> str:
    digest = hashlib.sha256(canonical_member_key.encode()).hexdigest()[:20]
    return f"unified:{digest}"
