"""The jobs a deploy starts after it (`on_deploy: true`): what is stored, and how it is shown.

A successful deploy that makes another project version current stores one entry per job with
``on_deploy: true`` in `DeployRun.job_starts_json`, each ``pending``, in the transaction that
moves the pointer. The worker then starts each job through `JobService.start`, the call of
``POST .../jobs/{job}/runs``, as the principal that deployed, and replaces the entry with what came
of it (`lakana_core.jobs.on_deploy`). A deploy that changes nothing (the commit already current), a
dry run, a rollback and a failed deploy store nothing, so nothing starts.

This module holds no start itself, so that `DeployService` can build its view without importing
the job service (which imports it).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lakana_core.projects.manifest import job_on_deploy, manifest_jobs

OUTCOME_PENDING = "pending"
"""Stored by the deploy; the worker has not started the job yet."""

OUTCOME_STARTED = "started"
"""A run of the job was queued (`run_id`)."""

OUTCOME_WAITING = "waiting"
"""The project was busy: the start waits for the active run (`waits_for_run_id`)."""

OUTCOME_NOT_STARTED = "not_started"
"""The job was not started; `reason` and `message` say why and what to do."""

REASON_LACKS_RUN = "lacks_run"
"""The principal that deployed may not `RUN` in the environment."""

REASON_REFUSED = "refused"
"""The start was refused as a start by hand would be (no run principal in a protected
environment, an inactive one, a selection that matches nothing, the job removed meanwhile)."""

REASON_ERROR = "error"
"""An internal error; the worker's log has it."""

REASON_LOST = "lost"
"""Shown, never stored: the start stayed ``pending`` longer than the run lease after the deploy
finished, so the worker stopped before it started the job."""


@dataclass(frozen=True)
class DeployJobStart:
    """What a deploy did with one job that starts after each deploy.

    Parameters
    ----------
    job
        The job's name.
    outcome
        ``pending``, ``started``, ``waiting`` or ``not_started``.
    run_id
        For ``started``, the queued run.
    waits_for_run_id
        For ``waiting``, the active run the start waits for.
    reason
        For ``not_started``: ``lacks_run``, ``refused``, ``error`` or ``lost``.
    message
        For ``not_started``, one sentence that names the cause and the next step.
    """

    job: str
    outcome: str
    run_id: uuid.UUID | None = None
    waits_for_run_id: uuid.UUID | None = None
    reason: str | None = None
    message: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Write the entry as it is stored, leaving out what is not set.

        Returns
        -------
        dict
            ``job`` and ``outcome``, and each of the other fields that has a value.
        """
        stored: dict[str, Any] = {"job": self.job, "outcome": self.outcome}
        if self.run_id is not None:
            stored["run_id"] = str(self.run_id)
        if self.waits_for_run_id is not None:
            stored["waits_for_run_id"] = str(self.waits_for_run_id)
        if self.reason is not None:
            stored["reason"] = self.reason
        if self.message is not None:
            stored["message"] = self.message
        return stored


def run_it_yourself(job: str, repository: str, environment: str) -> str:
    """Write the command that starts a job by hand, for a message.

    Parameters
    ----------
    job
        The job.
    repository
        Its repository.
    environment
        The environment.

    Returns
    -------
    str
        ``lakana job run JOB --repo REPO --env ENV`` in backticks.
    """
    return f"`lakana job run {job} --repo {repository} --env {environment}`"


def lost_message(job: str, repository: str, environment: str) -> str:
    """Say that a start was never made because the worker stopped.

    Parameters
    ----------
    job
        The job.
    repository
        Its repository.
    environment
        The environment.

    Returns
    -------
    str
        The cause and the command that starts the job.
    """
    return (
        f"job `{job}` was not started: the worker stopped before it started the job. Start it "
        f"with {run_it_yourself(job, repository, environment)}."
    )


def pending_job_starts(manifest: object) -> list[dict[str, Any]] | None:
    """List the jobs of a manifest that start after the deploy, each as a pending entry.

    Parameters
    ----------
    manifest
        The manifest the deploy makes current (already resolved for the environment).

    Returns
    -------
    list of dict or None
        ``{"job", "outcome": "pending"}`` per job with ``on_deploy: true``, by name; `None` when
        no job asks for it.
    """
    names = sorted(name for name, entry in manifest_jobs(manifest).items() if job_on_deploy(entry))
    if not names:
        return None
    return [DeployJobStart(job=name, outcome=OUTCOME_PENDING).to_json() for name in names]


def _uuid(value: object) -> uuid.UUID | None:
    """Read a stored id, or `None` when it is missing or damaged.

    Parameters
    ----------
    value
        The stored text.

    Returns
    -------
    uuid.UUID or None
        The id.
    """
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _text(value: object) -> str | None:
    """Read a stored text, or `None` when it is not one.

    Parameters
    ----------
    value
        The stored value.

    Returns
    -------
    str or None
        The text.
    """
    return value if isinstance(value, str) else None


def job_starts_view(
    stored: object, *, lost: bool, repository: str, environment: str
) -> tuple[DeployJobStart, ...]:
    """Read the stored job starts of a deploy for the API.

    Parameters
    ----------
    stored
        `DeployRun.job_starts_json`; untrusted, like any stored JSON.
    lost
        The deploy finished longer than the run lease ago, so an entry still ``pending`` is shown
        as ``not_started`` with the reason ``lost``.
    repository
        The repository, for the message of a lost start.
    environment
        The environment, for the message of a lost start.

    Returns
    -------
    tuple of DeployJobStart
        One per readable entry, in the stored order; an entry without a job name or an outcome is
        left out.
    """
    if not isinstance(stored, list):
        return ()
    starts: list[DeployJobStart] = []
    for entry in stored:
        if not isinstance(entry, Mapping):
            continue
        job, outcome = _text(entry.get("job")), _text(entry.get("outcome"))
        if job is None or outcome is None:
            continue
        if outcome == OUTCOME_PENDING and lost:
            starts.append(
                DeployJobStart(
                    job=job,
                    outcome=OUTCOME_NOT_STARTED,
                    reason=REASON_LOST,
                    message=lost_message(job, repository, environment),
                )
            )
            continue
        starts.append(
            DeployJobStart(
                job=job,
                outcome=outcome,
                run_id=_uuid(entry.get("run_id")),
                waits_for_run_id=_uuid(entry.get("waits_for_run_id")),
                reason=_text(entry.get("reason")),
                message=_text(entry.get("message")),
            )
        )
    return tuple(starts)
