"""`ConnectionService`: who may create, change and delete a connection, and its secret (S1)."""

from __future__ import annotations

import base64
import dataclasses
import inspect
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, cast

import pytest
from core_test_helpers import deploy_manifest
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind, TaskScope
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.connections.service import (
    ConnectionService,
    ConnectionView,
    missing_connections,
)
from lakana_core.db import refuse_concurrent_change
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Connection,
    ConnectionTest,
    ConnectionTestOutcome,
    ConnectionTestState,
    Environment,
    Principal,
    PrincipalType,
    Secret,
    SecretUseKind,
)
from lakana_core.projects.package_indexes import PackageIndexService
from lakana_core.secrets.crypto import decrypt_secret_row
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.secrets.uses import SecretUse, resolve_use

_KEYS = EnvKeyProvider(base64.b64encode(b"7" * 32).decode())
_PG = {"host": "db.example.com", "database": "crm"}
_SETTINGS: Mapping[str, Any] = MappingProxyType({"user": "reader"})
_VALUES: Mapping[str, str] = MappingProxyType({"password": "s3cr3t-value"})


def _principal(session: Session, name: str) -> PrincipalRef:
    """Persist a principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    PrincipalRef
        A reference to it.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
    session.commit()
    return ref


@dataclass
class World:
    """Two environments, a principal per role that matters here, and helpers to look around.

    Parameters
    ----------
    session
        Database session.
    admin
        `ADMIN` on the instance.
    data_engineer
        `DATA_ENGINEER` on `prod` and `dev`.
    analyst
        `ANALYST` on `prod`.
    """

    session: Session
    admin: PrincipalRef
    data_engineer: PrincipalRef
    analyst: PrincipalRef

    def authz(self) -> AuditingAuthorization:
        """Build the authorization the server uses: real grants, denials audited.

        Returns
        -------
        AuditingAuthorization
            The service.
        """
        return AuditingAuthorization(
            GrantAuthorization(self.session), self.session, DenialAuditLimiter(10_000)
        )

    def connections(self) -> ConnectionService:
        """Build the service under test.

        Returns
        -------
        ConnectionService
            The service.
        """
        return ConnectionService(self.session, self.authz(), _KEYS)

    def secrets(self) -> SecretService:
        """Build the secret service on the same grants.

        Returns
        -------
        SecretService
            The service.
        """
        return SecretService(self.session, self.authz(), _KEYS)

    def make_secret(
        self, environment: str, name: str, values: dict[str, str], use: SecretUse | None = None
    ) -> Secret:
        """Create a secret as the administrator.

        Parameters
        ----------
        environment
            Its environment.
        name
            Its name.
        values
            Its values.
        use
            What it is bound to; unbound when `None`.

        Returns
        -------
        Secret
            The row.
        """
        return self.secrets().create(
            self.admin, environment=environment, name=name, values=values, use=use
        )

    def secret(self, environment: str, name: str) -> Secret:
        """Read a secret afresh.

        Parameters
        ----------
        environment
            Its environment.
        name
            Its name.

        Returns
        -------
        Secret
            The row as committed.
        """
        self.session.expire_all()
        return self.session.execute(
            select(Secret)
            .join(Environment, Environment.id == Secret.environment_id)
            .where(Environment.name == environment, Secret.name == name)
        ).scalar_one()

    def values(self, environment: str, name: str) -> dict[str, str]:
        """Decrypt a secret, as the run-time fetch will.

        Parameters
        ----------
        environment
            Its environment.
        name
            Its name.

        Returns
        -------
        dict of str to str
            Its values.
        """
        return decrypt_secret_row(
            self.secret(environment, name), _KEYS, environment_name=environment
        )

    def connection(self, environment: str, name: str) -> Connection | None:
        """Read a connection afresh.

        Parameters
        ----------
        environment
            Its environment.
        name
            Its name.

        Returns
        -------
        Connection or None
            The row, or `None` when there is none.
        """
        self.session.expire_all()
        return self.session.execute(
            select(Connection)
            .join(Environment, Environment.id == Connection.environment_id)
            .where(Environment.name == environment, Connection.name == name)
        ).scalar_one_or_none()

    def connection_id(self, environment: str, name: str) -> uuid.UUID:
        """Read a connection's id.

        Parameters
        ----------
        environment
            Its environment.
        name
            Its name.

        Returns
        -------
        uuid.UUID
            The id.
        """
        row = self.connection(environment, name)
        assert row is not None
        return row.id

    def events(self, event_type: AuditEventType | None = None) -> list[AuditEvent]:
        """Read the audit events in the order they were written.

        Parameters
        ----------
        event_type
            Only events of this type, when given.

        Returns
        -------
        list of AuditEvent
            The events.
        """
        self.session.expire_all()
        query = select(AuditEvent).order_by(AuditEvent.occurred_at)
        if event_type is not None:
            query = query.where(AuditEvent.event_type == event_type)
        return list(self.session.execute(query).scalars())

    def audit_types(self) -> list[str]:
        """Name the audit events in the order they were written.

        Returns
        -------
        list of str
            Their types.
        """
        return [event.event_type.value for event in self.events()]

    def audit_details_text(self) -> str:
        """Write every audit event's detail as one JSON text.

        Returns
        -------
        str
            The details.
        """
        return json.dumps([event.detail for event in self.events()])

    def denial_reasons(self) -> list[str | None]:
        """List the reasons of the recorded `AUTHORIZATION_DENIED` events.

        Returns
        -------
        list
            ``detail["reason"]`` of each, `None` where the grant check refused.
        """
        return [
            event.detail.get("reason") for event in self.events(AuditEventType.AUTHORIZATION_DENIED)
        ]

    def deploy_manifest(
        self, environment: str, repository: str, assets: dict[str, list[str]]
    ) -> None:
        """Make a project version whose assets declare connections the current one.

        Parameters
        ----------
        environment
            The environment.
        repository
            The repository.
        assets
            Asset to the connections it declares.
        """
        deploy_manifest(self.session, environment, repository, assets)

    def postgres(
        self,
        p: PrincipalRef,
        name: str = "crm-db",
        *,
        settings: Mapping[str, Any] = _SETTINGS,
        secret: str | None = None,
        secret_values: Mapping[str, str] | None = _VALUES,
    ) -> ConnectionView:
        """Create a PostgreSQL connection in `prod`, by default with its own new secret.

        Parameters
        ----------
        p
            Who creates it.
        name
            Its name.
        settings
            Its settings.
        secret
            The name of an existing secret to use.
        secret_values
            Values for a new secret; `None` for none.

        Returns
        -------
        ConnectionView
            The view.
        """
        return self.connections().create(
            p,
            "prod",
            name=name,
            kind=ConnectionKind.POSTGRESQL,
            address=dict(_PG),
            settings=settings,
            secret=secret,
            secret_values=secret_values,
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
    session.commit()
    admin = _principal(session, "admin")
    data_engineer = _principal(session, "dana")
    analyst = _principal(session, "alice")
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    for environment in ("prod", "dev"):
        grants.grant(
            admin,
            principal="dana",
            role=Role.DATA_ENGINEER,
            kind=SecurableKind.ENVIRONMENT,
            environment=environment,
        )
    grants.grant(
        admin,
        principal="alice",
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="prod",
    )
    return World(session, admin, data_engineer, analyst)


def test_a_data_engineer_creates_a_connection_and_its_secret(world: World) -> None:
    """MANAGE_CONNECTION alone creates the connection and a new secret born bound to it (S1)."""
    view = world.connections().create(
        world.data_engineer,
        "prod",
        name="crm-db",
        kind=ConnectionKind.POSTGRESQL,
        address={"host": "db.example.com", "database": "crm"},
        settings={"user": "reader"},
        secret_values={"password": "s3cr3t-value"},
    )

    assert view.secret == "crm-db-connection"
    assert (view.name, view.kind, view.address, view.created_by) == (
        "crm-db",
        ConnectionKind.POSTGRESQL,
        {"host": "db.example.com", "port": 5432, "database": "crm"},
        "dana",
    )
    assert view.declared_by == []
    secret = world.secret("prod", "crm-db-connection")
    assert (secret.use_kind, secret.use_target_id) == (
        SecretUseKind.CONNECTION,
        world.connection_id("prod", "crm-db"),
    )
    row = world.connection("prod", "crm-db")
    assert row is not None and row.secret_id == secret.id
    assert world.values("prod", "crm-db-connection") == {"password": "s3cr3t-value"}
    events = world.audit_types()
    assert events[-3:] == ["SECRET_CREATED", "SECRET_BOUND", "CONNECTION_CREATED"]
    created = world.events(AuditEventType.CONNECTION_CREATED)[-1]
    assert created.detail == {
        "name": "crm-db",
        "kind": "postgresql",
        "address": {"host": "db.example.com", "port": 5432, "database": "crm"},
        "secret": "crm-db-connection",
    }
    assert created.securable_environment == "prod"
    assert "s3cr3t-value" not in world.audit_details_text()


def test_a_taken_secret_name_gives_the_next_free_one(world: World) -> None:
    """A secret already called `<name>-connection` is left alone; the new one is `-2`.

    The view reports the name the new secret got.
    """
    world.make_secret("prod", "crm-db-connection", {"password": "other"})

    view = world.postgres(world.data_engineer)

    assert view.secret == "crm-db-connection-2"
    assert world.values("prod", "crm-db-connection-2") == {"password": "s3cr3t-value"}
    assert world.values("prod", "crm-db-connection") == {"password": "other"}
    assert world.secret("prod", "crm-db-connection").use_kind is None


def test_a_deleted_connection_is_created_again_with_new_values(world: World) -> None:
    """Delete, then create again with values: works, with or without a revoke in between.

    The deleted connection's secret stays (unbound, or revoked by an administrator) and keeps
    its name, so each new secret takes the next free one.
    """
    service = world.connections()
    world.postgres(world.data_engineer)
    service.delete(world.data_engineer, "prod", "crm-db")

    again = world.postgres(world.data_engineer)
    assert again.secret == "crm-db-connection-2"

    service.delete(world.data_engineer, "prod", "crm-db")
    revoked = world.secret("prod", "crm-db-connection-2")
    world.secrets().revoke(world.admin, revoked.id)
    third = world.postgres(world.data_engineer)

    assert third.secret == "crm-db-connection-3"
    assert world.secret("prod", "crm-db-connection").use_kind is None
    assert world.secret("prod", "crm-db-connection-2").revoked_at is not None


def test_when_every_secret_name_is_taken_the_refusal_says_what_works(world: World) -> None:
    """After `-100` the create is a 409 whose next steps work for a data engineer."""
    world.make_secret("prod", "crm-db-connection", {"password": "x"})
    for number in range(2, 101):
        world.make_secret("prod", f"crm-db-connection-{number}", {"password": "x"})

    with pytest.raises(AlreadyExistsError) as caught:
        world.postgres(world.data_engineer)

    assert str(caught.value) == (
        "secrets `crm-db-connection` to `crm-db-connection-100` all exist in `prod`; give the "
        "connection another name, or create it without new values and ask an administrator "
        "to bind a secret to it: `lakana secret bind NAME --env prod --connection crm-db`"
    )
    assert world.connection("prod", "crm-db") is None


def test_a_taken_connection_name_is_a_conflict(world: World) -> None:
    """A second connection of the same name in one environment is a 409 with the update command."""
    world.postgres(world.data_engineer, secret_values=None)

    with pytest.raises(AlreadyExistsError) as caught:
        world.postgres(world.data_engineer, secret_values=None)

    assert str(caught.value) == (
        "environment `prod` already has a connection `crm-db`; change it with "
        "`lakana connection update crm-db --env prod`"
    )
    world.connections().create(
        world.data_engineer,
        "dev",
        name="crm-db",
        kind=ConnectionKind.POSTGRESQL,
        address=dict(_PG),
        settings={"user": "reader"},
    )
    assert world.connection("dev", "crm-db") is not None


@pytest.mark.parametrize("name", ["CRM", "1db", "crm_db", "a" * 64, "", "crm\x00db"])
def test_a_name_outside_the_rule_is_refused(world: World, name: str) -> None:
    """Names are lower-case letters, digits and '-', starting with a letter, at most 63.

    Parameters
    ----------
    world
        The world.
    name
        A name the rule refuses.
    """
    with pytest.raises(ValidationError) as caught:
        world.postgres(world.data_engineer, name=name)

    assert str(caught.value) == (
        "connection names are lower-case letters, digits and '-', starting with a letter, at "
        "most 63"
    )


def test_a_secret_and_secret_values_together_are_refused(world: World) -> None:
    """At most one way to give the secret."""
    with pytest.raises(ValidationError) as caught:
        world.postgres(world.data_engineer, secret="crm-pw")

    assert str(caught.value) == "give either --secret or --password-stdin/--values-stdin, not both"


def test_an_analyst_cannot_create_but_can_read(world: World) -> None:
    """READ_ENVIRONMENT lists and shows; create, update, delete need MANAGE_CONNECTION."""
    world.postgres(world.admin)
    service = world.connections()

    assert [view.name for view in service.list(world.analyst, "prod").items] == ["crm-db"]
    shown = service.get(world.analyst, "prod", "crm-db")
    assert (shown.name, shown.secret) == ("crm-db", "crm-db-connection")
    with pytest.raises(PermissionDenied):
        world.postgres(world.analyst, name="other")
    with pytest.raises(PermissionDenied):
        service.update(world.analyst, "prod", "crm-db", settings={"user": "writer"})
    with pytest.raises(PermissionDenied):
        service.delete(world.analyst, "prod", "crm-db")
    with pytest.raises(PermissionDenied):
        service.get(world.analyst, "dev", "crm-db")

    denials = world.events(AuditEventType.AUTHORIZATION_DENIED)
    assert [event.detail["action"] for event in denials] == [
        "MANAGE_CONNECTION",
        "MANAGE_CONNECTION",
        "MANAGE_CONNECTION",
        "READ_ENVIRONMENT",
    ]
    assert world.connection("prod", "other") is None
    row = world.connection("prod", "crm-db")
    assert row is not None and row.settings == {"user": "reader", "tls": "verify-full"}


def test_an_unknown_environment_or_connection_is_not_found(world: World) -> None:
    """A 404 names the listing command and nothing else."""
    service = world.connections()

    with pytest.raises(NotFoundError) as caught:
        service.get(world.admin, "prod", "nope")
    assert str(caught.value) == (
        "environment `prod` has no connection `nope`; `lakana connection list --env prod` "
        "lists them"
    )
    with pytest.raises(NotFoundError):
        service.get(world.admin, "prod", "not\x00a-name")
    with pytest.raises(NotFoundError, match="No environment named 'qa'"):
        service.list(world.admin, "qa")


def test_naming_an_unbound_secret_needs_manage_secret(world: World) -> None:
    """S1: --secret of an unbound secret binds only for a MANAGE_SECRET holder.

    A data engineer is refused with `lakana secret bind <name> --env prod --connection crm-db`.
    """
    world.make_secret("prod", "crm-pw", {"password": "pw"})

    with pytest.raises(PermissionDenied) as caught:
        world.postgres(world.data_engineer, secret="crm-pw", secret_values=None)

    assert "lakana secret bind crm-pw --env prod --connection crm-db" in str(caught.value)
    assert str(caught.value).startswith(
        "Secret 'crm-pw' of environment 'prod' is not bound to connection 'crm-db'."
    )
    assert world.connection("prod", "crm-db") is None
    assert world.secret("prod", "crm-pw").use_kind is None
    assert world.denial_reasons()[-1] == "secret_not_bound_to_entry"

    view = world.postgres(world.admin, secret="crm-pw", secret_values=None)

    assert view.secret == "crm-pw"
    secret = world.secret("prod", "crm-pw")
    assert (secret.use_kind, secret.use_target_id) == (
        SecretUseKind.CONNECTION,
        world.connection_id("prod", "crm-db"),
    )
    bound = world.events(AuditEventType.SECRET_BOUND)[-1]
    assert (bound.principal_id, bound.detail["use"], bound.detail["target"]) == (
        world.admin.id,
        "connection",
        "crm-db",
    )


def test_the_refusal_says_the_bind_makes_it_the_connections_secret(world: World) -> None:
    """The advice of S1 for a connection: one `lakana secret bind`, nothing after it.

    An administrator's bind makes the secret the one the connection uses, so the refusal
    says so instead of leaving the reader to look for a second step.
    """
    world.make_secret("prod", "crm-pw", {"password": "pw"})
    world.postgres(world.data_engineer, secret_values=None)

    with pytest.raises(PermissionDenied) as caught:
        world.connections().update(world.data_engineer, "prod", "crm-db", secret="crm-pw")

    assert str(caught.value) == (
        "Secret 'crm-pw' of environment 'prod' is not bound to connection 'crm-db'. Ask "
        "someone who may manage secrets in 'prod' to bind it to this connection: lakana secret "
        "bind crm-pw --env prod --connection crm-db (the bind also makes it the secret the "
        "connection uses)"
    )


def test_a_secret_of_that_name_in_another_environment_is_neither_found_nor_bound(
    world: World,
) -> None:
    """A connection's secret lives in its own environment; another one's is never touched."""
    world.make_secret("dev", "shared", {"password": "dev-pw"})

    with pytest.raises(NotFoundError) as caught:
        world.postgres(world.admin, secret="shared", secret_values=None)
    assert str(caught.value) == (
        "environment `prod` has no secret `shared`; `lakana secret list --env prod` lists them"
    )

    world.make_secret("prod", "shared", {"password": "prod-pw"})
    world.postgres(world.admin, secret="shared", secret_values=None)

    assert world.secret("prod", "shared").use_kind is SecretUseKind.CONNECTION
    assert world.secret("dev", "shared").use_kind is None


def test_a_secret_bound_elsewhere_is_refused_to_everyone_without_manage_secret(
    world: World,
) -> None:
    """S1: a secret bound to an index cannot be named by a data engineer.

    Nor by an administrator: moving it takes an explicit `lakana secret bind`.
    """
    index = PackageIndexService(world.session, world.authz()).create(
        world.admin, name="corp", url="https://corp.example.org/simple"
    )
    world.make_secret("prod", "corp-pw", {"password": "pw"}, SecretUse.package_index(index))

    for who in (world.data_engineer, world.admin):
        with pytest.raises(PermissionDenied) as caught:
            world.postgres(who, secret="corp-pw", secret_values=None)
        assert "is bound to package index 'corp', not to connection 'crm-db'" in str(caught.value)
        assert "lakana secret bind corp-pw --env prod --connection crm-db" in str(caught.value)

    assert world.denial_reasons()[-2:] == ["secret_bound_elsewhere", "secret_bound_elsewhere"]
    assert world.connection("prod", "crm-db") is None
    secret = world.secret("prod", "corp-pw")
    assert (secret.use_kind, secret.use_target_id) == (SecretUseKind.PACKAGE_INDEX, index.id)


def test_a_secret_its_environment_uses_is_refused(world: World) -> None:
    """The storage secret is never offered to a connection."""
    world.make_secret("prod", "s3", {"password": "pw"})
    env = world.session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
    env.storage_secret_ref = "s3"
    world.session.commit()

    with pytest.raises(PermissionDenied, match="serves its environment"):
        world.postgres(world.admin, secret="s3", secret_values=None)
    assert world.denial_reasons()[-1] == "secret_used_by_environment"


def test_keeping_the_bound_secret_needs_only_manage_connection(world: World) -> None:
    """Naming the secret already bound to this connection passes for a data engineer."""
    world.postgres(world.data_engineer)
    service = world.connections()

    view = service.update(
        world.data_engineer,
        "prod",
        "crm-db",
        secret="crm-db-connection",
        settings={"user": "writer"},
    )
    assert (view.secret, view.settings["user"]) == ("crm-db-connection", "writer")

    row = world.connection("prod", "crm-db")
    assert row is not None
    # An administrator's binding makes it the connection's secret at once.
    world.make_secret("prod", "new-pw", {"password": "new"}, SecretUse.connection(row))
    view = service.update(world.data_engineer, "prod", "crm-db", secret="new-pw")

    assert view.secret == "new-pw"
    assert world.secret("prod", "new-pw").use_kind is SecretUseKind.CONNECTION
    # A connection has one bound secret: the one it named before is let go, so it can never
    # be named again without the right to manage secrets.
    assert world.secret("prod", "crm-db-connection").use_kind is None
    with pytest.raises(PermissionDenied):
        service.update(world.data_engineer, "prod", "crm-db", secret="crm-db-connection")


_MOVES: list[tuple[ConnectionKind, dict[str, Any], dict[str, Any], dict[str, str], str, Any]] = [
    (ConnectionKind.POSTGRESQL, _PG, {"user": "r"}, {"password": "p"}, "address", {"host": "x.io"}),
    (ConnectionKind.POSTGRESQL, _PG, {"user": "r"}, {"password": "p"}, "address", {"port": 6543}),
    (
        ConnectionKind.POSTGRESQL,
        _PG,
        {"user": "r"},
        {"password": "p"},
        "settings",
        {"tls": "disable"},
    ),
    (ConnectionKind.POSTGRESQL, _PG, {"user": "r"}, {"password": "p"}, "settings", "ca"),
    (
        ConnectionKind.HTTP,
        {"base_url": "https://api.example.com/v1"},
        {"auth": "bearer"},
        {"token": "t"},
        "address",
        {"base_url": "https://evil.example.net/v1"},
    ),
    (
        ConnectionKind.HTTP,
        {"base_url": "https://api.example.com/v1"},
        {"auth": "api_key", "api_key_name": "X-Key"},
        {"api_key": "k"},
        "settings",
        {"api_key_in": "query"},
    ),
    (
        ConnectionKind.HTTP,
        {"base_url": "https://api.example.com/v1"},
        {"auth": "oauth2", "client_id": "c", "token_url": "https://id.example.com/token"},
        {"client_secret": "s"},
        "settings",
        {"token_url": "https://evil.example.net/token"},
    ),
    (
        ConnectionKind.S3,
        {"bucket": "raw-data", "region": "eu-west-1"},
        {"access_key_id": "AKIA"},
        {"secret_access_key": "s"},
        "address",
        {"endpoint": "https://evil.example.net"},
    ),
    (
        ConnectionKind.S3,
        {"bucket": "raw-data", "region": "eu-west-1"},
        {"access_key_id": "AKIA"},
        {"secret_access_key": "s"},
        "address",
        {"bucket": "other-data"},
    ),
    (
        ConnectionKind.AZURE,
        {"account": "corpdata", "container": "exports"},
        {},
        {"account_key": "k"},
        "address",
        {"account": "evilcorp"},
    ),
]


@pytest.mark.parametrize(("kind", "address", "settings", "values", "part", "change"), _MOVES)
def test_an_address_change_of_a_connection_with_a_secret_needs_manage_secret(
    world: World,
    test_ca_pem: str,
    kind: ConnectionKind,
    address: dict[str, Any],
    settings: dict[str, Any],
    values: dict[str, str],
    part: str,
    change: dict[str, Any] | str,
) -> None:
    """S1: moving the address of a connection with a secret is refused for a data engineer.

    Moving host, port, base_url, endpoint, token_url, bucket or account (or weakening how the
    secret is sent) needs new secret values in the same request; an admin may.

    Parameters
    ----------
    world
        The world.
    test_ca_pem
        A CA certificate.
    kind
        The connection's kind.
    address
        Its address.
    settings
        Its settings.
    values
        Its secret's values.
    part
        ``address`` or ``settings``: what the update changes.
    change
        The change.
    """
    changes = {"ca_certificate": test_ca_pem} if change == "ca" else change
    assert isinstance(changes, dict)
    moving: dict[str, Any] = {
        "address": changes if part == "address" else None,
        "settings": changes if part == "settings" else None,
    }
    service = world.connections()
    service.create(
        world.data_engineer,
        "prod",
        name="src",
        kind=kind,
        address=address,
        settings=settings,
        secret_values=values,
    )
    before = world.connection("prod", "src")
    assert before is not None
    stored = (dict(before.address), dict(before.settings))

    with pytest.raises(PermissionDenied) as caught:
        service.update(
            world.data_engineer,
            "prod",
            "src",
            address=moving["address"],
            settings=moving["settings"],
        )

    assert str(caught.value) == (
        "This change of connection 'src' (its address, TLS, CA certificate or how it sends its "
        "secret) could send secret 'src-connection' of environment 'prod' somewhere new. That "
        "needs the right to manage secrets in 'prod' (the ADMIN role). Give the connection a new "
        "secret in the same change instead (`lakana connection update src --env prod ... "
        "--password-stdin`, or --values-stdin), or ask someone who may manage secrets in 'prod' "
        "to make the change, or to bind the secret elsewhere first: lakana secret bind "
        "src-connection --env prod (with --index NAME, --repository NAME, --connection NAME or "
        "--own-env)"
    )
    assert world.denial_reasons()[-1] == "url_moves_bound_secret"
    after = world.connection("prod", "src")
    assert after is not None and (after.address, after.settings) == stored

    view = service.update(
        world.admin, "prod", "src", address=moving["address"], settings=moving["settings"]
    )
    for key, value in changes.items():
        if key != "ca_certificate":
            assert getattr(view, part)[key] == value


def test_a_change_that_moves_nothing_needs_no_manage_secret(world: World) -> None:
    """The database or the user go nowhere new; a connection without a secret moves freely."""
    world.postgres(world.data_engineer)
    service = world.connections()

    view = service.update(
        world.data_engineer,
        "prod",
        "crm-db",
        address={"database": "crm2"},
        settings={"user": "other"},
    )
    assert (view.address["database"], view.settings["user"]) == ("crm2", "other")

    world.postgres(world.data_engineer, name="plain", secret_values=None)
    moved = service.update(world.data_engineer, "prod", "plain", address={"host": "x.example.org"})
    assert moved.address["host"] == "x.example.org"


def test_new_values_on_update_rotate_the_bound_secret(world: World) -> None:
    """--password-stdin on update rotates the connection's own secret (SECRET_ROTATED).

    So the address may move in the same request.
    """
    world.postgres(world.data_engineer)
    before = world.secret("prod", "crm-db-connection").id

    view = world.connections().update(
        world.data_engineer,
        "prod",
        "crm-db",
        address={"host": "new.example.com"},
        secret_values={"password": "rotated-value"},
    )

    assert (view.address["host"], view.secret) == ("new.example.com", "crm-db-connection")
    assert world.secret("prod", "crm-db-connection").id == before
    assert world.values("prod", "crm-db-connection") == {"password": "rotated-value"}
    assert world.audit_types()[-2:] == ["SECRET_ROTATED", "CONNECTION_UPDATED"]
    updated = world.events(AuditEventType.CONNECTION_UPDATED)[-1]
    assert updated.detail == {
        "name": "crm-db",
        "changed": ["address.host", "secret"],
        "address": {"host": "new.example.com", "port": 5432, "database": "crm"},
        "secret": "crm-db-connection",
    }
    assert "rotated-value" not in world.audit_details_text()


def test_new_values_never_rotate_a_secret_that_was_moved_elsewhere(world: World) -> None:
    """A secret an administrator bound elsewhere is no longer the connection's to rotate."""
    world.postgres(world.data_engineer)
    index = PackageIndexService(world.session, world.authz()).create(
        world.admin, name="corp", url="https://corp.example.org/simple"
    )
    moved = world.secret("prod", "crm-db-connection")
    world.secrets().bind(world.admin, moved.id, SecretUse.package_index(index))

    view = world.connections().update(
        world.data_engineer, "prod", "crm-db", secret_values={"password": "mine"}
    )

    assert view.secret == "crm-db-connection-2"
    assert world.values("prod", "crm-db-connection-2") == {"password": "mine"}
    assert world.values("prod", "crm-db-connection") == {"password": "s3cr3t-value"}
    assert world.secret("prod", "crm-db-connection").use_kind is SecretUseKind.PACKAGE_INDEX


def test_new_values_give_a_connection_without_a_secret_its_own(world: World) -> None:
    """Values on update create `<name>-connection`, born bound, as on create."""
    world.postgres(world.data_engineer, secret_values=None)

    view = world.connections().update(
        world.data_engineer, "prod", "crm-db", secret_values={"password": "first"}
    )

    assert view.secret == "crm-db-connection"
    assert world.secret("prod", "crm-db-connection").use_kind is SecretUseKind.CONNECTION
    assert world.values("prod", "crm-db-connection") == {"password": "first"}


def test_the_secret_values_are_checked_for_the_kind(world: World) -> None:
    """A key the kind does not take is refused before anything is stored."""
    with pytest.raises(ValidationError, match="token is not a secret key of kind postgresql"):
        world.postgres(world.data_engineer, secret_values={"token": "t"})
    assert world.connection("prod", "crm-db") is None
    with pytest.raises(ValidationError, match="kind directory takes no secret"):
        world.connections().create(
            world.admin,
            "prod",
            name="exports",
            kind=ConnectionKind.DIRECTORY,
            address={"root": "shared", "path": "exports"},
            settings={},
            secret="anything",
        )


def test_name_and_kind_are_immutable(world: World) -> None:
    """ConnectionUpdate carries neither; the service has no way to change them."""
    parameters = set(inspect.signature(ConnectionService.update).parameters)

    assert parameters == {
        "self",
        "p",
        "environment",
        "name",
        "address",
        "settings",
        "secret",
        "secret_values",
        "clear",
    }
    world.postgres(world.data_engineer, secret_values=None)
    with pytest.raises(ValidationError, match=r"address\.kind is not a field"):
        world.connections().update(world.data_engineer, "prod", "crm-db", address={"kind": "http"})
    row = world.connection("prod", "crm-db")
    assert row is not None and (row.name, row.kind) == ("crm-db", ConnectionKind.POSTGRESQL)


def test_clear_removes_a_setting_and_the_secret(world: World, test_ca_pem: str) -> None:
    """clear=["ca_certificate"] drops the setting; clear=["secret"] leaves the secret unbound."""
    world.postgres(world.data_engineer, settings={"user": "r", "ca_certificate": test_ca_pem})
    service = world.connections()

    view = service.update(world.data_engineer, "prod", "crm-db", clear=["secret"])

    assert view.secret is None
    secret = world.secret("prod", "crm-db-connection")
    assert (secret.use_kind, secret.use_target_id, secret.revoked_at) == (None, None, None)
    unbound = world.events(AuditEventType.SECRET_BOUND)[-1]
    assert (unbound.detail["use"], unbound.detail["previous"]) == (None, "connection")

    view = service.update(world.data_engineer, "prod", "crm-db", clear=["ca_certificate"])

    assert "ca_certificate" not in view.settings
    row = world.connection("prod", "crm-db")
    assert row is not None and "ca_certificate" not in row.settings and row.secret_id is None
    with pytest.raises(ValidationError, match="nothing to clear called `nope`"):
        service.update(world.data_engineer, "prod", "crm-db", clear=["nope"])
    with pytest.raises(ValidationError, match="Nothing to change"):
        service.update(world.data_engineer, "prod", "crm-db")


def test_a_null_value_removes_a_key(world: World, test_ca_pem: str) -> None:
    """A key given as None is removed, as JSON null is in a PATCH."""
    world.postgres(world.admin, settings={"user": "r", "ca_certificate": test_ca_pem})

    view = world.connections().update(
        world.admin, "prod", "crm-db", settings={"ca_certificate": None}
    )

    assert "ca_certificate" not in view.settings


def test_http_addresses_are_refused_at_create_and_update(world: World) -> None:
    """S3: an http:// base_url or endpoint is a 422 naming the field, both ways."""
    service = world.connections()

    with pytest.raises(ValidationError, match=r"address\.base_url"):
        service.create(
            world.data_engineer,
            "prod",
            name="api",
            kind=ConnectionKind.HTTP,
            address={"base_url": "http://api.example.com"},
            settings={},
        )
    with pytest.raises(ValidationError, match=r"address\.endpoint"):
        service.create(
            world.data_engineer,
            "prod",
            name="lake",
            kind=ConnectionKind.S3,
            address={"bucket": "raw-data", "region": "eu-west-1", "endpoint": "http://s3.local"},
            settings={"access_key_id": "AKIA"},
        )
    assert world.connection("prod", "api") is None
    service.create(
        world.data_engineer,
        "prod",
        name="api",
        kind=ConnectionKind.HTTP,
        address={"base_url": "https://api.example.com"},
        settings={},
    )
    with pytest.raises(ValidationError, match=r"address\.base_url"):
        service.update(
            world.data_engineer, "prod", "api", address={"base_url": "http://api.example.com"}
        )
    row = world.connection("prod", "api")
    assert row is not None and row.address == {"base_url": "https://api.example.com"}


def test_no_response_or_audit_row_holds_a_secret_value(
    world: World,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    test_ca_pem: str,
) -> None:
    """create, update, get, list and the audit rows never contain the value or the CA text.

    Parameters
    ----------
    world
        The world.
    caplog
        Captures log records.
    capfd
        Captures the process's output.
    test_ca_pem
        A CA certificate.
    """
    caplog.set_level(logging.DEBUG)
    service = world.connections()
    body = test_ca_pem.splitlines()[1]
    views = [
        world.postgres(world.admin, settings={"user": "r", "ca_certificate": test_ca_pem}),
        service.update(world.admin, "prod", "crm-db", secret_values={"password": "second-value"}),
        service.get(world.admin, "prod", "crm-db"),
        *service.list(world.admin, "prod").items,
    ]

    for view in views:
        assert view.settings["ca_certificate"].startswith("sha256:")
        for text in (repr(view), str(view)):
            for leaked in ("s3cr3t-value", "second-value", body):
                assert leaked not in text
    out, err = capfd.readouterr()
    for text in (world.audit_details_text(), caplog.text, out, err):
        for leaked in ("s3cr3t-value", "second-value", body):
            assert leaked not in text


def test_delete_is_refused_while_a_deployed_version_declares_it(world: World) -> None:
    """Delete is a 409 naming the first declaring asset; after a deploy without it, it succeeds.

    "connection `crm-db` is used by `analytics: customers` in `prod`; deploy a version without
    it first."
    """
    world.postgres(world.data_engineer)
    service = world.connections()
    world.deploy_manifest("prod", "analytics", {"customers": ["crm-db"], "orders": []})
    world.deploy_manifest("dev", "analytics", {"customers": []})

    assert service.get(world.analyst, "prod", "crm-db").declared_by == [("analytics", "customers")]
    with pytest.raises(ConflictError) as caught:
        service.delete(world.data_engineer, "prod", "crm-db")
    assert str(caught.value) == (
        "connection `crm-db` is used by `analytics: customers` in `prod`; deploy a version "
        "without it first."
    )

    world.deploy_manifest("prod", "billing", {"invoices": ["crm-db"], "payments": ["crm-db"]})
    with pytest.raises(ConflictError) as caught:
        service.delete(world.data_engineer, "prod", "crm-db")
    assert str(caught.value) == (
        "connection `crm-db` is used by `analytics: customers` and 2 more in `prod`; deploy a "
        "version without it first."
    )
    assert world.connection("prod", "crm-db") is not None

    world.deploy_manifest("prod", "analytics", {"customers": []})
    world.deploy_manifest("prod", "billing", {"invoices": []})
    service.delete(world.data_engineer, "prod", "crm-db")

    assert world.connection("prod", "crm-db") is None
    secret = world.secret("prod", "crm-db-connection")
    assert (secret.use_kind, secret.use_target_id) == (None, None)
    assert world.audit_types()[-2:] == ["SECRET_BOUND", "CONNECTION_DELETED"]
    assert world.events(AuditEventType.CONNECTION_DELETED)[-1].detail == {
        "name": "crm-db",
        "kind": "postgresql",
    }


def test_declared_by_reads_an_unusual_manifest_without_failing(world: World) -> None:
    """A malformed Python entry declares every name it spells; a delete is refused, not let through.

    Entries that are not Python assets, or name no string, declare nothing.
    """
    world.postgres(world.admin, secret_values=None)
    version = deploy_manifest(world.session, "prod", "analytics", {"customers": ["crm-db"]})
    version.manifest_json = {
        "assets": [
            "junk",
            {"table": "a", "kind": "sql", "connections": ["crm-db"]},
            {"table": "b", "kind": "python", "connections": "crm-db"},
            {"table": "c", "kind": "python", "connections": [1, None]},
            {"table": 7, "kind": "python", "connections": ["crm-db"]},
            {"table": "d", "kind": "python", "connections": ["crm-db"]},
            {"table": "e", "kind": "python", "connections": ["crm-db", "crm-db", 3]},
            {"table": "f", "kind": "python", "connections": {"crm-db": 1}},
        ]
    }
    world.session.commit()

    env_id = world.session.execute(
        select(Environment.id).where(Environment.name == "prod")
    ).scalar_one()
    assert world.connections().declared_by(env_id, "crm-db") == [
        ("analytics", "b"),
        ("analytics", "d"),
        ("analytics", "e"),
    ]


def test_a_malformed_entry_that_names_a_connection_refuses_its_delete(world: World) -> None:
    world.postgres(world.admin, secret_values=None)
    version = deploy_manifest(world.session, "prod", "analytics", {"customers": []})
    version.manifest_json = {
        "assets": [{"table": "customers", "kind": "python", "connections": "crm-db"}]
    }
    world.session.commit()
    with pytest.raises(ConflictError) as info:
        world.connections().delete(world.admin, "prod", "crm-db")
    assert str(info.value) == (
        "connection `crm-db` is used by `analytics: customers` in `prod`; "
        "deploy a version without it first."
    )


def test_list_is_paged(world: World) -> None:
    """`limit` and `cursor` follow "Paginated list endpoints"."""
    for name in ("first", "second", "third"):
        world.postgres(world.data_engineer, name=name, secret_values=None)
    world.connections().create(
        world.data_engineer,
        "dev",
        name="elsewhere",
        kind=ConnectionKind.POSTGRESQL,
        address=dict(_PG),
        settings={"user": "r"},
    )
    service = world.connections()

    page = service.list(world.analyst, "prod", limit=2)
    assert [view.name for view in page.items] == ["third", "second"]
    assert page.next_cursor is not None
    rest = service.list(world.analyst, "prod", limit=2, cursor=page.next_cursor)
    assert [view.name for view in rest.items] == ["first"]
    assert rest.next_cursor is None
    with pytest.raises(ValidationError):
        service.list(world.analyst, "prod", limit=0)
    with pytest.raises(ValidationError):
        service.list(world.analyst, "prod", cursor="not-a-cursor")


def test_missing_connections_names_what_an_environment_lacks(world: World) -> None:
    """In the order given; the lock is a no-op on SQLite and changes nothing here."""
    world.postgres(world.admin, secret_values=None)
    env_id = world.session.execute(
        select(Environment.id).where(Environment.name == "prod")
    ).scalar_one()

    for lock in (False, True):
        assert missing_connections(
            world.session, env_id, ["zeta", "crm-db", "alpha"], lock=lock
        ) == ["zeta", "alpha"]
    assert missing_connections(world.session, env_id, [], lock=True) == []


def test_a_secret_manager_binds_a_secret_to_a_connection_of_its_own_environment_only(
    world: World,
) -> None:
    """`lakana secret bind --connection` finds the connection in the secret's environment."""
    world.postgres(world.admin, secret_values=None)
    secret = world.make_secret("dev", "pw", {"password": "p"})
    service = world.secrets()

    with pytest.raises(NotFoundError) as caught:
        service.use_from_names(world.admin, "dev", SecretUseKind.CONNECTION, "crm-db")
    assert str(caught.value) == (
        "No connection named 'crm-db' in environment 'dev'; `lakana connection list --env dev` "
        "shows them."
    )
    prod_row = world.connection("prod", "crm-db")
    assert prod_row is not None
    with pytest.raises(ValidationError, match="belongs to environment 'prod'"):
        service.bind(world.admin, secret.id, SecretUse.connection(prod_row))
    assert world.secret("dev", "pw").use_kind is None

    use = service.use_from_names(world.admin, "prod", SecretUseKind.CONNECTION, "crm-db")
    prod_secret = world.make_secret("prod", "pw", {"password": "p"}, use)
    assert (prod_secret.use_kind, prod_secret.use_target_id) == (
        SecretUseKind.CONNECTION,
        prod_row.id,
    )
    assert service.use_target_name(prod_secret) == "crm-db"
    assert service.use_target_url(prod_secret) == "db.example.com:5432"
    with pytest.raises(ValidationError, match="Name the connection"):
        resolve_use(
            world.session, SecretUseKind.CONNECTION, None, environment_id=prod_row.environment_id
        )


def test_new_values_let_go_of_any_other_secret_bound_to_the_connection(world: World) -> None:
    """After a rotation only the rotated secret is bound, so an old one cannot be kept again."""
    world.postgres(world.data_engineer)
    row = world.connection("prod", "crm-db")
    assert row is not None
    # A second bound secret cannot be made through the services any more; a row written by
    # an earlier release or by hand is let go all the same.
    old = world.make_secret("prod", "old-pw", {"password": "old"})
    old.use_kind, old.use_target_id = SecretUseKind.CONNECTION, row.id
    world.session.commit()

    world.connections().update(
        world.data_engineer,
        "prod",
        "crm-db",
        address={"host": "new.example.com"},
        secret_values={"password": "rotated"},
    )

    assert world.secret("prod", "old-pw").use_kind is None
    assert world.secret("prod", "crm-db-connection").use_kind is SecretUseKind.CONNECTION
    with pytest.raises(PermissionDenied):
        world.connections().update(world.data_engineer, "prod", "crm-db", secret="old-pw")


def test_a_revoked_secret_cannot_be_named(world: World) -> None:
    """A revoked secret is not found, for an administrator too."""
    secret = world.make_secret("prod", "gone-pw", {"password": "p"})
    world.secrets().revoke(world.admin, secret.id)

    with pytest.raises(NotFoundError, match="has no secret `gone-pw`"):
        world.postgres(world.admin, secret="gone-pw", secret_values=None)
    assert world.connection("prod", "crm-db") is None


def test_settings_that_take_no_secret_need_the_secret_cleared(world: World) -> None:
    """An http connection moved to auth none cannot keep a secret it would never send."""
    service = world.connections()
    service.create(
        world.admin,
        "prod",
        name="api",
        kind=ConnectionKind.HTTP,
        address={"base_url": "https://api.example.com"},
        settings={"auth": "bearer"},
        secret_values={"token": "t0ken-value"},
    )

    with pytest.raises(ValidationError, match="would use a secret its settings do not take"):
        service.update(world.admin, "prod", "api", settings={"auth": "none"})
    row = world.connection("prod", "api")
    assert row is not None and row.settings == {"auth": "bearer"}

    view = service.update(world.admin, "prod", "api", settings={"auth": "none"}, clear=["secret"])
    assert (view.settings, view.secret) == ({"auth": "none"}, None)


def test_a_create_that_loses_a_race_for_the_name_is_a_conflict(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database refuses the second row at the flush; the answer is the 409, not a crash.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Hides the first connection from the early check, as a concurrent create would.
    """
    world.postgres(world.data_engineer, secret_values=None)
    original = ConnectionService._exists
    calls: list[str] = []

    def hidden_once(self: ConnectionService, env: Environment, name: str) -> bool:
        """Answer `False` the first time, then look.

        Parameters
        ----------
        self
            The service.
        env
            The environment.
        name
            The connection's name.

        Returns
        -------
        bool
            `False` first, the truth afterwards.
        """
        calls.append(name)
        return False if len(calls) == 1 else original(self, env, name)

    monkeypatch.setattr(ConnectionService, "_exists", hidden_once)

    with pytest.raises(AlreadyExistsError) as caught:
        world.postgres(world.data_engineer)

    assert str(caught.value) == (
        "environment `prod` already has a connection `crm-db`; change it with "
        "`lakana connection update crm-db --env prod`"
    )
    monkeypatch.undo()
    row = world.connection("prod", "crm-db")
    assert row is not None and row.secret_id is None
    assert world.audit_types().count("CONNECTION_CREATED") == 1


def test_a_secret_that_loses_a_race_for_its_name_is_a_conflict(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `<name>-connection` created at the same moment is refused at the flush; try again.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Hides the secret from the early check, once, as a concurrent create would.
    """
    world.make_secret("prod", "crm-db-connection", {"password": "theirs"})
    original = ConnectionService._secret_named
    calls: list[str] = []

    def hidden_once(self: ConnectionService, env: Environment, secret: str) -> Secret | None:
        """Answer `None` the first time, then look.

        Parameters
        ----------
        self
            The service.
        env
            The environment.
        secret
            The name.

        Returns
        -------
        Secret or None
            `None` first, the row afterwards.
        """
        calls.append(secret)
        return None if len(calls) == 1 else original(self, env, secret)

    monkeypatch.setattr(ConnectionService, "_secret_named", hidden_once)

    for attempt in ("create", "update"):
        calls.clear()
        with pytest.raises(ConflictError) as caught:
            if attempt == "create":
                world.postgres(world.data_engineer)
            else:
                world.connections().update(
                    world.data_engineer, "prod", "crm-db", secret_values={"password": "mine"}
                )
        assert str(caught.value) == (
            "secret `crm-db-connection` was created in `prod` at the same time; try again"
        )
        if attempt == "create":
            assert world.connection("prod", "crm-db") is None
            world.postgres(world.data_engineer, secret_values=None)
    row = world.connection("prod", "crm-db")
    assert row is not None and row.secret_id is None
    assert world.values("prod", "crm-db-connection") == {"password": "theirs"}


def test_an_administrator_binding_makes_the_secret_the_connections_own(world: World) -> None:
    """`lakana secret bind X --connection crm-db` needs no second step, and lets go of the old."""
    world.postgres(world.data_engineer)
    row = world.connection("prod", "crm-db")
    assert row is not None
    service = world.secrets()

    created = world.make_secret(
        "prod",
        "x-pw",
        {"password": "x"},
        service.use_from_names(world.admin, "prod", SecretUseKind.CONNECTION, "crm-db"),
    )
    assert world.connections().get(world.analyst, "prod", "crm-db").secret == "x-pw"
    assert world.secret("prod", "crm-db-connection").use_kind is None

    other = world.make_secret("prod", "y-pw", {"password": "y"})
    service.bind(world.admin, other.id, SecretUse.connection(row))

    assert world.connections().get(world.analyst, "prod", "crm-db").secret == "y-pw"
    assert world.secret("prod", "x-pw").use_kind is None
    assert world.secret("prod", "y-pw").use_target_id == row.id
    bound = world.session.execute(
        select(Secret.name).where(
            Secret.use_kind == SecretUseKind.CONNECTION, Secret.use_target_id == row.id
        )
    ).scalars()
    assert list(bound) == ["y-pw"]
    assert created.id != other.id


def test_clearing_a_secret_the_connection_does_not_have_is_refused(world: World) -> None:
    """`clear=["secret"]` names something the connection does not have."""
    world.postgres(world.data_engineer, secret_values=None)

    with pytest.raises(ValidationError) as caught:
        world.connections().update(world.data_engineer, "prod", "crm-db", clear=["secret"])

    assert str(caught.value) == "connection `crm-db` has no secret to clear"


def test_a_grant_in_one_environment_manages_no_connection_of_another(world: World) -> None:
    """MANAGE_CONNECTION on `dev` creates, changes and deletes nothing in `prod`."""
    world.postgres(world.admin, secret_values=None)
    erin = _principal(world.session, "erin")
    GrantService(world.session, AllowAllAuthorization()).grant(
        world.admin,
        principal="erin",
        role=Role.DATA_ENGINEER,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    service = world.connections()

    with pytest.raises(PermissionDenied):
        world.postgres(erin, name="other", secret_values=None)
    with pytest.raises(PermissionDenied):
        service.update(erin, "prod", "crm-db", address={"host": "evil.example.net"})
    with pytest.raises(PermissionDenied):
        service.delete(erin, "prod", "crm-db")

    row = world.connection("prod", "crm-db")
    assert row is not None and row.address["host"] == "db.example.com"
    assert world.connection("prod", "other") is None
    service.create(
        erin,
        "dev",
        name="crm-db",
        kind=ConnectionKind.POSTGRESQL,
        address=dict(_PG),
        settings={"user": "r"},
    )
    assert world.connection("dev", "crm-db") is not None


def test_moving_a_secret_to_another_connection_leaves_the_first_without_one(
    world: World,
) -> None:
    """A secret bound to B is no longer A's: A shows none and its address moves freely."""
    world.postgres(world.data_engineer)
    world.postgres(world.data_engineer, name="other", secret_values=None)
    first, second = world.connection("prod", "crm-db"), world.connection("prod", "other")
    assert first is not None and second is not None
    moved = world.secret("prod", "crm-db-connection")

    world.secrets().bind(world.admin, moved.id, SecretUse.connection(second))

    service = world.connections()
    assert service.get(world.analyst, "prod", "crm-db").secret is None
    assert service.get(world.analyst, "prod", "other").secret == "crm-db-connection"
    row = world.connection("prod", "crm-db")
    assert row is not None and row.secret_id is None
    view = service.update(world.data_engineer, "prod", "crm-db", address={"host": "x.example.org"})
    assert (view.address["host"], view.secret) == ("x.example.org", None)


def test_moving_a_secret_to_an_index_leaves_the_connection_without_one(world: World) -> None:
    """Any new use of the secret takes it from the connection that used it."""
    world.postgres(world.data_engineer)
    index = PackageIndexService(world.session, world.authz()).create(
        world.admin, name="corp", url="https://corp.example.org/simple"
    )
    moved = world.secret("prod", "crm-db-connection")

    world.secrets().bind(world.admin, moved.id, SecretUse.package_index(index))

    row = world.connection("prod", "crm-db")
    assert row is not None and row.secret_id is None


def test_a_secret_id_that_disagrees_with_the_binding_counts_as_no_secret(world: World) -> None:
    """A row pointing at a secret bound elsewhere (written by hand) shows and uses no secret."""
    world.postgres(world.data_engineer, secret_values=None)
    stray = world.make_secret("prod", "stray", {"password": "p"})
    row = world.connection("prod", "crm-db")
    assert row is not None
    row.secret_id = stray.id
    world.session.commit()
    service = world.connections()

    assert service.get(world.analyst, "prod", "crm-db").secret is None
    with pytest.raises(ValidationError, match="has no secret to clear"):
        service.update(world.data_engineer, "prod", "crm-db", clear=["secret"])
    view = service.update(world.data_engineer, "prod", "crm-db", address={"host": "x.example.org"})
    assert view.secret is None
    assert world.secret("prod", "stray").use_kind is None


def test_an_administrator_cannot_give_a_secret_to_a_connection_that_takes_none(
    world: World,
) -> None:
    """Create and bind refuse a secret the connection's kind or settings never send."""
    service = world.connections()
    service.create(
        world.admin,
        "prod",
        name="exports",
        kind=ConnectionKind.DIRECTORY,
        address={"root": "shared", "path": "exports"},
        settings={},
    )
    service.create(
        world.admin,
        "prod",
        name="api",
        kind=ConnectionKind.HTTP,
        address={"base_url": "https://api.example.com"},
        settings={},
    )
    world.postgres(world.admin, secret_values=None)
    secrets = world.secrets()

    with pytest.raises(ValidationError) as caught:
        world.make_secret(
            "prod",
            "dir-pw",
            {"password": "p"},
            secrets.use_from_names(world.admin, "prod", SecretUseKind.CONNECTION, "exports"),
        )
    assert str(caught.value) == (
        "Connection 'exports' (kind directory) takes no secret, so secret 'dir-pw' cannot be "
        "bound to it. Bind the secret elsewhere, or change the connection's settings so that "
        "it sends one (for http: settings.auth)."
    )
    unbound = world.make_secret("prod", "api-pw", {"token": "t"})
    api = world.connection("prod", "api")
    assert api is not None
    with pytest.raises(ValidationError, match="Connection 'api' \\(kind http\\) takes no secret"):
        secrets.bind(world.admin, unbound.id, SecretUse.connection(api))
    with pytest.raises(ValidationError, match="token is not a secret key of kind postgresql"):
        world.make_secret(
            "prod",
            "pg-pw",
            {"token": "t"},
            secrets.use_from_names(world.admin, "prod", SecretUseKind.CONNECTION, "crm-db"),
        )
    assert world.secret("prod", "api-pw").use_kind is None


class _PgError(Exception):
    """A driver error carrying a SQLSTATE, as pg8000 (in `args[0]["C"]`) or psycopg does."""


class _PsycopgError(Exception):
    """A driver error carrying its SQLSTATE as an attribute, as psycopg does.

    Parameters
    ----------
    pgcode
        The SQLSTATE.
    """

    def __init__(self, pgcode: str) -> None:
        super().__init__("could not serialize access")
        self.pgcode = pgcode


@pytest.mark.parametrize(
    ("orig", "conflict"),
    [
        (_PgError({"C": "40P01", "M": "deadlock detected"}), True),
        (_PgError({"C": "40001"}), True),
        (_PgError({"C": "23505"}), False),
        (_PgError("no state"), False),
        (_PsycopgError("40001"), True),
        (_PsycopgError("23505"), False),
    ],
)
def test_a_deadlock_or_serialization_failure_is_a_conflict_to_retry(
    session: Session, orig: Exception, conflict: bool
) -> None:
    """Two administrators swapping secrets may deadlock; the caller is told to try again.

    Parameters
    ----------
    session
        Database session.
    orig
        The driver's error.
    conflict
        Whether it is a concurrent change.
    """
    error = OperationalError("SELECT 1", {}, orig)

    def fail() -> None:
        """Raise the database error.

        Raises
        ------
        OperationalError
            Always.
        """
        raise error

    if conflict:
        with (
            pytest.raises(ConflictError) as caught,
            refuse_concurrent_change(session, "secret 's'"),
        ):
            fail()
        assert str(caught.value) == "secret 's' changed at the same time; try again."
    else:
        with pytest.raises(OperationalError), refuse_concurrent_change(session, "secret 's'"):
            fail()


def test_a_bind_that_deadlocks_is_a_conflict(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The secret service maps the deadlock and keeps nothing.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Makes the commit fail as a deadlock would.
    """
    world.postgres(world.admin, secret_values=None)
    row = world.connection("prod", "crm-db")
    assert row is not None
    secret = world.make_secret("prod", "pw", {"password": "p"})

    def deadlock() -> None:
        """Fail as Postgres does when it breaks a deadlock.

        Raises
        ------
        OperationalError
            Always.
        """
        raise OperationalError("COMMIT", {}, _PgError({"C": "40P01"}))

    monkeypatch.setattr(world.session, "commit", deadlock)
    with pytest.raises(ConflictError, match="changed at the same time; try again"):
        world.secrets().bind(world.admin, secret.id, SecretUse.connection(row))
    monkeypatch.undo()
    assert world.secret("prod", "pw").use_kind is None


def test_binding_names_every_host_the_secret_goes_to(world: World) -> None:
    """An oauth2 client secret goes to the token URL's host too: the bind shows and records both.

    `use_target_url` (the secret listing, `lakana secret bind`'s output) and the SECRET_BOUND
    event name every endpoint of the connection, not only the API's.
    """
    world.connections().create(
        world.admin,
        "prod",
        name="api",
        kind=ConnectionKind.HTTP,
        address={"base_url": "https://api.example.com/v1"},
        settings={
            "auth": "oauth2",
            "client_id": "lakana",
            "token_url": "https://login.example.org/oauth/token",
        },
    )
    row = world.connection("prod", "api")
    assert row is not None
    service = world.secrets()
    secret = world.make_secret("prod", "api-client", {"client_secret": "c"})

    bound = service.bind(world.admin, secret.id, SecretUse.connection(row))

    hosts = "api.example.com:443, login.example.org:443"
    assert service.use_target_url(bound) == hosts
    assert world.events(AuditEventType.SECRET_BOUND)[-1].detail["url"] == hosts


def test_a_tcp_secret_names_its_host(world: World) -> None:
    """A tcp connection's secret goes to its one host and port."""
    world.connections().create(
        world.admin,
        "prod",
        name="exports",
        kind=ConnectionKind.TCP,
        address={"host": "db.example.com", "port": 1521},
        settings={},
        secret_values={"password": "pw"},
    )
    secret = world.secret("prod", "exports-connection")
    assert world.secrets().use_target_url(secret) == "db.example.com:1521"


def test_a_task_token_cannot_list_or_show_connections(world: World) -> None:
    """A principal acting with a task token is refused, naming no connection."""
    world.postgres(world.data_engineer)
    task = dataclasses.replace(
        world.data_engineer,
        task_scope=TaskScope(environment="prod", asset_scope=[], token_id=uuid.uuid4()),
    )
    service = world.connections()

    for call in (lambda: service.list(task, "prod"), lambda: service.get(task, "prod", "crm-db")):
        with pytest.raises(PermissionDenied) as caught:
            call()
        assert "crm-db" not in str(caught.value)
        assert str(caught.value) == (
            "a task token cannot list or show connections; read them with your own token"
        )


def test_an_update_gives_the_secret_one_way_only(world: World) -> None:
    """A named secret and new values together, or a cleared secret with a new one, are refused."""
    world.postgres(world.data_engineer)
    service = world.connections()

    with pytest.raises(ValidationError) as both:
        service.update(
            world.data_engineer,
            "prod",
            "crm-db",
            secret="crm-pw",
            secret_values={"password": "pw"},
        )
    with pytest.raises(ValidationError) as clear_and_new:
        service.update(
            world.data_engineer,
            "prod",
            "crm-db",
            secret_values={"password": "pw"},
            clear=["secret"],
        )

    assert str(both.value) == "give either --secret or --password-stdin/--values-stdin, not both"
    assert str(clear_and_new.value) == (
        "give a new secret or clear the secret, not both in the same change"
    )
    assert world.connections().get(world.analyst, "prod", "crm-db").secret == "crm-db-connection"


def test_a_commit_refused_for_another_reason_is_a_conflict_without_its_statement(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit the database refuses, with no name taken, is a 409 to retry; nothing is kept.

    The database's text, which holds the statement and its parameters, never reaches the
    caller.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Makes the commit fail once.
    """
    original = world.session.commit
    calls: list[int] = []

    def refused_once() -> None:
        """Refuse the first commit as the database would.

        Raises
        ------
        IntegrityError
            The first time.
        """
        calls.append(1)
        if len(calls) == 1:
            raise IntegrityError("INSERT INTO audit_events ... s3cr3t-value", {}, Exception("x"))
        original()

    monkeypatch.setattr(world.session, "commit", refused_once)

    with pytest.raises(ConflictError) as caught:
        world.postgres(world.data_engineer)

    assert str(caught.value) == (
        "Could not save connection `crm-db`: the write failed for a reason other than a name "
        "collision. Try again."
    )
    assert world.connection("prod", "crm-db") is None


def test_new_values_never_set_a_secret_revoked_after_the_connection_was_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secret revoked between the first read and its lock is not set again.

    The secret is read afresh under its lock before new values rotate it; revoked meanwhile,
    it is left alone, and the values make a new secret under the next free name.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Lets the first read see the secret as it was before a concurrent revoke.
    """
    world.postgres(world.data_engineer)
    secret = world.secret("prod", "crm-db-connection")
    secret.revoked_at = datetime.now(UTC)
    world.session.commit()
    before = secret.ciphertext
    monkeypatch.setattr(ConnectionService, "_used_secret", lambda self, row: secret)

    world.connections().update(
        world.data_engineer, "prod", "crm-db", secret_values={"password": "new"}
    )

    row = world.connection("prod", "crm-db")
    assert row is not None
    assert row.secret_id == world.secret("prod", "crm-db-connection-2").id
    assert world.secret("prod", "crm-db-connection").ciphertext == before


def test_naming_a_secret_for_settings_that_take_none_is_refused_on_update(world: World) -> None:
    """An http connection without auth takes no secret, by name either."""
    service = world.connections()
    service.create(
        world.admin,
        "prod",
        name="api",
        kind=ConnectionKind.HTTP,
        address={"base_url": "https://api.example.com"},
        settings={},
    )
    world.make_secret("prod", "api-token", {"token": "t"})

    with pytest.raises(ValidationError, match="An http connection with auth none takes no secret"):
        service.update(world.admin, "prod", "api", secret="api-token")
    assert world.secret("prod", "api-token").use_kind is None


def test_a_secret_revoked_while_its_binding_is_checked_is_not_taken(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding check reads the secret afresh under its lock; a revoked one is not used.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Revokes the secret during the check, as a concurrent revoke would.
    """
    from lakana_core.connections import service as module

    world.postgres(world.data_engineer, secret_values=None)
    secret = world.make_secret("prod", "crm-pw", {"password": "pw"})
    original = module.bind_or_require_named_secrets

    def revoking(*args: object, **kwargs: object) -> None:
        """Revoke the secret, then run the real check.

        Parameters
        ----------
        *args
            Passed on.
        **kwargs
            Passed on.
        """
        secret.revoked_at = datetime.now(UTC)
        original(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(module, "bind_or_require_named_secrets", revoking)

    with pytest.raises(NotFoundError) as caught:
        world.connections().update(world.admin, "prod", "crm-db", secret="crm-pw")

    assert str(caught.value) == (
        "environment `prod` has no secret `crm-pw` this connection may use; "
        "`lakana secret list --env prod` lists them"
    )
    assert world.connections().get(world.analyst, "prod", "crm-db").secret is None


def test_a_bind_whose_secret_moved_meanwhile_is_a_conflict(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secret bound to a third connection while the locks are taken is not moved; try again.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Moves the secret to `third` between the connection locks and the secret's re-read.
    """
    from lakana_core.secrets import service as module

    world.postgres(world.admin, "first", secret_values=None)
    world.postgres(world.admin, "second", secret_values=None)
    world.postgres(world.admin, "third", secret_values=None)
    first, second, third = (world.connection("prod", n) for n in ("first", "second", "third"))
    assert first is not None and second is not None and third is not None
    secrets = world.secrets()
    secret = world.make_secret("prod", "pw", {"password": "pw"}, SecretUse.connection(first))
    original = module.lock_entry

    def moving(*args: object, **kwargs: object) -> object:
        """Bind the secret to `third` behind the caller's back, then lock as usual.

        Parameters
        ----------
        *args
            Passed on.
        **kwargs
            Passed on.

        Returns
        -------
        object
            What the real lock returns.
        """
        world.session.execute(
            sa_update(Secret).where(Secret.id == secret.id).values(use_target_id=third.id)
        )
        return original(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(module, "lock_entry", moving)

    with pytest.raises(ConflictError, match=r"Secret 'pw' changed at the same time; try again\."):
        secrets.bind(world.admin, secret.id, SecretUse.connection(second))


def test_the_listing_names_where_each_secret_goes(world: World) -> None:
    """Unbound: nothing; an index: its name and URL; a connection: its name and hosts."""
    index = PackageIndexService(world.session, world.authz()).create(
        world.admin, name="corp", url="https://corp.example.org/simple"
    )
    secrets = world.secrets()
    unbound = world.make_secret("prod", "loose", {"password": "pw"})
    for_index = world.make_secret(
        "prod", "corp-pw", {"password": "pw"}, SecretUse.package_index(index)
    )

    assert (secrets.use_target_name(unbound), secrets.use_target_url(unbound)) == (None, None)
    assert (secrets.use_target_name(for_index), secrets.use_target_url(for_index)) == (
        "corp",
        "https://corp.example.org/simple",
    )


def test_a_region_change_moves_a_default_s3_endpoint_but_keeps_a_custom_one(
    world: World,
) -> None:
    """The default endpoint follows the region; a custom one stays.

    Moving the endpoint moves the secret, so a data engineer needs new values for it (S1);
    an administrator does not.
    """
    service = world.connections()
    for name, address in (
        ("aws", {"bucket": "landing", "region": "eu-central-1"}),
        (
            "minio",
            {
                "bucket": "landing",
                "region": "eu-central-1",
                "endpoint": "https://minio.example.com",
            },
        ),
    ):
        service.create(
            world.data_engineer,
            "prod",
            name=name,
            kind=ConnectionKind.S3,
            address=address,
            settings={"access_key_id": "AKIA1"},
            secret_values={"secret_access_key": "k"},
        )

    with pytest.raises(PermissionDenied, match="could send secret 'aws-connection'"):
        service.update(world.data_engineer, "prod", "aws", address={"region": "us-east-1"})
    moved = service.update(world.admin, "prod", "aws", address={"region": "us-east-1"})
    kept = service.update(world.data_engineer, "prod", "minio", address={"region": "us-east-1"})
    explicit = service.update(
        world.admin,
        "prod",
        "aws",
        address={"region": "eu-west-1", "endpoint": "https://s3.us-east-1.amazonaws.com"},
    )

    assert moved.address == {
        "bucket": "landing",
        "region": "us-east-1",
        "endpoint": "https://s3.us-east-1.amazonaws.com",
    }
    assert kept.address["endpoint"] == "https://minio.example.com"
    assert explicit.address["endpoint"] == "https://s3.us-east-1.amazonaws.com"
    assert world.events(AuditEventType.CONNECTION_UPDATED)[-3].detail["changed"] == [
        "address.endpoint",
        "address.region",
    ]


def test_a_name_the_environment_reserves_is_skipped_for_a_new_secret(world: World) -> None:
    """A name storage uses, with no secret of that name yet, is never given to a connection.

    The new secret takes the next free name instead of the create failing.
    """
    env = world.session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
    env.storage_secret_ref = "crm-db-connection"
    world.session.commit()

    view = world.postgres(world.data_engineer)

    assert view.secret == "crm-db-connection-2"
    assert (
        world.session.execute(
            select(Secret).where(Secret.name == "crm-db-connection")
        ).scalar_one_or_none()
        is None
    )


_NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _claimed(
    world: World, owner: str = "worker-1", *, lease_seconds: int = 60, now: datetime = _NOW
) -> ConnectionTest:
    """Claim the oldest test at `_NOW`.

    Parameters
    ----------
    world
        The world.
    owner
        The claiming worker.
    lease_seconds
        The lease.
    now
        The time of the claim.

    Returns
    -------
    ConnectionTest
        The claimed row.
    """
    claimed = world.connections().claim_test(owner, lease_seconds=lease_seconds, now=now)
    assert claimed is not None
    return claimed


def test_a_test_is_requested_queued_and_audited(world: World) -> None:
    world.postgres(world.data_engineer)

    test = world.connections().request_test(world.data_engineer, "prod", "crm-db")

    assert (test.state, test.outcome, test.message, test.finished_at) == (
        ConnectionTestState.QUEUED,
        None,
        None,
        None,
    )
    assert test.connection_id == world.connection_id("prod", "crm-db")
    event = world.events(AuditEventType.CONNECTION_TESTED)[-1]
    assert event.detail == {
        "connection": "crm-db",
        "environment": "prod",
        "test_id": str(test.id),
        "state": "queued",
    }
    assert event.securable_environment == "prod"
    shown = world.connections().test_result(world.data_engineer, "prod", "crm-db", str(test.id))
    assert shown.id == test.id


def test_a_second_test_is_refused_while_one_is_queued_or_running(world: World) -> None:
    world.postgres(world.data_engineer)
    service = world.connections()
    first = service.request_test(world.data_engineer, "prod", "crm-db")
    see = f"see it with lakana connection test crm-db --env prod --id {first.id}"

    with pytest.raises(ConflictError) as queued:
        service.request_test(world.data_engineer, "prod", "crm-db")
    _claimed(world)
    with pytest.raises(ConflictError) as running:
        service.request_test(world.data_engineer, "prod", "crm-db")

    assert str(queued.value) == f"a test of `crm-db` is already queued; {see}"
    assert str(running.value) == f"a test of `crm-db` is already running; {see}"
    service.finish_test(first.id, "worker-1", _NOW + timedelta(seconds=60), "ok", "fine")
    again = service.request_test(world.data_engineer, "prod", "crm-db")
    assert again.id != first.id


def test_a_test_needs_manage_connection_and_an_existing_connection(world: World) -> None:
    world.postgres(world.data_engineer)
    test = world.connections().request_test(world.data_engineer, "prod", "crm-db")

    with pytest.raises(PermissionDenied):
        world.connections().request_test(world.analyst, "prod", "crm-db")
    with pytest.raises(PermissionDenied):
        world.connections().test_result(world.analyst, "prod", "crm-db", str(test.id))
    with pytest.raises(NotFoundError, match="has no connection `nope`"):
        world.connections().request_test(world.data_engineer, "prod", "nope")


def test_a_test_of_another_connection_or_an_odd_id_is_not_found(world: World) -> None:
    world.postgres(world.data_engineer)
    world.postgres(world.data_engineer, "other-db")
    test = world.connections().request_test(world.data_engineer, "prod", "crm-db")
    service = world.connections()

    for name, test_id in (
        ("other-db", str(test.id)),
        ("crm-db", str(uuid.uuid4())),
        ("crm-db", "x"),
    ):
        with pytest.raises(
            NotFoundError,
            match=f"connection `{name}` has no test `{test_id}`; `lakana connection test {name} ",
        ):
            service.test_result(world.data_engineer, "prod", name, test_id)


def test_claim_takes_the_oldest_queued_test_and_sets_the_lease(world: World) -> None:
    world.postgres(world.data_engineer)
    world.postgres(world.data_engineer, "other-db")
    service = world.connections()
    older = service.request_test(world.data_engineer, "prod", "crm-db")
    younger = service.request_test(world.data_engineer, "prod", "other-db")
    for row, age in ((older, 10), (younger, 5)):
        world.session.execute(
            sa_update(ConnectionTest)
            .where(ConnectionTest.id == row.id)
            .values(created_at=_NOW - timedelta(minutes=age))
        )
    world.session.commit()

    first = _claimed(world, lease_seconds=30)
    second = _claimed(world, "worker-2")

    assert (first.id, first.state, first.lease_owner) == (
        older.id,
        ConnectionTestState.RUNNING,
        "worker-1",
    )
    assert first.lease_expires_at == _NOW + timedelta(seconds=30)
    assert second.id == younger.id
    assert service.claim_test("worker-3", now=_NOW) is None


def test_claim_takes_a_running_test_whose_lease_ran_out(world: World) -> None:
    world.postgres(world.data_engineer)
    test = world.connections().request_test(world.data_engineer, "prod", "crm-db")
    _claimed(world, lease_seconds=60)

    assert world.connections().claim_test("worker-2", now=_NOW + timedelta(seconds=59)) is None
    taken = _claimed(world, "worker-2", now=_NOW + timedelta(seconds=60))

    assert (taken.id, taken.lease_owner) == (test.id, "worker-2")
    assert taken.lease_expires_at == _NOW + timedelta(seconds=120)


def test_finish_records_the_outcome_and_audits_it(world: World) -> None:
    world.postgres(world.data_engineer)
    test = world.connections().request_test(world.data_engineer, "prod", "crm-db")
    claimed = _claimed(world)

    recorded = world.connections().finish_test(
        test.id, "worker-1", claimed.lease_expires_at, "refused", "connection refused"
    )

    assert recorded is True
    done = world.connections().test_result(world.data_engineer, "prod", "crm-db", str(test.id))
    assert (done.state, done.outcome, done.message) == (
        ConnectionTestState.DONE,
        ConnectionTestOutcome.REFUSED,
        "connection refused",
    )
    assert done.finished_at is not None
    assert done.lease_owner is None and done.lease_expires_at is None
    tested = world.events(AuditEventType.CONNECTION_TESTED)
    assert [event.detail["state"] for event in tested] == ["queued", "done"]
    assert tested[-1].detail == {
        "connection": "crm-db",
        "environment": "prod",
        "test_id": str(test.id),
        "state": "done",
        "outcome": "refused",
    }
    assert tested[-1].principal_id == world.data_engineer.id


def test_finish_by_a_stale_owner_or_lease_is_ignored(world: World) -> None:
    world.postgres(world.data_engineer)
    test = world.connections().request_test(world.data_engineer, "prod", "crm-db")
    first_lease = _claimed(world, "worker-1").lease_expires_at
    second_lease = _claimed(world, "worker-2", now=_NOW + timedelta(seconds=61)).lease_expires_at
    service = world.connections()

    wrong_owner = service.finish_test(test.id, "worker-1", second_lease, "ok", "late")
    old_lease = service.finish_test(test.id, "worker-2", first_lease, "ok", "late")

    assert (wrong_owner, old_lease) == (False, False)
    row = service.test_result(world.data_engineer, "prod", "crm-db", str(test.id))
    assert (row.state, row.outcome) == (ConnectionTestState.RUNNING, None)
    assert len(world.events(AuditEventType.CONNECTION_TESTED)) == 1
    assert service.finish_test(test.id, "worker-2", second_lease, "ok", "fine") is True


def test_a_message_is_cut_at_1000_characters(world: World) -> None:
    world.postgres(world.data_engineer)
    test = world.connections().request_test(world.data_engineer, "prod", "crm-db")
    claimed = _claimed(world)

    world.connections().finish_test(
        test.id, "worker-1", claimed.lease_expires_at, "failed", "x" * 5000
    )

    done = world.connections().test_result(world.data_engineer, "prod", "crm-db", str(test.id))
    assert done.message == "x" * 1000


_STUCK = (
    "no worker finished this test within 10 minutes; in this version only a worker with the "
    "local runner runs connection tests — read that worker's log (`docker compose logs worker`, "
    "or the `lakana local` terminal)"
)


def _aged(world: World, test_id: uuid.UUID, minutes: int) -> None:
    """Make a test older.

    Parameters
    ----------
    world
        The world.
    test_id
        The test.
    minutes
        How many minutes before now it was created.
    """
    world.session.execute(
        sa_update(ConnectionTest)
        .where(ConnectionTest.id == test_id)
        .values(created_at=datetime.now(UTC) - timedelta(minutes=minutes))
    )
    world.session.commit()


def test_a_stale_queued_test_is_closed_as_failed_and_the_new_one_queued(world: World) -> None:
    world.postgres(world.data_engineer)
    service = world.connections()
    stuck = service.request_test(world.data_engineer, "prod", "crm-db")
    _aged(world, stuck.id, 11)

    fresh = service.request_test(world.data_engineer, "prod", "crm-db")

    assert fresh.id != stuck.id and fresh.state == ConnectionTestState.QUEUED
    closed = service.test_result(world.data_engineer, "prod", "crm-db", str(stuck.id))
    assert (closed.state, closed.outcome, closed.message) == (
        ConnectionTestState.DONE,
        ConnectionTestOutcome.FAILED,
        _STUCK,
    )
    assert closed.finished_at is not None and closed.lease_owner is None
    tested = world.events(AuditEventType.CONNECTION_TESTED)
    assert [(e.detail["test_id"], e.detail["state"]) for e in tested] == [
        (str(stuck.id), "queued"),
        (str(stuck.id), "done"),
        (str(fresh.id), "queued"),
    ]
    assert tested[1].detail["outcome"] == "failed"


def test_a_stale_running_test_is_closed_the_same_way(world: World) -> None:
    world.postgres(world.data_engineer)
    service = world.connections()
    stuck = service.request_test(world.data_engineer, "prod", "crm-db")
    _claimed(world, now=datetime.now(UTC) - timedelta(minutes=9))
    _aged(world, stuck.id, 11)

    fresh = service.request_test(world.data_engineer, "prod", "crm-db")

    closed = service.test_result(world.data_engineer, "prod", "crm-db", str(stuck.id))
    assert (closed.state, closed.outcome, closed.message) == (
        ConnectionTestState.DONE,
        ConnectionTestOutcome.FAILED,
        _STUCK,
    )
    assert fresh.state == ConnectionTestState.QUEUED


def test_a_test_inside_ten_minutes_still_gets_the_409(world: World) -> None:
    world.postgres(world.data_engineer)
    service = world.connections()
    open_test = service.request_test(world.data_engineer, "prod", "crm-db")
    _aged(world, open_test.id, 9)

    with pytest.raises(ConflictError):
        service.request_test(world.data_engineer, "prod", "crm-db")

    row = service.test_result(world.data_engineer, "prod", "crm-db", str(open_test.id))
    assert row.state == ConnectionTestState.QUEUED


def test_the_worker_of_a_closed_test_is_ignored_afterwards(world: World) -> None:
    world.postgres(world.data_engineer)
    service = world.connections()
    stuck = service.request_test(world.data_engineer, "prod", "crm-db")
    lease = _claimed(world, now=datetime.now(UTC)).lease_expires_at
    _aged(world, stuck.id, 11)
    service.request_test(world.data_engineer, "prod", "crm-db")

    recorded = service.finish_test(stuck.id, "worker-1", lease, "ok", "late")

    assert recorded is False
    closed = service.test_result(world.data_engineer, "prod", "crm-db", str(stuck.id))
    assert (closed.outcome, closed.message) == (ConnectionTestOutcome.FAILED, _STUCK)


def test_a_request_locks_the_open_test_it_reads(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker's `finish_test` must not slip between the read and the stale close."""
    world.postgres(world.data_engineer)
    service = world.connections()
    service.request_test(world.data_engineer, "prod", "crm-db")
    locked: list[bool] = []
    original = cast("Callable[..., object]", world.session.execute)

    def spy(statement: object, *args: object, **kwargs: object) -> object:
        """Record whether a select of connection tests asks for a row lock.

        Parameters
        ----------
        statement
            The statement.
        *args
            Passed on.
        **kwargs
            Passed on.

        Returns
        -------
        object
            The result.
        """
        if "connection_tests.state IN" in str(statement):
            locked.append(getattr(statement, "_for_update_arg", None) is not None)
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(world.session, "execute", spy)
    with pytest.raises(ConflictError):
        service.request_test(world.data_engineer, "prod", "crm-db")
    assert locked and all(locked)
