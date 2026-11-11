"""Binding a secret: who may, and what naming a secret from an index or repository does."""

from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    JobVersion,
    NotificationDelivery,
    PackageIndex,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Secret,
    SecretUseKind,
)
from lakana_core.projects.package_indexes import PackageIndexService
from lakana_core.projects.repositories import RepositoryService
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.secrets.uses import SecretUse, resolve_use, used_by_its_environment

_KEYS = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())


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


@pytest.fixture
def world(session: Session) -> dict[str, PrincipalRef]:
    """Two environments, an administrator and a holder of the two manage actions alone.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    dict
        ``admin`` and ``manager``.
    """
    for name in ("dev", "prod"):
        session.add(Environment(id=uuid.uuid4(), name=name, storage_root=f"file:///srv/{name}"))
    session.commit()
    admin, manager = _principal(session, "admin"), _principal(session, "manager")
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    for action in (Action.MANAGE_PACKAGE_INDEX, Action.MANAGE_REPOSITORY):
        grants.grant(admin, principal="manager", action=action, kind=SecurableKind.INSTANCE)
    return {"admin": admin, "manager": manager}


def _secret(session: Session, environment: str, name: str, use: SecretUse | None = None) -> Secret:
    """Create a secret as an administrator would: unbound unless a use is given.

    Parameters
    ----------
    session
        Database session.
    environment
        Its environment.
    name
        Its name.
    use
        What it is for; `None` leaves it unbound.

    Returns
    -------
    Secret
        The row.
    """
    admin = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    ref = PrincipalRef(id=admin.id, name=admin.name, type=admin.type)
    return SecretService(session, AllowAllAuthorization(), _KEYS).create(
        ref, environment=environment, name=name, values={"password": "pw"}, use=use
    )


def _uses(session: Session, name: str) -> dict[str, tuple[SecretUseKind | None, uuid.UUID | None]]:
    """Read the use of every secret called `name`, by environment.

    Parameters
    ----------
    session
        Database session.
    name
        The secret name.

    Returns
    -------
    dict
        Environment name → (kind, target id).
    """
    session.expire_all()
    rows = session.execute(
        select(Environment.name, Secret.use_kind, Secret.use_target_id)
        .join(Environment, Environment.id == Secret.environment_id)
        .where(Secret.name == name)
    ).all()
    return {env: (kind, target) for env, kind, target in rows}


def _url(session: Session, model: type[PackageIndex] | type[Repository], row_id: uuid.UUID) -> str:
    """Read the stored URL of an index or repository afresh.

    Parameters
    ----------
    session
        Database session.
    model
        `PackageIndex` or `Repository`.
    row_id
        Its id.

    Returns
    -------
    str
        The URL as committed.
    """
    session.expire_all()
    row = session.get(model, row_id)
    assert row is not None
    return row.url


def _indexes(session: Session) -> PackageIndexService:
    """Build the index service on real grants.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PackageIndexService
        The service.
    """
    return PackageIndexService(session, GrantAuthorization(session))


def test_bind_needs_manage_secret_checks_the_target_and_audits(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Only a secret manager binds; an unknown target or a revoked secret is refused."""
    secret = _secret(session, "prod", "both")
    index = _indexes(session).create(
        world["admin"], name="corp", url="https://corp.example.org/simple"
    )
    granted = SecretService(session, GrantAuthorization(session), _KEYS)

    with pytest.raises(PermissionDenied):
        granted.bind(world["manager"], secret.id, SecretUse.package_index(index))
    bound = granted.bind(
        world["admin"], secret.id, resolve_use(session, SecretUseKind.PACKAGE_INDEX, "corp")
    )

    assert (bound.use_kind, bound.use_target_id) == (SecretUseKind.PACKAGE_INDEX, index.id)
    event = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.SECRET_BOUND)
        )
        .scalars()
        .one()
    )
    assert event.detail == {
        "name": "both",
        "use": "package_index",
        "target": "corp",
        "url": index.url,
        "previous": None,
    }
    with pytest.raises(NotFoundError, match="No package index named 'nope'"):
        resolve_use(session, SecretUseKind.PACKAGE_INDEX, "nope")
    with pytest.raises(ValidationError, match="takes no name"):
        resolve_use(session, SecretUseKind.ENVIRONMENT, "corp")
    granted.revoke(world["admin"], secret.id)
    with pytest.raises(ValidationError, match="has been revoked"):
        granted.bind(world["admin"], secret.id, SecretUse.environment())


_URL_REFUSAL = (
    "Changing the URL of {use} would send secret '{secret}' of environment 'prod' to another "
    "host. That needs the right to manage secrets in 'prod' (the ADMIN role). Ask someone who "
    "may manage secrets in 'prod' to change the URL, or to bind the secret elsewhere first: "
    "lakana secret bind {secret} --env prod (with --index NAME, --repository NAME, "
    "--connection NAME or --own-env)"
)


def _auditing(session: Session) -> AuditingAuthorization:
    """Build real grants that record denials, as the server does.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    AuditingAuthorization
        The authorization service.
    """
    return AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(10_000))


def _denials(session: Session) -> list[tuple[uuid.UUID | None, str]]:
    """List the recorded denials as (actor, reason).

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list
        One pair per `AUTHORIZATION_DENIED` row, oldest first.
    """
    session.expire_all()
    rows = (
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
            .order_by(AuditEvent.occurred_at)
        )
        .scalars()
        .all()
    )
    return [(row.principal_id, row.detail.get("reason", "")) for row in rows]


def _bound_events(session: Session) -> int:
    """Count the `SECRET_BOUND` events.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    int
        The number of rows.
    """
    return len(
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.SECRET_BOUND)
        )
        .scalars()
        .all()
    )


def _scoped(session: Session, world: dict[str, PrincipalRef], *, role: bool) -> PrincipalRef:
    """Persist `devsec`: both manage actions everywhere, and secret management in dev only.

    Parameters
    ----------
    session
        Database session.
    world
        The fixture's principals.
    role
        Give the `ADMIN` role scoped to `dev` instead of the single `MANAGE_SECRET` action.

    Returns
    -------
    PrincipalRef
        The principal.
    """
    ref = _principal(session, "devsec")
    grants = GrantService(session, AllowAllAuthorization())
    for action in (Action.MANAGE_PACKAGE_INDEX, Action.MANAGE_REPOSITORY):
        grants.grant(world["admin"], principal="devsec", action=action, kind=SecurableKind.INSTANCE)
    if role:
        grants.grant(
            world["admin"],
            principal="devsec",
            role=Role.ADMIN,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
        )
    else:
        grants.grant(
            world["admin"],
            principal="devsec",
            action=Action.MANAGE_SECRET,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
        )
    return ref


@pytest.mark.parametrize("role", [False, True])
def _repositories(session: Session) -> RepositoryService:
    """Build the repository service on real grants that record denials.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    RepositoryService
        The service.
    """
    return RepositoryService(session, _auditing(session))


def _credentialed_index(
    session: Session,
    world: dict[str, PrincipalRef],
    secret: str,
    environments: tuple[str, ...] = ("prod",),
    name: str = "corp",
) -> PackageIndex:
    """Register an index that names `secret`, then create the secret bound to it.

    Parameters
    ----------
    session
        Database session.
    world
        The fixture's principals.
    secret
        The secret's name.
    environments
        The environments that get a secret of that name, bound to the index.
    name
        The index's name.

    Returns
    -------
    PackageIndex
        The index.
    """
    row = _indexes(session).create(
        world["manager"],
        name=name,
        url=f"https://{name}.example.org/simple",
        credential_secret=secret,
    )
    for environment in environments:
        _secret(session, environment, secret, SecretUse.package_index(row))
    return row


def test_naming_a_secret_bound_to_the_entry_is_allowed(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """A manager names what an administrator bound to that very index."""
    row = _indexes(session).create(
        world["manager"], name="corp", url="https://corp.example.org/simple"
    )
    _secret(session, "prod", "pypi", SecretUse.package_index(row))

    _indexes(session).update(world["manager"], "corp", {"credential_secret": "pypi"})

    assert session.execute(select(PackageIndex.credential_secret)).scalar_one() == "pypi"
    assert _uses(session, "pypi") == {"prod": (SecretUseKind.PACKAGE_INDEX, row.id)}


def test_a_manager_never_binds_an_unbound_secret(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Naming an unbound secret is refused, on update and on create, and audited."""
    _secret(session, "prod", "pypi")
    indexes = PackageIndexService(session, _auditing(session))
    indexes.create(world["manager"], name="corp", url="https://corp.example.org/simple")

    with pytest.raises(PermissionDenied) as updated:
        indexes.update(world["manager"], "corp", {"credential_secret": "pypi"})
    with pytest.raises(PermissionDenied) as created:
        indexes.create(
            world["manager"], name="b", url="https://b.example.org/simple", credential_secret="pypi"
        )

    assert str(updated.value) == (
        "Secret 'pypi' of environment 'prod' is not bound to package index 'corp'. Ask someone "
        "who may manage secrets in 'prod' to bind it to this index: "
        "lakana secret bind pypi --env prod --index corp"
    )
    assert str(created.value) == (
        "Secret 'pypi' of environment 'prod' is not bound to package index 'b'. Register the "
        "index without a credential first, then ask someone who may manage secrets in 'prod' to "
        "bind it to the index: lakana secret bind pypi --env prod --index b"
    )
    assert _uses(session, "pypi") == {"prod": (None, None)}
    assert _denials(session) == [(world["manager"].id, "secret_not_bound_to_entry")] * 2
    assert _bound_events(session) == 0


def test_a_secret_bound_elsewhere_is_refused_to_a_manager_and_an_administrator(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Naming never changes a use, whoever names; `lakana secret bind` does."""
    first = _credentialed_index(session, world, "pypi", name="a")
    indexes = PackageIndexService(session, _auditing(session))
    indexes.create(world["manager"], name="b", url="https://b.example.org/simple")

    for who in ("manager", "admin"):
        with pytest.raises(PermissionDenied) as caught:
            indexes.update(world[who], "b", {"credential_secret": "pypi"})
        assert str(caught.value) == (
            "Secret 'pypi' of environment 'prod' is bound to package index 'a', not to package "
            "index 'b'. Ask someone who may manage secrets in 'prod' to give this index a secret "
            "of its own: `lakana secret create NEW --env prod --index b --values-stdin`, then "
            "`lakana index update b --credential-secret NEW`. Moving this secret instead breaks "
            "package index 'a': lakana secret bind pypi --env prod --index b"
        )
    assert _uses(session, "pypi") == {"prod": (SecretUseKind.PACKAGE_INDEX, first.id)}
    assert [reason for _, reason in _denials(session)] == ["secret_bound_elsewhere"] * 2


@pytest.mark.parametrize("how", ["clear", "delete"])
def test_lowering_what_names_a_secret_does_not_let_a_manager_claim_it(
    session: Session, world: dict[str, PrincipalRef], how: str
) -> None:
    """Clear or delete the other entry, move the URL, then name the secret: still refused."""
    indexes = PackageIndexService(session, _auditing(session))
    for name in ("a", "b"):
        indexes.create(
            world["manager"],
            name=name,
            url=f"https://{name}.example.org/simple",
            credential_secret="pypi",
        )
    _secret(session, "prod", "pypi")
    if how == "clear":
        indexes.update(world["manager"], "a", {"credential_secret": None})
    else:
        indexes.delete(world["manager"], "a")
    indexes.update(world["manager"], "b", {"credential_secret": None})
    indexes.update(world["manager"], "b", {"url": "https://evil.example.org/simple"})

    with pytest.raises(PermissionDenied, match="is not bound to package index 'b'"):
        indexes.update(world["manager"], "b", {"credential_secret": "pypi"})

    assert _uses(session, "pypi") == {"prod": (None, None)}


def test_the_same_holds_for_repositories_and_across_kinds(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """A repository manager and an index manager cannot claim each other's secret either."""
    repositories = _repositories(session)
    indexes = PackageIndexService(session, _auditing(session))
    repo = repositories.create(
        world["manager"],
        name="r",
        kind=RepositoryKind.HTTPS,
        url="https://git.example.org/r.git",
        credential_secret="tok",
    )
    indexes.create(world["manager"], name="a", url="https://a.example.org/simple")
    _secret(session, "prod", "tok")

    with pytest.raises(PermissionDenied, match="is not bound to package index 'a'"):
        indexes.update(world["manager"], "a", {"credential_secret": "tok"})
    repositories.update(world["manager"], "r", {"credential_secret": None})
    repositories.update(world["manager"], "r", {"url": "https://evil.example.org/r.git"})
    with pytest.raises(PermissionDenied, match="is not bound to repository 'r'"):
        repositories.update(world["manager"], "r", {"credential_secret": "tok"})
    with pytest.raises(PermissionDenied) as created:
        repositories.create(
            world["manager"],
            name="s",
            kind=RepositoryKind.HTTPS,
            url="https://git.example.org/s.git",
            credential_secret="tok",
        )
    assert "Register the repository without a credential first" in str(created.value)
    assert _uses(session, "tok") == {"prod": (None, None)}
    assert [reason for _, reason in _denials(session)] == ["secret_not_bound_to_entry"] * 3
    assert session.execute(select(Repository.name)).scalars().all() == ["r"]
    assert repo.id is not None


def _webhook_manifest(session: Session, world: dict[str, PrincipalRef], name: str) -> None:
    """Deploy a project version whose job notifies the webhook secret `name` in prod.

    Parameters
    ----------
    session
        Database session.
    world
        The fixture's principals.
    name
        The secret name.
    """
    prod = session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
    repository = RepositoryService(session, AllowAllAuthorization()).create(
        world["admin"], name="r", kind=RepositoryKind.HTTPS, url="https://git.example.org/r.git"
    )
    session.add(
        JobVersion(
            id=uuid.uuid4(),
            repository_id=repository.id,
            environment_id=prod.id,
            commit_sha="a" * 40,
            manifest_json={"jobs": {"nightly": {"notify": {"webhook": name}}}},
            code_uri="file:///code",
            code_sha256="0" * 64,
        )
    )
    session.commit()


def _delivery(session: Session, world: dict[str, PrincipalRef], name: str) -> None:
    """Record that a notification went through the webhook secret `name` in prod.

    Parameters
    ----------
    session
        Database session.
    world
        The fixture's principals.
    name
        The secret name.
    """
    prod = session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
    repository = RepositoryService(session, AllowAllAuthorization()).create(
        world["admin"], name="r", kind=RepositoryKind.HTTPS, url="https://git.example.org/r.git"
    )
    session.add(
        NotificationDelivery(
            id=uuid.uuid4(),
            environment_id=prod.id,
            repository_id=repository.id,
            job="nightly",
            event="test",
            format="json",
            secret_name=name,
        )
    )
    session.commit()


@pytest.mark.parametrize("known_through", ["storage", "manifest", "delivery"])
@pytest.mark.parametrize("who", ["manager", "admin"])
def test_a_secret_the_environment_uses_is_never_rebound_by_naming_it(
    session: Session, world: dict[str, PrincipalRef], known_through: str, who: str
) -> None:
    """Storage, a deployed webhook or a delivery: refused for everyone, with the explicit way."""
    _secret(session, "prod", "hook")
    if known_through == "storage":
        prod = session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
        prod.storage_secret_ref = "hook"
        session.commit()
    elif known_through == "manifest":
        _webhook_manifest(session, world, "hook")
    else:
        _delivery(session, world, "hook")
    service = PackageIndexService(session, _auditing(session))
    service.create(world["admin"], name="corp", url="https://corp.example.org/simple")

    with pytest.raises(PermissionDenied) as caught:
        service.update(world[who], "corp", {"credential_secret": "hook"})

    own = (
        "give the index a secret of its own: `lakana secret create NEW --env prod --index corp "
        "--values-stdin`, then `lakana index update corp --credential-secret NEW`"
    )
    moving = ", or to move this one on purpose (the environment then loses it): "
    on_purpose = moving + "lakana secret bind hook --env prod --index corp"
    assert str(caught.value) == (
        "Secret 'hook' of environment 'prod' serves its environment (storage or webhooks), so "
        "naming it for package index 'corp' will not move it. Ask someone who may manage secrets "
        f"in 'prod' to {own}" + ("" if known_through == "storage" else on_purpose)
    )
    assert _uses(session, "hook") == {"prod": (None, None)}
    assert _denials(session) == [(world[who].id, "secret_used_by_environment")]


def test_a_secret_created_without_a_use_stays_unbound(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Even the one index that names it does not take it; an explicit use binds and is audited."""
    index = _indexes(session).create(
        world["manager"], name="corp", url="https://corp.example.org/simple", credential_secret="p"
    )
    plain = _secret(session, "prod", "p")
    own = _secret(session, "dev", "s3", SecretUse.environment())
    bound = _secret(session, "dev", "p", SecretUse.package_index(index))

    assert (plain.use_kind, plain.use_target_id) == (None, None)
    assert (own.use_kind, own.use_target_id) == (SecretUseKind.ENVIRONMENT, None)
    assert (bound.use_kind, bound.use_target_id) == (SecretUseKind.PACKAGE_INDEX, index.id)
    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.SECRET_BOUND)
        )
        .scalars()
        .all()
    )
    assert sorted(e.detail["target"] or "" for e in events) == ["", "corp"]
    assert {e.principal_id for e in events} == {_admin_id(session)}


def _admin_id(session: Session) -> uuid.UUID:
    """Look up the administrator's id.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        The id.
    """
    return session.execute(select(Principal.id).where(Principal.name == "admin")).scalar_one()


def test_an_unchanged_credential_checks_nothing(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """An unrelated update, or the same name again, does not ask what the secret is bound to."""
    repositories = _repositories(session)
    repositories.create(
        world["manager"],
        name="r",
        kind=RepositoryKind.HTTPS,
        url="https://git.example.org/r.git",
        credential_secret="git",
    )
    _secret(session, "prod", "git")

    repositories.update(world["manager"], "r", {"default_ref": "main"})
    repositories.update(world["manager"], "r", {"credential_secret": "git"})

    assert _uses(session, "git") == {"prod": (None, None)}
    assert _denials(session) == []


def test_a_revoked_secret_does_not_count(session: Session, world: dict[str, PrincipalRef]) -> None:
    """Only active secrets are looked at: a revoked one bound elsewhere does not block naming."""
    first = _indexes(session).create(world["manager"], name="a", url="https://a.example.org/simple")
    old = _secret(session, "prod", "pypi", SecretUse.package_index(first))
    SecretService(session, AllowAllAuthorization(), _KEYS).revoke(world["admin"], old.id)
    _indexes(session).create(world["manager"], name="b", url="https://b.example.org/simple")

    _indexes(session).update(world["manager"], "b", {"credential_secret": "pypi"})

    assert (
        session.execute(
            select(PackageIndex.credential_secret).where(PackageIndex.name == "b")
        ).scalar_one()
        == "pypi"
    )


@pytest.mark.parametrize("kind", ["index", "repository"])
def test_a_re_registered_entry_of_the_same_name_does_not_inherit_the_binding(
    session: Session, world: dict[str, PrincipalRef], kind: str
) -> None:
    """The binding follows the id, so deleting and registering again starts unbound."""
    if kind == "index":
        service = _indexes(session)
        row = service.create(world["manager"], name="x", url="https://x.example.org/simple")
        _secret(session, "prod", "cred", SecretUse.package_index(row))
        service.delete(world["manager"], "x")
        service.create(world["manager"], name="x", url="https://x.example.org/simple")
        match = "bound to a package index that was removed"
        call = lambda: service.update(  # ruff: ignore[lambda-assignment]
            world["manager"], "x", {"credential_secret": "cred"}
        )
    else:
        repos = _repositories(session)
        url = "https://git.example.org/x.git"
        row = repos.create(world["manager"], name="x", kind=RepositoryKind.HTTPS, url=url)
        _secret(session, "prod", "cred", SecretUse.repository(row))
        repos.delete(world["manager"], "x")
        repos.create(world["manager"], name="x", kind=RepositoryKind.HTTPS, url=url)
        match = "bound to a repository that was removed"
        call = lambda: repos.update(  # ruff: ignore[lambda-assignment]
            world["manager"], "x", {"credential_secret": "cred"}
        )
    with pytest.raises(PermissionDenied, match=match):
        call()


def test_changing_the_url_of_a_credentialed_index_needs_manage_secret(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The URL is part of the use: moving it moves the secret to another host."""
    row = _credentialed_index(session, world, "pypi")
    service = PackageIndexService(session, _auditing(session))

    with pytest.raises(PermissionDenied) as caught:
        service.update(world["manager"], "corp", {"url": "https://evil.example.org/simple"})

    assert str(caught.value) == _URL_REFUSAL.format(use="package index 'corp'", secret="pypi")
    assert _url(session, PackageIndex, row.id) == "https://corp.example.org/simple"
    service.update(world["manager"], "corp", {"credential_secret": None})
    with pytest.raises(PermissionDenied):
        service.update(world["manager"], "corp", {"url": "https://other.example.org/simple"})
    assert _url(session, PackageIndex, row.id) == "https://corp.example.org/simple"
    service.update(world["admin"], "corp", {"url": "https://moved.example.org/simple"})
    assert _url(session, PackageIndex, row.id) == "https://moved.example.org/simple"
    assert [reason for _, reason in _denials(session)] == ["url_moves_bound_secret"] * 2


def test_clearing_the_credential_does_not_free_the_url(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Clear, move, name again: the secret stays bound to the index, so the move is refused."""
    row = _credentialed_index(session, world, "pypi")
    service = _indexes(session)

    service.update(world["manager"], "corp", {"credential_secret": None})
    with pytest.raises(PermissionDenied, match="Changing the URL of package index 'corp'"):
        service.update(world["manager"], "corp", {"url": "https://evil.example.org/simple"})
    with pytest.raises(PermissionDenied, match="Changing the URL of package index 'corp'"):
        service.update(
            world["manager"],
            "corp",
            {"url": "https://evil.example.org/simple", "credential_secret": "pypi"},
        )

    assert _url(session, PackageIndex, row.id) == "https://corp.example.org/simple"
    assert _uses(session, "pypi") == {"prod": (SecretUseKind.PACKAGE_INDEX, row.id)}


def test_clearing_the_credential_does_not_free_the_url_of_a_repository(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The same for repositories, and an administrator may still move it."""
    service = _repositories(session)
    repo = service.create(
        world["manager"],
        name="analytics",
        kind=RepositoryKind.HTTPS,
        url="https://git.example.org/analytics.git",
        credential_secret="git",
    )
    _secret(session, "prod", "git", SecretUse.repository(repo))

    with pytest.raises(PermissionDenied) as caught:
        service.update(world["manager"], "analytics", {"url": "https://evil.example.org/a.git"})
    assert str(caught.value) == _URL_REFUSAL.format(use="repository 'analytics'", secret="git")
    service.update(world["manager"], "analytics", {"credential_secret": None})
    with pytest.raises(PermissionDenied, match="Changing the URL of repository 'analytics'"):
        service.update(world["manager"], "analytics", {"url": "https://evil.example.org/a.git"})
    assert _url(session, Repository, repo.id) == "https://git.example.org/analytics.git"
    service.update(world["admin"], "analytics", {"url": "https://git.example.org/moved.git"})
    assert _url(session, Repository, repo.id) == "https://git.example.org/moved.git"


@pytest.mark.parametrize("role", [False, True])
def test_rights_in_dev_never_bind_or_redirect_anything_in_prod(
    session: Session, world: dict[str, PrincipalRef], role: bool
) -> None:
    """A dev secret manager cannot move an index whose secret also lives in prod."""
    devsec = _scoped(session, world, role=role)
    first = _credentialed_index(session, world, "pypi", ("dev", "prod"), name="a")
    other = _credentialed_index(session, world, "dev-only", ("dev",), name="o")
    service = PackageIndexService(session, _auditing(session))

    with pytest.raises(PermissionDenied, match="would send secret 'pypi' of environment 'prod'"):
        service.update(devsec, "a", {"url": "https://evil.example.org/simple"})

    assert _url(session, PackageIndex, first.id) == "https://a.example.org/simple"
    assert _denials(session) == [(devsec.id, "url_moves_bound_secret")]
    service.update(devsec, "o", {"url": "https://moved.example.org/simple"})
    assert _url(session, PackageIndex, other.id) == "https://moved.example.org/simple"


@pytest.mark.parametrize("on", ["create", "update"])
def test_someone_who_manages_secrets_binds_an_unbound_secret_by_naming_it(
    session: Session, world: dict[str, PrincipalRef], on: str
) -> None:
    """An administrator registers the index with its credential and is done; it is audited."""
    _secret(session, "prod", "pypi")
    indexes = PackageIndexService(session, _auditing(session))
    if on == "create":
        row = indexes.create(
            world["admin"],
            name="corp",
            url="https://corp.example.org/simple",
            credential_secret="pypi",
        )
    else:
        row = indexes.create(world["admin"], name="corp", url="https://corp.example.org/simple")
        indexes.update(world["admin"], "corp", {"credential_secret": "pypi"})

    assert _uses(session, "pypi") == {"prod": (SecretUseKind.PACKAGE_INDEX, row.id)}
    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.SECRET_BOUND)
        )
        .scalars()
        .all()
    )
    assert [(e.principal_id, e.detail["target"], e.detail["previous"]) for e in events] == [
        (world["admin"].id, "corp", None)
    ]
    assert _denials(session) == []


def test_an_administrator_binds_a_repository_secret_by_naming_it(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The same for repositories."""
    _secret(session, "prod", "git")
    repo = _repositories(session).create(
        world["admin"],
        name="r",
        kind=RepositoryKind.HTTPS,
        url="https://git.example.org/r.git",
        credential_secret="git",
    )
    assert _uses(session, "git") == {"prod": (SecretUseKind.REPOSITORY, repo.id)}


def test_a_manager_naming_the_same_unbound_secret_is_still_refused(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Only the right to manage secrets binds."""
    _secret(session, "prod", "pypi")
    with pytest.raises(PermissionDenied, match="is not bound to package index 'corp'"):
        PackageIndexService(session, _auditing(session)).create(
            world["manager"],
            name="corp",
            url="https://corp.example.org/simple",
            credential_secret="pypi",
        )
    assert _uses(session, "pypi") == {"prod": (None, None)}


def test_an_administrator_does_not_bind_in_an_environment_where_they_lack_the_right(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Rights are per environment: dev rights bind the dev secret and refuse for prod."""
    devsec = _scoped(session, world, role=False)
    _secret(session, "dev", "pypi")
    _secret(session, "prod", "pypi")

    with pytest.raises(PermissionDenied, match="environment 'prod' is not bound"):
        PackageIndexService(session, _auditing(session)).create(
            devsec, name="corp", url="https://corp.example.org/simple", credential_secret="pypi"
        )

    assert _uses(session, "pypi") == {"dev": (None, None), "prod": (None, None)}


def test_creating_a_secret_for_an_entry_that_was_removed_is_refused(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """`create(use=...)` checks the target still exists, as `bind` does."""
    row = _indexes(session).create(world["admin"], name="corp", url="https://c.example.org/simple")
    use = SecretUse.package_index(row)
    _indexes(session).delete(world["admin"], "corp")

    with pytest.raises(NotFoundError):
        _secret(session, "prod", "pypi", use)


@pytest.mark.parametrize("use_of", ["index", "repository"])
def test_a_secret_the_environment_uses_is_not_created_with_an_index_or_repository_use(
    session: Session, world: dict[str, PrincipalRef], use_of: str
) -> None:
    """Creating `s3` for an index while storage names it is refused, and nothing is stored."""
    prod = session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
    prod.storage_secret_ref = "s3"
    session.commit()
    if use_of == "index":
        entry = _indexes(session).create(
            world["admin"], name="corp", url="https://corp.example.org/simple"
        )
        use = SecretUse.package_index(entry)
    else:
        repository = Repository(
            id=uuid.uuid4(), name="repo", kind=RepositoryKind.HTTPS, url="https://git.example.org/r"
        )
        session.add(repository)
        session.commit()
        use = SecretUse.repository(repository)

    with pytest.raises(ValidationError) as caught:
        _secret(session, "prod", "s3", use)

    assert str(caught.value).startswith(
        "Environment 'prod' uses the name 's3' for its storage or a webhook, so a secret of "
        "that name cannot be created for "
    )
    assert str(caught.value).endswith(
        ". Create it with --own-env, or give the index or repository a secret under another name."
    )
    assert _uses(session, "s3") == {}
    own = _secret(session, "prod", "s3", SecretUse.environment())
    assert own.use_kind is SecretUseKind.ENVIRONMENT


@pytest.mark.parametrize("use_of", ["index", "repository"])
def test_the_storage_secret_cannot_be_bound_to_an_index_or_a_repository(
    session: Session, world: dict[str, PrincipalRef], use_of: str
) -> None:
    """Binding it would stop the environment's storage; webhooks stay a deliberate move."""
    secret = _secret(session, "prod", "s3")
    prod = session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
    prod.storage_secret_ref = "s3"
    if use_of == "index":
        entry = _indexes(session).create(
            world["admin"], name="corp", url="https://corp.example.org/simple"
        )
        use = SecretUse.package_index(entry)
    else:
        repository = Repository(
            id=uuid.uuid4(), name="repo", kind=RepositoryKind.HTTPS, url="https://git.example.org/r"
        )
        session.add(repository)
        session.commit()
        use = SecretUse.repository(repository)
    session.commit()
    service = SecretService(session, AllowAllAuthorization(), _KEYS)

    with pytest.raises(ValidationError) as caught:
        service.bind(world["admin"], secret.id, use)

    assert str(caught.value).startswith(
        "Secret 's3' is the storage secret of environment 'prod', so binding it to "
    )
    assert "would stop its storage" in str(caught.value)
    assert "a secret of its own" in str(caught.value)
    assert _uses(session, "s3") == {"prod": (None, None)}
    service.bind(world["admin"], secret.id, SecretUse.environment())
    assert _uses(session, "s3") == {"prod": (SecretUseKind.ENVIRONMENT, None)}


def test_a_webhook_secret_can_still_be_moved_on_purpose(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Only storage is protected from `bind`; a webhook secret is a deliberate move."""
    secret = _secret(session, "prod", "hook", SecretUse.environment())
    entry = _indexes(session).create(
        world["admin"], name="corp", url="https://corp.example.org/simple"
    )
    _webhook_manifest(session, world, "hook")

    SecretService(session, AllowAllAuthorization(), _KEYS).bind(
        world["admin"], secret.id, SecretUse.package_index(entry)
    )

    assert _uses(session, "hook") == {"prod": (SecretUseKind.PACKAGE_INDEX, entry.id)}


def _bound_urls(session: Session) -> list[str | None]:
    """List the URL each `SECRET_BOUND` event recorded, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list
        One URL (or `None`) per event.
    """
    session.expire_all()
    events = (
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.SECRET_BOUND)
            .order_by(AuditEvent.occurred_at)
        )
        .scalars()
        .all()
    )
    return [event.detail["url"] for event in events]


def test_naming_a_secret_on_add_records_the_url_of_the_new_index(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The row is not stored yet when the bind runs; the audit still names its URL."""
    _secret(session, "prod", "pypi")

    _indexes(session).create(
        world["admin"],
        name="corp",
        url="https://Corp.Example.org/simple/",
        credential_secret="pypi",
    )

    assert _bound_urls(session) == ["https://corp.example.org/simple"]


def test_naming_a_secret_while_moving_the_url_records_the_new_url(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The secret goes to the URL the update leaves, not to the one the entry had."""
    _secret(session, "prod", "pypi")
    indexes = _indexes(session)
    indexes.create(world["admin"], name="corp", url="https://old.example.org/simple")

    indexes.update(
        world["admin"],
        "corp",
        {"url": "https://new.example.org/simple", "credential_secret": "pypi"},
    )

    assert _bound_urls(session) == ["https://new.example.org/simple"]


def test_naming_a_secret_on_a_repository_records_its_url(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """On add, and on an update that also moves the URL."""
    _secret(session, "prod", "git")
    _secret(session, "prod", "git2")
    repositories = _repositories(session)
    repositories.create(
        world["admin"],
        name="r",
        kind=RepositoryKind.HTTPS,
        url="https://git.example.org/r.git",
        credential_secret="git",
    )
    repositories.create(
        world["admin"], name="s", kind=RepositoryKind.HTTPS, url="https://git.example.org/s.git"
    )

    repositories.update(
        world["admin"],
        "s",
        {"url": "https://git.example.org/moved.git", "credential_secret": "git2"},
    )

    assert _bound_urls(session) == [
        "https://git.example.org/r.git",
        "https://git.example.org/moved.git",
    ]


def test_a_webhook_of_another_name_does_not_make_a_secret_the_environments(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """Only the job that names the secret counts; other jobs, and jobs without a webhook, do not."""
    prod = session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
    repository = RepositoryService(session, AllowAllAuthorization()).create(
        world["admin"], name="r", kind=RepositoryKind.HTTPS, url="https://git.example.org/r.git"
    )
    session.add(
        JobVersion(
            id=uuid.uuid4(),
            repository_id=repository.id,
            environment_id=prod.id,
            commit_sha="a" * 40,
            manifest_json={
                "jobs": {
                    "plain": {},
                    "other": {"notify": {"webhook": "alerts"}},
                    "mine": {"notify": {"webhook": "hook"}},
                }
            },
            code_uri="file:///code",
            code_sha256="0" * 64,
        )
    )
    session.commit()

    assert used_by_its_environment(session, prod, "hook")
    assert used_by_its_environment(session, prod, "alerts")
    assert not used_by_its_environment(session, prod, "pypi")


def test_the_audit_records_the_url_the_entry_has_under_the_lock(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """A manager moves the URL after the target was resolved; the bind records the new one."""
    secret = _secret(session, "prod", "pypi")
    index = _indexes(session).create(
        world["admin"], name="corp", url="https://old.example.org/simple"
    )
    use = resolve_use(session, SecretUseKind.PACKAGE_INDEX, "corp")
    assert use.target_url == "https://old.example.org/simple"
    session.execute(
        update(PackageIndex)
        .where(PackageIndex.id == index.id)
        .values(url="https://new.example.org/simple")
    )

    SecretService(session, AllowAllAuthorization(), _KEYS).bind(world["admin"], secret.id, use)

    assert _bound_urls(session) == ["https://new.example.org/simple"]


def test_a_secret_created_with_a_use_records_the_url_under_the_lock(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The same on create."""
    index = _indexes(session).create(
        world["admin"], name="corp", url="https://old.example.org/simple"
    )
    use = resolve_use(session, SecretUseKind.PACKAGE_INDEX, "corp")
    session.execute(
        update(PackageIndex)
        .where(PackageIndex.id == index.id)
        .values(url="https://new.example.org/simple")
    )

    _secret(session, "prod", "pypi", use)

    assert _bound_urls(session) == ["https://new.example.org/simple"]


def test_an_index_deleted_before_it_is_locked_is_reported_by_name(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The service refuses to go on with a row another request removed."""
    index = _indexes(session).create(
        world["admin"], name="corp", url="https://corp.example.org/simple"
    )
    session.execute(delete(PackageIndex).where(PackageIndex.id == index.id))
    session.commit()

    with pytest.raises(NotFoundError, match="No package index named 'corp'"):
        _indexes(session)._lock(index)


def test_a_repository_deleted_before_it_is_locked_is_reported_by_name(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """The same for a repository."""
    repository = _repositories(session).create(
        world["admin"], name="r", kind=RepositoryKind.HTTPS, url="https://git.example.org/r.git"
    )
    session.execute(delete(Repository).where(Repository.id == repository.id))
    session.commit()

    with pytest.raises(NotFoundError, match="No repository named 'r'"):
        _repositories(session).lock(repository)


@pytest.mark.parametrize(
    ("kind", "listing"),
    [
        (SecretUseKind.PACKAGE_INDEX, "lakana index list"),
        (SecretUseKind.REPOSITORY, "lakana repo list"),
    ],
)
def test_an_index_or_repository_use_must_name_its_target(
    session: Session, kind: SecretUseKind, listing: str
) -> None:
    """Naming nothing is refused with the command that lists the candidates."""
    with pytest.raises(ValidationError, match=listing):
        resolve_use(session, kind, None)


def test_a_refusal_hides_the_name_of_a_connection_the_caller_may_not_read(
    session: Session, world: dict[str, PrincipalRef]
) -> None:
    """An index or repository manager without READ_ENVIRONMENT learns no connection's name.

    The secret named is bound to connection `hidden-crm` of `prod`; the refusal says "a
    connection of environment 'prod'". An administrator, who may read `prod`, sees the name.
    """
    from lakana_core.connections.kinds import ConnectionKind
    from lakana_core.connections.service import ConnectionService

    _secret(session, "prod", "crm-pw")
    ConnectionService(session, AllowAllAuthorization(), _KEYS).create(
        world["admin"],
        "prod",
        name="hidden-crm",
        kind=ConnectionKind.POSTGRESQL,
        address={"host": "db.example.com", "database": "crm"},
        settings={"user": "reader"},
        secret="crm-pw",
    )
    indexes = PackageIndexService(session, _auditing(session))
    indexes.create(world["manager"], name="a", url="https://a.example.org/simple")
    repositories = _repositories(session)

    refusals = []
    with pytest.raises(PermissionDenied) as caught:
        indexes.update(world["manager"], "a", {"credential_secret": "crm-pw"})
    refusals.append(str(caught.value))
    with pytest.raises(PermissionDenied) as caught:
        repositories.create(
            world["manager"],
            name="r",
            kind=RepositoryKind.HTTPS,
            url="https://git.example.org/r.git",
            credential_secret="crm-pw",
        )
    refusals.append(str(caught.value))

    for text in refusals:
        assert "hidden-crm" not in text
        assert "is bound to a connection of environment 'prod'" in text
    with pytest.raises(PermissionDenied) as admin:
        indexes.update(world["admin"], "a", {"credential_secret": "crm-pw"})
    assert "is bound to connection 'hidden-crm'" in str(admin.value)
