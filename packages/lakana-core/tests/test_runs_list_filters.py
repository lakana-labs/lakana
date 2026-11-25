"""`RunService.list_runs_page`: filters and cursor of the runs list."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog
from home_test_helpers import deployed_job, finished_run, grant, person
from sqlalchemy.orm import Session
from test_projects_dag import make_asset
from test_projects_deploys import _deployed, _repo

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.models import JobVersion, Run, RunStatus, RunTrigger
from lakana_core.pagination import encode_cursor
from lakana_core.projects.run_policy import RunCaps
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService

_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def version(session: Session, tmp_path: Path, principal: PrincipalRef) -> JobVersion:
    """Deploy `analytics` with one asset to `dev`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The admin.

    Returns
    -------
    JobVersion
        The current version.
    """
    _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    return _deployed(session, principal, _repo(session), "a" * 40, make_asset("gold.daily", ()))


def _service(session: Session) -> RunService:
    """Build a run service that allows everything.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    RunService
        The service.
    """
    return RunService(
        session, AllowAllAuthorization(), caps=RunCaps(max_retries=5, max_timeout_seconds=600)
    )


def _make_run(
    session: Session,
    principal: PrincipalRef,
    *,
    minutes: int = 0,
    status: RunStatus = RunStatus.SUCCEEDED,
    trigger: RunTrigger | None = None,
    job: str | None = None,
) -> Run:
    """Start a run of `analytics` and set the fields the list filters on.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The caller.
    minutes
        Minutes after `_T0` it was created.
    status
        Its status (a finished one by default, so the next start is allowed).
    trigger
        What started it.
    job
        The job it belongs to.

    Returns
    -------
    Run
        The row.
    """
    started = _service(session).start(principal, "dev", "analytics", Selection())
    run = session.get(Run, started.id)
    assert run is not None
    run.created_at = _T0 + timedelta(minutes=minutes)
    run.status = status
    run.trigger = trigger
    run.job = job
    session.commit()
    return run


def _ids(page: tuple[list, str | None]) -> list[uuid.UUID]:
    """Return the ids of a page's runs.

    Parameters
    ----------
    page
        What `list_runs_page` returned.

    Returns
    -------
    list of uuid.UUID
        The ids in order.
    """
    return [v.id for v in page[0]]


def test_status_filters_one_or_several_values(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    ok = _make_run(session, principal, minutes=1)
    bad = _make_run(session, principal, minutes=2, status=RunStatus.FAILED)
    gone = _make_run(session, principal, minutes=3, status=RunStatus.CANCELLED)
    service = _service(session)
    assert _ids(service.list_runs_page(principal, "dev", status=["failed"])) == [bad.id]
    both = service.list_runs_page(principal, "dev", status=["failed", "cancelled"])
    assert _ids(both) == [gone.id, bad.id]
    assert _ids(service.list_runs_page(principal, "dev", status=["succeeded"])) == [ok.id]


def test_trigger_job_and_since_filter(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    old = _make_run(session, principal, minutes=1, trigger=RunTrigger.SCHEDULE, job="nightly")
    new = _make_run(session, principal, minutes=60, trigger=RunTrigger.API, job="hourly")
    adhoc = _make_run(session, principal, minutes=120)
    service = _service(session)
    assert _ids(service.list_runs_page(principal, "dev", trigger=["schedule"])) == [old.id]
    assert _ids(service.list_runs_page(principal, "dev", trigger=["api", "schedule"])) == [
        new.id,
        old.id,
    ]
    assert _ids(service.list_runs_page(principal, "dev", job="hourly")) == [new.id]
    since = _T0 + timedelta(minutes=60)
    assert _ids(service.list_runs_page(principal, "dev", since=since)) == [adhoc.id, new.id]
    assert _ids(service.list_runs_page(principal, "dev", since=since + timedelta(seconds=1))) == [
        adhoc.id
    ]


def test_filters_combine_with_and(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    _make_run(session, principal, minutes=1, status=RunStatus.FAILED, job="a")
    hit = _make_run(
        session, principal, minutes=2, status=RunStatus.FAILED, job="a", trigger=RunTrigger.MANUAL
    )
    _make_run(session, principal, minutes=3, status=RunStatus.SUCCEEDED, job="a")
    page = _service(session).list_runs_page(
        principal,
        "dev",
        repository="analytics",
        status=["failed"],
        job="a",
        trigger=["manual"],
        since=_T0,
    )
    assert _ids(page) == [hit.id]
    assert page[1] is None


def test_an_unknown_status_or_trigger_is_refused_with_the_allowed_values(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    service = _service(session)
    with pytest.raises(ValidationError, match="`status` must be one of queued, running"):
        service.list_runs_page(principal, "dev", status=["done"])
    with pytest.raises(ValidationError, match="`trigger` must be one of schedule, api, manual"):
        service.list_runs_page(principal, "dev", trigger=["cron"])


def test_since_without_a_time_zone_is_refused(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    with pytest.raises(ValidationError, match="`since` has no time zone"):
        _service(session).list_runs_page(principal, "dev", since=datetime(2026, 10, 1, 12, 0))


def test_a_limit_below_one_is_refused(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    with pytest.raises(ValidationError, match="limit must be at least 1"):
        _service(session).list_runs_page(principal, "dev", limit=0)


def test_an_unknown_repository_is_not_found(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    with pytest.raises(NotFoundError):
        _service(session).list_runs_page(principal, "dev", repository="ghost")


def test_pages_continue_strictly_after_the_cursor_across_equal_times(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    runs = [_make_run(session, principal, minutes=5) for _ in range(5)]
    older = _make_run(session, principal, minutes=1)
    newer = _make_run(session, principal, minutes=9)
    expected = [newer.id, *sorted((r.id for r in runs), reverse=True), older.id]
    service = _service(session)
    seen: list[uuid.UUID] = []
    cursor = None
    pages = 0
    while True:
        page, cursor = service.list_runs_page(principal, "dev", limit=2, cursor=cursor)
        pages += 1
        seen += [v.id for v in page]
        if cursor is None:
            break
    assert (pages, seen) == (4, expected)


def test_a_new_run_while_paging_neither_repeats_nor_skips(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    first = [_make_run(session, principal, minutes=m) for m in (1, 2, 3, 4)]
    service = _service(session)
    page, cursor = service.list_runs_page(principal, "dev", limit=2)
    _make_run(session, principal, minutes=50)
    rest, last = service.list_runs_page(principal, "dev", limit=2, cursor=cursor)
    assert last is None
    assert [v.id for v in page + rest] == [r.id for r in reversed(first)]


def test_the_cursor_keeps_the_filter_of_the_caller_not_its_own(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    for m in (1, 2, 3):
        _make_run(session, principal, minutes=m, status=RunStatus.FAILED)
    _make_run(session, principal, minutes=4)
    service = _service(session)
    first, cursor = service.list_runs_page(principal, "dev", status=["failed"], limit=2)
    second, last = service.list_runs_page(
        principal, "dev", status=["failed"], limit=2, cursor=cursor
    )
    assert (len(first), len(second), last) == (2, 1, None)


def test_a_cursor_of_another_list_is_refused(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    other = encode_cursor("deploys", _T0, uuid.uuid4())
    with pytest.raises(ValidationError, match="came from the `deploys` list"):
        _service(session).list_runs_page(principal, "dev", cursor=other)


def test_list_runs_is_the_first_page_without_a_cursor(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    runs = [_make_run(session, principal, minutes=m) for m in (1, 2, 3)]
    assert [v.id for v in _service(session).list_runs(principal, "dev", limit=2)] == [
        runs[2].id,
        runs[1].id,
    ]


def _three_environments(session: Session) -> list[Run]:
    """Persist seven finished runs over `dev`, `test` and `prod`, one minute apart.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of Run
        The runs, oldest first.
    """
    runs = []
    repos = {
        env: deployed_job(session, env, f"shop_{env}", "nightly") for env in ("dev", "test", "prod")
    }
    for minute, env in enumerate(("dev", "test", "prod", "dev", "prod", "test", "dev")):
        repo, version = repos[env]
        runs.append(
            finished_run(
                session,
                repo,
                version,
                job="nightly" if minute % 2 else None,
                status=RunStatus.FAILED if minute < 3 else RunStatus.SUCCEEDED,
                finished_at=_T0 + timedelta(minutes=minute),
            )
        )
    return runs


def _grants(session: Session) -> GrantAuthorization:
    """Build the real authorization.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    GrantAuthorization
        Authorization from grants.
    """
    return GrantAuthorization(session)


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


def test_all_runs_pages_every_run_once_across_environments(
    session: Session, admin: PrincipalRef
) -> None:
    runs = _three_environments(session)
    service = RunService(session, _grants(session))
    seen: list[uuid.UUID] = []
    environments: list[str] = []
    cursor = None
    pages = 0
    while True:
        page, cursor = service.list_all_runs_page(admin, limit=2, cursor=cursor)
        pages += 1
        seen += [v.id for v in page]
        environments += [v.environment for v in page]
        if cursor is None:
            break
    assert (pages, seen) == (4, [r.id for r in reversed(runs)])
    assert environments == ["dev", "test", "prod", "dev", "prod", "test", "dev"][::-1]


def test_all_runs_skips_environments_you_may_not_read(session: Session) -> None:
    runs = _three_environments(session)
    reader = person(session, "rita")
    grant(session, reader, action=Action.READ_ENVIRONMENT, environment_name="test")
    service = RunService(session, _grants(session))
    page, cursor = service.list_all_runs_page(reader)
    assert ([v.id for v in page], cursor) == ([runs[5].id, runs[1].id], None)
    with pytest.raises(PermissionDenied):
        service.list_all_runs_page(reader, environment="prod")
    nobody = person(session, "nora")
    assert service.list_all_runs_page(nobody) == ([], None)


def test_all_runs_with_an_environment_is_that_environments_list(
    session: Session, admin: PrincipalRef
) -> None:
    _three_environments(session)
    service = RunService(session, _grants(session))
    own, own_cursor = service.list_runs_page(admin, "dev", limit=2)
    same, same_cursor = service.list_all_runs_page(admin, environment="dev", limit=2)
    assert ([v.id for v in same], same_cursor) == ([v.id for v in own], own_cursor)
    assert own_cursor is not None
    assert [
        v.id for v in service.list_all_runs_page(admin, environment="dev", cursor=own_cursor)[0]
    ] == [v.id for v in service.list_runs_page(admin, "dev", cursor=own_cursor)[0]]
    with pytest.raises(NotFoundError):
        service.list_all_runs_page(admin, environment="ghost")


def test_a_cursor_of_the_per_environment_list_is_refused_by_all_runs(
    session: Session, admin: PrincipalRef
) -> None:
    _three_environments(session)
    service = RunService(session, _grants(session))
    _, per_environment = service.list_runs_page(admin, "dev", limit=1)
    _, across = service.list_all_runs_page(admin, limit=1)
    assert per_environment is not None and across is not None
    with pytest.raises(ValidationError, match="came from the `runs` list"):
        service.list_all_runs_page(admin, cursor=per_environment)
    with pytest.raises(ValidationError, match="came from the `all-runs` list"):
        service.list_runs_page(admin, "dev", cursor=across)
    with pytest.raises(ValidationError, match="came from the `all-runs` list"):
        service.list_all_runs_page(admin, environment="dev", cursor=across)


def test_all_runs_filters_by_status_trigger_job_repository_and_since(
    session: Session, admin: PrincipalRef
) -> None:
    runs = _three_environments(session)
    for run, trigger in zip(runs, (RunTrigger.SCHEDULE, RunTrigger.API) * 4, strict=False):
        run.trigger = trigger
    session.commit()
    service = RunService(session, _grants(session))

    def ids(page: tuple[list, str | None]) -> list[uuid.UUID]:
        return [v.id for v in page[0]]

    assert ids(service.list_all_runs_page(admin, status=["failed"])) == [
        runs[2].id,
        runs[1].id,
        runs[0].id,
    ]
    assert ids(service.list_all_runs_page(admin, trigger=["api"])) == [
        runs[5].id,
        runs[3].id,
        runs[1].id,
    ]
    assert ids(service.list_all_runs_page(admin, job="nightly", status=["succeeded"])) == [
        runs[5].id,
        runs[3].id,
    ]
    assert ids(service.list_all_runs_page(admin, repository="shop_prod")) == [
        runs[4].id,
        runs[2].id,
    ]
    assert ids(service.list_all_runs_page(admin, since=_T0 + timedelta(minutes=5))) == [
        runs[6].id,
        runs[5].id,
    ]
    with pytest.raises(ValidationError):
        service.list_all_runs_page(admin, status=["bogus"])
    with pytest.raises(ValidationError):
        service.list_all_runs_page(admin, since=datetime(2026, 1, 1))


def test_all_runs_with_an_unknown_repository_is_not_found(
    session: Session, admin: PrincipalRef
) -> None:
    _three_environments(session)
    with pytest.raises(NotFoundError):
        RunService(session, _grants(session)).list_all_runs_page(admin, repository="ghost")


def _walk(
    service: RunService, principal: PrincipalRef, sort: str, order: str | None = None
) -> list[uuid.UUID]:
    """Read every page of the `dev` runs, one run a page, in a sort.

    Parameters
    ----------
    service
        The run service.
    principal
        The caller.
    sort
        The field.
    order
        The direction; the field's own without it.

    Returns
    -------
    list of uuid.UUID
        The ids, in the order the pages gave them.
    """
    seen: list[uuid.UUID] = []
    cursor = None
    while True:
        page, cursor = service.list_runs_page(
            principal, "dev", limit=1, cursor=cursor, sort=sort, order=order
        )
        seen += [v.id for v in page]
        if cursor is None:
            return seen


def test_runs_sort_by_creation_oldest_first_across_pages(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    runs = [_make_run(session, principal, minutes=m) for m in (3, 1, 2, 2)]
    tied = sorted((runs[2].id, runs[3].id))
    expected = [runs[1].id, *tied, runs[0].id]
    service = _service(session)
    assert _walk(service, principal, sort="created", order="asc") == expected
    whole, _ = service.list_runs_page(principal, "dev", sort="created", order="asc")
    assert [v.id for v in whole] == expected


def test_runs_sort_by_state_newest_first_within_a_state(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    ok_old = _make_run(session, principal, minutes=1)
    bad = _make_run(session, principal, minutes=2, status=RunStatus.FAILED)
    ok_new = _make_run(session, principal, minutes=3)
    gone = _make_run(session, principal, minutes=4, status=RunStatus.CANCELLED)
    service = _service(session)
    assert _walk(service, principal, sort="status") == [gone.id, bad.id, ok_new.id, ok_old.id]
    assert _walk(service, principal, sort="status", order="desc") == [
        ok_new.id,
        ok_old.id,
        bad.id,
        gone.id,
    ]


def test_runs_sort_by_name_is_the_job_else_the_repository_in_any_case(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    beta = _make_run(session, principal, minutes=1, job="Beta")
    adhoc = _make_run(session, principal, minutes=2)
    alpha = _make_run(session, principal, minutes=3, job="alpha")
    zeta = _make_run(session, principal, minutes=4, job="zeta")
    service = _service(session)
    # The run of no job is named after its repository, `analytics`.
    assert _walk(service, principal, sort="name") == [alpha.id, adhoc.id, beta.id, zeta.id]
    assert _walk(service, principal, sort="name", order="desc") == [
        zeta.id,
        beta.id,
        adhoc.id,
        alpha.id,
    ]


def test_runs_sort_keeps_the_filters(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    _make_run(session, principal, minutes=1, status=RunStatus.FAILED)
    first = _make_run(session, principal, minutes=2)
    second = _make_run(session, principal, minutes=3)
    page, cursor = _service(session).list_runs_page(
        principal, "dev", status=["succeeded"], sort="created", order="asc"
    )
    assert ([v.id for v in page], cursor) == ([first.id, second.id], None)


def test_an_unknown_sort_or_order_is_refused_with_the_choices(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    service = _service(session)
    with pytest.raises(ValidationError, match="`sort` must be one of: created, name, status"):
        service.list_runs_page(principal, "dev", sort="duration")
    with pytest.raises(ValidationError, match="`order` must be asc or desc; got 'up'"):
        service.list_runs_page(principal, "dev", sort="name", order="up")


def test_a_cursor_of_another_sort_is_refused_not_misread(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    for m in (1, 2, 3):
        _make_run(session, principal, minutes=m, job=f"job{m}")
    service = _service(session)
    _, by_name = service.list_runs_page(principal, "dev", limit=1, sort="name")
    _, newest = service.list_runs_page(principal, "dev", limit=1)
    _, oldest = service.list_runs_page(principal, "dev", limit=1, sort="created", order="asc")
    assert by_name is not None and newest is not None and oldest is not None
    with pytest.raises(
        ValidationError, match="sorted by `name:desc`: it came from the list sorted by `name:asc`"
    ):
        service.list_runs_page(principal, "dev", sort="name", order="desc", cursor=by_name)
    with pytest.raises(ValidationError, match="in its default order: it came from the list sorted"):
        service.list_runs_page(principal, "dev", cursor=oldest)
    with pytest.raises(
        ValidationError, match="sorted by `status:asc`: it came from the list in its default order"
    ):
        service.list_runs_page(principal, "dev", sort="status", cursor=newest)


def test_the_default_sort_named_is_the_default_list(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    for m in (1, 2, 3):
        _make_run(session, principal, minutes=m)
    service = _service(session)
    plain = service.list_runs_page(principal, "dev", limit=2)
    named = service.list_runs_page(principal, "dev", limit=2, sort="created", order="desc")
    assert ([v.id for v in plain[0]], plain[1]) == ([v.id for v in named[0]], named[1])


def test_all_runs_sort_across_environments(session: Session, admin: PrincipalRef) -> None:
    runs = _three_environments(session)
    service = RunService(session, _grants(session))
    seen: list[uuid.UUID] = []
    cursor = None
    while True:
        page, cursor = service.list_all_runs_page(
            admin, limit=2, cursor=cursor, sort="created", order="asc"
        )
        seen += [v.id for v in page]
        if cursor is None:
            break
    assert seen == [r.id for r in runs]
    _, by_name = service.list_all_runs_page(admin, limit=1, sort="name")
    assert by_name is not None
    with pytest.raises(ValidationError, match="came from the `all-runs` list"):
        service.list_runs_page(admin, "dev", sort="name", cursor=by_name)
