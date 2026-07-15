import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import organizer
from app.api.dependencies import get_current_user_id
from app.core.adapter import (
    AuthKind,
    PlaylistMutationResult,
    ProviderCredential,
    ProviderInfo,
    RateLimited,
    RemoveItemResult,
    RemoveTracksResult,
)
from app.core.capabilities import Capability, CapabilityDescriptor
from app.core.models import Playlist, PlaylistRef, Track
from app.core.organizer import (
    OrganizerAction,
    OrganizerIntent,
    OrganizerSelection,
    PlaylistActionSelection,
    PlaylistTrackSelection,
    ResolvedOrganizerItem,
    TrackSelection,
    UnsupportedOrganizerItem,
    build_confirmation_phrase,
    detect_duplicate_candidates,
    playlist_sequence_hash,
    resolve_playlist_action,
)
from app.core.organizer_service import resolve_organizer_selection
from app.db import models as orm
from app.db.base import Base
from app.jobs.organizer import (
    ItemExecutionError,
    _execute_item,
    _items_for_execution_stmt,
    _run_item,
    _track_outcome,
)
from app.jobs.worker import WorkerSettings


def test_safe_remove_never_falls_back_to_destructive_delete() -> None:
    capabilities = CapabilityDescriptor(
        capabilities={Capability.DELETE_PLAYLIST},
    )
    playlist = PlaylistRef(id="owned", name="Owned", is_owned=True)

    assert resolve_playlist_action(OrganizerIntent.REMOVE, capabilities, playlist) is None
    assert (
        resolve_playlist_action(OrganizerIntent.DELETE, capabilities, playlist)
        is OrganizerAction.DELETE_PLAYLIST
    )


def test_track_removal_requires_owned_or_collaborative_playlist() -> None:
    capabilities = CapabilityDescriptor(
        capabilities={Capability.REMOVE_TRACKS},
    )

    assert (
        resolve_playlist_action(
            OrganizerIntent.REMOVE_TRACKS,
            capabilities,
            PlaylistRef(id="owned", name="Owned", is_owned=True),
        )
        is OrganizerAction.REMOVE_TRACKS
    )
    assert (
        resolve_playlist_action(
            OrganizerIntent.REMOVE_TRACKS,
            capabilities,
            PlaylistRef(
                id="collaborative",
                name="Collaborative",
                is_owned=False,
                collaborative=True,
            ),
        )
        is OrganizerAction.REMOVE_TRACKS
    )
    assert (
        resolve_playlist_action(
            OrganizerIntent.REMOVE_TRACKS,
            capabilities,
            PlaylistRef(id="followed", name="Followed", is_owned=False),
        )
        is None
    )


def test_confirmation_phrase_counts_irreversible_changes() -> None:
    assert (
        build_confirmation_phrase(delete_count=2, removed_track_count=0)
        == "DELETE 2 PLAYLISTS"
    )
    assert (
        build_confirmation_phrase(delete_count=0, removed_track_count=1)
        == "REMOVE 1 SONG"
    )
    assert (
        build_confirmation_phrase(delete_count=1, removed_track_count=3)
        == "DELETE 1 PLAYLIST AND REMOVE 3 SONGS"
    )
    assert build_confirmation_phrase(delete_count=0, removed_track_count=0) is None


def test_playlist_sequence_hash_changes_only_for_exact_sequence() -> None:
    tracks = [
        Track(
            id="a",
            title="A",
            artist="Artist",
            provider_uris={"spotify": "spotify:track:a"},
            position=0,
        ),
        Track(
            id="b",
            title="B",
            artist="Artist",
            provider_uris={"spotify": "spotify:track:b"},
            position=1,
        ),
        Track(
            id="a",
            title="A",
            artist="Artist",
            provider_uris={"spotify": "spotify:track:a"},
            position=2,
        ),
    ]

    baseline = playlist_sequence_hash(tracks)
    expected = playlist_sequence_hash([tracks[0], tracks[2]])

    assert baseline != expected
    assert expected == playlist_sequence_hash([tracks[0], tracks[2]])
    assert playlist_sequence_hash([tracks[0], tracks[1]]) != playlist_sequence_hash(
        [tracks[1], tracks[0]]
    )


def test_duplicate_candidates_require_name_owner_and_track_overlap() -> None:
    candidates = detect_duplicate_candidates(
        [
            Playlist(
                id="first",
                name=" Road-trip! ",
                owner_id="owner",
                tracks=[
                    Track(id="a", title="A", artist="Artist"),
                    Track(id="b", title="B", artist="Artist"),
                ],
            ),
            Playlist(
                id="second",
                name="road trip",
                owner_id="owner",
                tracks=[
                    Track(id="a", title="A", artist="Artist"),
                    Track(id="b", title="B", artist="Artist"),
                    Track(id="c", title="C", artist="Artist"),
                ],
            ),
            Playlist(
                id="different-owner",
                name="Road Trip",
                owner_id="someone-else",
                tracks=[
                    Track(id="a", title="A", artist="Artist"),
                    Track(id="b", title="B", artist="Artist"),
                ],
            ),
            Playlist(
                id="different-tracks",
                name="Road Trip",
                owner_id="owner",
                tracks=[
                    Track(id="x", title="X", artist="Artist"),
                    Track(id="y", title="Y", artist="Artist"),
                ],
            ),
        ]
    )

    assert len(candidates) == 1
    assert candidates[0].playlist_ids == ("first", "second")
    assert candidates[0].overlap_count == 2
    assert candidates[0].overlap_ratio == 1.0
    assert "normalized name" in candidates[0].reasons
    assert "same owner" in candidates[0].reasons


def test_organizer_items_are_unique_per_job_playlist_action() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            orm.OrganizerJob(
                id="job",
                user_id="local",
                provider="spotify",
                account_id="account",
                request_payload={},
            )
        )
        session.add(
            orm.OrganizerItem(
                job_id="job",
                playlist_id="playlist",
                playlist_name="Roadtrip",
                action=OrganizerAction.UNFOLLOW_PLAYLIST,
                request_payload={},
            )
        )
        session.commit()

        session.add(
            orm.OrganizerItem(
                job_id="job",
                playlist_id="playlist",
                playlist_name="Roadtrip",
                action=OrganizerAction.UNFOLLOW_PLAYLIST,
                request_payload={},
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


class ResolverAdapter:
    def __init__(
        self,
        *,
        name: str,
        capabilities: set[Capability],
        refs: list[PlaylistRef],
        details: dict[str, Playlist],
        max_remove_batch: int = 100,
    ) -> None:
        self.info = ProviderInfo(
            name=name,
            display_name=name.title(),
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
            capabilities=CapabilityDescriptor(
                capabilities=capabilities,
                max_remove_batch=max_remove_batch,
            ),
        )
        self.refs = refs
        self.details = details

    async def iter_playlists(self, credential):
        for ref in self.refs:
            yield ref

    async def read_playlist(self, credential, ref):
        return self.details[ref.id]


def _credential(provider: str) -> ProviderCredential:
    return ProviderCredential(
        account_id="account",
        provider=provider,
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
    )


async def test_resolver_verifies_unknown_ownership_before_delete() -> None:
    adapter = ResolverAdapter(
        name="ytmusic",
        capabilities={Capability.DELETE_PLAYLIST},
        refs=[PlaylistRef(id="playlist", name="Owned", is_owned=None)],
        details={
            "playlist": Playlist(
                id="playlist",
                name="Owned",
                is_owned=True,
            )
        },
    )

    resolution = await resolve_organizer_selection(
        adapter,
        _credential("ytmusic"),
        OrganizerSelection(
            playlist_actions=[
                PlaylistActionSelection(
                    playlist_id="playlist",
                    intent=OrganizerIntent.DELETE,
                )
            ]
        ),
    )

    assert resolution.unsupported == []
    assert resolution.items[0].action is OrganizerAction.DELETE_PLAYLIST
    assert resolution.items[0].playlist.is_owned is True
    assert resolution.confirmation_phrase == "DELETE 1 PLAYLIST"


async def test_resolver_reports_safe_remove_as_unsupported_without_fallback() -> None:
    adapter = ResolverAdapter(
        name="ytmusic",
        capabilities={Capability.DELETE_PLAYLIST},
        refs=[PlaylistRef(id="playlist", name="Owned", is_owned=True)],
        details={},
    )

    resolution = await resolve_organizer_selection(
        adapter,
        _credential("ytmusic"),
        OrganizerSelection(
            playlist_actions=[PlaylistActionSelection(playlist_id="playlist")]
        ),
    )

    assert resolution.items == []
    assert resolution.confirmation_phrase is None
    assert len(resolution.unsupported) == 1
    assert "permanent deletion must be selected explicitly" in resolution.unsupported[0].reason


async def test_resolver_builds_spotify_snapshot_reconciliation_payload() -> None:
    tracks = [
        Track(
            id="a",
            title="A",
            artist="Artist",
            provider_uris={"spotify": "spotify:track:a"},
            position=0,
            source_item_id="a",
        ),
        Track(
            id="b",
            title="B",
            artist="Artist",
            provider_uris={"spotify": "spotify:track:b"},
            position=1,
            source_item_id="b",
        ),
    ]
    adapter = ResolverAdapter(
        name="spotify",
        capabilities={Capability.REMOVE_TRACKS},
        refs=[
            PlaylistRef(
                id="playlist",
                name="Owned",
                is_owned=True,
                snapshot_id="snapshot",
            )
        ],
        details={
            "playlist": Playlist(
                id="playlist",
                name="Owned",
                is_owned=True,
                snapshot_id="snapshot",
                tracks=tracks,
            )
        },
    )

    resolution = await resolve_organizer_selection(
        adapter,
        _credential("spotify"),
        OrganizerSelection(
            track_removals=[
                PlaylistTrackSelection(
                    playlist_id="playlist",
                    tracks=[TrackSelection(position=1, source_item_id="b")],
                )
            ]
        ),
    )

    payload = resolution.items[0].request_payload
    assert payload["baseline_sequence_hash"] == playlist_sequence_hash(tracks)
    assert payload["expected_sequence_hash"] == playlist_sequence_hash([tracks[0]])
    assert payload["tracks"][0]["position"] == 1
    assert resolution.confirmation_phrase == "REMOVE 1 SONG"


async def test_resolver_rejects_stale_track_identity() -> None:
    adapter = ResolverAdapter(
        name="spotify",
        capabilities={Capability.REMOVE_TRACKS},
        refs=[
            PlaylistRef(
                id="playlist",
                name="Owned",
                is_owned=True,
                snapshot_id="snapshot",
            )
        ],
        details={
            "playlist": Playlist(
                id="playlist",
                name="Owned",
                is_owned=True,
                snapshot_id="snapshot",
                tracks=[
                    Track(
                        id="current",
                        title="Current",
                        artist="Artist",
                        provider_uris={"spotify": "spotify:track:current"},
                        position=0,
                        source_item_id="current",
                    )
                ],
            )
        },
    )

    with pytest.raises(ValueError, match="changed"):
        await resolve_organizer_selection(
            adapter,
            _credential("spotify"),
            OrganizerSelection(
                track_removals=[
                    PlaylistTrackSelection(
                        playlist_id="playlist",
                        tracks=[TrackSelection(position=0, source_item_id="old")],
                    )
                ]
            ),
        )


class ExecutionAdapter:
    def __init__(
        self,
        *,
        name: str,
        refs: list[PlaylistRef] | None = None,
        details: dict[str, Playlist] | None = None,
    ) -> None:
        self.info = ProviderInfo(
            name=name,
            display_name=name.title(),
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
            capabilities=CapabilityDescriptor(
                capabilities={
                    Capability.UNFOLLOW_PLAYLIST,
                    Capability.DELETE_PLAYLIST,
                    Capability.REMOVE_TRACKS,
                }
            ),
        )
        self.refs = refs or []
        self.details = details or {}
        self.unfollow_calls = 0
        self.delete_calls = 0
        self.remove_calls: list[list] = []
        self.rate_limit_once = False

    async def iter_playlists(self, credential):
        for ref in self.refs:
            yield ref

    async def read_playlist(self, credential, ref):
        return self.details[ref.id]

    async def unfollow_playlist(self, credential, ref):
        self.unfollow_calls += 1
        if self.rate_limit_once:
            self.rate_limit_once = False
            raise RateLimited(retry_after_s=0.001)
        return PlaylistMutationResult()

    async def delete_playlist(self, credential, ref):
        self.delete_calls += 1
        return PlaylistMutationResult()

    async def remove_tracks(self, credential, ref, items):
        selected = list(items)
        self.remove_calls.append(selected)
        return RemoveTracksResult(
            items=[
                RemoveItemResult(
                    source_item_id=item.source_item_id,
                    provider_uri=item.provider_uri,
                    position=item.position,
                    ok=True,
                )
                for item in selected
            ]
        )


async def test_spotify_retry_reconciles_exact_expected_sequence_without_repeating() -> None:
    current_tracks = [
        Track(
            id="a",
            title="A",
            artist="Artist",
            provider_uris={"spotify": "spotify:track:a"},
            position=0,
        )
    ]
    adapter = ExecutionAdapter(
        name="spotify",
        details={
            "playlist": Playlist(
                id="playlist",
                name="Owned",
                is_owned=True,
                snapshot_id="next",
                tracks=current_tracks,
            )
        },
    )
    item = orm.OrganizerItem(
        action=OrganizerAction.REMOVE_TRACKS,
        playlist_id="playlist",
        playlist_name="Owned",
        request_payload={
            "playlist": PlaylistRef(
                id="playlist",
                name="Owned",
                is_owned=True,
                snapshot_id="original",
            ).model_dump(mode="json"),
            "tracks": [
                {
                    "source_item_id": "b",
                    "provider_uri": "spotify:track:b",
                    "position": 1,
                }
            ],
            "baseline_sequence_hash": "different",
            "expected_sequence_hash": playlist_sequence_hash(current_tracks),
        },
    )

    outcome = await _execute_item(adapter, _credential("spotify"), item)

    assert outcome.payload == {"already_applied": True}
    assert adapter.remove_calls == []


async def test_ytmusic_retry_removes_only_occurrences_still_present() -> None:
    adapter = ExecutionAdapter(
        name="ytmusic",
        details={
            "playlist": Playlist(
                id="playlist",
                name="Owned",
                is_owned=True,
                tracks=[
                    Track(
                        id="video",
                        title="Song",
                        artist="Artist",
                        provider_uris={"ytmusic": "ytmusic:video:video"},
                        source_item_id="set-remaining",
                        position=0,
                    )
                ],
            )
        },
    )
    item = orm.OrganizerItem(
        action=OrganizerAction.REMOVE_TRACKS,
        playlist_id="playlist",
        playlist_name="Owned",
        request_payload={
            "playlist": PlaylistRef(
                id="playlist",
                name="Owned",
                is_owned=True,
            ).model_dump(mode="json"),
            "tracks": [
                {
                    "source_item_id": "set-removed",
                    "provider_uri": "ytmusic:video:video",
                    "position": 0,
                },
                {
                    "source_item_id": "set-remaining",
                    "provider_uri": "ytmusic:video:video",
                    "position": 1,
                },
            ],
        },
    )

    outcome = await _execute_item(adapter, _credential("ytmusic"), item)

    assert outcome.payload["items"][0]["source_item_id"] == "set-remaining"
    assert len(adapter.remove_calls) == 1
    assert [item.source_item_id for item in adapter.remove_calls[0]] == ["set-remaining"]


def test_execution_query_never_repeats_successful_items() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            orm.OrganizerJob(
                id="job",
                user_id="local",
                provider="spotify",
                account_id="account",
                request_payload={},
            )
        )
        session.add_all(
            [
                orm.OrganizerItem(
                    id="succeeded",
                    job_id="job",
                    playlist_id="one",
                    playlist_name="One",
                    action=OrganizerAction.UNFOLLOW_PLAYLIST,
                    request_payload={},
                    status="succeeded",
                ),
                orm.OrganizerItem(
                    id="failed",
                    job_id="job",
                    playlist_id="two",
                    playlist_name="Two",
                    action=OrganizerAction.UNFOLLOW_PLAYLIST,
                    request_payload={},
                    status="failed",
                ),
                orm.OrganizerItem(
                    id="pending",
                    job_id="job",
                    playlist_id="three",
                    playlist_name="Three",
                    action=OrganizerAction.UNFOLLOW_PLAYLIST,
                    request_payload={},
                    status="pending",
                ),
            ]
        )
        session.commit()

        item_ids = [
            item.id for item in session.scalars(_items_for_execution_stmt("job"))
        ]

    assert item_ids == ["pending"]


def test_partial_track_failure_preserves_per_song_results() -> None:
    result = RemoveTracksResult(
        items=[
            RemoveItemResult(
                source_item_id="ok",
                provider_uri="spotify:track:ok",
                position=0,
                ok=True,
            ),
            RemoveItemResult(
                source_item_id="failed",
                provider_uri="spotify:track:failed",
                position=1,
                ok=False,
                error="provider rejected item",
            ),
        ]
    )

    with pytest.raises(ItemExecutionError) as exc_info:
        _track_outcome(result)

    assert exc_info.value.payload["items"][0]["ok"] is True
    assert exc_info.value.payload["items"][1]["error"] == "provider rejected item"


class WorkerSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        return None

    async def scalar(self, statement):
        return 1


async def test_rate_limited_item_retries_then_succeeds() -> None:
    adapter = ExecutionAdapter(name="spotify")
    adapter.rate_limit_once = True
    item = orm.OrganizerItem(
        action=OrganizerAction.UNFOLLOW_PLAYLIST,
        playlist_id="playlist",
        playlist_name="Roadtrip",
        request_payload={
            "playlist": PlaylistRef(
                id="playlist",
                name="Roadtrip",
            ).model_dump(mode="json")
        },
    )
    job = orm.OrganizerJob(
        id="job",
        user_id="local",
        provider="spotify",
        account_id="account",
        request_payload={},
    )

    succeeded = await _run_item(
        WorkerSession(),
        job,
        item,
        adapter=adapter,
        credential=_credential("spotify"),
        limiter_key="unconfigured-test-key",
    )

    assert succeeded is True
    assert item.status == "succeeded"
    assert item.attempts == 2
    assert adapter.unfollow_calls == 2


def test_worker_registers_organizer_job() -> None:
    assert any(function.__name__ == "run_organizer" for function in WorkerSettings.functions)


def test_all_organizer_routes_require_current_user() -> None:
    routes = [
        route
        for route in organizer.router.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/organizer")
    ]

    assert routes
    for route in routes:
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        assert get_current_user_id in dependencies, f"{route.methods} {route.path}"


def test_playlist_view_exposes_unknown_ownership_as_preflight_check() -> None:
    adapter = ResolverAdapter(
        name="ytmusic",
        capabilities={Capability.DELETE_PLAYLIST, Capability.REMOVE_TRACKS},
        refs=[],
        details={},
    )

    view = organizer._playlist_view(
        adapter,
        PlaylistRef(id="playlist", name="Maybe owned", is_owned=None),
    )

    assert view.ownership == "unknown"
    assert view.requires_ownership_check is True
    assert set(view.available_intents) == {
        OrganizerIntent.DELETE,
        OrganizerIntent.REMOVE_TRACKS,
    }


def test_irreversible_job_requires_exact_confirmation() -> None:
    resolution = organizer.OrganizerResolution(
        items=[
            ResolvedOrganizerItem(
                playlist=PlaylistRef(id="playlist", name="Owned", is_owned=True),
                action=OrganizerAction.DELETE_PLAYLIST,
                destructive=True,
                recovery="Cannot restore",
                request_payload={},
            )
        ],
        confirmation_phrase="DELETE 1 PLAYLIST",
    )

    with pytest.raises(HTTPException) as exc_info:
        organizer._validate_job_request(resolution, "delete 1 playlist")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "organizer_confirmation_required"
    organizer._validate_job_request(resolution, "DELETE 1 PLAYLIST")


def test_unsupported_items_block_job_creation() -> None:
    resolution = organizer.OrganizerResolution(
        unsupported=[
            UnsupportedOrganizerItem(
                playlist_id="playlist",
                playlist_name="Owned",
                intent=OrganizerIntent.REMOVE,
                reason="No safe remove",
            )
        ]
    )

    with pytest.raises(HTTPException) as exc_info:
        organizer._validate_job_request(resolution, None)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["unsupported"][0]["reason"] == "No safe remove"


def test_owned_organizer_job_statement_scopes_by_user() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            orm.OrganizerJob(
                id="job",
                user_id="alice",
                provider="spotify",
                account_id="account",
                request_payload={},
            )
        )
        session.commit()

        assert session.scalar(organizer._owned_job_stmt("job", "alice")) is not None
        assert session.scalar(organizer._owned_job_stmt("job", "bob")) is None
