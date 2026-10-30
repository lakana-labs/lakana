"""The protected-environment write lockdown and its audited break-glass.

In an environment with `is_protected`, a catalog mutation the caller's grants
allow is still refused (audited `PROTECTED_WRITE_DENIED`) unless it runs inside
a running task run of that environment (the run context the worker sets) or
carries a break-glass reason from a holder of `MANAGE_ENVIRONMENT`. The guard
runs after the operation's own authorization: a caller without the grant gets
the ordinary denial (audited `AUTHORIZATION_DENIED`) and never learns whether
the environment is protected. Reads, governance and table maintenance never come here.

A context taken from a bound task token (the claim form) is additionally
held to the task's own asset in every environment. A task may write that asset,
or add a version to its own artifact; it may never delete a version or drop an
artifact.

A task's own asset has an output kind in its project version's manifest: a task that
produces a table may not add an artifact version, and one that produces an
artifact may not write a table (a missing or malformed entry counts as a table).

A write that carries a run context is verified in every environment, protected
or not: a superseded attempt (its task reclaimed, its run finished or
cancelled) never commits. A write without a run context in an unprotected
environment is not affected by that.

A write executed by a worker (a `PrincipalRef` with a `write_fence`) is also held to its query
run: it commits only while that run is still `RUNNING` under that worker, and the same
statement records what the commit wrote on the run.
"""

from __future__ import annotations

import enum
import unicodedata
import uuid
from dataclasses import dataclass
from typing import NoReturn

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from lakana_core.artifacts.manifest import shown
from lakana_core.audit.service import AuditService
from lakana_core.auth import (
    Action,
    AuthorizationService,
    PrincipalRef,
    RunContext,
    Securable,
    WriteFence,
)
from lakana_core.db import rowcount
from lakana_core.errors import PermissionDenied, ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    OWN_TIP_NO_SNAPSHOT,
    AuditEventType,
    Environment,
    JobVersion,
    QueryRun,
    QueryRunStatus,
    Run,
    RunStatus,
    TaskRun,
    TaskStatus,
)
from lakana_core.policy.schema_change import merge_schema_change
from lakana_core.projects.manifest import entry_output, python_entry

PROTECTED_MESSAGE = (
    "Environment '{environment}' is protected: only runs of the deployed project version may "
    "write. See `lakana run`."
)
SUPERSEDED_MESSAGE = "This task attempt was superseded: its run no longer holds this write."
WRITE_FENCE_MESSAGE = (
    "This query run was ended because its worker was lost; nothing was written. Run it again."
)
TABLE_TASK_MESSAGE = (
    "Task '{asset}' produces a table; it cannot add an artifact version. "
    'To store an artifact, declare the asset with output="artifact".'
)
ARTIFACT_TASK_MESSAGE = (
    "Task '{asset}' produces an artifact; it cannot write a table. "
    "Write the artifact's files into lakana.output_dir() instead."
)
OTHER_ASSET_MESSAGE = (
    "Task '{asset}' may not touch '{target}' ({operation}): a task only writes its own asset. "
    "Do this outside the task."
)
OWN_ASSET_MESSAGE = (
    "Task '{asset}' may not do '{operation}' to {subject}; "
    "a task may only materialize its own asset: for a table, {tables}; "
    "for an artifact, {artifacts}. Do this outside the task."
)
WORKER_VERSION_MESSAGE = (
    "Task '{asset}' adds artifact versions through its task token, not from the worker."
)
OUTPUT_KIND_REASON = "output_kind"
OTHER_ASSET_REASON = "other_asset"
OWN_ASSET_REASON = "own_asset_operation"
MIN_REASON_LENGTH = 10
MAX_REASON_LENGTH = 500
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
"""Control, format, surrogate, private-use and line/paragraph separators.

`Cn` (unassigned) is left out: whether a code point is assigned depends on
the Unicode tables of the Python build, so refusing it would make the same
reason valid on one worker and invalid on another.
"""


class WriteOperation(enum.StrEnum):
    """A guarded catalog mutation, as named in the audit detail."""

    SQL_WRITE = "sql_write"
    WRITE = "write"
    APPEND = "append"
    MERGE = "merge"
    COMMIT = "commit"
    CREATE_TABLE = "create_table"
    CREATE_EXTERNAL_TABLE = "create_external_table"
    CREATE_VIEW = "create_view"
    DROP_TABLE = "drop_table"
    RENAME_TABLE = "rename_table"
    CREATE_NAMESPACE = "create_namespace"
    DROP_NAMESPACE = "drop_namespace"
    UPDATE_NAMESPACE = "update_namespace"
    ARTIFACT_VERSION = "artifact_version"
    DELETE_ARTIFACT_VERSION = "delete_artifact_version"
    DROP_ARTIFACT = "drop_artifact"


_DESTRUCTIVE = frozenset({WriteOperation.DELETE_ARTIFACT_VERSION, WriteOperation.DROP_ARTIFACT})
"""Operations that each record their own break-glass event, with their own operation.

A table write passes several choke points of one request and is recorded once per target;
two deletions of different versions, or a deletion and a drop, are different acts.
"""

_MATERIALIZING = frozenset(
    {
        WriteOperation.SQL_WRITE,
        WriteOperation.WRITE,
        WriteOperation.APPEND,
        WriteOperation.MERGE,
        WriteOperation.COMMIT,
        WriteOperation.CREATE_VIEW,
        WriteOperation.ARTIFACT_VERSION,
    }
)
"""What a task may do to its own asset.

Deleting an artifact's version and dropping an artifact are deliberately absent.
"""

_TABLES_TEXT = ", ".join(
    kind.value.replace("_", " ")
    for kind in WriteOperation
    if kind in _MATERIALIZING and kind is not WriteOperation.ARTIFACT_VERSION
)
"""What a table-producing task may do to its asset, in enum order, for `OWN_ASSET_MESSAGE`."""

_ARTIFACTS_TEXT = WriteOperation.ARTIFACT_VERSION.value.replace("_", " ")
"""What an artifact-producing task may do to its asset, for `OWN_ASSET_MESSAGE`."""

_NAMESPACE_OPERATIONS = frozenset(
    {
        WriteOperation.CREATE_NAMESPACE,
        WriteOperation.DROP_NAMESPACE,
        WriteOperation.UPDATE_NAMESPACE,
    }
)
"""Operations whose target is a namespace, which is not the asset even if it has its name."""

Target = tuple[str, ...]
"""A guarded table or namespace as its parts, outermost first.

Compared structurally, never as a dotted display string: a legacy namespace
component containing a dot (`["bronze", "x.y"]`) would otherwise collide
with the deeper path `["bronze", "x", "y"]`.
"""


def table_target(ref: TableRef) -> Target:
    """Return a table's guard target.

    Parameters
    ----------
    ref
        The table.

    Returns
    -------
    Target
        Its namespace parts followed by its name.
    """
    return (*ref.namespace.parts, ref.name)


def _dotted(target: Target) -> str:
    """Render a target for the audit log.

    Parameters
    ----------
    target
        The target.

    Returns
    -------
    str
        Its dotted display form.
    """
    return ".".join(target)


def validate_break_glass_reason(reason: str) -> str:
    """Check a break-glass reason.

    Parameters
    ----------
    reason
        The reason as given.

    Returns
    -------
    str
        The reason without surrounding whitespace.

    Raises
    ------
    ValidationError
        Unless it is 10 to 500 characters after stripping, or if it contains
        a control, format, surrogate or private-use character, or a line or
        paragraph separator (U+2028, U+2029): the reason is stored in the
        audit log, where a newline would forge a line and an escape sequence
        would rewrite a terminal.
    """
    stripped = reason.strip()
    if not MIN_REASON_LENGTH <= len(stripped) <= MAX_REASON_LENGTH:
        raise ValidationError(
            f"A break-glass reason must be {MIN_REASON_LENGTH} to {MAX_REASON_LENGTH} characters."
        )
    if any(unicodedata.category(char) in _REFUSED_CATEGORIES for char in stripped):
        raise ValidationError(
            "A break-glass reason must be one line of text without control or invisible "
            "formatting characters."
        )
    return stripped


@dataclass(frozen=True)
class _KindRefusal:
    """A run context's write refused for a reason of its own, not as a stale attempt.

    Parameters
    ----------
    message
        What the caller reads.
    reason
        What the audit detail records under ``reason``.
    """

    message: str
    reason: str = OUTPUT_KIND_REASON


@dataclass(frozen=True)
class _RunWrite:
    """A write the guard let through on its run context, to verify again before commit.

    Parameters
    ----------
    principal
        The acting principal.
    context
        The run context it relied on.
    environment_id
        The environment's id.
    environment
        Its name.
    operation
        What is being done.
    target
        The table or namespace, as parts.
    protected
        Whether the environment was protected at `check`.
    rollback
        Whether the write was declared the internal rollback of a set-back at `check`.
    """

    principal: PrincipalRef
    context: RunContext
    environment_id: uuid.UUID
    environment: str
    operation: WriteOperation
    target: Target
    protected: bool
    rollback: bool = False


class WriteFenceRefused(PermissionDenied):
    """A worker's table write was refused because its query run is no longer its running run.

    Always carries `WRITE_FENCE_MESSAGE`. A subclass of `PermissionDenied`, so that nothing
    that handles a refusal changes, and so that the worker can tell this refusal from a
    grant's: a task whose write was ended with its worker is retried, and the run's cause is
    `worker_lost`, not `permission_denied`.
    """

    def __init__(self) -> None:
        super().__init__(WRITE_FENCE_MESSAGE)


@dataclass(frozen=True)
class _Evidence:
    """What a write is about to commit, to be recorded on its run with the commit.

    Parameters
    ----------
    key
        The environment and target the write is for.
    rows
        Rows written.
    snapshot_id
        The new snapshot, or `None` when the write made none.
    table_id
        The table or view written.
    fence
        The fence in effect when it was noted: a note is used only for that run.
    snapshot_before
        The table's current snapshot when the write was staged, or `None`.
    publishes
        Whether the write created the table or found it not published.
    policy_warnings
        The row filters and column masks the write leaves out of date, as stored
        (`lakana_core.policy.schema_change.BrokenPolicy.to_json`).
    changed_table
        The table whose columns the write removed or replaced, or `None`.
    """

    key: tuple[str, Target]
    rows: int
    snapshot_id: int | None
    table_id: uuid.UUID
    fence: WriteFence | None
    snapshot_before: int | None
    publishes: bool
    policy_warnings: tuple[dict[str, str], ...] = ()
    changed_table: str | None = None


class ProtectedWriteGuard:
    """Refuse writes into a protected environment outside a run or a break-glass.

    A write carrying a run context is also refused in an unprotected
    environment once its attempt no longer holds the run (with
    `SUPERSEDED_MESSAGE`), so a stalled worker cannot commit over the
    attempt that replaced it.

    One guard lives on each `CatalogService` and `QueryService`, so it
    records `BREAK_GLASS_WRITE` once per (environment, target) for the
    lifetime of that service — one request, or one worker write — even when
    a write passes several choke points (`append` → `commit_table`). The two
    destructive artifact operations are the exception: each one that commits
    records its own event with its own operation, however often the same
    target was deleted from before, and their pending record is dropped by
    the next `check`, whatever it is.

    The service calls `check` right after the operation's own authorization,
    so a caller without the grant gets the ordinary denial and never learns
    whether the environment is protected.

    A break-glass write is only *pending* after `check`: later checks (a
    lineage source's read grant, a failure mid-write) can still refuse it,
    and a denial commits the session, which must not carry a break-glass row
    for a write that never happened. The service calls `record_pending`
    right before the commit that lands the write.

    A write by a worker also carries a fence (`PrincipalRef.write_fence`): at that same point
    the guard refuses unless the worker's query run is still `RUNNING` under that worker, and
    writes the evidence of the commit (`note_result`) onto the run in the same statement and
    the same transaction. A run ended meanwhile (its worker was frozen, the sweep ended it)
    therefore never has a late write land.

    The same statement records `snapshot_before`, and for a SQL task whose asset has checks
    (`TaskRun.has_checks`) `record_pending` also stamps the task run *written* (table,
    snapshot before and after, whether the write publishes the table) in that transaction
    , so the record and the pointer swap land together or not at all.

    Parameters
    ----------
    session
        Database session.
    authz
        Authorization, for the break-glass `MANAGE_ENVIRONMENT` check.
    """

    def __init__(self, session: Session, authz: AuthorizationService) -> None:
        self._session = session
        self._authz = authz
        self._recorded: set[tuple[str, Target]] = set()
        self._pending: tuple[tuple[str, Target], uuid.UUID, dict[str, str]] | None = None
        self._relied_on: _RunWrite | None = None
        self._fence: WriteFence | None = None
        self._evidence: _Evidence | None = None

    def note_result(
        self,
        environment: str,
        target: Target,
        *,
        rows: int,
        snapshot_id: int | None,
        table_id: uuid.UUID,
        snapshot_before: int | None = None,
        publishes: bool = False,
        policy_warnings: tuple[dict[str, str], ...] = (),
        changed_table: str | None = None,
    ) -> None:
        """Remember what the write of `target` is about to commit, for `record_pending`.

        Nothing is written here: `record_pending` records it on the fenced run in the statement
        that checks the fence, so the run's row is locked only for the end of the transaction
        and not across the slow work (object storage) between the two calls. Dropped by the
        next `check` of another target.

        Parameters
        ----------
        environment
            The environment written to.
        target
            The table or view, as parts.
        rows
            Rows written (0 for a view or an empty append).
        snapshot_id
            The new snapshot, or `None` when the write made none.
        table_id
            The table or view written.
        snapshot_before
            The table's current snapshot when the write was staged; `None` for a table that
            did not exist or had no snapshot, and for a view.
        publishes
            Whether the write created the table or found it not published.
        policy_warnings
            The row filters and column masks the write leaves out of date, as stored; recorded on
            the task run of a write in a run (`_note_schema_change`).
        changed_table
            The table whose columns the write removed or replaced, or `None`; recorded the same
            way, with or without a policy.
        """
        self._evidence = _Evidence(
            (environment, target),
            rows,
            snapshot_id,
            table_id,
            self._fence,
            snapshot_before,
            publishes,
            policy_warnings,
            changed_table,
        )

    def note_own_commit(
        self,
        p: PrincipalRef,
        environment: str,
        target: Target,
        *,
        rows: int,
        snapshot_id: int,
        table_id: uuid.UUID,
        snapshot_before: int | None,
        publishes: bool,
    ) -> None:
        """Note a commit that a Python task made itself, unless a write noted one already.

        `append` and an Iceberg REST commit reach `CatalogService.commit_table` without a note,
        so a task's own chain of writes would not move for them. Noted only for the claim form
        of a run context (a bound task token) and never under a write fence: a note under a
        fence makes `_hold_fence` record `rows_written`, which a reclaim reads as "committed".
        A write that noted its result for this target (`write_batches`) keeps its own note.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment written to.
        target
            The table, as parts.
        rows
            Rows the commit added.
        snapshot_id
            The snapshot the commit added and made the head.
        table_id
            The table.
        snapshot_before
            The table's current snapshot before the commit.
        publishes
            Whether the table was not published before the commit.
        """
        context = p.run_context
        if p.write_fence is not None or context is None or context.claim is None:
            return
        if self._evidence is not None and self._evidence.key == (environment, target):
            return
        self.note_result(
            environment,
            target,
            rows=rows,
            snapshot_id=snapshot_id,
            table_id=table_id,
            snapshot_before=snapshot_before,
            publishes=publishes,
        )

    def forget_result(self) -> None:
        """Drop a noted result that no commit has taken.

        A write that noted its result and then failed before `record_pending` leaves the note
        behind, and a later commit of the same target on this guard would record it as its own
        evidence. The rollback calls this first.
        """
        self._evidence = None

    def check(
        self,
        p: PrincipalRef,
        environment: str,
        operation: WriteOperation,
        target: Target,
        *,
        record_break_glass: bool = True,
        rollback: bool = False,
    ) -> None:
        """Allow the write or refuse it.

        Parameters
        ----------
        p
            The acting principal, with its run context and break-glass reason.
        environment
            The environment written to.
        operation
            What is being done.
        target
            The table or namespace, as parts (`table_target` for a table,
            `NamespacePath.parts` for a namespace).
        record_break_glass
            Record `BREAK_GLASS_WRITE`; `False` at submission of a queued
            write, which is recorded when the worker executes it.
        rollback
            Whether this is the platform's own rollback commit of a set-back, which is the only
            write a task set to set its table back may do under a cancel request. Only the
            catalog service's rollback paths say so; no request field reaches it.

        Raises
        ------
        PermissionDenied
            With `PROTECTED_MESSAGE` (audited `PROTECTED_WRITE_DENIED`), with
            `SUPERSEDED_MESSAGE` for a stale run context in an unprotected
            environment (audited the same way), `OTHER_ASSET_MESSAGE` for a live
            task's write past its own asset there (audited with
            ``reason: other_asset``, or `OWN_ASSET_MESSAGE` with ``reason: own_asset_operation``
            for an operation it may not do to its own asset), `TABLE_TASK_MESSAGE`,
            `WORKER_VERSION_MESSAGE` or `ARTIFACT_TASK_MESSAGE` for a task
            writing its own asset in the wrong output kind (audited with
            ``reason: output_kind``), or the authorization denial of a
            break-glass without `MANAGE_ENVIRONMENT`.
        ValidationError
            For a malformed break-glass reason.
        """
        self._fence = p.write_fence
        if self._evidence is not None and self._evidence.key != (environment, target):
            self._evidence = None
        if self._pending is not None:
            pending_key, _, pending_detail = self._pending
            # A pending record of a deletion never outlives the call that made it, and a
            # deletion never inherits one: only table writes share a record across choke points.
            destructive = operation in _DESTRUCTIVE or any(
                pending_detail["operation"] == kind.value for kind in _DESTRUCTIVE
            )
            if pending_key != (environment, target) or destructive:
                self._pending = None
        self._relied_on = None
        env = self._session.execute(
            select(Environment).where(Environment.name == environment)
        ).scalar_one_or_none()
        if env is None:
            return
        protected = env.is_protected
        if p.run_context is not None:
            verdict = self._run_allows(
                p.run_context,
                p.id,
                env.id,
                operation,
                target,
                own_asset_only=protected,
                rollback=rollback,
            )
            if verdict is True:
                self._relied_on = _RunWrite(
                    p, p.run_context, env.id, environment, operation, target, protected, rollback
                )
                return
            self._deny(
                p,
                environment,
                operation,
                target,
                in_run=True,
                protected=protected,
                refusal=None if isinstance(verdict, bool) else verdict,
            )
        if not protected:
            return
        if p.break_glass_reason is not None:
            reason = validate_break_glass_reason(p.break_glass_reason)
            self._authz.authorize(
                p, Action.MANAGE_ENVIRONMENT, Securable.environment_scope(environment)
            )
            key = (environment, target)
            fresh = self._pending is None or self._pending[0] != key
            once = operation not in _DESTRUCTIVE
            if record_break_glass and (key not in self._recorded or not once) and fresh:
                self._pending = (
                    key,
                    p.id,
                    {"reason": reason, "operation": operation.value, "target": _dotted(target)},
                )
            return
        self._deny(p, environment, operation, target, in_run=False, protected=True)

    def record_pending(self) -> None:
        """Add the pending `BREAK_GLASS_WRITE` row to the caller's transaction.

        Called by the service right before the commit that lands the write,
        after the operation's own authorization has passed.

        A write that passed on its run context is verified again here, now
        holding row locks on its task run and run: a scheduler reclaim or a
        cancellation that landed after `check` cannot be overtaken, because it
        either committed first (and is seen) or waits for this transaction.
        This holds in unprotected environments too. Otherwise a no-op when no
        break-glass write is pending.

        A write that carries a fence is then held to its query run (`_hold_fence`), after the
        run context's verification: the task run and the run are locked before the query
        run, the order in which `store._apply_task` updates them. A fenced SQL write of a task
        with checks then stamps the task run *written* (`_stamp_written`) in the same
        transaction; a note made under another fence, a refused fence and a claim-form context
        stamp nothing. A write under a claim-form context (a Python task's own write) instead
        advances the tip of its task run's own chain of writes (`_advance_own_tip`).

        Raises
        ------
        PermissionDenied
            If the run context no longer covers the write, with the same texts
            as `check` (audited, the caller's uncommitted state rolled back
            first).
        WriteFenceRefused
            With `WRITE_FENCE_MESSAGE` if the fenced query run is no longer `RUNNING` under
            the fence's worker (not audited: whoever ended the run did; the caller's
            uncommitted state is rolled back first).
        """
        relied_on, self._relied_on = self._relied_on, None
        fence, self._fence = self._fence, None
        evidence, self._evidence = self._evidence, None
        verdict = (
            True
            if relied_on is None
            else self._run_allows(
                relied_on.context,
                relied_on.principal.id,
                relied_on.environment_id,
                relied_on.operation,
                relied_on.target,
                own_asset_only=relied_on.protected,
                lock=True,
                rollback=relied_on.rollback,
            )
        )
        if relied_on is not None and verdict is not True:
            self._deny(
                relied_on.principal,
                relied_on.environment,
                relied_on.operation,
                relied_on.target,
                in_run=True,
                protected=relied_on.protected,
                refusal=None if isinstance(verdict, bool) else verdict,
            )
        noted = evidence if evidence is not None and evidence.fence == fence else None
        if fence is not None:
            # A note made under another run's fence is not this run's evidence.
            self._hold_fence(fence, noted)
            if (
                noted is not None
                and relied_on is not None
                and relied_on.context.query_run_id is not None
            ):
                self._stamp_written(relied_on.context.task_run_id, noted)
        if noted is not None and relied_on is not None and relied_on.context.claim is not None:
            self._advance_own_tip(relied_on.context.task_run_id, noted)
        if noted is not None and relied_on is not None and noted.changed_table is not None:
            self._note_schema_change(
                relied_on.context.task_run_id, noted.changed_table, noted.policy_warnings
            )
        if self._pending is None:
            return
        key, principal_id, detail = self._pending
        self._pending = None
        if detail["operation"] not in {kind.value for kind in _DESTRUCTIVE}:
            self._recorded.add(key)
        AuditService(self._session).record(
            principal_id,
            AuditEventType.BREAK_GLASS_WRITE,
            securable=Securable.environment_scope(key[0]),
            detail=detail,
        )

    def _note_schema_change(
        self, task_run_id: uuid.UUID, table: str, warnings: tuple[dict[str, str], ...]
    ) -> None:
        """Add a write's schema change to its task run, in the write's own transaction.

        Both forms of a run context get here (a SQL task's write run and a Python task's own
        write), after the context held again, so the record lands with the schema change or not
        at all. It is recorded for every write that removed or replaced a column, with or without
        a policy (`merge_schema_change` keeps each entry once).

        Parameters
        ----------
        task_run_id
            The task run whose write this is.
        table
            The table whose columns changed.
        warnings
            The policies it left out of date, as stored.
        """
        held = self._session.scalar(
            select(TaskRun.schema_change_json).where(TaskRun.id == task_run_id)
        )
        self._session.execute(
            update(TaskRun)
            .where(TaskRun.id == task_run_id)
            .values(schema_change_json=merge_schema_change(held, table, warnings))
            .execution_options(synchronize_session=False)
        )

    def _stamp_written(self, task_run_id: uuid.UUID, evidence: _Evidence) -> None:
        """Record on a SQL task with checks what its write is about to commit (*written*).

        Runs in the write's own transaction, after the fence held: the stamp lands with the
        pointer swap or not at all. It matches no row for a task without checks, so such a
        task's commit takes no new lock; for one with checks it updates the task run's row
        that this transaction already holds `FOR SHARE` (`_run_allows(lock=True)`), which
        PostgreSQL grants to the only locker without waiting. A claim-form context (a Python
        task's own write through the REST API) is never stamped: the worker records that
        after the process has ended.

        Parameters
        ----------
        task_run_id
            The task run whose write this is.
        evidence
            What the write is about to commit.
        """
        self._session.execute(
            update(TaskRun)
            .where(TaskRun.id == task_run_id, TaskRun.has_checks.is_(True))
            .values(
                written_table_id=evidence.table_id,
                written_snapshot_before=evidence.snapshot_before,
                written_snapshot_after=evidence.snapshot_id,
                written_publishes=evidence.publishes,
            )
            .execution_options(synchronize_session=False)
        )

    def _advance_own_tip(self, task_run_id: uuid.UUID, evidence: _Evidence) -> None:
        """Record that a Python task's write is the next link of its task run's own chain.

        A Python asset writes through the REST API under a claim-form context, possibly more
        than once and in more than one attempt, and its stamp cannot land with its commit. So
        that a rollback can name exactly what the task run wrote, the commit of each such
        write moves `task_runs.own_snapshot_id` to the snapshot it made, in its own
        transaction (the task run's row is already held `FOR SHARE` here), but only when the
        write started from where the chain stands:

        - the chain is empty and the write was staged on the task run's baseline (the same
          snapshot, and the same answer to "would a write publish": so a table somebody else
          created after a "missing" baseline is not taken for the run's own), or
        - the write was staged on the chain's tip.

        A write that was staged on anything else leaves the tip where it is: somebody else
        changed the table in between, and the rollback will find the table not at the tip
        (``changed_since``). A write that made no snapshot on a table that had none (a table
        created without rows) sets the tip to `OWN_TIP_NO_SNAPSHOT`, which is not "never
        wrote"; one that made no snapshot on a table that has one (an append of no rows)
        changed nothing and leaves the tip alone. It matches no row for a
        task run without a baseline (a task without checks, a SQL task): a null baseline equals
        nothing and there is no tip to continue, so nothing is ever recorded for those.

        Parameters
        ----------
        task_run_id
            The task run whose write this is.
        evidence
            What the write is about to commit, and the snapshot it was staged on.
        """
        staged_on = evidence.snapshot_before
        if evidence.snapshot_id is None and staged_on is not None:
            # An append of no rows into a table that has a snapshot changed nothing: it is no
            # link of the chain, and must not put "no snapshot" over the tip.
            return
        self._session.execute(
            update(TaskRun)
            .where(
                TaskRun.id == task_run_id,
                TaskRun.has_checks.is_(True),
                or_(
                    and_(
                        TaskRun.own_snapshot_id.is_(None),
                        TaskRun.baseline_snapshot_id.is_not_distinct_from(staged_on),
                        TaskRun.baseline_publishes == evidence.publishes,
                    ),
                    TaskRun.own_snapshot_id
                    == (OWN_TIP_NO_SNAPSHOT if staged_on is None else staged_on),
                ),
            )
            .values(
                own_snapshot_id=OWN_TIP_NO_SNAPSHOT
                if evidence.snapshot_id is None
                else evidence.snapshot_id
            )
            .execution_options(synchronize_session=False)
        )

    def _hold_fence(self, fence: WriteFence, evidence: _Evidence | None) -> None:
        """Refuse unless the fenced run is still `RUNNING` under the fence's worker.

        One statement checks and locks the row, in the transaction of the write: on Postgres
        a sweep or a cancel that ended the run either committed first and is seen here, or
        waits for this transaction and then finds a run that has its evidence. With a noted
        result the statement is the `UPDATE` that records it; without one, a `SELECT ... FOR
        SHARE`. On SQLite neither locks; the `WHERE` clause is the check and one process
        writes at a time there.

        Parameters
        ----------
        fence
            The run and worker the write belongs to.
        evidence
            What the write commits, or `None` when `note_result` was not called for it.

        Raises
        ------
        WriteFenceRefused
            With `WRITE_FENCE_MESSAGE`, after rolling back the caller's uncommitted work.
        """
        still_held = (
            QueryRun.id == fence.query_run_id,
            QueryRun.status == QueryRunStatus.RUNNING,
            QueryRun.worker_id.is_not_distinct_from(fence.worker_id),
        )
        if evidence is None:
            held = (
                self._session.execute(
                    select(QueryRun.id).where(*still_held).with_for_update(read=True)
                ).scalar_one_or_none()
                is not None
            )
        else:
            held = (
                rowcount(
                    self._session.execute(
                        update(QueryRun)
                        .where(*still_held)
                        .values(
                            rows_written=evidence.rows,
                            snapshot_id=evidence.snapshot_id,
                            snapshot_before=evidence.snapshot_before,
                            target_table_id=evidence.table_id,
                        )
                        .execution_options(synchronize_session=False)
                    )
                )
                == 1
            )
        if not held:
            self._pending = None
            self._session.rollback()
            raise WriteFenceRefused()

    def _run_allows(
        self,
        context: RunContext,
        principal_id: uuid.UUID,
        environment_id: uuid.UUID,
        operation: WriteOperation,
        target: Target,
        *,
        own_asset_only: bool,
        lock: bool = False,
        rollback: bool = False,
    ) -> bool | _KindRefusal:
        """Decide whether a run context covers this write, from the database.

        Parameters
        ----------
        context
            The claimed run context.
        principal_id
            The acting principal, which must be the run's bound principal.
        environment_id
            The environment written to.
        operation
            What is being done.
        target
            The table or namespace, as parts.
        own_asset_only
            Restrict the write to the task's own asset (protected
            environments); otherwise only the attempt's liveness is checked
            and the write's scope is left to grants, as without a run. A
            claim-form context is restricted to its own asset regardless.
        lock
            Read the task run and its run `FOR SHARE`: on Postgres this blocks
            the scheduler's reclaim and a cancellation until the caller's
            transaction ends, and re-evaluates after any wait.
        rollback
            Whether the write was declared the internal rollback of a set-back.

        Returns
        -------
        bool or _KindRefusal
            A refusal when the task's own asset is written in the wrong output
            kind: an artifact version by a task that produces a table, or by the
            worker's query-run form, or a table write by a task that produces an
            artifact. Otherwise `True` only for a running task run whose current query run is the
            claimed one (the plain form) or whose claim counter is the context's
            (claim form), of a live (running, not cancel-requested) run in
            the environment bound to `principal_id` — except that a claim-form context of a task
            row marked `setting_back` also passes a cancel request for the rollback commit of its
            own asset — and, with `own_asset_only`,
            writing its own asset (or creating an ancestor namespace of it).
            A claim-form context is restricted to the task's own asset whether
            or not `own_asset_only`; a context with both forms or neither
            covers nothing.
        """
        by_claim = context.claim is not None
        if by_claim == (context.query_run_id is not None):
            return False
        task_query = (
            select(TaskRun)
            .where(TaskRun.id == context.task_run_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            task_query = task_query.with_for_update(read=True)
        task = self._session.execute(task_query).scalar_one_or_none()
        if task is None or task.status is not TaskStatus.RUNNING:
            return False
        if by_claim:
            if task.claim_count != context.claim:
                return False
        elif task.query_run_id != context.query_run_id:
            return False
        run_query = (
            select(Run).where(Run.id == task.run_id).execution_options(populate_existing=True)
        )
        if lock:
            run_query = run_query.with_for_update(read=True)
        run = self._session.execute(run_query).scalar_one_or_none()
        # A project asset is validated dot-free per component, so splitting
        # its name is exact; the target is never re-joined for comparison.
        asset = tuple(task.asset.split("."))
        # Setting the table back is the cancel's own work, so a cancel request does not refuse
        # that one commit. The row decides, never the context: the flag is the scheduler's.
        sets_back = (
            by_claim
            and task.setting_back
            and operation is WriteOperation.COMMIT
            and rollback
            and target == asset
        )
        if (
            run is None
            or run.environment_id != environment_id
            or run.status is not RunStatus.RUNNING
            or (run.cancel_requested and not sets_back)
            or run.run_as_principal_id != principal_id
        ):
            return False
        if operation in _MATERIALIZING and target == asset:
            version = self._session.get(JobVersion, run.job_version_id)
            entry = None if version is None else python_entry(version.manifest_json, task.asset)
            produces_artifact = entry is not None and entry_output(entry) == "artifact"
            if operation is WriteOperation.ARTIFACT_VERSION:
                if not produces_artifact:
                    return _KindRefusal(TABLE_TASK_MESSAGE.format(asset=task.asset))
                if not by_claim:
                    return _KindRefusal(WORKER_VERSION_MESSAGE.format(asset=task.asset))
            elif produces_artifact:
                return _KindRefusal(ARTIFACT_TASK_MESSAGE.format(asset=task.asset))
        if not own_asset_only and not by_claim:
            return True
        if operation in _MATERIALIZING:
            covered = target == asset
        elif operation is WriteOperation.CREATE_NAMESPACE:
            covered = len(target) < len(asset) and asset[: len(target)] == target
        else:
            covered = False
        if not covered and by_claim and not own_asset_only:
            # A live attempt that reaches past its own asset in an unprotected environment is
            # not a stale one; say so, and let the audit entry tell the two apart.
            operation_text = operation.value.replace("_", " ")
            if target == asset:
                # Not another asset: this operation on its own one, which a task may not do.
                return _KindRefusal(
                    OWN_ASSET_MESSAGE.format(
                        asset=shown(task.asset),
                        operation=operation_text,
                        subject=(
                            "a namespace with its own asset's name"
                            if operation in _NAMESPACE_OPERATIONS
                            else "its own asset"
                        ),
                        tables=_TABLES_TEXT,
                        artifacts=_ARTIFACTS_TEXT,
                    ),
                    OWN_ASSET_REASON,
                )
            return _KindRefusal(
                OTHER_ASSET_MESSAGE.format(
                    asset=shown(task.asset),
                    operation=operation_text,
                    target=shown(_dotted(target)),
                ),
                OTHER_ASSET_REASON,
            )
        return covered

    def _deny(
        self,
        p: PrincipalRef,
        environment: str,
        operation: WriteOperation,
        target: Target,
        *,
        in_run: bool,
        protected: bool,
        refusal: _KindRefusal | None = None,
    ) -> NoReturn:
        """Audit and refuse.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment written to.
        operation
            What was attempted.
        target
            The table or namespace, as parts.
        in_run
            Whether a (non-covering) run context was presented.
        protected
            Whether the environment is protected; an unprotected one only
            refuses a superseded attempt, which gets the neutral message.
        refusal
            Set for a write of the wrong output kind, or by a live attempt past its own asset
            in an unprotected environment: its text replaces both other messages and the
            audit detail gets its `reason` (``output_kind``, ``other_asset`` or
            ``own_asset_operation``).

        Raises
        ------
        PermissionDenied
            Always, with the text of `refusal` if given, else `PROTECTED_MESSAGE` (or
            `SUPERSEDED_MESSAGE` when unprotected). The caller's uncommitted state
            (a half-built write's audit and lineage rows) is rolled back
            first so the denial's commit persists nothing but itself.
        """
        self._pending = None
        self._session.rollback()
        detail: dict[str, object] = {
            "operation": operation.value,
            "target": _dotted(target),
            "in_run": in_run,
            "protected": protected,
        }
        if refusal is not None:
            detail["reason"] = refusal.reason
        AuditService(self._session).record(
            p.id,
            AuditEventType.PROTECTED_WRITE_DENIED,
            securable=Securable.environment_scope(environment),
            detail=detail,
            commit=True,
        )
        if refusal is not None:
            raise PermissionDenied(refusal.message)
        if not protected:
            raise PermissionDenied(SUPERSEDED_MESSAGE)
        raise PermissionDenied(PROTECTED_MESSAGE.format(environment=environment))
