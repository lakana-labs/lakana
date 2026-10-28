"""The detail of the audit events about starts that wait or are dropped.

`JOB_START_WAITING` and `JOB_START_DROPPED` are written from several places: the park of a
start, the re-check of a waiting start, a scheduled fire that did not start, a deploy or an
undeploy that deleted the job. Built in one place, they carry the same fields on every path, so
that a person who filters the audit log for a job reads every event the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def start_detail(
    repository: str, job: str, pending: Mapping[str, Any] | None, **extra: object
) -> dict[str, Any]:
    """Build the detail of an event about one start of a job.

    Parameters
    ----------
    repository
        The repository's name.
    job
        The job's name.
    pending
        The stored waiting start (``pending_start``), or `None` when there is none to describe.
        Missing keys read as: no requester, no scheduled time, nothing missed, one request.
    **extra
        Fields of the event itself (``reason``, ``waits_for_run_id``, ``replaced``).

    Returns
    -------
    dict
        ``repository``, ``job``, ``trigger``, ``requested_by``, ``scheduled_for``, ``missed``
        and ``merged``, ``ci_rule_id`` for the start of a CI deploy's ``on_deploy`` job, then
        `extra`.
    """
    stored = pending or {}
    rule = {"ci_rule_id": stored["ci_rule_id"]} if stored.get("ci_rule_id") else {}
    return {
        "repository": repository,
        "job": job,
        "trigger": stored.get("trigger"),
        "requested_by": stored.get("requested_by"),
        "scheduled_for": stored.get("scheduled_for"),
        "missed": stored.get("missed", 0),
        "merged": stored.get("merged", 1),
        **rule,
        **extra,
    }
