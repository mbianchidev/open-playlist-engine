from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.core.adapter import (
    AddItemResult,
    AuthChallenge,
    AuthKind,
    ChallengeShape,
    CreatePlaylistSpec,
    ProviderCredential,
    ProviderInfo,
    TrackCandidate,
)
from app.core.capabilities import Capability, CapabilityDescriptor
from app.core.generator import (
    CopilotSDKModel,
    ExplicitPreference,
    GeneratedPlaylistPlan,
    GeneratedTrackIntent,
    GenerationSpec,
    GeneratorControls,
    GeneratorInvalidOutput,
    GeneratorNotConfigured,
    GeneratorTimedOut,
    GeneratorUnavailable,
    OpenAICompatibleModel,
    PreferenceSummary,
    ensure_confirmable,
    parse_model_output,
    preference_summary_from_tracks,
    resolve_generated_plan,
)
from app.core.models import Playlist, PlaylistRef, Track
from app.settings import GeneratorBackend, Settings


def _settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "generator_backend": GeneratorBackend.OPENAI_COMPATIBLE,
        "generator_openai_base_url": "http://model.test/v1",
        "generator_openai_api_key": "local-secret",
        "generator_model": "local-model",
        "generator_timeout_s": 2,
        "generator_max_prompt_chars": 2_000,
        "generator_max_output_chars": 8_000,
        "generator_max_tracks": 25,
    }
    values.update(updates)
    return Settings(**values)


def _spec(prompt: str = "A focused evening playlist") -> GenerationSpec:
    return GenerationSpec(
        prompt=prompt,
        controls=GeneratorControls(
            genres=["ambient", "jazz"],
            moods=["focused"],
            eras=["1990s"],
            energy=2,
            track_count=12,
            duration_minutes=55,
            seed_artists=["Nils Frahm"],
            seed_tracks=["Avril 14th by Aphex Twin"],
            explicit=ExplicitPreference.EXCLUDE,
            familiarity=70,
            discovery=30,
        ),
    )


def _plan_payload() -> dict[str, object]:
    return {
        "name": "Quiet Focus",
        "description": "Low-key instrumental focus music.",
        "tracks": [
            {
                "title": "Song One",
                "artist": "Artist One",
                "album": "Album One",
                "reason": "Matches the calm focus brief.",
            }
        ],
    }


def test_generation_input_limits_are_strict() -> None:
    with pytest.raises(ValidationError):
        GenerationSpec(prompt="x" * 2_001, controls=GeneratorControls(track_count=10))

    with pytest.raises(ValidationError):
        GeneratorControls(track_count=51)

    with pytest.raises(ValidationError):
        GeneratorControls(genres=[f"genre-{index}" for index in range(11)])


def test_model_output_is_schema_validated_and_bounded() -> None:
    parsed = parse_model_output(
        f"```json\n{json.dumps(_plan_payload())}\n```",
        max_chars=8_000,
        max_tracks=25,
        requested_tracks=12,
    )

    assert parsed.name == "Quiet Focus"
    assert parsed.tracks[0].title == "Song One"

    invalid = _plan_payload()
    invalid["unexpected"] = "not allowed"
    with pytest.raises(GeneratorInvalidOutput, match="schema"):
        parse_model_output(
            json.dumps(invalid),
            max_chars=8_000,
            max_tracks=25,
            requested_tracks=12,
        )

    oversized = json.dumps(_plan_payload()) + (" " * 8_000)
    with pytest.raises(GeneratorInvalidOutput, match="too large"):
        parse_model_output(
            oversized,
            max_chars=8_000,
            max_tracks=25,
            requested_tracks=12,
        )


async def test_openai_request_contains_only_bounded_generation_context() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_plan_payload())}}]},
        )

    model = OpenAICompatibleModel(
        _settings(),
        transport=httpx.MockTransport(handler),
    )
    summary = PreferenceSummary(
        top_artists=["Nils Frahm"],
        top_genres=["ambient"],
        source_track_count=42,
    )

    result = await model.generate(_spec(), summary)

    assert result.name == "Quiet Focus"
    assert captured["url"] == "http://model.test/v1/chat/completions"
    assert captured["authorization"] == "Bearer local-secret"
    body_text = json.dumps(captured["body"])
    assert "A focused evening playlist" in body_text
    assert "Nils Frahm" in body_text
    assert "ambient" in body_text
    assert "local-secret" not in body_text
    assert "target_account_id" not in body_text
    assert "provider_credential" not in body_text
    assert "raw_history" not in body_text


async def test_openai_model_fails_clearly_when_unconfigured() -> None:
    model = OpenAICompatibleModel(_settings(generator_openai_base_url=""))

    with pytest.raises(GeneratorNotConfigured, match="OPE_GENERATOR_OPENAI_BASE_URL"):
        await model.generate(_spec(), None)


async def test_openai_model_maps_unavailability_and_timeout() -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(GeneratorUnavailable, match="unavailable"):
        await OpenAICompatibleModel(
            _settings(),
            transport=httpx.MockTransport(unavailable),
        ).generate(_spec(), None)

    def timed_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(GeneratorTimedOut, match="timed out"):
        await OpenAICompatibleModel(
            _settings(),
            transport=httpx.MockTransport(timed_out),
        ).generate(_spec(), None)


class _CopilotResponseData:
    content = json.dumps(_plan_payload())


class _CopilotResponse:
    data = _CopilotResponseData()


class _FakeCopilotSession:
    def __init__(self) -> None:
        self.prompt: str | None = None
        self.disconnected = False

    async def send_and_wait(self, prompt: str) -> _CopilotResponse:
        self.prompt = prompt
        return _CopilotResponse()

    async def disconnect(self) -> None:
        self.disconnected = True


class _FakeCopilotClient:
    def __init__(self, **kwargs: object) -> None:
        self.client_kwargs = kwargs
        self.session_kwargs: dict[str, object] = {}
        self.session = _FakeCopilotSession()
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def create_session(self, **kwargs: object) -> _FakeCopilotSession:
        self.session_kwargs = kwargs
        return self.session

    async def stop(self) -> None:
        self.stopped = True


async def test_copilot_sdk_uses_empty_toolless_private_session() -> None:
    clients: list[_FakeCopilotClient] = []

    def factory(**kwargs: object) -> _FakeCopilotClient:
        client = _FakeCopilotClient(**kwargs)
        clients.append(client)
        return client

    model = CopilotSDKModel(
        _settings(
            generator_backend=GeneratorBackend.COPILOT_SDK,
            generator_model="auto",
            generator_copilot_github_token="copilot-secret",
        ),
        client_factory=factory,
    )

    result = await model.generate(_spec(), None)

    client = clients[0]
    assert result.name == "Quiet Focus"
    assert client.client_kwargs["mode"] == "empty"
    assert client.client_kwargs["github_token"] == "copilot-secret"
    assert client.session_kwargs["available_tools"] == []
    assert client.session_kwargs["enable_session_telemetry"] is False
    assert client.session_kwargs["enable_config_discovery"] is False
    assert client.session_kwargs["enable_skills"] is False
    assert client.session_kwargs["memory"] == {"enabled": False}
    assert client.session.prompt is not None
    assert "copilot-secret" not in client.session.prompt
    assert client.session.disconnected is True
    assert client.stopped is True


def test_preference_summary_is_local_aggregate_not_raw_history() -> None:
    summary = preference_summary_from_tracks(
        [
            Track(title="Private title one", artist="Artist A", genre="Ambient"),
            Track(title="Private title two", artist="artist a", genre="ambient"),
            Track(title="Private title three", artist="Artist B", genre="Jazz"),
        ],
        max_values=1,
    )

    assert summary.top_artists == ["Artist A"]
    assert summary.top_genres == ["Ambient"]
    assert summary.source_track_count == 3
    dumped = summary.model_dump_json()
    assert "Private title" not in dumped


class _NoWriteAdapter:
    def __init__(self) -> None:
        self.info = ProviderInfo(
            name="fake",
            display_name="Fake",
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
            capabilities=CapabilityDescriptor(
                capabilities={Capability.CREATE_PLAYLIST, Capability.ADD_TRACKS}
            ),
        )
        self.auth = _FakeAuth()
        self.create_calls = 0
        self.add_calls = 0

    async def search_tracks(
        self,
        cred: ProviderCredential,
        track: Track,
        *,
        limit: int = 5,
    ) -> list[TrackCandidate]:
        if track.title == "Missing":
            return []
        if track.title == "Questionable":
            return [
                TrackCandidate(
                    provider_track_id="questionable",
                    uri="fake:questionable",
                    title="Different Cover",
                    artist="Someone Else",
                )
            ]
        return [
            TrackCandidate(
                provider_track_id="shared",
                uri="fake:shared",
                title=track.title,
                artist=track.artist,
            )
        ]

    async def create_playlist(self, cred: ProviderCredential, spec: CreatePlaylistSpec) -> str:
        self.create_calls += 1
        return "created"

    async def add_tracks(
        self,
        cred: ProviderCredential,
        playlist_id: str,
        uris: Sequence[str],
    ) -> list[AddItemResult]:
        self.add_calls += 1
        return [AddItemResult(uri=uri, ok=True) for uri in uris]

    async def iter_playlists(self, cred: ProviderCredential) -> AsyncIterator[PlaylistRef]:
        if False:
            yield PlaylistRef(id="", name="")

    async def iter_playlist_items(
        self,
        cred: ProviderCredential,
        ref: PlaylistRef,
    ) -> AsyncIterator[Track]:
        if False:
            yield Track(title="", artist="")

    async def read_playlist(self, cred: ProviderCredential, ref: PlaylistRef) -> Playlist:
        return Playlist(id=ref.id, name=ref.name)

    async def test_connection(self, cred: ProviderCredential) -> None:
        return None

    async def validate_uri(self, cred: ProviderCredential, uri: str) -> bool:
        return uri.startswith("fake:")


class _FakeAuth:
    kind = AuthKind.LONG_LIVED_TOKEN

    async def begin(self, *, user_id: str, account_label: str | None = None) -> AuthChallenge:
        return AuthChallenge(shape=ChallengeShape.FORM)

    async def complete(self, *, user_id: str, callback: dict[str, Any]) -> ProviderCredential:
        return _credential()

    async def refresh(self, cred: ProviderCredential) -> ProviderCredential:
        return cred

    async def revoke(self, cred: ProviderCredential) -> None:
        return None


def _credential() -> ProviderCredential:
    return ProviderCredential(
        account_id="account",
        provider="fake",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
    )


async def test_resolution_deduplicates_marks_unresolved_and_never_writes() -> None:
    adapter = _NoWriteAdapter()
    plan = GeneratedPlaylistPlan(
        name="Generated",
        tracks=[
            GeneratedTrackIntent(title="Song One", artist="Artist One"),
            GeneratedTrackIntent(title="Song Two", artist="Artist Two"),
            GeneratedTrackIntent(title="Questionable", artist="Expected Artist"),
            GeneratedTrackIntent(title="Missing", artist="Nobody"),
        ],
    )

    items = await resolve_generated_plan(
        plan,
        target=adapter,
        credential=_credential(),
        review_threshold=0.8,
    )

    assert [item.status for item in items] == ["resolved", "needs_review", "unresolved"]
    assert [item.intent.title for item in items] == ["Song One", "Questionable", "Missing"]
    assert adapter.create_calls == 0
    assert adapter.add_calls == 0

    with pytest.raises(ValueError, match="before confirmation"):
        ensure_confirmable(items)
