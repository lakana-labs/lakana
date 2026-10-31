"""Runs of a deployed project version, their task runs, attempts and the scheduler lease."""

from __future__ import annotations

import enum
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
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime, utcnow

SCHEDULER_LEASE_ID = 1
"""The primary key of the one `scheduler_lease` row."""

_ACTIVE_WHERE = text("status IN ('QUEUED', 'RUNNING')")
"""The partial index predicate; the database stores enum member names."""


class RunKind(enum.StrEnum):
    """What a run covers."""

    FULL = "full"
    SELECTION = "selection"
    RESUME = "resume"


class RunTrigger(enum.StrEnum):
    """What started a run of a job; an ad-hoc `lakana run` has none.

    ``deploy`` is a start that a deploy of the project made for a job with ``on_deploy: true``,
    as the principal that deployed.
    """

    SCHEDULE = "schedule"
    API = "api"
    MANUAL = "manual"
    DEPLOY = "deploy"


class RunStatus(enum.StrEnum):
    """The lifecycle of one run."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_RUN_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})
"""A run in one of these blocks another start for the same deployment."""


OWN_TIP_NO_SNAPSHOT = 0
"""`TaskRun.own_snapshot_id` of a task run whose own write made no snapshot.

Iceberg snapshot ids are positive, so 0 is free; null means that the task run wrote nothing.
"""


class TaskStatus(enum.StrEnum):
    """The lifecycle of one task run."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UPSTREAM_FAILED = "upstream_failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.UPSTREAM_FAILED, TaskStatus.CANCELLED}
)
"""A task run in one of these never changes again."""


class AttemptOutcome(enum.StrEnum):
    """How one attempt of a task run ended."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    LOST = "lost"
    RELEASED = "released"
    CANCELLED = "cancelled"


class Run(Base):
    """One run of a deployed project version: the whole DAG, a selection, or a resume.

    At most one queued or running run exists per (environment, repository):
    `ix_runs_one_active` is a partial unique index, so two concurrent starts
    leave exactly one row and the loser sees `IntegrityError`.
    """

    __tablename__ = "runs"
    __table_args__ = (
        Index(
            "ix_runs_one_active",
            "environment_id",
            "repository_id",
            unique=True,
            sqlite_where=_ACTIVE_WHERE,
            postgresql_where=_ACTIVE_WHERE,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    job_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_versions.id", ondelete="CASCADE")
    )
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    run_as_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[RunKind] = mapped_column(SAEnum(RunKind, native_enum=False, length=9))
    resumed_from_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    selection_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[RunStatus] = mapped_column(
        SAEnum(RunStatus, native_enum=False, length=9), index=True
    )
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    job: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    """The job the run belongs to; null for an ad-hoc `lakana run`."""
    trigger: Mapped[RunTrigger | None] = mapped_column(
        SAEnum(RunTrigger, native_enum=False, length=8), nullable=True
    )
    """What started it: the schedule, an API call by a service account, or a person by hand.
    Null for an ad-hoc `lakana run`."""
    scheduled_for: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    """For a scheduled run, the time the schedule named, not the time it started."""
    missed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """How many earlier scheduled times this run stands in for: catch-up is off, so after
    downtime or a wait one run covers them all."""


class TaskRun(Base):
    """One asset of one run.

    `upstream_json` lists the selected upstream assets inside the same run;
    an upstream outside the selection is not listed.
    """

    __tablename__ = "task_runs"
    __table_args__ = (UniqueConstraint("run_id", "asset"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    asset: Mapped[str] = mapped_column(String(1024))
    status: Mapped[TaskStatus] = mapped_column(
        SAEnum(TaskStatus, native_enum=False, length=15), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    claim_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """Times the task was claimed. Unlike `attempt`, which an uncounted retry (a
    release, the first conflict) takes back, it only grows: the lease guards compare
    it, so a zombie of an earlier claim never matches the claim that replaced it."""
    kind: Mapped[str] = mapped_column(String(16), default="asset", server_default="asset")
    """What the task is: ``asset`` (a table to build) or ``source_check`` (the checks of a
    table the project does not build)."""
    has_checks: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    """Whether the asset has checks: only then is the task's write stamped *written*."""
    setting_back: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    """Whether the task is only to set its table back before it ends. The scheduler sets it when
    it reclaims a task whose worker was lost after it may have written, and nothing clears it:
    the task ends after the set-back, and a resumed run gets new task rows."""
    set_back_claims: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """Claims taken while `setting_back` was set; the scheduler stops putting the task back for
    another one at a limit, so a set-back that kills its worker every time cannot loop."""
    check_claims: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """Claims taken while the task already awaited its checks: claims whose only work is the check
    phase. The scheduler sends the task to set its table back at a limit
    (`lakana_core.runs.checks.MAX_CHECK_CLAIMS`), so checks that never finish cannot loop."""
    written_table_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    written_snapshot_before: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    written_snapshot_after: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    written_publishes: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """What the task's write did, recorded in the write's own commit for a SQL task and after
    the process for a Python task. `written_table_id` is null until then. No foreign
    key: a dropped table must not take the record with it."""
    baseline_snapshot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    baseline_publishes: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """What the table of a Python asset with checks was before the task run's FIRST process
    started: its current snapshot, and whether a write would publish it (it did not
    exist, or was not published). Recorded once; every later attempt rolls back to it, because
    an earlier attempt of the same task run may have written already. `baseline_publishes` is
    null until then. Never recorded for a SQL task: its stamp lands with its commit."""
    own_snapshot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """The tip of this task run's own chain of writes: the snapshot its last write made
    that started from the baseline or from the tip before. Set in the write's own commit by the
    write guard, only for a task run that has a baseline. Null: it wrote nothing of its own.
    `OWN_TIP_NO_SNAPSHOT`: it wrote, and the commit made no snapshot (a table created without
    rows). A rollback names this snapshot, never "whatever the table is at": a commit by
    anyone else, before, between or after the task run's writes, is then not the run's to undo."""
    max_attempts: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    retry_delay_seconds: Mapped[int] = mapped_column(Integer)
    ready_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    """When the task became ready; the claim orders FIFO by it."""
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    query_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("query_runs.id", ondelete="SET NULL"), nullable=True
    )
    upstream_json: Mapped[list] = mapped_column(JSON, default=list)
    conflict_retried: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """A `lakana_core.runs.failures.FailureCode` value, stored with `error_message`: why the task
    failed. Null for a task that did not fail, and for a row from before the codes existed
    (shown as ``unknown``)."""
    check_message_state: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    """What `error_message` was built from besides the check results, when the check phase built
    it (`lakana_core.runs.check_message.CheckMessageState`); null for any other message."""
    schema_change_json: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    """What the task's writes did to its table's columns, when one removed or replaced a column
    (`lakana_core.policy.schema_change.merge_schema_change`): ``{"tables": [...], "policies":
    [...]}``, the row filters and column masks that use such a column, each once. Null when no
    write removed or replaced a column. Recorded in the commit of the write, and kept when a later
    attempt changes nothing."""
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class TaskAttempt(Base):
    """One attempt of a task run: the structured log."""

    __tablename__ = "task_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer)
    worker: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    outcome: Mapped[AttemptOutcome | None] = mapped_column(
        SAEnum(AttemptOutcome, native_enum=False, length=9), nullable=True
    )
    rows_written: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """Why this attempt did not succeed (a `FailureCode` value); null otherwise and for old rows."""
    check_message_state: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    """The `CheckMessageState` of this attempt's `error_message`, as on the task run."""


class SchedulerLease(Base):
    """The one row naming the current scheduler holder, for the API."""

    __tablename__ = "scheduler_lease"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    holder: Mapped[str | None] = mapped_column(String(255), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
