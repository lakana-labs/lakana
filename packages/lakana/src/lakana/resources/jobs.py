"""Job resources: `Client.jobs`."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lakana._http import HttpTransport
from lakana._models import Delivery, Job, JobStart, Trigger, WaitingStart
from lakana._routes import ROUTES
from lakana.resources.queries import waiting_for_from_json
from lakana.resources.runs import notification_from_json, run_from_json

_JobList = list[Job]


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


_KINDS = ("schedule", "deploy", "on_demand")
"""The trigger kinds this SDK reads; any other is left out."""


def trigger_from_json(data: dict[str, Any]) -> Trigger:
    """Build a `Trigger` from a `TriggerOut` body.

    Parameters
    ----------
    data
        The decoded trigger.

    Returns
    -------
    Trigger
        The trigger.
    """
    return Trigger(
        kind=data["kind"],
        cron=data.get("cron"),
        timezone=data.get("timezone"),
        paused=data.get("paused"),
        next_run_at=_when(data.get("next_run_at")),
    )


def job_from_json(data: dict[str, Any]) -> Job:
    """Build a `Job` from a `JobOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    Job
        The job.
    """
    return Job(
        name=data["name"],
        environment=data["environment"],
        repository=data["repository"],
        select=tuple(data["select"]),
        upstream=data["upstream"],
        downstream=data["downstream"],
        cron=data["cron"],
        timezone=data["timezone"],
        state=data["state"],
        paused=data["paused"],
        next_run_at=_when(data["next_run_at"]),
        next_run_local=data["next_run_local"],
        waiting_for=waiting_for_from_json(data.get("waiting_for")),
        waits_for_run_id=data["waits_for_run_id"],
        last_run_id=data["last_run_id"],
        last_run_status=data["last_run_status"],
        last_run_trigger=data["last_run_trigger"],
        last_run_at=_when(data["last_run_at"]),
        triggers=tuple(
            trigger_from_json(item)
            for item in data.get("triggers", [])
            if item.get("kind") in _KINDS
        ),
    )


def waiting_start_from_json(data: dict[str, Any]) -> WaitingStart:
    """Build a `WaitingStart` from a `WaitingStartOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    WaitingStart
        The waiting start.
    """
    return WaitingStart(
        job=data["job"],
        waits_for_run_id=data["waits_for_run_id"],
        trigger=data["trigger"],
        requested_at=datetime.fromisoformat(data["requested_at"]),
        requested_by=data["requested_by"],
        scheduled_for=_when(data["scheduled_for"]),
        missed=data["missed"],
        merged=data["merged"],
        message=data["message"],
    )


class Jobs:
    """List the jobs of a project, start one, pause and resume its schedule.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(self, environment: str, *, repository: str | None = None) -> _JobList:
        """List the jobs of the deployed repositories (needs `READ_ENVIRONMENT`).

        Parameters
        ----------
        environment
            Environment name.
        repository
            Only this repository's jobs.

        Returns
        -------
        list of Job
            By repository, then name.
        """
        response = self._transport.request(
            ROUTES["jobs.list"],
            path_params={"env": environment},
            params={"repository": repository} if repository is not None else None,
        )
        return [job_from_json(item) for item in response.json()]

    def get(self, environment: str, job: str, *, repository: str | None = None) -> Job:
        """Show one job with its triggers (needs `READ_ENVIRONMENT`).

        Parameters
        ----------
        environment
            Environment name.
        job
            The job's name.
        repository
            Repository name; needed only when several repositories declare a job of this name.

        Returns
        -------
        Job
            The job, as `list` returns it.

        Raises
        ------
        lakana.errors.NotFound
            If the job does not exist; the message lists the jobs that do.
        lakana.errors.Conflict
            If several repositories declare it and none was named.
        """
        response = self._transport.request(
            ROUTES["jobs.get"],
            path_params={"env": environment, "job": job},
            params={"repository": repository} if repository is not None else None,
        )
        return job_from_json(response.json())

    def start(self, environment: str, repository: str, job: str) -> JobStart:
        """Start a job, or let the start wait for the active run of its project (needs `RUN`).

        A paused job can be started. The run principal follows the rules of `Runs.start`.

        Parameters
        ----------
        environment
            Environment name.
        repository
            Repository name.
        job
            The job's name.

        Returns
        -------
        JobStart
            The queued run (`run`), or the waiting start (`waiting`) with the run it waits for.

        Raises
        ------
        lakana.errors.NotFound
            If the job does not exist; the message lists the jobs that do.
        lakana.errors.Conflict
            If no run principal is bound in a protected environment.
        """
        response = self._transport.request(
            ROUTES["jobs.start"],
            path_params={"env": environment, "job": job},
            json={"repository": repository},
        )
        body = response.json()
        return JobStart(
            run=run_from_json(body["run"]) if body.get("run") is not None else None,
            waiting=(
                waiting_start_from_json(body["waiting"])
                if body.get("waiting") is not None
                else None
            ),
        )

    def pause(self, environment: str, repository: str, job: str) -> Job:
        """Pause a job's schedule (needs `DEPLOY` on the environment).

        A waiting start is kept and still happens; a start by hand still works.

        Parameters
        ----------
        environment
            Environment name.
        repository
            Repository name.
        job
            The job's name.

        Returns
        -------
        Job
            The job.

        Raises
        ------
        lakana.errors.Conflict
            If the job has no schedule, so there is nothing to pause.
        """
        response = self._transport.request(
            ROUTES["jobs.pause"],
            path_params={"env": environment, "job": job},
            json={"repository": repository},
        )
        return job_from_json(response.json())

    def resume(self, environment: str, repository: str, job: str) -> Job:
        """Resume a job's schedule from now (needs `DEPLOY` on the environment).

        The times missed while it was paused do not fire.

        Parameters
        ----------
        environment
            Environment name.
        repository
            Repository name.
        job
            The job's name.

        Returns
        -------
        Job
            The job.
        """
        response = self._transport.request(
            ROUTES["jobs.resume"],
            path_params={"env": environment, "job": job},
            json={"repository": repository},
        )
        return job_from_json(response.json())

    def notify_test(self, environment: str, repository: str, job: str) -> str:
        """Queue a test message to a job's webhook (needs `DEPLOY` on the environment).

        A worker sends it like any notification, with clearly fake run data. Read the result
        with `delivery`.

        Parameters
        ----------
        environment
            Environment name.
        repository
            Repository name.
        job
            The job's name.

        Returns
        -------
        str
            The delivery's id.

        Raises
        ------
        lakana.errors.NotFound
            If the job does not exist; the message lists the jobs that do.
        lakana.errors.Conflict
            If the job has no ``notify:`` in the environment.
        """
        response = self._transport.request(
            ROUTES["jobs.notify_test"],
            path_params={"env": environment, "job": job},
            json={"repository": repository},
        )
        return response.json()["delivery_id"]

    def delivery(self, environment: str, delivery_id: str) -> Delivery:
        """Read one notification delivery (needs `DEPLOY`, `RUN` or `READ_ENVIRONMENT`).

        Parameters
        ----------
        environment
            Environment name.
        delivery_id
            The delivery, as `notify_test` returned it.

        Returns
        -------
        Delivery
            Its state, attempts and short result.

        Raises
        ------
        lakana.errors.NotFound
            If the delivery does not exist in the environment.
        """
        response = self._transport.request(
            ROUTES["jobs.delivery"],
            path_params={"env": environment, "delivery_id": delivery_id},
        )
        data = response.json()
        return Delivery(
            **vars(notification_from_json(data)),
            job=data["job"],
            repository=data["repository"],
            next_attempt_at=_when(data["next_attempt_at"]),
        )
