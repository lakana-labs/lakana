"""`DeployKeyService`: an SSH deploy key the server makes, one key pair per environment."""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    SecurableKind,
    SessionContext,
)
from lakana_core.errors import ConflictError, PermissionDenied, StepUpRequired, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Secret,
    SecretUseKind,
)
from lakana_core.projects import deploy_keys as deploy_keys_module
from lakana_core.projects.deploy_keys import (
    DeployKeyService,
    deploy_key_secret_name,
    generate_deploy_key,
)
from lakana_core.projects.probes import key_fingerprint
from lakana_core.projects.repositories import RepositoryService
from lakana_core.secrets.crypto import decrypt_secret_row
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.secrets.uses import SecretUse

_KEYS = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
_HOST_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIIuE9bmUom2wGn1ig3NKAYhOQ46XwTSXMg9/uXafUgYR"
_HOSTS = f"github.com ssh-ed25519 {_HOST_KEY}\n"
_URL = "git@github.com:acme/analytics.git"
_NAME = "analytics-deploy-key"
_ENVIRONMENTS = ("dev", "prod", "staging")


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
    """Three environments, an SSH repository and three people.

    Parameters
    ----------
    session
        Database session.
    admin
        Holds `ADMIN` on the instance.
    devops
        Manages repositories, and secrets in ``dev`` only (and reads ``dev``).
    repoman
        Manages repositories, and no secrets anywhere.
    repo
        The repository ``analytics``.
    """

    session: Session
    admin: PrincipalRef
    devops: PrincipalRef
    repoman: PrincipalRef
    repo: Repository

    def service(self) -> DeployKeyService:
        """Build the service with real grants that record denials, as the server does.

        Returns
        -------
        DeployKeyService
            The service under test.
        """
        authz = AuditingAuthorization(
            GrantAuthorization(self.session), self.session, DenialAuditLimiter(10_000)
        )
        return DeployKeyService(self.session, authz, _KEYS)

    def secrets(self, name: str = _NAME) -> dict[str, Secret]:
        """Read the secrets called `name`, by environment.

        Parameters
        ----------
        name
            The secret name.

        Returns
        -------
        dict
            Environment name → row.
        """
        rows = self.session.execute(
            select(Environment.name, Secret)
            .join(Environment, Environment.id == Secret.environment_id)
            .where(Secret.name == name)
        ).all()
        return {env: secret for env, secret in rows}

    def private_key(self, environment: str, name: str = _NAME) -> str:
        """Decrypt the private key an environment's secret holds.

        Parameters
        ----------
        environment
            The environment.
        name
            The secret name.

        Returns
        -------
        str
            The OpenSSH PEM.
        """
        secret = self.secrets(name)[environment]
        return decrypt_secret_row(secret, _KEYS, environment_name=environment)["private_key"]

    def denials(self) -> list[str]:
        """List the recorded denials' reasons.

        Returns
        -------
        list of str
            One per denial, oldest first.
        """
        events = self.session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
            .order_by(AuditEvent.occurred_at)
        ).scalars()
        return [str(event.detail.get("reason")) for event in events]


@pytest.fixture
def world(session: Session) -> World:
    """Build the three environments, the people and the repository.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    World
        The rows.
    """
    for name in _ENVIRONMENTS:
        session.add(Environment(id=uuid.uuid4(), name=name, storage_root=f"file:///srv/{name}"))
    session.commit()
    admin = _principal(session, "admin")
    devops, repoman = _principal(session, "devops"), _principal(session, "repoman")
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    for name in ("devops", "repoman"):
        grants.grant(
            admin, principal=name, action=Action.MANAGE_REPOSITORY, kind=SecurableKind.INSTANCE
        )
    for action in (Action.MANAGE_SECRET, Action.READ_ENVIRONMENT):
        grants.grant(
            admin,
            principal="devops",
            action=action,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
        )
    repo = RepositoryService(session, AllowAllAuthorization()).create(
        admin, name="analytics", kind=RepositoryKind.SSH, url=_URL, ssh_known_hosts=_HOSTS
    )
    return World(session, admin, devops, repoman, repo)


def _public_of(private_pem: str) -> str:
    """Derive the OpenSSH public key line (type and base64) of an OpenSSH private key.

    Parameters
    ----------
    private_pem
        The private key.

    Returns
    -------
    str
        ``ssh-ed25519 <base64>``.
    """
    key = serialization.load_ssh_private_key(private_pem.encode(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


def test_generated_key_is_ed25519_and_the_pair_matches() -> None:
    """The private key is unencrypted OpenSSH PEM, the public one its ed25519 half and a comment."""
    pair = generate_deploy_key("lakana deploy key for analytics")

    assert pair.private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    kind, blob, comment = pair.public_key.split(" ", 2)
    assert (kind, comment) == ("ssh-ed25519", "lakana deploy key for analytics")
    assert _public_of(pair.private_key) == f"{kind} {blob}"
    assert "PRIVATE" not in repr(pair) and pair.private_key not in repr(pair)
    assert generate_deploy_key("c").public_key != generate_deploy_key("c").public_key


def test_the_default_secret_name() -> None:
    """The secret is named after the repository."""
    assert deploy_key_secret_name("analytics") == "analytics-deploy-key"


def test_each_environment_gets_its_own_key(world: World) -> None:
    """Every environment gets a key pair of its own, bound to the repository, made for it."""
    result = world.service().generate(world.admin, "analytics")

    assert result.secret == _NAME and result.skipped == () and result.replaced == ()
    assert [k.environment for k in result.keys] == list(_ENVIRONMENTS)
    assert all(k.made for k in result.keys)
    assert len({k.public_key for k in result.keys}) == 3
    privates = {env: world.private_key(env) for env in _ENVIRONMENTS}
    assert len(set(privates.values())) == 3
    secrets = world.secrets()
    for key in result.keys:
        secret = secrets[key.environment]
        assert _public_of(privates[key.environment]) == " ".join(key.public_key.split()[:2])
        assert key.public_key.endswith(" lakana deploy key for analytics")
        blob = base64.b64decode(key.public_key.split(" ")[1])
        assert key.fingerprint == key_fingerprint(blob)
        assert (secret.use_kind, secret.use_target_id) == (SecretUseKind.REPOSITORY, world.repo.id)
        assert secret.generated_for == world.repo.id
        assert secret.public_key == key.public_key
    world.session.refresh(world.repo)
    assert world.repo.credential_secret == _NAME


def test_key_is_written_only_where_the_caller_manages_secrets(world: World) -> None:
    """A secret manager of `dev` alone makes dev's key; the others are listed as skipped."""
    result = world.service().generate(world.devops, "analytics")

    assert [k.environment for k in result.keys] == ["dev"]
    assert result.skipped == ("prod", "staging")
    assert set(world.secrets()) == {"dev"}


def test_no_environment_to_manage_is_refused_naming_manage_secret(world: World) -> None:
    """Without `MANAGE_SECRET` anywhere: refused with the next step, audited, nothing written."""
    with pytest.raises(PermissionDenied, match="MANAGE_SECRET") as refused:
        world.service().generate(world.repoman, "analytics")

    assert "ADMIN" in str(refused.value)
    assert world.secrets() == {}
    assert world.denials() == ["no_environment_to_write_deploy_key"]
    world.session.refresh(world.repo)
    assert world.repo.credential_secret is None


@pytest.mark.parametrize("holder", ["unbound", "another repository", "hand-made"])
def test_secret_bound_elsewhere_stops_the_whole_write(world: World, holder: str) -> None:
    """A secret of the name in prod that is not a key made for this repository stops everything."""
    other = RepositoryService(world.session, AllowAllAuthorization()).create(
        world.admin, name="other", kind=RepositoryKind.SSH, url=_URL, ssh_known_hosts=_HOSTS
    )
    use = {
        "unbound": None,
        "another repository": SecretUse.repository(other),
        "hand-made": SecretUse.repository(world.repo),
    }[holder]
    SecretService(world.session, AllowAllAuthorization(), _KEYS).create(
        world.admin, environment="prod", name=_NAME, values={"private_key": "HAND"}, use=use
    )

    with pytest.raises(ConflictError, match="'prod'") as refused:
        world.service().generate(world.admin, "analytics")

    assert "--secret" in str(refused.value)
    assert set(world.secrets()) == {"prod"}
    world.session.refresh(world.repo)
    assert world.repo.credential_secret is None


def test_revoked_key_of_the_default_name_is_refused_naming_another_name(world: World) -> None:
    """A revoked key cannot be made again under its name; `--secret` makes new keys elsewhere."""
    service = world.service()
    service.generate(world.admin, "analytics")
    secrets = SecretService(world.session, AllowAllAuthorization(), _KEYS)
    secrets.revoke(world.admin, world.secrets()["prod"].id)

    with pytest.raises(ConflictError, match="revoked") as refused:
        service.generate(world.admin, "analytics")
    assert "lakana repo deploy-key analytics --secret" in str(refused.value)

    result = service.generate(world.admin, "analytics", secret="analytics-deploy-key-2")
    assert result.secret == "analytics-deploy-key-2"
    assert all(k.made for k in result.keys) and len(result.keys) == 3
    world.session.refresh(world.repo)
    assert world.repo.credential_secret == "analytics-deploy-key-2"
    event = world.session.execute(
        select(AuditEvent)
        .where(AuditEvent.event_type == AuditEventType.DEPLOY_KEY_GENERATED)
        .order_by(AuditEvent.occurred_at.desc())
        .limit(1)
    ).scalar_one()
    assert event.detail["previous_credential_secret"] == _NAME


def test_key_of_a_deleted_repository_with_the_same_name_is_refused(world: World) -> None:
    """A key made for a repository that was removed is never taken over by a new one."""
    world.service().generate(world.admin, "analytics")
    repositories = RepositoryService(world.session, AllowAllAuthorization())
    repositories.delete(world.admin, "analytics")
    repositories.create(
        world.admin, name="analytics", kind=RepositoryKind.SSH, url=_URL, ssh_known_hosts=_HOSTS
    )

    with pytest.raises(ConflictError, match="--secret") as refused:
        world.service().generate(world.admin, "analytics")
    # The page has no secret-name control, so the next step is only the CLI command.
    assert "page" not in str(refused.value)
    assert str(refused.value).endswith(
        "Use another secret name: lakana repo deploy-key analytics --secret OTHER-NAME."
    )


def test_secret_name_option_writes_that_name(world: World) -> None:
    """`secret` names the secret; the repository then names it as its credential."""
    result = world.service().generate(world.admin, "analytics", secret="git-key")

    assert result.secret == "git-key"
    assert set(world.secrets("git-key")) == set(_ENVIRONMENTS)
    world.session.refresh(world.repo)
    assert world.repo.credential_secret == "git-key"


@pytest.mark.parametrize("name", ["has space", "a:b", "x" * 256, ""])
def test_a_secret_name_that_cannot_be_a_credential_is_refused(world: World, name: str) -> None:
    """The name must be one a repository can name as its credential."""
    with pytest.raises(ValidationError, match="secret name"):
        world.service().generate(world.admin, "analytics", secret=name)
    assert world.secrets(name) == {}


def test_a_default_name_that_is_too_long_names_the_option(world: World) -> None:
    """A repository name near the limit leaves no room for the suffix: `--secret` is named."""
    long_name = "r" * 250
    RepositoryService(world.session, AllowAllAuthorization()).create(
        world.admin, name=long_name, kind=RepositoryKind.SSH, url=_URL, ssh_known_hosts=_HOSTS
    )

    with pytest.raises(ValidationError, match="--secret"):
        world.service().generate(world.admin, long_name)


def test_storage_name_collision_in_a_later_environment_writes_nothing(world: World) -> None:
    """Staging uses the name for its storage: refused before dev and prod get a key."""
    staging = world.session.execute(
        select(Environment).where(Environment.name == "staging")
    ).scalar_one()
    staging.storage_secret_ref = _NAME
    world.session.commit()

    with pytest.raises(ConflictError, match="'staging'") as refused:
        world.service().generate(world.admin, "analytics")

    assert "--secret" in str(refused.value)
    assert world.secrets() == {}


def test_a_failing_write_in_the_third_environment_rolls_everything_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error after two keys were added leaves no key, no credential and no audit event."""
    real = deploy_keys_module.insert_secret
    calls: list[str] = []

    def _third_fails(*args: object, **kwargs: object) -> Secret:
        calls.append("x")
        if len(calls) == 3:
            raise RuntimeError("the third write failed")
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(deploy_keys_module, "insert_secret", _third_fails)

    with pytest.raises(RuntimeError):
        world.service().generate(world.admin, "analytics")

    assert world.secrets() == {}
    world.session.refresh(world.repo)
    assert world.repo.credential_secret is None
    generated = world.session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.DEPLOY_KEY_GENERATED)
    ).all()
    assert generated == []


def test_a_commit_that_fails_is_a_conflict_with_a_fixed_message(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent insert of the same name surfaces as a 409 that never quotes the statement."""

    def _clash() -> None:
        raise IntegrityError("INSERT INTO secrets ... ciphertext", {"p": "BOUND"}, Exception())

    monkeypatch.setattr(world.session, "commit", _clash)

    with pytest.raises(ConflictError) as refused:
        world.service().generate(world.admin, "analytics")

    assert "INSERT" not in str(refused.value) and "BOUND" not in str(refused.value)
    assert "try again" in str(refused.value)


def test_second_call_makes_a_key_only_where_none_exists(world: World) -> None:
    """A key already made is listed unchanged; only the environments without one get a key."""
    service = world.service()
    first = service.generate(world.admin, "analytics", environments=["dev"])
    assert [(k.environment, k.made) for k in first.keys] == [("dev", True)]
    dev_private = world.private_key("dev")

    second = service.generate(world.admin, "analytics")

    assert [(k.environment, k.made) for k in second.keys] == [
        ("dev", False),
        ("prod", True),
        ("staging", True),
    ]
    assert second.keys[0].public_key == first.keys[0].public_key
    assert world.private_key("dev") == dev_private


def test_a_call_with_nothing_to_make_changes_nothing(world: World) -> None:
    """When every environment has its key, a call lists them and records nothing new."""
    service = world.service()
    service.generate(world.admin, "analytics")
    before = world.session.execute(select(AuditEvent.id)).all()

    again = service.generate(world.admin, "analytics")

    assert not any(k.made for k in again.keys)
    assert world.session.execute(select(AuditEvent.id)).all() == before


def test_replace_needs_named_environments_and_touches_only_them(world: World) -> None:
    """`replace` without environments is refused; with prod it replaces prod's key alone."""
    service = world.service()
    service.generate(world.admin, "analytics")
    before = {env: world.private_key(env) for env in _ENVIRONMENTS}
    public_before = {env: s.public_key for env, s in world.secrets().items()}

    with pytest.raises(ValidationError, match="--environment"):
        service.generate(world.admin, "analytics", replace=True)

    result = service.generate(world.admin, "analytics", environments=["prod"], replace=True)

    assert [(k.environment, k.made) for k in result.keys] == [("prod", True)]
    assert result.replaced == ("prod",)
    assert result.notes == ("The old key for prod stops working: replace it at your git host now.",)
    after = {env: world.private_key(env) for env in _ENVIRONMENTS}
    assert after["prod"] != before["prod"]
    assert (after["dev"], after["staging"]) == (before["dev"], before["staging"])
    assert world.secrets()["prod"].public_key == result.keys[0].public_key
    assert result.keys[0].public_key != public_before["prod"]


def test_named_environment_the_caller_cannot_manage_is_refused(world: World) -> None:
    """Naming prod without the right to manage its secrets is a recorded denial; nothing changes."""
    with pytest.raises(PermissionDenied):
        world.service().generate(world.devops, "analytics", environments=["dev", "prod"])

    assert world.secrets() == {}
    assert len(world.denials()) == 1


def test_an_unknown_environment_is_not_found(world: World) -> None:
    """A named environment that does not exist is refused before anything is written."""
    from lakana_core.errors import NotFoundError

    with pytest.raises(NotFoundError, match="nowhere"):
        world.service().generate(world.admin, "analytics", environments=["nowhere"])
    assert world.secrets() == {}


def test_new_key_is_refused_while_an_environment_the_caller_cannot_manage_holds_the_old_one(
    world: World,
) -> None:
    """Switching to a new secret name would leave prod and staging on the old key: refused."""
    world.service().generate(world.admin, "analytics")

    with pytest.raises(PermissionDenied, match="'prod'") as refused:
        world.service().generate(world.devops, "analytics", secret="analytics-deploy-key-2")

    assert "Someone who manages secrets in 'prod' makes the key" in str(refused.value)
    assert world.secrets("analytics-deploy-key-2") == {}
    assert world.denials() == ["deploy_key_leaves_an_environment_behind"]
    world.session.refresh(world.repo)
    assert world.repo.credential_secret == _NAME


def test_a_new_name_must_reach_every_environment(world: World) -> None:
    """Switching the name for some environments only would leave the others without a key."""
    world.service().generate(world.admin, "analytics")

    with pytest.raises(ValidationError, match="--environment"):
        world.service().generate(world.admin, "analytics", secret="k2", environments=["dev"])
    assert world.secrets("k2") == {}


def test_rows_in_environments_the_caller_cannot_manage_do_not_stop_the_write(
    world: World,
) -> None:
    """An unbound secret of the name in prod, where devops manages nothing, is left alone."""
    SecretService(world.session, AllowAllAuthorization(), _KEYS).create(
        world.admin, environment="prod", name=_NAME, values={"private_key": "HAND"}
    )

    result = world.service().generate(world.devops, "analytics")

    assert [k.environment for k in result.keys] == ["dev"]
    assert result.skipped == ("prod", "staging")
    prod = world.secrets()["prod"]
    assert (prod.use_kind, prod.generated_for) == (None, None)


def test_deploy_key_needs_step_up(world: World) -> None:
    """A browser session without a fresh passkey touch is refused before anything is written."""
    now = datetime.now(UTC)
    stale = PrincipalRef(
        id=world.admin.id,
        name=world.admin.name,
        type=world.admin.type,
        session=SessionContext(
            session_id=uuid.uuid4(),
            key_id="k",
            via="passkey",
            created_at=now - timedelta(hours=1),
            last_touch_at=now - timedelta(hours=1),
            source_pat_id=None,
            verified_at=now,
        ),
    )

    with pytest.raises(StepUpRequired):
        world.service().generate(stale, "analytics")
    assert world.secrets() == {}


def test_deploy_key_needs_manage_repository(world: World, session: Session) -> None:
    """A person who manages secrets but not repositories is refused."""
    nobody = _principal(session, "nobody")

    with pytest.raises(PermissionDenied):
        world.service().generate(nobody, "analytics")
    assert world.secrets() == {}


def test_repository_without_confirmed_host_keys_is_refused(world: World) -> None:
    """An https repository, or an ssh one without host keys, gets no deploy key."""
    RepositoryService(world.session, AllowAllAuthorization()).create(
        world.admin, name="web", kind=RepositoryKind.HTTPS, url="https://github.com/acme/w.git"
    )
    with pytest.raises(ValidationError, match="ssh"):
        world.service().generate(world.admin, "web")

    world.repo.ssh_known_hosts = None
    world.session.commit()
    with pytest.raises(ValidationError, match="lakana repo host-keys"):
        world.service().generate(world.admin, "analytics")
    assert world.secrets() == {}


def test_another_credential_secret_is_refused_naming_it(world: World) -> None:
    """A repository that names a secret of its own keeps it; the refusal says how to clear it."""
    SecretService(world.session, AllowAllAuthorization(), _KEYS).create(
        world.admin,
        environment="dev",
        name="hand-key",
        values={"private_key": "HAND"},
        use=SecretUse.repository(world.repo),
    )
    RepositoryService(world.session, AllowAllAuthorization()).update(
        world.admin, "analytics", {"credential_secret": "hand-key"}
    )

    with pytest.raises(ConflictError, match="hand-key") as refused:
        world.service().generate(world.admin, "analytics")

    assert "lakana repo update analytics --clear credential_secret" in str(refused.value)
    assert world.secrets() == {}


def test_generation_is_audited_with_fingerprint_and_environments(world: World) -> None:
    """One event names the repository, the secret and each environment's fingerprint, no key."""
    service = world.service()
    service.generate(world.devops, "analytics")
    world.session.expire_all()
    result = service.generate(world.admin, "analytics", environments=["dev"], replace=True)

    events = list(
        world.session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.DEPLOY_KEY_GENERATED)
            .order_by(AuditEvent.occurred_at)
        ).scalars()
    )
    assert [e.securable_kind for e in events] == [SecurableKind.INSTANCE] * 2
    first, second = events[0].detail, events[1].detail
    assert first["repository"] == "analytics" and first["secret"] == _NAME
    assert set(first["made"]) == {"dev"}
    assert first["skipped"] == ["prod", "staging"]
    assert first["kept"] == [] and first["replaced"] == []
    assert first["previous_credential_secret"] is None
    assert second["made"] == {"dev": result.keys[0].fingerprint}
    assert second["replaced"] == ["dev"]
    assert second["previous_credential_secret"] == _NAME
    for event in events:
        text = json.dumps(event.detail)
        assert "ssh-ed25519" not in text and "PRIVATE" not in text


def test_a_generated_deploy_key_cannot_be_bound_elsewhere(world: World) -> None:
    """A key made for a repository is never moved to another use by `secret bind`."""
    world.service().generate(world.admin, "analytics")
    other = RepositoryService(world.session, AllowAllAuthorization()).create(
        world.admin, name="other", kind=RepositoryKind.SSH, url=_URL, ssh_known_hosts=_HOSTS
    )
    secrets = SecretService(world.session, AllowAllAuthorization(), _KEYS)
    dev = world.secrets()["dev"]

    for use in (SecretUse.repository(other), SecretUse(SecretUseKind.ENVIRONMENT)):
        with pytest.raises(ValidationError, match="made for repository 'analytics'"):
            secrets.bind(world.admin, dev.id, use)
    world.session.refresh(dev)
    assert (dev.use_kind, dev.use_target_id) == (SecretUseKind.REPOSITORY, world.repo.id)


def test_a_generated_deploy_key_cannot_be_rotated_by_hand(world: World) -> None:
    """Only `repo deploy-key --replace` replaces a made key; revoking it stays allowed."""
    world.service().generate(world.admin, "analytics")
    secrets = SecretService(world.session, AllowAllAuthorization(), _KEYS)
    dev = world.secrets()["dev"]
    before = world.private_key("dev")

    with pytest.raises(ValidationError, match="--environment dev --replace"):
        secrets.rotate(world.admin, dev.id, values={"private_key": "MINE"})
    assert world.private_key("dev") == before

    secrets.revoke(world.admin, dev.id)
    world.session.refresh(dev)
    assert dev.revoked_at is not None


def test_deploy_keys_are_listed_only_where_the_caller_may_read(world: World) -> None:
    """The public keys of a repository: live, made for it, in environments the reader reads."""
    service = world.service()
    service.generate(world.admin, "analytics")

    everything = service.deploy_keys(world.admin, [world.repo])[world.repo.id]
    assert [k.environment for k in everything] == list(_ENVIRONMENTS)
    assert not any(k.made for k in everything)
    only_dev = service.deploy_keys(world.devops, [world.repo])[world.repo.id]
    assert [k.environment for k in only_dev] == ["dev"]
    assert service.deploy_keys(world.repoman, [world.repo])[world.repo.id] == ()

    SecretService(world.session, AllowAllAuthorization(), _KEYS).revoke(
        world.admin, world.secrets()["prod"].id
    )
    shown = service.deploy_keys(world.admin, [world.repo])[world.repo.id]
    assert [k.environment for k in shown] == ["dev", "staging"]


def test_changing_the_credential_secret_clears_the_public_key(world: World) -> None:
    """Once the repository names another secret (or none), no public key is shown for it."""
    service = world.service()
    service.generate(world.admin, "analytics")

    RepositoryService(world.session, AllowAllAuthorization()).update(
        world.admin, "analytics", {"credential_secret": None}
    )

    assert service.deploy_keys(world.admin, [world.repo])[world.repo.id] == ()


def test_the_listing_reads_every_repository_at_once(world: World) -> None:
    """A repository without a key gets an empty tuple."""
    other = RepositoryService(world.session, AllowAllAuthorization()).create(
        world.admin, name="other", kind=RepositoryKind.SSH, url=_URL, ssh_known_hosts=_HOSTS
    )
    service = world.service()
    service.generate(world.admin, "analytics", environments=["dev"])

    shown = service.deploy_keys(world.admin, [world.repo, other])

    assert [k.environment for k in shown[world.repo.id]] == ["dev"]
    assert shown[other.id] == ()


def test_a_clash_during_the_second_write_is_a_conflict_that_logs_no_ciphertext(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A same-name insert seen at an autoflush is the fixed 409; nothing quotes the statement."""
    real = deploy_keys_module.insert_secret
    calls: list[str] = []

    def _second_clashes(*args: object, **kwargs: object) -> Secret:
        calls.append("x")
        if len(calls) == 2:
            raise IntegrityError(
                "INSERT INTO secrets (ciphertext, nonce) VALUES (?, ?)",
                {"ciphertext": b"CIPHERTEXT-BYTES", "nonce": b"NONCE-BYTES"},
                Exception("duplicate key"),
            )
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(deploy_keys_module, "insert_secret", _second_clashes)

    with caplog.at_level("DEBUG"), pytest.raises(ConflictError) as refused:
        world.service().generate(world.admin, "analytics")

    text = str(refused.value) + caplog.text
    assert "try again" in str(refused.value)
    assert refused.value.__cause__ is None and refused.value.__suppress_context__
    for leaked in ("CIPHERTEXT", "NONCE", "INSERT"):
        assert leaked not in text
    assert world.secrets() == {}


def test_a_name_another_repository_also_uses_does_not_block_a_switch(world: World) -> None:
    """Keys of another repository under the same name are not this repository's credential."""
    other = RepositoryService(world.session, AllowAllAuthorization()).create(
        world.admin, name="other", kind=RepositoryKind.SSH, url=_URL, ssh_known_hosts=_HOSTS
    )
    service = world.service()
    service.generate(world.admin, "analytics", secret="shared", environments=["dev"])
    service.generate(world.admin, "other", secret="shared", environments=["prod"])

    result = service.generate(world.admin, "analytics", secret="analytics-2")

    assert result.secret == "analytics-2"
    world.session.refresh(other)
    assert other.credential_secret == "shared"


def test_a_hand_made_credential_beside_made_keys_is_refused_saying_so(world: World) -> None:
    """A person's own secret of the name, bound to this repository, is named as the cause."""
    service = world.service()
    service.generate(world.admin, "analytics", secret="mixed", environments=["dev"])
    SecretService(world.session, AllowAllAuthorization(), _KEYS).create(
        world.admin,
        environment="prod",
        name="mixed",
        values={"private_key": "HAND"},
        use=SecretUse.repository(world.repo),
    )

    with pytest.raises(ConflictError, match="Lakana did not make") as refused:
        service.generate(world.admin, "analytics", secret="analytics-2")
    assert "'prod'" in str(refused.value)


def test_a_hand_made_credential_where_the_caller_manages_nothing_is_not_named(world: World) -> None:
    """The person's secret is in prod, where devops may not manage secrets: a recorded denial."""
    service = world.service()
    service.generate(world.admin, "analytics", secret="mixed", environments=["dev"])
    SecretService(world.session, AllowAllAuthorization(), _KEYS).create(
        world.admin,
        environment="prod",
        name="mixed",
        values={"private_key": "HAND"},
        use=SecretUse.repository(world.repo),
    )

    with pytest.raises(PermissionDenied) as refused:
        service.generate(world.devops, "analytics", secret="analytics-2")

    assert "prod" not in str(refused.value)
    assert "ADMIN" in str(refused.value)
    assert world.denials() == ["credential_not_made_by_lakana"]
    assert world.secrets("analytics-2") == {}


def test_no_environment_at_all_is_refused_naming_env_create(session: Session) -> None:
    """Without any environment there is nowhere to keep a key; the error names the next step.

    Parameters
    ----------
    session
        Database session, without environments.
    """
    admin = _principal(session, "admin")
    GrantService(session, AllowAllAuthorization()).grant(
        admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    RepositoryService(session, AllowAllAuthorization()).create(
        admin, name="analytics", kind=RepositoryKind.SSH, url=_URL, ssh_known_hosts=_HOSTS
    )
    service = DeployKeyService(session, GrantAuthorization(session), _KEYS)

    with pytest.raises(ValidationError, match="lakana env create"):
        service.generate(admin, "analytics")
    assert session.execute(select(Secret)).scalars().all() == []


def test_a_new_name_for_every_environment_with_a_key_leaves_keyless_ones_alone(
    world: World,
) -> None:
    """Environments that hold no key under the old name do not stop a switch."""
    world.service().generate(world.admin, "analytics", environments=["dev"])

    result = world.service().generate(world.admin, "analytics", secret="k2", environments=["dev"])

    assert result.secret == "k2"
    assert list(world.secrets("k2")) == ["dev"]
    world.session.refresh(world.repo)
    assert world.repo.credential_secret == "k2"


def test_the_listing_asks_once_per_environment_for_several_repositories(world: World) -> None:
    """Two repositories with keys in the same environment are both listed there."""
    other = RepositoryService(world.session, AllowAllAuthorization()).create(
        world.admin, name="other", kind=RepositoryKind.SSH, url=_URL, ssh_known_hosts=_HOSTS
    )
    service = world.service()
    service.generate(world.admin, "analytics", environments=["dev"])
    service.generate(world.admin, "other", environments=["dev"])

    shown = service.deploy_keys(world.devops, [world.repo, other])

    assert [k.environment for k in shown[world.repo.id]] == ["dev"]
    assert [k.environment for k in shown[other.id]] == ["dev"]
    assert shown[world.repo.id][0].fingerprint != shown[other.id][0].fingerprint


def test_the_listing_of_no_repositories_is_empty(world: World) -> None:
    """Listing no repositories asks nothing and returns an empty mapping."""
    assert world.service().deploy_keys(world.admin, []) == {}
