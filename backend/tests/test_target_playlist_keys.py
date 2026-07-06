from app.api import migrations as migration_api
from app.core.adapter import AuthKind, NotFound, ProviderCredential
from app.core.models import Playlist, PlaylistRef, Track
from app.jobs import migration as migration_job


def _cred() -> ProviderCredential:
    return ProviderCredential(
        account_id="acc",
        provider="target",
        auth_kind=AuthKind.LONG_LIVED_TOKEN,
    )


class UnreadableTarget:
    async def read_playlist(self, cred: ProviderCredential, ref: PlaylistRef) -> Playlist:
        raise NotFound(ref.id)


class ReadableTarget:
    async def read_playlist(self, cred: ProviderCredential, ref: PlaylistRef) -> Playlist:
        return Playlist(
            id=ref.id,
            name=ref.name,
            tracks=[Track(id="target-1", title="Song One", artist="Artist One")],
        )


async def test_job_duplicate_check_treats_unreadable_target_playlist_as_empty() -> None:
    keys = await migration_job._target_playlist_keys(UnreadableTarget(), _cred(), "PL-new")

    assert keys == set()


async def test_review_duplicate_check_treats_unreadable_target_playlist_as_empty() -> None:
    keys = await migration_api._target_playlist_keys(UnreadableTarget(), _cred(), "PL-new")

    assert keys == set()


async def test_job_duplicate_check_still_reads_available_target_playlist() -> None:
    keys = await migration_job._target_playlist_keys(ReadableTarget(), _cred(), "PL-existing")

    assert "id:target-1" in keys
