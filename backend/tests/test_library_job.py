from __future__ import annotations

from collections.abc import Sequence

from app.core.adapter import AddItemResult, AuthKind, ProviderCredential, ProviderInfo
from app.core.capabilities import Capability, CapabilityDescriptor
from app.core.library_match import LibraryMatchResult
from app.core.models import Album, ArtistCollectionSemantics, MigrationEntityType
from app.db import models as orm
from app.jobs import migration


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)


class LibraryTarget:
    def __init__(self) -> None:
        self.info = ProviderInfo(
            name="target",
            display_name="Target",
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
            capabilities=CapabilityDescriptor(
                capabilities={
                    Capability.READ_SAVED_ALBUMS,
                    Capability.WRITE_SAVED_ALBUMS,
                },
                max_library_batch=2,
            ),
            artist_collection_semantics=ArtistCollectionSemantics.FOLLOW,
        )
        self.saved: list[list[str]] = []

    async def contains_saved_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]:
        return [uri.endswith("existing") for uri in uris]

    async def save_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[AddItemResult]:
        self.saved.append(list(uris))
        return [AddItemResult(uri=uri, ok=True, position=index) for index, uri in enumerate(uris)]


class MaterializationSource:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.info = ProviderInfo(
            name="source",
            display_name="Source",
            auth_kind=AuthKind.LONG_LIVED_TOKEN,
            capabilities=CapabilityDescriptor(
                capabilities={Capability.READ_SAVED_ALBUMS}
            ),
        )

    async def contains_saved_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]:
        rows = [row for row in self.session.added if isinstance(row, orm.JobItem)]
        assert len(rows) == len(uris)
        assert all(row.status == "pending" for row in rows)
        return [False] * len(uris)


class PresentAlbumSource(MaterializationSource):
    async def contains_saved_albums(
        self, cred: ProviderCredential, uris: Sequence[str]
    ) -> list[bool]:
        return [True] * len(uris)

    async def read_saved_album(
        self, cred: ProviderCredential, album_id: str
    ) -> Album:
        return Album(id=album_id, title="Unknown Album", artists=["Unknown Artist"])


def _item(uri: str) -> orm.JobItem:
    return orm.JobItem(
        job_id="job",
        entity_type=MigrationEntityType.ALBUM,
        source_entity_id=uri,
        source_entity_name=uri,
        position=0,
        title=uri,
        artist="Artist",
        target_entity_id=uri.rsplit(":", 1)[-1],
        target_uri=uri,
        status="matched",
    )


async def test_library_write_skips_existing_and_ledgers_new_items(monkeypatch) -> None:
    async def noop_commit(session, job) -> None:
        return None

    monkeypatch.setattr(migration, "commit_job_counts", noop_commit)
    session = FakeSession()
    target = LibraryTarget()
    job = orm.MigrationJob(id="job", user_id="local")
    cred = ProviderCredential(
        account_id="target",
        provider="target",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
    )
    existing = _item("target:album:existing")
    new = _item("target:album:new")

    await migration._write_library_items(
        session,
        job,
        target,
        cred,
        MigrationEntityType.ALBUM,
        [existing, new],
    )

    assert existing.status == "skipped"
    assert "already saved" in (existing.reason or "")
    assert new.status == "written"
    assert target.saved == [["target:album:new"]]
    ledgers = [row for row in session.added if isinstance(row, orm.OperationLedger)]
    assert len(ledgers) == 1
    assert ledgers[0].op == "save_album"


def test_library_job_item_preserves_explicit_entity_metadata() -> None:
    item = migration._library_job_item(
        "job",
        MigrationEntityType.ALBUM,
        Album(
            id="album-1",
            title="Album One",
            artists=["Artist One"],
            upc="0123456789012",
        ),
        position=3,
    )

    assert item.entity_type == MigrationEntityType.ALBUM
    assert item.source_playlist_id is None
    assert item.source_entity_id == "album-1"
    assert item.source_entity_name == "Album One"
    assert item.position == 3
    assert item.source_metadata["upc"] == "0123456789012"


async def test_library_items_are_materialized_before_provider_io(monkeypatch) -> None:
    async def noop_commit(session, job) -> None:
        return None

    async def unread(*args, **kwargs):
        raise AssertionError("membership failures must not read catalog entities")

    async def unresolved(*args, **kwargs):
        raise AssertionError("membership failures must not search target entities")

    monkeypatch.setattr(migration, "commit_job_counts", noop_commit)
    session = FakeSession()
    source = MaterializationSource(session)
    target = LibraryTarget()
    job = orm.MigrationJob(id="job", user_id="local")
    cred = ProviderCredential(
        account_id="account",
        provider="source",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
    )

    await migration._migrate_library_entities(
        session,
        job=job,
        source=source,
        target=target,
        source_cred=cred,
        target_cred=cred,
        entity_type=MigrationEntityType.ALBUM,
        entity_ids=["album-1", "album-2"],
        read_entity=unread,
        resolve=unresolved,
    )

    items = [row for row in session.added if isinstance(row, orm.JobItem)]
    assert len(items) == 2
    assert all(item.status == "skipped" for item in items)
    assert all("source library" in (item.reason or "") for item in items)


async def test_unmatched_library_item_remains_reviewable(monkeypatch) -> None:
    async def noop_commit(session, job) -> None:
        return None

    async def unresolved(*args, **kwargs):
        return LibraryMatchResult(
            candidate=None,
            confidence=0.0,
            source="none",
            needs_review=True,
            review_reason="No target album match found.",
        )

    monkeypatch.setattr(migration, "commit_job_counts", noop_commit)
    session = FakeSession()
    source = PresentAlbumSource(session)
    target = LibraryTarget()
    job = orm.MigrationJob(id="job", user_id="local")
    cred = ProviderCredential(
        account_id="account",
        provider="source",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
    )

    await migration._migrate_library_entities(
        session,
        job=job,
        source=source,
        target=target,
        source_cred=cred,
        target_cred=cred,
        entity_type=MigrationEntityType.ALBUM,
        entity_ids=["album-1"],
        read_entity=source.read_saved_album,
        resolve=unresolved,
    )

    item = next(row for row in session.added if isinstance(row, orm.JobItem))
    assert item.status == "needs_review"
    assert item.target_uri is None
