from __future__ import annotations

from app.core.adapter import ProviderAdapter, ProviderCredential, TrackRemoval
from app.core.capabilities import Capability
from app.core.models import Playlist, PlaylistRef
from app.core.organizer import (
    DuplicateCandidate,
    OrganizerAction,
    OrganizerIntent,
    OrganizerResolution,
    OrganizerSelection,
    ResolvedOrganizerItem,
    UnsupportedOrganizerItem,
    build_confirmation_phrase,
    detect_duplicate_candidates,
    normalize_playlist_name,
    playlist_sequence_hash,
    resolve_playlist_action,
)


async def resolve_organizer_selection(
    adapter: ProviderAdapter,
    credential: ProviderCredential,
    selection: OrganizerSelection,
) -> OrganizerResolution:
    _validate_selection(selection)
    refs = {ref.id: ref async for ref in adapter.iter_playlists(credential)}
    details: dict[str, Playlist] = {}
    items: list[ResolvedOrganizerItem] = []
    unsupported: list[UnsupportedOrganizerItem] = []

    for selected in selection.playlist_actions:
        ref = _require_ref(refs, selected.playlist_id)
        resolved_ref = ref
        if selected.intent is OrganizerIntent.DELETE and ref.is_owned is not True:
            detail = await _detail(adapter, credential, ref, details)
            resolved_ref = _ref_from_detail(detail, ref)
        action = resolve_playlist_action(selected.intent, adapter.info.capabilities, resolved_ref)
        if action is None:
            unsupported.append(
                UnsupportedOrganizerItem(
                    playlist_id=ref.id,
                    playlist_name=ref.name,
                    intent=selected.intent,
                    reason=_unsupported_reason(adapter, selected.intent, resolved_ref),
                )
            )
            continue
        items.append(
            ResolvedOrganizerItem(
                playlist=resolved_ref,
                action=action,
                destructive=action is OrganizerAction.DELETE_PLAYLIST,
                recovery=_recovery(adapter, action),
                request_payload={"playlist": resolved_ref.model_dump(mode="json")},
            )
        )

    for selected in selection.track_removals:
        ref = _require_ref(refs, selected.playlist_id)
        detail = await _detail(adapter, credential, ref, details)
        resolved_ref = _ref_from_detail(detail, ref)
        action = resolve_playlist_action(
            OrganizerIntent.REMOVE_TRACKS,
            adapter.info.capabilities,
            resolved_ref,
        )
        if action is None:
            unsupported.append(
                UnsupportedOrganizerItem(
                    playlist_id=ref.id,
                    playlist_name=ref.name,
                    intent=OrganizerIntent.REMOVE_TRACKS,
                    reason=_unsupported_reason(
                        adapter,
                        OrganizerIntent.REMOVE_TRACKS,
                        resolved_ref,
                    ),
                )
            )
            continue
        removals = _selected_tracks(adapter.info.name, detail, selected.tracks)
        limit = adapter.info.capabilities.max_remove_batch
        if len(removals) > limit:
            raise ValueError(
                f"{adapter.info.display_name} can remove at most {limit} selected songs per job"
            )
        payload = {
            "playlist": resolved_ref.model_dump(mode="json"),
            "tracks": [removal.model_dump(mode="json") for removal in removals],
        }
        if adapter.info.name == "spotify":
            if not resolved_ref.snapshot_id:
                raise ValueError("Spotify track removal requires a current playlist snapshot")
            selected_positions = {removal.position for removal in removals}
            expected_tracks = [
                track for track in detail.tracks if track.position not in selected_positions
            ]
            payload.update(
                {
                    "baseline_sequence_hash": playlist_sequence_hash(detail.tracks),
                    "expected_sequence_hash": playlist_sequence_hash(expected_tracks),
                }
            )
        items.append(
            ResolvedOrganizerItem(
                playlist=resolved_ref,
                action=OrganizerAction.REMOVE_TRACKS,
                destructive=True,
                recovery=_recovery(adapter, OrganizerAction.REMOVE_TRACKS),
                selected_track_count=len(removals),
                request_payload=payload,
            )
        )

    delete_count = sum(item.action is OrganizerAction.DELETE_PLAYLIST for item in items)
    removed_track_count = sum(item.selected_track_count for item in items)
    return OrganizerResolution(
        items=items,
        unsupported=unsupported,
        confirmation_phrase=build_confirmation_phrase(
            delete_count=delete_count,
            removed_track_count=removed_track_count,
        ),
    )


async def find_duplicate_candidates(
    adapter: ProviderAdapter,
    credential: ProviderCredential,
) -> list[DuplicateCandidate]:
    refs = [ref async for ref in adapter.iter_playlists(credential)]
    names: dict[str, list[PlaylistRef]] = {}
    for ref in refs:
        normalized = normalize_playlist_name(ref.name)
        if normalized:
            names.setdefault(normalized, []).append(ref)
    candidate_refs = [
        ref
        for group in names.values()
        if len(group) > 1
        for ref in group
    ]
    details = [
        await adapter.read_playlist(credential, ref)
        for ref in candidate_refs
    ]
    return detect_duplicate_candidates(details)


def _validate_selection(selection: OrganizerSelection) -> None:
    if not selection.playlist_actions and not selection.track_removals:
        raise ValueError("Select at least one playlist or song")
    action_ids = [selected.playlist_id for selected in selection.playlist_actions]
    track_ids = [selected.playlist_id for selected in selection.track_removals]
    if len(action_ids) != len(set(action_ids)):
        raise ValueError("A playlist can only have one playlist-level action")
    if len(track_ids) != len(set(track_ids)):
        raise ValueError("A playlist can only have one song-removal selection")
    overlap = set(action_ids) & set(track_ids)
    if overlap:
        raise ValueError(
            f"Playlist '{sorted(overlap)[0]}' cannot be removed and edited in the same job"
        )


def _require_ref(refs: dict[str, PlaylistRef], playlist_id: str) -> PlaylistRef:
    try:
        return refs[playlist_id]
    except KeyError as exc:
        raise ValueError(f"Playlist '{playlist_id}' is no longer in this account") from exc


async def _detail(
    adapter: ProviderAdapter,
    credential: ProviderCredential,
    ref: PlaylistRef,
    cache: dict[str, Playlist],
) -> Playlist:
    if ref.id not in cache:
        cache[ref.id] = await adapter.read_playlist(credential, ref)
    return cache[ref.id]


def _ref_from_detail(detail: Playlist, fallback: PlaylistRef) -> PlaylistRef:
    return PlaylistRef(
        id=detail.id or fallback.id,
        name=detail.name or fallback.name,
        track_count=len(detail.tracks),
        owner_id=detail.owner_id or fallback.owner_id,
        owner_name=detail.owner_name or fallback.owner_name,
        is_owned=detail.is_owned if detail.is_owned is not None else fallback.is_owned,
        is_followed=detail.is_followed
        if detail.is_followed is not None
        else fallback.is_followed,
        collaborative=detail.collaborative
        if detail.collaborative is not None
        else fallback.collaborative,
        snapshot_id=detail.snapshot_id or fallback.snapshot_id,
        tracks_href=fallback.tracks_href,
        created_at=detail.created_at or fallback.created_at,
        updated_at=detail.updated_at or fallback.updated_at,
        kind=detail.kind,
    )


def _selected_tracks(provider: str, playlist: Playlist, selections) -> list[TrackRemoval]:
    by_position = {
        track.position: track
        for track in playlist.tracks
        if track.position is not None
    }
    removals: list[TrackRemoval] = []
    seen_positions: set[int] = set()
    for selection in selections:
        if selection.position in seen_positions:
            raise ValueError(f"Song position {selection.position} was selected more than once")
        seen_positions.add(selection.position)
        track = by_position.get(selection.position)
        if track is None:
            raise ValueError(f"Song position {selection.position} is no longer in the playlist")
        if selection.source_item_id and selection.source_item_id != track.source_item_id:
            raise ValueError(
                f"Song position {selection.position} changed; refresh the playlist before retrying"
            )
        provider_uri = track.provider_uris.get(provider)
        if not provider_uri:
            raise ValueError(
                f"Song position {selection.position} has no {provider} item identifier"
            )
        if provider == "ytmusic" and not track.metadata.get("ytmusic_set_video_id"):
            raise ValueError(
                f"Song position {selection.position} lacks a YouTube Music setVideoId"
            )
        removals.append(
            TrackRemoval(
                source_item_id=track.source_item_id,
                provider_uri=provider_uri,
                position=selection.position,
            )
        )
    if not removals:
        raise ValueError(f"Select at least one song from '{playlist.name}'")
    return removals


def _unsupported_reason(
    adapter: ProviderAdapter,
    intent: OrganizerIntent,
    playlist: PlaylistRef,
) -> str:
    display_name = adapter.info.display_name
    if intent is OrganizerIntent.REMOVE:
        if adapter.info.capabilities.can(Capability.DELETE_PLAYLIST):
            return (
                f"{display_name} has no verified safe remove operation; permanent deletion "
                "must be selected explicitly"
            )
        return f"{display_name} cannot remove playlists through its current API"
    if intent is OrganizerIntent.DELETE:
        if not adapter.info.capabilities.can(Capability.DELETE_PLAYLIST):
            return f"{display_name} does not support permanent playlist deletion"
        return f"{display_name} playlist ownership could not be confirmed"
    if not adapter.info.capabilities.can(Capability.REMOVE_TRACKS):
        return f"{display_name} does not support exact song removal"
    if playlist.collaborative is not True:
        return f"{display_name} playlist ownership could not be confirmed"
    return f"{display_name} cannot remove the selected songs"


def _recovery(adapter: ProviderAdapter, action: OrganizerAction) -> str:
    display_name = adapter.info.display_name
    if action is OrganizerAction.UNFOLLOW_PLAYLIST:
        return (
            f"Removes the playlist from your {display_name} library without deleting its "
            "provider-side contents. You can follow it again if it remains available."
        )
    if action is OrganizerAction.DELETE_PLAYLIST:
        return (
            f"Permanently deletes the owned playlist from {display_name}. "
            "Open Playlist Engine cannot restore it."
        )
    return (
        f"Removes only the selected playlist entries from {display_name}. "
        "Re-add them manually if you need to recover them."
    )
