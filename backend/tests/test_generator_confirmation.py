from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.adapter import AuthKind, ProviderCredential
from app.core.generator import GENERATED_SOURCE_PROVIDER, GenerationDraftNotConfirmable
from app.db import models as orm
from app.jobs import migration
from app.jobs.generator import build_confirmed_job


def _draft() -> orm.GenerationDraft:
    return orm.GenerationDraft(
        id="draft-1",
        user_id="local",
        target_provider="fake",
        target_account_id="target-account",
        name="Generated playlist",
        description="Generated locally.",
        model_backend="openai_compatible",
        status="draft",
    )


def _item(
    item_id: str,
    position: int,
    *,
    status: str = "resolved",
    target_uri: str | None = None,
) -> orm.GenerationDraftItem:
    return orm.GenerationDraftItem(
        id=item_id,
        draft_id="draft-1",
        position=position,
        intent_title=f"Song {position}",
        intent_artist=f"Artist {position}",
        resolved_title=f"Song {position}",
        resolved_artist=f"Artist {position}",
        provider_track_id=f"track-{position}",
        target_uri=target_uri or f"fake:track:{position}",
        confidence=1.0,
        status=status,
    )


def test_confirmation_snapshots_only_reviewed_real_tracks() -> None:
    draft = _draft()
    job, items = build_confirmed_job(draft, [_item("second", 1), _item("first", 0)])

    assert draft.status == "confirmed"
    assert draft.confirmed_job_id == job.id
    assert job.source_provider == GENERATED_SOURCE_PROVIDER
    assert job.source_account_id == draft.id
    assert job.target_provider == "fake"
    assert job.selection["playlist_ids"] == [draft.id]
    assert job.selection["generated_playlist"]["name"] == "Generated playlist"
    assert [item.position for item in items] == [0, 1]
    assert [item.target_uri for item in items] == ["fake:track:0", "fake:track:1"]
    assert all(item.status == "matched" for item in items)


def test_confirmation_rejects_unresolved_or_unreviewed_items() -> None:
    with pytest.raises(GenerationDraftNotConfirmable, match="unresolved"):
        build_confirmed_job(_draft(), [_item("missing", 0, status="unresolved", target_uri=None)])

    with pytest.raises(GenerationDraftNotConfirmable, match="review"):
        build_confirmed_job(_draft(), [_item("review", 0, status="needs_review")])


def test_confirmation_is_single_use() -> None:
    draft = _draft()
    build_confirmed_job(draft, [_item("first", 0)])

    with pytest.raises(GenerationDraftNotConfirmable, match="already confirmed"):
        build_confirmed_job(draft, [_item("first", 0)])


async def test_generated_jobs_fork_before_source_registry_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    async def run_generated(session: object, job: orm.MigrationJob) -> None:
        called.append(job.id)

    def unexpected_registry(provider: str) -> object:
        raise AssertionError(f"registry lookup should not run for {provider}")

    monkeypatch.setattr(migration, "_run_generated", run_generated)
    monkeypatch.setattr(migration, "get", unexpected_registry)
    job = orm.MigrationJob(
        id="job-1",
        user_id="local",
        source_provider=GENERATED_SOURCE_PROVIDER,
        source_account_id="draft-1",
        target_provider="fake",
        target_account_id="target-account",
        selection={"playlist_ids": ["draft-1"]},
        status="pending",
    )

    await migration._run(object(), job)  # type: ignore[arg-type]

    assert called == ["job-1"]


class _GeneratedSession:
    def __init__(self, items: list[orm.JobItem]) -> None:
        self.items = items
        self.commits = 0

    async def execute(self, statement: object) -> object:
        items = self.items

        class _Result:
            def scalars(self) -> list[orm.JobItem]:
                return items

        return _Result()

    async def commit(self) -> None:
        self.commits += 1


async def test_generated_worker_writes_confirmed_uris_without_rematching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = orm.MigrationJob(
        id="job-1",
        user_id="local",
        source_provider=GENERATED_SOURCE_PROVIDER,
        source_account_id="draft-1",
        target_provider="fake",
        target_account_id="target-account",
        selection={
            "playlist_ids": ["draft-1"],
            "generated_playlist": {
                "draft_id": "draft-1",
                "name": "Generated playlist",
                "description": "Reviewed first",
            },
        },
        status="pending",
    )
    items = [
        orm.JobItem(
            id="item-1",
            job_id=job.id,
            source_playlist_id="draft-1",
            source_playlist_name="Generated playlist",
            position=0,
            title="Song",
            artist="Artist",
            source_metadata={},
            target_uri="fake:track:1",
            status="matched",
        )
    ]
    session = _GeneratedSession(items)
    adapter = SimpleNamespace(
        info=SimpleNamespace(
            capabilities=SimpleNamespace(max_add_batch=100),
        )
    )
    credential = ProviderCredential(
        account_id="target-account",
        provider="fake",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
    )
    writes: list[list[str | None]] = []

    monkeypatch.setattr(migration, "get", lambda provider: adapter)

    async def load_credential(*args: object, **kwargs: object) -> tuple[ProviderCredential, object]:
        return credential, object()

    async def resolve_target(*args: object, **kwargs: object) -> str:
        return "target-playlist"

    async def target_keys(*args: object, **kwargs: object) -> set[str]:
        return set()

    async def write_items(
        session_arg: object,
        job_arg: orm.MigrationJob,
        target_arg: object,
        credential_arg: ProviderCredential,
        playlist_id: str,
        write_items_arg: list[orm.JobItem],
        *,
        existing_keys: set[str],
    ) -> None:
        writes.append([item.target_uri for item in write_items_arg])
        for item in write_items_arg:
            item.status = "written"

    async def counts(session_arg: object, job_arg: orm.MigrationJob) -> None:
        return None

    monkeypatch.setattr(migration, "load_fresh_credential", load_credential)
    monkeypatch.setattr(migration, "_resolve_target_playlist", resolve_target)
    monkeypatch.setattr(migration, "_target_playlist_keys", target_keys)
    monkeypatch.setattr(migration, "_write_matched_items", write_items)
    monkeypatch.setattr(migration, "commit_job_counts", counts)

    await migration._run_generated(session, job)  # type: ignore[arg-type]

    assert writes == [["fake:track:1"]]
    assert items[0].target_playlist_id == "target-playlist"
    assert job.status == "done"
