from collections.abc import Sequence

from app.core.adapter import AddItemResult, AuthKind, ProviderCredential, ProviderInfo
from app.core.capabilities import Capability, CapabilityDescriptor
from app.db import models as orm
from app.jobs import migration as migration_job


class FakeSession:
    def __init__(self) -> None:
        self.added = []

    def add(self, row) -> None:
        self.added.append(row)


class ChunkTarget:
    def __init__(self) -> None:
        self.info = ProviderInfo(
            name="target",
            display_name="Target",
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
            capabilities=CapabilityDescriptor(
                capabilities={Capability.ADD_TRACKS},
                max_add_batch=2,
            ),
        )
        self.calls: list[list[str]] = []

    async def add_tracks(
        self, cred: ProviderCredential, playlist_id: str, uris: Sequence[str]
    ) -> list[AddItemResult]:
        self.calls.append(list(uris))
        return [
            AddItemResult(uri=uri, ok=True, position=position)
            for position, uri in enumerate(uris)
        ]


def _item(uri: str) -> orm.JobItem:
    return orm.JobItem(
        job_id="job",
        source_playlist_id="source-playlist",
        position=0,
        title=uri,
        artist="Artist",
        target_playlist_id="target-playlist",
        target_uri=uri,
        source_metadata={},
        status="matched",
    )


async def test_flush_matched_chunk_waits_until_provider_batch_is_full(
    monkeypatch,
) -> None:
    async def noop_commit(session, job) -> None:
        return None

    monkeypatch.setattr(migration_job, "commit_job_counts", noop_commit)
    target = ChunkTarget()
    session = FakeSession()
    job = orm.MigrationJob(
        id="job",
        user_id="local",
        source_provider="source",
        target_provider="target",
    )
    cred = ProviderCredential(
        account_id="target-account",
        provider="target",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
    )
    first = _item("target:one")
    matched = [first]
    existing_keys: set[str] = set()

    await migration_job._flush_matched_chunk(
        session,
        job,
        target,
        cred,
        "target-playlist",
        matched,
        existing_keys=existing_keys,
    )

    assert target.calls == []
    assert [item.target_uri for item in matched] == ["target:one"]

    second = _item("target:two")
    matched.append(second)
    await migration_job._flush_matched_chunk(
        session,
        job,
        target,
        cred,
        "target-playlist",
        matched,
        existing_keys=existing_keys,
    )

    assert target.calls == [["target:one", "target:two"]]
    assert matched == []
    assert first.status == "written"
    assert second.status == "written"
    assert len(session.added) == 2
