"""`PackageIndexService`: registration, validation, authorization and audit."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from core_test_helpers import _catalog_with
from sqlalchemy import delete, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    SecurableKind,
    TaskScope,
)
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
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
    Base,
    PackageIndex,
    Principal,
    PrincipalType,
)
from lakana_core.projects.package_indexes import PackageIndexService, registered_indexes
from lakana_core.projects.package_sources import RegisteredIndex

_URL = "https://pkgs.example.org/simple"


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


def _events(session: Session, kind: AuditEventType) -> list[dict]:
    """Read the details of every audit event of one type.

    Parameters
    ----------
    session
        Database session.
    kind
        The event type.

    Returns
    -------
    list of dict
        Each event's `detail`, oldest first.
    """
    rows = session.execute(select(AuditEvent).where(AuditEvent.event_type == kind)).scalars()
    return [row.detail for row in rows]


@pytest.fixture
def service(session: Session) -> PackageIndexService:
    """Build an allow-all service.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PackageIndexService
        The service under test.
    """
    return PackageIndexService(session, AllowAllAuthorization())


@pytest.fixture
def admin(session: Session) -> PrincipalRef:
    """Persist the acting principal.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        A reference to the row.
    """
    return _principal(session, "admin")


def test_create_stores_the_normalized_url_and_audits_names_only(
    session: Session, service: PackageIndexService, admin: PrincipalRef
) -> None:
    row = service.create(
        admin,
        name="team",
        url="https://Pkgs.Example.org:443/simple/",
        credential_secret="team-index",
    )
    assert (row.url, row.created_by) == (_URL, admin.id)
    assert _events(session, AuditEventType.PACKAGE_INDEX_CREATED) == [
        {"name": "team", "url": _URL, "credential_secret": "team-index"}
    ]
    assert registered_indexes(session) == (RegisteredIndex("team", _URL, "team-index", row.id),)


def test_an_index_needs_no_credential(
    session: Session, service: PackageIndexService, admin: PrincipalRef
) -> None:
    row = service.create(admin, name="open", url=_URL)
    assert row.credential_secret is None
    assert registered_indexes(session) == (RegisteredIndex("open", _URL, None, row.id),)


def test_a_second_spelling_of_a_registered_url_is_refused_and_names_the_index(
    service: PackageIndexService, admin: PrincipalRef
) -> None:
    service.create(admin, name="team", url=_URL)
    with pytest.raises(AlreadyExistsError) as caught:
        service.create(admin, name="other", url="https://PKGS.example.org/simple/")
    assert str(caught.value) == (
        "The package index 'team' already has this URL. Use that index, or remove it first "
        "(`lakana index remove team`)."
    )
    with pytest.raises(AlreadyExistsError, match="A package index named 'team' already exists"):
        service.create(admin, name="team", url="https://other.example.org/simple")


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"name": "a.b"}, "Package index name"),
        ({"name": ""}, "Package index name must not be empty."),
        ({"name": "n" * 256}, "longer than 255 characters"),
        ({"url": "https://u:p@pkgs.example.org/simple"}, "must not contain a user name"),
        ({"url": "http://pkgs.example.org/simple"}, "must use https"),
        ({"credential_secret": "two words"}, "credential_secret must be a secret name"),
        ({"credential_secret": ""}, "credential_secret must be a secret name"),
        # A lone surrogate cannot be encoded for PostgreSQL: refused, never a server error (#79).
        ({"credential_secret": "team\ud800"}, "credential_secret must be a secret name"),
        ({"credential_secret": "team\u202eindex"}, "credential_secret must be a secret name"),
    ],
)
def test_create_validates_every_field(
    session: Session,
    service: PackageIndexService,
    admin: PrincipalRef,
    fields: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        service.create(admin, **{"name": "team", "url": _URL, **fields})
    assert session.execute(select(PackageIndex)).first() is None
    assert _events(session, AuditEventType.PACKAGE_INDEX_CREATED) == []


def test_update_changes_only_given_fields_and_clears_the_secret_with_none(
    session: Session, service: PackageIndexService, admin: PrincipalRef
) -> None:
    service.create(admin, name="team", url=_URL, credential_secret="team-index")
    row = service.update(admin, "team", {"url": "https://New.example.org/simple/"})
    assert (row.url, row.credential_secret) == ("https://new.example.org/simple", "team-index")
    row = service.update(admin, "team", {"credential_secret": None})
    assert (row.url, row.credential_secret) == ("https://new.example.org/simple", None)
    assert _events(session, AuditEventType.PACKAGE_INDEX_UPDATED) == [
        {
            "name": "team",
            "changed": ["url"],
            "url": "https://new.example.org/simple",
            "credential_secret": "team-index",
            "previous": {"url": _URL},
        },
        {
            "name": "team",
            "changed": ["credential_secret"],
            "url": "https://new.example.org/simple",
            "credential_secret": None,
            "previous": {"credential_secret": "team-index"},
        },
    ]


def test_an_update_that_changes_nothing_records_no_event_and_lists_only_real_changes(
    session: Session, service: PackageIndexService, admin: PrincipalRef
) -> None:
    """The same URL in another spelling, or the same secret, is not a change (#80)."""
    service.create(admin, name="team", url=_URL, credential_secret="team-index")
    row = service.update(
        admin,
        "team",
        {"url": "https://PKGS.example.org/simple/", "credential_secret": "team-index"},
    )
    assert (row.url, row.credential_secret) == (_URL, "team-index")
    assert _events(session, AuditEventType.PACKAGE_INDEX_UPDATED) == []
    service.update(admin, "team", {"url": _URL, "credential_secret": "other-index"})
    assert _events(session, AuditEventType.PACKAGE_INDEX_UPDATED) == [
        {
            "name": "team",
            "changed": ["credential_secret"],
            "url": _URL,
            "credential_secret": "other-index",
            "previous": {"credential_secret": "team-index"},
        }
    ]


def test_an_update_audit_event_holds_the_old_and_the_new_value_of_each_changed_field(
    session: Session, service: PackageIndexService, admin: PrincipalRef
) -> None:
    """The event says what an index was before as well as what it became; names only."""
    service.create(admin, name="team", url=_URL, credential_secret="old-secret")
    service.update(
        admin,
        "team",
        {"url": "https://New.example.org/simple/", "credential_secret": "new-secret"},
    )
    assert _events(session, AuditEventType.PACKAGE_INDEX_UPDATED) == [
        {
            "name": "team",
            "changed": ["credential_secret", "url"],
            "url": "https://new.example.org/simple",
            "credential_secret": "new-secret",
            "previous": {"url": _URL, "credential_secret": "old-secret"},
        }
    ]


@pytest.mark.parametrize(
    ("changes", "error", "message"),
    [
        ({}, ValidationError, "Nothing to change; name at least one field."),
        ({"name": "x"}, ValidationError, "name cannot be changed."),
        ({"url": None}, ValidationError, "url cannot be cleared."),
        ({5: "x"}, ValidationError, "cannot be changed"),
        ({"url": 5}, ValidationError, "URL must be text"),
        ({"url": ["https://x.example.org/simple"]}, ValidationError, "URL must be text"),
        ({"credential_secret": 5}, ValidationError, "credential_secret must be a secret name"),
        ({"credential_secret": b"x"}, ValidationError, "credential_secret must be a secret name"),
        ({"url": _URL, "color": "red"}, ValidationError, "color cannot be changed"),
        ({"url": "http://pkgs.example.org/simple"}, ValidationError, "must use https"),
        (
            {"credential_secret": "two words"},
            ValidationError,
            "credential_secret must be a secret name with no spaces.",
        ),
        ({"url": "https://OTHER.example.org/simple/"}, AlreadyExistsError, "'other'"),
    ],
)
def test_update_refusals_change_nothing(
    session: Session,
    service: PackageIndexService,
    admin: PrincipalRef,
    changes: dict,
    error: type[Exception],
    message: str,
) -> None:
    service.create(admin, name="team", url=_URL, credential_secret="team-index")
    service.create(admin, name="other", url="https://other.example.org/simple")
    with pytest.raises(error, match=message):
        service.update(admin, "team", changes)
    session.expire_all()
    row = service.get(admin, "team")
    assert (row.url, row.credential_secret) == (_URL, "team-index")
    assert _events(session, AuditEventType.PACKAGE_INDEX_UPDATED) == []


_TESTPYPI = "https://test.pypi.org/simple"
_TESTPYPI_REFUSAL = (
    "The package index URL is TestPyPI's address. TestPyPI needs no credential for downloads; "
    "register it without one (leave out --credential-secret)."
)


def test_testpypi_is_registered_without_a_credential_and_refused_with_one(
    session: Session, service: PackageIndexService, admin: PrincipalRef
) -> None:
    with pytest.raises(ValidationError) as caught:
        service.create(admin, name="testpypi", url=_TESTPYPI, credential_secret="team-index")
    assert str(caught.value) == _TESTPYPI_REFUSAL
    assert session.execute(select(PackageIndex)).first() is None
    row = service.create(admin, name="testpypi", url=_TESTPYPI + "/")
    assert row.url == _TESTPYPI
    assert registered_indexes(session) == (RegisteredIndex("testpypi", _TESTPYPI, None, row.id),)


@pytest.mark.parametrize(
    ("first", "changes"),
    [
        ({"url": _TESTPYPI}, {"credential_secret": "team-index"}),
        ({"url": _TESTPYPI}, {"url": _TESTPYPI + "/", "credential_secret": "team-index"}),
        ({"url": _URL, "credential_secret": "team-index"}, {"url": _TESTPYPI}),
        ({"url": _URL}, {"url": _TESTPYPI, "credential_secret": "team-index"}),
    ],
)
def test_no_update_leaves_testpypi_with_a_credential(
    session: Session,
    service: PackageIndexService,
    admin: PrincipalRef,
    first: dict[str, str],
    changes: dict[str, str | None],
) -> None:
    service.create(admin, name="team", **first)
    with pytest.raises(ValidationError) as caught:
        service.update(admin, "team", changes)
    assert str(caught.value) == _TESTPYPI_REFUSAL
    session.expire_all()
    row = service.get(admin, "team")
    assert (row.url, row.credential_secret) == (first["url"], first.get("credential_secret"))
    assert _events(session, AuditEventType.PACKAGE_INDEX_UPDATED) == []


def test_an_update_may_move_an_index_to_testpypi_when_it_drops_the_credential(
    service: PackageIndexService, admin: PrincipalRef
) -> None:
    service.create(admin, name="team", url=_URL, credential_secret="team-index")
    row = service.update(admin, "team", {"url": _TESTPYPI, "credential_secret": None})
    assert (row.url, row.credential_secret) == (_TESTPYPI, None)
    row = service.update(admin, "team", {"url": _URL, "credential_secret": "team-index"})
    assert (row.url, row.credential_secret) == (_URL, "team-index")


def test_a_public_host_is_refused_on_create_and_update_whatever_the_credential(
    service: PackageIndexService, admin: PrincipalRef
) -> None:
    service.create(admin, name="team", url=_URL)
    for secret in (None, "team-index"):
        with pytest.raises(ValidationError, match="PyPI's own address"):
            service.create(
                admin, name="pypi", url="https://pypi.org/simple", credential_secret=secret
            )
        with pytest.raises(ValidationError, match="PyPI's own address"):
            service.update(
                admin,
                "team",
                {"url": "https://files.pythonhosted.org", "credential_secret": secret},
            )


def test_updating_an_unknown_index_is_not_found(
    service: PackageIndexService, admin: PrincipalRef
) -> None:
    with pytest.raises(NotFoundError, match="No package index named 'team'"):
        service.update(admin, "team", {"url": _URL})
    with pytest.raises(NotFoundError, match="No package index named 'team'"):
        service.get(admin, "team")


def test_an_index_may_keep_its_own_url_on_update(
    service: PackageIndexService, admin: PrincipalRef
) -> None:
    service.create(admin, name="team", url=_URL)
    assert service.update(admin, "team", {"url": _URL + "/"}).url == _URL


def test_delete_removes_the_row_and_audits_name_and_url(
    session: Session, service: PackageIndexService, admin: PrincipalRef
) -> None:
    service.create(admin, name="team", url=_URL)
    service.delete(admin, "team")
    assert session.execute(select(PackageIndex)).first() is None
    assert _events(session, AuditEventType.PACKAGE_INDEX_DELETED) == [{"name": "team", "url": _URL}]
    with pytest.raises(NotFoundError):
        service.delete(admin, "team")
    assert service.list_indexes(admin) == []


def test_mutations_need_manage_package_index(session: Session, admin: PrincipalRef) -> None:
    PackageIndexService(session, AllowAllAuthorization()).create(admin, name="team", url=_URL)
    stranger = _principal(session, "stranger")
    service = PackageIndexService(session, GrantAuthorization(session))
    with pytest.raises(PermissionDenied):
        service.create(stranger, name="other", url="https://other.example.org/simple")
    for name in ("team", "missing"):
        with pytest.raises(PermissionDenied):
            service.update(stranger, name, {"credential_secret": None})
        with pytest.raises(PermissionDenied):
            service.delete(stranger, name)
    assert session.execute(select(PackageIndex.name)).scalars().all() == ["team"]


def test_reading_needs_read_environment_somewhere_or_the_action(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """A project author sees names and URLs; a stranger sees nothing."""
    _catalog_with(session, tmp_path, AllowAllAuthorization())
    PackageIndexService(session, AllowAllAuthorization()).create(admin, name="team", url=_URL)
    reader, stranger, manager = (
        _principal(session, name) for name in ("reader", "stranger", "manager")
    )
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(
        admin,
        principal="reader",
        action=Action.READ_ENVIRONMENT,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    grants.grant(admin, principal="manager", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    service = PackageIndexService(session, GrantAuthorization(session))
    assert [row.name for row in service.list_indexes(reader)] == ["team"]
    assert service.get(reader, "team").url == _URL
    assert [row.name for row in service.list_indexes(manager)] == ["team"]
    for call in (lambda: service.list_indexes(stranger), lambda: service.get(stranger, "team")):
        with pytest.raises(PermissionDenied):
            call()
    with pytest.raises(PermissionDenied):
        service.get(stranger, "missing")


def test_a_task_token_may_not_read_the_registry_even_with_read_environment(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """A task has no use for the registry; its token's environment grant does not open it."""
    _catalog_with(session, tmp_path, AllowAllAuthorization())
    PackageIndexService(session, AllowAllAuthorization()).create(admin, name="team", url=_URL)
    scope = TaskScope(environment="dev", asset_scope=["*"], token_id=uuid.uuid4())
    task = dataclasses.replace(admin, task_scope=scope)
    GrantService(session, AllowAllAuthorization()).grant(
        admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    for authz in (GrantAuthorization(session), AllowAllAuthorization()):
        service = PackageIndexService(session, authz)
        with pytest.raises(PermissionDenied):
            service.list_indexes(task)
        with pytest.raises(PermissionDenied):
            service.get(task, "team")
    assert PackageIndexService(session, GrantAuthorization(session)).get(admin, "team")


@pytest.mark.parametrize("name", ["a\x00b", "x" * 256, ""], ids=["nul", "too-long", "empty"])
@pytest.mark.parametrize("verb", ["get", "update", "delete"])
def test_a_name_that_could_never_be_created_is_refused_before_the_lookup(
    service: PackageIndexService, admin: PrincipalRef, verb: str, name: str
) -> None:
    calls = {
        "get": lambda: service.get(admin, name),
        "update": lambda: service.update(admin, name, {"credential_secret": None}),
        "delete": lambda: service.delete(admin, name),
    }
    with pytest.raises(ValidationError):
        calls[verb]()


def test_the_name_check_comes_after_authorization(session: Session, admin: PrincipalRef) -> None:
    stranger = _principal(session, "stranger")
    service = PackageIndexService(session, GrantAuthorization(session))
    with pytest.raises(PermissionDenied):
        service.get(stranger, "a\x00b")
    with pytest.raises(PermissionDenied):
        service.update(stranger, "a\x00b", {"credential_secret": None})
    with pytest.raises(PermissionDenied):
        service.delete(stranger, "a\x00b")


def test_a_missing_name_is_shown_escaped_and_cut(
    service: PackageIndexService, admin: PrincipalRef
) -> None:
    name = "é\x1b[2J" + "y" * 240
    with pytest.raises(NotFoundError) as caught:
        service.get(admin, name)
    text = str(caught.value)
    assert "\x1b" not in text
    assert "\\x1b" in text and "\\xe9" in text
    assert len(text) < 300


def test_a_lost_race_on_the_name_or_the_url_is_already_exists(
    session: Session,
    service: PackageIndexService,
    admin: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loser of two concurrent creates gets the same answer as a late one."""
    answers = iter([None, "A package index named 'team' already exists."])

    def clash(name: str, url: str, *, except_id: uuid.UUID | None = None) -> str | None:
        """Find nothing before the commit and the winner's row after it.

        Parameters
        ----------
        name
            Ignored.
        url
            Ignored.
        except_id
            Ignored.

        Returns
        -------
        str or None
            The next scripted answer.
        """
        return next(answers)

    def commit() -> None:
        """Fail as the unique constraint does for the loser.

        Raises
        ------
        IntegrityError
            Always.
        """
        raise IntegrityError("INSERT", {}, Exception("unique"))

    monkeypatch.setattr(service, "_clash", clash)
    monkeypatch.setattr(session, "commit", commit)
    with pytest.raises(AlreadyExistsError, match="A package index named 'team' already exists"):
        service.create(admin, name="team", url=_URL)


def test_a_commit_failure_that_is_no_collision_is_a_conflict(
    session: Session,
    service: PackageIndexService,
    admin: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def commit() -> None:
        """Fail for a reason that is no collision.

        Raises
        ------
        IntegrityError
            Always.
        """
        raise IntegrityError("INSERT", {}, Exception("secret detail"))

    monkeypatch.setattr(session, "commit", commit)
    with pytest.raises(ConflictError) as caught:
        service.create(admin, name="team", url=_URL)
    assert str(caught.value) == (
        "Could not register package index 'team': the write failed for a reason other than a "
        "name or URL collision."
    )
    assert "secret detail" not in str(caught.value)


def test_an_update_that_fails_to_commit_is_a_conflict_or_already_exists(
    session: Session,
    service: PackageIndexService,
    admin: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.create(admin, name="team", url=_URL)
    service.create(admin, name="other", url="https://other.example.org/simple")

    def commit() -> None:
        """Fail as the unique constraint does.

        Raises
        ------
        IntegrityError
            Always.
        """
        raise IntegrityError("UPDATE", {}, Exception("unique"))

    monkeypatch.setattr(session, "commit", commit)
    with pytest.raises(ConflictError, match="Could not update package index 'team'"):
        service.update(admin, "team", {"credential_secret": "x"})
    other = "https://third.example.org/simple"
    answers = iter([None, "The package index 'other' already has this URL."])
    monkeypatch.setattr(service, "_clash", lambda *a, **k: next(answers))
    with pytest.raises(AlreadyExistsError, match="'other' already has this URL"):
        service.update(admin, "team", {"url": other})


def test_a_name_that_needs_quoting_is_quoted_in_the_command_the_message_suggests(
    service: PackageIndexService, admin: PrincipalRef
) -> None:
    """Review I4: the name rule allows spaces and shell characters; the command is pasted."""
    service.create(admin, name="a b; rm -rf ~", url=_URL)
    with pytest.raises(AlreadyExistsError) as caught:
        service.create(admin, name="other", url=_URL)
    assert str(caught.value) == (
        "The package index 'a b; rm -rf ~' already has this URL. Use that index, or remove it "
        "first (`lakana index remove 'a b; rm -rf ~'`)."
    )


@pytest.fixture
def two_sessions(tmp_path: Path) -> Iterator[tuple[Session, Session]]:
    """Provide two sessions on one file-backed SQLite database.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Yields
    ------
    tuple of (Session, Session)
        The loser's session and the winner's session.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path / 'race.db'}"))
    Base.metadata.create_all(engine)
    with Session(engine) as loser, Session(engine) as winner:
        yield loser, winner
    engine.dispose()


def _win_after_the_check(
    service: PackageIndexService, monkeypatch: pytest.MonkeyPatch, winner: Callable[[], object]
) -> None:
    """Let `winner` commit right after the service's first look for a collision.

    Parameters
    ----------
    service
        The loser's service.
    monkeypatch
        Pytest's monkeypatch.
    winner
        Commits the winner's row on the other session.
    """
    original = service._clash
    calls = []

    def clash(name: str, url: str, *, except_id: uuid.UUID | None = None) -> str | None:
        """Look for a collision, and on the first look let the winner commit.

        Parameters
        ----------
        name
            The name being registered.
        url
            The URL being registered.
        except_id
            The row being updated.

        Returns
        -------
        str or None
            What the real check found.
        """
        found = original(name, url, except_id=except_id)
        if not calls:
            winner()
        calls.append(1)
        return found

    monkeypatch.setattr(service, "_clash", clash)


@pytest.mark.parametrize(
    ("name", "url", "message"),
    [
        ("team", "https://other.example.org/simple", "A package index named 'team' already exists"),
        ("late", _URL, "The package index 'team' already has this URL"),
    ],
)
def test_the_loser_of_a_real_create_race_gets_already_exists(
    two_sessions: tuple[Session, Session],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    url: str,
    message: str,
) -> None:
    """Review I5: the rollback in the commit path turns a unique violation into a clear answer."""
    loser, winner = two_sessions
    admin = _principal(loser, "admin")
    service = PackageIndexService(loser, AllowAllAuthorization())
    _win_after_the_check(
        service,
        monkeypatch,
        lambda: (
            winner.add(PackageIndex(id=uuid.uuid4(), name="team", url=_URL)),
            winner.commit(),
        ),
    )
    with pytest.raises(AlreadyExistsError, match=message):
        service.create(admin, name=name, url=url)
    assert _events(loser, AuditEventType.PACKAGE_INDEX_CREATED) == []
    assert loser.execute(select(PackageIndex.name)).scalars().all() == ["team"]


def test_the_loser_of_a_real_update_race_gets_already_exists(
    two_sessions: tuple[Session, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    loser, winner = two_sessions
    admin = _principal(loser, "admin")
    service = PackageIndexService(loser, AllowAllAuthorization())
    service.create(admin, name="team", url=_URL)
    moved = "https://moved.example.org/simple"
    _win_after_the_check(
        service,
        monkeypatch,
        lambda: (
            winner.add(PackageIndex(id=uuid.uuid4(), name="rival", url=moved)),
            winner.commit(),
        ),
    )
    with pytest.raises(AlreadyExistsError, match="The package index 'rival' already has this URL"):
        service.update(admin, "team", {"url": moved})
    assert _events(loser, AuditEventType.PACKAGE_INDEX_UPDATED) == []
    assert service.get(admin, "team").url == _URL


def test_an_update_racing_a_delete_of_the_same_index_is_not_found(
    two_sessions: tuple[Session, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is gone when the update commits; the answer is the service's own."""
    loser, winner = two_sessions
    admin = _principal(loser, "admin")
    service = PackageIndexService(loser, AllowAllAuthorization())
    service.create(admin, name="team", url=_URL)
    _win_after_the_check(
        service,
        monkeypatch,
        lambda: (
            winner.execute(delete(PackageIndex).where(PackageIndex.name == "team")),
            winner.commit(),
        ),
    )
    with pytest.raises(NotFoundError, match="No package index named 'team'"):
        service.update(admin, "team", {"url": "https://moved.example.org/simple"})
    assert _events(loser, AuditEventType.PACKAGE_INDEX_UPDATED) == []
    assert loser.execute(select(PackageIndex.name)).scalars().all() == []


@pytest.mark.parametrize(
    "changes", [{"url": "https://moved.example.org/simple"}, {"credential_secret": None}]
)
def test_an_update_whose_flush_finds_the_row_gone_is_not_found(
    session: Session,
    service: PackageIndexService,
    admin: PrincipalRef,
    changes: dict[str, str | None],
) -> None:
    """A `StaleDataError` at any flush of an update (local mode: no row locks) is a 404.

    Parameters
    ----------
    session
        Database session.
    service
        The service under test.
    admin
        The acting principal.
    changes
        The fields to change.
    """
    # With a secret, so that clearing it is a change and reaches the flush.
    service.create(admin, name="team", url=_URL, credential_secret="team-index")

    def _gone(*_: object) -> None:
        """Fail the way SQLite does when a concurrent delete removed the row.

        Raises
        ------
        StaleDataError
            Always.
        """
        raise StaleDataError("UPDATE statement on table 'package_indexes' expected to update 1 row")

    event.listen(session, "before_flush", _gone)
    try:
        with pytest.raises(NotFoundError, match="No package index named 'team'"):
            service.update(admin, "team", changes)
    finally:
        event.remove(session, "before_flush", _gone)
