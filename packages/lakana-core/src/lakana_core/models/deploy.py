"""Deploy runs, immutable project versions and the per-environment pointer."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime, utcnow


class DeployKind(enum.StrEnum):
    """What a deploy run does."""

    DEPLOY = "deploy"
    DRY_RUN = "dry_run"
    ROLLBACK = "rollback"


class DeployStatus(enum.StrEnum):
    """The lifecycle of one deploy run."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobVersion(Base):
    """An immutable, validated snapshot of one commit for one environment.

    Users call it a project version. The class and the ``job_versions`` table keep their first
    name, from before "job" came to mean a named entry of ``lakana.yaml``, because renaming a
    table would need a migration for no gain.

    Never updated. Redeploying a SHA that already has a row for the
    environment reuses it and only moves the pointer.
    """

    __tablename__ = "job_versions"
    __table_args__ = (UniqueConstraint("repository_id", "commit_sha", "environment_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    commit_sha: Mapped[str] = mapped_column(String(40))
    manifest_json: Mapped[dict] = mapped_column(JSON)
    code_uri: Mapped[str] = mapped_column(String(1024))
    code_sha256: Mapped[str] = mapped_column(String(64))
    code_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    """The size of the code archive; `None` for a version deployed before sizes were recorded."""
    deployed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DeployRun(Base):
    """One queued deploy, dry run or rollback and its outcome.

    The queue item and the result in one row, like `QueryRun`. Never
    deleted except with its environment or repository.
    """

    __tablename__ = "deploy_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[DeployKind] = mapped_column(SAEnum(DeployKind, native_enum=False, length=8))
    requested_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolved_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rollback_to_job_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_versions.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[DeployStatus] = mapped_column(
        SAEnum(DeployStatus, native_enum=False, length=9), index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_withheld_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    """The lines of `error_message` that tell of a reference target, each with the line a
    reader who may not read the target is shown instead (`projects.issues.withheld_lines`);
    `None` for an error without such lines and for one written before the column existed."""
    diff_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    checks_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    """The checks a deploy or dry run found, table to ``[{"check", "on_failure"}]``;
    `None` for a run without checks and for a run written before the column existed."""
    warnings_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    """What the deploy leaves for its owner to act on and only the deploy could tell, as
    sentences: a waiting job start it dropped, a scheduled job with no run principal."""
    job_starts_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    """The jobs with ``on_deploy: true`` that a successful deploy starts, one entry per job:
    ``job``, ``outcome`` (``pending`` until the worker started it) and what came of the start
    (`lakana_core.projects.deploy_jobs`). `None` for a run that starts no job: a dry run, a
    rollback, a failed deploy, a deploy that left the project version as it was, and a run
    written before the column existed."""
    job_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """The worker that claimed this run; `None` for a run that is queued or was claimed
    by a release before the column existed. No foreign key: the worker's row goes, the run's
    history stays."""
    ci_token_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    """The CI token (`CiToken`) that submitted this run; `None` for a run submitted with any
    other credential. No foreign key, like `QueryRun.task_token_id`: a missing token row must
    not turn the run into one that no CI token submitted, so the worker refuses a run whose
    token it cannot find."""


class EnvironmentDeployment(Base):
    """The one mutable pointer: which project version of a repository is current there."""

    __tablename__ = "environment_deployments"

    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), primary_key=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    current_job_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_versions.id", ondelete="CASCADE")
    )
    run_as_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    """The service account runs in this environment write as; survives deploys."""
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
