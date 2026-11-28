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

from sqlalchemy import Select, String, and_, false, func, or_, select, text, type_coerce, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.service import AuditService
from lakana_core.audit.views import View, ViewDetail, record_view, sessions_like
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, permits_each
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
    QueryOrigin,
    QueryRun,
    QueryRunStatus,
    Table,
    TableType,
    TaskRun,
    WriteMode,
)
from lakana_core.pagination import (
    ListSort,
    Position,
    SortKey,
    decode_cursor,
    encode_cursor,
    encode_sorted_cursor,
    keyset_sorted,
    parse_choices,
    parse_sort,
    read_sorted_position,
)
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
"""How long a run stays in `list_queries(include_ended=True)` after it ended: 24 hours, unless the
caller names an older `since`."""

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
    environment
        The name of the run's environment.
    origin
        Where the run came from: a `QueryOrigin` value.
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
    environment: str
    origin: str
    submitter: str | None
    kind: str
    target: str | None
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_cause: str | None
    waiting_for: WaitReason | None


@dataclass(frozen=True)
class QueryEnvironment:
    """An environment where the caller may submit a query (`QueryService.query_environments`).

    Parameters
    ----------
    name
        The environment's name.
    protected
        Whether the environment is protected (`Environment.is_protected`).
    """

    name: str
    protected: bool


SINCE_WITHOUT_ZONE = (
    "`since` has no time zone. Write it with one, for example 2026-10-18T00:00:00Z "
    "or 2026-10-18T00:00:00+02:00."
)
"""Why a query list refuses a `since` without a time zone."""


QUERY_SORTS: dict[str, bool] = {"created": True, "status": False}
"""The fields a query list sorts by instead of its own order (running, queued, then ended): when
the query was submitted, the newest first, or its state from A, one state's newest first."""


def _query_sort_keys(field: str) -> tuple[SortKey, ...]:
    """Give the keys a query list is sorted by in a field.

    Parameters
    ----------
    field
        A key of `QUERY_SORTS`.

    Returns
    -------
    tuple of SortKey
        ``created``: the time submitted. ``status``: the state as stored, then the newest
        first.
    """
    if field == "created":
        return (SortKey(QueryRun.created_at, datetime),)
    return (
        SortKey(type_coerce(QueryRun.status, String), str),
        SortKey(QueryRun.created_at, datetime, descending=True),
    )


@dataclass(frozen=True)
class _ListFilters:
    """The parsed filters of a query list, with its page's size and position.

    Parameters
    ----------
    kind
        The list, as its cursor names it: ``queries`` or ``all-queries``.
    include_ended
        Also list the ended runs.
    statuses
        The parsed status filter.
    origins
        The parsed origin filter.
    submitter
        The submitter filter.
    since
        The oldest end time of a listed ended run (UTC), or `None` for the last 24 hours.
    limit
        The page size.
    position
        Where the previous page ended, or `None` for the first page; in the list's own order.
    sort
        The sort the list is read in; `None` for its own order.
    cursor
        The `next_cursor` of the previous page as sent, read in `sort` by `_read_sorted`.
    """

    kind: str
    include_ended: bool
    statuses: tuple[QueryRunStatus, ...]
    origins: tuple[QueryOrigin, ...]
    submitter: str | None
    since: datetime | None
    limit: int
    position: Position | None
    sort: ListSort | None = None
    cursor: str | None = None

    @classmethod
    def parse(
        cls,
        kind: str,
        *,
        include_ended: bool,
        status: Sequence[str],
        submitter: str | None,
        origin: Sequence[str],
        since: datetime | None,
        limit: int,
        cursor: str | None,
        sort: str | None = None,
        order: str | None = None,
    ) -> _ListFilters:
        """Check and parse the filters of a query list.

        Parameters
        ----------
        kind
            The list, as its cursor names it.
        include_ended
            Also list the ended runs; a `since` implies it.
        status
            The statuses, as sent (any letter case).
        submitter
            The submitter's name.
        origin
            The origins, as sent.
        since
            The oldest end time of a listed ended run; it must have a time zone.
        limit
            The page size.
        cursor
            The `next_cursor` of the previous page.
        sort
            The field the list is sorted by (`QUERY_SORTS`); with `order` also `None`, the
            list's own order.
        order
            ``asc`` or ``desc``; the field's own direction without it.

        Returns
        -------
        _ListFilters
            The filters.

        Raises
        ------
        ValidationError
            If `limit` is outside 1 to 200, a status or an origin is unknown, `since` has no
            time zone, `sort` or `order` is unknown, or `cursor` is not one this server issued
            for the list `kind` in its own order (a sorted cursor is read later).
        """
        if not 1 <= limit <= QUERY_LIST_MAX:
            raise ValidationError(f"limit must be between 1 and {QUERY_LIST_MAX}, not {limit}.")
        statuses = parse_choices("status", status, QueryRunStatus, any_case=True)
        origins = parse_choices("origin", origin, QueryOrigin)
        if since is not None and since.tzinfo is None:
            raise ValidationError(SINCE_WITHOUT_ZONE)
        chosen = (
            None
            if sort is None and order is None
            else parse_sort(sort, order, QUERY_SORTS, "created")
        )
        return cls(
            kind=kind,
            # A `since` asks for the runs that ended since then; it never does nothing.
            include_ended=include_ended or since is not None,
            statuses=statuses,
            origins=origins,
            submitter=submitter,
            since=None if since is None else since.astimezone(UTC),
            limit=limit,
            position=None if cursor is None or chosen is not None else decode_cursor(kind, cursor),
            sort=chosen,
            cursor=cursor,
        )


def _listed_columns() -> Select[*tuple[Any, ...]]:
    """Start the select of a query list's rows: their columns, never the SQL.

    Returns
    -------
    Select
        The columns of `QueryRun` a list shows, its environment's name and its submitter's.
    """
    return (
        select(
            QueryRun.id,
            QueryRun.environment_id,
            Environment.name.label("environment"),
            QueryRun.origin,
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
        .join(Environment, Environment.id == QueryRun.environment_id)
        .outerjoin(Principal, Principal.id == QueryRun.principal_id)
    )


def _own_runs(p: PrincipalRef) -> list[Any]:
    """Return the conditions that keep a caller's own runs: their principal's, or their token's.

    Parameters
    ----------
    p
        The acting principal.

    Returns
    -------
    list
        `principal_id` is the caller's and, for a task token, `task_token_id` is that token.
    """
    own: list[Any] = [QueryRun.principal_id == p.id]
    if p.task_scope is not None:
        own.append(QueryRun.task_token_id == p.task_scope.token_id)
    return own


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

HISTORY_IS_NOT_A_QUERY_MODE = (
    "history is written by assets (`write: history`); a query writes with create, replace or append"
)
"""Why a write run refuses `history`: the key, the versions and the checks belong to an asset."""


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
            If `mode` is `MAINTAIN`, `MERGE` or `HISTORY` (assets, `tables.merge` and history
            assets write those, a query does not), `incremental_key` is set for a mode other
            than `APPEND`, or `evolve_schema` for a mode other than `REPLACE`/`APPEND`.
        """
        if self.mode is WriteMode.MAINTAIN:
            raise ValidationError("maintain is not a query target mode.")
        if self.mode is WriteMode.MERGE:
            raise ValidationError(MERGE_IS_NOT_A_QUERY_MODE)
        if self.mode is WriteMode.HISTORY:
            raise ValidationError(HISTORY_IS_NOT_A_QUERY_MODE)
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
            origin=QueryOrigin.WRITTEN if p.task_scope is None else QueryOrigin.TASK,
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

    def record_export(
        self,
        p: PrincipalRef,
        environment: str,
        run: QueryRun,
        result_format: Literal["arrow", "csv", "xlsx"],
        *,
        rows: int | None,
        size_bytes: int | None,
    ) -> None:
        """Record a whole-result download as `QUERY_RESULT_EXPORTED`, committed.

        The caller has already read the run with `get_query_run` and decided every refusal,
        and calls this before the first byte is sent. A JSON page is not an export and is never
        recorded. Nor is an Arrow download by a token bound to a task attempt, as for
        `ARTIFACT_DOWNLOADED`: a pipeline task reads its inputs as Arrow on every run, which
        is not a person exporting data. That token's CSV or spreadsheet download is recorded
        like anyone's.

        Parameters
        ----------
        p
            The downloading principal.
        environment
            The run's environment; the event's securable.
        run
            The run whose result is downloaded.
        result_format
            ``arrow``, ``csv`` or ``xlsx``.
        rows
            The result's rows, from the run or else from the result itself; `None` if neither
            is known.
        size_bytes
            The stored Arrow file's length, from the run or else from the file read; `None` if
            neither is known.
        """
        if result_format == "arrow" and p.task_scope is not None and p.task_scope.bound is not None:
            return
        AuditService(self._session).record(
            p.id,
            AuditEventType.QUERY_RESULT_EXPORTED,
            securable=Securable.environment_scope(environment),
            detail={
                "query_id": str(run.id),
                "format": result_format,
                "rows": rows,
                "size_bytes": size_bytes,
            },
            commit=True,
        )

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
        origin: Sequence[str] = (),
        since: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
        sort: str | None = None,
        order: str | None = None,
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
        in the last `QUERY_LIST_ENDED_SECONDS` (or at or after `since`), newest first; `limit`
        cuts that order, and ties on the time are broken by id. A page continues strictly after
        the last row of the previous one (`cursor`), in that order. A query that changes status
        between two pages moves to another part of the order, so a long listing of live queries
        can miss or repeat it; the ended part never changes.

        `status`, `submitter` and `origin` only narrow the rows the caller may already see: the
        rights above are applied first and a filter is one more condition, so `submitter` never
        shows another principal's query to a caller who does not see it. A status that is no
        longer running or queued asks for the ended runs, with or without `include_ended`.
        Every statement selects columns, never the SQL text. The ended read filters and orders
        by `finished_at` alone, by the index on `(environment_id, finished_at)`: every writer of
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
            Also list the runs that ended in the last 24 hours (or at or after `since`).
        status
            Only runs in one of these statuses (``queued``, ``running``, ``succeeded``,
            ``failed``, ``cancelled``; any letter case); none means any.
        submitter
            Only runs submitted by the principal with this name.
        origin
            Only runs of one of these origins (``written``, ``preview``, ``check``, ``write``,
            ``maintenance``, ``task``); none means any.
        since
            The ended runs listed are those that ended at or after this time, of any age,
            instead of those of the last 24 hours; given, they are listed with or without
            `include_ended`. It must have a time zone.
        limit
            Most rows returned (1 to 200).
        cursor
            The `next_cursor` of the previous page. Send the same filters with every page.
        sort
            ``created`` (when submitted) or ``status``, in one order across the parts; with
            `order` also `None`, the list's own order.
        order
            ``asc`` or ``desc``; by default ``desc`` for ``created``, ``asc`` for ``status``.

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
            If `limit` is outside 1 to 200, a status or an origin is unknown, `since` has no
            time zone, or `cursor` is not one this server issued for the queries list.
        """
        scope = Securable.environment_scope(environment)
        manager = p.task_scope is None and self._authz.permits(p, Action.MANAGE_ENVIRONMENT, scope)
        if manager:
            self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, scope)
        else:
            self._authz.authorize(p, Action.SUBMIT_QUERY, scope)
        env = self._find_environment(environment)
        filters = _ListFilters.parse(
            "queries",
            include_ended=include_ended,
            status=status,
            submitter=submitter,
            origin=origin,
            since=since,
            limit=limit,
            cursor=cursor,
            sort=sort,
            order=order,
        )
        visible = [QueryRun.environment_id == env.id]
        if not manager:
            visible += _own_runs(p)
        rows, next_cursor, now = self._read_list(filters, visible)
        if manager:
            self._audit_listing(p, environment, rows, filters, submitter)
        return self._list_items(rows, now, p), next_cursor

    # A public method states its own parameters: these are those of `list_queries_page`, plus
    # `environment`.
    # jscpd:ignore-start
    def list_all_queries_page(
        self,
        p: PrincipalRef,
        *,
        environment: str | None = None,
        include_ended: bool = False,
        status: Sequence[str] = (),
        submitter: str | None = None,
        origin: Sequence[str] = (),
        since: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> tuple[list[QueryListItem], str | None]:
        # jscpd:ignore-end
        """List the queries of every environment the caller may query, one page at a time.

        With `environment`, this is `list_queries_page` of that environment: its rows, its
        cursor and its refusals. Without it, the environments are every live environment where
        the caller holds `SUBMIT_QUERY` or, with a personal token, `MANAGE_ENVIRONMENT`, asked
        in one `permits_each` batch; one where the caller holds neither is left out, so the
        list is never refused. A row is visible by the rule of `list_queries_page` for its own
        environment: everyone's where the caller manages it, the caller's own elsewhere, and a
        task token's own runs only. That rule is one condition of the statement. The order and
        the parts are those of `list_queries_page`, over all those environments together, in
        one statement per part; the cursor belongs to the ``all-queries`` list, so a cursor of
        the per-environment list is refused. A manager's page is audited for each environment
        they manage as `list_queries_page` audits it (`_audit_listing`): an `ACTIVITY_VIEWED`
        row for each environment whose rows on the page include someone else's, or whose
        listing targets someone else with `submitter`.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Only this environment, with the answers and refusals of `list_queries_page`.
        include_ended
            Also list the runs that ended in the last 24 hours (or at or after `since`).
        status
            Only runs in one of these statuses (any letter case); none means any.
        submitter
            Only runs submitted by the principal with this name.
        origin
            Only runs of one of these origins; none means any.
        since
            The ended runs listed are those that ended at or after this time, of any age,
            whether `include_ended` is set or not. A time zone is required.
        limit
            Most rows returned (1 to 200).
        cursor
            The `next_cursor` of the previous page. Send the same filters with every page.
        sort
            ``created`` (when submitted) or ``status``, in one order across the parts; with
            `order` also `None`, the list's own order.
        order
            ``asc`` or ``desc``; by default ``desc`` for ``created``, ``asc`` for ``status``.

        Returns
        -------
        tuple of (list of QueryListItem, str or None)
            The rows, and the cursor of the next page or `None` on the last.

        Raises
        ------
        PermissionDenied
            With `environment` only, as `list_queries_page`.
        NotFoundError
            With `environment` only, if it does not exist.
        ValidationError
            If `limit` is outside 1 to 200, a status or an origin is unknown, `since` has no
            time zone, or `cursor` is not one this server issued for this list.
        AuditUnavailableError
            If a manager's page could not be audited; the page is not returned.
        """
        if environment is not None:
            return self.list_queries_page(
                p,
                environment,
                include_ended=include_ended,
                status=status,
                submitter=submitter,
                origin=origin,
                since=since,
                limit=limit,
                cursor=cursor,
                sort=sort,
                order=order,
            )
        filters = _ListFilters.parse(
            "all-queries",
            include_ended=include_ended,
            status=status,
            submitter=submitter,
            origin=origin,
            since=since,
            limit=limit,
            cursor=cursor,
            sort=sort,
            order=order,
        )
        managed, own_only = self._queryable_environments(p)
        shown = []
        if managed:
            shown.append(QueryRun.environment_id.in_(list(managed)))
        if own_only:
            shown.append(and_(QueryRun.environment_id.in_(own_only), *_own_runs(p)))
        if not shown:
            return [], None
        rows, next_cursor, now = self._read_list(filters, [or_(*shown)])
        for env_id, name in managed.items():
            self._audit_listing(
                p, name, [row for row in rows if row.environment_id == env_id], filters, submitter
            )
        return self._list_items(rows, now, p), next_cursor

    def query_environments(self, p: PrincipalRef) -> list[QueryEnvironment]:
        """List the environments where the caller may submit a query, by name.

        The live environments are read once and the caller's `SUBMIT_QUERY` on each is asked
        in one `permits_each` batch.

        Parameters
        ----------
        p
            The acting principal.

        Returns
        -------
        list of QueryEnvironment
            Each one's name and whether it is protected, sorted by name; empty when there is
            none.
        """
        envs = self._session.execute(
            select(Environment.name, Environment.is_protected)
            .where(LIVE_ENVIRONMENT)
            .order_by(Environment.name)
        ).all()
        if not envs:
            return []
        allowed = permits_each(
            self._authz,
            p,
            [(Action.SUBMIT_QUERY, Securable.environment_scope(env.name)) for env in envs],
        )
        return [
            QueryEnvironment(name=env.name, protected=bool(env.is_protected))
            for env, ok in zip(envs, allowed, strict=True)
            if ok
        ]

    def _queryable_environments(
        self, p: PrincipalRef
    ) -> tuple[dict[uuid.UUID, str], list[uuid.UUID]]:
        """Sort the live environments by what the caller may see of their queries.

        Every question is asked in one `permits_each` batch: `SUBMIT_QUERY` on each
        environment and, for a personal token only, `MANAGE_ENVIRONMENT` too. A task token
        never takes the manager's path, as in `list_queries_page`.

        Parameters
        ----------
        p
            The acting principal.

        Returns
        -------
        tuple of (dict, list)
            The names of the environments the caller manages, by id, and the ids of those
            where they see their own runs only. An environment in neither is left out.
        """
        envs = self._session.execute(
            select(Environment.id, Environment.name).where(LIVE_ENVIRONMENT)
        ).all()
        personal = p.task_scope is None
        questions: list[tuple[Action, Securable]] = []
        for env in envs:
            scope = Securable.environment_scope(env.name)
            questions.append((Action.SUBMIT_QUERY, scope))
            if personal:
                questions.append((Action.MANAGE_ENVIRONMENT, scope))
        answers = iter(permits_each(self._authz, p, questions) if questions else [])
        managed: dict[uuid.UUID, str] = {}
        own_only: list[uuid.UUID] = []
        for env in envs:
            may_submit = next(answers)
            if personal and next(answers):
                managed[env.id] = env.name
            elif may_submit:
                own_only.append(env.id)
        return managed, own_only

    def _read_list(
        self, filters: _ListFilters, visible: Sequence[Any]
    ) -> tuple[list[Any], str | None, datetime]:
        """Read one page of a query list: running, queued, then ended, one statement per part.

        Parameters
        ----------
        filters
            The parsed filters, the page's size and position, and the list's kind.
        visible
            The conditions that keep the rows the caller may see.

        Returns
        -------
        tuple of (list, str or None, datetime)
            The page's rows (columns, never the SQL), the cursor of the next page or `None`,
            and the database's current time.
        """
        now = db_now(self._session)
        conditions = list(visible)
        if filters.submitter is not None:
            conditions.append(Principal.name == filters.submitter)
        if filters.origins:
            conditions.append(QueryRun.origin.in_(filters.origins))
        started = func.coalesce(QueryRun.started_at, QueryRun.created_at)
        cutoff = filters.since or now - timedelta(seconds=QUERY_LIST_ENDED_SECONDS)
        statuses = filters.statuses
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
        if wanted_final or (filters.include_ended and not statuses):
            ended = QueryRun.status.in_(wanted_final or final) & (QueryRun.finished_at >= cutoff)
            parts.append((_ENDED, ended, QueryRun.finished_at, False))
        if filters.sort is not None:
            rows, next_cursor = self._read_sorted(
                filters, filters.sort, conditions, [c for _, c, _, _ in parts]
            )
            return rows, next_cursor, now
        limit, position = filters.limit, filters.position
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
                    _listed_columns()
                    .where(*conditions, condition, *after)
                    .order_by(when if oldest_first else when.desc(), QueryRun.id)
                    .limit(limit + 1 - len(found))
                ).all()
            ]
        page = found[:limit]
        next_cursor = None
        if len(found) > limit:
            part, last = page[-1]
            moment = {
                _RUNNING: last.started_at or last.created_at,
                _QUEUED: last.created_at,
                _ENDED: last.finished_at,
            }[part]
            next_cursor = encode_cursor(filters.kind, moment, last.id, part)
        return [row for _, row in page], next_cursor, now

    def _read_sorted(
        self,
        filters: _ListFilters,
        sort: ListSort,
        conditions: Sequence[Any],
        parts: Sequence[Any],
    ) -> tuple[list[Any], str | None]:
        """Read one page of a query list in a sort of the caller's choice, in one statement.

        The rows are those of the list's own order (every part's condition, the same visible
        rows and filters); only their sequence differs.

        Parameters
        ----------
        filters
            The parsed filters, with the cursor as sent.
        sort
            The sort.
        conditions
            The conditions that keep the rows the caller may see, and the filters.
        parts
            The condition of each part of the list's own order that is listed.

        Returns
        -------
        tuple of (list, str or None)
            The page's rows and the cursor of the next page, or `None` on the last.

        Raises
        ------
        ValidationError
            If the cursor is not one this server issued for this list in this sort.
        """
        keys = _query_sort_keys(sort.field)
        at = (
            None
            if filters.cursor is None
            else read_sorted_position(filters.kind, sort, filters.cursor, keys)
        )
        query = _listed_columns().where(*conditions, or_(false(), *parts))
        ordered = keyset_sorted(query, keys, QueryRun.id, sort.descending, at)
        rows = list(self._session.execute(ordered.limit(filters.limit + 1)).all())
        page = rows[: filters.limit]
        next_cursor = None
        if len(rows) > filters.limit:
            last = page[-1]
            width = len(last) - len(keys) - 1
            next_cursor = encode_sorted_cursor(filters.kind, sort.name, last[width:-1], last[-1])
        return page, next_cursor

    def _list_items(
        self, rows: Sequence[Any], now: datetime, p: PrincipalRef
    ) -> list[QueryListItem]:
        """Turn the rows of a query list into its items, with the reasons of the queued ones.

        Parameters
        ----------
        rows
            The rows `_read_list` read.
        now
            The database's current time.
        p
            The listing caller.

        Returns
        -------
        list of QueryListItem
            One item per row, in order.
        """
        reasons = self._reasons_for_listing(rows, now, p.id)
        return [
            QueryListItem(
                id=row.id,
                environment=row.environment,
                origin=row.origin.value,
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
        ]

    def _audit_listing(
        self,
        p: PrincipalRef,
        environment: str,
        rows: Sequence[Any],
        filters: _ListFilters,
        submitter: str | None,
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
        filters
            The parsed filters of the page. The detail records what the page shows: the
            effective `include_ended` (true when `since` widened it), the statuses, the origins,
            `since` in ISO 8601 UTC (left out when none) and the page size.
        submitter
            The submitter filter; the targeting key unless it names the manager.

        Raises
        ------
        AuditUnavailableError
            If the row could not be written; the page is not returned.
        """
        others = [row.principal_id for row in rows if row.principal_id != p.id]
        shown: dict[str, object] = {
            "include_ended": filters.include_ended,
            "status": [status.value for status in filters.statuses],
            "origin": [origin.value for origin in filters.origins],
        }
        if filters.since is not None:
            shown["since"] = filters.since.isoformat()
        shown["limit"] = filters.limit
        record_view(
            sessions_like(self._session),
            p,
            View.QUERIES,
            Securable.environment_scope(environment),
            None if submitter is None or submitter == p.name else {"submitter": submitter},
            ViewDetail(
                filters=shown,
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
            origin=QueryOrigin.WRITE if p.task_scope is None else QueryOrigin.TASK,
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
            origin=QueryOrigin.MAINTENANCE,
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
