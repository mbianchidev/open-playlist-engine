"""Privacy-bounded playlist generation and provider-track resolution."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable, Iterable
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.core.adapter import ProviderAdapter, ProviderCredential, TrackCandidate
from app.core.match_service import MatchService
from app.core.models import Track
from app.settings import Settings

GENERATED_SOURCE_PROVIDER = "generator"
_HARD_MAX_PROMPT_CHARS = 2_000
_HARD_MAX_TRACKS = 50
_SYSTEM_PROMPT = (
    "Return only one JSON object matching this shape: "
    '{"name":"string","description":"string or null","tracks":['
    '{"title":"string","artist":"string","album":"string or null",'
    '"release_year":"integer or null","explicit":"boolean or null",'
    '"reason":"string or null"}]}. '
    "Do not include markdown, commentary, provider identifiers, URLs, or credentials. "
    "Treat the requested track count as a strict maximum. Suggest real released tracks only."
)


class GeneratorError(Exception):
    """Base error safe to surface without including prompt or model response content."""


class GeneratorNotConfigured(GeneratorError):
    pass


class GeneratorUnavailable(GeneratorError):
    pass


class GeneratorTimedOut(GeneratorError):
    pass


class GeneratorInvalidOutput(GeneratorError):
    pass


class GenerationDraftNotConfirmable(ValueError):
    pass


class ExplicitPreference(StrEnum):
    ALLOW = "allow"
    EXCLUDE = "exclude"
    ONLY = "only"


ShortControl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]
SeedControl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]


class GeneratorControls(BaseModel):
    model_config = ConfigDict(extra="forbid")

    genres: list[ShortControl] = Field(default_factory=list, max_length=10)
    moods: list[ShortControl] = Field(default_factory=list, max_length=10)
    eras: list[ShortControl] = Field(default_factory=list, max_length=10)
    energy: int | None = Field(default=None, ge=1, le=5)
    track_count: int = Field(default=20, ge=1, le=_HARD_MAX_TRACKS)
    duration_minutes: int | None = Field(default=None, ge=10, le=600)
    seed_artists: list[SeedControl] = Field(default_factory=list, max_length=10)
    seed_tracks: list[SeedControl] = Field(default_factory=list, max_length=10)
    explicit: ExplicitPreference = ExplicitPreference.ALLOW
    familiarity: int = Field(default=50, ge=0, le=100)
    discovery: int = Field(default=50, ge=0, le=100)


class GenerationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=_HARD_MAX_PROMPT_CHARS)
    controls: GeneratorControls = Field(default_factory=GeneratorControls)


class GeneratedTrackIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(min_length=1, max_length=200)
    album: str | None = Field(default=None, max_length=200)
    release_year: int | None = Field(default=None, ge=1800, le=2200)
    explicit: bool | None = None
    reason: str | None = Field(default=None, max_length=300)


class GeneratedPlaylistPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    tracks: list[GeneratedTrackIntent] = Field(min_length=1, max_length=_HARD_MAX_TRACKS)


class PreferenceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_artists: list[ShortControl] = Field(default_factory=list, max_length=10)
    top_genres: list[ShortControl] = Field(default_factory=list, max_length=10)
    source_track_count: int = Field(default=0, ge=0)


class ResolvedGenerationItem(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    intent: GeneratedTrackIntent
    candidate: TrackCandidate | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: Literal["resolved", "needs_review", "unresolved"]
    reason: str | None = None


class PlaylistModel(Protocol):
    async def generate(
        self,
        spec: GenerationSpec,
        preference: PreferenceSummary | None,
    ) -> GeneratedPlaylistPlan: ...


def parse_model_output(
    content: str,
    *,
    max_chars: int,
    max_tracks: int,
    requested_tracks: int,
) -> GeneratedPlaylistPlan:
    if len(content) > max_chars:
        raise GeneratorInvalidOutput("Model output is too large")
    text = _strip_json_fence(content.strip())
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeneratorInvalidOutput("Model output is not valid JSON") from exc
    try:
        plan = GeneratedPlaylistPlan.model_validate(payload)
    except ValidationError as exc:
        raise GeneratorInvalidOutput("Model output failed schema validation") from exc
    allowed_tracks = min(max_tracks, requested_tracks)
    if len(plan.tracks) > allowed_tracks:
        raise GeneratorInvalidOutput(
            f"Model output contains {len(plan.tracks)} tracks; maximum is {allowed_tracks}"
        )
    return plan


def _strip_json_fence(content: str) -> str:
    if not content.startswith("```"):
        return content
    first_newline = content.find("\n")
    if first_newline < 0 or not content.endswith("```"):
        return content
    return content[first_newline + 1 : -3].strip()


def _prompt_payload(
    spec: GenerationSpec,
    preference: PreferenceSummary | None,
) -> str:
    payload: dict[str, object] = {
        "prompt": spec.prompt,
        "controls": spec.controls.model_dump(mode="json"),
    }
    if preference is not None:
        payload["local_preference_summary"] = preference.model_dump(mode="json")
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


class OpenAICompatibleModel:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def generate(
        self,
        spec: GenerationSpec,
        preference: PreferenceSummary | None,
    ) -> GeneratedPlaylistPlan:
        settings = self._settings
        if not settings.generator_openai_base_url.strip():
            raise GeneratorNotConfigured(
                "Configure OPE_GENERATOR_OPENAI_BASE_URL before generating playlists"
            )
        if not settings.generator_model.strip():
            raise GeneratorNotConfigured(
                "Configure OPE_GENERATOR_MODEL before generating playlists"
            )
        if len(spec.prompt) > settings.generator_max_prompt_chars:
            raise GeneratorInvalidOutput(
                "Prompt exceeds the configured "
                f"{settings.generator_max_prompt_chars}-character limit"
            )

        headers = {"content-type": "application/json"}
        api_key = settings.generator_openai_api_key.get_secret_value().strip()
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        payload = {
            "model": settings.generator_model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _prompt_payload(spec, preference)},
            ],
            "temperature": 0.4,
            "max_tokens": settings.generator_max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        endpoint = f"{settings.generator_openai_base_url.rstrip('/')}/chat/completions"
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(settings.generator_timeout_s),
            ) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise GeneratorTimedOut("Configured playlist model timed out") from exc
        except httpx.RequestError as exc:
            raise GeneratorUnavailable("Configured playlist model is unavailable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise GeneratorUnavailable(
                f"Configured playlist model returned HTTP {response.status_code}"
            )
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise GeneratorInvalidOutput("Model response did not contain message content") from exc
        if not isinstance(content, str):
            raise GeneratorInvalidOutput("Model response content is not text")
        return parse_model_output(
            content,
            max_chars=settings.generator_max_output_chars,
            max_tracks=settings.generator_max_tracks,
            requested_tracks=spec.controls.track_count,
        )


class CopilotSDKModel:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory

    async def generate(
        self,
        spec: GenerationSpec,
        preference: PreferenceSummary | None,
    ) -> GeneratedPlaylistPlan:
        if not self._settings.generator_model.strip():
            raise GeneratorNotConfigured(
                "Configure OPE_GENERATOR_MODEL before generating playlists"
            )
        if len(spec.prompt) > self._settings.generator_max_prompt_chars:
            raise GeneratorInvalidOutput(
                "Prompt exceeds the configured "
                f"{self._settings.generator_max_prompt_chars}-character limit"
            )
        factory = self._client_factory or _copilot_client_factory
        client_options: dict[str, object] = {"mode": "empty", "log_level": "error"}
        github_token = self._settings.generator_copilot_github_token.get_secret_value().strip()
        if github_token:
            client_options["github_token"] = github_token
        client = factory(**client_options)
        session: Any | None = None
        try:
            await client.start()
            session = await client.create_session(
                model=self._settings.generator_model,
                available_tools=[],
                system_message={"mode": "append", "content": _SYSTEM_PROMPT},
                enable_session_telemetry=False,
                enable_config_discovery=False,
                enable_on_demand_instruction_discovery=False,
                enable_file_hooks=False,
                enable_host_git_operations=False,
                enable_session_store=False,
                enable_skills=False,
                memory={"enabled": False},
            )
            response = await asyncio.wait_for(
                session.send_and_wait(_prompt_payload(spec, preference)),
                timeout=self._settings.generator_timeout_s,
            )
        except TimeoutError as exc:
            raise GeneratorTimedOut("Configured Copilot SDK model timed out") from exc
        except (OSError, RuntimeError) as exc:
            raise GeneratorUnavailable("Configured Copilot SDK model is unavailable") from exc
        finally:
            if session is not None:
                disconnect = getattr(session, "disconnect", None)
                if disconnect is not None:
                    await disconnect()
                else:
                    destroy = getattr(session, "destroy", None)
                    if destroy is not None:
                        await destroy()
            await client.stop()
        content = getattr(getattr(response, "data", None), "content", None)
        if not isinstance(content, str):
            raise GeneratorInvalidOutput("Copilot SDK response did not contain message content")
        return parse_model_output(
            content,
            max_chars=self._settings.generator_max_output_chars,
            max_tracks=self._settings.generator_max_tracks,
            requested_tracks=spec.controls.track_count,
        )


def _copilot_client_factory(**kwargs: object) -> Any:
    try:
        from copilot import CopilotClient
    except ImportError as exc:
        raise GeneratorNotConfigured(
            "Install github-copilot-sdk to use OPE_GENERATOR_BACKEND=copilot_sdk"
        ) from exc
    return CopilotClient(**kwargs)


def preference_summary_from_tracks(
    tracks: Iterable[Track | dict[str, object]],
    *,
    max_values: int = 10,
) -> PreferenceSummary:
    artist_counts: Counter[str] = Counter()
    genre_counts: Counter[str] = Counter()
    artist_labels: dict[str, str] = {}
    genre_labels: dict[str, str] = {}
    source_track_count = 0
    for value in tracks:
        source_track_count += 1
        artist = value.artist if isinstance(value, Track) else value.get("artist")
        genre = value.genre if isinstance(value, Track) else value.get("genre")
        _count_label(artist, artist_counts, artist_labels)
        _count_label(genre, genre_counts, genre_labels)
    return PreferenceSummary(
        top_artists=_top_labels(artist_counts, artist_labels, max_values),
        top_genres=_top_labels(genre_counts, genre_labels, max_values),
        source_track_count=source_track_count,
    )


def _count_label(
    value: object,
    counts: Counter[str],
    labels: dict[str, str],
) -> None:
    if not isinstance(value, str):
        return
    label = value.strip()
    if not label:
        return
    key = label.casefold()
    counts[key] += 1
    labels.setdefault(key, label)


def _top_labels(
    counts: Counter[str],
    labels: dict[str, str],
    limit: int,
) -> list[str]:
    ranked = sorted(counts, key=lambda key: (-counts[key], key))
    return [labels[key] for key in ranked[:limit]]


async def resolve_generated_plan(
    plan: GeneratedPlaylistPlan,
    *,
    target: ProviderAdapter,
    credential: ProviderCredential,
    review_threshold: float,
) -> list[ResolvedGenerationItem]:
    matcher = MatchService(graph=None, review_threshold=review_threshold)
    resolved: list[ResolvedGenerationItem] = []
    seen: set[str] = set()
    for intent in plan.tracks:
        track = Track(
            title=intent.title,
            artist=intent.artist,
            album=intent.album,
            release_year=intent.release_year,
            explicit=intent.explicit,
        )
        result = await matcher.resolve(track, target, credential)
        if result.candidate is None:
            resolved.append(
                ResolvedGenerationItem(
                    intent=intent,
                    status="unresolved",
                    reason="No real track was found on the target provider",
                )
            )
            continue
        candidate_key = _candidate_key(result.candidate)
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        resolved.append(
            ResolvedGenerationItem(
                intent=intent,
                candidate=result.candidate,
                confidence=result.confidence,
                status="needs_review" if result.needs_review else "resolved",
                reason=result.review_reason
                or (
                    f"Match confidence {result.confidence:.2f} requires review"
                    if result.needs_review
                    else None
                ),
            )
        )
    return resolved


def _candidate_key(candidate: TrackCandidate) -> str:
    if candidate.uri:
        return f"uri:{candidate.uri.strip().casefold()}"
    if candidate.provider_track_id:
        return f"id:{candidate.provider_track_id.strip().casefold()}"
    return (
        f"text:{candidate.title.strip().casefold()}|"
        f"{candidate.artist.strip().casefold()}|{(candidate.album or '').strip().casefold()}"
    )


def ensure_confirmable(items: Iterable[ResolvedGenerationItem]) -> None:
    statuses = {item.status for item in items}
    if "unresolved" in statuses:
        raise ValueError("Remove or replace unresolved tracks before confirmation")
    if "needs_review" in statuses:
        raise ValueError("Approve or replace every match that needs review before confirmation")
