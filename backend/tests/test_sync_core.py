from datetime import UTC, datetime, timedelta

from app.core.adapter import AuthKind, ProviderInfo
from app.core.capabilities import Capability, CapabilityDescriptor
from app.core.models import MediaType, Playlist, Track
from app.core.sync import (
    build_playlist_snapshot,
    carry_target_mappings,
    diff_snapshots,
    mirror_unavailable_reason,
    next_run_after,
    target_uri_sequence,
)


def _track(
    track_id: str,
    *,
    position: int,
    uri: str | None = None,
    source_item_id: str | None = None,
) -> Track:
    return Track(
        id=track_id,
        title=f"Song {track_id}",
        artist="Artist",
        position=position,
        source_item_id=source_item_id,
        provider_uris={"source": uri or f"source:track:{track_id}"},
    )


def _snapshot(*tracks: Track) -> dict:
    return build_playlist_snapshot(
        Playlist(id="playlist", name="Playlist", tracks=list(tracks)),
        provider="source",
    )


def test_snapshot_diff_is_occurrence_aware_for_duplicate_additions() -> None:
    previous = _snapshot(
        _track("a", position=0),
        _track("a", position=1),
        _track("b", position=2),
    )
    current = _snapshot(
        _track("a", position=0),
        _track("a", position=1),
        _track("a", position=2),
        _track("b", position=3),
    )

    diff = diff_snapshots(previous, current)

    assert diff.added_positions == [2]
    assert diff.removed_count == 0
    assert diff.reordered_count == 0


def test_snapshot_diff_detects_reorder_without_false_additions() -> None:
    previous = _snapshot(
        _track("a", position=0),
        _track("b", position=1),
        _track("c", position=2),
    )
    current = _snapshot(
        _track("c", position=0),
        _track("a", position=1),
        _track("b", position=2),
    )

    diff = diff_snapshots(previous, current)

    assert diff.added_positions == []
    assert diff.removed_count == 0
    assert diff.reordered_count == 3


def test_target_mappings_follow_duplicate_occurrences_across_reorder() -> None:
    previous = _snapshot(
        _track("a", position=0),
        _track("a", position=1),
        _track("b", position=2),
    )
    previous_tokens = [entry["token"] for entry in previous["tracks"]]
    current = _snapshot(
        _track("b", position=0),
        _track("a", position=1),
        _track("a", position=2),
    )

    carried = carry_target_mappings(
        previous,
        current,
        {
            previous_tokens[0]: "target:track:a",
            previous_tokens[1]: "target:track:a",
            previous_tokens[2]: "target:track:b",
        },
    )

    assert list(carried.values()) == [
        "target:track:b",
        "target:track:a",
        "target:track:a",
    ]


def test_snapshot_filters_unsupported_items_and_preserves_ordered_target_uris() -> None:
    unsupported = _track("local", position=1)
    unsupported.media_type = MediaType.LOCAL_FILE
    unsupported.is_local = True
    playlist = Playlist(
        id="playlist",
        name="Playlist",
        tracks=[
            _track("a", position=0, uri="target:track:a"),
            unsupported,
            _track("a", position=2, uri="target:track:a"),
        ],
    )

    snapshot = build_playlist_snapshot(playlist, provider="source")

    assert snapshot["unsupported_count"] == 1
    assert target_uri_sequence(snapshot) == ["target:track:a", "target:track:a"]


class _MirrorAdapter:
    info = ProviderInfo(
        name="target",
        display_name="Target",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
        capabilities=CapabilityDescriptor(
            capabilities={Capability.REMOVE_TRACKS, Capability.REORDER}
        ),
    )

    async def replace_playlist_tracks(self, cred, playlist_id, uris) -> None:
        return None


class _CapabilitiesOnlyAdapter:
    info = _MirrorAdapter.info


def test_mirror_requires_capabilities_and_adapter_implementation() -> None:
    assert mirror_unavailable_reason(_MirrorAdapter()) is None
    assert (
        mirror_unavailable_reason(_CapabilitiesOnlyAdapter())
        == "Target does not implement ordered playlist replacement"
    )

    _CapabilitiesOnlyAdapter.info = _CapabilitiesOnlyAdapter.info.model_copy(
        update={
            "capabilities": CapabilityDescriptor(
                capabilities={Capability.REMOVE_TRACKS}
            )
        }
    )
    assert (
        mirror_unavailable_reason(_CapabilitiesOnlyAdapter())
        == "Target cannot remove and reorder playlist tracks"
    )


def test_next_run_uses_a_timezone_aware_cadence() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

    assert next_run_after(now, cadence_minutes=90) == now + timedelta(minutes=90)
