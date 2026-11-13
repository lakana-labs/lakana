"""Notifications on Postgres: two workers, one row; a failed run to a webhook; a worker that dies.

The webhook is a local HTTPS server with a throwaway CA. The worker's egress client is let through
to the loopback address by its test hook (`_TEST_ALLOW_LOOPBACK`) and the host name resolves to
`127.0.0.1` through a replaced lookup; every other rule (the certificate checked against the name,
the pinned address, no redirects) is the product's. The workers are `deliver_due` calls with
database time, as the worker's thread would make them.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from scripts.test_support.egress_helpers import Authority, Server
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.config import WebhookConfig
from lakana_core.db import create_engine, db_now
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.jobs.service import JobService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    EnvironmentDeployment,
    NotificationDelivery,
    NotificationState,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Run,
    RunStatus,
    TaskRun,
    TaskStatus,
)
from lakana_core.runs.store import scheduler_tick
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_worker import egress, notify
from lakana_worker.deploy import DeploySettings
from lakana_worker.notify import NotifySettings, deliver_due
from lakana_worker.poller import poll_once
from lakana_worker.python_support import PythonSupport
from lakana_worker.tasks import RunEngine, TaskSettings
from tests.integration_helpers import commit_files, unique_name
from tests.python_asset_helpers import MASTER_KEY, QueryStack, query_stack

pytestmark = pytest.mark.integration

NAME = "hooks.example.test"
KEY = "SIGNINGKEYSECRET"
_WAIT = 30.0


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[QueryStack]:
    """Start Postgres, SeaweedFS and a real HTTP server.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    QueryStack
        The stack.
    """
    with query_stack(tmp_path_factory) as built:
        yield built


@pytest.fixture(autouse=True)
def _no_leftovers(stack: QueryStack) -> None:
    """End every active run and every pending notification of an earlier test.

    Parameters
    ----------
    stack
        The stack.
    """
    with Session(stack.db) as session:
        active = [RunStatus.QUEUED, RunStatus.RUNNING]
        session.execute(
            update(Run).where(Run.status.in_(active)).values(status=RunStatus.CANCELLED)
        )
        session.execute(
            update(TaskRun)
            .where(TaskRun.status.in_([TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING]))
            .values(status=TaskStatus.CANCELLED)
        )
        session.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.state == NotificationState.PENDING)
            .values(state=NotificationState.FAILED, next_attempt_at=None)
        )
        session.commit()


@dataclass(frozen=True)
class Hook:
    """A local HTTPS webhook and the worker settings that reach it.

    Parameters
    ----------
    server
        The server; its `requests` list records every request.
    webhook
        The operator settings that trust its CA.
    """

    server: Server
    webhook: WebhookConfig

    @property
    def url(self) -> str:
        """The webhook URL, with a path that stands for a secret.

        Returns
        -------
        str
            The URL.
        """
        return f"https://{NAME}:{self.server.port}/services/T0SECRETPATH"

    def settings(self, owner: str, **changes: object) -> NotifySettings:
        """Build one worker's settings.

        Parameters
        ----------
        owner
            The worker's id.
        **changes
            Fields to override.

        Returns
        -------
        NotifySettings
            The settings.
        """
        base = NotifySettings(
            owner=owner, key_provider=EnvKeyProvider(MASTER_KEY), webhook=self.webhook
        )
        return dataclasses.replace(base, **changes)

    def delivery_ids(self) -> list[str]:
        """List the delivery id of each request received.

        Returns
        -------
        list of str
            The ids, in the order received.
        """
        return [json.loads(body).get("delivery_id") for _, _, body in self.server.requests]


@pytest.fixture
def hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Hook]:
    """Serve a webhook on the loopback address and let the worker's client reach it.

    Parameters
    ----------
    tmp_path
        For the CA.
    monkeypatch
        Sets the egress test hook and the lookup; undone after the test.

    Yields
    ------
    Hook
        The webhook.
    """
    monkeypatch.setattr(egress, "_TEST_ALLOW_LOOPBACK", True)
    monkeypatch.setattr(egress, "_resolve", lambda host, port: ["127.0.0.1"])
    authority = Authority(tmp_path)
    with Server(authority.issue(NAME)) as server:
        yield Hook(server, WebhookConfig(ca_bundle=authority.bundle))


def _secret(stack: QueryStack, hook: Hook, environment: str, name: str = "alerts") -> str:
    """Store the webhook as a secret of an environment.

    Parameters
    ----------
    stack
        The stack.
    hook
        The webhook.
    environment
        The environment.
    name
        The secret's name.

    Returns
    -------
    str
        The name.
    """
    stack.admin.secrets.create(environment, name, {"url": hook.url, "signing_key": KEY})
    return name


def _queue(stack: QueryStack, environment: str, secret: str, count: int = 1) -> list[uuid.UUID]:
    """Queue test notifications, which have no run.

    Parameters
    ----------
    stack
        The stack.
    environment
        The environment.
    secret
        The secret that holds the webhook.
    count
        How many rows.

    Returns
    -------
    list of uuid.UUID
        The delivery ids.
    """
    with Session(stack.db) as session:
        environment_id = session.scalars(
            select(Environment.id).where(Environment.name == environment)
        ).one()
        repository = Repository(
            id=uuid.uuid4(), name=unique_name(), kind=RepositoryKind.LOCAL, url="/nowhere"
        )
        session.add(repository)
        session.flush()
        ids = [uuid.uuid4() for _ in range(count)]
        now = db_now(session)
        for row_id in ids:
            session.add(
                NotificationDelivery(
                    id=row_id,
                    run_id=None,
                    environment_id=environment_id,
                    repository_id=repository.id,
                    job="daily",
                    event="test",
                    format="json",
                    secret_name=secret,
                    state=NotificationState.PENDING,
                    attempts=0,
                    next_attempt_at=now - timedelta(seconds=1),
                )
            )
        session.commit()
    return ids


def _row(stack: QueryStack, row_id: uuid.UUID) -> NotificationDelivery:
    """Read a delivery.

    Parameters
    ----------
    stack
        The stack.
    row_id
        Its id.

    Returns
    -------
    NotificationDelivery
        The row, detached.
    """
    with Session(stack.db) as session:
        row = session.get(NotificationDelivery, row_id)
        assert row is not None
        session.expunge(row)
        return row


def _now(stack: QueryStack) -> datetime:
    """Read the database's time.

    Parameters
    ----------
    stack
        The stack.

    Returns
    -------
    datetime
        Database time.
    """
    with Session(stack.db) as session:
        return db_now(session)


def test_two_workers_that_meet_one_row_send_it_once(stack: QueryStack, hook: Hook) -> None:
    environment = stack.environment()
    (row_id,) = _queue(stack, environment, _secret(stack, hook, environment))
    sending = threading.Event()
    other_done = threading.Event()
    real_send = notify.send_webhook

    def slow_send(*args: object) -> object:
        """Hold the first worker inside its send until the second has come and gone.

        Parameters
        ----------
        *args
            The sender's arguments.

        Returns
        -------
        object
            The result of the real send.
        """
        sending.set()
        assert other_done.wait(_WAIT)
        return real_send(*args)  # ty: ignore[invalid-argument-type]

    first = hook.settings("worker-a", send=slow_send)
    second = hook.settings("worker-b")
    outcome: list[int] = []
    thread = threading.Thread(
        target=lambda: outcome.append(deliver_due(stack.db, _now(stack), first)), daemon=True
    )
    thread.start()
    assert sending.wait(_WAIT)
    # The first worker is inside its send: the row is leased, so the second finds nothing due.
    assert deliver_due(stack.db, _now(stack), second) == 0
    other_done.set()
    thread.join(timeout=_WAIT)
    assert outcome == [1]
    assert hook.delivery_ids() == [str(row_id)]
    row = _row(stack, row_id)
    assert (row.state, row.attempts, row.lease_owner) == (NotificationState.DELIVERED, 1, None)


def test_a_row_another_transaction_holds_is_passed_by_not_waited_for(
    stack: QueryStack, hook: Hook
) -> None:
    environment = stack.environment()
    (row_id,) = _queue(stack, environment, _secret(stack, hook, environment))

    def short_lock_timeout(connection: object, record: object) -> None:
        """Make a wait for a row lock fail after two seconds.

        Parameters
        ----------
        connection
            The new DBAPI connection.
        record
            Unused.
        """
        cursor = connection.cursor()  # ty: ignore[unresolved-attribute]
        cursor.execute("SET lock_timeout = '2s'")
        cursor.close()

    claimer = create_engine(stack.services.settings.database)
    event.listen(claimer, "connect", short_lock_timeout)
    try:
        with Session(stack.db) as holder:
            holder.execute(
                select(NotificationDelivery)
                .where(NotificationDelivery.id == row_id)
                .with_for_update()
            ).all()
            # Without SKIP LOCKED the claim waits and fails on the lock timeout; without
            # FOR UPDATE its UPDATE does. With both it finds nothing.
            assert deliver_due(claimer, _now(stack), hook.settings("worker-a")) == 0
            holder.rollback()
        assert not hook.server.requests
        assert deliver_due(claimer, _now(stack), hook.settings("worker-a")) == 1
    finally:
        claimer.dispose()


def test_workers_that_drain_a_queue_together_send_each_row_once(
    stack: QueryStack, hook: Hook
) -> None:
    environment = stack.environment()
    ids = _queue(stack, environment, _secret(stack, hook, environment), count=12)
    errors: list[BaseException] = []

    def drain(owner: str) -> None:
        """Deliver in small batches until nothing is due.

        Parameters
        ----------
        owner
            The worker's id.
        """
        try:
            settings = hook.settings(owner)
            while deliver_due(stack.db, _now(stack), settings, limit=3):
                pass
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=drain, args=(f"worker-{n}",), daemon=True) for n in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_WAIT * 2)
    assert not errors
    assert sorted(hook.delivery_ids()) == sorted(str(row_id) for row_id in ids)
    assert {_row(stack, row_id).state for row_id in ids} == {NotificationState.DELIVERED}


def test_a_worker_that_dies_mid_delivery_is_followed_by_a_redelivery_with_the_same_id(
    stack: QueryStack, hook: Hook
) -> None:
    class Died(BaseException):
        """The process ends: nothing after the send runs."""

    real_send = notify.send_webhook

    def send_then_die(*args: object) -> None:
        """Let the request reach the webhook, then end the worker before it records.

        Parameters
        ----------
        *args
            The sender's arguments.

        Raises
        ------
        Died
            Always, after the request went out.
        """
        real_send(*args)  # ty: ignore[invalid-argument-type]
        raise Died

    environment = stack.environment()
    (row_id,) = _queue(stack, environment, _secret(stack, hook, environment))
    start = _now(stack)
    with pytest.raises(Died):
        deliver_due(stack.db, start, hook.settings("dead-worker", send=send_then_die))
    held = _row(stack, row_id)
    assert (held.state, held.lease_owner, held.attempts) == (
        NotificationState.PENDING,
        "dead-worker",
        1,
    )
    # While the lease runs, another worker leaves the row alone.
    survivor = hook.settings("survivor")
    assert deliver_due(stack.db, start + timedelta(seconds=30), survivor) == 0
    # Once it ran out, the survivor takes the row over and sends it again.
    assert deliver_due(stack.db, start + timedelta(seconds=61), survivor) == 1
    row = _row(stack, row_id)
    assert (row.state, row.attempts, row.lease_owner) == (NotificationState.DELIVERED, 2, None)
    assert hook.delivery_ids() == [str(row_id), str(row_id)]


_YAML = (
    "defaults:\n  retry_delay: 0\n  retries: 0\n"
    "jobs:\n"
    "  daily:\n"
    "    select: ['silver.*']\n"
    "    notify: { webhook: alerts, format: slack }\n"
)


def _admin(stack: QueryStack) -> PrincipalRef:
    """Return the bootstrap administrator.

    Parameters
    ----------
    stack
        The stack.

    Returns
    -------
    PrincipalRef
        The administrator.
    """
    return PrincipalRef(id=stack.principal_id("admin"), name="admin", type=PrincipalType.USER)


def test_a_failed_job_run_delivers_a_slack_message_and_stays_failed(
    stack: QueryStack, hook: Hook
) -> None:
    environment = stack.environment()
    _secret(stack, hook, environment)
    repository = unique_name()
    path = stack.services.roots / repository
    commit_files(
        path,
        {
            "lakana.yaml": _YAML,
            "assets/silver/x.sql": (
                "SELECT CAST('not a number' AS INTEGER) AS id FROM bronze.nums\n"
            ),
        },
    )
    stack.admin.repositories.create(repository, "local", str(path))
    deploy_settings = DeploySettings(
        repo_roots=(str(stack.services.roots),),
        python=PythonSupport(server_url=stack.url, uv=shutil.which("uv") or "/usr/bin/uv"),
    )
    engine = RunEngine(
        session_factory=lambda: Session(stack.db),
        authz_factory=GrantAuthorization,
        settings=TaskSettings(worker_id="run-worker", renew_seconds=1, lease_seconds=30),
    )

    def work() -> None:
        """Run what is queued, as a worker would."""
        while True:
            with Session(stack.db) as session:
                if not poll_once(
                    session, GrantAuthorization(session), deploy=deploy_settings, engine=engine
                ):
                    return

    deploy = stack.admin.deploys.create(environment, repository)
    work()
    stack.admin.deploys.wait(environment, deploy.id, timeout=30)
    with Session(stack.db) as session:
        account = Principal(
            id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name=unique_name(), is_active=True
        )
        session.add(account)
        session.flush()
        session.execute(
            update(EnvironmentDeployment)
            .where(
                EnvironmentDeployment.repository_id
                == select(Repository.id).where(Repository.name == repository).scalar_subquery()
            )
            .values(run_as_principal_id=account.id)
        )
        session.commit()
        started = JobService(session, GrantAuthorization(session)).start(
            _admin(stack), environment, repository, "daily"
        )
    assert started.run is not None
    run_id = uuid.UUID(str(started.run.id))

    for _ in range(50):
        with Session(stack.db) as session:
            scheduler_tick(session, db_now(session), lambda: 0.5)
        work()
        with Session(stack.db) as session:
            run = session.get(Run, run_id)
            assert run is not None
            status = run.status
        if status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
            break
    assert status is RunStatus.FAILED

    with Session(stack.db) as session:
        rows = list(
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.run_id == run_id)
            )
        )
    assert [(row.event, row.format, row.state) for row in rows] == [
        ("failed", "slack", NotificationState.PENDING)
    ]
    assert deliver_due(stack.db, _now(stack), hook.settings("notify-worker")) == 1

    (_, headers, body) = hook.server.requests[0]
    text = json.loads(body)["text"]
    assert "daily" in text and "failed" in text and environment in text
    assert "X-Lakana-Signature" not in headers  # only a json webhook is signed
    assert "not a number" not in text  # no error text, no data
    assert _row(stack, rows[0].id).state is NotificationState.DELIVERED
    with Session(stack.db) as session:
        run = session.get(Run, run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED
        failures = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.NOTIFICATION_FAILED)
        ).all()
        assert not [e for e in failures if e.securable_environment == environment]
