"""The delivery loop: claims, secrets, results, retries, and what must never leak."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from lakana_core.config import DatabaseConfig, WebhookConfig
from lakana_core.db import create_engine
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    Environment,
    JobVersion,
    Namespace,
    NotificationDelivery,
    NotificationState,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    RunTrigger,
    Secret,
    SecretUseKind,
    Table,
    TableHealthSignal,
    TableType,
    TaskRun,
    TaskStatus,
)
from lakana_core.secrets.crypto import SecretBinding, encrypt_secret_values
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_worker import egress, notify
from lakana_worker.egress import DeliveryResult
from lakana_worker.notify import NotifySettings, deliver_due

T0 = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)
URL = "https://hooks.example.test/services/T0SECRETPATH/B0/XSECRETKEY?token=SECRETQUERY"
KEY = "SIGNINGKEYSECRET"
KEYS = EnvKeyProvider(base64.b64encode(b"k" * 32).decode())
OK = DeliveryResult(ok=True, permanent=False, reason="the server answered 200", status=200)


class Sent:
    """A fake sender that records each request and answers from a script.

    Parameters
    ----------
    *answers
        The results to return in turn; the last repeats. An `Exception` is raised instead.
    """

    def __init__(self, *answers: DeliveryResult | Exception) -> None:
        self.answers = list(answers) or [OK]
        self.requests: list[tuple[str, bytes, dict[str, str]]] = []
        self.during: list = []

    def __call__(
        self, url: str, body: bytes, headers: dict[str, str], webhook: WebhookConfig, timeout: float
    ) -> DeliveryResult:
        """Record the request, run the hooks and answer from the script.

        Parameters
        ----------
        url
            The webhook URL.
        body
            The request body.
        headers
            The request headers.
        webhook
            The operator settings.
        timeout
            The attempt's time budget.

        Returns
        -------
        DeliveryResult
            The next scripted result.

        Raises
        ------
        Exception
            The scripted exception, when the script holds one.
        """
        self.requests.append((url, body, headers))
        for hook in self.during:
            hook()
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class World:
    """A database with an environment, a failed run and a webhook secret.

    Parameters
    ----------
    engine
        The database.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        with Session(engine) as session:
            self.admin = Principal(
                id=uuid.uuid4(), type=PrincipalType.USER, name="admin", is_active=True
            )
            self.environment = Environment(
                id=uuid.uuid4(), name="prod", storage_root="file:///tmp/p"
            )
            self.repository = Repository(
                id=uuid.uuid4(), name="analytics", kind=RepositoryKind.HTTPS, url="https://h/a.git"
            )
            session.add_all([self.admin, self.environment, self.repository])
            session.flush()
            version = JobVersion(
                id=uuid.uuid4(),
                repository_id=self.repository.id,
                environment_id=self.environment.id,
                commit_sha="a" * 40,
                manifest_json={"version": 6, "assets": [], "edges": [], "jobs": {}},
                code_uri="file:///tmp/p/code.tar.gz",
                code_sha256="0" * 64,
            )
            session.add(version)
            session.flush()
            self.run_id = uuid.uuid4()
            self.env_id = self.environment.id
            self.repo_id = self.repository.id
            self.admin_id = self.admin.id
            session.add(
                Run(
                    id=self.run_id,
                    environment_id=self.environment.id,
                    repository_id=self.repository.id,
                    job_version_id=version.id,
                    kind=RunKind.FULL,
                    selection_json={},
                    status=RunStatus.FAILED,
                    created_at=T0,
                    finished_at=T0,
                    job="daily",
                    trigger=RunTrigger.SCHEDULE,
                )
            )
            session.flush()
            session.add(
                TaskRun(
                    id=uuid.uuid4(),
                    run_id=self.run_id,
                    asset="silver.x",
                    status=TaskStatus.FAILED,
                    error_message="ROWVALUE",
                    max_attempts=1,
                    timeout_seconds=60,
                    retry_delay_seconds=0,
                )
            )
            session.commit()
        self.first = True
        self.secret("alerts", {"url": URL, "signing_key": KEY})

    def another_run(self, session: Session) -> uuid.UUID:
        """Add a failed run like the first.

        Parameters
        ----------
        session
            Database session.

        Returns
        -------
        uuid.UUID
            The run's id.
        """
        first = session.get(Run, self.run_id)
        assert first is not None
        run_id = uuid.uuid4()
        session.add(
            Run(
                id=run_id,
                environment_id=first.environment_id,
                repository_id=first.repository_id,
                job_version_id=first.job_version_id,
                kind=RunKind.FULL,
                selection_json={},
                status=RunStatus.FAILED,
                created_at=T0,
                finished_at=T0,
                job="daily",
                trigger=RunTrigger.SCHEDULE,
            )
        )
        session.flush()
        return run_id

    def secret(
        self,
        name: str,
        values: dict[str, str],
        *,
        revoked: bool = False,
        use: tuple[SecretUseKind, uuid.UUID | None] | None = None,
    ) -> None:
        """Store a secret in the environment.

        Parameters
        ----------
        name
            The secret's name.
        values
            Its key-value bag.
        revoked
            Whether it is revoked.
        use
            The secret's use and target; `None` for unbound.
        """
        secret_id = uuid.uuid4()
        sealed = encrypt_secret_values(values, KEYS, SecretBinding(secret_id, self.env_id, name))
        with Session(self.engine) as session:
            session.add(
                Secret(
                    id=secret_id,
                    environment_id=self.env_id,
                    name=name,
                    ciphertext=sealed.ciphertext,
                    nonce=sealed.nonce,
                    wrapped_data_key=sealed.wrapped_data_key,
                    format_version=sealed.format_version,
                    key_id=sealed.key_id,
                    created_by=self.admin_id,
                    created_at=T0,
                    revoked_by=self.admin_id if revoked else None,
                    revoked_at=T0 if revoked else None,
                    use_kind=use[0] if use else None,
                    use_target_id=use[1] if use else None,
                )
            )
            session.commit()

    def delivery(
        self,
        *,
        fmt: str = "json",
        secret: str = "alerts",
        event: str = "failed",
        run: bool = True,
        attempts: int = 0,
        due: datetime | None = T0,
        lease: tuple[str, datetime] | None = None,
    ) -> uuid.UUID:
        """Queue a delivery.

        Parameters
        ----------
        fmt
            Its format.
        secret
            The secret it names.
        event
            Its event.
        run
            Whether it belongs to the run; a test delivery has none.
        attempts
            Attempts already made.
        due
            When it is due.
        lease
            An owner and expiry, when a worker holds it.

        Returns
        -------
        uuid.UUID
            The delivery id.
        """
        row_id = uuid.uuid4()
        with Session(self.engine) as session:
            run_id = None
            if run:
                # (run, event) is unique, so a further delivery gets a run of its own.
                run_id = self.run_id if self.first else self.another_run(session)
                self.first = False
            session.add(
                NotificationDelivery(
                    id=row_id,
                    run_id=run_id,
                    environment_id=self.env_id,
                    repository_id=self.repo_id,
                    job="daily",
                    event=event,
                    format=fmt,
                    secret_name=secret,
                    state=NotificationState.PENDING,
                    attempts=attempts,
                    next_attempt_at=due,
                    lease_owner=lease[0] if lease else None,
                    lease_expires_at=lease[1] if lease else None,
                )
            )
            session.commit()
        return row_id

    def row(self, row_id: uuid.UUID) -> NotificationDelivery:
        """Read a delivery.

        Parameters
        ----------
        row_id
            Its id.

        Returns
        -------
        NotificationDelivery
            The row, detached.
        """
        with Session(self.engine) as session:
            row = session.get(NotificationDelivery, row_id)
            assert row is not None
            session.expunge(row)
            return row

    def audit(self) -> list[AuditEvent]:
        """List the notification audit events.

        Returns
        -------
        list of AuditEvent
            The events, detached.
        """
        with Session(self.engine) as session:
            rows = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.event_type == AuditEventType.NOTIFICATION_FAILED
                    )
                )
            )
            session.expunge_all()
            return rows


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    """Provide a database with a failed run and the secret `alerts`.

    Parameters
    ----------
    tmp_path
        Pytest's directory.

    Yields
    ------
    World
        The world.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/n.db"))
    Base.metadata.create_all(engine)
    yield World(engine)
    engine.dispose()


def settings(sent: Sent, owner: str = "w1", **changes: object) -> NotifySettings:
    """Build settings around a fake sender and a steady jitter.

    Parameters
    ----------
    sent
        The sender.
    owner
        The worker's id.
    **changes
        Fields to override.

    Returns
    -------
    NotifySettings
        The settings.
    """
    base = NotifySettings(owner=owner, key_provider=KEYS, send=sent, rand=lambda: 0.5)
    return dataclasses.replace(base, **changes)


def test_a_2xx_answer_delivers_exactly_the_built_bytes_signed_over_the_timestamp(
    world: World,
) -> None:
    row_id = world.delivery()
    sent = Sent()
    assert deliver_due(world.engine, T0, settings(sent), limit=10) == 1
    url, body, headers = sent.requests[0]
    assert url == URL
    assert json.loads(body)["delivery_id"] == str(row_id)
    stamp = headers["X-Lakana-Timestamp"]
    assert stamp == str(int(T0.timestamp()))
    digest = hmac.new(KEY.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    assert headers["X-Lakana-Signature"] == f"sha256={digest}"
    row = world.row(row_id)
    assert (row.state, row.attempts, row.lease_owner, row.next_attempt_at) == (
        NotificationState.DELIVERED,
        1,
        None,
        None,
    )
    assert row.delivered_at == T0
    assert world.audit() == []


def test_only_a_json_webhook_with_a_signing_key_is_signed(world: World) -> None:
    sent = Sent()
    world.delivery(fmt="slack")
    deliver_due(world.engine, T0, settings(sent), limit=10)
    assert "X-Lakana-Signature" not in sent.requests[0][2]
    assert json.loads(sent.requests[0][1])["text"].startswith("Lakana: job")
    world.secret("plain", {"url": URL})
    world.delivery(secret="plain")
    deliver_due(world.engine, T0, settings(sent), limit=10)
    assert "X-Lakana-Signature" not in sent.requests[1][2]


def test_a_permanent_answer_fails_at_once_and_is_audited_without_the_url(world: World) -> None:
    row_id = world.delivery()
    sent = Sent(DeliveryResult(False, True, "the server answered 404", 404))
    deliver_due(world.engine, T0, settings(sent), limit=10)
    row = world.row(row_id)
    assert (row.state, row.attempts, row.last_result) == (
        NotificationState.FAILED,
        1,
        "the server answered 404",
    )
    (event,) = world.audit()
    assert event.principal_id is None
    assert event.securable_environment == "prod"
    assert event.detail == {
        "run_id": str(world.run_id),
        "job": "daily",
        "event": "failed",
        "reason": "the server answered 404",
    }


@pytest.mark.parametrize(
    "answer",
    [
        DeliveryResult(False, False, "the server answered 503", 503),
        DeliveryResult(False, False, "the server answered 429", 429),
        DeliveryResult(False, False, "timeout", None),
    ],
)
def test_other_failures_retry_with_the_backoff(world: World, answer: DeliveryResult) -> None:
    row_id = world.delivery()
    sent = Sent(answer)
    now = T0
    waits = []
    for _ in range(4):
        assert deliver_due(world.engine, now, settings(sent), limit=10) == 1
        row = world.row(row_id)
        assert row.state is NotificationState.PENDING and row.lease_owner is None
        assert row.last_result == answer.reason
        assert row.next_attempt_at is not None
        waits.append((row.next_attempt_at - now).total_seconds())
        assert deliver_due(world.engine, now, settings(sent), limit=10) == 0  # not due yet
        now = row.next_attempt_at
    assert waits == [30, 120, 600, 1800]


def test_the_fifth_attempt_is_the_last_and_is_audited(world: World) -> None:
    row_id = world.delivery(attempts=4)
    sent = Sent(DeliveryResult(False, False, "timeout", None))
    deliver_due(world.engine, T0, settings(sent), limit=10)
    row = world.row(row_id)
    assert (row.state, row.attempts, row.last_result) == (NotificationState.FAILED, 5, "timeout")
    assert [e.detail["reason"] for e in world.audit()] == ["timeout"]


def test_an_attempt_that_raises_is_retried_with_a_fixed_reason(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    row_id = world.delivery()
    deliver_due(world.engine, T0, settings(Sent(RuntimeError(URL + KEY))), limit=10)
    row = world.row(row_id)
    assert (row.state, row.last_result) == (NotificationState.PENDING, notify.UNEXPECTED)
    assert (
        row.last_result == "the sender failed in a way that was not expected (see the worker's log)"
    )
    assert row.last_result != egress.FAILED
    assert URL not in caplog.text and KEY not in caplog.text and "SECRET" not in caplog.text


def test_a_row_whose_lease_ran_out_is_taken_over_and_one_that_is_held_is_not(
    world: World,
) -> None:
    held = world.delivery(lease=("other", T0 + timedelta(seconds=30)))
    lost = world.delivery(lease=("dead", T0 - timedelta(seconds=1)), attempts=1)
    sent = Sent()
    assert deliver_due(world.engine, T0, settings(sent), limit=10) == 1
    assert world.row(held).state is NotificationState.PENDING
    taken = world.row(lost)
    assert (taken.state, taken.attempts) == (NotificationState.DELIVERED, 2)
    assert json.loads(sent.requests[0][1])["delivery_id"] == str(lost)


def test_the_claim_sets_a_lease_of_a_minute_and_counts_the_attempt_before_sending(
    world: World,
) -> None:
    row_id = world.delivery()
    seen: list[NotificationDelivery] = []
    sent = Sent()
    sent.during.append(lambda: seen.append(world.row(row_id)))
    deliver_due(world.engine, T0, settings(sent), limit=10)
    assert (seen[0].lease_owner, seen[0].lease_expires_at, seen[0].attempts) == (
        "w1",
        T0 + timedelta(seconds=60),
        1,
    )


def test_a_lost_lease_records_nothing(world: World) -> None:
    row_id = world.delivery()
    sent = Sent()

    def other_worker_takes_over() -> None:
        with Session(world.engine) as session:
            session.execute(
                update(NotificationDelivery)
                .where(NotificationDelivery.id == row_id)
                .values(lease_owner="w2", lease_expires_at=T0 + timedelta(seconds=200))
            )
            session.commit()

    sent.during.append(other_worker_takes_over)
    deliver_due(world.engine, T0, settings(sent), limit=10)
    row = world.row(row_id)
    assert (row.state, row.lease_owner, row.delivered_at) == (NotificationState.PENDING, "w2", None)
    assert world.audit() == []


def test_a_drain_claims_nothing_and_charges_nothing_for_a_row_it_never_sent(
    world: World,
) -> None:
    first = world.delivery()
    second = world.delivery()
    stop = threading.Event()
    stop.set()
    assert deliver_due(world.engine, T0, settings(Sent()), limit=10, stop=stop) == 0
    assert world.row(first).lease_owner is None

    stop.clear()
    sent = Sent()
    sent.during.append(stop.set)  # the drain arrives during the first send
    assert deliver_due(world.engine, T0, settings(sent), limit=10, stop=stop) == 1
    rows = [world.row(first), world.row(second)]
    assert sorted(r.state.value for r in rows) == ["delivered", "pending"]
    left = next(r for r in rows if r.state is NotificationState.PENDING)
    assert (left.lease_owner, left.lease_expires_at, left.attempts) == (None, None, 0)


def test_each_row_is_claimed_with_a_fresh_time_so_its_lease_covers_its_own_send(
    world: World,
) -> None:
    for _ in range(3):
        world.delivery()
    ticks = iter(T0 + timedelta(seconds=25 * n) for n in range(100))
    clock_now: list[datetime] = []

    def clock() -> datetime:
        clock_now.append(next(ticks))
        return clock_now[-1]

    covered: list[tuple[datetime, datetime | None, str]] = []
    sent = Sent()

    def look() -> None:
        with Session(world.engine) as session:
            leased = session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.lease_owner == "w1")
            ).all()
        assert len(leased) == 1  # only the row being sent is held
        covered.append((clock_now[-1], leased[0].lease_expires_at, leased[0].last_result or ""))

    sent.during.append(look)
    assert deliver_due(world.engine, clock, settings(sent), limit=10) == 3
    for claimed_at, expires, _ in covered:
        assert expires == claimed_at + timedelta(seconds=60)
    stamps = [int(h["X-Lakana-Timestamp"]) for _, _, h in sent.requests]
    assert stamps == sorted(set(stamps)) and len(stamps) == 3  # each signed at its own claim
    with Session(world.engine) as session:
        delivered = sorted(
            r.delivered_at
            for r in session.scalars(select(NotificationDelivery)).all()
            if r.delivered_at is not None
        )
    assert delivered == [T0 + timedelta(seconds=25 * n) for n in (0, 1, 2)]


def test_the_step_stops_claiming_when_its_budget_is_used(world: World) -> None:
    for _ in range(3):
        world.delivery()
    sent = Sent()
    assert deliver_due(world.engine, T0, settings(sent), limit=10, budget=0) == 0
    assert not sent.requests


def test_a_test_delivery_has_no_run_and_says_so(world: World) -> None:
    row_id = world.delivery(event="test", run=False)
    sent = Sent()
    deliver_due(world.engine, T0, settings(sent), limit=10)
    data = json.loads(sent.requests[0][1])
    assert data["event"] == "test"
    assert data["run_id"] == "00000000-0000-0000-0000-000000000000"
    assert data["delivery_id"] == str(row_id)
    assert world.row(row_id).state is NotificationState.DELIVERED


def test_a_health_delivery_carries_the_table_and_the_alert_of_its_signal(world: World) -> None:
    with Session(world.engine) as session:
        namespace = Namespace(id=uuid.uuid4(), environment_id=world.env_id, path="silver")
        session.add(namespace)
        session.flush()
        table = Table(
            id=uuid.uuid4(), namespace_id=namespace.id, name="orders", table_type=TableType.ICEBERG
        )
        session.add(table)
        session.flush()
        signal = TableHealthSignal(
            id=uuid.uuid4(),
            environment_id=world.env_id,
            table_id=table.id,
            signal="volume",
            column_name=None,
            snapshot_id=7,
            sequence_number=7,
            observed={"added": 3, "total": 3},
            band={"low": 900, "high": 1_100, "median": 1_000},
            state="fired",
            effect="warn",
            observed_at=T0,
        )
        session.add(signal)
        session.commit()
        signal_id = signal.id
    row_id = world.delivery(event="health", run=False)
    with Session(world.engine) as session:
        session.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id == row_id)
            .values(health_signal_id=signal_id)
        )
        session.commit()
    sent = Sent()
    deliver_due(world.engine, T0, settings(sent), limit=10)
    data = json.loads(sent.requests[0][1])
    assert (data["event"], data["table"], data["run_id"], data["delivery_id"]) == (
        "health",
        "silver.orders",
        "",
        str(row_id),
    )
    assert data["alert"].startswith("silver.orders: volume 3 rows, usually 900")
    assert data["command"] == "lakana table health silver.orders --env prod"
    assert world.row(row_id).state is NotificationState.DELIVERED


NO_URL = notify.NOT_A_WEBHOOK


@pytest.mark.parametrize(
    ("name", "values", "revoked", "reason"),
    [
        ("absent", None, False, notify.SECRET_MISSING),
        ("revoked", {"url": URL}, True, notify.SECRET_REVOKED),
        ("nourl", {"signing_key": KEY, "other": URL}, False, NO_URL),
        ("empty", {"url": ""}, False, NO_URL),
        ("junk", {"url": "SECRETJUNK"}, False, NO_URL),
        ("ftp", {"url": "ftp://hooks.example.test/SECRETPATH"}, False, NO_URL),
    ],
)
def test_a_secret_that_is_not_a_webhook_fails_with_a_fixed_reason_and_sends_nothing(
    world: World,
    caplog: pytest.LogCaptureFixture,
    name: str,
    values: dict[str, str] | None,
    revoked: bool,
    reason: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    if values is not None:
        world.secret(name, values, revoked=revoked)
    row_id = world.delivery(secret=name)
    sent = Sent()
    deliver_due(world.engine, T0, settings(sent, webhook=WebhookConfig()), limit=10)
    row = world.row(row_id)
    assert row.state is NotificationState.FAILED
    assert row.last_result == reason.format(name)
    assert not sent.requests
    dump = json.dumps([row.last_result, [e.detail for e in world.audit()]]) + caplog.text
    assert "SECRET" not in dump and URL not in dump and KEY not in dump
    assert len(world.audit()) == 1


def test_a_webhook_secret_bound_to_an_index_fails_and_sends_nothing(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """A secret that serves a package index never becomes a webhook URL."""
    caplog.set_level(logging.DEBUG)
    world.secret("hook", {"url": URL}, use=(SecretUseKind.PACKAGE_INDEX, uuid.uuid4()))
    row_id = world.delivery(secret="hook")
    sent = Sent()

    deliver_due(world.engine, T0, settings(sent), limit=10)

    row = world.row(row_id)
    assert row.state is NotificationState.FAILED
    assert "is bound to a package index that was removed" in (row.last_result or "")
    assert row.last_result is not None and row.last_result.endswith(
        "lakana secret bind hook --env prod --own-env"
    )
    assert not sent.requests
    assert URL not in row.last_result + caplog.text + json.dumps([e.detail for e in world.audit()])


def test_a_secret_bound_to_its_environment_serves_a_webhook(world: World) -> None:
    """Unbound (every other test here) and environment-bound secrets both send."""
    world.secret("hook", {"url": URL}, use=(SecretUseKind.ENVIRONMENT, None))
    row_id = world.delivery(secret="hook")
    sent = Sent()

    deliver_due(world.engine, T0, settings(sent), limit=10)

    assert world.row(row_id).state is NotificationState.DELIVERED
    assert len(sent.requests) == 1


def test_an_http_url_is_refused_by_the_real_sender_unless_the_host_is_allowed(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    world.secret("plainhttp", {"url": "http://hooks.example.test/SECRETPATH"})
    row_id = world.delivery(secret="plainhttp")
    deliver_due(world.engine, T0, NotifySettings(owner="w1", key_provider=KEYS), limit=10)
    row = world.row(row_id)
    assert (row.state, row.last_result) == (NotificationState.FAILED, egress.NOT_HTTPS)
    assert "SECRET" not in caplog.text + (row.last_result or "")


def test_a_row_whose_last_attempt_was_lost_is_failed_not_sent_again(world: World) -> None:
    row_id = world.delivery(attempts=5, lease=("dead", T0 - timedelta(seconds=1)))
    sent = Sent()
    assert deliver_due(world.engine, T0, settings(sent), limit=10) == 0
    row = world.row(row_id)
    assert (row.state, row.last_result) == (NotificationState.FAILED, notify.GAVE_UP)
    assert "never recorded" in notify.GAVE_UP
    assert not sent.requests
    assert len(world.audit()) == 1


def test_a_secret_that_cannot_be_decrypted_is_retried_and_fails_only_after_the_last_attempt(
    world: World,
) -> None:
    row_id = world.delivery()
    wrong = EnvKeyProvider(base64.b64encode(b"x" * 32).decode())
    sent = Sent()
    reason = notify.CANNOT_DECRYPT.format("alerts")
    now = T0
    for attempt in range(1, 5):
        deliver_due(world.engine, now, settings(sent, key_provider=wrong), limit=10)
        row = world.row(row_id)
        assert (row.state, row.last_result, row.attempts) == (
            NotificationState.PENDING,
            reason,
            attempt,
        )
        assert row.next_attempt_at is not None
        now = row.next_attempt_at
    deliver_due(world.engine, now, settings(sent, key_provider=wrong), limit=10)
    row = world.row(row_id)
    assert (row.state, row.last_result, row.attempts) == (NotificationState.FAILED, reason, 5)
    assert not sent.requests


def test_a_secret_in_the_old_format_fails_at_once_with_the_command_to_set_it_again(
    world: World,
) -> None:
    with Session(world.engine) as session:
        session.execute(update(Secret).values(format_version=1))
        session.commit()
    row_id = world.delivery()
    deliver_due(world.engine, T0, settings(Sent()), limit=10)
    row = world.row(row_id)
    assert (row.state, row.last_result) == (
        NotificationState.FAILED,
        notify.OLD_FORMAT.format("alerts"),
    )
    assert "lakana secret rotate" in notify.OLD_FORMAT


def test_a_secret_from_a_newer_lakana_is_retried_so_that_an_upgraded_worker_can_send_it(
    world: World,
) -> None:
    with Session(world.engine) as session:
        session.execute(update(Secret).values(format_version=99))
        session.commit()
    row_id = world.delivery()
    deliver_due(world.engine, T0, settings(Sent()), limit=10)
    row = world.row(row_id)
    assert (row.state, row.last_result) == (
        NotificationState.PENDING,
        notify.NEWER_FORMAT.format("alerts"),
    )
    assert "upgrade this worker" in notify.NEWER_FORMAT


def test_the_exact_expiry_of_the_lease_is_part_of_the_record(world: World) -> None:
    row_id = world.delivery()
    sent = Sent()

    def renew_by_someone_else_with_the_same_owner() -> None:
        with Session(world.engine) as session:
            session.execute(
                update(NotificationDelivery)
                .where(NotificationDelivery.id == row_id)
                .values(lease_expires_at=T0 + timedelta(seconds=999))
            )
            session.commit()

    sent.during.append(renew_by_someone_else_with_the_same_owner)
    deliver_due(world.engine, T0, settings(sent), limit=10)
    row = world.row(row_id)
    assert (row.state, row.lease_owner, row.delivered_at) == (NotificationState.PENDING, "w1", None)


FAILURES = [
    DeliveryResult(False, True, "the server answered 410", 410),
    DeliveryResult(False, False, "timeout", None),
    DeliveryResult(False, False, "TLS error", None),
]


@pytest.mark.parametrize("answer", FAILURES)
def test_no_url_or_key_reaches_a_log_a_row_or_an_audit_detail(
    world: World, caplog: pytest.LogCaptureFixture, answer: DeliveryResult
) -> None:
    caplog.set_level(logging.DEBUG)
    row_id = world.delivery(attempts=4)
    deliver_due(world.engine, T0, settings(Sent(answer)), limit=10)
    with Session(world.engine) as session:
        columns = session.get(NotificationDelivery, row_id)
        assert columns is not None
        everything = json.dumps(
            [
                [str(getattr(columns, c.name)) for c in NotificationDelivery.__table__.columns],
                [e.detail for e in world.audit()],
            ]
        )
    for leak in (URL, KEY, "SECRET", "hooks.example.test", "ROWVALUE"):
        assert leak not in everything + caplog.text, leak


def test_delivery_never_writes_to_the_run_or_its_tasks(world: World) -> None:
    def snapshot() -> list[str]:
        with Session(world.engine) as session:
            run = session.get(Run, world.run_id)
            tasks = session.scalars(select(TaskRun)).all()
            assert run is not None
            return [repr((run.status, run.error_message, run.finished_at))] + [
                repr((t.status, t.error_message, t.attempt, t.claim_count)) for t in tasks
            ]

    before = snapshot()
    world.delivery(attempts=4)
    deliver_due(world.engine, T0, settings(Sent(FAILURES[1])), limit=10)
    deliver_due(world.engine, T0, settings(Sent(OK)), limit=10)
    assert snapshot() == before


def test_the_default_sender_checks_the_destination_and_posts(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        notify, "check_destination", lambda url, config: calls.append("check") or "dest"
    )
    monkeypatch.setattr(
        notify, "post", lambda dest, body, headers, *, timeout: calls.append("post") or OK
    )
    assert notify.send_webhook(URL, b"{}", {}, WebhookConfig(), 10.0) == OK
    assert calls == ["check", "post"]


@pytest.mark.parametrize(
    ("reason", "permanent"),
    [
        ("egress.PRIVATE", True),
        ("egress.NOT_HTTPS", True),
        ("egress.INVALID", True),
        ("egress.NOT_443", True),
        ("egress.UNRESOLVED", False),
        ("egress.RESOLVE_TIMED_OUT", False),
    ],
)
def test_a_refused_destination_is_permanent_only_when_waiting_cannot_help(
    monkeypatch: pytest.MonkeyPatch, reason: str, permanent: bool
) -> None:
    text = getattr(notify.egress, reason.split(".")[1])

    def refuse(url: str, config: WebhookConfig) -> None:
        raise notify.EgressRefused(text)

    monkeypatch.setattr(notify, "check_destination", refuse)
    result = notify.send_webhook(URL, b"{}", {}, WebhookConfig(), 10.0)
    assert (result.ok, result.permanent, result.reason, result.status) == (
        False,
        permanent,
        text,
        None,
    )


def test_the_loop_runs_until_stopped_and_survives_a_failing_step(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    stop = threading.Event()
    steps: list[int] = []

    def step(
        engine: Engine,
        now: datetime,
        cfg: NotifySettings,
        *,
        limit: int = 10,
        stop: threading.Event | None = None,
    ) -> int:
        steps.append(1)
        if len(steps) == 1:
            raise RuntimeError(URL)
        if len(steps) == 3 and stop is not None:
            stop.set()
        return 0

    monkeypatch.setattr(notify, "deliver_due", step)
    loop = notify.DeliveryLoop(world.engine, settings(Sent()), idle_seconds=0.01, stop=stop)
    thread = loop.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(steps) == 3


def test_a_message_that_cannot_be_built_fails_with_a_fixed_reason(world: World) -> None:
    row_id = world.delivery(fmt="xml")
    sent = Sent()
    deliver_due(world.engine, T0, settings(sent), limit=10)
    row = world.row(row_id)
    assert (row.state, row.last_result) == (NotificationState.FAILED, notify.UNSENDABLE)
    assert not sent.requests


def test_an_attempt_on_a_row_whose_lease_changed_hands_sends_nothing(world: World) -> None:
    row_id = world.delivery()
    claim = notify._claim(world.engine, T0, settings(Sent()))
    assert claim is not None
    with Session(world.engine) as session:
        session.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id == row_id)
            .values(lease_owner="w2")
        )
        session.commit()
    sent = Sent()
    notify._attempt(world.engine, settings(sent), claim)
    assert not sent.requests
    assert world.row(row_id).state is NotificationState.PENDING


def test_the_loop_steps_again_at_once_after_work_and_ends_on_stop(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    results = iter([1, 1])
    stepped = threading.Event()

    def step(
        engine: Engine,
        now: datetime,
        cfg: NotifySettings,
        *,
        limit: int = 10,
        stop: threading.Event | None = None,
    ) -> int:
        try:
            return next(results)
        except StopIteration:
            stepped.set()
            return 0

    monkeypatch.setattr(notify, "deliver_due", step)
    loop = notify.DeliveryLoop(world.engine, settings(Sent()), idle_seconds=60)
    thread = loop.start()
    assert stepped.wait(10)  # two busy steps ran without the 60 s sleep in between
    loop.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_a_database_that_stays_down_is_logged_once_a_minute_without_the_url(
    monkeypatch: pytest.MonkeyPatch, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    stop = threading.Event()
    steps: list[int] = []

    def step(
        engine: Engine,
        now: object,
        cfg: NotifySettings,
        *,
        limit: int = 10,
        stop: threading.Event | None = None,
    ) -> int:
        steps.append(1)
        if len(steps) == 6 and stop is not None:
            stop.set()
        raise RuntimeError(URL + KEY)

    monkeypatch.setattr(notify, "deliver_due", step)
    loop = notify.DeliveryLoop(world.engine, settings(Sent()), idle_seconds=0.001, stop=stop)
    loop.start().join(timeout=10)
    assert len(steps) == 6
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert URL not in caplog.text and KEY not in caplog.text and "SECRET" not in caplog.text


def test_a_step_sends_at_most_its_limit(world: World) -> None:
    for _ in range(3):
        world.delivery()
    assert deliver_due(world.engine, T0, settings(Sent()), limit=2) == 2


def test_the_loop_reads_the_database_clock_and_delivers_a_due_row(world: World) -> None:
    row_id = world.delivery(due=datetime(2000, 1, 1, tzinfo=UTC))
    stop = threading.Event()
    sent = Sent()
    sent.during.append(stop.set)
    notify.DeliveryLoop(world.engine, settings(sent), idle_seconds=0.01, stop=stop).run()
    assert world.row(row_id).state is NotificationState.DELIVERED


class _QuickStop(threading.Event):
    """A stop event whose waits return at once, so thousands of failing steps take no time."""

    def wait(self, timeout: float | None = None) -> bool:
        """Return without sleeping.

        Parameters
        ----------
        timeout
            Ignored.

        Returns
        -------
        bool
            Whether the event is set.
        """
        return self.is_set()


def test_a_long_streak_of_failures_does_not_kill_the_thread_and_recovery_is_logged(
    monkeypatch: pytest.MonkeyPatch, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="lakana_worker.notify")
    stop = _QuickStop()
    steps: list[int] = []

    def step(
        engine: Engine,
        now: object,
        cfg: NotifySettings,
        *,
        limit: int = 10,
        stop: threading.Event | None = None,
    ) -> int:
        steps.append(1)
        if len(steps) <= 2000:
            raise RuntimeError(URL)
        if len(steps) == 2002 and stop is not None:
            stop.set()
        return 0

    monkeypatch.setattr(notify, "deliver_due", step)
    loop = notify.DeliveryLoop(world.engine, settings(Sent()), idle_seconds=2.0, stop=stop)
    thread = loop.start()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert len(steps) == 2002  # it stepped again after 2000 failures in a row
    assert caplog.text.count("delivering again") == 1
    assert URL not in caplog.text
    assert "upgrade this worker" in notify.NEWER_FORMAT
