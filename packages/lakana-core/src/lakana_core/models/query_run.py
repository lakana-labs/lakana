"""The query_run model."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, Index, String, Text, Uuid, false
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, TimestampMixin, UTCDateTime


class QueryRunStatus(enum.StrEnum):
    """The lifecycle state of one submitted query.

    `CANCELLED`: ended by a cancel request, before or while it ran.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class QueryErrorCause(enum.StrEnum):
    """Why a query run failed or was cancelled, in a form a program can branch on.

    Stored as the member's value in `QueryRun.error_cause` and returned by the REST API as
    ``error_cause``. A client must treat a value it does not know like `INTERNAL`, and a
    missing one (a run recorded before the field existed) as unknown.
    """

    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    RESULT_TOO_LARGE = "result_too_large"
    OUT_OF_MEMORY = "out_of_memory"
    INVALID_QUERY = "invalid_query"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERNAL = "internal"
    WORKER_LOST = "worker_lost"
    """The worker running the query was lost: it could not reach the database for a lease and
    stopped the query itself, or the scheduler ended the run."""


class WriteMode(enum.StrEnum):
    """What a write run does to its target.

    The database stores the member *names* (SQLAlchemy's default for a
    non-native enum); the lowercase *values* are the REST API's spelling. `HISTORY` is written
    only by a history asset's run (`write: history`).
    """

    CREATE = "create"
    REPLACE = "replace"
    APPEND = "append"
    MERGE = "merge"
    VIEW = "view"
    MAINTAIN = "maintain"
    HISTORY = "history"


class QueryRun(Base, TimestampMixin):
    """One submission to the SQL query API and its outcome.

    A row is inserted `QUEUED` by `QueryService.submit_query` and updated by the
    worker as it dequeues, runs, and finishes the query — never deleted, so it
    doubles as the query's audit and result-retrieval record.
    ``principal_id`` cascades to ``NULL`` when the submitting principal is
    deleted, rather than cascading the delete, so query history outlives its
    author the same way table and namespace ownership already does.
    """

    __tablename__ = "query_runs"
    __table_args__ = (
        Index("ix_query_runs_status_created_at", "status", "created_at"),
        Index("ix_query_runs_environment_id_finished_at", "environment_id", "finished_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )
    sql_text: Mapped[str] = mapped_column(Text)
    status: Mapped[QueryRunStatus] = mapped_column(SAEnum(QueryRunStatus, native_enum=False))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    """Set by `QueryService.cancel_query` on a running run; the worker reads it about twice a
    second while the query's process runs."""
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_cause: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """A `QueryErrorCause` value, set with `error_message` when the worker records a failure
    or a cancellation (`CANCELLED`, cause `cancelled`), and by the scheduler's sweep of a
    lost worker's run (`worker_lost`); `None` for a run that did not fail, one recorded
    before the column existed, and a task's write or query that the task's lost lease failed
    (`store._apply_task`)."""
    lineage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_location: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    task_token_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    """The task token that submitted this run, if any.

    Deliberately has no foreign key: a deleted token must stay detectable
    so the worker fails the run rather than treating it as a PAT-submitted
    run under an ``ON DELETE SET NULL``. ``None`` for a run submitted with
    a personal access token.
    """
    write_mode: Mapped[WriteMode | None] = mapped_column(
        SAEnum(WriteMode, native_enum=False, length=16), nullable=True
    )
    """`None` for a read query; set for a write, view or maintenance run."""
    target_name: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    """The target's dotted name as submitted — display only; a `create` has no id yet."""
    target_table_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    """The target's id once known. No foreign key, so a run's history survives a drop."""
    incremental_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    merge_key: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    """The columns a merge matches rows on: set by the worker for a SQL asset with
    `write: merge` (its `MERGE` runs and the `CREATE` of its first run, which is checked the same
    way); `None` for every other run."""
    history: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    """How a history asset's run writes (`HistorySpec.to_json`: key, valid_from, close_missing):
    set by the worker for a SQL asset with `write: history` (its `HISTORY` runs and the `CREATE`
    of its first run); `None` for every other run."""
    evolve_schema: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    watermark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """The type-tagged watermark an incremental append filtered on, e.g. ``long:42``."""
    rows_written: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    snapshot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    snapshot_before: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """The target's current snapshot when the write was staged.

    `None` for a target that did not exist or had no snapshot. Written with the evidence, in
    the commit's transaction."""
    schema_diff: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    maintenance: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    """``{"request": {...}, "report": {...}}`` for a maintenance run, else `None`."""
    task_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL", use_alter=True), nullable=True, index=True
    )
    """The task run this query executes for. Set by the worker for a SQL
    task's write run, and by `QueryService` from a bound task token's database binding;
    never from a request field. It is the run context the protected-environment lockdown
    checks. `use_alter` breaks the query_runs <-> task_runs cycle for
    `metadata.sorted_tables`/`create_all`; the migration already adds it as a separate
    step."""
    break_glass_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """A break-glass write's reason, re-checked and audited when the worker runs it."""
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """The worker that claimed this run; `None` for a run that is queued or was claimed
    by a release before the column existed. No foreign key: the worker's row goes, the run's
    history stays."""
    check_result_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    """The check result whose rows this query shows; `None` for every other run.

    Set only by `lakana_core.runs.check_rows.submit_check_rows`, never from a request field:
    the worker reads the one table the result names at the snapshot the check read, also
    when that table is not published. Deliberately has no foreign key: a result that was
    removed with its run must leave the id here, so that the worker fails the query with a
    message; an ``ON DELETE SET NULL`` would silently turn it into a query of the table as it
    is now.
    """
