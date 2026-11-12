"""Bound task tokens and `expected_snapshot_id` on Postgres and SeaweedFS.

A real HTTP server (uvicorn on a loopback port) on real Postgres: the races
that SQLite cannot show. A task attempt and its bound token are set up
directly in the database (`python_asset_helpers.bound_attempt`), so nothing
here installs a package; the run of a real Python asset is in
`test_python_assets_integration.py`.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta

import pyarrow as pa
import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

import lakana
from lakana.errors import AuthenticationError, Conflict, NotFound
from lakana.errors import PermissionDenied as SdkPermissionDenied
from lakana_core.catalog import service as catalog_service
from lakana_core.catalog.health import StoredStats
from lakana_core.db import create_engine, db_now
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import (
    AttemptOutcome,
    AuditEvent,
    AuditEventType,
    QueryRun,
    QueryRunStatus,
    Run,
    TaskAttempt,
    TaskRun,
    TaskStatus,
    TaskToken,
    WriteMode,
)
from lakana_core.runs.lockdown import ProtectedWriteGuard
from lakana_core.runs.store import WORKER_LOST_WRITE_MESSAGE, scheduler_tick
from lakana_server.app import create_app
from lakana_worker.pipeline import run_one
from lakana_worker.poller import poll_once
from tests.integration_helpers import backend_pid, unique_name, wait_for_lock_waiter
from tests.python_asset_helpers import (
    TOKEN,
    BoundAttempt,
    Services,
    bound_attempt,
    running_services,
    serving,
)

pytestmark = pytest.mark.integration

_IDS = pa.schema([pa.field("id", pa.int64())])
_JOIN_SECONDS = 60


def _rows(*ids: int) -> pa.Table:
    """Build a one-column table.

    Parameters
    ----------
    *ids
        The values of `id`.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table({"id": pa.array(list(ids), pa.int64())})


@dataclass(frozen=True)
class _Stack:
    """The services, a running server, and an admin client.

    Parameters
    ----------
    services
        Postgres and SeaweedFS.
    url
        The server's base URL.
    admin
        An SDK client with the bootstrap token.
    db
        An engine on the metadata database, for the test's own reads and writes.
    """

    services: Services
    url: str
    admin: lakana.Client
    db: Engine

    def client(self, token: str = TOKEN) -> lakana.Client:
        """Open another SDK client on the server.

        Parameters
        ----------
        token
            The bearer token.

        Returns
        -------
        lakana.Client
            The client; the caller closes it.
        """
        return lakana.Client(self.url, token, timeout=60.0)

    def environment(self) -> str:
        """Create an environment with `bronze.events` (ids 1 and 2).

        Returns
        -------
        str
            Its name.
        """
        name = unique_name()
        self.services.environment(self.admin, name)
        self.admin.tables.create(name, "bronze.events", schema=_IDS)
        self.admin.tables.append(name, "bronze.events", _rows(1, 2))
        return name

    def attempt(self, environment: str, *, protected: bool = False) -> BoundAttempt:
        """Start an attempt of `gold.scores`, reading `bronze.events`, and mint its token.

        Parameters
        ----------
        environment
            The environment.
        protected
            Protect the environment first.

        Returns
        -------
        BoundAttempt
            The attempt.
        """
        if protected:
            self.admin.environments.set_protection(environment, True)
        return bound_attempt(
            self.db, self.admin, environment, asset="gold.scores", inputs={"obs": "bronze.events"}
        )

    def ids(self, environment: str, table: str) -> list[int]:
        """Read a table's ids as the admin.

        Parameters
        ----------
        environment
            The environment.
        table
            ``namespace.table``.

        Returns
        -------
        list of int
            The values of `id`, sorted.
        """
        rows = self.admin.sql(
            f"SELECT id FROM {table} ORDER BY id", environment, timeout=60
        ).to_pylist()
        return [row["id"] for row in rows]


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    """Start Postgres, SeaweedFS, a real HTTP server and a worker thread for queries.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    _Stack
        The stack.

    Raises
    ------
    BaseException
        Whatever ended the query worker thread, re-raised at teardown.
    """
    with running_services(tmp_path_factory) as services:
        db = create_engine(services.settings.database)
        stop = threading.Event()
        errors: list[BaseException] = []

        def serve_queries() -> None:
            """Run queued queries until stopped, as a task-less worker would."""
            try:
                while not stop.is_set():
                    with Session(db) as session:
                        busy = poll_once(session, GrantAuthorization(session), only_queries=True)
                    if not busy:
                        stop.wait(0.05)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=serve_queries, name="lakana-test-queries", daemon=True)
        try:
            with (
                serving(create_app(services.settings)) as url,
                lakana.Client(url, TOKEN, timeout=60.0) as admin,
            ):
                worker.start()
                try:
                    yield _Stack(services=services, url=url, admin=admin, db=db)
                finally:
                    stop.set()
                    worker.join(timeout=_JOIN_SECONDS)
        finally:
            db.dispose()
        assert not worker.is_alive()
        if errors:
            raise errors[0]


def _race_two_creates(stack: _Stack, environment: str, table: str) -> list[str]:
    """Replace a missing table from two clients at once, both expecting no table.

    Parameters
    ----------
    stack
        The stack.
    environment
        The environment.
    table
        A table that does not exist.

    Returns
    -------
    list of str
        ``created``, ``conflict`` or the text of an unexpected error, per client.
    """
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def create(value: int) -> None:
        try:
            with stack.client() as client:
                barrier.wait(timeout=_JOIN_SECONDS)
                client.tables.replace(environment, table, _rows(value), expected_snapshot_id="none")
            outcomes.append("created")
        except Conflict:
            outcomes.append("conflict")
        except BaseException as error:
            outcomes.append(repr(error))

    threads = [threading.Thread(target=create, args=(value,)) for value in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_JOIN_SECONDS)
    assert not any(thread.is_alive() for thread in threads)
    return outcomes


def test_two_concurrent_creates_of_a_missing_table_exactly_one_wins(stack: _Stack) -> None:
    """`replace` with `expected_snapshot_id=none` on a missing table is a real CAS."""
    environment = stack.environment()
    for round_ in range(6):
        table = f"gold.fresh{round_}"
        outcomes = _race_two_creates(stack, environment, table)
        assert sorted(outcomes) == ["conflict", "created"], outcomes
        assert len(stack.ids(environment, table)) == 1


def test_a_bound_token_creates_and_replaces_only_its_own_table(stack: _Stack) -> None:
    """Tasks 6 and 9: `replace(none)` for a table that is not the task's asset is refused."""
    environment = stack.environment()
    stack.admin.tables.replace(environment, "gold.other", _rows(5), expected_snapshot_id="none")
    other = stack.admin.tables.get(environment, "gold.other").snapshot_id
    attempt = stack.attempt(environment)
    with stack.client(attempt.token) as task:
        for table, expected in (
            ("gold.fresh", "none"),
            ("gold.other", other),
            ("gold.other", None),
        ):
            with pytest.raises(SdkPermissionDenied, match=r"gold\.scores"):
                task.tables.replace(environment, table, _rows(7), expected_snapshot_id=expected)
        with pytest.raises(SdkPermissionDenied):
            task.tables.append(environment, "bronze.events", _rows(7))
        written = task.tables.replace(
            environment, "gold.scores", _rows(7), expected_snapshot_id="none"
        )
        assert written.rows == 1
    with pytest.raises(NotFound):
        stack.admin.tables.get(environment, "gold.fresh")
    assert stack.ids(environment, "gold.other") == [5]
    assert stack.ids(environment, "bronze.events") == [1, 2]
    assert stack.ids(environment, "gold.scores") == [7]


def test_a_bound_tokens_write_run_into_another_table_is_refused_by_the_worker(
    stack: _Stack,
) -> None:
    """A `CREATE TABLE AS` into another table is refused at submission and in `run_one`."""
    environment = stack.environment()
    attempt = stack.attempt(environment)
    with (
        stack.client(attempt.token) as task,
        pytest.raises(SdkPermissionDenied, match=r"gold\.scores"),
    ):
        task.sql("SELECT 1::BIGINT AS id", environment, into="gold.fresh", mode="create")
    with Session(stack.db) as session:
        token = session.execute(
            select(TaskToken).where(TaskToken.task_run_id == attempt.task_run_id)
        ).scalar_one()
        run = session.get(Run, attempt.run_id)
        assert run is not None
        forged = QueryRun(
            id=uuid.uuid4(),
            environment_id=run.environment_id,
            principal_id=attempt.principal_id,
            sql_text="SELECT 1::BIGINT AS id",
            status=QueryRunStatus.RUNNING,  # never claimed by the query worker thread
            write_mode=WriteMode.CREATE,
            target_name="gold.fresh",
            task_token_id=token.id,
            task_run_id=attempt.task_run_id,
        )
        session.add(forged)
        session.commit()
        session.execute(
            update(QueryRun).where(QueryRun.id == forged.id).values(status=QueryRunStatus.QUEUED)
        )
        run_one(session, forged.id, GrantAuthorization(session))
        session.rollback()
        row = session.get(QueryRun, forged.id)
        assert row is not None
        assert row.status is QueryRunStatus.FAILED
        assert "may write only its own asset 'gold.scores'" in (row.error_message or "")
    with pytest.raises(NotFound):
        stack.admin.tables.get(environment, "gold.fresh")


def _reclaim(db: Engine, attempt: BoundAttempt) -> int:
    """Run one scheduler tick, which reclaims the attempt once its lease has expired.

    Parameters
    ----------
    db
        The metadata database engine.
    attempt
        The attempt.

    Returns
    -------
    int
        What the tick returned.
    """
    with Session(db) as session:
        return scheduler_tick(session, db_now(session), lambda: 0.5)


def _expire_lease(db: Engine, attempt: BoundAttempt) -> None:
    """Put the attempt's lease in the past, as if its worker had stopped renewing.

    Parameters
    ----------
    db
        The metadata database engine.
    attempt
        The attempt.
    """
    with Session(db) as session:
        session.execute(
            update(TaskRun)
            .where(TaskRun.id == attempt.task_run_id)
            .values(lease_expires_at=db_now(session) - timedelta(seconds=5))
        )
        session.commit()


def _request_cancel(db: Engine, attempt: BoundAttempt) -> int:
    """Request the run's cancellation, in its own transaction.

    Parameters
    ----------
    db
        The metadata database engine.
    attempt
        The attempt.

    Returns
    -------
    int
        Rows updated.
    """
    with Session(db) as session:
        session.execute(update(Run).where(Run.id == attempt.run_id).values(cancel_requested=True))
        session.commit()
    return 1


_ENDINGS: dict[str, Callable[[Engine, BoundAttempt], int]] = {
    "reclaim": _reclaim,
    "cancel": _request_cancel,
}


@pytest.mark.parametrize("ending", ["reclaim", "cancel"])
@pytest.mark.parametrize("protected", [True, False], ids=["protected", "unprotected"])
def test_an_ending_between_the_check_and_the_commit_of_a_bound_upload_lands_no_write(
    stack: _Stack, monkeypatch: pytest.MonkeyPatch, ending: str, protected: bool
) -> None:
    """The guard passed for a live attempt; the `FOR SHARE` re-check sees it end."""
    environment = stack.environment()
    stack.admin.tables.replace(environment, "gold.scores", _rows(1), expected_snapshot_id="none")
    attempt = stack.attempt(environment, protected=protected)
    _expire_lease(stack.db, attempt)
    ended: list[int] = []
    original = catalog_service.commit_metadata_pointer

    def end_first(
        pointer_session: Session,
        table_id: uuid.UUID,
        *,
        expected_location: str | None,
        new_location: str,
        publish: bool = False,
        stats: StoredStats | None = None,
    ) -> None:
        if not ended:
            ended.append(_ENDINGS[ending](stack.db, attempt))
        original(
            pointer_session,
            table_id,
            expected_location=expected_location,
            new_location=new_location,
            publish=publish,
            stats=stats,
        )

    monkeypatch.setattr(catalog_service, "commit_metadata_pointer", end_first)
    with (
        stack.client(attempt.token) as task,
        pytest.raises((SdkPermissionDenied, AuthenticationError)) as refused,
    ):
        task.tables.replace(environment, "gold.scores", _rows(42))
    assert isinstance(refused.value, SdkPermissionDenied), refused.value
    assert ended and ended[0] >= 1
    assert stack.ids(environment, "gold.scores") == [1]
    with stack.client(attempt.token) as task, pytest.raises(AuthenticationError):
        task.tables.replace(environment, "gold.scores", _rows(43))
    assert stack.ids(environment, "gold.scores") == [1]


@pytest.mark.parametrize("ending", ["reclaim", "cancel"])
def test_an_ending_waits_for_a_bound_upload_that_holds_its_task_and_then_applies(
    stack: _Stack, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """The waiting side of the `FOR SHARE` lock: the upload lands, then the ending."""
    environment = stack.environment()
    stack.admin.tables.replace(environment, "gold.scores", _rows(1), expected_snapshot_id="none")
    attempt = stack.attempt(environment, protected=True)
    _expire_lease(stack.db, attempt)
    applied: list[int | BaseException] = []
    threads: list[threading.Thread] = []
    seen_waiting: list[bool] = []

    def end() -> None:
        try:
            applied.append(_ENDINGS[ending](stack.db, attempt))
        except BaseException as error:
            applied.append(error)

    original = ProtectedWriteGuard.record_pending

    def hold_then_let_the_ending_queue(self: ProtectedWriteGuard) -> None:
        original(self)  # the task run and run rows are now locked FOR SHARE
        if threads:
            return
        thread = threading.Thread(target=end)
        thread.start()
        threads.append(thread)
        wait_for_lock_waiter(stack.db, backend_pid(self._session))
        seen_waiting.append(applied == [])

    monkeypatch.setattr(ProtectedWriteGuard, "record_pending", hold_then_let_the_ending_queue)
    with stack.client(attempt.token) as task:
        written = task.tables.replace(environment, "gold.scores", _rows(42))
    assert written.rows == 1
    assert seen_waiting == [True]
    for thread in threads:
        thread.join(timeout=_JOIN_SECONDS)
        assert not thread.is_alive()
    assert all(isinstance(count, int) and count >= 1 for count in applied), applied
    assert stack.ids(environment, "gold.scores") == [42]
    with Session(stack.db) as session:
        task_row = session.get(TaskRun, attempt.task_run_id)
        run_row = session.get(Run, attempt.run_id)
        assert task_row is not None and run_row is not None
        if ending == "reclaim":
            assert task_row.status is not TaskStatus.RUNNING
            lost = session.scalars(
                select(TaskAttempt).where(TaskAttempt.task_run_id == attempt.task_run_id)
            ).one()
            assert lost.outcome is AttemptOutcome.LOST
        else:
            assert run_row.cancel_requested is True
    with stack.client(attempt.token) as task, pytest.raises(AuthenticationError):
        task.tables.replace(environment, "gold.scores", _rows(43))


def test_a_reclaim_fails_a_running_query_stamped_by_a_bound_token(stack: _Stack) -> None:
    """The task's own query, left `RUNNING` by a lost worker, fails at the reclaim."""
    environment = stack.environment()
    attempt = stack.attempt(environment)
    with Session(stack.db) as session:
        token = session.execute(
            select(TaskToken).where(TaskToken.task_run_id == attempt.task_run_id)
        ).scalar_one()
    with stack.client(attempt.token) as task:
        assert task.sql(
            "SELECT id FROM bronze.events ORDER BY id", environment, timeout=60
        ).to_pylist() == [
            {"id": 1},
            {"id": 2},
        ]
    with Session(stack.db) as session:
        served = session.scalars(select(QueryRun).where(QueryRun.task_token_id == token.id)).one()
        assert served.task_run_id == attempt.task_run_id
        assert served.status is QueryRunStatus.SUCCEEDED
        query_id = served.id
        session.execute(
            update(QueryRun)
            .where(QueryRun.id == query_id)
            .values(status=QueryRunStatus.RUNNING, finished_at=None)
        )
        session.commit()
    _expire_lease(stack.db, attempt)
    assert _reclaim(stack.db, attempt) >= 1
    with Session(stack.db) as session:
        lost = session.get(QueryRun, query_id)
        assert lost is not None
        assert (lost.status, lost.error_message) == (
            QueryRunStatus.FAILED,
            WORKER_LOST_WRITE_MESSAGE,
        )
    with stack.client(attempt.token) as task, pytest.raises(AuthenticationError):
        task.sql("SELECT id FROM bronze.events", environment, timeout=60)


def test_minting_for_another_principal_needs_instance_admin_also_through_a_group(
    stack: _Stack,
) -> None:
    """`MINT_TASK_TOKEN` mints for the caller; `ADMIN` via a group for anyone."""
    environment = stack.environment()
    admin = stack.admin
    minter, operator, subject, group = (f"{kind}-{unique_name()}" for kind in "mosg")
    for name in (minter, operator, subject):
        admin.principals.create(name, "USER")
    admin.grants.grant(principal=minter, action="MINT_TASK_TOKEN", kind="INSTANCE")
    admin.groups.create(group)
    admin.groups.add_member(group, operator)
    admin.grants.grant(group=group, role="ADMIN", kind="INSTANCE")
    with stack.client(admin.tokens.issue(minter, "pat").token) as as_minter:
        own = as_minter.task_tokens.mint(minter, environment, ["gold.x"], 60)
        assert own.token.startswith("lakana_task_")
        for name in (subject, "nobody-" + unique_name()):
            with pytest.raises(SdkPermissionDenied, match="only for itself"):
                as_minter.task_tokens.mint(name, environment, ["gold.x"], 60)
    with stack.client(admin.tokens.issue(operator, "pat").token) as as_operator:
        minted = as_operator.task_tokens.mint(subject, environment, ["gold.x"], 60)
    with stack.client(minted.token) as as_subject:
        assert as_subject.whoami().name == subject
    with Session(stack.db) as session:
        denied = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
        ).all()
        refused = [e.detail for e in denied if (e.detail or {}).get("reason") == "not_self"]
        assert subject in {detail["subject"] for detail in refused}
        assert {detail["action"] for detail in refused} == {"MINT_TASK_TOKEN"}
        minted_events = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TASK_TOKEN_MINTED)
        ).all()
        assert any(minted.key_id in str(event.detail) for event in minted_events)
