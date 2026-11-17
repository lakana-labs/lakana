"""`DeployService.history_page`: the repository filter and the cursor of the deploys list."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_projects_deploys import _principal, _repo

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.models import DeployKind, DeployRun, DeployStatus, Environment, Principal
from lakana_core.pagination import encode_cursor
from lakana_core.projects.deploys import DeployService

_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def env(session: Session, tmp_path: Path) -> Environment:
    """Create `dev`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.

    Returns
    -------
    Environment
        The row.
    """
    _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    return session.scalars(select(Environment)).one()


def _deploy(session: Session, env: Environment, repository: uuid.UUID, minutes: int) -> DeployRun:
    """Insert a finished deploy of a repository.

    Parameters
    ----------
    session
        Database session; commits.
    env
        The environment.
    repository
        The repository's id.
    minutes
        Minutes after `_T0` it was created.

    Returns
    -------
    DeployRun
        The row.
    """
    run = DeployRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        repository_id=repository,
        kind=DeployKind.DEPLOY,
        status=DeployStatus.SUCCEEDED,
        created_at=_T0 + timedelta(minutes=minutes),
    )
    session.add(run)
    session.commit()
    return run


def _service(session: Session) -> DeployService:
    """Build a deploy service that allows everything.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    DeployService
        The service.
    """
    return DeployService(session, AllowAllAuthorization())


def test_the_repository_filter_keeps_only_that_repositorys_deploys(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    mine = _repo(session, "analytics")
    other = _repo(session, "ml")
    a = _deploy(session, env, mine.id, 1)
    _deploy(session, env, other.id, 2)
    b = _deploy(session, env, mine.id, 3)
    page, cursor = _service(session).history_page(principal, "dev", repository="analytics")
    assert ([v.id for v in page], cursor) == ([b.id, a.id], None)
    assert all(v.repository == "analytics" for v in page)


def test_an_unknown_repository_is_not_found(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    with pytest.raises(NotFoundError, match="ghost"):
        _service(session).history_page(principal, "dev", repository="ghost")


def test_pages_continue_strictly_after_the_cursor_across_equal_times(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    tied = [_deploy(session, env, repo.id, 5) for _ in range(5)]
    older = _deploy(session, env, repo.id, 1)
    newer = _deploy(session, env, repo.id, 9)
    expected = [newer.id, *sorted((r.id for r in tied), reverse=True), older.id]
    service = _service(session)
    seen: list[uuid.UUID] = []
    cursor = None
    pages = 0
    while True:
        page, cursor = service.history_page(principal, "dev", limit=2, cursor=cursor)
        pages += 1
        seen += [v.id for v in page]
        if cursor is None:
            break
    assert (pages, seen) == (4, expected)


def test_a_deploy_made_while_paging_is_neither_repeated_nor_skipped(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    first = [_deploy(session, env, repo.id, m) for m in (1, 2, 3, 4)]
    service = _service(session)
    page, cursor = service.history_page(principal, "dev", limit=2)
    _deploy(session, env, repo.id, 50)
    rest, last = service.history_page(principal, "dev", limit=2, cursor=cursor)
    assert last is None
    assert [v.id for v in page + rest] == [r.id for r in reversed(first)]


def test_a_cursor_of_another_list_is_refused(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    other = encode_cursor("runs", _T0, uuid.uuid4())
    with pytest.raises(ValidationError, match="came from the `runs` list"):
        _service(session).history_page(principal, "dev", cursor=other)


def test_a_limit_below_one_is_refused(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    with pytest.raises(ValidationError, match="limit must be at least 1"):
        _service(session).history_page(principal, "dev", limit=0)


def test_the_history_names_who_submitted_each_deploy(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    ada = _principal(session, "ada")
    first = _deploy(session, env, repo.id, 1)
    second = _deploy(session, env, repo.id, 2)
    first.principal_id = principal.id
    second.principal_id = ada.id
    session.commit()
    page, _ = _service(session).history_page(principal, "dev")
    assert [(v.id, v.submitted_by) for v in page] == [
        (second.id, "ada"),
        (first.id, principal.name),
    ]


def test_a_deleted_submitter_is_null(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    ada = _principal(session, "ada")
    run = _deploy(session, env, repo.id, 1)
    run.principal_id = ada.id
    session.commit()
    session.delete(session.get(Principal, ada.id))
    session.commit()
    session.refresh(run)
    assert run.principal_id is None
    page, _ = _service(session).history_page(principal, "dev")
    assert [v.submitted_by for v in page] == [None]


def test_the_history_carries_no_jobs(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    repo = _repo(session)
    _deploy(session, env, repo.id, 1)
    page, _ = _service(session).history_page(principal, "dev")
    assert [v.jobs for v in page] == [None]
