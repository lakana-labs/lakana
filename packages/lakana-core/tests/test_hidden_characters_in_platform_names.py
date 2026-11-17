"""A new repository, group, package index, secret, token or principal name meets the name rules.

A format character such as U+202E RIGHT-TO-LEFT OVERRIDE, a line or paragraph separator, or a
space at either end makes a name look like another one wherever it is shown (#668). Catalog
names are refused the same way (#593, #652). Only a name being created is judged: a stored one
stays readable, changeable and deletable.
"""

from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.errors import ValidationError
from lakana_core.identity.groups import GroupService
from lakana_core.identity.links import invisible_character_problem
from lakana_core.identity.passkeys import _passkey_name
from lakana_core.identity.pats import PatService
from lakana_core.identity.principals import PrincipalService
from lakana_core.identity.tokens import parse_token
from lakana_core.models import (
    Environment,
    Group,
    PackageIndex,
    PersonalAccessToken,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Secret,
)
from lakana_core.projects.package_indexes import PackageIndexService
from lakana_core.projects.repositories import RepositoryService
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService

_HIDDEN = [
    ("analy\u202etics", "U+202E"),
    ("\u200banalytics", "U+200B"),
    ("analytics\ufeff", "U+FEFF"),
    ("analy\u2028tics", "U+2028"),
    ("analy\u2029tics", "U+2029"),
]
_OUTER = [
    (" analytics", "starts"),
    ("analytics\xa0", "ends"),
    ("\u3000analytics ", "starts and ends"),
]


def _admin(session: Session) -> PrincipalRef:
    """Persist an acting principal.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        A reference to the row.
    """
    ref = PrincipalRef(
        id=uuid.uuid4(), name=f"admin-{uuid.uuid4().hex[:8]}", type=PrincipalType.USER
    )
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.commit()
    return ref


def _repositories(tmp_path: Path, session: Session) -> RepositoryService:
    """Build an allow-all repository service.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory, the only repo root.
    session
        Database session.

    Returns
    -------
    RepositoryService
        The service.
    """
    return RepositoryService(session, AllowAllAuthorization(), repo_roots=[str(tmp_path)])


def _create_repository(service: RepositoryService, p: PrincipalRef, name: str) -> None:
    """Register an https repository.

    Parameters
    ----------
    service
        The repository service.
    p
        The acting principal.
    name
        The new name.
    """
    service.create(p, name=name, kind=RepositoryKind.HTTPS, url="https://example.org/a.git")


@pytest.mark.parametrize(("name", "code"), _HIDDEN)
def test_a_new_repository_name_with_a_hidden_character_is_refused(
    session: Session, tmp_path: Path, name: str, code: str
) -> None:
    service = _repositories(tmp_path, session)
    with pytest.raises(
        ValidationError, match=rf"^Repository name '.*' holds {re.escape(code)} at character"
    ):
        _create_repository(service, _admin(session), name)
    assert session.query(Repository).count() == 0


@pytest.mark.parametrize(("name", "where"), _OUTER)
def test_a_new_repository_name_with_an_outer_space_is_refused(
    session: Session, tmp_path: Path, name: str, where: str
) -> None:
    service = _repositories(tmp_path, session)
    with pytest.raises(ValidationError, match=rf"^Repository name .* {where} with a space"):
        _create_repository(service, _admin(session), name)
    assert session.query(Repository).count() == 0


def test_a_stored_repository_name_with_a_hidden_character_stays_usable(
    session: Session, tmp_path: Path
) -> None:
    """A name stored before the rule is shown, changed and deleted as before."""
    admin = _admin(session)
    name = "analy\u202etics"
    session.add(
        Repository(
            id=uuid.uuid4(),
            name=name,
            kind=RepositoryKind.HTTPS,
            url="https://example.org/a.git",
            created_by=admin.id,
        )
    )
    session.commit()
    service = _repositories(tmp_path, session)

    assert service.get(admin, name).name == name
    assert service.update(admin, name, {"default_ref": "main"}).default_ref == "main"
    service.delete(admin, name)
    assert session.query(Repository).count() == 0


@pytest.mark.parametrize(("name", "code"), _HIDDEN)
def test_a_new_group_name_with_a_hidden_character_is_refused(
    session: Session, name: str, code: str
) -> None:
    service = GroupService(session, AllowAllAuthorization())
    with pytest.raises(ValidationError, match=rf"^Group name '.*' holds {re.escape(code)}"):
        service.create_group(_admin(session), name=name)
    assert session.query(Group).count() == 0


@pytest.mark.parametrize(("name", "where"), _OUTER)
def test_a_new_group_name_with_an_outer_space_is_refused(
    session: Session, name: str, where: str
) -> None:
    service = GroupService(session, AllowAllAuthorization())
    with pytest.raises(ValidationError, match=rf"^Group name .* {where} with a space"):
        service.create_group(_admin(session), name=name)
    assert session.query(Group).count() == 0


def test_a_stored_group_name_with_a_hidden_character_stays_usable(session: Session) -> None:
    admin = _admin(session)
    name = "engi\u202eneers"
    session.add(Group(id=uuid.uuid4(), name=name))
    session.commit()
    service = GroupService(session, AllowAllAuthorization())

    service.add_member(admin, name, admin.name)
    assert [member.name for member in service.list_members(admin, name)] == [admin.name]
    service.delete_group(admin, name)
    assert session.query(Group).count() == 0


@pytest.mark.parametrize(("name", "code"), _HIDDEN)
def test_a_new_package_index_name_with_a_hidden_character_is_refused(
    session: Session, name: str, code: str
) -> None:
    service = PackageIndexService(session, AllowAllAuthorization())
    with pytest.raises(ValidationError, match=rf"^Package index name '.*' holds {re.escape(code)}"):
        service.create(_admin(session), name=name, url="https://pkgs.example.org/simple/")
    assert session.query(PackageIndex).count() == 0


@pytest.mark.parametrize(("name", "where"), _OUTER)
def test_a_new_package_index_name_with_an_outer_space_is_refused(
    session: Session, name: str, where: str
) -> None:
    service = PackageIndexService(session, AllowAllAuthorization())
    with pytest.raises(ValidationError, match=rf"^Package index name .* {where} with a space"):
        service.create(_admin(session), name=name, url="https://pkgs.example.org/simple/")
    assert session.query(PackageIndex).count() == 0


def test_a_stored_package_index_name_with_a_hidden_character_stays_usable(
    session: Session,
) -> None:
    admin = _admin(session)
    service = PackageIndexService(session, AllowAllAuthorization())
    service.create(admin, name="team", url="https://pkgs.example.org/simple/")
    name = "te\u202eam"
    session.query(PackageIndex).update({PackageIndex.name: name})
    session.commit()

    assert service.get(admin, name).name == name
    service.delete(admin, name)
    assert session.query(PackageIndex).count() == 0


def _secrets(session: Session) -> SecretService:
    """Build an allow-all secret service over an environment ``dev``.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    SecretService
        The service.
    """
    session.add(Environment(id=uuid.uuid4(), name="dev", storage_root="file:///dev"))
    session.commit()
    keys = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    return SecretService(session, AllowAllAuthorization(), keys)


@pytest.mark.parametrize(
    ("name", "code"),
    [("s3\u200bcreds", "U+200B"), ("s3creds\ufeff", "U+FEFF"), ("s3\xadcreds", "U+00AD")],
)
def test_a_new_secret_name_with_a_format_character_is_refused(
    session: Session, name: str, code: str
) -> None:
    """Bidirectional controls were refused before (#79); every other format character is now."""
    service = _secrets(session)
    with pytest.raises(ValidationError, match=rf"^Secret name '.*' holds {re.escape(code)}"):
        service.create(_admin(session), environment="dev", name=name, values={"k": "v"})
    assert session.query(Secret).count() == 0


@pytest.mark.parametrize(("name", "where"), [("\xa0s3creds", "starts"), ("s3creds\u3000", "ends")])
def test_a_new_secret_name_with_an_outer_space_is_refused(
    session: Session, name: str, where: str
) -> None:
    """An ASCII space was refused before; any other Unicode space at an end is now."""
    service = _secrets(session)
    with pytest.raises(ValidationError, match=rf"^Secret name .* {where} with a space"):
        service.create(_admin(session), environment="dev", name=name, values={"k": "v"})
    assert session.query(Secret).count() == 0


def test_a_stored_secret_name_with_a_hidden_character_stays_usable(session: Session) -> None:
    """A secret named before the rule is listed, rotated and revoked as before."""
    admin = _admin(session)
    service = _secrets(session)
    created = service.create(admin, environment="dev", name="s3creds", values={"k": "v"})
    name = "s3\u200bcreds"
    session.query(Secret).update({Secret.name: name})
    session.commit()

    assert [secret.name for secret in service.list_for_environment(admin, "dev")] == [name]
    assert service.rotate(admin, created.id, values={"k": "w"}).name == name
    service.revoke(admin, created.id)
    assert service.list_for_environment(admin, "dev") == []


def test_a_stored_token_name_with_a_hidden_character_stays_usable(session: Session) -> None:
    """A token named before the rule is listed, signs in and is revoked as before."""
    admin = _admin(session)
    service = PatService(session, AllowAllAuthorization())
    issued = service.issue(admin, admin.name, name="laptop")
    name = "lap\u202etop"
    session.query(PersonalAccessToken).update({PersonalAccessToken.name: name})
    session.commit()

    assert [token.name for token in service.list(admin, admin.name)] == [name]
    parsed = parse_token(issued.token, "lakana_pat")
    assert parsed is not None
    assert service.verify(issued.key_id, parsed[1]).id == admin.id
    service.revoke(admin, admin.name, issued.key_id)
    assert all(token.revoked_at is not None for token in service.list(admin, admin.name))


@pytest.mark.parametrize(("name", "code"), _HIDDEN)
def test_a_new_token_name_with_a_hidden_character_is_refused(
    session: Session, name: str, code: str
) -> None:
    admin = _admin(session)
    service = PatService(session, AllowAllAuthorization())
    with pytest.raises(ValidationError, match=rf"^Token name '.*' holds {re.escape(code)}"):
        service.issue(admin, admin.name, name=name)
    assert service.list(admin, admin.name) == []


@pytest.mark.parametrize(("name", "where"), _OUTER)
def test_a_new_token_name_with_an_outer_space_is_refused(
    session: Session, name: str, where: str
) -> None:
    admin = _admin(session)
    service = PatService(session, AllowAllAuthorization())
    with pytest.raises(ValidationError, match=rf"^Token name .* {where} with a space"):
        service.issue(admin, admin.name, name=name)
    assert service.list(admin, admin.name) == []


@pytest.mark.parametrize(("name", "code"), [("ali\u2028ce", "U+2028"), ("ali\u2029ce", "U+2029")])
def test_a_new_principal_name_with_a_line_separator_is_refused(name: str, code: str) -> None:
    """Control and format characters were refused before; line separators are now."""
    problem = invisible_character_problem(name)
    assert problem is not None
    assert f"({code})" in problem


@pytest.mark.parametrize(("name", "where"), [(" alice", "starts"), ("alice\xa0", "ends")])
def test_a_new_principal_name_with_an_outer_space_is_refused(
    session: Session, name: str, where: str
) -> None:
    """An invite refused it before; creating a principal directly refuses it now too."""
    service = PrincipalService(session, AllowAllAuthorization())
    with pytest.raises(ValidationError, match=rf"^Principal name .* {where} with a space"):
        service.create_principal(_admin(session), name=name, type=PrincipalType.SERVICE_ACCOUNT)
    assert session.query(Principal).filter(Principal.name == name).count() == 0


@pytest.mark.parametrize("name", ["Mac\u2028Book", "Mac\u2029Book"])
def test_a_passkey_name_with_a_line_separator_is_refused(name: str) -> None:
    """Control and format characters were refused before; line separators are now."""
    with pytest.raises(ValidationError, match=re.escape("no control or format characters")):
        _passkey_name(name)
