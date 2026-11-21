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

import re
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.tombstones import drop_row
from lakana_core.db import db_now
from lakana_core.errors import ConflictError, ValidationError
from lakana_core.identifiers import TableRef, check_new_name
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

REBUILD_SELECTION_MISMATCH = (
    "The run for `{name}` would cover {assets} assets instead of exactly that one, so nothing was "
    "dropped. Deploy the project again, then rebuild again."
)
"""The refusal when the selection of a rebuild does not resolve to exactly its own asset."""

REBUILD_STORED_NAME = (
    "`{name}` was not dropped: its name is kept only while the catalog holds the dropped "
    "table, which it does not for {kind}, and a new name must meet today's rules. {problem} "
    "Rename the asset's file in git and deploy, so the run creates the table under the new name."
)
"""The refusal of a rebuild whose run could not create the table again (#609)."""


def _literal(dotted: str) -> str:
    """Escape a dotted name for a selection pattern, so that it matches only itself.

    Selections are `fnmatch` globs, and a table name may hold ``*``, ``?`` or ``[``; each is
    put in brackets, where it stands for itself.

    Parameters
    ----------
    dotted
        The dotted table name.

    Returns
    -------
    str
        A pattern that matches exactly `dotted`.
    """
    return re.sub(r"([*?\[])", r"[\1]", dotted)


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

    def _refuse_a_name_the_run_cannot_create(self, ref: TableRef, kind: TableType) -> None:
        """Refuse a rebuild whose run could not create the table under its stored name (#609).

        The run creates the table as a new one. A table's drop leaves a tombstone for the undo
        period, and `CatalogService._prepare_table` lets a name a tombstone holds pass the rules
        for new names. A view or an external table leaves none, and with an undo period of 0
        days the tombstone may be purged before the run, so a stored name such as ``v$`` that
        today's rules refuse is refused here, before anything is dropped. A table whose drop
        turns out to leave no tombstone is refused after the drop, which the rebuild then rolls
        back.

        Parameters
        ----------
        ref
            The table.
        kind
            Its row's kind.

        Raises
        ------
        ConflictError
            `REBUILD_STORED_NAME`, naming what is wrong with the name.
        """
        keeps_its_name = kind is TableType.ICEBERG and self._undo_days > 0
        if keeps_its_name:
            return
        what = {TableType.VIEW: "a view", TableType.EXTERNAL: "an external table"}.get(
            kind, "a table dropped without an undo period"
        )
        self._refuse_a_new_name(ref, what)

    @staticmethod
    def _refuse_a_new_name(ref: TableRef, kind: str) -> None:
        """Refuse the rebuild when today's rules refuse `ref`'s name as a new one.

        Parameters
        ----------
        ref
            The table.
        kind
            What it is, for the sentence, such as ``"a view"``.

        Raises
        ------
        ConflictError
            `REBUILD_STORED_NAME`, naming what is wrong with the name.
        """
        try:
            check_new_name(ref.name, what="Table name")
        except ValidationError as exc:
            raise ConflictError(
                REBUILD_STORED_NAME.format(name=ref.dotted(), kind=kind, problem=str(exc))
            ) from None

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
            (`REBUILD_RUN_ACTIVE`), a view, an external table or a table without an undo
            period has a stored name the run could not create again (`REBUILD_STORED_NAME`), a
            protected environment has no usable run principal, or
            the selection does not resolve to exactly its asset
            (`REBUILD_SELECTION_MISMATCH`). Nothing is written.
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
        self._refuse_a_name_the_run_cannot_create(ref, row.table_type)
        table_id = row.id
        # Decided now: the drop revokes the table's own grants and deletes its row, after which
        # a read granted on the table itself no longer counts.
        path = found.path if self._authz.permits(p, Action.READ_TABLE, scope) else None
        prepared = self._runs.prepare_start(
            p,
            ref.environment,
            found.repository,
            Selection(patterns=(_literal(ref.dotted()),)),
            authorize=False,
        )
        if tuple(prepared.assets) != (ref.dotted(),):
            raise ConflictError(
                REBUILD_SELECTION_MISMATCH.format(name=ref.dotted(), assets=len(prepared.assets))
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
        try:
            row = self._catalog._lock_table_row(ref, table_id)
            now = db_now(self._session)
            kept = drop_row(
                self._session,
                p,
                ref,
                row,
                now=now,
                purge_after=now + timedelta(days=self._undo_days),
                reason="rebuild",
                detail={"rebuild_reason": reason},
            )
            if kept is None:
                # No tombstone (a table sharing an old location): the run would take the name
                # as a new one, so a name today's rules refuse rolls the drop back.
                self._refuse_a_new_name(ref, "a table whose drop keeps no record of it")
            # No repository and no path in the event: the audit log is read with
            # `READ_AUDIT_LOG` alone, and a file is named only to a caller who may read the
            # table. The run's own `RUN_STARTED` names the repository and the project version.
            AuditService(self._session).record(
                p.id,
                AuditEventType.TABLE_REBUILT,
                securable=scope,
                detail={"table_id": str(table_id), "reason": reason, "run_id": str(run.id)},
            )
            self._guard.record_pending()
            self._session.commit()
        except BaseException:
            # The run is flushed; never leave it for the caller's session to commit without
            # the drop (the row vanished under its lock, or the drop failed).
            self._session.rollback()
            raise
        return RebuildResult(ref=ref, run_id=run.id, repository=found.repository, path=path)
