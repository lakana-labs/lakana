"""`RepositoryService`: registration, validation, authorization and audit."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from core_test_helpers import _catalog_with
from sqlalchemy import Engine, delete, event, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
)
from lakana_core.projects.repositories import RepositoryService


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
        A reference to the row.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
    session.commit()
    return ref


@pytest.fixture
def service(session: Session, tmp_path: Path) -> RepositoryService:
    """Build an allow-all service with `tmp_path/repos` as the only root.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    RepositoryService
        The service under test.
    """
    (tmp_path / "repos" / "analytics").mkdir(parents=True)
    return RepositoryService(session, AllowAllAuthorization(), repo_roots=[str(tmp_path / "repos")])


def test_create_https_repository_and_audit_names_only_the_secret(
    session: Session, service: RepositoryService, principal: PrincipalRef
) -> None:
    """The audit event records the secret's name, the default username is stored as NULL."""
    repo = service.create(
        principal,
        name="analytics",
        kind=RepositoryKind.HTTPS,
        url="https://github.com/acme/analytics.git",
        credential_secret="github-token",
        default_ref="main",
    )
    assert repo.credential_username is None
    event = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.REPOSITORY_CREATED)
    ).scalar_one()
    assert event.detail == {
        "name": "analytics",
        "kind": "https",
        "url": "https://github.com/acme/analytics.git",
        "credential_secret": "github-token",
    }


def test_create_local_repository_confined_to_roots(
    service: RepositoryService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A local repository under a root is accepted; outside it is refused."""
    repo = service.create(
        principal,
        name="local",
        kind=RepositoryKind.LOCAL,
        url=str(tmp_path / "repos" / "analytics"),
    )
    assert repo.kind is RepositoryKind.LOCAL
    with pytest.raises(ValidationError, match="not under any"):
        service.create(principal, name="escape", kind=RepositoryKind.LOCAL, url=str(tmp_path))


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"name": "a.b"}, "must not contain a '.'"),
        ({"kind": RepositoryKind.SSH, "url": "git@h:a.git"}, "ssh_known_hosts is required"),
        ({"ssh_known_hosts": "h ssh-ed25519 AAAA"}, "only for ssh"),
        ({"credential_username": "a:b"}, "credential_username"),
        ({"credential_secret": "has space"}, "credential_secret"),
        ({"default_ref": "--upload-pack=x"}, "not a valid git ref"),
        ({"url": "https://u:p@github.com/a/b"}, "credentials"),
    ],
)
def test_create_validates_every_field(
    service: RepositoryService, principal: PrincipalRef, fields: dict, message: str
) -> None:
    """Each field's rule is enforced at registration."""
    arguments = {
        "name": "repo",
        "kind": RepositoryKind.HTTPS,
        "url": "https://github.com/acme/analytics.git",
        **fields,
    }
    with pytest.raises(ValidationError, match=message):
        service.create(principal, **arguments)


def test_local_repository_takes_no_credential(
    service: RepositoryService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A credential on a local repository is meaningless and refused."""
    with pytest.raises(ValidationError, match="local repository takes no credential"):
        service.create(
            principal,
            name="local",
            kind=RepositoryKind.LOCAL,
            url=str(tmp_path / "repos" / "analytics"),
            credential_secret="x",
        )


def test_duplicate_name_is_already_exists(
    service: RepositoryService, principal: PrincipalRef
) -> None:
    """Names are unique."""
    service.create(principal, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git")
    with pytest.raises(AlreadyExistsError):
        service.create(principal, name="r", kind=RepositoryKind.HTTPS, url="https://h/s.git")


def test_update_changes_only_given_fields_and_clears_with_none(
    session: Session, service: RepositoryService, principal: PrincipalRef
) -> None:
    """PATCH semantics: absent fields stay; `None` clears; unknown fields are refused."""
    service.create(
        principal,
        name="r",
        kind=RepositoryKind.HTTPS,
        url="https://h/r.git",
        credential_secret="tok",
        default_ref="main",
    )
    updated = service.update(
        principal, "r", {"default_ref": None, "credential_username": "gitlab+deploy"}
    )
    assert updated.default_ref is None
    assert updated.credential_username == "gitlab+deploy"
    assert updated.credential_secret == "tok"
    with pytest.raises(ValidationError, match="cannot be changed"):
        service.update(principal, "r", {"kind": "ssh"})
    with pytest.raises(ValidationError, match="not a valid git ref"):
        service.update(principal, "r", {"default_ref": "-x"})
    assert session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.REPOSITORY_UPDATED)
    ).scalar_one().detail == {
        "name": "r",
        "changed": ["credential_username", "default_ref"],
        "credential_secret": "tok",
    }


def test_delete_is_refused_while_deployed(
    session: Session, service: RepositoryService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A repository with a current deployment cannot be deleted; without one it can."""
    _catalog_with(session, tmp_path, AllowAllAuthorization())
    repo = service.create(principal, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git")
    env = session.execute(select(Environment)).scalar_one()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env.id,
        commit_sha="a" * 40,
        manifest_json={},
        code_uri="file:///x",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    session.add(
        EnvironmentDeployment(
            environment_id=env.id, repository_id=repo.id, current_job_version_id=version.id
        )
    )
    session.commit()
    with pytest.raises(ConflictError, match="deployed in dev") as refused:
        service.delete(principal, "r")
    message = str(refused.value)
    assert "lakana undeploy r --env dev" in message
    assert "issues/" not in message
    session.query(EnvironmentDeployment).delete()
    session.commit()
    assert session.query(JobVersion).count() == 1
    service.delete(principal, "r")
    assert session.query(JobVersion).count() == 0
    with pytest.raises(NotFoundError):
        service.get(principal, "r")
    assert session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.REPOSITORY_DELETED)
    ).scalar_one().detail == {"name": "r"}


def test_mutations_need_manage_repository(session: Session, tmp_path: Path) -> None:
    """Without MANAGE_REPOSITORY, create, update and delete are all refused."""
    admin = _principal(session, "admin")
    RepositoryService(session, AllowAllAuthorization()).create(
        admin, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git"
    )
    reader = _principal(session, "reader")
    service = RepositoryService(session, GrantAuthorization(session))
    with pytest.raises(PermissionDenied):
        service.create(reader, name="s", kind=RepositoryKind.HTTPS, url="https://h/s.git")
    with pytest.raises(PermissionDenied):
        service.update(reader, "r", {"default_ref": "main"})
    with pytest.raises(PermissionDenied):
        service.delete(reader, "r")
    assert session.execute(select(Repository.name)).scalars().all() == ["r"]
    assert session.execute(select(Repository)).scalar_one().default_ref is None


def test_listing_needs_read_environment_on_any_environment(
    session: Session, tmp_path: Path
) -> None:
    """A reader of one environment may list; a principal with no grants may not."""
    admin = _principal(session, "admin")
    _catalog_with(session, tmp_path, AllowAllAuthorization())
    RepositoryService(session, AllowAllAuthorization()).create(
        admin, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git"
    )
    reader = _principal(session, "reader")
    stranger = _principal(session, "stranger")
    GrantService(session, AllowAllAuthorization()).grant(
        admin,
        principal="reader",
        action=Action.READ_ENVIRONMENT,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    service = RepositoryService(session, GrantAuthorization(session))
    assert [r.name for r in service.list_repositories(reader)] == ["r"]
    assert service.get(reader, "r").name == "r"
    with pytest.raises(PermissionDenied):
        service.list_repositories(stranger)
    with pytest.raises(PermissionDenied):
        service.get(stranger, "r")


def test_manage_repository_alone_may_list(session: Session) -> None:
    """An admin with no environments yet can still see what it registered."""
    admin = _principal(session, "admin")
    GrantService(session, AllowAllAuthorization()).grant(
        admin, principal="admin", action=Action.MANAGE_REPOSITORY, kind=SecurableKind.INSTANCE
    )
    service = RepositoryService(session, GrantAuthorization(session))
    service.create(admin, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git")
    assert [r.name for r in service.list_repositories(admin)] == ["r"]


def test_update_missing_repository_is_not_found(
    service: RepositoryService, principal: PrincipalRef
) -> None:
    """Updating or deleting an unknown repository is a NotFoundError."""
    with pytest.raises(NotFoundError):
        service.update(principal, "nope", {"default_ref": "main"})
    with pytest.raises(NotFoundError):
        service.delete(principal, "nope")


def test_update_cannot_clear_the_url(service: RepositoryService, principal: PrincipalRef) -> None:
    """`url` is mandatory, so clearing it is refused."""
    service.create(principal, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git")
    with pytest.raises(ValidationError, match="url cannot be cleared"):
        service.update(principal, "r", {"url": None})


def test_update_normalizes_sha_ref_and_failed_update_changes_nothing(
    session: Session, service: RepositoryService, principal: PrincipalRef
) -> None:
    """A SHA default ref is lower-cased; an invalid update is rolled back."""
    service.create(principal, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git")
    assert service.update(principal, "r", {"default_ref": "A" * 40}).default_ref == "a" * 40
    with pytest.raises(ValidationError):
        service.update(principal, "r", {"url": "https://u:p@h/r.git"})
    session.expire_all()
    assert service.get(principal, "r").url == "https://h/r.git"


def test_update_url_of_local_repository_is_reconfined(
    service: RepositoryService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Changing a local repository's URL re-checks `LAKANA_REPO_ROOTS`."""
    service.create(
        principal, name="l", kind=RepositoryKind.LOCAL, url=str(tmp_path / "repos" / "analytics")
    )
    with pytest.raises(ValidationError, match="not under any"):
        service.update(principal, "l", {"url": str(tmp_path)})


def test_ssh_known_hosts_rules(service: RepositoryService, principal: PrincipalRef) -> None:
    """Ssh needs non-blank known hosts without NUL; https username is https-only."""
    base = {"name": "s", "kind": RepositoryKind.SSH, "url": "git@h:a.git"}
    with pytest.raises(ValidationError, match="ssh_known_hosts is required"):
        service.create(principal, **base, ssh_known_hosts="   ")
    with pytest.raises(ValidationError, match="too large or contains a NUL"):
        service.create(principal, **base, ssh_known_hosts="h ssh-ed25519 A\x00")
    with pytest.raises(ValidationError, match="too large or contains a NUL"):
        service.create(principal, **base, ssh_known_hosts="x" * (64 * 1024 + 1))
    with pytest.raises(ValidationError, match="only for https"):
        service.create(
            principal, **base, ssh_known_hosts="h ssh-ed25519 AAAA", credential_username="git"
        )
    repo = service.create(principal, **base, ssh_known_hosts="h ssh-ed25519 AAAA")
    assert repo.kind is RepositoryKind.SSH


def test_listing_with_only_manage_repository_skips_environment_checks(
    session: Session, tmp_path: Path
) -> None:
    """An admin with no READ_ENVIRONMENT grant lists even when environments exist."""
    admin = _principal(session, "admin")
    _catalog_with(session, tmp_path, AllowAllAuthorization())
    GrantService(session, AllowAllAuthorization()).grant(
        admin, principal="admin", action=Action.MANAGE_REPOSITORY, kind=SecurableKind.INSTANCE
    )
    service = RepositoryService(session, GrantAuthorization(session))
    assert service.list_repositories(admin) == []


def test_listing_denied_when_environments_exist_but_none_readable(
    session: Session, tmp_path: Path
) -> None:
    """A principal with no grants is refused even when environments exist."""
    stranger = _principal(session, "stranger")
    _catalog_with(session, tmp_path, AllowAllAuthorization())
    service = RepositoryService(session, GrantAuthorization(session))
    with pytest.raises(PermissionDenied):
        service.list_repositories(stranger)


def test_update_with_unparseable_url_is_a_validation_error_and_rolls_back(
    session: Session, service: RepositoryService, principal: PrincipalRef
) -> None:
    """A URL `urlsplit` rejects is a ValidationError, and the row stays clean."""
    service.create(principal, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git")
    with pytest.raises(ValidationError, match="malformed"):
        service.update(principal, "r", {"url": "https://[::1/x"})
    assert not session.dirty
    session.expire_all()
    assert service.get(principal, "r").url == "https://h/r.git"


def test_concurrent_create_loser_is_already_exists(
    session: Session, service: RepositoryService, principal: PrincipalRef
) -> None:
    """A create that loses the race on commit gets AlreadyExists, not a raw IntegrityError."""
    fired = []

    def _rival(sess: Session, *_: object) -> None:
        if fired:
            return
        fired.append(True)
        engine = sess.get_bind()
        assert isinstance(engine, Engine)
        with engine.connect() as rival:
            rival.execute(
                insert(Repository).values(
                    id=uuid.uuid4(), name="r", kind=RepositoryKind.HTTPS, url="https://h/rival.git"
                )
            )
            rival.commit()

    event.listen(session, "before_flush", _rival)
    with pytest.raises(AlreadyExistsError, match="'r'"):
        service.create(principal, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git")
    event.remove(session, "before_flush", _rival)
    assert session.execute(select(Repository.url)).scalars().all() == ["https://h/rival.git"]


def test_an_update_racing_a_delete_is_not_found(
    session: Session, service: RepositoryService, principal: PrincipalRef
) -> None:
    """The row is gone when the update commits; the answer is the service's own.

    A change of the URL or the credential takes the row lock before anything is flushed, so a
    real delete waits for it; a change of another field is first written at the commit.
    """
    service.create(principal, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git")
    fired = []

    def _rival(sess: Session, *_: object) -> None:
        if fired:
            return
        fired.append(True)
        engine = sess.get_bind()
        assert isinstance(engine, Engine)
        with engine.connect() as rival:
            rival.execute(delete(Repository).where(Repository.name == "r"))
            rival.commit()

    event.listen(session, "before_flush", _rival)
    with pytest.raises(NotFoundError, match="No repository named 'r'"):
        service.update(principal, "r", {"default_ref": "main"})
    event.remove(session, "before_flush", _rival)
    assert session.execute(select(Repository.name)).scalars().all() == []
    assert (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.REPOSITORY_UPDATED)
        ).first()
        is None
    )


@pytest.mark.parametrize("changes", [{"url": "https://h/moved.git"}, {"default_ref": "main"}])
def test_an_update_whose_flush_finds_the_row_gone_is_not_found(
    session: Session,
    service: RepositoryService,
    principal: PrincipalRef,
    changes: dict[str, str | None],
) -> None:
    """A `StaleDataError` at any flush of an update (local mode: no row locks) is a 404.

    Parameters
    ----------
    session
        Database session.
    service
        The service under test.
    principal
        The acting principal.
    changes
        The fields to change; a URL change flushes while it checks the binding, another field
        only at the commit.
    """
    service.create(principal, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git")

    def _gone(*_: object) -> None:
        """Fail the way SQLite does when a concurrent delete removed the row.

        Raises
        ------
        StaleDataError
            Always.
        """
        raise StaleDataError("UPDATE statement on table 'repositories' expected to update 1 row")

    event.listen(session, "before_flush", _gone)
    try:
        with pytest.raises(NotFoundError, match="No repository named 'r'"):
            service.update(principal, "r", changes)
    finally:
        event.remove(session, "before_flush", _gone)


def test_commit_failure_that_is_not_a_name_clash_is_a_conflict(
    session: Session, service: RepositoryService, principal: PrincipalRef
) -> None:
    """An IntegrityError with no row of that name (e.g. a concurrent delete) is a ConflictError."""

    def _boom(*_: object) -> None:
        raise IntegrityError("stmt", {}, Exception("secret detail"))

    event.listen(session, "before_commit", _boom)
    with pytest.raises(ConflictError) as raised:
        service.create(principal, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git")
    event.remove(session, "before_commit", _boom)
    assert "secret detail" not in str(raised.value)


def test_create_coerces_a_string_kind(
    session: Session, service: RepositoryService, principal: PrincipalRef
) -> None:
    """A caller passing ``"https"`` gets a repository of that kind, not an enum-less row."""
    repo = service.create(
        principal,
        name="r",
        kind="https",  # ty: ignore[invalid-argument-type]
        url="https://h/r.git",
    )
    assert repo.kind is RepositoryKind.HTTPS
    event_detail = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.REPOSITORY_CREATED)
    ).scalar_one()
    assert event_detail.detail["kind"] == "https"


def test_create_with_an_unknown_kind_is_a_validation_error(
    service: RepositoryService, principal: PrincipalRef
) -> None:
    """A kind that is not local, https or ssh is a 422, not a 500."""
    with pytest.raises(ValidationError, match="kind"):
        service.create(
            principal,
            name="r",
            kind="svn",  # ty: ignore[invalid-argument-type]
            url="https://h/r.git",
        )


def test_an_empty_update_is_refused_and_writes_no_audit_event(
    session: Session, service: RepositoryService, principal: PrincipalRef
) -> None:
    """Nothing to change is a validation error; it must not leave a `changed: []` event."""
    service.create(principal, name="r", kind=RepositoryKind.HTTPS, url="https://h/r.git")
    with pytest.raises(ValidationError, match="Nothing to change"):
        service.update(principal, "r", {})
    assert (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.REPOSITORY_UPDATED)
        ).first()
        is None
    )


def test_a_local_repository_is_refused_when_no_repo_roots_are_configured(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The default (`LAKANA_REPO_ROOTS` empty) admits no local repository at all."""
    service = RepositoryService(session, AllowAllAuthorization())
    with pytest.raises(ValidationError, match="LAKANA_REPO_ROOTS is empty"):
        service.create(principal, name="r", kind=RepositoryKind.LOCAL, url=str(tmp_path))


_ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIIuE9bmUom2wGn1ig3NKAYhOQ46XwTSXMg9/uXafUgYR"
_ED25519_FINGERPRINT = "SHA256:BbeSiQQjnjpybq7nV8S8tEVnQiZNHfceXjKvrLVxecI"


def test_repository_creation_audits_the_trusted_fingerprints(
    session: Session, service: RepositoryService, principal: PrincipalRef
) -> None:
    """Creating an ssh repository, and changing its known hosts, record what is trusted."""
    hosts = (
        "# fetched with lakana repo host-keys\n"
        f"github.com ssh-ed25519 {_ED25519}\n"
        f"@cert-authority *.example.com ssh-ed25519 {_ED25519}\n"
        "broken ssh-rsa not-base64!\n"
    )
    service.create(
        principal,
        name="viassh",
        kind=RepositoryKind.SSH,
        url="git@github.com:acme/a.git",
        ssh_known_hosts=hosts,
    )
    service.update(principal, "viassh", {"ssh_known_hosts": "h ssh-ed25519 AAAA"})
    service.update(principal, "viassh", {"default_ref": "main"})

    created, *updated = session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.event_type.in_(
                [AuditEventType.REPOSITORY_CREATED, AuditEventType.REPOSITORY_UPDATED]
            )
        )
        .order_by(AuditEvent.occurred_at)
    ).scalars()
    assert created.detail["host_key_fingerprints"] == [_ED25519_FINGERPRINT]
    assert len(updated[0].detail["host_key_fingerprints"]) == 1
    assert updated[0].detail["host_key_fingerprints"] != [_ED25519_FINGERPRINT]
    assert "host_key_fingerprints" not in updated[1].detail
