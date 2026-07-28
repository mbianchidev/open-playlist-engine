"""ORM models.

Two concerns are deliberately separated (duck review):

* **Private, per-user data** — accounts, credentials, jobs, ledger.
* **Evidence graph** — ``TrackIdentity`` + ``TrackEdge``. Keyed by an internal
  UUID (NOT ISRC), storing candidate edges with confidence/evidence so one
  context's bad match never becomes global truth. PII-free and only shared
  across users when ``OPE_ENABLE_SHARED_GRAPH`` is on.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import MigrationEntityType
from app.db.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Identity & credentials (private)
# --------------------------------------------------------------------------- #
class ProviderAccount(Base):
    __tablename__ = "provider_account"
    __table_args__ = (UniqueConstraint("user_id", "provider", "provider_user_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    provider: Mapped[str] = mapped_column(String, index=True)
    provider_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    ephemeral_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    credentials: Mapped[list[ProviderCredential]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class ProviderCredential(Base):
    __tablename__ = "provider_credential"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("provider_account.id", ondelete="CASCADE"))
    auth_kind: Mapped[str] = mapped_column(String)
    enc_blob: Mapped[bytes] = mapped_column(LargeBinary)  # encrypted token JSON
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    account: Mapped[ProviderAccount] = relationship(back_populates="credentials")


# --------------------------------------------------------------------------- #
# Provider read cache (private)
# --------------------------------------------------------------------------- #
class CachedPlaylistRef(Base):
    __tablename__ = "cached_playlist_ref"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", "account_id", "playlist_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    provider: Mapped[str] = mapped_column(String, index=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("provider_account.id", ondelete="CASCADE"), index=True
    )
    playlist_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    track_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_name: Mapped[str | None] = mapped_column(String, nullable=True)
    is_owned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_followed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    collaborative: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    snapshot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tracks_href: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CachedPlaylistTracks(Base):
    __tablename__ = "cached_playlist_tracks"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", "account_id", "playlist_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    provider: Mapped[str] = mapped_column(String, index=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("provider_account.id", ondelete="CASCADE"), index=True
    )
    playlist_id: Mapped[str] = mapped_column(String, index=True)
    snapshot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String)
    owner_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tracks: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --------------------------------------------------------------------------- #
# Playlist generator preferences and private review drafts
# --------------------------------------------------------------------------- #
class GenerationPreference(Base):
    __tablename__ = "generation_preference"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GenerationDraft(Base):
    __tablename__ = "generation_draft"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    target_provider: Mapped[str] = mapped_column(String)
    target_account_id: Mapped[str] = mapped_column(
        ForeignKey("provider_account.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    model_backend: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft", index=True)
    confirmed_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    items: Mapped[list[GenerationDraftItem]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )


class GenerationDraftItem(Base):
    __tablename__ = "generation_draft_item"
    __table_args__ = (UniqueConstraint("draft_id", "position"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("generation_draft.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    intent_title: Mapped[str] = mapped_column(String)
    intent_artist: Mapped[str] = mapped_column(String)
    intent_album: Mapped[str | None] = mapped_column(String, nullable=True)
    intent_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_track_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_title: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_artist: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_album: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    isrc: Mapped[str | None] = mapped_column(String, nullable=True)
    explicit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, index=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    draft: Mapped[GenerationDraft] = relationship(back_populates="items")


# --------------------------------------------------------------------------- #
# Ephemeral normalized imports (private): local-file uploads, public URL
# resolutions, and pasted-text snapshots all share this lease-backed table.
# ``source_kind``/``source_provider``/``source_label``/``source_locator``/
# ``source_fingerprint`` are only populated for URL/text records; the
# required local-file columns (filename, detected_format, ...) are always
# populated with synthetic values for those records so the rest of the
# migration pipeline can treat every row uniformly.
# --------------------------------------------------------------------------- #
class LocalPlaylistImport(Base):
    __tablename__ = "local_playlist_import"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    filename: Mapped[str] = mapped_column(String)
    detected_format: Mapped[str] = mapped_column(String)
    encoding: Mapped[str | None] = mapped_column(String, nullable=True)
    file_size: Mapped[int] = mapped_column(Integer)
    # ready | queued | failed
    status: Mapped[str] = mapped_column(String, default="ready", index=True)
    queued_job_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    playlists: Mapped[list] = mapped_column(JSON, default=list)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    playlist_count: Mapped[int] = mapped_column(Integer, default=0)
    track_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    malformed_count: Mapped[int] = mapped_column(Integer, default=0)
    unsupported_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # public-url/pasted-text metadata (nullable; unused by local-file imports)
    source_kind: Mapped[str] = mapped_column(
        String, server_default="local_file", default="local_file"
    )
    source_provider: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    source_label: Mapped[str | None] = mapped_column(String, nullable=True)
    source_locator: Mapped[str | None] = mapped_column(String, nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True, index=True)


# --------------------------------------------------------------------------- #
# Local library snapshots (private metadata; archives live in configured storage)
# --------------------------------------------------------------------------- #
class SnapshotProfile(Base):
    __tablename__ = "snapshot_profile"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    retention_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sources: Mapped[list[SnapshotProfileSource]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list[LibrarySnapshot]] = relationship(back_populates="profile")


class SnapshotProfileSource(Base):
    __tablename__ = "snapshot_profile_source"
    __table_args__ = (UniqueConstraint("profile_id", "provider", "account_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("snapshot_profile.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_account.id", ondelete="SET NULL"), nullable=True, index=True
    )
    account_label: Mapped[str | None] = mapped_column(String, nullable=True)
    collection_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    profile: Mapped[SnapshotProfile] = relationship(back_populates="sources")


class LibrarySnapshot(Base):
    __tablename__ = "library_snapshot"
    __table_args__ = (
        UniqueConstraint("user_id", "archive_sha256", name="uq_library_snapshot_archive"),
        Index(
            "uq_library_snapshot_active_profile",
            "profile_id",
            unique=True,
            postgresql_where=text(
                "profile_id IS NOT NULL AND status IN ('pending', 'running')"
            ),
            sqlite_where=text("profile_id IS NOT NULL AND status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("snapshot_profile.id", ondelete="SET NULL"), nullable=True, index=True
    )
    bundle_id: Mapped[str] = mapped_column(String, index=True)
    library_id: Mapped[str] = mapped_column(String, index=True)
    source_providers: Mapped[list] = mapped_column(JSON, default=list)
    source_labels: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    archive_name: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    verification_status: Mapped[str] = mapped_column(String, default="unverified")
    verification_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    profile: Mapped[SnapshotProfile | None] = relationship(back_populates="snapshots")


# --------------------------------------------------------------------------- #
# Jobs & operation ledger (private)
# --------------------------------------------------------------------------- #
class MigrationJob(Base):
    __tablename__ = "migration_job"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    source_kind: Mapped[str] = mapped_column(String, default="provider", index=True)
    source_provider: Mapped[str] = mapped_column(String)
    target_provider: Mapped[str] = mapped_column(String)
    source_account_id: Mapped[str] = mapped_column(String)
    source_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("library_snapshot.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_account_id: Mapped[str] = mapped_column(String)
    source_share_id: Mapped[str | None] = mapped_column(
        ForeignKey("playlist_share.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    selection: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    origin: Mapped[str] = mapped_column(String, default="manual", index=True)
    sync_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("sync_run.id", ondelete="CASCADE"), nullable=True, unique=True, index=True
    )
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    result_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    details_purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    items: Mapped[list[JobItem]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    sync_run: Mapped[SyncRun | None] = relationship(back_populates="migration_job")


# --------------------------------------------------------------------------- #
# Immutable public playlist shares
# --------------------------------------------------------------------------- #
class PlaylistShare(Base):
    __tablename__ = "playlist_share"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(String, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    enc_token: Mapped[bytes] = mapped_column(LargeBinary)
    visibility: Mapped[str] = mapped_column(String, default="unlisted")
    snapshot_schema_version: Mapped[str] = mapped_column(String, default="1.0")
    snapshot: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ShareRecipientAuthState(Base):
    __tablename__ = "share_recipient_auth_state"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    share_id: Mapped[str] = mapped_column(
        ForeignKey("playlist_share.id", ondelete="CASCADE"), index=True
    )
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    recipient_user_id: Mapped[str] = mapped_column(String, index=True)
    provider: Mapped[str] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobItem(Base):
    __tablename__ = "job_item"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("migration_job.id", ondelete="CASCADE"), index=True
    )
    entity_type: Mapped[str] = mapped_column(
        String, default=MigrationEntityType.TRACK.value, index=True
    )
    source_playlist_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_playlist_name: Mapped[str | None] = mapped_column(String, nullable=True)
    target_playlist_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_entity_name: Mapped[str | None] = mapped_column(String, nullable=True)
    target_entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String)
    artist: Mapped[str] = mapped_column(String)
    album: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    release_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    explicit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    isrc: Mapped[str | None] = mapped_column(String, nullable=True)
    source_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    target_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # pending | matched | needs_review | written | skipped | failed
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    review_action: Mapped[str | None] = mapped_column(String, nullable=True)
    review_original_status: Mapped[str | None] = mapped_column(String, nullable=True)
    review_original_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    job: Mapped[MigrationJob] = relationship(back_populates="items")


class ReviewDecision(Base):
    """Accepted low-confidence decisions retained after detailed job rows expire."""

    __tablename__ = "review_decision"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("migration_job.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(String, index=True)
    source_provider: Mapped[str] = mapped_column(String)
    target_provider: Mapped[str] = mapped_column(String)
    source_account_id: Mapped[str] = mapped_column(String)
    target_account_id: Mapped[str] = mapped_column(String)
    entity_type: Mapped[str] = mapped_column(
        String,
        default=MigrationEntityType.TRACK.value,
        server_default=MigrationEntityType.TRACK.value,
        index=True,
    )
    source_entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_entity_name: Mapped[str | None] = mapped_column(String, nullable=True)
    target_entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    artist: Mapped[str] = mapped_column(String)
    album: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    isrc: Mapped[str | None] = mapped_column(String, nullable=True)
    source_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    target_uri: Mapped[str] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OperationLedger(Base):
    """Records intended vs observed provider writes so retries reconcile by
    reading target state instead of blindly re-issuing non-idempotent calls."""

    __tablename__ = "operation_ledger"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("migration_job.id", ondelete="CASCADE"), index=True
    )
    op: Mapped[str] = mapped_column(String)  # create_playlist | add_track
    intent: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_target_id: Mapped[str | None] = mapped_column(String, nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String, default="intended")  # intended | done | ambiguous
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# Organizer idempotency is stored on these rows. OperationLedger remains migration-only.
class OrganizerJob(Base):
    __tablename__ = "organizer_job"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    provider: Mapped[str] = mapped_column(String, index=True)
    account_id: Mapped[str] = mapped_column(String, index=True)
    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    items: Mapped[list[OrganizerItem]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class OrganizerItem(Base):
    __tablename__ = "organizer_item"
    __table_args__ = (UniqueConstraint("job_id", "playlist_id", "action"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("organizer_job.id", ondelete="CASCADE"), index=True
    )
    playlist_id: Mapped[str] = mapped_column(String, index=True)
    playlist_name: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    destructive: Mapped[bool] = mapped_column(Boolean, default=False)
    ownership: Mapped[str] = mapped_column(String, default="unknown")
    collaborative: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    retryable: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    job: Mapped[OrganizerJob] = relationship(back_populates="items")


# --------------------------------------------------------------------------- #
# Scheduled playlist synchronization (private)
# --------------------------------------------------------------------------- #
class SyncRule(Base):
    __tablename__ = "sync_rule"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "source_provider",
            "source_account_id",
            "source_playlist_id",
            "target_provider",
            "target_account_id",
            "target_playlist_id",
            name="uq_sync_rule_endpoint_pair",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    source_provider: Mapped[str] = mapped_column(String)
    source_account_id: Mapped[str] = mapped_column(String)
    source_playlist_id: Mapped[str] = mapped_column(String)
    source_playlist_name: Mapped[str] = mapped_column(String)
    target_provider: Mapped[str] = mapped_column(String)
    target_account_id: Mapped[str] = mapped_column(String)
    target_playlist_id: Mapped[str] = mapped_column(String)
    target_playlist_name: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String, default="add_only")
    cadence_minutes: Mapped[int] = mapped_column(Integer, default=60)
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    status: Mapped[str] = mapped_column(String, default="idle", index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    last_added: Mapped[int] = mapped_column(Integer, default=0)
    last_removed: Mapped[int] = mapped_column(Integer, default=0)
    last_reordered: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    runs: Mapped[list[SyncRun]] = relationship(
        back_populates="rule", cascade="all, delete-orphan"
    )
    checkpoint: Mapped[SyncCheckpoint | None] = relationship(
        back_populates="rule", cascade="all, delete-orphan", uselist=False
    )


class SyncRun(Base):
    __tablename__ = "sync_run"
    __table_args__ = (
        Index(
            "uq_sync_run_active_rule",
            "rule_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    rule_id: Mapped[str] = mapped_column(
        ForeignKey("sync_rule.id", ondelete="CASCADE"), index=True
    )
    trigger: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    lease_token: Mapped[str] = mapped_column(String, default=_uuid)
    queue_job_id: Mapped[str] = mapped_column(String, unique=True)
    source_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    target_before: Mapped[dict] = mapped_column(JSON, default=dict)
    target_after: Mapped[dict] = mapped_column(JSON, default=dict)
    added: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    reordered: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    rule: Mapped[SyncRule] = relationship(back_populates="runs")
    migration_job: Mapped[MigrationJob | None] = relationship(
        back_populates="sync_run", uselist=False
    )


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoint"

    rule_id: Mapped[str] = mapped_column(
        ForeignKey("sync_rule.id", ondelete="CASCADE"), primary_key=True
    )
    source_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    target_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    mappings: Mapped[dict] = mapped_column(JSON, default=dict)
    unresolved: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    rule: Mapped[SyncRule] = relationship(back_populates="checkpoint")


# --------------------------------------------------------------------------- #
# Evidence graph (shareable, PII-free)
# --------------------------------------------------------------------------- #
class TrackIdentity(Base):
    __tablename__ = "track_identity"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    isrc: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    upc: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    artist: Mapped[str] = mapped_column(String)
    album: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    edges: Mapped[list[TrackEdge]] = relationship(
        back_populates="identity", cascade="all, delete-orphan"
    )


class TrackEdge(Base):
    """A candidate link between an identity and a provider track. Not truth —
    weighted by confidence/evidence and (optionally) user verification."""

    __tablename__ = "track_edge"
    __table_args__ = (UniqueConstraint("provider", "provider_track_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    identity_id: Mapped[str] = mapped_column(ForeignKey("track_identity.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String, index=True)
    provider_track_id: Mapped[str] = mapped_column(String)
    provider_uri: Mapped[str] = mapped_column(String)
    market: Mapped[str | None] = mapped_column(String, nullable=True)
    explicit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String)  # isrc_exact | fuzzy | user_confirmed
    verified_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    identity: Mapped[TrackIdentity] = relationship(back_populates="edges")
