"""Two sessions on a real PostgreSQL: a submit and an undeploy of one repository take turns (#838).

Under READ COMMITTED an undeploy's check does not see a deploy queued in a transaction that has not
committed, so without a lock both commit and the queued deploy deploys the repository again behind
the undeploy's back. The scenarios are the SQLite ones (`test_undeploy_queued_deploys.py`); here the
waiting session must wait on a lock, not merely be slower.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from test_undeploy_queued_deploys import (
    InFlight,
    Waiter,
    a_pointer_moved_meanwhile_is_not_deleted,
    a_pointer_removed_meanwhile_is_not_found,
    a_submit_first_refuses_the_undeploy,
    an_undeploy_first_lets_the_submit_deploy_again,
)
from testcontainers.postgres import PostgresContainer

from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Start one Postgres.

    Yields
    ------
    Engine
        An engine on it.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        yield engine
        engine.dispose()


def _waiting_on_the_queue_lock(engine: Engine) -> Waiter:
    """Build the check that a thread's backend waits for the deploy-queue lock.

    Parameters
    ----------
    engine
        The Postgres engine.

    Returns
    -------
    Waiter
        Returns once the thread is executing `pg_advisory_xact_lock` and its own backend (by pid)
        waits on an advisory lock in `pg_stat_activity`; fails after 30 s or when the thread ends.
    """

    def wait(thread: threading.Thread, in_flight: InFlight) -> None:
        """Poll the thread's statement and its backend until it waits for the advisory lock.

        Parameters
        ----------
        thread
            The thread that should be waiting.
        in_flight
            What each thread executes, with its backend's pid.

        Raises
        ------
        AssertionError
            If the thread ended, or did not wait within 30 s.
        """
        deadline = time.monotonic() + 30.0
        with engine.connect() as connection:
            while time.monotonic() < deadline and thread.is_alive():
                current = in_flight.of(thread)
                if current is not None and "pg_advisory_xact_lock" in current[0]:
                    waiting = connection.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE pid = :pid "
                            "AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                        ),
                        {"pid": current[1]},
                    ).scalar_one()
                    if waiting:
                        return
                connection.rollback()
                time.sleep(0.05)
        raise AssertionError("the second session never waited for the deploy-queue lock")

    return wait


def test_a_submit_first_refuses_the_undeploy(engine: Engine, tmp_path: Path) -> None:
    """The undeploy waits for the submit's commit and then sees its queued deploy."""
    a_submit_first_refuses_the_undeploy(engine, tmp_path, _waiting_on_the_queue_lock(engine))


def test_an_undeploy_first_lets_the_submit_deploy_again(engine: Engine, tmp_path: Path) -> None:
    """The submit waits for the undeploy's commit; its deploy then makes a new pointer."""
    an_undeploy_first_lets_the_submit_deploy_again(
        engine, tmp_path, _waiting_on_the_queue_lock(engine)
    )


def test_a_pointer_moved_meanwhile_is_not_deleted(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deploy that commits between the undeploy's read and its delete stays deployed."""
    a_pointer_moved_meanwhile_is_not_deleted(engine, tmp_path, monkeypatch)


def test_a_pointer_removed_meanwhile_is_not_found(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undeploy that commits in between leaves the second one a 404."""
    a_pointer_removed_meanwhile_is_not_found(engine, tmp_path, monkeypatch)
