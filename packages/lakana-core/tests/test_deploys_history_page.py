"""`DeployService.history_page`: the repository filter and the cursor of the deploys list."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog
from home_test_helpers import deployed_job, environment, grant, person
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from test_projects_deploys import _principal, _repo

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.models import (
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    Principal,
)
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


def _three_environments(session: Session) -> list[DeployRun]:
    """Persist seven finished deploys over `dev`, `test` and `prod`, one minute apart.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of DeployRun
        The deploys, oldest first.
    """
    envs = {name: environment(session, name) for name in ("dev", "test", "prod")}
    repos = {name: deployed_job(session, name, f"shop_{name}", "nightly")[0] for name in envs}
    runs = []
    for minute, name in enumerate(("dev", "test", "prod", "dev", "prod", "test", "dev")):
        run = DeployRun(
            id=uuid.uuid4(),
            environment_id=envs[name],
            repository_id=repos[name].id,
            kind=DeployKind.DEPLOY,
            status=DeployStatus.SUCCEEDED,
            created_at=_T0 + timedelta(minutes=minute),
        )
        session.add(run)
        runs.append(run)
    session.commit()
    return runs


@pytest.fixture
def admin(session: Session) -> PrincipalRef:
    """Persist an administrator.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        Their reference.
    """
    ref = person(session, "ada")
    grant(session, ref, role=Role.ADMIN)
    return ref


def test_all_deploys_pages_every_deploy_once_across_environments(
    session: Session, admin: PrincipalRef
) -> None:
    runs = _three_environments(session)
    service = DeployService(session, GrantAuthorization(session))
    seen: list[uuid.UUID] = []
    environments: list[str] = []
    cursor = None
    pages = 0
    while True:
        page, cursor = service.list_all_deploys_page(admin, limit=2, cursor=cursor)
        pages += 1
        seen += [v.id for v in page]
        environments += [v.environment for v in page]
        if cursor is None:
            break
    assert (pages, seen) == (4, [r.id for r in reversed(runs)])
    assert environments == ["dev", "test", "prod", "dev", "prod", "test", "dev"][::-1]


def test_all_deploys_skips_environments_you_may_not_read(session: Session) -> None:
    runs = _three_environments(session)
    reader = person(session, "rita")
    grant(session, reader, action=Action.READ_ENVIRONMENT, environment_name="test")
    service = DeployService(session, GrantAuthorization(session))
    page, cursor = service.list_all_deploys_page(reader)
    assert ([v.id for v in page], cursor) == ([runs[5].id, runs[1].id], None)
    with pytest.raises(PermissionDenied):
        service.list_all_deploys_page(reader, environment="prod")
    nobody = person(session, "nora")
    assert service.list_all_deploys_page(nobody) == ([], None)


def test_all_deploys_with_an_environment_is_that_environments_history(
    session: Session, admin: PrincipalRef
) -> None:
    _three_environments(session)
    service = DeployService(session, GrantAuthorization(session))
    own, own_cursor = service.history_page(admin, "dev", limit=2)
    same, same_cursor = service.list_all_deploys_page(admin, environment="dev", limit=2)
    assert ([v.id for v in same], same_cursor) == ([v.id for v in own], own_cursor)
    assert own_cursor is not None
    assert [
        v.id for v in service.list_all_deploys_page(admin, environment="dev", cursor=own_cursor)[0]
    ] == [v.id for v in service.history_page(admin, "dev", cursor=own_cursor)[0]]
    with pytest.raises(NotFoundError):
        service.list_all_deploys_page(admin, environment="ghost")


def test_a_cursor_of_the_per_environment_history_is_refused_by_all_deploys(
    session: Session, admin: PrincipalRef
) -> None:
    _three_environments(session)
    service = DeployService(session, GrantAuthorization(session))
    _, per_environment = service.history_page(admin, "dev", limit=1)
    _, across = service.list_all_deploys_page(admin, limit=1)
    assert per_environment is not None and across is not None
    with pytest.raises(ValidationError, match="came from the `deploys` list"):
        service.list_all_deploys_page(admin, cursor=per_environment)
    with pytest.raises(ValidationError, match="came from the `all-deploys` list"):
        service.history_page(admin, "dev", cursor=across)
    with pytest.raises(ValidationError, match="came from the `all-deploys` list"):
        service.list_all_deploys_page(admin, environment="dev", cursor=across)


def test_all_deploys_filters_by_repository(session: Session, admin: PrincipalRef) -> None:
    runs = _three_environments(session)
    page, _ = DeployService(session, GrantAuthorization(session)).list_all_deploys_page(
        admin, repository="shop_prod"
    )
    assert [v.id for v in page] == [runs[4].id, runs[2].id]


def test_all_deploys_with_an_unknown_repository_is_not_found(
    session: Session, admin: PrincipalRef
) -> None:
    _three_environments(session)
    with pytest.raises(NotFoundError, match="ghost"):
        DeployService(session, GrantAuthorization(session)).list_all_deploys_page(
            admin, repository="ghost"
        )


def test_all_deploys_asks_the_environments_in_one_batch(
    session: Session, admin: PrincipalRef
) -> None:
    _three_environments(session)
    batches: list[int] = []

    class Spy(GrantAuthorization):
        def permits_each(self, principal, questions):  # type: ignore[no-untyped-def]  # ruff: ignore[missing-type-function-argument, missing-return-type-private-function]
            batches.append(len(questions))
            return super().permits_each(principal, questions)

    page, _ = DeployService(session, Spy(session)).list_all_deploys_page(admin)
    assert len(page) == 7
    assert batches == [3]


def _statements(session: Session, call: Callable[[], object]) -> int:
    """Count the SQL statements a call runs.

    Parameters
    ----------
    session
        A session on the engine watched.
    call
        What to run.

    Returns
    -------
    int
        The number of statements.
    """
    seen: list[str] = []
    engine = session.get_bind()

    def record(_conn: object, _cur: object, statement: str, *_: object) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(seen)


def test_a_page_costs_the_same_statements_whatever_its_size(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    repos = [_repo(session, f"repo{i}") for i in range(3)]
    service = _service(session)
    for minute in range(3):
        _deploy(session, env, repos[minute % 3].id, minute)
    session.expire_all()
    small = _statements(session, lambda: service.history_page(principal, "dev", limit=3))
    for minute in range(3, 30):
        _deploy(session, env, repos[minute % 3].id, minute)
    session.expire_all()
    large = _statements(session, lambda: service.history_page(principal, "dev", limit=30))
    assert large == small
    session.expire_all()
    everywhere = _statements(session, lambda: service.list_all_deploys_page(principal, limit=30))
    assert everywhere == small


def test_the_names_of_a_page_are_those_of_each_run(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    repos = [_repo(session, f"repo{i}") for i in range(3)]
    runs = [_deploy(session, env, repos[i % 3].id, i) for i in range(9)]
    session.expire_all()
    page, _ = _service(session).history_page(principal, "dev", limit=9)
    by_id = {r.id: i for i, r in enumerate(runs)}
    assert [(v.environment, v.repository) for v in page] == [
        ("dev", f"repo{by_id[v.id] % 3}") for v in page
    ]
    assert len(page) == 9


def _walk_deploys(service: DeployService, principal: PrincipalRef, **sort: str) -> list[uuid.UUID]:
    """Read every page of the `dev` deploys, one a page, in a sort.

    Parameters
    ----------
    service
        The deploy service.
    principal
        The caller.
    **sort
        `sort` and `order`, as the list takes them.

    Returns
    -------
    list of uuid.UUID
        The ids, in the order the pages gave them.
    """
    seen: list[uuid.UUID] = []
    cursor = None
    while True:
        page, cursor = service.history_page(principal, "dev", limit=1, cursor=cursor, **sort)
        seen += [v.id for v in page]
        if cursor is None:
            return seen


def test_deploys_sort_oldest_first_and_by_state(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    repo = _repo(session, "analytics")
    old = _deploy(session, env, repo.id, 1)
    failed = _deploy(session, env, repo.id, 2)
    new = _deploy(session, env, repo.id, 3)
    failed.status = DeployStatus.FAILED
    session.commit()
    service = _service(session)
    assert _walk_deploys(service, principal, sort="created", order="asc") == [
        old.id,
        failed.id,
        new.id,
    ]
    assert _walk_deploys(service, principal, sort="status") == [failed.id, new.id, old.id]
    assert _walk_deploys(service, principal, sort="status", order="desc") == [
        new.id,
        old.id,
        failed.id,
    ]


def test_deploys_refuse_an_unknown_sort_and_a_cursor_of_another_sort(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    repo = _repo(session, "analytics")
    for minutes in (1, 2):
        _deploy(session, env, repo.id, minutes)
    service = _service(session)
    with pytest.raises(ValidationError, match="`sort` must be one of: created, status; got 'name'"):
        service.history_page(principal, "dev", sort="name")
    _, by_state = service.history_page(principal, "dev", limit=1, sort="status")
    assert by_state is not None
    with pytest.raises(ValidationError, match="in its default order"):
        service.history_page(principal, "dev", cursor=by_state)


def test_all_deploys_sort_oldest_first(session: Session, admin: PrincipalRef) -> None:
    runs = _three_environments(session)
    service = DeployService(session, GrantAuthorization(session))
    seen: list[uuid.UUID] = []
    cursor = None
    while True:
        page, cursor = service.list_all_deploys_page(
            admin, limit=3, cursor=cursor, sort="created", order="asc"
        )
        seen += [v.id for v in page]
        if cursor is None:
            break
    assert seen == [r.id for r in runs]
    page, _ = service.list_all_deploys_page(admin, environment="dev", sort="created", order="asc")
    assert [v.id for v in page] == [runs[0].id, runs[3].id, runs[6].id]
