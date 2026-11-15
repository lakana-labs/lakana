"""Rebuild a table a deployed project builds: drop it and queue a run of its asset, in one go.

A rebuild is the way back for a table that git owns and that has to be built again from
scratch: a person may not drop it (`lakana_core.catalog.git_owned`), and its next run would
only write into it. `TableRebuild.rebuild` drops the table, which keeps its undo period like
any drop (`TABLE_DROPPED` with reason ``rebuild``), records `TABLE_REBUILT`, and queues a run
of exactly the asset that declares the table, all in one transaction. `TABLE_REBUILT` holds the
table's id, the reason and the run's id, never the repository or the file. When the run cannot be
queued (another run of the project is active, a protected environment has no run principal)
nothing is written, so a person is never left with a dropped table and no run.

This module imports `lakana_core.runs.service`; `runs.service` never imports
`lakana_core.catalog.service`, so there is no cycle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.tombstones import drop_row
from lakana_core.db import db_now
from lakana_core.errors import ConflictError
from lakana_core.identifiers import TableRef
from lakana_core.identity.sessions import require_step_up
from lakana_core.models import AuditEventType, RunKind, TableType
from lakana_core.projects.definition import defining_ref
from lakana_core.runs.lockdown import (
    ProtectedWriteGuard,
    WriteOperation,
    table_target,
    validate_break_glass_reason,
)
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import ActiveRunError, RunService

NOT_BUILT_BY_A_PROJECT = (
    "`{name}` is not built by a deployed project, so there is nothing to rebuild it from. "
    "Drop it instead: `lakana table delete {name} --env {environment}`."
)
"""The refusal of a rebuild of a name no current deployment declares."""

REBUILD_ARTIFACT = (
    "`{name}` is an artifact; rebuild works on tables and views. Delete versions with "
    "`lakana artifact versions delete`."
)
"""The refusal of a rebuild of an artifact."""

REBUILD_RUN_ACTIVE = (
    "Run {run_id} of `{repository}` is already active in {environment}, so `{name}` was not "
    "dropped. Wait for it to end (`lakana runs show {run_id} --env {environment}`), then "
    "rebuild again."
)
"""The refusal of a rebuild while another run of the declaring project is active."""

REBUILD_RUN_ACTIVE_UNREAD = (
    "A run of `{repository}` is already active in {environment}, so `{name}` was not dropped. "
    "Wait for it to end, then rebuild again."
)
"""`REBUILD_RUN_ACTIVE` when the active run ended before its id could be read."""


@dataclass(frozen=True)
class RebuildResult:
    """What a rebuild did.

    Parameters
    ----------
    ref
        The table dropped.
    run_id
        The queued run that builds it again.
    repository
        The repository whose asset builds it.
    path
        The asset's file in the repository; `None` to a caller who may not read the table
        (`Action.READ_TABLE`), who is not told the file.
    """

    ref: TableRef
    run_id: uuid.UUID
    repository: str
    path: str | None


class TableRebuild:
    """Drop a table a deployed project builds and queue a run of its asset, in one transaction.

    Parameters
    ----------
    session
        Database session; `rebuild` commits it.
    catalog
        The catalog, which finds and locks the table row.
    runs
        The run service, which prepares and inserts the run.
    authz
        Authorization: `DROP_TABLE` on the table, `RUN` on the environment.
    undo_days
        How long the dropped table stays restorable (`LAKANA_TABLES__UNDO_DAYS`).
    """

    def __init__(
        self,
        session: Session,
        catalog: CatalogService,
        runs: RunService,
        authz: AuthorizationService,
        *,
        undo_days: int,
    ) -> None:
        self._session = session
        self._catalog = catalog
        self._runs = runs
        self._authz = authz
        self._undo_days = undo_days
        self._guard = ProtectedWriteGuard(session, authz)

    def rebuild(self, p: PrincipalRef, ref: TableRef, *, reason: str) -> RebuildResult:
        """Drop `ref` and queue a run of the asset that declares it.

        Any name a current deployment declares can be rebuilt, whatever the row's kind: a
        table, a view, an external table, or a table under an artifact asset's name. The
        drop keeps the undo period, so `undrop` restores the old table within it. The order
        of the writes follows the deployment's lock order (`RunService.insert_run`): the run
        first, then the table row's lock, the drop and the audit, then one commit.

        Parameters
        ----------
        p
            The caller; a `break_glass_reason` on it lets the drop into a protected
            environment.
        ref
            The table.
        reason
            Why, 10 to 500 characters on one line; recorded in `TABLE_DROPPED` and
            `TABLE_REBUILT`.

        Returns
        -------
        RebuildResult
            The table, the queued run, and the file that builds it.

        Raises
        ------
        PermissionDenied
            Without `DROP_TABLE` on the table or `RUN` on the environment, or in a protected
            environment without a break-glass reason.
        StepUpRequired
            If a browser session's last passkey touch is older than 15 minutes; a token is
            never asked. Checked before anything is read.
        ValidationError
            If the reason is not 10 to 500 characters of one line, or the selection matches
            nothing.
        NotFoundError
            If the table does not exist.
        ConflictError
            If it is an artifact (`REBUILD_ARTIFACT`), no current deployment declares it
            (`NOT_BUILT_BY_A_PROJECT`), another run of the project is active
            (`REBUILD_RUN_ACTIVE`), or a protected environment has no usable run principal.
            Nothing is written.
        """
        scope = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        self._authz.authorize(p, Action.DROP_TABLE, scope)
        self._authz.authorize(p, Action.RUN, Securable.environment_scope(ref.environment))
        require_step_up(p)
        reason = validate_break_glass_reason(reason, what="The rebuild reason")
        self._guard.check(p, ref.environment, WriteOperation.DROP_TABLE, table_target(ref))
        ns, row = self._catalog._find_table(ref)
        if row.table_type is TableType.ARTIFACT:
            raise ConflictError(REBUILD_ARTIFACT.format(name=ref.dotted()))
        found = defining_ref(self._session, ns.environment_id, ref)
        if found is None:
            raise ConflictError(
                NOT_BUILT_BY_A_PROJECT.format(name=ref.dotted(), environment=ref.environment)
            )
        table_id = row.id
        prepared = self._runs.prepare_start(
            p,
            ref.environment,
            found.repository,
            Selection(patterns=(ref.dotted(),)),
            authorize=False,
        )
        try:
            run = self._runs.insert_run(
                prepared.actor,
                prepared.env,
                prepared.repo,
                prepared.version,
                prepared.choice,
                prepared.assets,
                kind=RunKind.SELECTION,
                selection=prepared.selection,
                resumed_from=None,
            )
        except ActiveRunError as exc:
            if exc.run_id is None:
                message = REBUILD_RUN_ACTIVE_UNREAD.format(
                    repository=found.repository, environment=ref.environment, name=ref.dotted()
                )
            else:
                message = REBUILD_RUN_ACTIVE.format(
                    run_id=exc.run_id,
                    repository=found.repository,
                    environment=ref.environment,
                    name=ref.dotted(),
                )
            raise ConflictError(message) from None
        row = self._catalog._lock_table_row(ref, table_id)
        now = db_now(self._session)
        drop_row(
            self._session,
            p,
            ref,
            row,
            now=now,
            purge_after=now + timedelta(days=self._undo_days),
            reason="rebuild",
            detail={"rebuild_reason": reason},
        )
        # No repository and no path in the event: the audit log is read with `READ_AUDIT_LOG`
        # alone, and a file is named only to a caller who may read the table. The run's own
        # `RUN_STARTED` names the repository and the project version.
        AuditService(self._session).record(
            p.id,
            AuditEventType.TABLE_REBUILT,
            securable=scope,
            detail={"table_id": str(table_id), "reason": reason, "run_id": str(run.id)},
        )
        path = found.path if self._authz.permits(p, Action.READ_TABLE, scope) else None
        self._guard.record_pending()
        self._session.commit()
        return RebuildResult(ref=ref, run_id=run.id, repository=found.repository, path=path)
