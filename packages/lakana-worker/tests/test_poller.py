"""Tests for the dequeue loop."""

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, AuthorizationService, PrincipalRef
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, QueryLimitsConfig, StorageConfig
from lakana_core.db import create_engine, db_now
from lakana_core.models import (
    AuditEvent,
    Base,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    JobVersion,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    TaskRun,
    TaskStatus,
    Worker,
    WriteMode,
)
from lakana_core.runs import workers
from lakana_core.runs.store import end_lost_runs
from lakana_core.runs.tasks import ClaimedTask
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import poller
from lakana_worker.poller import WorkerPool, _next_query, poll_forever, poll_iteration, poll_once
from lakana_worker.tasks import RunEngine, TaskSettings


class _StopLoop(Exception):
    """Raised from a patched `time.sleep` to break out of `poll_forever`'s loop."""


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Provide the acting principal, persisted so `QueryRun.principal_id`'s FK resolves.

    Parameters
    ----------
    session
        Database session to persist the backing `Principal` row into.

    Returns
    -------
    PrincipalRef
        The acting principal, whose id matches a persisted `Principal` row.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name="admin"))
    session.commit()
    return PrincipalRef(id=principal_id, name="admin", type=PrincipalType.USER)


def test_poll_once_returns_false_when_the_queue_is_empty(session: Session) -> None:
    """An empty queue is a no-op, not an error."""
    assert poll_once(session, AllowAllAuthorization()) is False


def test_poll_once_claims_and_runs_the_oldest_queued_run(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A queued run is claimed, handed to `run_one`, and reported as claimed."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT 1")
    authz = AllowAllAuthorization()

    with mock.patch("lakana_worker.poller.run_one") as run_one_mock:
        claimed = poll_once(session, authz)

    assert claimed is True
    run_one_mock.assert_called_once_with(
        session,
        run.id,
        authz,
        None,
        limits=None,
        worker_id=None,
        running_cap=None,
        on_cap_timeout=None,
    )


def test_poll_forever_sleeps_only_after_an_empty_poll() -> None:
    """The loop skips the sleep when it just claimed a run, and sleeps when it did not."""
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    Base.metadata.create_all(engine)
    calls: list[bool] = []

    def fake_poll_once(
        session: Session,
        authz: AuthorizationService,
        key_provider: object = None,
        limits: object = None,
        deploy: object = None,
        engine: object = None,
        only_queries: object = False,
        **kwargs: object,
    ) -> bool:
        claimed = len(calls) == 0
        calls.append(claimed)
        return claimed

    try:
        with (
            mock.patch("lakana_worker.poller.poll_once", side_effect=fake_poll_once),
            mock.patch("lakana_worker.poller.time.sleep", side_effect=_StopLoop) as sleep_mock,
            pytest.raises(_StopLoop),
        ):
            poll_forever(
                lambda: Session(engine), lambda _: AllowAllAuthorization(), poll_interval_seconds=0
            )
    finally:
        engine.dispose()

    assert calls == [True, False]
    sleep_mock.assert_called_once_with(0)


def test_poll_forever_logs_a_failing_iteration_and_keeps_going() -> None:
    """An exception from one iteration must not take the worker process down.

    `run_one` already guarantees this for a bad query; everything outside a
    query — a dropped connection while claiming a row — had no such guard,
    and the first transient database error stopped the queue draining for
    everyone.
    """
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    Base.metadata.create_all(engine)
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky_poll_once(
        session: Session,
        authz: AuthorizationService,
        key_provider: object = None,
        limits: object = None,
        deploy: object = None,
        engine: object = None,
        only_queries: object = False,
        **kwargs: object,
    ) -> bool:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("transient database failure")
        return False

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise _StopLoop

    try:
        with (
            mock.patch("lakana_worker.poller.poll_once", side_effect=flaky_poll_once),
            mock.patch("lakana_worker.poller.time.sleep", side_effect=fake_sleep),
            mock.patch.object(poller.logger, "exception") as log_mock,
            pytest.raises(_StopLoop),
        ):
            poll_forever(
                lambda: Session(engine), lambda _: AllowAllAuthorization(), poll_interval_seconds=0
            )
    finally:
        engine.dispose()

    # The logger itself is spied on rather than captured: `test_migrations.py`
    # runs Alembic, whose `fileConfig` disables every logger that already
    # exists, so `caplog` sees nothing when the whole suite runs in one process.
    assert calls == [0, 1]
    log_mock.assert_called_once()


def test_poll_iteration_reports_a_failed_iteration_as_unclaimed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reporting a failure as "nothing claimed" makes the caller back off, not spin."""
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    Base.metadata.create_all(engine)

    try:
        with mock.patch("lakana_worker.poller.poll_once", side_effect=RuntimeError("boom")):
            assert (
                poll_iteration(lambda: Session(engine), lambda _: AllowAllAuthorization()) is False
            )
    finally:
        engine.dispose()
    assert "poll_failed" in {getattr(r, "event", None) for r in caplog.records}


def test_poll_iteration_authorizes_through_the_session_it_runs_the_work_against() -> None:
    """The authorization service is built from the very session `poll_once` works with.

    `GrantAuthorization` queries grants through its session, so one built from another session
    (or one shared across iterations) would decide on a different transaction's view of the
    grants than the work it permits. The factory's argument, the session `poll_once` receives
    and the service `poll_once` receives must be one chain, fresh on every iteration.
    """
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    Base.metadata.create_all(engine)
    factory_sessions: list[Session] = []
    built: list[AuthorizationService] = []
    served: list[tuple[Session, object]] = []

    def authz_factory(session: Session) -> AuthorizationService:
        factory_sessions.append(session)
        built.append(AllowAllAuthorization())
        return built[-1]

    def fake_poll_once(
        session: Session, authz: AuthorizationService, *args: object, **kw: object
    ) -> bool:
        served.append((session, authz))
        return True

    try:
        with mock.patch("lakana_worker.poller.poll_once", side_effect=fake_poll_once):
            assert poll_iteration(lambda: Session(engine), authz_factory) is True
            assert poll_iteration(lambda: Session(engine), authz_factory) is True
    finally:
        engine.dispose()

    assert len(factory_sessions) == len(served) == len(built) == 2
    for built_from, (worked_with, authz), service in zip(
        factory_sessions, served, built, strict=True
    ):
        assert built_from is worked_with
        assert authz is service, "poll_once gets the service built for its own session"
    assert factory_sessions[0] is not factory_sessions[1], "a fresh session per iteration"


def _deploy_world(session: Session, principal: PrincipalRef, tmp_path: Path) -> uuid.UUID:
    """Create `dev`, a repository row and one queued deploy run.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    tmp_path
        Storage root.

    Returns
    -------
    uuid.UUID
        The queued deploy run's id.
    """
    from lakana_core.models import DeployKind, Repository, RepositoryKind
    from lakana_core.projects.deploys import DeployService

    CatalogService(session, StorageResolver(session), AllowAllAuthorization()).create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    session.add(Repository(id=uuid.uuid4(), name="r", kind=RepositoryKind.HTTPS, url="https://h/r"))
    session.commit()
    return (
        DeployService(session, AllowAllAuthorization())
        .submit(principal, "dev", repository="r", kind=DeployKind.DEPLOY)
        .id
    )


def test_poll_once_runs_a_queued_deploy(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """With only a deploy queued, `run_deploy` gets it with the deploy settings."""
    from lakana_worker.deploy import DeploySettings

    run_id = _deploy_world(session, principal, tmp_path)
    settings = DeploySettings(repo_roots=("/r",))
    authz = AllowAllAuthorization()
    with mock.patch("lakana_worker.poller.run_deploy") as run_deploy_mock:
        assert poll_once(session, authz, deploy=settings) is True
    run_deploy_mock.assert_called_once_with(session, run_id, authz, None, settings, worker_id=None)


def test_poll_once_takes_the_oldest_item_across_both_queues(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A deploy queued before a query runs first; then the query."""
    from lakana_worker.deploy import DeploySettings

    run_id = _deploy_world(session, principal, tmp_path)
    QueryService(session, AllowAllAuthorization()).submit_query(principal, "dev", "SELECT 1")
    authz = AllowAllAuthorization()
    with (
        mock.patch("lakana_worker.poller.run_deploy") as run_deploy_mock,
        mock.patch("lakana_worker.poller.run_one") as run_one_mock,
    ):
        poll_once(session, authz)
        run_deploy_mock.assert_called_once_with(
            session, run_id, authz, None, DeploySettings(), worker_id=None
        )
        run_one_mock.assert_not_called()


def test_poll_once_prefers_an_older_query(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A query queued before the deploy runs first."""
    from datetime import UTC, datetime, timedelta

    from lakana_core.models import DeployRun

    run_id = _deploy_world(session, principal, tmp_path)
    query = QueryService(session, AllowAllAuthorization()).submit_query(
        principal, "dev", "SELECT 1"
    )
    deploy_row = session.get(DeployRun, run_id)
    assert deploy_row is not None
    deploy_row.created_at = datetime.now(UTC) + timedelta(minutes=1)
    session.commit()
    authz = AllowAllAuthorization()
    with (
        mock.patch("lakana_worker.poller.run_deploy") as run_deploy_mock,
        mock.patch("lakana_worker.poller.run_one") as run_one_mock,
    ):
        poll_once(session, authz)
    run_one_mock.assert_called_once_with(
        session,
        query.id,
        authz,
        None,
        limits=None,
        worker_id=None,
        running_cap=None,
        on_cap_timeout=None,
    )
    run_deploy_mock.assert_not_called()


def test_poll_iteration_passes_the_deploy_settings_down(session: Session) -> None:
    """`poll_iteration` hands its deploy settings to `poll_once`."""
    from lakana_worker.deploy import DeploySettings

    settings = DeploySettings(repo_roots=("/r",))
    authz = AllowAllAuthorization()
    with mock.patch("lakana_worker.poller.poll_once", return_value=False) as poll_once_mock:
        assert poll_iteration(lambda: session, lambda _s: authz, deploy=settings) is False
    poll_once_mock.assert_called_once_with(
        session,
        authz,
        None,
        limits=None,
        deploy=settings,
        engine=None,
        only_queries=False,
        worker_id=None,
        may_claim=None,
        pool=None,
        passed_by=None,
    )


def test_poll_once_runs_a_ready_task_when_no_query_or_deploy_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tasks after queries and deploys; nothing is claimed while draining."""
    from lakana_worker.tasks import RunEngine, TaskSettings

    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/p.db"))
    Base.metadata.create_all(engine)
    ran: list[object] = []
    claims: list[tuple[object, ...]] = []
    claimed = object()

    def claim(*args: object) -> object:
        claims.append(args[1:])
        return claimed

    monkeypatch.setattr("lakana_worker.poller.claim_task", claim)
    monkeypatch.setattr(
        "lakana_worker.poller.run_claimed", lambda e, c, serve_queries: ran.append(c)
    )
    run_engine = RunEngine(
        session_factory=lambda: Session(engine),
        authz_factory=lambda _s: AllowAllAuthorization(),
        settings=TaskSettings(worker_id="w1", lease_seconds=45),
    )
    with Session(engine) as session:
        assert poll_once(session, AllowAllAuthorization(), engine=run_engine) is True
        assert ran == [claimed]
        assert claims == [("w1", 45)]
        monkeypatch.setattr("lakana_worker.poller.claim_task", lambda *args: None)
        assert poll_once(session, AllowAllAuthorization(), engine=run_engine) is False
        run_engine.drain.set()
        claims.clear()
        monkeypatch.setattr("lakana_worker.poller.claim_task", claim)
        assert poll_once(session, AllowAllAuthorization(), engine=run_engine) is False
        assert claims == []
    engine.dispose()


def test_poll_forever_returns_once_draining() -> None:
    """SIGTERM sets the drain event; the loop stops claiming and returns."""
    drain = threading.Event()
    drain.set()
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    with mock.patch("lakana_worker.poller.poll_iteration") as iteration:
        poll_forever(lambda: Session(engine), lambda _s: AllowAllAuthorization(), drain=drain)
    iteration.assert_not_called()
    engine.dispose()


def test_poll_forever_polls_until_the_drain_is_set() -> None:
    """The loop runs iterations (passing the engine down) until draining."""
    drain = threading.Event()
    from lakana_worker.tasks import RunEngine, TaskSettings

    engine = create_engine(DatabaseConfig(url="sqlite://"))
    sentinel = RunEngine(
        session_factory=lambda: Session(engine),
        authz_factory=lambda _s: AllowAllAuthorization(),
        settings=TaskSettings(worker_id="w1"),
    )
    seen: list[object] = []

    def iteration(*args: object, engine: object = None, **kwargs: object) -> bool:
        seen.append(engine)
        drain.set()
        return True

    with mock.patch("lakana_worker.poller.poll_iteration", iteration):
        poll_forever(
            lambda: Session(engine),
            lambda _s: AllowAllAuthorization(),
            engine=sentinel,
            drain=drain,
        )
    assert seen == [sentinel]
    engine.dispose()


def test_a_claimed_task_gets_a_query_only_serve_callback(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a Python task runs, the loop serves query runs and claims nothing else."""
    engine = RunEngine(
        session_factory=lambda: Session(session.get_bind()),
        authz_factory=lambda _s: AllowAllAuthorization(),
        settings=TaskSettings(worker_id="w1"),
    )
    claimed = ClaimedTask(uuid.uuid4(), uuid.uuid4(), "gold.scores", 1, 60, 1)
    captured: list[Callable[[], bool] | None] = []
    claims = [claimed]
    asked: list[str] = []

    def claim(*_args: object) -> ClaimedTask | None:
        asked.append("claim")
        return claims.pop() if claims else None

    def run(_engine: RunEngine, _claimed: ClaimedTask, serve_queries: Callable[[], bool]) -> None:
        captured.append(serve_queries)

    monkeypatch.setattr(poller, "claim_task", claim)
    monkeypatch.setattr(poller, "run_claimed", run)
    assert poll_once(session, AllowAllAuthorization(), engine=engine) is True
    (serve,) = captured
    assert serve is not None
    monkeypatch.setattr(poller, "run_deploy", lambda *_a, **_k: pytest.fail("a deploy was claimed"))
    assert serve() is False
    assert asked == ["claim"]


def test_the_serve_callback_runs_a_queued_query_with_the_limits_of_the_loop(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callback is a query-only `poll_iteration`: a fresh session, the loop's limits."""
    CatalogService(session, StorageResolver(session), AllowAllAuthorization()).create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    engine = RunEngine(
        session_factory=lambda: Session(session.get_bind()),
        authz_factory=lambda _s: AllowAllAuthorization(),
        settings=TaskSettings(worker_id="w1"),
    )
    limits = QueryLimitsConfig(max_result_rows=7)
    served: list[bool] = []

    def run(_engine: RunEngine, _claimed: ClaimedTask, serve_queries: Callable[[], bool]) -> None:
        queued = QueryService(session, AllowAllAuthorization()).submit_query(
            principal, "dev", "SELECT 1"
        )
        with mock.patch("lakana_worker.poller.run_one") as run_one_mock:
            served.append(serve_queries())
        (call,) = run_one_mock.call_args_list
        assert call.args[1] == queued.id
        assert call.args[0] is not session
        assert call.kwargs == {
            "limits": limits,
            "worker_id": None,
            "running_cap": None,
            "on_cap_timeout": None,
        }

    claimed = ClaimedTask(uuid.uuid4(), uuid.uuid4(), "gold.scores", 1, 60, 1)
    claims = [claimed]
    monkeypatch.setattr(poller, "claim_task", lambda *_a: claims.pop() if claims else None)
    monkeypatch.setattr(poller, "run_claimed", run)
    assert poll_once(session, AllowAllAuthorization(), limits=limits, engine=engine) is True
    assert served == [True]


def test_poll_once_hands_the_workers_id_to_a_query_and_to_a_deploy(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    from lakana_worker.deploy import DeploySettings

    deploy_id = _deploy_world(session, principal, tmp_path)
    authz = AllowAllAuthorization()
    with mock.patch("lakana_worker.poller.run_deploy") as run_deploy_mock:
        poll_once(session, authz, worker_id="w1")
    run_deploy_mock.assert_called_once_with(
        session, deploy_id, authz, None, DeploySettings(), worker_id="w1"
    )
    session.execute(delete(DeployRun))
    session.commit()
    query = QueryService(session, authz).submit_query(principal, "dev", "SELECT 1")
    with mock.patch("lakana_worker.poller.run_one") as run_one_mock:
        poll_once(session, authz, worker_id="w1")
    run_one_mock.assert_called_once_with(
        session,
        query.id,
        authz,
        None,
        limits=None,
        worker_id="w1",
        running_cap=None,
        on_cap_timeout=None,
    )


def test_nothing_is_claimed_while_the_worker_may_not_claim(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate is the first thing `poll_once` does: no statement reaches the database."""
    _deploy_world(session, principal, tmp_path)
    query = QueryService(session, AllowAllAuthorization()).submit_query(
        principal, "dev", "SELECT 1"
    )
    statements: list[str] = []
    event.listen(
        session.get_bind(), "before_cursor_execute", lambda *args: statements.append(args[2])
    )
    monkeypatch.setattr(poller, "claim_task", lambda *_a: pytest.fail("a task was claimed"))
    engine = RunEngine(
        session_factory=lambda: Session(session.get_bind()),
        authz_factory=lambda _s: AllowAllAuthorization(),
        settings=TaskSettings(worker_id="w1"),
    )
    with (
        mock.patch("lakana_worker.poller.run_one") as run_one_mock,
        mock.patch("lakana_worker.poller.run_deploy") as run_deploy_mock,
    ):
        claimed = poll_once(
            session, AllowAllAuthorization(), engine=engine, worker_id="w1", may_claim=lambda: False
        )
    assert claimed is False
    assert statements == []
    run_one_mock.assert_not_called()
    run_deploy_mock.assert_not_called()
    session.expire_all()
    stored = session.get(QueryRun, query.id)
    assert stored is not None
    assert stored.status is QueryRunStatus.QUEUED


def test_a_worker_that_is_stopping_for_memory_claims_nothing(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#154: between the memory stop and the exit the loop idles; nothing new is claimed.

    A child started then would outlive the worker, and its run would be recorded with the
    memory message or left running. No statement reaches the database.

    Parameters
    ----------
    session
        The database session.
    principal
        Submits the query.
    tmp_path
        Holds the deployed project.
    monkeypatch
        Sets `ACTIVITY.stopping` and fails a task claim.
    """
    from lakana_worker.activity import ACTIVITY

    _deploy_world(session, principal, tmp_path)
    query = QueryService(session, AllowAllAuthorization()).submit_query(
        principal, "dev", "SELECT 1"
    )
    statements: list[str] = []
    event.listen(
        session.get_bind(), "before_cursor_execute", lambda *args: statements.append(args[2])
    )
    monkeypatch.setattr(poller, "claim_task", lambda *_a: pytest.fail("a task was claimed"))
    monkeypatch.setattr(ACTIVITY, "stopping", True)
    engine = RunEngine(
        session_factory=lambda: Session(session.get_bind()),
        authz_factory=lambda _s: AllowAllAuthorization(),
        settings=TaskSettings(worker_id="w1"),
    )
    with (
        mock.patch("lakana_worker.poller.run_one") as run_one_mock,
        mock.patch("lakana_worker.poller.run_deploy") as run_deploy_mock,
    ):
        assert poll_once(session, AllowAllAuthorization(), engine=engine, worker_id="w1") is False
    assert statements == []
    run_one_mock.assert_not_called()
    run_deploy_mock.assert_not_called()
    session.expire_all()
    stored = session.get(QueryRun, query.id)
    assert stored is not None
    assert stored.status is QueryRunStatus.QUEUED


def test_a_worker_that_may_claim_claims(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _deploy_world(session, principal, tmp_path)
    with mock.patch("lakana_worker.poller.run_deploy"):
        assert poll_once(session, AllowAllAuthorization(), may_claim=lambda: True) is True


def test_poll_forever_hands_its_id_and_gate_to_every_iteration() -> None:

    def gate() -> bool:
        """Allow every claim.

        Returns
        -------
        bool
            Always `True`.
        """
        return True

    drain = threading.Event()

    def stop(*args: object, **kwargs: object) -> bool:
        drain.set()
        return True

    with (
        mock.patch("lakana_worker.poller.poll_iteration", side_effect=stop) as iteration,
        mock.patch("lakana_worker.poller.end_own_orphans"),
    ):
        poll_forever(
            lambda: pytest.fail("no session"),
            lambda _s: AllowAllAuthorization(),
            drain=drain,
            worker_id="w1",
            may_claim=gate,
        )
    assert iteration.call_args.kwargs["worker_id"] == "w1"
    assert iteration.call_args.kwargs["may_claim"] is gate


def test_the_queries_of_a_python_task_are_served_under_the_same_id_and_gate(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = RunEngine(
        session_factory=lambda: Session(session.get_bind()),
        authz_factory=lambda _s: AllowAllAuthorization(),
        settings=TaskSettings(worker_id="w1"),
    )
    claimed = ClaimedTask(uuid.uuid4(), uuid.uuid4(), "gold.scores", 1, 60, 1)
    claims = [claimed]
    captured: list[Callable[[], bool]] = []
    monkeypatch.setattr(poller, "claim_task", lambda *_a: claims.pop() if claims else None)
    monkeypatch.setattr(
        poller, "run_claimed", lambda _e, _c, serve_queries: captured.append(serve_queries)
    )

    def gate() -> bool:
        """Allow every claim.

        Returns
        -------
        bool
            Always `True`.
        """
        return True

    assert poll_once(
        session, AllowAllAuthorization(), engine=engine, worker_id="w1", may_claim=gate
    )
    seen: list[dict[str, object]] = []

    def iteration(*args: object, **kwargs: object) -> bool:
        seen.append(kwargs)
        return False

    monkeypatch.setattr(poller, "poll_iteration", iteration)
    captured[0]()
    assert seen[0]["worker_id"] == "w1"
    assert seen[0]["may_claim"] is gate
    assert seen[0]["only_queries"] is True


def test_poll_forever_runs_maintenance_before_each_poll_and_survives_its_failure() -> None:
    """The sweep hook runs every iteration; its failure is logged, not fatal."""
    calls: list[str] = []

    def maintenance() -> None:
        calls.append("maintenance")
        raise OSError("disk gone")

    def fake_poll_iteration(*args: object, **kwargs: object) -> bool:
        calls.append("poll")
        return False

    engine = create_engine(DatabaseConfig(url="sqlite://"))
    with (
        mock.patch("lakana_worker.poller.poll_iteration", side_effect=fake_poll_iteration),
        mock.patch("lakana_worker.poller.time.sleep", side_effect=[None, _StopLoop]),
        pytest.raises(_StopLoop),
    ):
        poll_forever(
            lambda: Session(engine),
            lambda _s: AllowAllAuthorization(),
            maintenance=maintenance,
        )
    assert calls == ["maintenance", "poll", "maintenance", "poll"]


def _forever(
    monkeypatch: pytest.MonkeyPatch,
    *,
    polls: int,
    times: list[float],
    worker_id: str | None = None,
    may_claim: Callable[[], bool] | None = None,
    claims: bool = False,
    ender: mock.MagicMock | None = None,
) -> mock.MagicMock:
    """Run `poll_forever` for a number of polls with a scripted monotonic clock.

    Parameters
    ----------
    monkeypatch
        Patches the poller's clock and its orphan ender.
    polls
        Polls to run before the drain is set.
    times
        The values the loop's clock returns, one per read.
    worker_id
        Passed to `poll_forever`.
    may_claim
        Passed to `poll_forever`.
    claims
        Whether every poll claims and runs an item (`True`) or finds the queue empty.
    ender
        The stand-in for `end_own_orphans`; a new mock when `None`.

    Returns
    -------
    mock.MagicMock
        The stand-in for `end_own_orphans`.
    """
    drain = threading.Event()
    count = [0]
    clock = iter(times)

    def poll(*args: object, **kw: object) -> bool:
        count[0] += 1
        if count[0] >= polls:
            drain.set()
        return claims

    ender = ender or mock.MagicMock()
    monkeypatch.setattr(poller, "end_own_orphans", ender)
    monkeypatch.setattr(poller, "_monotonic", lambda: next(clock))
    monkeypatch.setattr(poller, "poll_iteration", poll)
    poll_forever(
        lambda: Session(),
        lambda _s: AllowAllAuthorization(),
        drain=drain,
        worker_id=worker_id,
        may_claim=may_claim,
        poll_interval_seconds=0,
    )
    return ender


def test_an_idle_worker_ends_its_orphans_at_most_every_five_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ender = _forever(monkeypatch, polls=6, times=[0.0, 1.0, 5.0, 5.5, 9.9, 10.0], worker_id="w1")
    assert ender.call_count == 3  # at 0.0, 5.0 and 10.0
    assert all(call.args[1] == "w1" for call in ender.call_args_list)


def test_the_worker_checks_once_right_after_each_item_it_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N4: the 5 s throttle does not delay the check after a query, a deploy or a task."""
    ender = _forever(monkeypatch, polls=4, times=[0.0, 0.5, 1.0, 1.5], worker_id="w1", claims=True)
    assert ender.call_count == 4


def test_a_failing_orphan_check_does_not_repeat_faster_than_the_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = mock.MagicMock(side_effect=RuntimeError("the database is away"))
    _forever(
        monkeypatch,
        polls=6,
        times=[0.0, 1.0, 2.0, 3.0, 4.0, 6.0],
        worker_id="w1",
        ender=failing,
    )
    assert failing.call_count == 2  # at 0.0 and 6.0, not on every pass


def test_no_orphan_check_without_an_id_or_for_a_worker_that_may_not_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _forever(monkeypatch, polls=2, times=[0.0, 9.0, 18.0]).call_count == 0
    ender = _forever(
        monkeypatch, polls=2, times=[0.0, 9.0, 18.0], worker_id="w1", may_claim=lambda: False
    )
    assert ender.call_count == 0


def test_no_orphan_check_while_a_query_is_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    from lakana_worker.activity import ACTIVITY

    with ACTIVITY.query(uuid.uuid4()):
        ender = _forever(monkeypatch, polls=2, times=[0.0, 9.0, 18.0], worker_id="w1")
    assert ender.call_count == 0


def test_a_failing_orphan_check_does_not_stop_the_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(poller, "end_own_orphans", mock.MagicMock(side_effect=RuntimeError("x")))
    drain = threading.Event()
    monkeypatch.setattr(poller, "poll_iteration", lambda *a, **k: drain.set() or True)
    poll_forever(lambda: Session(), lambda _s: AllowAllAuthorization(), drain=drain, worker_id="w")
    assert "Ending this worker's orphaned runs failed" in caplog.text


def test_the_queries_served_beside_a_python_task_never_end_orphans(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphan check lives in `poll_forever` only: a query in flight is never an orphan."""
    engine = RunEngine(
        session_factory=lambda: Session(session.get_bind()),
        authz_factory=lambda _s: AllowAllAuthorization(),
        settings=TaskSettings(worker_id="w1"),
    )
    claimed = ClaimedTask(uuid.uuid4(), uuid.uuid4(), "gold.scores", 1, 60, 1)
    monkeypatch.setattr(poller, "claim_task", lambda *_a: claimed)
    ender = mock.MagicMock()
    monkeypatch.setattr(poller, "end_own_orphans", ender)
    served: list[bool] = []
    monkeypatch.setattr(
        poller, "run_claimed", lambda _e, _c, serve_queries: served.append(serve_queries())
    )
    assert poll_once(session, AllowAllAuthorization(), engine=engine, worker_id="w1") is True
    assert served == [False]
    ender.assert_not_called()


def _deploy_row(session: Session, worker_id: str | None, status: DeployStatus) -> uuid.UUID:
    """Insert a deploy run with an environment and a repository of its own.

    Parameters
    ----------
    session
        Database session.
    worker_id
        The worker id the run carries.
    status
        The run's status.

    Returns
    -------
    uuid.UUID
        The run's id.
    """
    environment = session.query(Environment).first()
    if environment is None:
        environment = Environment(
            id=uuid.uuid4(),
            name="dev",
            storage_root="file:///tmp/x",
            storage_options={},
            is_protected=False,
            properties={},
        )
        session.add(environment)
    repository = session.query(Repository).first()
    if repository is None:
        repository = Repository(
            id=uuid.uuid4(), name="r", kind=RepositoryKind.HTTPS, url="https://h/a.git"
        )
        session.add(repository)
    session.flush()
    run = DeployRun(
        id=uuid.uuid4(),
        environment_id=environment.id,
        repository_id=repository.id,
        kind=DeployKind.DEPLOY,
        status=status,
        worker_id=worker_id,
    )
    session.add(run)
    session.commit()
    return run.id


def _deploy_status(session: Session, run_id: uuid.UUID) -> DeployStatus:
    """Read a deploy run's stored status.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.

    Returns
    -------
    DeployStatus
        The status.
    """
    session.expire_all()
    run = session.get(DeployRun, run_id)
    assert run is not None
    return run.status


def test_an_idle_worker_ends_its_own_leftover_deploy_and_no_other(
    file_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`poll_forever`'s idle ender fails a deploy left `running` under its id."""
    mine = _deploy_row(file_session, "w1", DeployStatus.RUNNING)
    foreign = _deploy_row(file_session, "w2", DeployStatus.RUNNING)
    drain = threading.Event()
    real = poller.end_own_orphans

    def end_then_stop(factory: Callable[[], Session], worker_id: str) -> int:
        ended = real(factory, worker_id)
        drain.set()
        return ended

    monkeypatch.setattr(poller, "end_own_orphans", end_then_stop)
    bind = file_session.get_bind()
    poll_forever(
        lambda: Session(bind),
        lambda _s: AllowAllAuthorization(),
        drain=drain,
        worker_id="w1",
        poll_interval_seconds=0,
    )
    assert _deploy_status(file_session, mine) is DeployStatus.FAILED
    assert _deploy_status(file_session, foreign) is DeployStatus.RUNNING


def test_no_deploy_is_ended_while_the_worker_may_not_claim(file_session: Session) -> None:
    """The query ender's gate holds for deploys too: a stale heartbeat leaves them to the sweep."""
    mine = _deploy_row(file_session, "w1", DeployStatus.RUNNING)
    drain = threading.Event()
    bind = file_session.get_bind()

    def closed() -> bool:
        drain.set()
        return False

    poll_forever(
        lambda: Session(bind),
        lambda _s: AllowAllAuthorization(),
        drain=drain,
        worker_id="w1",
        may_claim=closed,
        poll_interval_seconds=0,
    )
    assert _deploy_status(file_session, mine) is DeployStatus.RUNNING


def test_a_deploy_in_flight_is_never_ended_and_one_it_left_behind_is(
    file_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deploy runs inline in the poll loop, so the idle ender never sees it in flight.

    The deploy is claimed `running` under this worker, its final write "fails" (the stand-in
    returns without finishing it), and the ender runs after it, not during it.
    """
    queued = _deploy_row(file_session, None, DeployStatus.QUEUED)
    bind = file_session.get_bind()
    drain = threading.Event()
    in_flight: list[bool] = []
    during: list[DeployStatus] = []

    def deploy_that_cannot_finish(
        session: Session, run_id: uuid.UUID, *args: object, worker_id: str | None, **kwargs: object
    ) -> None:
        in_flight.append(True)
        run = session.get(DeployRun, run_id)
        assert run is not None
        run.status, run.worker_id = DeployStatus.RUNNING, worker_id
        session.commit()
        during.append(_deploy_status(session, run_id))
        in_flight.pop()

    real = poller.end_own_orphans
    seen_in_flight: list[bool] = []

    def end_then_stop(factory: Callable[[], Session], worker_id: str) -> int:
        seen_in_flight.append(bool(in_flight))
        ended = real(factory, worker_id)
        if ended:
            drain.set()
        return ended

    empty_polls: list[int] = []

    def bounded_sleep(_seconds: float) -> None:
        """Drain after 200 empty polls, so that a worker taking no deploy fails, not hangs."""
        empty_polls.append(1)
        if len(empty_polls) >= 200:
            drain.set()

    monkeypatch.setattr(poller, "run_deploy", deploy_that_cannot_finish)
    monkeypatch.setattr(poller, "end_own_orphans", end_then_stop)
    monkeypatch.setattr(poller.time, "sleep", bounded_sleep)
    poll_forever(
        lambda: Session(bind),
        lambda _s: AllowAllAuthorization(),
        drain=drain,
        worker_id="w1",
        poll_interval_seconds=0,
    )
    assert during == [DeployStatus.RUNNING], "the worker never took the queued deploy"
    assert seen_in_flight and not any(seen_in_flight)
    assert _deploy_status(file_session, queued) is DeployStatus.FAILED


def _queue_task_run(
    session: Session, environment_id: uuid.UUID, principal_id: uuid.UUID
) -> uuid.UUID:
    """Insert a running task run with the rows it needs.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment of the run.
    principal_id
        The run-as principal.

    Returns
    -------
    uuid.UUID
        The task run's id.
    """
    repo = Repository(id=uuid.uuid4(), name="tr", kind=RepositoryKind.HTTPS, url="https://h/t.git")
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=environment_id,
        commit_sha="a" * 40,
        manifest_json={},
        code_uri="file:///x",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    pipeline_run = Run(
        id=uuid.uuid4(),
        environment_id=environment_id,
        repository_id=repo.id,
        job_version_id=version.id,
        run_as_principal_id=principal_id,
        kind=RunKind.FULL,
        status=RunStatus.RUNNING,
        selection_json={},
    )
    session.add(pipeline_run)
    session.flush()
    task = TaskRun(
        id=uuid.uuid4(),
        run_id=pipeline_run.id,
        asset="a.b",
        status=TaskStatus.RUNNING,
        max_attempts=1,
        timeout_seconds=60,
        retry_delay_seconds=0,
        upstream_json=[],
    )
    session.add(task)
    session.flush()
    return task.id


def _queue_one_of_each(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> dict[str, uuid.UUID]:
    """Queue an ad-hoc query, maintenance, a Python task's input query and a deploy, in that age.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    tmp_path
        Storage root of `dev`.

    Returns
    -------
    dict[str, uuid.UUID]
        The names `adhoc`, `maintain`, `input` and `deploy` to the ids of the queued runs,
        oldest first, one second apart.
    """
    deploy_id = _deploy_world(session, principal, tmp_path)
    env = session.scalars(select(Environment)).one()
    adhoc = QueryService(session, AllowAllAuthorization()).submit_query(
        principal, "dev", "SELECT 1"
    )
    maintain = QueryRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=principal.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.QUEUED,
        write_mode=WriteMode.MAINTAIN,
    )
    task_run_id = _queue_task_run(session, env.id, principal.id)
    input_query = QueryRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=principal.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.QUEUED,
        task_run_id=task_run_id,
        task_token_id=uuid.uuid4(),
    )
    session.add_all([maintain, input_query])
    start = datetime.now(UTC)
    for offset, row in enumerate(
        (session.get(QueryRun, adhoc.id), maintain, input_query, session.get(DeployRun, deploy_id))
    ):
        assert row is not None
        row.created_at = start + timedelta(seconds=offset)
    session.commit()
    return {
        "adhoc": adhoc.id,
        "maintain": maintain.id,
        "input": input_query.id,
        "deploy": deploy_id,
    }


def _run_engine(session: Session) -> RunEngine:
    """Build an engine stand-in on the session's database.

    Parameters
    ----------
    session
        Database session whose database the engine uses.

    Returns
    -------
    RunEngine
        An engine whose id is `w1`.
    """
    return RunEngine(
        session_factory=lambda: Session(session.get_bind()),
        authz_factory=lambda _s: AllowAllAuthorization(),
        settings=TaskSettings(worker_id="w1"),
    )


@pytest.mark.parametrize(
    ("takes", "only_queries", "claimed"),
    [
        ("all", False, ["adhoc", "maintain", "input", "deploy", "task"]),
        ("all", True, ["adhoc", "maintain", "input"]),
        ("interactive", False, ["adhoc"]),
        ("interactive", True, ["adhoc"]),
        ("pipeline", False, ["maintain", "input", "deploy", "task"]),
        ("pipeline", True, ["maintain", "input"]),
    ],
)
def test_each_worker_kind_takes_exactly_its_work(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    takes: str,
    only_queries: bool,
    claimed: list[str],
) -> None:
    ids = _queue_one_of_each(session, principal, tmp_path)
    names = {run_id: name for name, run_id in ids.items()}
    taken: list[str] = []
    tasks = [ClaimedTask(uuid.uuid4(), uuid.uuid4(), "gold.scores", 1, 60, 1)]

    def run_one(db: Session, run_id: uuid.UUID, *args: object, **kwargs: object) -> None:
        taken.append(names[run_id])
        row = db.get(QueryRun, run_id)
        assert row is not None
        row.status = QueryRunStatus.SUCCEEDED
        db.commit()

    def run_deploy(db: Session, run_id: uuid.UUID, *args: object, **kwargs: object) -> None:
        taken.append(names[run_id])
        row = db.get(DeployRun, run_id)
        assert row is not None
        row.status = DeployStatus.SUCCEEDED
        db.commit()

    monkeypatch.setattr(poller, "run_one", run_one)
    monkeypatch.setattr(poller, "run_deploy", run_deploy)
    monkeypatch.setattr(poller, "claim_task", lambda *a, **k: tasks.pop() if tasks else None)
    monkeypatch.setattr(poller, "run_claimed", lambda *a, **k: taken.append("task"))
    engine = _run_engine(session)
    for _ in range(10):
        if not poll_once(
            session,
            AllowAllAuthorization(),
            engine=engine,
            only_queries=only_queries,
            pool=WorkerPool(takes=takes),
        ):
            break
    else:
        pytest.fail("the worker never ran out of work")
    assert taken == claimed
    session.expire_all()
    for name in ("adhoc", "maintain", "input"):
        row = session.get(QueryRun, ids[name])
        assert row is not None
        assert (row.status is QueryRunStatus.QUEUED) == (name not in claimed), name
    deploy_row = session.get(DeployRun, ids["deploy"])
    assert deploy_row is not None
    assert (deploy_row.status is DeployStatus.QUEUED) == ("deploy" not in claimed)


def test_without_a_pool_a_worker_takes_everything(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pool=None`, the default of every call that does not say, is the worker kind `all`."""
    ids = _queue_one_of_each(session, principal, tmp_path)
    names = {run_id: name for name, run_id in ids.items()}
    taken: list[str] = []

    def run_one(db: Session, run_id: uuid.UUID, *args: object, **kwargs: object) -> None:
        taken.append(names[run_id])
        row = db.get(QueryRun, run_id)
        assert row is not None
        row.status = QueryRunStatus.SUCCEEDED
        db.commit()

    def run_deploy(db: Session, run_id: uuid.UUID, *args: object, **kwargs: object) -> None:
        taken.append(names[run_id])
        row = db.get(DeployRun, run_id)
        assert row is not None
        row.status = DeployStatus.SUCCEEDED
        db.commit()

    monkeypatch.setattr(poller, "run_one", run_one)
    monkeypatch.setattr(poller, "run_deploy", run_deploy)
    while poll_once(session, AllowAllAuthorization()):
        assert len(taken) <= 4
    assert taken == ["adhoc", "maintain", "input", "deploy"]


def test_an_interactive_worker_never_asks_for_a_task(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(poller, "claim_task", lambda *a, **k: pytest.fail("a task was asked for"))
    assert (
        poll_once(
            session,
            AllowAllAuthorization(),
            engine=_run_engine(session),
            pool=WorkerPool(takes="interactive"),
        )
        is False
    )


def test_a_pipeline_worker_that_supervises_a_task_passes_an_ad_hoc_query_by(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The serve callback of a pipeline worker skips the older ad-hoc query and reaches the next."""
    ids = _queue_one_of_each(session, principal, tmp_path)
    session.execute(delete(DeployRun))
    session.commit()
    names = {run_id: name for name, run_id in ids.items()}
    captured: list[Callable[[], bool]] = []
    served: list[str] = []

    def run_one(db: Session, run_id: uuid.UUID, *args: object, **kwargs: object) -> None:
        served.append(names[run_id])
        row = db.get(QueryRun, run_id)
        assert row is not None
        row.status = QueryRunStatus.SUCCEEDED
        db.commit()

    claims = [ClaimedTask(uuid.uuid4(), uuid.uuid4(), "gold.scores", 1, 60, 1)]
    monkeypatch.setattr(poller, "claim_task", lambda *a, **k: claims.pop() if claims else None)
    monkeypatch.setattr(
        poller, "run_claimed", lambda e, c, serve_queries: captured.append(serve_queries)
    )
    monkeypatch.setattr(poller, "run_one", run_one)
    engine = _run_engine(session)
    # The task is claimed while only the ad-hoc query waits (a pipeline worker never sees it).
    for name in ("maintain", "input"):
        row = session.get(QueryRun, ids[name])
        assert row is not None
        row.status = QueryRunStatus.CANCELLED
    session.commit()
    assert poll_once(
        session, AllowAllAuthorization(), engine=engine, pool=WorkerPool(takes="pipeline")
    )
    (serve,) = captured
    for name in ("maintain", "input"):
        row = session.get(QueryRun, ids[name])
        assert row is not None
        row.status = QueryRunStatus.QUEUED
    session.commit()
    assert serve() is True, "the maintenance run behind the ad-hoc query is served"
    assert serve() is True, "and the input query of the task"
    assert serve() is False
    assert served == ["maintain", "input"]
    session.expire_all()
    adhoc = session.get(QueryRun, ids["adhoc"])
    assert adhoc is not None and adhoc.status is QueryRunStatus.QUEUED


def test_the_serve_callback_carries_the_pool(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = [ClaimedTask(uuid.uuid4(), uuid.uuid4(), "gold.scores", 1, 60, 1)]
    captured: list[Callable[[], bool]] = []
    monkeypatch.setattr(poller, "claim_task", lambda *a, **k: claims.pop() if claims else None)
    monkeypatch.setattr(
        poller, "run_claimed", lambda e, c, serve_queries: captured.append(serve_queries)
    )
    pool = WorkerPool(takes="pipeline")
    assert poll_once(session, AllowAllAuthorization(), engine=_run_engine(session), pool=pool)
    seen: list[dict[str, object]] = []

    def iteration(*args: object, **kwargs: object) -> bool:
        seen.append(kwargs)
        return False

    monkeypatch.setattr(poller, "poll_iteration", iteration)
    captured[0]()
    assert seen[0]["pool"] is pool
    assert seen[0]["only_queries"] is True


def test_poll_forever_and_poll_iteration_pass_the_pool_down(session: Session) -> None:
    pool = WorkerPool(takes="interactive")
    with mock.patch("lakana_worker.poller.poll_once", return_value=False) as poll_once_mock:
        poll_iteration(lambda: session, lambda _s: AllowAllAuthorization(), pool=pool)
    assert poll_once_mock.call_args.kwargs["pool"] is pool
    drain = threading.Event()

    def stop(*args: object, **kwargs: object) -> bool:
        drain.set()
        return True

    with (
        mock.patch("lakana_worker.poller.poll_iteration", side_effect=stop) as iteration,
        mock.patch("lakana_worker.poller.end_own_orphans"),
    ):
        poll_forever(
            lambda: pytest.fail("no session"),
            lambda _s: AllowAllAuthorization(),
            drain=drain,
            pool=pool,
        )
    assert iteration.call_args.kwargs["pool"] is pool


def test_next_query_passes_a_capped_principal_by(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _deploy_world(session, principal, tmp_path)
    env = session.scalars(select(Environment)).one()
    other = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="other")
    session.add(other)
    session.flush()
    start = datetime.now(UTC)
    rows = {}
    for offset, (name, fields) in enumerate(
        (
            ("capped adhoc", {"principal_id": principal.id}),
            ("capped maintain", {"principal_id": principal.id, "write_mode": WriteMode.MAINTAIN}),
            ("anonymous", {"principal_id": None}),
            ("other", {"principal_id": other.id}),
        )
    ):
        run = QueryRun(
            id=uuid.uuid4(),
            environment_id=env.id,
            sql_text="SELECT 1",
            status=QueryRunStatus.QUEUED,
            created_at=start + timedelta(seconds=offset),
            **fields,
        )
        session.add(run)
        rows[name] = run.id
    session.commit()
    capped = [principal.id]

    def first(takes: str) -> uuid.UUID | None:
        run = _next_query(session, takes, capped)
        return run.id if run is not None else None

    assert first("interactive") == rows["anonymous"]
    assert first("all") == rows["capped maintain"], "its own maintenance is pipeline work"
    assert first("pipeline") == rows["capped maintain"], "the list is ignored"
    session.execute(delete(QueryRun).where(QueryRun.id == rows["capped maintain"]))
    assert first("pipeline") is None
    assert first("all") == rows["anonymous"]
    run = _next_query(session, "all")
    assert run is not None and run.id == rows["capped adhoc"], "no list, nothing passed by"


def test_a_worker_that_takes_one_kind_still_ends_its_own_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idle worker's ender is not work a pool takes: an interactive worker ends its runs."""
    ender = mock.MagicMock()
    monkeypatch.setattr(poller, "end_own_orphans", ender)
    drain = threading.Event()

    def stop(*args: object, **kwargs: object) -> bool:
        drain.set()
        return False

    monkeypatch.setattr(poller, "poll_iteration", stop)
    monkeypatch.setattr(poller.time, "sleep", lambda _s: None)
    poll_forever(
        lambda: pytest.fail("no session"),
        lambda _s: AllowAllAuthorization(),
        drain=drain,
        worker_id="w1",
        pool=WorkerPool(takes="interactive"),
    )
    ender.assert_called_once()


@pytest.fixture
def dev(session: Session) -> uuid.UUID:
    """Insert the environment `dev` and return its id.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        The environment's id.
    """
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/lakana-test",
        storage_options={},
        is_protected=False,
        properties={},
    )
    session.add(env)
    session.commit()
    return env.id


def _person(session: Session, name: str, kind: PrincipalType = PrincipalType.USER) -> uuid.UUID:
    """Insert a principal.

    Parameters
    ----------
    session
        Database session.
    name
        The principal's name.
    kind
        User or service account.

    Returns
    -------
    uuid.UUID
        The principal's id.
    """
    person = Principal(id=uuid.uuid4(), type=kind, name=name)
    session.add(person)
    session.commit()
    return person.id


_CLOCK = datetime(2026, 1, 1, tzinfo=UTC)
"""Creation times of queued runs count up from here, so that their age is their order."""


def _runs(
    session: Session,
    environment_id: uuid.UUID,
    principal_id: uuid.UUID | None,
    n: int,
    *,
    status: QueryRunStatus = QueryRunStatus.RUNNING,
    worker_id: str | None = "busy",
    age: int = 0,
    write_mode: WriteMode | None = None,
    task_run_id: uuid.UUID | None = None,
    task_token_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Insert `n` ad-hoc query runs of a principal.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The runs' environment.
    principal_id
        The submitter, or `None` for a deleted one.
    n
        How many.
    status
        Their status; `RUNNING` under `worker_id` unless a row says otherwise.
    worker_id
        The worker id of a `RUNNING` run.
    age
        Seconds after `_CLOCK` that the first run was created; later runs follow one second
        apart.
    write_mode
        The runs' write mode.
    task_run_id
        The task run the runs belong to.
    task_token_id
        The task token the runs were submitted with.

    Returns
    -------
    list[uuid.UUID]
        The ids, oldest first.
    """
    ids = []
    for offset in range(n):
        run = QueryRun(
            id=uuid.uuid4(),
            environment_id=environment_id,
            principal_id=principal_id,
            sql_text="SELECT 1",
            status=status,
            worker_id=worker_id if status is QueryRunStatus.RUNNING else None,
            created_at=_CLOCK + timedelta(seconds=age + offset),
            write_mode=write_mode,
            task_run_id=task_run_id,
            task_token_id=task_token_id,
        )
        session.add(run)
        ids.append(run.id)
    session.commit()
    return ids


def _queued(
    session: Session,
    environment_id: uuid.UUID,
    principal_id: uuid.UUID | None,
    *,
    age: int = 100,
    write_mode: WriteMode | None = None,
    task_run_id: uuid.UUID | None = None,
    task_token_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one queued ad-hoc query run.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The run's environment.
    principal_id
        The submitter.
    age
        Seconds after `_CLOCK`; the smaller, the older.
    write_mode
        The run's write mode.
    task_run_id
        The task run the run belongs to.
    task_token_id
        The task token the run was submitted with.

    Returns
    -------
    uuid.UUID
        The run's id.
    """
    (run_id,) = _runs(
        session,
        environment_id,
        principal_id,
        1,
        status=QueryRunStatus.QUEUED,
        age=age,
        write_mode=write_mode,
        task_run_id=task_run_id,
        task_token_id=task_token_id,
    )
    return run_id


def _workers(session: Session, *takes: str, age: int = 0) -> None:
    """Register one worker per value of `takes`; the first one's heartbeat is `age` seconds old.

    Parameters
    ----------
    session
        Database session.
    *takes
        What each worker takes.
    age
        How old the first worker's heartbeat is, in seconds; 0 leaves it fresh.
    """
    for number, value in enumerate(takes):
        workers.register(session, f"w{number}", host="h", version="1", takes=value)
    session.commit()
    if age:
        session.execute(
            update(Worker)
            .where(Worker.id == "w0")
            .values(heartbeat_at=db_now(session) - timedelta(seconds=age))
        )
        session.commit()


def _end(session: Session, run_id: uuid.UUID) -> None:
    """Mark a running run `SUCCEEDED`.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.
    """
    session.execute(
        update(QueryRun).where(QueryRun.id == run_id).values(status=QueryRunStatus.SUCCEEDED)
    )
    session.commit()


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Replace `poller.run_one` with a recorder that starts the run and leaves it running.

    Parameters
    ----------
    monkeypatch
        The pytest fixture.

    Returns
    -------
    list[dict[str, object]]
        One entry per call: the run's id (`id`) and the keyword arguments it got.
    """
    calls: list[dict[str, object]] = []

    def run_one(db: Session, run_id: uuid.UUID, *args: object, **kwargs: object) -> None:
        calls.append({"id": run_id, **kwargs})
        db.execute(
            update(QueryRun)
            .where(QueryRun.id == run_id)
            .values(status=QueryRunStatus.RUNNING, worker_id="me")
        )
        db.commit()

    monkeypatch.setattr(poller, "run_one", run_one)
    return calls


def _poll(session: Session, cap: int | str | None = "auto", takes: str = "interactive") -> bool:
    """Poll once as a worker with the given pool.

    Parameters
    ----------
    session
        Database session.
    cap
        `running_per_principal` of the pool.
    takes
        `takes` of the pool.

    Returns
    -------
    bool
        What `poll_once` returned.
    """
    pool = WorkerPool(takes=takes, running_per_principal=cap, lease_seconds=60)  # ty: ignore[invalid-argument-type]
    return poll_once(session, AllowAllAuthorization(), worker_id="me", pool=pool)


def test_a_capped_principals_query_is_overtaken_and_starts_when_one_of_theirs_ends(
    session: Session, dev: uuid.UUID, started: list[dict[str, object]]
) -> None:
    """Cap 2: A's third query waits behind B's younger one and starts when one of A's ends."""
    a, b = _person(session, "a"), _person(session, "b")
    running = _runs(session, dev, a, 2)
    first = _queued(session, dev, a, age=10)
    second = _queued(session, dev, a, age=11)
    theirs = _queued(session, dev, b, age=20)
    assert _poll(session, 2) is True
    assert [call["id"] for call in started] == [theirs]
    assert _poll(session, 2) is False
    assert [call["id"] for call in started] == [theirs]
    stored = session.get(QueryRun, first)
    assert stored is not None and stored.status is QueryRunStatus.QUEUED
    _end(session, running[0])
    assert _poll(session, 2) is True
    assert [call["id"] for call in started] == [theirs, first], "the older of A's own starts first"
    assert _poll(session, 2) is False
    session.expire_all()
    waiting = session.get(QueryRun, second)
    assert waiting is not None and waiting.status is QueryRunStatus.QUEUED


@pytest.mark.parametrize(
    ("alive", "cap"),
    [
        (("all",), 1),
        (("all", "all"), 2),
        (("all", "interactive", "interactive"), 2),
        (("interactive",) * 4, 3),
        (("all", "interactive", "pipeline", "pipeline"), 2),
        (("pipeline",), 1),
        ((), 1),
    ],
)
def test_auto_with_one_two_three_and_four_workers(
    session: Session,
    dev: uuid.UUID,
    started: list[dict[str, object]],
    alive: tuple[str, ...],
    cap: int,
) -> None:
    """With `auto`, a principal below the cap starts and one at the cap is passed by."""
    _workers(session, *alive)
    below, at = _person(session, "below"), _person(session, "at")
    _runs(session, dev, below, cap - 1)
    _runs(session, dev, at, cap)
    waiting = _queued(session, dev, at, age=10)
    startable = _queued(session, dev, below, age=20)
    assert _poll(session) is True
    assert [call["id"] for call in started] == [startable]
    assert started[0]["running_cap"] == cap
    assert _poll(session) is False, "both principals are at the cap now"
    session.expire_all()
    stored = session.get(QueryRun, waiting)
    assert stored is not None and stored.status is QueryRunStatus.QUEUED


def test_a_worker_that_dies_at_three_leaves_two_and_at_four_leaves_two(
    session: Session, dev: uuid.UUID, started: list[dict[str, object]]
) -> None:
    """The cap follows the alive workers; a running query is never stopped by it."""
    _workers(session, "all", "all", "all", age=61)
    two, one = _person(session, "two"), _person(session, "one")
    _runs(session, dev, two, 2)
    _runs(session, dev, one, 1)
    waits = _queued(session, dev, two, age=10)
    goes = _queued(session, dev, one, age=20)
    assert _poll(session) is True
    assert [call["id"] for call in started] == [goes], "still two with three workers, two alive"
    assert _poll(session) is False
    session.execute(delete(Worker))
    session.execute(update(QueryRun).where(QueryRun.id.in_([waits, goes])).values(status="FAILED"))
    session.commit()
    _workers(session, "all", "all", "all", "all", age=61)
    three = _person(session, "three")
    running = _runs(session, dev, three, 3)
    blocked = _queued(session, dev, three, age=30)
    assert _poll(session) is False
    session.expire_all()
    stopped = [session.get(QueryRun, run_id) for run_id in running]
    assert None not in stopped
    assert {run.status for run in stopped if run} == {QueryRunStatus.RUNNING}, "nothing is stopped"
    _end(session, running[0])
    assert _poll(session) is False, "two of three are still running"
    _end(session, running[1])
    assert _poll(session) is True
    assert started[-1]["id"] == blocked


def test_a_fixed_1_with_two_workers_keeps_one_free(
    session: Session, dev: uuid.UUID, started: list[dict[str, object]]
) -> None:
    """A fixed 1 holds a principal to one query although two workers could run two."""
    _workers(session, "all", "all")
    busy, free = _person(session, "busy"), _person(session, "free")
    _runs(session, dev, busy, 1)
    _queued(session, dev, busy, age=10)
    other = _queued(session, dev, free, age=20)
    assert _poll(session, 1) is True
    assert [call["id"] for call in started] == [other]


def test_a_number_at_or_above_the_workers_is_no_limit(
    session: Session, dev: uuid.UUID, started: list[dict[str, object]]
) -> None:
    """Cap 5 with two workers: a principal with two running still starts a third."""
    _workers(session, "all", "all")
    busy = _person(session, "busy")
    _runs(session, dev, busy, 2)
    queued = _queued(session, dev, busy)
    assert _poll(session, 5) is True
    assert [call["id"] for call in started] == [queued]


def test_no_cap_without_a_principal(
    session: Session, dev: uuid.UUID, started: list[dict[str, object]]
) -> None:
    """A queued run whose principal was deleted is handed on beside a list of capped ones."""
    busy = _person(session, "busy")
    _runs(session, dev, busy, 1)
    _queued(session, dev, busy, age=10)
    orphan = _queued(session, dev, None, age=20)
    assert _poll(session, 1) is True
    assert [call["id"] for call in started] == [orphan]


def test_a_running_run_without_a_principal_is_never_counted(
    session: Session, dev: uuid.UUID, started: list[dict[str, object]]
) -> None:
    """Running runs of deleted principals are no principal's: nobody is capped by them."""
    _runs(session, dev, None, 3)
    queued = _queued(session, dev, _person(session, "someone"))
    assert _poll(session, 1) is True
    assert [call["id"] for call in started] == [queued]


def test_pipeline_work_under_the_same_principal_is_neither_counted_nor_held_back(
    session: Session, dev: uuid.UUID, started: list[dict[str, object]]
) -> None:
    """Cap 1, `all`: a running maintenance run is not counted; a queued one is not held back."""
    busy = _person(session, "busy")
    _runs(session, dev, busy, 1, write_mode=WriteMode.MAINTAIN)
    adhoc = _queued(session, dev, busy, age=10)
    assert _poll(session, 1, takes="all") is True
    assert [call["id"] for call in started] == [adhoc], "the maintenance run did not count"
    maintain = _queued(session, dev, busy, age=20, write_mode=WriteMode.MAINTAIN)
    waiting = _queued(session, dev, busy, age=5)
    assert _poll(session, 1, takes="all") is True
    assert [call["id"] for call in started] == [adhoc, maintain], "maintenance goes, ad-hoc waits"
    assert _poll(session, 1, takes="all") is False
    stored = session.get(QueryRun, waiting)
    assert stored is not None and stored.status is QueryRunStatus.QUEUED


def test_a_principal_at_the_cap_still_gets_a_python_tasks_input_query_served(
    session: Session, dev: uuid.UUID, started: list[dict[str, object]]
) -> None:
    """The input query of a Python task is pipeline work: never capped."""
    busy = _person(session, "busy")
    _runs(session, dev, busy, 1)
    task_run_id = _queue_task_run(session, dev, busy)
    session.commit()
    token = uuid.uuid4()
    input_query = _queued(session, dev, busy, age=10, task_run_id=task_run_id, task_token_id=token)
    assert _poll(session, 1, takes="pipeline") is True
    assert [call["id"] for call in started] == [input_query]
    assert started[0]["running_cap"] is None


def test_a_service_account_is_capped_like_a_user(
    session: Session, dev: uuid.UUID, started: list[dict[str, object]]
) -> None:
    """The cap is per principal, whatever its type."""
    robot = _person(session, "robot", PrincipalType.SERVICE_ACCOUNT)
    _runs(session, dev, robot, 1)
    waiting = _queued(session, dev, robot, age=10)
    other = _queued(session, dev, _person(session, "human"), age=20)
    assert _poll(session, 1) is True
    assert [call["id"] for call in started] == [other]
    stored = session.get(QueryRun, waiting)
    assert stored is not None and stored.status is QueryRunStatus.QUEUED


def test_a_lost_workers_running_query_counts_until_it_is_ended(
    session: Session, dev: uuid.UUID, started: list[dict[str, object]]
) -> None:
    """A running query whose worker died still counts; the sweep that ends it frees the slot."""
    _workers(session, "all", age=61)
    workers.register(session, "me", host="h", version="1", takes="all")
    session.commit()
    busy = _person(session, "busy")
    _runs(session, dev, busy, 1, worker_id="w0")
    queued = _queued(session, dev, busy)
    assert _poll(session, 1) is False
    end_lost_runs(session, db_now(session), 60)
    session.commit()
    assert _poll(session, 1) is True
    assert [call["id"] for call in started] == [queued]


def test_the_cap_handed_to_run_one_is_the_resolved_number(
    session: Session, dev: uuid.UUID, started: list[dict[str, object]]
) -> None:
    """Under `auto` with three workers, `run_one` gets 2; a fixed number is handed as it is."""
    _workers(session, "all", "all", "all")
    _queued(session, dev, _person(session, "someone"), age=10)
    assert _poll(session) is True
    assert started[-1]["running_cap"] == 2
    _queued(session, dev, _person(session, "someone else"), age=11)
    assert _poll(session, 7) is True
    assert started[-1]["running_cap"] == 7


def test_a_pipeline_worker_hands_no_cap(
    session: Session, dev: uuid.UUID, started: list[dict[str, object]]
) -> None:
    """A worker that takes no interactive work resolves no cap and applies none."""
    busy = _person(session, "busy")
    _runs(session, dev, busy, 3)
    maintain = _queued(session, dev, busy, write_mode=WriteMode.MAINTAIN)
    assert _poll(session, 1, takes="pipeline") is True
    assert [(call["id"], call["running_cap"]) for call in started] == [(maintain, None)]


def test_a_stale_hint_costs_one_empty_claim_and_the_next_poll_does_not_spin(
    session: Session,
    dev: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The principal reached the cap after the read: the claim matches nothing, the run waits.

    Nothing is executed, written or audited; the next poll lists the principal as capped and
    returns `False` without calling `run_one` again.
    """
    busy = _person(session, "busy")
    queued = _queued(session, dev, busy)
    real = poller.pools.running_by_principal
    calls: list[int] = []

    def stale_once(db: Session, at_least: int = 1) -> dict[uuid.UUID, int]:
        calls.append(at_least)
        if len(calls) == 1:
            _runs(db, dev, busy, 1)  # lands after the poller looked, before it claims
            return {}
        return real(db, at_least)

    monkeypatch.setattr(poller.pools, "running_by_principal", stale_once)

    def refuse(*args: object, **kwargs: object) -> object:
        pytest.fail("a capped claim started a query")

    monkeypatch.setattr("lakana_worker.pipeline.execute_query", refuse)
    assert _poll(session, 1) is True
    session.expire_all()
    stored = session.get(QueryRun, queued)
    assert stored is not None
    assert (stored.status, stored.worker_id, stored.started_at) == (
        QueryRunStatus.QUEUED,
        None,
        None,
    )
    assert session.scalars(select(AuditEvent)).all() == []
    assert _poll(session, 1) is False


def test_an_idle_worker_that_takes_interactive_work_reads_the_workers_and_the_capped_principals_and_a_pipeline_worker_neither(  # ruff: ignore[line-too-long]
    session: Session,
) -> None:
    """With an empty queue the cap costs three statements: the clock, the workers, the list."""

    def statements(pool: WorkerPool) -> list[str]:
        seen: list[str] = []

        def record(conn: object, cursor: object, statement: str, *rest: object) -> None:
            seen.append(statement)

        event.listen(session.get_bind(), "before_cursor_execute", record)
        try:
            assert poll_once(session, AllowAllAuthorization(), pool=pool) is False
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", record)
        session.rollback()
        return seen

    pipeline_only = statements(WorkerPool(takes="pipeline"))
    assert statements(WorkerPool(takes="pipeline", running_per_principal="auto")) == pipeline_only
    plain = statements(WorkerPool(takes="all"))
    capped = statements(WorkerPool(takes="all", running_per_principal="auto"))
    assert len(capped) == len(plain) + 3
    assert [s for s in capped if "FROM workers" in s] != []
    assert [s for s in capped if "GROUP BY" in s] != []


def test_a_takes_outside_the_three_selects_nothing(session: Session, dev: uuid.UUID) -> None:
    """`takes_kind` takes nothing for an unknown value; the dequeue agrees (fail closed)."""
    busy = _person(session, "busy")
    _queued(session, dev, busy)
    _queued(session, dev, busy, write_mode=WriteMode.MAINTAIN)
    for unknown in ("batch", "", "ALL"):
        assert _next_query(session, unknown) is None
        assert _next_query(session, unknown, [busy]) is None
    assert _next_query(session, "all") is not None


class _Clock:
    """A clock the test moves."""

    def __init__(self) -> None:
        """Start at 1000 seconds."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Read the clock.

        Returns
        -------
        float
            The current time in seconds.
        """
        return self.now


class _Contended:
    """A stand-in for `run_one` whose cap lock times out for some principals."""

    def __init__(self) -> None:
        """Start with no contended principal and nothing tried."""
        self.principals: set[uuid.UUID] = set()
        self.tried: list[uuid.UUID] = []

    def __call__(self, db: Session, run_id: uuid.UUID, *args: object, **kwargs: object) -> None:
        """Run, or report a lock timeout for a contended principal and leave the run queued.

        Parameters
        ----------
        db
            Database session.
        run_id
            The run.
        *args
            Ignored.
        **kwargs
            The keyword arguments `poll_once` hands to `run_one`; `on_cap_timeout` is used.
        """
        run = db.get(QueryRun, run_id)
        assert run is not None and run.principal_id is not None
        self.tried.append(run_id)
        if run.principal_id in self.principals:
            callback = kwargs["on_cap_timeout"]
            assert callable(callback)
            callback(run.principal_id)
            return
        db.execute(
            update(QueryRun)
            .where(QueryRun.id == run_id)
            .values(status=QueryRunStatus.RUNNING, worker_id="me")
        )
        db.commit()


@pytest.fixture
def contended(monkeypatch: pytest.MonkeyPatch) -> _Contended:
    """Replace `poller.run_one` with a `_Contended`.

    Parameters
    ----------
    monkeypatch
        The pytest fixture.

    Returns
    -------
    _Contended
        The stand-in.
    """
    stand_in = _Contended()
    monkeypatch.setattr(poller, "run_one", stand_in)
    return stand_in


def _poll_passing_by(session: Session, passed_by: poller.PassedBy) -> bool:
    """Poll once as an interactive worker with a cap, remembering timeouts in `passed_by`.

    Parameters
    ----------
    session
        Database session.
    passed_by
        The poller's memory of principals to pass by.

    Returns
    -------
    bool
        What `poll_once` returned.
    """
    pool = WorkerPool(takes="interactive", running_per_principal=5, lease_seconds=60)
    return poll_once(
        session, AllowAllAuthorization(), worker_id="me", pool=pool, passed_by=passed_by
    )


def test_after_a_cap_lock_timeout_the_principal_is_passed_by_for_30_seconds(
    session: Session, dev: uuid.UUID, contended: _Contended
) -> None:
    """A's claim times out: the next polls take B's younger query, A's is tried again at 30 s."""
    a, b = _person(session, "a"), _person(session, "b")
    first = _queued(session, dev, a, age=10)
    theirs = _queued(session, dev, b, age=20)
    contended.principals.add(a)
    clock = _Clock()
    passed_by = poller.PassedBy(clock=clock)

    assert _poll_passing_by(session, passed_by) is True
    assert contended.tried == [first], "the oldest query is tried and times out"
    assert _poll_passing_by(session, passed_by) is True
    assert contended.tried == [first, theirs], "B's younger query is taken instead"
    assert _poll_passing_by(session, passed_by) is False, (
        "A is still passed by; nothing else queued"
    )
    assert contended.tried == [first, theirs]

    clock.now += poller.PASS_BY_SECONDS - 1
    assert _poll_passing_by(session, passed_by) is False
    assert contended.tried == [first, theirs]
    clock.now += 1
    assert _poll_passing_by(session, passed_by) is True
    assert contended.tried == [first, theirs, first], "after 30 s A's query is tried again"


def _passed_by_until(session: Session, principal_id: uuid.UUID) -> datetime | None:
    """Read the time the database says a principal is passed by until.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The principal.

    Returns
    -------
    datetime or None
        `Principal.query_passed_by_until`.
    """
    session.expire_all()
    return session.scalars(
        select(Principal.query_passed_by_until).where(Principal.id == principal_id)
    ).one()


def test_a_cap_lock_timeout_writes_the_time_the_wait_reason_reads(
    session: Session, dev: uuid.UUID, contended: _Contended
) -> None:
    """#95: the pass-by is also in the database, for 30 s by its clock; others have none."""
    a, b = _person(session, "a"), _person(session, "b")
    _queued(session, dev, a, age=10)
    contended.principals.add(a)
    before = db_now(session)

    assert _poll_passing_by(session, poller.PassedBy(clock=_Clock())) is True

    until = _passed_by_until(session, a)
    assert until is not None
    assert (
        before + timedelta(seconds=poller.PASS_BY_SECONDS)
        <= until
        <= db_now(session) + timedelta(seconds=poller.PASS_BY_SECONDS)
    )
    assert _passed_by_until(session, b) is None


def test_a_held_lock_ends_in_a_reason_that_names_it(
    session: Session, dev: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#95 from end to end: the real claim times out on the lock, the real reason says so.

    Another claim is simulated as holding the principal's lock (the Postgres lock itself is
    not on SQLite): the query stays queued while a worker is free, and shows `lock_timeout`,
    not `starting`.
    """
    a = _person(session, "a")
    run_id = _queued(session, dev, a, age=10)
    _workers(session, "interactive")
    monkeypatch.setattr(poller.pools, "lock_principal", lambda inner, principal_id: False)
    service = QueryService(session, AllowAllAuthorization())
    run = session.get(QueryRun, run_id)
    assert run is not None
    before = service.waiting_for(run)
    assert before is not None and before.code == "starting"

    assert _poll_passing_by(session, poller.PassedBy(clock=_Clock())) is True

    session.expire_all()
    run = session.get(QueryRun, run_id)
    assert run is not None and run.status is QueryRunStatus.QUEUED
    reason = service.waiting_for(run)
    assert reason is not None
    assert reason.code == "lock_timeout"
    assert "lock that serialises the start of this user's queries" in reason.message


def test_a_pass_by_that_cannot_be_written_does_not_stop_the_loop(
    session: Session,
    dev: uuid.UUID,
    contended: _Contended,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The worker still remembers the principal in memory; the reason just stays `starting`."""
    a = _person(session, "a")
    _queued(session, dev, a, age=10)
    contended.principals.add(a)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("UPDATE principals", {}, Exception("database is locked"))

    monkeypatch.setattr(poller.pools, "pass_by", broken)
    passed_by = poller.PassedBy(clock=_Clock())

    assert _poll_passing_by(session, passed_by) is True
    assert passed_by.active() == [a]
    assert _passed_by_until(session, a) is None
    assert "could not be recorded" in caplog.text


def test_a_principal_that_timed_out_still_gets_its_pipeline_work_taken(
    session: Session, dev: uuid.UUID, contended: _Contended
) -> None:
    """The pass-by is for ad-hoc queries only, as the cap is: A's maintenance run is claimed."""
    a = _person(session, "a")
    adhoc = _queued(session, dev, a, age=10)
    maintain = _queued(session, dev, a, age=20, write_mode=WriteMode.MAINTAIN)
    contended.principals.add(a)
    passed_by = poller.PassedBy(clock=_Clock())
    pool = WorkerPool(takes="all", running_per_principal=5, lease_seconds=60)

    assert poll_once(session, AllowAllAuthorization(), pool=pool, passed_by=passed_by) is True
    assert contended.tried == [adhoc]
    contended.principals.clear()
    assert poll_once(session, AllowAllAuthorization(), pool=pool, passed_by=passed_by) is True
    assert contended.tried == [adhoc, maintain]


def test_passed_by_state_belongs_to_the_loop_not_the_module(
    session: Session, dev: uuid.UUID, contended: _Contended
) -> None:
    """Two loops (two `PassedBy`) do not share what they remember."""
    a = _person(session, "a")
    first = _queued(session, dev, a, age=10)
    contended.principals.add(a)
    one, two = poller.PassedBy(clock=_Clock()), poller.PassedBy(clock=_Clock())
    _poll_passing_by(session, one)
    _poll_passing_by(session, two)
    assert contended.tried == [first, first]


def test_poll_forever_keeps_one_memory_for_all_its_iterations() -> None:
    """The loop makes one `PassedBy` and hands the same one to every iteration."""
    drain = threading.Event()
    seen: list[object] = []

    def iteration(*args: object, **kwargs: object) -> bool:
        seen.append(kwargs["passed_by"])
        if len(seen) == 2:
            drain.set()
        return True

    with mock.patch.object(poller, "poll_iteration", iteration):
        poll_forever(mock.Mock(), mock.Mock(), drain=drain, poll_interval_seconds=0)
    assert len(seen) == 2
    assert isinstance(seen[0], poller.PassedBy) and seen[0] is seen[1]
