"""`RunService.task_attempts`: one task's attempts across runs, newest first, in pages."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.errors import ConflictError, NotFoundError, ValidationError
from lakana_core.models import (
    AttemptOutcome,
    JobVersion,
    Repository,
    RepositoryKind,
    Run,
    RunStatus,
    TaskAttempt,
    TaskRun,
)
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService, TaskHistoryEntry

_T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _service(session: Session) -> RunService:
    """Build the service with allow-all authorization.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    RunService
        The service.
    """
    return RunService(session, AllowAllAuthorization())


def _history(
    session: Session, principal: PrincipalRef, run_id: uuid.UUID
) -> tuple[list[uuid.UUID], str]:
    """Give three runs of job ``nightly`` 25 attempts of task ``silver.a`` between them.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    run_id
        The run that exists already (the first).

    Returns
    -------
    tuple of (list of uuid.UUID, str)
        The three run ids, oldest first, and the task's name.
    """
    service = _service(session)
    runs = [run_id]
    for _ in range(2):
        for run in session.scalars(select(Run)):
            run.status = RunStatus.SUCCEEDED
        session.flush()
        runs.append(service.start(principal, "dev", "analytics", Selection()).id)
    n = 0
    for index, rid in enumerate(runs):
        session.get(Run, rid).job = "nightly"  # ty: ignore[invalid-assignment]
        task = session.scalars(
            select(TaskRun).where(TaskRun.run_id == rid, TaskRun.asset == "silver.a")
        ).one()
        for attempt in range(1, (9 if index < 2 else 7) + 1):
            n += 1
            session.add(
                TaskAttempt(
                    id=uuid.uuid4(),
                    task_run_id=task.id,
                    attempt=attempt,
                    worker="w",
                    started_at=_T0 + timedelta(minutes=n),
                    finished_at=_T0 + timedelta(minutes=n, seconds=30),
                    outcome=AttemptOutcome.SUCCEEDED,
                    peak_memory_bytes=n,
                )
            )
    session.commit()
    return runs, "silver.a"


def test_the_newest_attempts_come_first_and_the_cursor_continues(
    session: Session, run_id: uuid.UUID, principal: PrincipalRef
) -> None:
    """25 attempts over 3 runs: 20 newest, then the other 5, none twice."""
    runs, task = _history(session, principal, run_id)
    service = _service(session)
    first, cursor = service.task_attempts(principal, "dev", "nightly", task)
    assert len(first) == 20
    assert isinstance(first[0], TaskHistoryEntry)
    assert [e.started_at for e in first] == sorted((e.started_at for e in first), reverse=True)
    assert first[0].peak_memory_bytes == 25
    assert cursor is not None
    rest, last = service.task_attempts(principal, "dev", "nightly", task, cursor=cursor)
    assert [e.peak_memory_bytes for e in rest] == [5, 4, 3, 2, 1]
    assert last is None
    assert {e.run_id for e in first + rest} == set(runs)
    entry = first[0]
    assert (entry.status, entry.duration_seconds, entry.failure_code) == ("succeeded", 30.0, None)


def test_a_small_limit_pages_through_the_history(
    session: Session, run_id: uuid.UUID, principal: PrincipalRef
) -> None:
    """Pages of 10 give 10, 10, 5."""
    _, task = _history(session, principal, run_id)
    service = _service(session)
    sizes: list[int] = []
    cursor = None
    while True:
        page, cursor = service.task_attempts(
            principal, "dev", "nightly", task, limit=10, cursor=cursor
        )
        sizes.append(len(page))
        if cursor is None:
            break
    assert sizes == [10, 10, 5]


def test_an_attempt_without_an_end_is_running(
    session: Session, run_id: uuid.UUID, principal: PrincipalRef
) -> None:
    """No outcome yet: status ``running`` and no duration."""
    _, task = _history(session, principal, run_id)
    newest = session.scalars(select(TaskAttempt).order_by(TaskAttempt.started_at.desc())).first()
    assert newest is not None
    newest.outcome = None
    newest.finished_at = None
    session.commit()
    (entry, *_), _ = _service(session).task_attempts(principal, "dev", "nightly", task, limit=1)
    assert (entry.status, entry.duration_seconds) == ("running", None)


@pytest.mark.parametrize("limit", [0, 201])
def test_the_limit_is_between_1_and_200(
    session: Session, run_id: uuid.UUID, principal: PrincipalRef, limit: int
) -> None:
    """Outside the range: a plain message."""
    with pytest.raises(ValidationError, match="between 1 and 200"):
        _service(session).task_attempts(principal, "dev", "nightly", "silver.a", limit=limit)


def test_an_unknown_job_or_task_is_not_found(
    session: Session, run_id: uuid.UUID, principal: PrincipalRef
) -> None:
    """The message names both and the command that lists jobs."""
    _history(session, principal, run_id)
    with pytest.raises(NotFoundError, match=r"'silver.a'.*'nope'.*lakana job list"):
        _service(session).task_attempts(principal, "dev", "nope", "silver.a")
    with pytest.raises(NotFoundError, match=r"'ghost'.*'nightly'.*lakana job list"):
        _service(session).task_attempts(principal, "dev", "nightly", "ghost")


def test_two_repositories_with_the_job_need_a_name(
    session: Session, run_id: uuid.UUID, principal: PrincipalRef
) -> None:
    """409 without `repository`; with it only that repository's attempts."""
    runs, task = _history(session, principal, run_id)
    other = Repository(
        id=uuid.uuid4(), name="other", kind=RepositoryKind.HTTPS, url="https://example.invalid/x"
    )
    session.add(other)
    session.flush()
    moved = session.get(Run, runs[2])
    assert moved is not None
    moved.repository_id = other.id
    session.commit()
    service = _service(session)
    with pytest.raises(ConflictError, match="--repo"):
        service.task_attempts(principal, "dev", "nightly", task)
    mine, _ = service.task_attempts(principal, "dev", "nightly", task, repository="other")
    assert {e.run_id for e in mine} == {runs[2]}
    theirs, _ = service.task_attempts(principal, "dev", "nightly", task, repository="analytics")
    assert {e.run_id for e in theirs} == set(runs[:2])


def _declare_job(session: Session, name: str) -> None:
    """Declare job `name` in the manifests of every deployed project version.

    Parameters
    ----------
    session
        Database session.
    name
        The job's name.
    """
    for version in session.scalars(select(JobVersion)):
        version.manifest_json = {**version.manifest_json, "jobs": {name: {"select": ["silver.*"]}}}
    session.commit()


def test_a_declared_task_that_never_ran_has_an_empty_history(
    session: Session, run_id: uuid.UUID, principal: PrincipalRef
) -> None:
    """The job and the task are declared but nothing ran them: an empty page, not a 404."""
    _declare_job(session, "nightly")
    service = _service(session)
    assert service.task_attempts(principal, "dev", "nightly", "silver.a") == ([], None)
    named = service.task_attempts(principal, "dev", "nightly", "silver.a", repository="analytics")
    assert named == ([], None)


def test_a_declared_job_with_an_unknown_task_is_not_found(
    session: Session, run_id: uuid.UUID, principal: PrincipalRef
) -> None:
    """A task the project does not declare is a typo: 404, naming both."""
    _declare_job(session, "nightly")
    with pytest.raises(NotFoundError, match=r"'ghost'.*'nightly'.*lakana job list"):
        _service(session).task_attempts(principal, "dev", "nightly", "ghost")
    with pytest.raises(NotFoundError, match=r"'silver.a'.*'other'"):
        _service(session).task_attempts(principal, "dev", "other", "silver.a")
