"""Run resources: `Client.runs`, `Client.scheduler` and `Client.workers`."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from typing import Any

from lakana._http import NEXT_CURSOR_HEADER, HttpTransport, csv_param, query_params
from lakana._models import (
    Failure,
    Notification,
    Run,
    RunPage,
    RunTask,
    SchedulerStatus,
    TaskAttempt,
    TaskLog,
    WorkerCaches,
    WorkerInfo,
)
from lakana._routes import ROUTES
from lakana.errors import RunFailed
from lakana.resources.checks import check_result_from_json
from lakana.resources.queries import waiting_for_from_json

_FINISHED = frozenset({"succeeded", "failed", "cancelled"})
_RunList = list[Run]
_WorkerList = list[WorkerInfo]


def _when(value: str | None) -> datetime | None:
    """Parse an optional ISO timestamp.

    Parameters
    ----------
    value
        The timestamp or `None`.

    Returns
    -------
    datetime or None
        The parsed value.
    """
    return datetime.fromisoformat(value) if value is not None else None


def notification_from_json(data: dict[str, Any]) -> Notification:
    """Build a `Notification` from a `NotificationOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    Notification
        The notification.
    """
    return Notification(
        id=data["id"],
        event=data["event"],
        state=data["state"],
        attempts=data["attempts"],
        max_attempts=data["max_attempts"],
        last_result=data["last_result"],
        delivered_at=_when(data["delivered_at"]),
    )


def failure_from_json(data: object) -> Failure | None:
    """Build a `Failure` from the `failure` value of a task or an attempt.

    A failure is only a help for the reader, so a value that cannot be read never fails a
    command: anything but an object with a string `code`, `cause` and `next_step` is `None`.

    Parameters
    ----------
    data
        The decoded value, normally an object, or `None` (a task with no error, or a server that
        predates the field). Fields this SDK does not know are ignored; an unknown code is kept
        as is.

    Returns
    -------
    Failure or None
        The failure, or `None`.
    """
    if not isinstance(data, dict):
        return None
    code, cause, next_step = data.get("code"), data.get("cause"), data.get("next_step")
    if not (isinstance(code, str) and isinstance(cause, str) and isinstance(next_step, str)):
        return None
    return Failure(code, cause, next_step)


def run_from_json(data: dict[str, Any]) -> Run:
    """Build a `Run` from a `RunOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    Run
        The run.
    """
    return Run(
        id=data["id"],
        environment=data["environment"],
        repository=data["repository"],
        project_version_id=data["project_version_id"],
        commit_sha=data["commit_sha"],
        kind=data["kind"],
        status=data["status"],
        cancel_requested=data["cancel_requested"],
        triggered_by=data["triggered_by"],
        run_as=data["run_as"],
        resumed_from_run_id=data["resumed_from_run_id"],
        selection=data["selection"],
        error_message=data["error_message"],
        created_at=datetime.fromisoformat(data["created_at"]),
        started_at=_when(data["started_at"]),
        finished_at=_when(data["finished_at"]),
        tasks=[
            RunTask(
                asset=t["asset"],
                status=t["status"],
                attempt=t["attempt"],
                max_attempts=t["max_attempts"],
                timeout_seconds=t["timeout_seconds"],
                next_attempt_at=_when(t["next_attempt_at"]),
                started_at=_when(t["started_at"]),
                finished_at=_when(t["finished_at"]),
                error_message=t["error_message"],
                query_run_id=t["query_run_id"],
                rows_written=t["rows_written"],
                waiting_for=waiting_for_from_json(t.get("waiting_for")),
                kind=t.get("kind", "asset"),
                checks=tuple(check_result_from_json(c) for c in t.get("checks", ())),
                kept_check=t.get("kept_check", False),
                setting_back=t.get("setting_back", False),
                failure=failure_from_json(t.get("failure")),
            )
            for t in data["tasks"]
        ],
        job=data.get("job"),
        trigger=data.get("trigger"),
        scheduled_for=_when(data.get("scheduled_for")),
        missed=data.get("missed", 0),
        notifications=[notification_from_json(n) for n in data.get("notifications", ())],
        failed_tasks=data.get("failed_tasks", 0),
    )


class Runs:
    """Start, follow, cancel and resume runs.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def start(
        self,
        environment: str,
        repository: str,
        *,
        select: Sequence[str] = (),
        upstream: bool = False,
        downstream: bool = False,
    ) -> Run:
        """Start a run of the deployed project version (needs `RUN`).

        Parameters
        ----------
        environment
            Environment name.
        repository
            Repository name.
        select
            Shell-glob patterns over dotted table names; empty runs everything.
        upstream
            Add every ancestor of the matched assets.
        downstream
            Add every descendant of the matched assets.

        Returns
        -------
        Run
            The queued run.

        Raises
        ------
        lakana.errors.Conflict
            If a run is already active (its id is in the message), or no run
            principal is bound in a protected environment.
        lakana.errors.ValidationError
            If a pattern matches nothing.
        """
        response = self._transport.request(
            ROUTES["runs.start"],
            path_params={"env": environment},
            json={
                "repository": repository,
                "select": list(select),
                "upstream": upstream,
                "downstream": downstream,
            },
        )
        return run_from_json(response.json())

    # `list`, `list_page` and `iter` take the same filters and each documents them: a public
    # method states its own parameters.
    # jscpd:ignore-start
    def list(
        self,
        environment: str,
        *,
        repository: str | None = None,
        status: str | Sequence[str] | None = None,
        trigger: str | Sequence[str] | None = None,
        job: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> _RunList:
        """List runs, newest first, without tasks.

        The runs of one page; `list_page` also returns the cursor of the next one, and `iter`
        walks every page.

        Parameters
        ----------
        environment
            Environment name.
        repository
            Only this repository's runs.
        status
            Only runs in this status or one of these (``queued``, ``running``, ``succeeded``,
            ``failed``, ``cancelled``).
        trigger
            Only runs started this way or one of these (``schedule``, ``api``, ``manual``,
            ``deploy``).
        job
            Only runs of this job.
        since
            Only runs created at or after this time, which needs a time zone.
        limit
            Most runs returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same filters with every page.

        Returns
        -------
        list of Run
            Newest first.

        Raises
        ------
        lakana.errors.ValidationError
            If `limit` is outside 1 to 200, a filter value is unknown, `since` has no time zone,
            or `cursor` is not one the server issued for the runs list.
        """
        return self.list_page(
            environment,
            repository=repository,
            status=status,
            trigger=trigger,
            job=job,
            since=since,
            limit=limit,
            cursor=cursor,
        ).items

    def list_page(
        self,
        environment: str,
        *,
        repository: str | None = None,
        status: str | Sequence[str] | None = None,
        trigger: str | Sequence[str] | None = None,
        job: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> RunPage:
        """List one page of runs, newest first, with the cursor of the next page.

        Parameters
        ----------
        environment
            Environment name.
        repository
            Only this repository's runs.
        status
            Only runs in this status or one of these (``queued``, ``running``, ``succeeded``,
            ``failed``, ``cancelled``).
        trigger
            Only runs started this way or one of these (``schedule``, ``api``, ``manual``,
            ``deploy``).
        job
            Only runs of this job.
        since
            Only runs created at or after this time, which needs a time zone.
        limit
            Most runs returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same filters with every page.

        Returns
        -------
        RunPage
            The runs, and the cursor for the next page (`None` on the last).

        Raises
        ------
        lakana.errors.ValidationError
            If `limit` is outside 1 to 200, a filter value is unknown, `since` has no time zone,
            or `cursor` is not one the server issued for the runs list.
        """
        response = self._transport.request(
            ROUTES["runs.list"],
            path_params={"env": environment},
            params=query_params(
                repository=repository,
                status=csv_param(status),
                trigger=csv_param(trigger),
                job=job,
                since=None if since is None else since.isoformat(),
                limit=limit,
                cursor=cursor,
            ),
        )
        return RunPage(
            items=[run_from_json(item) for item in response.json()],
            next_cursor=response.headers.get(NEXT_CURSOR_HEADER),
        )

    def iter(
        self,
        environment: str,
        *,
        repository: str | None = None,
        status: str | Sequence[str] | None = None,
        trigger: str | Sequence[str] | None = None,
        job: str | None = None,
        since: datetime | None = None,
        page_size: int = 100,
    ) -> Iterator[Run]:
        """Yield every matching run, newest first, fetching pages as needed.

        Parameters
        ----------
        environment
            Environment name.
        repository
            Only this repository's runs.
        status
            Only runs in this status or one of these.
        trigger
            Only runs started this way or one of these.
        job
            Only runs of this job.
        since
            Only runs created at or after this time, which needs a time zone.
        page_size
            Runs per request (1 to 200).

        Yields
        ------
        Run
            Each run, once.
        """
        cursor: str | None = None
        while True:
            page = self.list_page(
                environment,
                repository=repository,
                status=status,
                trigger=trigger,
                job=job,
                since=since,
                limit=page_size,
                cursor=cursor,
            )
            yield from page.items
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    # jscpd:ignore-end

    def get(self, environment: str, run_id: str) -> Run:
        """Show one run with its tasks.

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The run.

        Returns
        -------
        Run
            The run.
        """
        response = self._transport.request(
            ROUTES["runs.get"], path_params={"env": environment, "run_id": run_id}
        )
        return run_from_json(response.json())

    def cancel(self, environment: str, run_id: str) -> Run:
        """Request cancellation.

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The run.

        Returns
        -------
        Run
            The run with `cancel_requested`.
        """
        response = self._transport.request(
            ROUTES["runs.cancel"], path_params={"env": environment, "run_id": run_id}
        )
        return run_from_json(response.json())

    def resume(self, environment: str, run_id: str) -> Run:
        """Start a new run over a finished run's failed, upstream_failed and cancelled tasks.

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The finished run.

        Returns
        -------
        Run
            The new run.
        """
        response = self._transport.request(
            ROUTES["runs.resume"], path_params={"env": environment, "run_id": run_id}
        )
        return run_from_json(response.json())

    def log(self, environment: str, run_id: str, asset: str, offset: int | None = None) -> TaskLog:
        """Return a task's free-text log and attempts, all of it or from a byte offset.

        To follow a log, pass the previous answer's `next_offset` as `offset`: each byte then
        travels once. One answer carries at most 1 MiB; `more` says whether to ask again at once.

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The run.
        asset
            The task's dotted table name.
        offset
            The byte to read from; `None` returns the whole log.

        Returns
        -------
        TaskLog
            Text, where it ends in the stored log (`next_offset`), whether more is stored,
            whether the task is finished and its log read to the end, and attempts.
        """
        response = self._transport.request(
            ROUTES["runs.log"],
            path_params={"env": environment, "run_id": run_id, "asset": asset},
            params=query_params(offset=offset),
        )
        data = response.json()
        return TaskLog(
            run_id=data["run_id"],
            asset=data["asset"],
            text=data["text"],
            next_offset=data["next_offset"],
            more=data["more"],
            complete=data["complete"],
            reset=data["reset"],
            attempts=[
                TaskAttempt(
                    attempt=a["attempt"],
                    worker=a["worker"],
                    started_at=datetime.fromisoformat(a["started_at"]),
                    finished_at=_when(a["finished_at"]),
                    outcome=a["outcome"],
                    rows_written=a["rows_written"],
                    error_message=a["error_message"],
                    failure=failure_from_json(a.get("failure")),
                )
                for a in data["attempts"]
            ],
        )

    def wait(
        self,
        environment: str,
        run_id: str,
        timeout: float | None = None,
        poll_interval: float = 1.0,
        max_interval: float = 5.0,
        on_update: Callable[[Run], None] | None = None,
    ) -> Run:
        """Poll a run until it finishes.

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The run.
        timeout
            Seconds to keep polling, or `None` for no limit.
        poll_interval
            First interval, doubling up to `max_interval`.
        max_interval
            Largest interval.
        on_update
            Called with every polled state (progress display).

        Returns
        -------
        Run
            The run, once `succeeded`.

        Raises
        ------
        lakana.errors.RunFailed
            If it ends `failed` or `cancelled`.
        TimeoutError
            If `timeout` elapses first; the run keeps going server-side.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        interval = poll_interval
        while True:
            run = self.get(environment, run_id)
            if on_update is not None:
                on_update(run)
            if run.status == "succeeded":
                return run
            if run.status in _FINISHED:
                raise RunFailed(f"Run {run_id} {run.status}", run)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Run {run_id} still {run.status} after {timeout}s")
            time.sleep(interval)
            interval = min(interval * 2, max_interval)


class Scheduler:
    """The scheduler role's status (admin).

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def status(self) -> SchedulerStatus:
        """Report the holder and the queue lengths.

        Returns
        -------
        SchedulerStatus
            Holder, heartbeat and counts.
        """
        data = self._transport.request(ROUTES["scheduler.status"]).json()
        return SchedulerStatus(
            holder=data["holder"],
            heartbeat_at=_when(data["heartbeat_at"]),
            active_runs=data["active_runs"],
            pending=data["pending"],
            ready=data["ready"],
            running=data["running"],
        )


def _caches(data: object) -> WorkerCaches | None:
    """Read the cache sizes of a listed worker.

    Parameters
    ----------
    data
        The entry's `caches`: an object, `null`, or absent from a server of an earlier release.

    Returns
    -------
    WorkerCaches or None
        `None` unless it is an object.
    """
    if not isinstance(data, dict):
        return None
    return WorkerCaches(
        environments_bytes=data["environments_bytes"],
        packages_bytes=data["packages_bytes"],
        tasks_bytes=data["tasks_bytes"],
        complete=data["complete"],
        measured_at=datetime.fromisoformat(data["measured_at"]),
    )


class Workers:
    """The workers of this instance (admin).

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(self) -> _WorkerList:
        """List the registered workers, by id.

        A worker is `alive` while its heartbeat is younger than the server's lease and `lost`
        after that; a lost worker's row is kept for an hour. Each entry shows the run ids of
        what the worker is doing (a query, a deploy, a pipeline task), never SQL, and `takes`
        says which work it takes: `all`, `interactive` or `pipeline` (`all` when the server is
        of an earlier release and sends none). `caches` has the sizes of the worker's caches.

        Returns
        -------
        list of WorkerInfo
            The workers; empty when none is registered.

        Raises
        ------
        lakana.errors.PermissionDenied
            Without `MANAGE_ENVIRONMENT` on the instance (as `scheduler.status`).
        """
        return [
            WorkerInfo(
                id=item["id"],
                host=item["host"],
                version=item["version"],
                started_at=datetime.fromisoformat(item["started_at"]),
                heartbeat_at=datetime.fromisoformat(item["heartbeat_at"]),
                status=item["status"],
                takes=item["takes"] if isinstance(item.get("takes"), str) else "all",
                query_run_id=item["query_run_id"],
                deploy_run_id=item["deploy_run_id"],
                task_run_id=item["task_run_id"],
                caches=_caches(item.get("caches")),
            )
            for item in self._transport.request(ROUTES["workers.list"]).json()
        ]
