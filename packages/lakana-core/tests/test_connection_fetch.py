"""`fetch_for_task`: a running task's fetch of a connection it declares, and every refusal."""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from core_test_helpers import python_task_run
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, TaskScope
from lakana_core.connections.endpoints import Endpoint
from lakana_core.connections.fetch import TaskConnection, fetch_for_task
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.connections.service import ConnectionService
from lakana_core.errors import AuthenticationError, PermissionDenied
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.identity.tokens import parse_token
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Connection,
    Environment,
    Principal,
    PrincipalType,
    Repository,
    Run,
    Secret,
    TaskRun,
    TaskStatus,
)
from lakana_core.secrets.keys import EnvKeyProvider

_KEYS = EnvKeyProvider(base64.b64encode(b"7" * 32).decode())
_DEV_PASSWORD = "dev-s3cr3t-value"
_PROD_PASSWORD = "prod-s3cr3t-value"
_DECLARED = ["crm-db", "prod-only", "gone-db", "bare-db"]
_NOT_BOUND = "only a running task can fetch a connection"
_FETCHED = AuditEventType.CONNECTION_CREDENTIAL_FETCHED
_REFUSED = AuditEventType.CONNECTION_CREDENTIAL_REFUSED


@dataclass
class World:
    """`dev` and `prod`, their connections, and a running attempt of `gold.scores` with a token.

    Parameters
    ----------
    session
        Database session.
    admin
        The principal who created everything and runs the task.
    task
        The running task run (claim 2) of the asset declaring `_DECLARED`.
    token
        Its bound token.
    """

    session: Session
    admin: PrincipalRef
    task: TaskRun
    token: str

    def principal(self) -> PrincipalRef:
        """Authenticate with the bound token, as every request does.

        Returns
        -------
        PrincipalRef
            The principal with its bound task scope.
        """
        parsed = parse_token(self.token, "lakana_task")
        assert parsed is not None
        self.session.expire_all()
        return TaskTokenService(self.session, AllowAllAuthorization()).verify(*parsed).principal

    def fetch(
        self, p: PrincipalRef, name: str, limiter: DenialAuditLimiter | None = None
    ) -> TaskConnection:
        """Fetch a connection.

        Parameters
        ----------
        p
            The caller.
        name
            The connection's name.
        limiter
            The denial limiter; a generous one when `None`.

        Returns
        -------
        TaskConnection
            What `fetch_for_task` returned.
        """
        return fetch_for_task(
            self.session, _KEYS, p, name, limiter=limiter or DenialAuditLimiter(10_000)
        )

    def events(self, event_type: AuditEventType) -> list[AuditEvent]:
        """Read the committed audit events of a type, oldest first.

        Parameters
        ----------
        event_type
            The type.

        Returns
        -------
        list of AuditEvent
            The events.
        """
        self.session.rollback()
        return list(
            self.session.execute(
                select(AuditEvent)
                .where(AuditEvent.event_type == event_type)
                .order_by(AuditEvent.occurred_at)
            ).scalars()
        )

    def all_details(self) -> str:
        """Write every audit event's detail as one JSON text.

        Returns
        -------
        str
            The details.
        """
        self.session.rollback()
        return json.dumps([e.detail for e in self.session.execute(select(AuditEvent)).scalars()])


def _create(
    service: ConnectionService,
    admin: PrincipalRef,
    environment: str,
    name: str,
    password: str | None,
) -> None:
    """Create a PostgreSQL connection, with its own new secret when a password is given.

    Parameters
    ----------
    service
        The connection service.
    admin
        Who creates it.
    environment
        Its environment.
    name
        Its name.
    password
        The password of its new secret; `None` for none.
    """
    service.create(
        admin,
        environment,
        name=name,
        kind=ConnectionKind.POSTGRESQL,
        address={"host": f"{environment}.example.com", "database": "crm"},
        settings={"user": "reader"},
        secret_values=None if password is None else {"password": password},
    )


def _attempt(world_session: Session, admin: PrincipalRef, extra: dict[str, object]) -> TaskRun:
    """Insert a running attempt (claim 2) of `gold.scores` in `dev`.

    Parameters
    ----------
    world_session
        Database session.
    admin
        The run principal.
    extra
        Manifest entry keys.

    Returns
    -------
    TaskRun
        The task run.
    """
    return python_task_run(world_session, admin.id, claim=2, extra=extra)


def _mint(session: Session, task: TaskRun) -> str:
    """Mint the attempt's bound token, as the worker does.

    Parameters
    ----------
    session
        Database session.
    task
        The task run.

    Returns
    -------
    str
        The token.
    """
    return (
        TaskTokenService(session, AllowAllAuthorization())
        .mint_for_task(task.id, task.claim_count, ttl_seconds=300)
        .token
    )


@pytest.fixture
def world(session: Session) -> World:
    """Build the world.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    World
        The world.
    """
    for name in ("dev", "prod"):
        session.add(Environment(id=uuid.uuid4(), name=name, storage_root=f"file:///srv/{name}"))
    admin = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    session.add(Principal(id=admin.id, type=admin.type, name="admin", is_active=True))
    session.commit()
    service = ConnectionService(session, AllowAllAuthorization(), _KEYS)
    _create(service, admin, "dev", "crm-db", _DEV_PASSWORD)
    _create(service, admin, "dev", "other-db", "other-s3cr3t")
    _create(service, admin, "dev", "bare-db", None)
    _create(service, admin, "prod", "crm-db", _PROD_PASSWORD)
    _create(service, admin, "prod", "prod-only", "prod-only-s3cr3t")
    task = _attempt(session, admin, {"connections": _DECLARED})
    return World(session, admin, task, _mint(session, task))


def test_a_running_task_gets_its_declared_connection_with_its_secret(world: World) -> None:
    found = world.fetch(world.principal(), "crm-db")

    assert (found.name, found.kind) == ("crm-db", ConnectionKind.POSTGRESQL)
    assert found.address == {"host": "dev.example.com", "port": 5432, "database": "crm"}
    assert found.settings["user"] == "reader"
    assert found.endpoints == (Endpoint("dev.example.com", 5432, "service"),)
    assert found.secret == {"password": _DEV_PASSWORD}
    assert _DEV_PASSWORD not in repr(found)
    (event,) = world.events(_FETCHED)  # committed: a rollback keeps it
    run = world.session.get(Run, world.task.run_id)
    assert run is not None
    repository = world.session.get(Repository, run.repository_id)
    assert repository is not None
    assert event.principal_id == world.admin.id
    assert event.securable_environment == "dev"
    assert event.detail == {
        "connection": "crm-db",
        "environment": "dev",
        "repository": repository.name,
        "asset": "gold.scores",
        "run_id": str(world.task.run_id),
        "task_run_id": str(world.task.id),
        "attempt": 2,
    }
    assert world.events(_REFUSED) == []
    assert _DEV_PASSWORD not in world.all_details()


def test_a_connection_without_a_secret_answers_none(world: World) -> None:
    found = world.fetch(world.principal(), "bare-db")
    assert found.secret is None
    (event,) = world.events(_FETCHED)
    assert event.detail["connection"] == "bare-db"
    assert "reason" not in event.detail


def test_a_secret_no_longer_bound_to_the_connection_counts_as_none(world: World) -> None:
    """`require_use`, not `secret_id`, decides: a secret moved elsewhere is never sent."""
    row = world.session.execute(
        select(Connection)
        .join(Environment, Environment.id == Connection.environment_id)
        .where(Environment.name == "dev", Connection.name == "crm-db")
    ).scalar_one()
    world.session.execute(
        update(Secret).where(Secret.id == row.secret_id).values(use_target_id=uuid.uuid4())
    )
    world.session.commit()

    found = world.fetch(world.principal(), "crm-db")

    assert found.secret is None
    (event,) = world.events(_FETCHED)
    assert event.detail["reason"] == "secret_not_bound"


def test_a_revoked_secret_counts_as_none(world: World) -> None:
    world.session.execute(
        update(Secret)
        .where(Secret.name == "crm-db-connection")
        .values(revoked_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    world.session.commit()

    assert world.fetch(world.principal(), "crm-db").secret is None


def _refused(world: World, p: PrincipalRef, name: str) -> tuple[str, dict[str, object]]:
    """Fetch, expecting a refusal; return its text and the one refusal row's detail.

    Parameters
    ----------
    world
        The world.
    p
        The caller.
    name
        The connection's name.

    Returns
    -------
    tuple
        The message and the detail.
    """
    with pytest.raises(PermissionDenied) as refused:
        world.fetch(p, name)
    (event,) = world.events(_REFUSED)
    assert world.events(_FETCHED) == []
    return str(refused.value), event.detail


def test_a_personal_token_is_refused_as_not_bound(world: World) -> None:
    message, detail = _refused(world, world.admin, "crm-db")
    assert message == _NOT_BOUND
    assert detail == {"connection": "crm-db", "reason": "not_bound"}


def test_a_rest_minted_task_token_is_refused_as_not_bound(world: World) -> None:
    p = PrincipalRef(
        id=world.admin.id,
        name="admin",
        type=PrincipalType.USER,
        task_scope=TaskScope(environment="dev", asset_scope=["*"], token_id=uuid.uuid4()),
    )
    message, detail = _refused(world, p, "crm-db")
    assert message == _NOT_BOUND
    assert detail == {"connection": "crm-db", "environment": "dev", "reason": "not_bound"}


@pytest.mark.parametrize(
    "change",
    [{"status": TaskStatus.SUCCEEDED}, {"claim_count": 3}],
    ids=["ended", "re-claimed"],
)
def test_a_token_of_an_attempt_that_is_over_never_authenticates(
    world: World, change: dict[str, object]
) -> None:
    """Authentication refuses it (401) before any fetch: no row of either kind."""
    world.session.execute(update(TaskRun).where(TaskRun.id == world.task.id).values(**change))
    world.session.commit()
    with pytest.raises(AuthenticationError):
        world.principal()
    assert world.events(_FETCHED) == [] and world.events(_REFUSED) == []


@pytest.mark.parametrize("name", ["other-db", "nope-db", "Not A Name!", ""])
def test_an_undeclared_name_is_refused_whether_or_not_it_exists(world: World, name: str) -> None:
    """The same answer for an existing, a missing and a malformed name: no existence oracle."""
    message, detail = _refused(world, world.principal(), name)
    assert message == f"connection `{name}` is not declared by asset `gold.scores`"
    assert detail["reason"] == "not_declared"
    assert detail["asset"] == "gold.scores"
    assert detail["attempt"] == 2


@pytest.mark.parametrize("name", ["gone-db", "prod-only"])
def test_a_declared_name_the_environment_lacks_is_refused_naming_only_it(
    world: World, name: str
) -> None:
    """`prod-only` exists in `prod`: the answer is about `dev` and nothing else."""
    message, detail = _refused(world, world.principal(), name)
    assert message == f"connection `{name}` is not in environment `dev`"
    assert "prod" not in message.replace(name, "")
    assert detail["reason"] == "not_in_environment"
    assert detail["environment"] == "dev"
    assert detail["task_run_id"] == str(world.task.id)


def test_an_unreadable_manifest_value_declares_nothing(world: World) -> None:
    task = _attempt(world.session, world.admin, {"connections": "crm-db"})
    token = _mint(world.session, task)
    world.token = token
    message, detail = _refused(world, world.principal(), "crm-db")
    assert message == "connection `crm-db` is not declared by asset `gold.scores`"
    assert detail["reason"] == "not_declared"


def test_a_name_with_control_characters_is_escaped_in_the_message(world: World) -> None:
    message, detail = _refused(world, world.principal(), "a\nb")
    assert message == "connection `a\\nb` is not declared by asset `gold.scores`"
    assert detail["connection"] == "a\\nb"


def test_refusals_are_recorded_through_the_denial_limiter(world: World) -> None:
    """A task asking again and again is still refused each time, but not recorded each time."""
    limiter = DenialAuditLimiter(1)
    p = world.principal()
    for _ in range(3):
        with pytest.raises(PermissionDenied):
            world.fetch(p, "nope-db", limiter)
    (event,) = world.events(_REFUSED)
    assert event.detail["reason"] == "not_declared"


def test_a_secret_of_another_environment_counts_as_none(world: World) -> None:
    """A `secret_id` that points into another environment: fail closed, no 500, no name."""
    prod_secret = world.session.execute(
        select(Secret.id)
        .join(Environment, Environment.id == Secret.environment_id)
        .where(Environment.name == "prod", Secret.name == "crm-db-connection")
    ).scalar_one()
    world.session.execute(
        update(Connection)
        .where(Connection.name == "crm-db", Connection.secret_id != prod_secret)
        .values(secret_id=prod_secret)
    )
    world.session.commit()

    found = world.fetch(world.principal(), "crm-db")

    assert found.secret is None
    (event,) = world.events(_FETCHED)
    assert event.detail["reason"] == "secret_not_bound"
    assert _PROD_PASSWORD not in world.all_details()


def test_each_reason_records_its_first_refusal_and_a_suppressed_one_is_logged(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    limiter = DenialAuditLimiter(1)
    p = world.principal()
    for name in ("nope-db", "gone-db", "nope-db"):
        with caplog.at_level(logging.INFO), pytest.raises(PermissionDenied):
            world.fetch(p, name, limiter)
    reasons = [event.detail["reason"] for event in world.events(_REFUSED)]
    assert reasons == ["not_declared", "not_in_environment"]
    (line,) = [r.getMessage() for r in caplog.records if "not recorded" in r.getMessage()]
    assert "reason=not_declared" in line
    assert "connection=nope-db" in line
    assert "asset=gold.scores" in line
    assert f"task_run_id={world.task.id}" in line


def test_a_refusal_after_suppressed_ones_records_how_many_were_not_recorded(world: World) -> None:
    """The next row a principal gets carries the count of the refusals in between."""
    now = [0.0]
    limiter = DenialAuditLimiter(1, clock=lambda: now[0])
    p = world.principal()
    for step in (0.0, 1.0, 60.0):
        now[0] = step
        with pytest.raises(PermissionDenied):
            world.fetch(p, "nope-db", limiter)
    first, later = world.events(_REFUSED)
    assert "suppressed" not in first.detail
    assert later.detail["suppressed"] == 1
    assert later.detail["reason"] == "not_declared"


def test_a_bound_attempt_whose_task_run_is_gone_is_described_without_its_run(
    world: World,
) -> None:
    """The detail names the attempt the token is bound to; the run comes from its row."""
    p = world.principal()
    assert p.task_scope is not None and p.task_scope.bound is not None
    gone = dataclasses.replace(p.task_scope.bound, task_run_id=uuid.uuid4())
    ghost = dataclasses.replace(p, task_scope=dataclasses.replace(p.task_scope, bound=gone))
    message, detail = _refused(world, ghost, "nope-db")
    assert message == "connection `nope-db` is not declared by asset `gold.scores`"
    assert "run_id" not in detail
    assert detail["task_run_id"] == str(gone.task_run_id)
