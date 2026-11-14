"""The artifact store's rows: versions, open uploads and stored files."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class ArtifactVersion(Base):
    """One immutable, numbered version of an artifact.

    A version row is never deleted while its artifact exists: deleting a
    version sets `deleted_at` (and `deleted_by`) and keeps the row, so version
    numbers are never reused and lineage still resolves. `manifest_json` is
    the canonical manifest and `manifest_sha256` its digest. `task_run_id`
    and `claim` name the task attempt that committed it, when one did.
    """

    __tablename__ = "artifact_versions"
    __table_args__ = (UniqueConstraint("artifact_id", "version"),)
    # Every foreign key to a principal, a task run or a job version is indexed, so a delete
    # there finds its rows without a scan (migration 0079).

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tables.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    manifest_json: Mapped[dict] = mapped_column(JSON)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    file_count: Mapped[int] = mapped_column(Integer)
    is_directory: Mapped[bool] = mapped_column(Boolean)
    task_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    claim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True, index=True
    )


class ArtifactUpload(Base):
    """An open, three-step upload of one new version.

    While open it records the manifest the client announced, who announced it and,
    for a task, which attempt. The commit that turns it into a version records the
    version's number in `committed_version` and leaves the row, so a repeated
    commit (an answer that was lost) returns the same version; the
    sweep, or the next `begin_upload`, removes it once `expires_at` has passed.
    While it is open (`committed_version` is `None`, not expired) it counts toward
    the artifact's cap and the files its manifest names are protected from
    deletion; once committed it protects nothing by itself, its version does.

    A committed row's `manifest_json` is ``{}`` (the version holds the manifest) and its
    `expires_at` is one upload lifetime after the commit (the time a commit can be repeated);
    nothing may parse the manifest of a committed row.
    """

    __tablename__ = "artifact_uploads"
    # `principal_id` is served by this index too (its first column).
    __table_args__ = (
        Index("ix_artifact_uploads_principal_id_expires_at", "principal_id", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tables.id", ondelete="CASCADE"), index=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id", ondelete="CASCADE"))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    manifest_json: Mapped[dict] = mapped_column(JSON)
    task_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    claim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    committed_version: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ArtifactBlob(Base):
    """A file stored for one artifact.

    A row exists exactly while the file is stored at
    ``artifacts/<artifact id>/blobs/<sha256>``; the primary key is the pair, so
    one file shared by several versions has one row.
    """

    __tablename__ = "artifact_blobs"

    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tables.id", ondelete="CASCADE"), primary_key=True
    )
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
