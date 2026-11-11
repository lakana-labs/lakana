"""Home is a person's page: a task or CI token, even of an administrator, gets none of it."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from home_test_helpers import environment, grant, person
from sqlalchemy.orm import Session

from lakana_core.auth import CiScope, PrincipalRef, TaskScope
from lakana_core.errors import PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.home.attention import AttentionService
from lakana_core.home.get_started import get_started
from lakana_core.home.work import your_work
from lakana_core.jobs.service import JobService
from lakana_core.runs.service import RunService


def _tokens(admin: PrincipalRef) -> list[PrincipalRef]:
    """Build a task token's and a CI token's reference of the administrator.

    Parameters
    ----------
    admin
        The administrator.

    Returns
    -------
    list of PrincipalRef
        The task token's, then the CI token's.
    """
    task = PrincipalRef(
        id=admin.id,
        name=admin.name,
        type=admin.type,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=uuid.uuid4()),
    )
    ci = PrincipalRef(
        id=admin.id,
        name=admin.name,
        type=admin.type,
        ci_scope=CiScope(
            token_id=uuid.uuid4(),
            rule_id=uuid.uuid4(),
            environment="dev",
            environment_id=uuid.uuid4(),
            repository="shop",
            repository_id=uuid.uuid4(),
            kinds=frozenset({"deploy"}),
            sha=None,
        ),
    )
    return [task, ci]


@pytest.fixture
def admin(session: Session) -> PrincipalRef:
    """Persist an administrator and the environment `dev`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        Their reference.
    """
    environment(session, "dev")
    ref = person(session, "ada")
    grant(session, ref, role=Role.ADMIN)
    return ref


def _attention(session: Session, p: PrincipalRef) -> object:
    """Ask Needs your attention.

    Parameters
    ----------
    session
        Database session.
    p
        The caller.

    Returns
    -------
    object
        The answer.
    """
    return AttentionService(session, GrantAuthorization(session), lease_seconds=60).for_caller(p)


def _work(session: Session, p: PrincipalRef) -> object:
    """Ask Your work.

    Parameters
    ----------
    session
        Database session.
    p
        The caller.

    Returns
    -------
    object
        The answer.
    """
    authz = GrantAuthorization(session)
    return your_work(session, authz, JobService(session, authz), p)


def _started(session: Session, p: PrincipalRef) -> object:
    """Ask Get started.

    Parameters
    ----------
    session
        Database session.
    p
        The caller.

    Returns
    -------
    object
        The answer.
    """
    return get_started(session, GrantService(session, GrantAuthorization(session)), p, local=False)


@pytest.mark.parametrize("ask", [_attention, _work, _started])
def test_a_task_or_ci_token_of_an_administrator_is_refused(
    session: Session, admin: PrincipalRef, ask: Callable[[Session, PrincipalRef], object]
) -> None:
    assert ask(session, admin) is not None
    for token in _tokens(admin):
        with pytest.raises(PermissionDenied, match="task or CI token"):
            ask(session, token)


def test_a_task_or_ci_token_of_an_administrator_gets_no_status_detail(
    session: Session, admin: PrincipalRef
) -> None:
    runs = RunService(session, GrantAuthorization(session))
    assert runs.instance_status(admin, ()).detail is not None
    for token in _tokens(admin):
        assert runs.instance_status(token, ()).detail is None
