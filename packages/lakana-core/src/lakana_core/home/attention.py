"""Needs your attention: what failed or is late, for Home, filtered by what the person may read now.

An administrator (`holds_instance_admin`) sees the jobs whose newest finished run failed, the late
tables, the checks whose newest result failed or errored, the open recovery links and the lost
workers. Anyone else sees the late tables and failed checks among the tables they read
(`table_reads`) or opened (`recent_opens`), and their own failed ad-hoc queries. Each row is
checked when the list is read, so a revoked grant or a dropped table makes its row disappear,
never a refusal. Every check is `permits`, which records nothing: reading a list is not an
attempt to do anything.

Each source reads at most `CANDIDATE_LIMIT` rows, newest first, in a few indexed statements;
the rows are then filtered, merged and cut to `ATTENTION_LIMIT`. A row's title is a name (a
job, a table, a check, a person, a host, or ``Query`` and the first eight characters of its
id), never a value, an error text or SQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.catalog.health_read import newest_freshness
from lakana_core.catalog.health_record import NOT_SET_BACK
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.db import db_now
from lakana_core.errors import ValidationError
from lakana_core.grants.admin import holds_instance_admin
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.identity.links import open_links
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    CheckOutcome,
    CheckResult,
    Environment,
    Namespace,
    Principal,
    QueryRun,
    QueryRunStatus,
    RecentOpen,
    Repository,
    Run,
    RunStatus,
    Table,
    TableHealthSignal,
    TableRead,
    Worker,
)
from lakana_core.runs.hidden_checks import ReferenceCheckFilter
from lakana_core.runs.workers import alive_ids

ATTENTION_WINDOW = timedelta(days=7)
"""How far back a failure, a check result or a read counts."""

ATTENTION_LIMIT = 50
"""The most rows the list holds; `AttentionService.for_caller` says when it cut more."""

CANDIDATE_LIMIT = 200
"""The most rows read from one source before filtering."""

AttentionKind = Literal[
    "run_failed", "table_late", "check_failed", "query_failed", "recovery_link_open", "worker_lost"
]
"""What a row reports."""

_FINISHED = (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)
_CHECK_FAILED = (CheckOutcome.FAILED, CheckOutcome.ERRORED)


@dataclass(frozen=True)
class AttentionItem:
    """One row of Needs your attention.

    Parameters
    ----------
    kind
        What it reports.
    environment
        The environment's name; `None` for a recovery link and a worker.
    title
        The object's name: a job (or the repository, for a run of the whole repository), a
        dotted table name, a check, ``Query <first 8 of id>``, the person a recovery link
        signs in, a worker's host.
    ref
        The object the row opens: a run's id (for a failed run and a failed check), a dotted
        table name, a query's id, a recovery link's id, a worker's id.
    at
        When it happened: the run or check ended, the table was found late, the query was
        submitted, the link was issued, the worker last reported.
    """

    kind: AttentionKind
    environment: str | None
    title: str
    ref: str
    at: datetime


@dataclass(frozen=True)
class _Table:
    """A table's names.

    Parameters
    ----------
    environment
        Its environment's name.
    ref
        Its reference, for its read right.
    """

    environment: str
    ref: TableRef


class AttentionService:
    """Compose Needs your attention for the caller.

    Parameters
    ----------
    session
        Database session; read only.
    authz
        Authorization service, asked with `permits` only.
    lease_seconds
        The lease length (`LAKANA_RUN__LEASE_SECONDS`): a worker whose heartbeat is older is
        lost, as `lakana_core.runs.workers` defines alive.
    """

    def __init__(
        self, session: Session, authz: AuthorizationService, *, lease_seconds: int
    ) -> None:
        self._session = session
        self._authz = authz
        self._lease_seconds = lease_seconds
        self._asked: dict[tuple[Action, str], bool] = {}

    def for_caller(self, p: PrincipalRef) -> tuple[list[AttentionItem], bool]:
        """List what needs the caller's attention.

        Parameters
        ----------
        p
            The caller.

        Returns
        -------
        tuple of (list of AttentionItem, bool)
            At most `ATTENTION_LIMIT` rows, newest first, and whether more were cut.
        """
        self._asked = {}
        now = db_now(self._session)
        since = now - ATTENTION_WINDOW
        if holds_instance_admin(self._session, p.id):
            items = [
                *self._failed_runs(since),
                *self._late_tables(None),
                *self._failed_checks(p, since, None),
                *self._recovery_links(now),
                *self._lost_workers(now),
            ]
        else:
            tables = self._readable_tables(p, since)
            items = [
                *self._late_tables(tables),
                *self._failed_checks(p, since, tables),
                *self._failed_queries(p, since),
            ]
        items.sort(key=lambda item: (item.at, item.kind, item.ref), reverse=True)
        return items[:ATTENTION_LIMIT], len(items) > ATTENTION_LIMIT

    def _permits(self, p: PrincipalRef, action: Action, environment: str) -> bool:
        """Ask once per environment whether the caller holds an environment right.

        Parameters
        ----------
        p
            The caller.
        action
            The right.
        environment
            The environment's name.

        Returns
        -------
        bool
            Whether `permits` holds.
        """
        key = (action, environment)
        if key not in self._asked:
            self._asked[key] = self._authz.permits(
                p, action, Securable.environment_scope(environment)
            )
        return self._asked[key]

    def _may_read(self, p: PrincipalRef, table: _Table) -> bool:
        """Say whether the caller may read a table now.

        Parameters
        ----------
        p
            The caller.
        table
            The table.

        Returns
        -------
        bool
            Whether `permits(READ_TABLE)` holds.
        """
        scope = Securable.table_scope(table.environment, table.ref.namespace, table.ref.name)
        return self._authz.permits(p, Action.READ_TABLE, scope)

    def _names(self, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, _Table]:
        """Read the names of tables in live environments.

        Parameters
        ----------
        ids
            The tables.

        Returns
        -------
        dict of uuid.UUID to _Table
            The tables found; a dropped table, or one of a deleted environment, is missing.
        """
        wanted = list(set(ids))
        if not wanted:
            return {}
        rows = self._session.execute(
            select(Table.id, Table.name, Namespace.path, Environment.name)
            .join(Namespace, Namespace.id == Table.namespace_id)
            .join(Environment, Environment.id == Namespace.environment_id)
            .where(Table.id.in_(wanted), LIVE_ENVIRONMENT)
        ).all()
        return {
            table_id: _Table(
                environment=env,
                ref=TableRef(environment=env, namespace=NamespacePath.parse(path), name=name),
            )
            for table_id, name, path, env in rows
        }

    def _readable_tables(self, p: PrincipalRef, since: datetime) -> dict[uuid.UUID, _Table]:
        """Find the tables the caller read within the window or opened, and may read now.

        Parameters
        ----------
        p
            The caller.
        since
            The window's start.

        Returns
        -------
        dict of uuid.UUID to _Table
            The tables, by id.
        """
        last = func.max(TableRead.last_read_at)
        read = self._session.execute(
            select(TableRead.table_id)
            .where(TableRead.principal_id == p.id, TableRead.day >= since.date())
            .group_by(TableRead.table_id)
            .order_by(last.desc())
            .limit(CANDIDATE_LIMIT)
        ).scalars()
        ids = set(read)
        opened = self._session.execute(
            select(Environment.name, RecentOpen.ref)
            .join(Environment, Environment.id == RecentOpen.environment_id)
            .where(RecentOpen.principal_id == p.id, RecentOpen.kind == "table", LIVE_ENVIRONMENT)
        ).all()
        for env, dotted in opened:
            try:
                ref = TableRef.from_dotted(env, dotted)
            except ValidationError:
                continue
            found = resolve_table_id(self._session, env, ref.namespace.joined(), ref.name)
            if found is not None:
                ids.add(found)
        return {
            table_id: table
            for table_id, table in self._names(ids).items()
            if self._may_read(p, table)
        }

    def _late_tables(self, tables: dict[uuid.UUID, _Table] | None) -> Iterator[AttentionItem]:
        """List the tables whose newest freshness row fired.

        The candidates are the newest `CANDIDATE_LIMIT` tables with a fired freshness row;
        `newest_freshness` confirms each is still late.

        Parameters
        ----------
        tables
            Only these tables (already checked as readable); `None` for every table.

        Yields
        ------
        AttentionItem
            A ``table_late`` row per late table.
        """
        if tables is not None and not tables:
            return
        last = func.max(TableHealthSignal.observed_at)
        query = select(TableHealthSignal.table_id).where(
            TableHealthSignal.signal == "freshness",
            TableHealthSignal.state == "fired",
            TableHealthSignal.sequence_number.is_(None),
            NOT_SET_BACK,
        )
        if tables is not None:
            query = query.where(TableHealthSignal.table_id.in_(list(tables)))
        candidates = list(
            self._session.execute(
                query.group_by(TableHealthSignal.table_id)
                .order_by(last.desc())
                .limit(CANDIDATE_LIMIT)
            ).scalars()
        )
        names = tables if tables is not None else self._names(candidates)
        for table_id in candidates:
            table = names.get(table_id)
            newest = newest_freshness(self._session, table_id)
            if table is None or newest is None or newest.state != "fired":
                continue
            dotted = table.ref.dotted()
            yield AttentionItem("table_late", table.environment, dotted, dotted, newest.observed_at)

    def _failed_checks(
        self, p: PrincipalRef, since: datetime, tables: dict[uuid.UUID, _Table] | None
    ) -> Iterator[AttentionItem]:
        """List the checks whose newest result within the window failed or errored.

        Parameters
        ----------
        p
            The caller.
        since
            The window's start.
        tables
            Only the checks of these tables (already checked as readable), shown where the
            caller may read the environment's check results, and not a generated reference
            check whose target the caller may not read; `None` for every check, unfiltered
            (an administrator).

        Yields
        ------
        AttentionItem
            A ``check_failed`` row per check; its ref is the run that checked.
        """
        if tables is not None and not tables:
            return
        ranked = (
            select(
                CheckResult.id,
                func.row_number()
                .over(
                    partition_by=(CheckResult.environment_id, CheckResult.check_name),
                    order_by=(CheckResult.finished_at.desc(), CheckResult.id.desc()),
                )
                .label("position"),
            )
            .where(CheckResult.finished_at >= since)
            .subquery()
        )
        query = (
            select(CheckResult, Environment.name)
            .join(ranked, ranked.c.id == CheckResult.id)
            .join(Environment, Environment.id == CheckResult.environment_id)
            .where(ranked.c.position == 1, CheckResult.outcome.in_(_CHECK_FAILED), LIVE_ENVIRONMENT)
        )
        if tables is not None:
            query = query.where(CheckResult.table_id.in_(list(tables)))
        rows = self._session.execute(
            query.order_by(CheckResult.finished_at.desc(), CheckResult.id.desc()).limit(
                CANDIDATE_LIMIT
            )
        ).all()
        withheld: dict[str, ReferenceCheckFilter] = {}
        for result, env in rows:
            if tables is not None:
                if not self._permits(p, Action.READ_ENVIRONMENT, env):
                    continue
                if env not in withheld:
                    withheld[env] = ReferenceCheckFilter(self._session, self._authz, p, env)
                if withheld[env].hides(result.run_id, result.check_name, result.generated):
                    continue
            yield AttentionItem(
                "check_failed", env, result.check_name, str(result.run_id), result.finished_at
            )

    def _failed_runs(self, since: datetime) -> Iterator[AttentionItem]:
        """List the jobs whose newest finished run within the window failed.

        Parameters
        ----------
        since
            The window's start.

        Yields
        ------
        AttentionItem
            A ``run_failed`` row per job (or per repository, for its runs of no job).
        """
        ranked = (
            select(
                Run.id,
                func.row_number()
                .over(
                    partition_by=(Run.environment_id, Run.repository_id, Run.job),
                    order_by=(Run.finished_at.desc(), Run.id.desc()),
                )
                .label("position"),
            )
            .where(Run.finished_at >= since, Run.status.in_(_FINISHED))
            .subquery()
        )
        rows = self._session.execute(
            select(Run.id, Run.job, Run.finished_at, Repository.name, Environment.name)
            .join(ranked, ranked.c.id == Run.id)
            .join(Repository, Repository.id == Run.repository_id)
            .join(Environment, Environment.id == Run.environment_id)
            .where(ranked.c.position == 1, Run.status == RunStatus.FAILED, LIVE_ENVIRONMENT)
            .order_by(Run.finished_at.desc(), Run.id.desc())
            .limit(CANDIDATE_LIMIT)
        ).all()
        for run_id, job, finished_at, repository, env in rows:
            # The window's condition holds only for a run with an end.
            ended = cast("datetime", finished_at)
            yield AttentionItem("run_failed", env, job or repository, str(run_id), ended)

    def _failed_queries(self, p: PrincipalRef, since: datetime) -> Iterator[AttentionItem]:
        """List the caller's own failed ad-hoc queries within the window.

        A query of a pipeline's SQL task (`task_run_id` set) is its run's failure, not one of
        the person's queries. A query is listed only where the caller may still submit
        queries, as its own page shows it.

        Parameters
        ----------
        p
            The caller.
        since
            The window's start.

        Yields
        ------
        AttentionItem
            A ``query_failed`` row per query.
        """
        rows = self._session.execute(
            select(QueryRun.id, QueryRun.created_at, Environment.name)
            .join(Environment, Environment.id == QueryRun.environment_id)
            .where(
                QueryRun.principal_id == p.id,
                QueryRun.status == QueryRunStatus.FAILED,
                QueryRun.task_run_id.is_(None),
                QueryRun.created_at >= since,
                LIVE_ENVIRONMENT,
            )
            .order_by(QueryRun.created_at.desc(), QueryRun.id.desc())
            .limit(CANDIDATE_LIMIT)
        ).all()
        for query_id, created_at, env in rows:
            if self._permits(p, Action.SUBMIT_QUERY, env):
                ref = str(query_id)
                yield AttentionItem("query_failed", env, f"Query {ref[:8]}", ref, created_at)

    def _recovery_links(self, now: datetime) -> Iterator[AttentionItem]:
        """List the open recovery links, newest `CANDIDATE_LIMIT` of them.

        Parameters
        ----------
        now
            The time a link must expire after.

        Yields
        ------
        AttentionItem
            A ``recovery_link_open`` row per link, titled with the person it signs in.
        """
        links = open_links(self._session, "recovery", now)[-CANDIDATE_LIMIT:]
        people = {link.principal_id for link in links}
        names = {
            principal_id: name
            for principal_id, name in self._session.execute(
                select(Principal.id, Principal.name).where(Principal.id.in_(people))
            )
        }
        for link in links:
            name = names.get(link.principal_id)
            if name is not None:
                yield AttentionItem("recovery_link_open", None, name, str(link.id), link.created_at)

    def _lost_workers(self, now: datetime) -> Iterator[AttentionItem]:
        """List the lost workers that no restart on their host replaced.

        A worker whose heartbeat is older than the lease is lost. One that a worker started
        on the same host after its last heartbeat replaced is not missing, as the instance
        status counts it (`lakana_core.runs.instance_status.replaced_ids`).

        Parameters
        ----------
        now
            The database's time.

        Yields
        ------
        AttentionItem
            A ``worker_lost`` row per worker, titled with its host.
        """
        alive = alive_ids(self._session, now, self._lease_seconds)
        rows = list(
            self._session.execute(
                select(Worker).order_by(Worker.heartbeat_at.desc()).limit(CANDIDATE_LIMIT)
            ).scalars()
        )
        for lost in rows:
            if lost.id in alive:
                continue
            replaced = any(
                other.id in alive
                and other.host == lost.host
                and other.started_at > lost.heartbeat_at
                for other in rows
            )
            if not replaced:
                yield AttentionItem("worker_lost", None, lost.host, lost.id, lost.heartbeat_at)
