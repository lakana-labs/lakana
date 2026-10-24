"""The query submission service.

A sibling to `CatalogService`, not a subclass: it owns a different table
(`query_runs`) and a narrower lifecycle (insert once, read status), and does
not touch object storage the way every `CatalogService` write does.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.service import AuditService
from lakana_core.audit.views import View, ViewDetail, record_view, sessions_like
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.catalog.identity import artifact_refusal, namespace_exists, resolve_table_id
from lakana_core.catalog.maintenance import (
    DEFAULT_KEEP_DAYS,
    DEFAULT_KEEP_LAST,
    DEFAULT_ORPHAN_AGE_HOURS,
    check_orphan_age,
)
from lakana_core.config import ViewAuditConfig
from lakana_core.db import db_now, rowcount
from lakana_core.errors import AlreadyExistsError, ConflictError, NotFoundError, ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    AuditEventType,
    Environment,
    Principal,
    QueryErrorCause,
    QueryRun,
    QueryRunStatus,
    Table,
    TableType,
    TaskRun,
    WriteMode,
)
from lakana_core.pagination import decode_cursor, encode_cursor, parse_choices
from lakana_core.runs import pools
from lakana_core.runs.lockdown import (
    ProtectedWriteGuard,
    WriteOperation,
    table_target,
    validate_break_glass_reason,
)
from lakana_core.runs.store import end_lost_run, is_check_count, record_query_completed
from lakana_core.runs.waiting import (
    UserLimit,
    WaitReason,
    ahead_kind,
    ahead_of,
    alive_workers,
    count_ahead,
    query_wait_reason,
    queued_times,
    settled_early,
    with_work,
)
from lakana_core.runs.workers import without_alive_worker

QUERY_CANCELLED_MESSAGE = "The query was cancelled."
"""`error_message` of a cancelled query run; the worker's `CANCELLED_MESSAGE` is the same text."""

QUERY_LIST_ENDED_SECONDS = 86_400
"""How long a run stays in `list_queries(include_ended=True)` after it ended: 24 hours."""

QUERY_LIST_MAX = 200
"""The most rows `list_queries` returns."""

_RUNNING, _QUEUED, _ENDED = 0, 1, 2
"""The parts a listing is read in, in order; a cursor names the part of its last row."""

_LIVE_STATUSES = (QueryRunStatus.RUNNING, QueryRunStatus.QUEUED)
"""The statuses of a query that has not ended."""

MAX_AHEAD_COUNTED = 10_000
"""The most queued runs a listing counts ahead of a row; beyond it the reason says "more than"."""

CANCEL_LOCK_TIMEOUT_SECONDS = 2
"""How long a cancel waits for a run's row while a worker starts or finishes the run (Postgres);
after that the cancel is refused with a conflict instead of hanging behind a frozen worker."""


@dataclass(frozen=True)
class QueryListItem:
    """One row of `QueryService.list_queries`: no SQL, no error message, no lineage.

    Parameters
    ----------
    id
        The run's id.
    submitter
        The submitting principal's name, or `None` when the principal was deleted (or the run
        had none).
    kind
        `read`, or the write mode: `create`, `replace`, `append`, `view` or `maintain`.
    target
        A write run's target as submitted (dotted), `None` for a read.
    status
        The run's status value (`QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`).
    created_at
        When it was submitted.
    started_at
        When a worker started it.
    finished_at
        When it ended.
    error_cause
        Why it failed or was cancelled; never the message.
    waiting_for
        Why a `QUEUED` run has not started; `None` for any other status.
    """

    id: uuid.UUID
    submitter: str | None
    kind: str
    target: str | None
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_cause: str | None
    waiting_for: WaitReason | None


def _bound_task_run_id(p: PrincipalRef) -> uuid.UUID | None:
    """Return the task run a principal's token is bound to, if any.

    Parameters
    ----------
    p
        The submitting principal.

    Returns
    -------
    uuid.UUID or None
        `p.task_scope.bound.task_run_id`, or `None` for a PAT or an unbound token.
    """
    scope = p.task_scope
    return scope.bound.task_run_id if scope is not None and scope.bound is not None else None


MERGE_IS_NOT_A_QUERY_MODE = (
    "merge is written by assets (`write: merge`) and by `client.tables.merge`; a query writes "
    "with create, replace or append"
)
"""Why a write run refuses `merge`: the key and the checks belong to an asset or a merge call."""


@dataclass(frozen=True)
class WriteTarget:
    """Where a write run puts its result.

    Parameters
    ----------
    table
        The target's dotted name.
    mode
        `CREATE`, `REPLACE`, `APPEND` or `VIEW`.
    incremental_key
        The key column, `APPEND` only.
    evolve_schema
        Let the target adopt the result's schema, `REPLACE`/`APPEND` only.
    """

    table: str
    mode: WriteMode
    incremental_key: str | None = None
    evolve_schema: bool = False

    def validate(self) -> None:
        """Refuse an option combination the spec forbids.

        Raises
        ------
        ValidationError
            If `mode` is `MAINTAIN` or `MERGE` (assets and `tables.merge` merge, a query
            does not), `incremental_key` is set for a mode other than `APPEND`, or
            `evolve_schema` for a mode other than `REPLACE`/`APPEND`.
        """
        if self.mode is WriteMode.MAINTAIN:
            raise ValidationError("maintain is not a query target mode.")
        if self.mode is WriteMode.MERGE:
            raise ValidationError(MERGE_IS_NOT_A_QUERY_MODE)
        if self.incremental_key is not None and self.mode is not WriteMode.APPEND:
            raise ValidationError("incremental_key applies to mode append only.")
        if self.evolve_schema and self.mode not in (WriteMode.REPLACE, WriteMode.APPEND):
            raise ValidationError("evolve_schema applies to modes replace and append only.")


class QueryService:
    """Submit SQL queries and read back their status and result.

    Parameters
    ----------
    session
        Database session. Each method commits internally on success, the same
        contract `CatalogService` uses.
    authz
        Authorization service consulted before every operation.
    lease_seconds
        `LAKANA_RUN__LEASE_SECONDS`: a worker whose heartbeat is older is not alive, which is
        how `cancel_query` tells a run that can still be asked to stop from one whose worker is
        gone. The server passes the configured value; the default is the setting's own.
    running_per_principal
        `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL`: `auto` or a whole number of 1 or more. Only
        the wait reasons read it here (they say `user_limit` when a principal is at the limit
        now); the workers enforce it. The server passes the configured value; the default is
        the setting's own.
    view_audit
        `LAKANA_VIEW_AUDIT__*`: whether and how often a manager's listing of other people's
        queries is recorded as `ACTIVITY_VIEWED`. `None` (the default) is the settings'
        default: on, with a window of 60 minutes.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        *,
        lease_seconds: int = 60,
        running_per_principal: int | Literal["auto"] = "auto",
        view_audit: ViewAuditConfig | None = None,
    ) -> None:
        self._session = session
        self._authz = authz
        self._lease_seconds = lease_seconds
        self._running_per_principal = running_per_principal
        self._view_audit = view_audit or ViewAuditConfig()
        self._guard = ProtectedWriteGuard(session, authz)

    def submit_query(self, p: PrincipalRef, environment: str, sql: str) -> QueryRun:
        """Enqueue a query for execution.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment to run the query against.
        sql
            The SQL text, unmodified.

        Returns
        -------
        QueryRun
            The inserted row, with `status` `QUEUED`. Its `task_token_id` is
            set from `p.task_scope.token_id` when `p` authenticated via a
            task token, `None` for a PAT — this is what later scopes
            `get_query_run` and lets the worker re-check the token's own
            validity before running the query. `task_run_id` is set
            when `p` authenticated with a token bound to a task attempt
            , so the lockdown judges a write in such a query as that
            task's.

        Raises
        ------
        NotFoundError
            If the environment does not exist.
        PermissionDenied
            If the principal may not submit queries in this environment.
        """
        self._authz.authorize(p, Action.SUBMIT_QUERY, Securable.environment_scope(environment))
        env = self._session.execute(
            select(Environment).where(Environment.name == environment, LIVE_ENVIRONMENT)
        ).scalar_one_or_none()
        if env is None:
            raise NotFoundError(f"No environment named {environment!r}.")
        run = QueryRun(
            id=uuid.uuid4(),
            environment_id=env.id,
            principal_id=p.id,
            sql_text=sql,
            status=QueryRunStatus.QUEUED,
            task_token_id=p.task_scope.token_id if p.task_scope is not None else None,
            task_run_id=_bound_task_run_id(p),
        )
        self._session.add(run)
        self._session.commit()
        return run

    def get_query_run(self, p: PrincipalRef, environment: str, query_id: uuid.UUID) -> QueryRun:
        """Fetch a query run's current status and, once finished, its outcome.

        Only the principal that submitted a run can read it. A caller
        authenticated via a task token additionally only sees runs
        submitted with that same token — a PAT caller sees every run
        its principal submitted, task-token or not, since a PAT is not
        itself scoped to one token.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment the query was submitted to.
        query_id
            The query run's id.

        Returns
        -------
        QueryRun
            The row.

        Raises
        ------
        NotFoundError
            If the environment does not exist, or if no query run with that
            id exists in it that `p` submitted — a run belonging to someone
            else is indistinguishable from a missing one, by design:
            its result was computed under its submitter's row filters and
            column masks, not the caller's. Also raised for a run `p`'s
            principal submitted through a different task token, or through
            a PAT, when `p` itself is task-token-scoped.
        PermissionDenied
            If the principal may not read queries in this environment.
        """
        self._authz.authorize(p, Action.SUBMIT_QUERY, Securable.environment_scope(environment))
        env = self._session.execute(
            select(Environment).where(Environment.name == environment, LIVE_ENVIRONMENT)
        ).scalar_one_or_none()
        if env is None:
            raise NotFoundError(f"No environment named {environment!r}.")
        conditions = [
            QueryRun.id == query_id,
            QueryRun.environment_id == env.id,
            QueryRun.principal_id == p.id,
        ]
        if p.task_scope is not None:
            conditions.append(QueryRun.task_token_id == p.task_scope.token_id)
        run = self._session.execute(select(QueryRun).where(*conditions)).scalar_one_or_none()
        if run is None:
            raise NotFoundError(f"No query run {query_id} in {environment!r}.")
        return run

    def waiting_for(self, run: QueryRun) -> WaitReason | None:
        """Say why a run has not started, when it is still `QUEUED`.

        Computed when asked and never stored. A read that already authorized `run` as its
        submitter's own (the status read, the cancel) passes it in, so the reader is the
        principal who submitted it: a `user_limit` reason carries that principal's own running
        count. This method grants nothing and reads no other run's data, only counts: the
        workers and what they take, the principal's running ad-hoc queries (an interactive run
        with a principal, while a worker takes interactive work) and the queued query and
        deploy runs ahead of `run`, in every environment (the dequeue order is global). For a
        run in any other status it issues no statement. For a queued run it issues two when no
        worker is alive or none takes its kind (the clock, the workers); otherwise the clock,
        the workers and what they carry (queries, deploys, task leases), five, and: the count
        ahead (the query part by the index on `query_runs (status, created_at)`) when every
        worker of its kind is busy, 6; when one is free, for an interactive run with a
        principal also the principal's running count (read in the same statement as the time
        until which workers pass the principal by after a lock timeout, `lock_timeout`), then
        the count ahead unless the limit is reached, 7, or 6 when the limit is reached and
        decides. A run that is not capped
        (pipeline work, or without a principal) never reads the principal's count.

        The numbers are estimates: for up to a lease after a worker died it still counts as
        alive, and as busy if it held a run, and its running query still counts against the
        limit; a draining worker counts as free. The limit is the server's setting, resolved
        now over the workers that are alive now (`pools.cap_for`, the workers' own function).
        `user_limit` is said only while a worker that takes interactive work is free: with all
        of them busy the limit holds nothing the workers do not hold, and the reason is
        `workers_busy`.

        Parameters
        ----------
        run
            A query run the caller may see, as its submitter.

        Returns
        -------
        WaitReason or None
            The reason, or `None` when the run is not `QUEUED`.
        """
        if run.status != QueryRunStatus.QUEUED:
            return None
        now = db_now(self._session)
        snapshot = alive_workers(self._session, now, self._lease_seconds)
        kind = pools.kind_of(run.task_run_id, run.write_mode)
        maintenance = run.write_mode is WriteMode.MAINTAIN
        counts = snapshot.for_query(kind)
        if counts.alive == 0 or counts.taking == 0:
            return query_wait_reason(counts, 0, kind=kind, maintenance=maintenance)
        snapshot = with_work(self._session, snapshot)
        counts = snapshot.for_query(kind)
        limit: UserLimit | None = None
        passed_by: datetime | None = None
        if counts.free and kind == pools.INTERACTIVE and run.principal_id is not None:
            running, passed_by = pools.running_and_passed_by(self._session, run.principal_id, now)
            limit = UserLimit(
                running, pools.cap_for(self._running_per_principal, snapshot.interactive())
            )
        ahead = 0
        if not settled_early(counts, limit):
            ahead = count_ahead(self._session, run.created_at, ahead_kind(snapshot, kind))
        return query_wait_reason(
            counts,
            ahead,
            kind=kind,
            maintenance=maintenance,
            limit=limit,
            passed_by_until=passed_by,
        )

    def list_queries(
        self,
        p: PrincipalRef,
        environment: str,
        *,
        include_ended: bool = False,
        limit: int = 100,
    ) -> list[QueryListItem]:
        """List the queries of an environment: running, then queued, then (optionally) ended.

        The first page of `list_queries_page` without a filter.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        include_ended
            Also list the runs that ended in the last 24 hours.
        limit
            Most rows returned (1 to 200).

        Returns
        -------
        list of QueryListItem
            The rows, in the order of `list_queries_page`.

        Raises
        ------
        PermissionDenied
            If the caller may not read queries in this environment (as `get_query_run`).
        NotFoundError
            If the environment does not exist.
        ValidationError
            If `limit` is outside 1 to 200.
        """
        return self.list_queries_page(p, environment, include_ended=include_ended, limit=limit)[0]

    def list_queries_page(
        self,
        p: PrincipalRef,
        environment: str,
        *,
        include_ended: bool = False,
        status: Sequence[str] = (),
        submitter: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[QueryListItem], str | None]:
        """List the queries of an environment, one page at a time: running, queued, then ended.

        Who sees which row follows `cancel_query`. A caller with `MANAGE_ENVIRONMENT` on the
        environment who authenticated with a personal token (`permits` only chooses the path;
        `authorize` is the gate, and what is audited) sees every row of this environment.
        Everyone else needs `SUBMIT_QUERY` on it, as the status read does, and sees only the
        runs their principal submitted; a task-token caller only the runs of that token. The
        rows of others are absent, not redacted, so the list tells a non-manager nothing about
        whether a query exists. The status read stays submitter-only: a manager sees another
        user's query here and still cannot read its SQL, its error text or its result.

        The rows never hold the SQL, the error message, the lineage, the result location or
        the worker. Order: `RUNNING` runs longest-running first, then `QUEUED` runs in the
        order they will be taken (oldest first), then, with `include_ended`, the runs that ended
        in the last `QUERY_LIST_ENDED_SECONDS`, newest first; `limit` cuts that order, and ties on
        the time are broken by id. A page continues strictly after the last row of the previous
        one (`cursor`), in that order. A query that changes status between two pages moves to
        another part of the order, so a long listing of live queries can miss or repeat it;
        the ended part never changes.

        `status` and `submitter` only narrow the rows the caller may already see: the rights
        above are applied first and a filter is one more condition, so `submitter` never shows
        another principal's query to a caller who does not see it. A status that is no longer
        running or queued asks for the ended runs of the last 24 hours, with or without
        `include_ended`. Every
        statement selects columns, never the SQL text. The ended read filters and orders by
        `finished_at` alone, by the index on `(environment_id, finished_at)`: every writer of
        a final status sets it, so a final run without one (none is known) would not appear.

        Reasons (`waiting_for`) are computed for the `QUEUED` rows only. The workers are read
        once, the principals at the limit once and the queued times once per kind of queue, so
        the number of statements does not depend on the number of rows: the clock, the
        environment, at most three reads of runs, and, when a queued row is listed, up to four
        of the workers, one grouped count of running ad-hoc queries (only when a queued row is
        interactive, has a principal and a worker that takes interactive work is free) and the
        queued times (one read while a worker takes
        `all`, else one per kind: two statements for pipeline work, one for interactive work).
        The queued times are the oldest `MAX_AHEAD_COUNTED + 1` of the instance; a row with
        more than that ahead of it says "more than N". In a manager's listing a capped row of
        another principal carries the limit and never the number running.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        include_ended
            Also list the runs that ended in the last 24 hours.
        status
            Only runs in one of these statuses (``queued``, ``running``, ``succeeded``,
            ``failed``, ``cancelled``; any letter case); none means any.
        submitter
            Only runs submitted by the principal with this name.
        limit
            Most rows returned (1 to 200).
        cursor
            The `next_cursor` of the previous page. Send the same filters with every page.

        Returns
        -------
        tuple of (list of QueryListItem, str or None)
            The rows, in the order above, and the cursor of the next page or `None` on the last.

        Raises
        ------
        PermissionDenied
            If the caller may not read queries in this environment (as `get_query_run`).
        NotFoundError
            If the environment does not exist.
        ValidationError
            If `limit` is outside 1 to 200, a status is unknown, or `cursor` is not one this
            server issued for the queries list.
        """
        scope = Securable.environment_scope(environment)
        manager = p.task_scope is None and self._authz.permits(p, Action.MANAGE_ENVIRONMENT, scope)
        if manager:
            self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, scope)
        else:
            self._authz.authorize(p, Action.SUBMIT_QUERY, scope)
        env = self._find_environment(environment)
        if not 1 <= limit <= QUERY_LIST_MAX:
            raise ValidationError(f"limit must be between 1 and {QUERY_LIST_MAX}, not {limit}.")
        statuses = parse_choices("status", status, QueryRunStatus, any_case=True)
        position = None if cursor is None else decode_cursor("queries", cursor)
        visible = [QueryRun.environment_id == env.id]
        if not manager:
            visible.append(QueryRun.principal_id == p.id)
            if p.task_scope is not None:
                visible.append(QueryRun.task_token_id == p.task_scope.token_id)
        if submitter is not None:
            visible.append(Principal.name == submitter)
        now = db_now(self._session)
        started = func.coalesce(QueryRun.started_at, QueryRun.created_at)
        cutoff = now - timedelta(seconds=QUERY_LIST_ENDED_SECONDS)
        final = [m for m in QueryRunStatus if m not in _LIVE_STATUSES]
        wanted_final = [m for m in statuses if m in final]
        # (part, condition, sort time, oldest first); a cursor names the part of its last row
        parts: list[tuple[int, Any, Any, bool]] = []
        if not statuses or QueryRunStatus.RUNNING in statuses:
            parts.append((_RUNNING, QueryRun.status == QueryRunStatus.RUNNING, started, True))
        if not statuses or QueryRunStatus.QUEUED in statuses:
            parts.append(
                (_QUEUED, QueryRun.status == QueryRunStatus.QUEUED, QueryRun.created_at, True)
            )
        if wanted_final or (include_ended and not statuses):
            ended = QueryRun.status.in_(wanted_final or final) & (QueryRun.finished_at >= cutoff)
            parts.append((_ENDED, ended, QueryRun.finished_at, False))
        found: list[tuple[int, Any]] = []
        for part, condition, when, oldest_first in parts:
            if len(found) > limit:
                break
            if position is not None and part < position.part:
                continue
            after = []
            if position is not None and part == position.part:
                after = [
                    or_(
                        (when > position.at) if oldest_first else (when < position.at),
                        and_(when == position.at, QueryRun.id > position.id),
                    )
                ]
            found += [
                (part, row)
                for row in self._session.execute(
                    select(
                        QueryRun.id,
                        Principal.name,
                        QueryRun.write_mode,
                        QueryRun.target_name,
                        QueryRun.status,
                        QueryRun.created_at,
                        QueryRun.started_at,
                        QueryRun.finished_at,
                        QueryRun.error_cause,
                        QueryRun.principal_id,
                        QueryRun.task_run_id,
                    )
                    .outerjoin(Principal, Principal.id == QueryRun.principal_id)
                    .where(*visible, condition, *after)
                    .order_by(when if oldest_first else when.desc(), QueryRun.id)
                    .limit(limit + 1 - len(found))
                ).all()
            ]
        page = found[:limit]
        rows = [row for _, row in page]
        next_cursor = None
        if len(found) > limit:
            part, last = page[-1]
            moment = {
                _RUNNING: last.started_at or last.created_at,
                _QUEUED: last.created_at,
                _ENDED: last.finished_at,
            }[part]
            next_cursor = encode_cursor("queries", moment, last.id, part)
        if manager:
            self._audit_listing(p, environment, rows, include_ended, statuses, submitter, limit)
        reasons = self._reasons_for_listing(rows, now, p.id)
        return [
            QueryListItem(
                id=row.id,
                submitter=row.name,
                kind="read" if row.write_mode is None else row.write_mode.value,
                target=row.target_name,
                status=row.status.value,
                created_at=row.created_at,
                started_at=row.started_at,
                finished_at=row.finished_at,
                error_cause=row.error_cause,
                waiting_for=reasons.get(row.id),
            )
            for row in rows
        ], next_cursor

    def _audit_listing(
        self,
        p: PrincipalRef,
        environment: str,
        rows: Sequence[Any],
        include_ended: bool,
        statuses: Sequence[QueryRunStatus],
        submitter: str | None,
        limit: int,
    ) -> None:
        """Record a manager's page of the query list that shows or targets someone else.

        A page holding only the manager's own runs, with no filter on another submitter, writes
        nothing (`record_view`). The cursor is not part of the view, so the next page of the same
        listing is the same view.

        Parameters
        ----------
        p
            The manager.
        environment
            The environment listed.
        rows
            The rows of the page, with their `principal_id`.
        include_ended
            The `include_ended` filter.
        statuses
            The parsed status filter.
        submitter
            The submitter filter; the targeting key unless it names the manager.
        limit
            The page size.

        Raises
        ------
        AuditUnavailableError
            If the row could not be written; the page is not returned.
        """
        others = [row.principal_id for row in rows if row.principal_id != p.id]
        record_view(
            sessions_like(self._session),
            p,
            View.QUERIES,
            Securable.environment_scope(environment),
            None if submitter is None or submitter == p.name else {"submitter": submitter},
            ViewDetail(
                filters={
                    "include_ended": include_ended,
                    "status": [status.value for status in statuses],
                    "limit": limit,
                },
                rows_shown=len(rows),
                others_shown=len(others),
                distinct_others=len(set(others)),
            ),
            config=self._view_audit,
        )

    def _reasons_for_listing(
        self, rows: Sequence[Any], now: datetime, caller_id: uuid.UUID
    ) -> dict[uuid.UUID, WaitReason]:
        """Compute the reasons of the `QUEUED` rows of a listing from one read of the workers.

        The workers and what they carry are read once; the principals at the limit once, as one
        grouped count, and only when a queued row is interactive, has a principal and a worker
        that takes interactive work is free (the limit is said only then); the principals passed by
        after a lock timeout, in one statement, under the same condition; the queued times once
        per kind of queue the rows need (one while a worker takes `all`). A row of another
        principal (a manager's listing) is not
        the caller's own: its reason carries the limit and never the number running.

        Parameters
        ----------
        rows
            The listed rows (`id`, `status`, `created_at`, `write_mode`, `principal_id`,
            `task_run_id`).
        now
            The database's current time.
        caller_id
            The listing caller's principal id.

        Returns
        -------
        dict
            The reason per queued run id; empty, with no statement, when none is queued.
        """
        queued = [row for row in rows if row.status == QueryRunStatus.QUEUED]
        if not queued:
            return {}
        snapshot = with_work(self._session, alive_workers(self._session, now, self._lease_seconds))
        kinds = {row.id: pools.kind_of(row.task_run_id, row.write_mode) for row in queued}
        counts = {kind: snapshot.for_query(kind) for kind in set(kinds.values())}
        cap = pools.cap_for(self._running_per_principal, snapshot.interactive())
        capped = [
            row
            for row in queued
            if kinds[row.id] == pools.INTERACTIVE
            and row.principal_id is not None
            and counts[pools.INTERACTIVE].free
        ]
        running = pools.running_by_principal(self._session, at_least=cap) if capped else {}
        passed_by = (
            pools.passed_by_until(self._session, {row.principal_id for row in capped}, now)
            if capped
            else {}
        )
        limits = {
            row.id: UserLimit(
                running.get(row.principal_id, 0), cap, own=row.principal_id == caller_id
            )
            for row in capped
        }
        times: dict[str | None, list[datetime]] = {}
        for row in queued:
            if settled_early(counts[kinds[row.id]], limits.get(row.id)):
                continue
            wanted = ahead_kind(snapshot, kinds[row.id])
            if wanted not in times:
                times[wanted] = queued_times(self._session, MAX_AHEAD_COUNTED + 1, kind=wanted)
        return {
            row.id: query_wait_reason(
                counts[kinds[row.id]],
                ahead_of(times.get(ahead_kind(snapshot, kinds[row.id]), []), row.created_at),
                kind=kinds[row.id],
                maintenance=row.write_mode is WriteMode.MAINTAIN,
                limit=limits.get(row.id),
                ahead_cap=MAX_AHEAD_COUNTED,
                passed_by_until=passed_by.get(row.principal_id) if row.id in limits else None,
            )
            for row in queued
        }

    def cancel_query(self, p: PrincipalRef, environment: str, query_id: uuid.UUID) -> QueryRun:
        """Cancel a query run, or ask the worker that runs it to stop it.

        Two kinds of caller may cancel. The run's submitter, under exactly the rule of
        `get_query_run` (a task-token caller: only runs of that token). And a holder of
        `MANAGE_ENVIRONMENT` on the environment, who authenticated with a personal token: a
        task token never takes this path, whatever its principal holds. Everyone else gets
        the answer `get_query_run` gives them, so the route tells nobody whether a run exists.

        A queued run becomes `CANCELLED` here, in one conditional update, so a worker that
        claims it in the same instant finds nothing to start. A running one whose worker is
        alive gets `cancel_requested`; its worker reads the flag about twice a second and
        decides the final status. A running one that no alive worker holds is ended
        here, at once, by the evidence the write left: without it (a read, a write that did not
        commit, maintenance that had started) `CANCELLED`; with it (`rows_written` set) the
        write did commit, so it ends `SUCCEEDED` and the answer says so, never `CANCELLED` over
        data that is there. Such a run is also ended by the scheduler's sweep within a tick.
        On Postgres the cancel waits at most `CANCEL_LOCK_TIMEOUT_SECONDS` for the run's row: a
        worker that dequeued the run and has yet to start it, or one inside its commit, holds
        it. A cancel that cannot take the row in that time changes nothing and is refused with
        a conflict, which the caller can repeat.
        A run that has ended is returned as it is. Each accepted request is audited
        (`QUERY_CANCELLED`), and a run ended here as the worker would have ended it records its
        `QUERY_COMPLETED` once.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment the query was submitted to.
        query_id
            The query run's id.

        Returns
        -------
        QueryRun
            The row after the request: `CANCELLED`, still `RUNNING`, or its final status.

        Raises
        ------
        NotFoundError
            If the environment or the run does not exist, or the caller is neither its
            submitter nor a holder of `MANAGE_ENVIRONMENT`. The refusal for a run is audited
            (`_audit_refused_cancel`).
        PermissionDenied
            If the caller may not read queries in this environment (as `get_query_run`).
        ValidationError
            If the run is the write of a pipeline task or the count of one of its checks (the
            pipeline run is cancelled instead), or a
            maintenance run that has already started under a worker that is alive (it cannot be
            stopped).
        ConflictError
            If the run's row stayed locked for `CANCEL_LOCK_TIMEOUT_SECONDS` because a worker is
            starting the run or finishing it (Postgres only). Nothing was changed.
        DBAPIError
            For any other failure of the database.
        """
        scope = Securable.environment_scope(environment)
        manager = p.task_scope is None and self._authz.permits(p, Action.MANAGE_ENVIRONMENT, scope)
        if manager:
            # `permits` only chose the path; `authorize` is the gate, and what gets audited.
            self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, scope)
            env = self._find_environment(environment)
            run = self._session.execute(
                select(QueryRun).where(QueryRun.id == query_id, QueryRun.environment_id == env.id)
            ).scalar_one_or_none()
            if run is None:
                raise NotFoundError(f"No query run {query_id} in {environment!r}.")
        else:
            try:
                run = self.get_query_run(p, environment, query_id)
            except NotFoundError:
                self._audit_refused_cancel(p, environment, scope)
                raise
        if run.task_run_id is not None and run.task_token_id is None:
            pipeline_run = self._session.execute(
                select(TaskRun.run_id).where(TaskRun.id == run.task_run_id)
            ).scalar_one()
            what = (
                f"counts a check of a task in pipeline run {pipeline_run}; it is not a query of "
                "its own"
                if is_check_count(self._session, run)
                else f"is the write of a task in pipeline run {pipeline_run}, not a query of "
                "its own"
            )
            raise ValidationError(
                f"Query run {query_id} {what}. Cancel that run instead: "
                f"lakana runs cancel {pipeline_run} --env {environment}"
            )
        postgres = pools.on_postgres(self._session)
        try:
            if postgres:
                # Every row-locking statement of the cancel is bounded: the run's row is locked by
                # a worker that dequeued it and has yet to start it, and by one inside its commit.
                self._session.execute(
                    text(f"SET LOCAL lock_timeout = '{int(CANCEL_LOCK_TIMEOUT_SECONDS * 1000)}ms'")  # nosec B608
                )
            cancelled = self._session.execute(
                update(QueryRun)
                .where(QueryRun.id == run.id, QueryRun.status == QueryRunStatus.QUEUED)
                .values(
                    status=QueryRunStatus.CANCELLED,
                    cancel_requested=True,
                    error_message=QUERY_CANCELLED_MESSAGE,
                    error_cause=QueryErrorCause.CANCELLED.value,
                    finished_at=datetime.now(UTC),
                )
                .execution_options(synchronize_session=False)
            )
            was = QueryRunStatus.QUEUED if rowcount(cancelled) == 1 else self._cancel_running(run)
            if postgres:
                # SET LOCAL lasts until the transaction ends; the audit rows and the commit after
                # this are not the statements the bound was meant for.
                self._session.execute(text("SET LOCAL lock_timeout = DEFAULT"))
        except DBAPIError as error:
            if not pools.is_lock_timeout(error):
                raise
            # PostgreSQL aborts the transaction on the error; nothing else runs on it until this.
            self._session.rollback()
            raise ConflictError(
                f"Query {query_id} is being started or is finishing right now, so it cannot be "
                "cancelled at this moment. Check it with: "
                f"lakana query status {query_id} --env {environment}. If it is still running, "
                f"cancel it with: lakana query cancel {query_id} --env {environment}"
            ) from error
        if was is None:
            self._session.rollback()
            self._session.refresh(run)
            if run.status is QueryRunStatus.RUNNING:
                raise ValidationError(
                    f"Query run {query_id} is a maintenance run that has already started. It "
                    "cannot be stopped and ends by itself; follow it with: "
                    f"lakana query status {query_id} --env {environment}"
                )
        else:
            AuditService(self._session).record(
                p.id,
                AuditEventType.QUERY_CANCELLED,
                securable=scope,
                detail={
                    "query_id": str(run.id),
                    "status": was.value,
                    "submitted_by": None if run.principal_id is None else str(run.principal_id),
                },
            )
            self._session.commit()
        self._session.refresh(run)
        return run

    def _cancel_running(self, run: QueryRun) -> QueryRunStatus | None:
        """Cancel or ask for the cancel of a `RUNNING` run, by who holds it and what it left.

        Three conditional updates, each deciding by its own `WHERE`: a run no alive worker holds
        and without write evidence becomes `CANCELLED`; otherwise a run that is not maintenance
        gets `cancel_requested`, and when it has write evidence and no alive worker holds it,
        it is ended `SUCCEEDED` as the sweep ends it (`store._end_query_run`).

        Parameters
        ----------
        run
            The run, checked for authorization and for not being a task's own write.

        Returns
        -------
        QueryRunStatus or None
            `RUNNING` when the request was accepted (the run may have ended meanwhile), `None`
            when the run is not running or is maintenance under an alive worker.
        """
        now = db_now(self._session)
        lost = self._session.execute(
            update(QueryRun)
            .where(
                QueryRun.id == run.id,
                QueryRun.status == QueryRunStatus.RUNNING,
                QueryRun.rows_written.is_(None),
                without_alive_worker(QueryRun, now, self._lease_seconds),
            )
            .values(
                status=QueryRunStatus.CANCELLED,
                cancel_requested=True,
                error_message=QUERY_CANCELLED_MESSAGE,
                error_cause=QueryErrorCause.CANCELLED.value,
                finished_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if rowcount(lost) == 1:
            self._session.refresh(run)
            record_query_completed(
                self._session, run, status=QueryRunStatus.CANCELLED, row_count=None
            )
            return QueryRunStatus.RUNNING
        # Maintenance runs in the worker's own process: while that worker is alive nothing can
        # stop it once it has started, and saying "asked" would be untrue.
        asked = self._session.execute(
            update(QueryRun)
            .where(
                QueryRun.id == run.id,
                QueryRun.status == QueryRunStatus.RUNNING,
                QueryRun.write_mode.is_distinct_from(WriteMode.MAINTAIN),
            )
            .values(cancel_requested=True)
            .execution_options(synchronize_session=False)
        )
        if rowcount(asked) != 1:
            return None
        # No alive worker and the evidence set: the write committed. The sweep's own statement
        # ends it SUCCEEDED (skipping a row a worker is committing at this instant).
        end_lost_run(self._session, run.id, now, self._lease_seconds)
        return QueryRunStatus.RUNNING

    def _audit_refused_cancel(self, p: PrincipalRef, environment: str, scope: Securable) -> None:
        """Record a cancel that was refused as `AUTHORIZATION_DENIED` (reason `not_submitter`).

        One row for "somebody else's run" and "no such run" alike, through the denial
        limiter of an auditing authorization, so the answer and the audit reveal nothing about
        whether the run exists. A missing environment is not a denial and records nothing. The
        row is readable by holders of `READ_AUDIT_LOG`, not by the caller.

        Parameters
        ----------
        p
            The refused caller.
        environment
            The environment named in the request.
        scope
            The environment's securable.
        """
        exists = self._session.execute(
            select(Environment.id).where(Environment.name == environment, LIVE_ENVIRONMENT)
        ).scalar_one_or_none()
        if exists is not None and isinstance(self._authz, AuditingAuthorization):
            self._authz.record_denial(p, Action.MANAGE_ENVIRONMENT, scope, reason="not_submitter")

    def _find_environment(self, environment: str) -> Environment:
        """Look an environment up by name.

        Parameters
        ----------
        environment
            The environment's name.

        Returns
        -------
        Environment
            The row.

        Raises
        ------
        NotFoundError
            If it does not exist.
        """
        env = self._session.execute(
            select(Environment).where(Environment.name == environment, LIVE_ENVIRONMENT)
        ).scalar_one_or_none()
        if env is None:
            raise NotFoundError(f"No environment named {environment!r}.")
        return env

    def submit_write(
        self, p: PrincipalRef, environment: str, sql: str, target: WriteTarget
    ) -> QueryRun:
        """Enqueue a write run: a query whose result goes into a table or view.

        Authorization is checked here and again in the worker:
        SUBMIT_QUERY on the environment; CREATE_TABLE on the target for
        `create` and for a new view; WRITE_TABLE for `replace`, `append`
        and an existing view; READ_TABLE too for an incremental append
        . Each action is authorized before the target's existence
        is revealed. In a protected environment the write needs a break-glass
        reason; it travels on the run and is audited when the worker executes
        it.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment to run in.
        sql
            The SELECT, unmodified.
        target
            Where the result goes.

        Returns
        -------
        QueryRun
            The queued run, with its write fields set.

        Raises
        ------
        ValidationError
            If the principal's break-glass reason is not 10 to 500
            characters (checked in every environment), if `target` fails
            `WriteTarget.validate`, its name is not
            dotted, the target is a table for `mode: view`, or not a
            managed table for `replace`/`append`.
        PermissionDenied
            If any required action is refused, or the environment is protected
            and this write has no authorized break-glass.
        NotFoundError
            If the environment or the target's namespace does not exist, or
            the target of a `replace`/`append` does not.
        AlreadyExistsError
            If `create` names an existing table or view.
        """
        target.validate()
        reason = (
            None
            if p.break_glass_reason is None
            else validate_break_glass_reason(p.break_glass_reason)
        )
        self._authz.authorize(p, Action.SUBMIT_QUERY, Securable.environment_scope(environment))
        env = self._find_environment(environment)
        ref = TableRef.from_dotted(environment, target.table)
        securable = Securable.table_scope(ref.environment, ref.namespace, ref.name)
        joined = ref.namespace.joined()
        table_id = resolve_table_id(self._session, environment, joined, ref.name)
        row = None if table_id is None else self._session.get(Table, table_id)
        if target.mode is WriteMode.CREATE:
            self._authz.authorize(p, Action.CREATE_TABLE, securable)
            if row is not None and row.table_type is TableType.ARTIFACT:
                raise ValidationError(artifact_refusal(ref.dotted(), "a SQL write"))
            if row is not None:
                raise AlreadyExistsError(
                    f"{ref.dotted()} already exists; use mode replace or append."
                )
        elif target.mode is WriteMode.VIEW:
            if row is not None and row.table_type is TableType.VIEW:
                self._authz.authorize(p, Action.WRITE_TABLE, securable)
            else:
                self._authz.authorize(p, Action.CREATE_TABLE, securable)
                if row is not None and row.table_type is TableType.ARTIFACT:
                    raise ValidationError(artifact_refusal(ref.dotted(), "a SQL write"))
                if row is not None:
                    raise ValidationError(f"{ref.dotted()} is a table; a view can't replace it.")
        else:
            self._authz.authorize(p, Action.WRITE_TABLE, securable)
            if target.incremental_key is not None:
                self._authz.authorize(p, Action.READ_TABLE, securable)
            if row is None:
                raise NotFoundError(f"Table {ref.dotted()!r} not found.")
            if row.table_type is TableType.ARTIFACT:
                raise ValidationError(artifact_refusal(ref.dotted(), "a SQL write"))
            if row.table_type is not TableType.ICEBERG:
                raise ValidationError(f"{ref.dotted()} is not a managed table.")
        self._guard.check(
            p, environment, WriteOperation.SQL_WRITE, table_target(ref), record_break_glass=False
        )
        if row is None and not namespace_exists(self._session, environment, joined):
            raise NotFoundError(f"Namespace {ref.namespace} not found in {environment!r}.")
        run = QueryRun(
            id=uuid.uuid4(),
            environment_id=env.id,
            principal_id=p.id,
            sql_text=sql,
            status=QueryRunStatus.QUEUED,
            task_token_id=p.task_scope.token_id if p.task_scope is not None else None,
            task_run_id=_bound_task_run_id(p),
            write_mode=target.mode,
            target_name=ref.dotted(),
            target_table_id=None if row is None else row.id,
            incremental_key=target.incremental_key,
            evolve_schema=target.evolve_schema,
            break_glass_reason=reason,
        )
        self._session.add(run)
        self._session.commit()
        return run

    def submit_maintenance(
        self,
        p: PrincipalRef,
        environment: str,
        table: str,
        *,
        keep_days: int = DEFAULT_KEEP_DAYS,
        keep_last: int = DEFAULT_KEEP_LAST,
        dry_run: bool = False,
        older_than_hours: int = DEFAULT_ORPHAN_AGE_HOURS,
    ) -> QueryRun:
        """Enqueue a maintenance run for one table.

        A `QueryRun` with `write_mode = MAINTAIN`, so it shares the query
        runs' status and polling. It needs MAINTAIN_TABLE on the table and
        SUBMIT_QUERY on the environment, since the run is read back through
        the queries API. Its `sql_text` is a comment naming the
        table; the worker never parses it.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment.
        table
            The table's dotted name.
        keep_days
            Snapshots younger than this many days are kept.
        keep_last
            The most recent this many snapshots are kept.
        dry_run
            Report without changing anything.
        older_than_hours
            Unreferenced files younger than this are kept (at least 1, at
            most `MAX_ORPHAN_AGE_HOURS`).

        Returns
        -------
        QueryRun
            The queued run.

        Raises
        ------
        ValidationError
            If `keep_days` is negative, `keep_last` below 1,
            `older_than_hours` outside 1 to `MAX_ORPHAN_AGE_HOURS`, or
            `table` not dotted, or `table` is an artifact, which has
            nothing to maintain.
        PermissionDenied
            If `p` lacks SUBMIT_QUERY or MAINTAIN_TABLE.
        NotFoundError
            If the environment or table does not exist.
        """
        if keep_days < 0 or keep_last < 1:
            raise ValidationError("keep_days must be at least 0 and keep_last at least 1.")
        check_orphan_age(older_than_hours)
        self._authz.authorize(p, Action.SUBMIT_QUERY, Securable.environment_scope(environment))
        env = self._find_environment(environment)
        ref = TableRef.from_dotted(environment, table)
        self._authz.authorize(
            p,
            Action.MAINTAIN_TABLE,
            Securable.table_scope(ref.environment, ref.namespace, ref.name),
        )
        table_id = resolve_table_id(self._session, environment, ref.namespace.joined(), ref.name)
        if table_id is None:
            raise NotFoundError(f"Table {ref.dotted()!r} not found.")
        row = self._session.get(Table, table_id)
        if row is not None and row.table_type is TableType.ARTIFACT:
            raise ValidationError(artifact_refusal(ref.dotted(), "maintenance"))
        run = QueryRun(
            id=uuid.uuid4(),
            environment_id=env.id,
            principal_id=p.id,
            sql_text=f"-- maintain {ref.dotted()}",
            status=QueryRunStatus.QUEUED,
            task_token_id=p.task_scope.token_id if p.task_scope is not None else None,
            task_run_id=_bound_task_run_id(p),
            write_mode=WriteMode.MAINTAIN,
            target_name=ref.dotted(),
            target_table_id=table_id,
            maintenance={
                "request": {
                    "keep_days": keep_days,
                    "keep_last": keep_last,
                    "dry_run": dry_run,
                    "older_than_hours": older_than_hours,
                }
            },
        )
        self._session.add(run)
        self._session.commit()
        return run
