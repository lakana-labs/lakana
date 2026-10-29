"""The `checks` resource: `Client.checks`, the results of data quality checks."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from lakana._http import HttpTransport, query_params
from lakana._models import CheckResult, CheckResultPage
from lakana._routes import ROUTES
from lakana.resources.queries import QueryRun

_ResultList = list[CheckResult]


def check_result_from_json(data: dict[str, Any]) -> CheckResult:
    """Build a `CheckResult` from a `CheckResultOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    CheckResult
        The result.
    """
    return CheckResult(
        run_id=data["run_id"],
        task=data["task"],
        name=data["name"],
        check=data["check"],
        path=data["path"],
        table=data["table"],
        table_id=data["table_id"],
        on_failure=data["on_failure"],
        outcome=data["outcome"],
        failing_rows=data["failing_rows"],
        error=data["error"],
        checked_snapshot_id=data["checked_snapshot_id"],
        rollback_outcome=data["rollback_outcome"],
        policy_applied=data["policy_applied"],
        policy_note=data["policy_note"],
        query_run_id=data["query_run_id"],
        started_at=datetime.fromisoformat(data["started_at"]),
        finished_at=datetime.fromisoformat(data["finished_at"]),
        generated=data.get("generated", False),
    )


class Checks:
    """Read the results of the data quality checks a run made.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def for_run(self, environment: str, run_id: str) -> _ResultList:
        """List the check results of one run (needs `READ_ENVIRONMENT` or `RUN`).

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The run.

        Returns
        -------
        list of CheckResult
            By task, then by check name.

        Raises
        ------
        lakana.errors.NotFound
            If the run does not exist in this environment.
        lakana.errors.PermissionDenied
            If the caller may not read the run.
        """
        response = self._transport.request(
            ROUTES["checks.for_run"], path_params={"env": environment, "run_id": run_id}
        )
        return [check_result_from_json(item) for item in response.json()]

    def rows(self, run_id: str, check: str, *, environment: str) -> QueryRun:
        """Queue the query that shows the rows a check found in a run.

        The rows are shown as they were then, also after the table was set back. The query is an
        ordinary query of yours: it runs the check's SQL under your own rights, row filters and
        column masks, and reads the checked table at the snapshot the check read. Wait for it
        and fetch the rows with the handle: `.wait()`, then `.result()`.

        Needs the right to read the run (`RUN` or `READ_ENVIRONMENT`) and `SUBMIT_QUERY`. The
        rows can be read for seven days after a failed blocking check; then table maintenance
        removes the snapshot.

        Parameters
        ----------
        run_id
            The run.
        check
            The check as `lakana check list` shows it: ``<namespace>.<table>/<check>``.
        environment
            Environment name.

        Returns
        -------
        QueryRun
            The handle of the queued query, as `client.queries.submit` returns it. A query that
            cannot be answered (the snapshot is gone, the table is not shown to you) ends
            `FAILED`; `.wait()` raises `lakana.errors.QueryFailed` with the reason.

        Raises
        ------
        lakana.errors.NotFound
            If the run does not exist in this environment, or has no result for the check.
        lakana.errors.PermissionDenied
            If you lack the right to read the run or to submit queries.
        lakana.errors.Conflict
            If the check read no snapshot, so there are no rows to show, or the run's project
            version no longer holds the check's SQL.
        """
        response = self._transport.request(
            ROUTES["checks.rows"],
            path_params={"env": environment, "run_id": run_id},
            json={"check": check},
        )
        return QueryRun(self._transport, id=response.json()["query_id"], environment=environment)

    def list(
        self,
        environment: str,
        *,
        table: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> CheckResultPage:
        """List one page of an environment's check results, newest first (needs `READ_ENVIRONMENT`).

        The filter is not part of the cursor: pass the same `table` with every page. `iter`
        does this for you.

        Parameters
        ----------
        environment
            Environment name.
        table
            Only this table's results (dotted name).
        limit
            Results per page, between 1 and 200 inclusive.
        cursor
            A previous page's `next_cursor`, to fetch the page after it.

        Returns
        -------
        CheckResultPage
            Up to `limit` results, and the cursor for the next page.

        Raises
        ------
        lakana.errors.PermissionDenied
            If the caller may not read the environment.
        lakana.errors.ValidationError
            If `limit` is out of range or `cursor` is not one the server issued.
        """
        response = self._transport.request(
            ROUTES["checks.list"],
            path_params={"env": environment},
            params=query_params(table=table, limit=limit, cursor=cursor),
        )
        body = response.json()
        return CheckResultPage(
            items=[check_result_from_json(item) for item in body["items"]],
            next_cursor=body["next_cursor"],
            hidden=body.get("hidden", 0),
        )

    def iter(
        self, environment: str, *, table: str | None = None, page_size: int = 50
    ) -> Iterator[CheckResult]:
        """Yield every check result of an environment, newest first, fetching pages as needed.

        Parameters
        ----------
        environment
            Environment name.
        table
            Only this table's results (dotted name).
        page_size
            Results per request, between 1 and 200 inclusive.

        Yields
        ------
        CheckResult
            Each result, once.

        Raises
        ------
        lakana.errors.PermissionDenied
            If the caller may not read the environment.
        """
        cursor: str | None = None
        while True:
            page = self.list(environment, table=table, limit=page_size, cursor=cursor)
            yield from page.items
            if page.next_cursor is None:
                return
            cursor = page.next_cursor
