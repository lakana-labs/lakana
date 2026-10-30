"""The `queries` resource: `Client.queries`, and the `QueryRun` handle."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

import pyarrow as pa
import pyarrow.ipc

from lakana._http import NEXT_CURSOR_HEADER, HttpTransport, csv_param, query_params
from lakana._models import QueryListEntry, QueryListPage, QueryStatus, WaitingFor
from lakana._routes import ROUTES
from lakana.errors import QueryFailed, ValidationError

_EntryList = list[QueryListEntry]


def _int(value: object) -> int | None:
    """Keep a number the server sent as a count, drop anything else.

    Parameters
    ----------
    value
        A decoded JSON value.

    Returns
    -------
    int or None
        `value` if it is an integer (not a boolean), else `None`.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def waiting_for_from_json(data: object) -> WaitingFor | None:
    """Build a `WaitingFor` from the `waiting_for` value of a response body.

    A reason is only a help for the reader, so a value that cannot be read never fails a
    command: anything but an object with a string `code` and a string `message` is `None`, and
    an optional field of the wrong shape is left out.

    Parameters
    ----------
    data
        The decoded value, normally an object, or `None` (a status that is not waiting, or a
        server that predates the field). Fields this SDK does not know are ignored; an unknown
        code is kept as is.

    Returns
    -------
    WaitingFor or None
        The reason, or `None`.
    """
    if not isinstance(data, dict):
        return None
    code, message = data.get("code"), data.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        return None
    upstream = data.get("upstream")
    when = data.get("next_attempt_at")
    names = (
        tuple(upstream)
        if isinstance(upstream, list) and all(isinstance(n, str) for n in upstream)
        else None
    )
    try:
        next_attempt = datetime.fromisoformat(when) if isinstance(when, str) else None
    except ValueError:
        next_attempt = None
    return WaitingFor(
        code=code,
        message=message,
        workers=_int(data.get("workers")),
        ahead=_int(data.get("ahead")),
        upstream=names,
        upstream_count=_int(data.get("upstream_count")),
        hidden_upstream=_int(data.get("hidden_upstream")),
        attempt=_int(data.get("attempt")),
        max_attempts=_int(data.get("max_attempts")),
        next_attempt_at=next_attempt,
        other_workers=_int(data.get("other_workers")),
        running=_int(data.get("running")),
        limit=_int(data.get("limit")),
    )


def _status_from_json(data: dict[str, Any]) -> QueryStatus:
    """Build a `QueryStatus` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `QueryStatusOut` JSON body.

    Returns
    -------
    QueryStatus
        The parsed query status.
    """
    return QueryStatus(
        id=data["query_id"],
        status=data["status"],
        error_message=data.get("error_message"),
        lineage=data.get("lineage"),
        write_mode=data.get("write_mode"),
        target=data.get("target"),
        rows_written=data.get("rows_written"),
        snapshot_id=data.get("snapshot_id"),
        watermark=data.get("watermark"),
        schema_diff=data.get("schema_diff"),
        maintenance=data.get("maintenance"),
        error_cause=data.get("error_cause"),
        waiting_for=waiting_for_from_json(data.get("waiting_for")),
    )


def _entry_from_json(data: dict[str, Any]) -> QueryListEntry:
    """Build a `QueryListEntry` from a `QueryListOut` body.

    Parameters
    ----------
    data
        The decoded row.

    Returns
    -------
    QueryListEntry
        The entry.
    """
    started = data["started_at"]
    finished = data["finished_at"]
    return QueryListEntry(
        id=data["id"],
        submitter=data["submitter"],
        kind=data["kind"],
        target=data["target"],
        status=data["status"],
        created_at=datetime.fromisoformat(data["created_at"]),
        started_at=None if started is None else datetime.fromisoformat(started),
        finished_at=None if finished is None else datetime.fromisoformat(finished),
        error_cause=data["error_cause"],
        waiting_for=waiting_for_from_json(data.get("waiting_for")),
    )


def _write_target(
    into: str | None, mode: str | None, incremental_key: str | None, evolve_schema: bool
) -> dict[str, object] | None:
    """Build a submission's `target`, refusing options without their partner.

    Parameters
    ----------
    into
        The target table.
    mode
        The write mode.
    incremental_key
        The incremental key.
    evolve_schema
        Whether to evolve the schema.

    Returns
    -------
    dict or None
        The `target` object, or `None` for a plain query.

    Raises
    ------
    ValidationError
        If `into` is given without `mode`, or `mode`, `incremental_key` or
        `evolve_schema` without `into`. The server checks which
        options each mode allows.
    """
    if into is None:
        if mode is not None or incremental_key is not None or evolve_schema:
            raise ValidationError("mode, incremental_key and evolve_schema need into.")
        return None
    if mode is None:
        raise ValidationError("into needs mode: create, replace, append or view.")
    return {
        "table": into,
        "mode": mode,
        "incremental_key": incremental_key,
        "evolve_schema": evolve_schema,
    }


class QueryRun:
    """A handle to a submitted query, for polling status and fetching results.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    id
        The query's id.
    environment
        The environment the query runs against.
    """

    def __init__(self, transport: HttpTransport, id: str, environment: str) -> None:
        self._transport = transport
        self.id = id
        self.environment = environment

    def status(self) -> QueryStatus:
        """Fetch this query's current status.

        Returns
        -------
        QueryStatus
            The query's status.

        Raises
        ------
        lakana.errors.NotFound
            If the query does not exist, or was submitted by another
            principal.
        """
        response = self._transport.request(
            ROUTES["queries.get"], path_params={"env": self.environment, "query_id": self.id}
        )
        return _status_from_json(response.json())

    def wait(
        self,
        timeout: float | None = None,
        poll_interval: float = 0.25,
        max_interval: float = 2.0,
        on_update: Callable[[QueryStatus], None] | None = None,
    ) -> QueryStatus:
        """Poll this query until it reaches a terminal state.

        Parameters
        ----------
        timeout
            Seconds to keep polling before giving up, or `None` to poll
            indefinitely.
        poll_interval
            Seconds to wait before the first re-check, doubling after every
            subsequent non-terminal check, up to `max_interval`.
        max_interval
            The largest interval between polls.
        on_update
            Called with every polled `QueryStatus`, including the last one, before `wait`
            returns or raises (so a progress display can show why a query is `QUEUED`:
            `status.waiting_for`). It is called on every poll, not only when something
            changed: compare with the previous call to react to a change. An exception it
            raises ends the wait and propagates. `None` (the default) calls nothing.

        Returns
        -------
        QueryStatus
            The query's status, once `SUCCEEDED`.

        Raises
        ------
        lakana.errors.QueryFailed
            If the query's status becomes `FAILED` or `CANCELLED`, carrying the server's
            own `error_message` and, in `.cause`, its machine-readable cause (`cancelled` for
            a cancelled query).
        TimeoutError
            If `timeout` elapses before the query reaches a terminal state.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        interval = poll_interval
        while True:
            status = self.status()
            if on_update is not None:
                on_update(status)
            if status.status == "SUCCEEDED":
                return status
            if status.status == "FAILED":
                raise QueryFailed(status.error_message or "", cause=status.error_cause)
            if status.status == "CANCELLED":
                raise QueryFailed(
                    status.error_message or "The query was cancelled.",
                    cause=status.error_cause or "cancelled",
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Query {self.id} still {status.status} after {timeout}s")
            time.sleep(interval)
            interval = min(interval * 2, max_interval)

    def cancel(self) -> QueryStatus:
        """Cancel this query if it is queued, or ask the worker to stop it if it runs.

        Returns
        -------
        QueryStatus
            The query's status after the request: `CANCELLED`, still `RUNNING` (it ends within
            the server's grace period; poll `status()` or call `wait()`), or the final status
            of a query that had already ended, which is not an error.

        Raises
        ------
        lakana.errors.NotFound
            If the query does not exist, or you neither submitted it nor hold
            `MANAGE_ENVIRONMENT` on its environment.
        lakana.errors.ValidationError
            If the query is the write of a pipeline task (cancel the run) or a maintenance run
            that has started.
        lakana.errors.Conflict
            If a worker is starting the query or finishing it at this moment and the cancel
            found it locked; nothing was changed. Check the status and cancel again if the
            query is still running.
        """
        response = self._transport.request(
            ROUTES["queries.cancel"], path_params={"env": self.environment, "query_id": self.id}
        )
        return _status_from_json(response.json())

    def result(self) -> pa.Table:
        """Fetch this query's result as a pyarrow Table.

        Returns
        -------
        pa.Table
            The query's result, read from the server's Arrow IPC **file**
            response.

        Raises
        ------
        lakana.errors.NotFound
            If the query does not exist, or was submitted by another
            principal.
        lakana.errors.Conflict
            If the query has not succeeded yet, or it succeeded but is not a read: a write, a view
            or a maintenance run has no result rows, and the message names the command that
            shows what it did.
        """
        response = self._transport.request(
            ROUTES["queries.result"], path_params={"env": self.environment, "query_id": self.id}
        )
        with pyarrow.ipc.open_file(pa.BufferReader(response.content)) as reader:
            return reader.read_all()


class Queries:
    """The `queries` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    # `list` and `list_page` take the same filters and each documents them: a public method
    # states its own parameters.
    # jscpd:ignore-start
    def list(
        self,
        environment: str,
        *,
        include_ended: bool = False,
        status: str | Sequence[str] | None = None,
        submitter: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> _EntryList:
        """List the queries of an environment: running, then queued, then (optionally) ended.

        With `SUBMIT_QUERY` you see the queries you submitted (a task token: its own); with
        `MANAGE_ENVIRONMENT` on the environment, everyone's. A row never holds the SQL text, an
        error message or a result, not even your own: read those through the query's status.
        Running queries come first, longest running first, then queued ones in the order they
        will be taken, with the reason each waits in `waiting_for`; with `include_ended`, then
        the queries that ended in the last 24 hours, newest first. `limit` cuts that order.
        `status` and `submitter` only narrow what you may see; `list_page` also returns the
        cursor of the next page.

        Parameters
        ----------
        environment
            The environment.
        include_ended
            Also list the queries that ended in the last 24 hours.
        status
            Only queries in this status or one of these (``queued``, ``running``,
            ``succeeded``, ``failed``, ``cancelled``). An ended status lists the ended queries
            of the last 24 hours, with or without `include_ended`.
        submitter
            Only the queries submitted by the principal with this name.
        limit
            Most entries returned (1 to 200).
        cursor
            A previous page's `next_cursor`. Send the same filters with every page.

        Returns
        -------
        list of QueryListEntry
            The entries, in the order above.

        Raises
        ------
        lakana.errors.PermissionDenied
            If you may not submit queries in this environment.
        lakana.errors.NotFound
            If the environment does not exist.
        lakana.errors.ValidationError
            If `limit` is outside 1 to 200, a status is unknown, or `cursor` is not one the
            server issued for the queries list.
        """
        return self.list_page(
            environment,
            include_ended=include_ended,
            status=status,
            submitter=submitter,
            limit=limit,
            cursor=cursor,
        ).items

    def list_page(
        self,
        environment: str,
        *,
        include_ended: bool = False,
        status: str | Sequence[str] | None = None,
        submitter: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> QueryListPage:
        """List one page of the queries of an environment, with the cursor of the next page.

        Parameters
        ----------
        environment
            The environment.
        include_ended
            Also list the queries that ended in the last 24 hours.
        status
            Only queries in this status or one of these (``queued``, ``running``,
            ``succeeded``, ``failed``, ``cancelled``).
        submitter
            Only the queries submitted by the principal with this name.
        limit
            Most entries returned (1 to 200).
        cursor
            A previous page's `next_cursor`. Send the same filters with every page.

        Returns
        -------
        QueryListPage
            The entries in the order of `list`, and the cursor for the next page (`None` on
            the last).

        Raises
        ------
        lakana.errors.PermissionDenied
            If you may not submit queries in this environment.
        lakana.errors.NotFound
            If the environment does not exist.
        lakana.errors.ValidationError
            If `limit` is outside 1 to 200, a status is unknown, or `cursor` is not one the
            server issued for the queries list.
        """
        response = self._transport.request(
            ROUTES["queries.list"],
            path_params={"env": environment},
            params=query_params(
                include_ended="true" if include_ended else "false",
                status=csv_param(status),
                submitter=submitter,
                limit=limit,
                cursor=cursor,
            ),
        )
        return QueryListPage(
            items=[_entry_from_json(item) for item in response.json()],
            next_cursor=response.headers.get(NEXT_CURSOR_HEADER),
        )

    # jscpd:ignore-end

    def submit(
        self,
        sql: str,
        environment: str,
        *,
        into: str | None = None,
        mode: str | None = None,
        incremental_key: str | None = None,
        evolve_schema: bool = False,
        break_glass_reason: str | None = None,
    ) -> QueryRun:
        """Submit a query for execution.

        Parameters
        ----------
        sql
            The SQL text to run.
        environment
            The environment to run the query against.
        into
            Write the result into this table or view.
        mode
            `create`, `replace`, `append` or `view`.
        incremental_key
            With `mode="append"`, append only rows whose key is past the
            target's maximum.
        evolve_schema
            Let the target adopt the result's schema.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited. The next run of the deployed code may overwrite
            the fix.

        Returns
        -------
        QueryRun
            A handle for polling status and fetching results.

        Raises
        ------
        lakana.errors.NotFound
            If the environment does not exist.
        lakana.errors.ValidationError
            If the write options don't go together.
        """
        body: dict[str, object] = {"sql": sql}
        target = _write_target(into, mode, incremental_key, evolve_schema)
        if target is not None:
            body["target"] = target
        if break_glass_reason is not None:
            body["break_glass_reason"] = break_glass_reason
        response = self._transport.request(
            ROUTES["queries.submit"], path_params={"env": environment}, json=body
        )
        return QueryRun(self._transport, id=response.json()["query_id"], environment=environment)

    def get(self, query_id: str, environment: str) -> QueryRun:
        """Build a handle for an already-submitted query.

        Parameters
        ----------
        query_id
            The query's id.
        environment
            The environment the query runs against.

        Returns
        -------
        QueryRun
            A handle for polling status and fetching results. No request is
            sent until the handle is used.
        """
        return QueryRun(self._transport, id=query_id, environment=environment)

    def cancel(self, query_id: str, environment: str) -> QueryStatus:
        """Cancel a query by id; the same call as `get(query_id, environment).cancel()`.

        Parameters
        ----------
        query_id
            The query's id.
        environment
            The environment the query runs against.

        Returns
        -------
        QueryStatus
            The query's status after the request: `CANCELLED`, still `RUNNING` (it ends within
            the server's grace period), or the final status of a query that had already ended.

        Raises
        ------
        lakana.errors.NotFound
            If the query does not exist, or you neither submitted it nor hold
            `MANAGE_ENVIRONMENT` on its environment.
        lakana.errors.ValidationError
            If the query is the write of a pipeline task (cancel the run) or a maintenance run
            that has started.
        """
        return self.get(query_id, environment).cancel()
