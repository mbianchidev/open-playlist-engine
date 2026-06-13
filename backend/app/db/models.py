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
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
# Jobs & operation ledger (private)
# --------------------------------------------------------------------------- #
class MigrationJob(Base):
    __tablename__ = "migration_job"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    source_provider: Mapped[str] = mapped_column(String)
    target_provider: Mapped[str] = mapped_column(String)
    source_account_id: Mapped[str] = mapped_column(String)
    target_account_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list[JobItem]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class JobItem(Base):
    __tablename__ = "job_item"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("migration_job.id", ondelete="CASCADE"), index=True
    )
    source_playlist_id: Mapped[str] = mapped_column(String)
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String)
    artist: Mapped[str] = mapped_column(String)
    isrc: Mapped[str | None] = mapped_column(String, nullable=True)
    target_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # pending | matched | needs_review | written | skipped | failed
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)

    job: Mapped[MigrationJob] = relationship(back_populates="items")


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
