"""Attempt resources: the value type, the validators and their storage on `record_attempt`."""

from __future__ import annotations

import uuid

import pytest
from core_test_helpers import half_jitter
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.models import QueryRun, QueryRunStatus, Run, TaskAttempt, TaskRun
from lakana_core.runs.resources import AttemptResources, valid_count, valid_seconds
from lakana_core.runs.service import RunService
from lakana_core.runs.tasks import ClaimedTask, FailureKind, claim_task, record_attempt


def test_merged_takes_max_memory_and_sums_the_rest() -> None:
    """Memory is a high-water mark; processor time and bytes add up."""
    a = AttemptResources(100, 1.5, 10)
    b = AttemptResources(300, 0.5, None)
    assert a.merged(b) == AttemptResources(300, 2.0, 10)


def test_merged_bytes_stay_exact_integers() -> None:
    """Large counts add without a float round-trip; an unknown side is ignored."""
    big = 2**62 + 1
    assert AttemptResources(bytes_read=big).merged(AttemptResources(bytes_read=1)).bytes_read == (
        big + 1
    )
    assert AttemptResources().merged(AttemptResources(bytes_read=7)).bytes_read == 7
    assert AttemptResources(bytes_read=7).merged(AttemptResources()).bytes_read == 7


def test_merged_keeps_none_when_both_unknown() -> None:
    """Unknown stays unknown; zero is a figure."""
    assert AttemptResources().merged(AttemptResources()) == AttemptResources()
    assert not AttemptResources().known
    assert AttemptResources(0, None, None).known


@pytest.mark.parametrize("value", [True, -1, 2**63, "5", 1.5, None])
def test_valid_count_refuses(value: object) -> None:
    """Only an int in range is a count."""
    assert valid_count(value) is None


@pytest.mark.parametrize("value", [0, 5, 2**63 - 1])
def test_valid_count_accepts(value: int) -> None:
    """The bounds are inclusive."""
    assert valid_count(value) == value


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, "1", True, None])
def test_valid_seconds_refuses(value: object) -> None:
    """Only a finite, non-negative number is a duration."""
    assert valid_seconds(value) is None


@pytest.mark.parametrize(("value", "expected"), [(0, 0.0), (2, 2.0), (0.25, 0.25)])
def test_valid_seconds_accepts(value: float, expected: float) -> None:
    """Ints and floats pass, as floats."""
    assert valid_seconds(value) == expected


def _claim(session: Session) -> ClaimedTask:
    """Claim one ready task as `w1`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    ClaimedTask
        The claim.
    """
    claimed = claim_task(session, "w1", 60)
    assert claimed is not None
    return claimed


def _figures(session: Session) -> tuple[object, object, object]:
    """Read the three figures of the only attempt.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple
        Peak memory, processor seconds, bytes read.
    """
    session.expire_all()
    attempt = session.scalars(select(TaskAttempt)).one()
    return attempt.peak_memory_bytes, attempt.cpu_seconds, attempt.bytes_read


def test_record_attempt_stores_the_figures(session: Session, run_id: uuid.UUID) -> None:
    """The figures the worker measured land on the attempt row."""
    claimed = _claim(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=None,
        rand=half_jitter,
        resources=AttemptResources(412 * 2**20, 3.1, 2**30),
    )
    assert _figures(session) == (412 * 2**20, 3.1, 2**30)


def test_a_released_attempt_without_figures_stores_null(
    session: Session, run_id: uuid.UUID
) -> None:
    """No measurement and no write run: three nulls."""
    claimed = _claim(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.RELEASED,
        rand=half_jitter,
    )
    assert _figures(session) == (None, None, None)


def _link_run(session: Session, claimed: ClaimedTask, **figures: object) -> None:
    """Give a claimed task a current write run with these figures.

    Parameters
    ----------
    session
        Database session.
    claimed
        The claim.
    **figures
        The `QueryRun` figure columns.
    """
    pipeline = session.get(Run, claimed.run_id)
    assert pipeline is not None
    write = QueryRun(
        id=uuid.uuid4(),
        environment_id=pipeline.environment_id,
        principal_id=pipeline.run_as_principal_id,
        sql_text="SELECT 1",
        status=QueryRunStatus.RUNNING,
        task_run_id=claimed.task_run_id,
        **figures,
    )
    session.add(write)
    session.flush()
    session.execute(
        update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(query_run_id=write.id)
    )
    session.commit()


def test_a_sql_task_attempt_takes_its_write_runs_figures(
    session: Session, run_id: uuid.UUID
) -> None:
    """Without figures of its own, the attempt carries its write run's."""
    claimed = _claim(session)
    _link_run(session, claimed, peak_memory_bytes=5, cpu_seconds=0.5, bytes_read=7)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=None,
        rand=half_jitter,
        resources=None,
    )
    assert _figures(session) == (5, 0.5, 7)


def test_empty_figures_do_not_fall_back_to_the_write_run(
    session: Session, run_id: uuid.UUID
) -> None:
    """Given figures win even when all unknown: only `None` means not given."""
    claimed = _claim(session)
    _link_run(session, claimed, peak_memory_bytes=5, cpu_seconds=0.5, bytes_read=7)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=None,
        rand=half_jitter,
        resources=AttemptResources(),
    )
    assert _figures(session) == (None, None, None)


def test_a_failed_attempt_keeps_what_was_measured(session: Session, run_id: uuid.UUID) -> None:
    """A partial measurement is stored as it is."""
    claimed = _claim(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.PERMANENT,
        rand=half_jitter,
        resources=AttemptResources(9, None, None),
    )
    assert _figures(session) == (9, None, None)


def test_the_attempt_view_shows_the_figures(
    session: Session, run_id: uuid.UUID, principal: PrincipalRef
) -> None:
    """`task_log` shows the three columns of each attempt."""
    claimed = _claim(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=None,
        rand=half_jitter,
        resources=AttemptResources(1, 2.0, 3),
    )
    task = session.get(TaskRun, claimed.task_run_id)
    assert task is not None
    (view,) = (
        RunService(session, AllowAllAuthorization())
        .task_log(principal, "dev", run_id, task.asset)
        .attempts
    )
    assert (view.peak_memory_bytes, view.cpu_seconds, view.bytes_read) == (1, 2.0, 3)
