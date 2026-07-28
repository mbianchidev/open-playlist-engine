from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.models import Playlist, Track
from app.core.sharing import ShareVisibility, build_shared_snapshot
from app.db.base import Base
from app.db.shares import (
    ShareNotFound,
    ShareUnavailable,
    create_playlist_share,
    decrypt_share_token,
    load_public_share,
    revoke_playlist_share,
)
from app.settings import Settings


def _settings(**updates) -> Settings:
    return Settings(
        secret_key="s" * 64,
        owner_access_token="o" * 48,
        **updates,
    )


def _snapshot():
    return build_shared_snapshot(
        Playlist(
            name="Immutable",
            description="Published once",
            tracks=[Track(title="Track", artist="Artist")],
        ),
        provider="spotify",
        playlist_id="playlist",
        attribution="Owner",
        approved_artwork_hosts={"i.scdn.co"},
        max_tracks=10,
        max_bytes=100_000,
    )


def test_public_sharing_is_disabled_until_public_url_and_strong_secrets_are_configured() -> None:
    assert not Settings().sharing_enabled
    assert "OPE_PUBLIC_BASE_URL" in Settings().sharing_disabled_reason

    missing_owner_token = Settings(
        public_base_url="https://music.example",
        secret_key="s" * 64,
    )
    assert missing_owner_token.owner_auth_required
    assert not missing_owner_token.sharing_enabled
    assert "OPE_OWNER_ACCESS_TOKEN" in missing_owner_token.sharing_disabled_reason

    weak_secret = Settings(
        public_base_url="https://music.example",
        owner_access_token="o" * 48,
        secret_key="short",
    )
    assert not weak_secret.sharing_enabled
    assert "OPE_SECRET_KEY" in weak_secret.sharing_disabled_reason

    configured = _settings(public_base_url="https://music.example/")
    assert configured.sharing_enabled
    assert configured.public_base_url_normalized == "https://music.example"
    assert "i.scdn.co" in configured.approved_share_artwork_hosts


@pytest.mark.asyncio
async def test_share_persistence_uses_hash_lookup_and_keeps_an_immutable_copy() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    now = datetime(2026, 7, 14, tzinfo=UTC)
    snapshot = _snapshot()
    async with sessionmaker() as session:
        share, token = await create_playlist_share(
            session,
            owner_user_id="local",
            snapshot=snapshot,
            visibility=ShareVisibility.UNLISTED,
            expires_at=now + timedelta(days=7),
        )
        snapshot.name = "Mutated after publish"
        snapshot.tracks[0].title = "Mutated after publish"
        await session.commit()

        assert token not in share.token_hash
        assert token.encode() not in share.enc_token
        assert decrypt_share_token(share) == token

        loaded = await load_public_share(session, token, now=now)
        assert loaded.id == share.id
        assert loaded.snapshot["name"] == "Immutable"
        assert loaded.snapshot["tracks"][0]["title"] == "Track"

        with pytest.raises(ShareNotFound):
            await load_public_share(session, f"{token}wrong", now=now)

    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_and_revoked_share_tokens_return_clear_unavailable_states() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    now = datetime(2026, 7, 14, tzinfo=UTC)
    async with sessionmaker() as session:
        expired, expired_token = await create_playlist_share(
            session,
            owner_user_id="local",
            snapshot=_snapshot(),
            visibility=ShareVisibility.PUBLIC,
            expires_at=now - timedelta(seconds=1),
        )
        active, active_token = await create_playlist_share(
            session,
            owner_user_id="local",
            snapshot=_snapshot(),
            visibility=ShareVisibility.UNLISTED,
            expires_at=None,
        )
        await session.commit()

        with pytest.raises(ShareUnavailable, match="expired") as expired_error:
            await load_public_share(session, expired_token, now=now)
        assert expired_error.value.reason == "expired"
        assert expired.id

        await revoke_playlist_share(session, active, now=now)
        await session.commit()
        with pytest.raises(ShareUnavailable, match="revoked") as revoked_error:
            await load_public_share(session, active_token, now=now)
        assert revoked_error.value.reason == "revoked"

        loaded_revoked = await load_public_share(
            session,
            active_token,
            now=now,
            require_active=False,
        )
        assert loaded_revoked.id == active.id

    await engine.dispose()
