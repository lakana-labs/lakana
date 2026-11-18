"""The failed-runs rule that Home's attention list and the Jobs page's failed list share."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from home_test_helpers import deployed_job, finished_run, grant, person
from sqlalchemy import update
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef, Securable
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.models import Environment, RunStatus
from lakana_core.runs.failed import FAILED_RUN_WINDOW, FailedRun, failed_runs, run_environments

_NOW: list[datetime] = []
"""The test's start, whole seconds, so a row's time and the expected one are the same moment."""


@pytest.fixture(autouse=True)
def _start() -> None:
    """Fix the moment `_ago` counts back from, once per test."""
    _NOW[:] = [datetime.now(UTC).replace(microsecond=0)]


def _ago(**delta: float) -> datetime:
    """Name a moment before the test's start.

    Parameters
    ----------
    **delta
        `timedelta` arguments.

    Returns
    -------
    datetime
        The start minus the delta, in UTC.
    """
    return _NOW[0] - timedelta(**delta)


def _failed(session: Session, p: PrincipalRef) -> list[FailedRun]:
    """List the failed runs the caller may see within the window, with real grants.

    Parameters
    ----------
    session
        Database session.
    p
        The caller.

    Returns
    -------
    list of FailedRun
        Newest first.
    """
    return failed_runs(session, GrantAuthorization(session), p, since=_ago() - FAILED_RUN_WINDOW)


@pytest.fixture
def admin(session: Session) -> PrincipalRef:
    """Persist an administrator.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        Their reference.
    """
    ref = person(session, "ada")
    grant(session, ref, role=Role.ADMIN)
    return ref


def test_the_newest_finished_run_of_each_job_counts(session: Session, admin: PrincipalRef) -> None:
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    finished_run(
        session, repo, version, job="nightly", status=RunStatus.FAILED, finished_at=_ago(hours=2)
    )
    finished_run(
        session, repo, version, job="nightly", status=RunStatus.SUCCEEDED, finished_at=_ago(hours=1)
    )
    hourly = finished_run(
        session, repo, version, job="hourly", status=RunStatus.FAILED, finished_at=_ago(hours=3)
    )
    assert _failed(session, admin) == [
        FailedRun(hourly.id, "dev", "shop", "hourly", _ago(hours=3)),
    ]


def test_a_later_cancelled_run_hides_an_earlier_failure(
    session: Session, admin: PrincipalRef
) -> None:
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    finished_run(
        session, repo, version, job="nightly", status=RunStatus.FAILED, finished_at=_ago(hours=2)
    )
    finished_run(
        session, repo, version, job="nightly", status=RunStatus.CANCELLED, finished_at=_ago(hours=1)
    )
    assert _failed(session, admin) == []


def test_a_failed_run_of_no_job_is_listed_by_repository(
    session: Session, admin: PrincipalRef
) -> None:
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    whole = finished_run(
        session, repo, version, job=None, status=RunStatus.FAILED, finished_at=_ago(hours=2)
    )
    assert _failed(session, admin) == [FailedRun(whole.id, "dev", "shop", None, _ago(hours=2))]


def test_a_run_older_than_seven_days_is_not_listed(session: Session, admin: PrincipalRef) -> None:
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    finished_run(
        session, repo, version, job="nightly", status=RunStatus.FAILED, finished_at=_ago(days=8)
    )
    assert _failed(session, admin) == []


def _two_environments(session: Session) -> tuple[FailedRun, FailedRun]:
    """Persist a failed job run in `dev` and one in `prod`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple of (FailedRun, FailedRun)
        The rows expected for `dev` and for `prod`.
    """
    expected = []
    for env, hours in (("dev", 1), ("prod", 2)):
        repo, version = deployed_job(session, env, f"shop_{env}", "nightly")
        run = finished_run(
            session,
            repo,
            version,
            job="nightly",
            status=RunStatus.FAILED,
            finished_at=_ago(hours=hours),
        )
        expected.append(FailedRun(run.id, env, f"shop_{env}", "nightly", _ago(hours=hours)))
    return expected[0], expected[1]


def test_only_environments_where_the_caller_may_read_runs(session: Session) -> None:
    dev, _ = _two_environments(session)
    reader = person(session, "rita")
    grant(session, reader, action=Action.READ_ENVIRONMENT, environment_name="dev")
    assert _failed(session, reader) == [dev]


def test_run_alone_does_not_list_the_environments_failures(session: Session) -> None:
    _two_environments(session)
    runner = person(session, "ron")
    grant(session, runner, action=Action.RUN, environment_name="dev")
    assert _failed(session, runner) == []


class _BatchSpy:
    """Answer as grants do and count each kind of question.

    Parameters
    ----------
    inner
        The grant authorization that answers.
    """

    def __init__(self, inner: GrantAuthorization) -> None:
        self._inner = inner
        self.batches: list[list[tuple[Action, Securable]]] = []
        self.single = 0

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Pass a check on.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.
        """
        self._inner.authorize(principal, action, securable)

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Count one question and answer it.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Returns
        -------
        bool
            The grants' answer.
        """
        self.single += 1
        return self._inner.permits(principal, action, securable)

    def permits_each(
        self, principal: PrincipalRef, questions: Sequence[tuple[Action, Securable]]
    ) -> list[bool]:
        """Note one batch and answer it.

        Parameters
        ----------
        principal
            The actor.
        questions
            The questions.

        Returns
        -------
        list of bool
            The grants' answers.
        """
        self.batches.append(list(questions))
        return self._inner.permits_each(principal, questions)


def test_the_environments_are_asked_in_one_batch(session: Session) -> None:
    dev, _ = _two_environments(session)
    reader = person(session, "rita")
    grant(session, reader, action=Action.READ_ENVIRONMENT, environment_name="dev")
    spy = _BatchSpy(GrantAuthorization(session))
    assert list(run_environments(session, spy, reader).values()) == ["dev"]
    assert failed_runs(session, spy, reader, since=_ago(days=7)) == [dev]
    assert spy.single == 0
    assert [sorted(s.environment or "" for _, s in batch) for batch in spy.batches] == [
        ["dev", "prod"],
        ["dev", "prod"],
    ]
    assert {action for batch in spy.batches for action, _ in batch} == {Action.READ_ENVIRONMENT}


def test_a_deleted_environment_is_left_out(session: Session, admin: PrincipalRef) -> None:
    dev, _ = _two_environments(session)
    session.execute(
        update(Environment).where(Environment.name == "prod").values(deleted_at=datetime.now(UTC))
    )
    session.commit()
    assert _failed(session, admin) == [dev]
    assert list(run_environments(session, GrantAuthorization(session), admin).values()) == ["dev"]
