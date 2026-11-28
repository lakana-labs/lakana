"""`QueryService.list_queries_page`: the status, submitter, origin and since filters, the cursor."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from test_query_service import MANAGER, SUBMITTER, _add_run, _ago, _env, _ids, _persisted

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.query_service import QueryService
from lakana_core.errors import ValidationError
from lakana_core.models import Principal, PrincipalType, QueryOrigin, QueryRunStatus
from lakana_core.pagination import encode_cursor


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Provide the acting principal, persisted as `admin`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        The caller.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name="admin"))
    session.commit()
    return PrincipalRef(id=principal_id, name="admin", type=PrincipalType.USER)


def test_the_status_filter_keeps_the_named_statuses_in_any_case(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    queued = _add_run(session, env, principal, age=100)
    running = _add_run(session, env, principal, QueryRunStatus.RUNNING, started_at=_ago(10))
    done = _add_run(
        session, env, principal, QueryRunStatus.SUCCEEDED, age=500, finished_at=_ago(60)
    )
    failed = _add_run(session, env, principal, QueryRunStatus.FAILED, age=500, finished_at=_ago(30))
    service = QueryService(session, SUBMITTER)
    assert _ids(service.list_queries_page(principal, "dev", status=["queued"])[0]) == [queued.id]
    assert _ids(service.list_queries_page(principal, "dev", status=["RUNNING"])[0]) == [running.id]
    both = service.list_queries_page(principal, "dev", status=["running", "queued"])[0]
    assert _ids(both) == [running.id, queued.id]
    # naming an ended status asks for ended runs; `include_ended` is not needed
    ended = service.list_queries_page(principal, "dev", status=["failed", "succeeded"])[0]
    assert _ids(ended) == [failed.id, done.id]
    assert _ids(service.list_queries_page(principal, "dev", status=["failed"])[0]) == [failed.id]
    assert _ids(service.list_queries_page(principal, "dev")[0]) == [running.id, queued.id]


def test_an_ended_status_keeps_the_24_hour_window(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    _add_run(session, env, principal, QueryRunStatus.FAILED, age=9000, finished_at=_ago(90_000))
    page = QueryService(session, SUBMITTER).list_queries_page(principal, "dev", status=["failed"])
    assert page == ([], None)


def test_an_unknown_status_is_refused_with_the_allowed_values(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _env(session, principal, tmp_path, "dev")
    with pytest.raises(ValidationError, match="`status` must be one of QUEUED, RUNNING, SUCCEEDED"):
        QueryService(session, SUBMITTER).list_queries_page(principal, "dev", status=["done"])


def test_the_submitter_filter_narrows_a_managers_list(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    bob = _persisted(session, "bob")
    mine = _add_run(session, env, principal)
    theirs = _add_run(session, env, bob)
    service = QueryService(session, MANAGER)
    assert _ids(service.list_queries_page(principal, "dev", submitter="bob")[0]) == [theirs.id]
    assert _ids(service.list_queries_page(principal, "dev", submitter="admin")[0]) == [mine.id]
    assert service.list_queries_page(principal, "dev", submitter="nobody") == ([], None)


def test_the_submitter_filter_never_shows_the_queries_of_others(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    bob = _persisted(session, "bob")
    mine = _add_run(session, env, principal)
    _add_run(session, env, bob)
    service = QueryService(session, SUBMITTER)
    assert service.list_queries_page(principal, "dev", submitter="bob") == ([], None)
    assert _ids(service.list_queries_page(principal, "dev", submitter="admin")[0]) == [mine.id]
    both = service.list_queries_page(principal, "dev", submitter="bob", status=["queued"])
    assert both == ([], None)


def test_pages_follow_the_order_running_queued_ended_across_ties(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    same_start = _ago(120)
    running = [
        _add_run(session, env, principal, QueryRunStatus.RUNNING, started_at=same_start)
        for _ in range(3)
    ]
    same_creation = _ago(60)
    queued = [_add_run(session, env, principal, created_at=same_creation) for _ in range(3)]
    same_finish = _ago(30)
    ended = [
        _add_run(session, env, principal, QueryRunStatus.FAILED, age=500, finished_at=same_finish)
        for _ in range(3)
    ]
    expected = [
        *sorted(r.id for r in running),
        *sorted(r.id for r in queued),
        *sorted(r.id for r in ended),
    ]
    service = QueryService(session, SUBMITTER)
    for size in (1, 2, 4, 9, 10):
        seen: list[uuid.UUID] = []
        cursor = None
        while True:
            items, cursor = service.list_queries_page(
                principal, "dev", include_ended=True, limit=size, cursor=cursor
            )
            seen += _ids(items)
            if cursor is None:
                break
        assert seen == expected, size


def test_a_full_last_page_has_no_cursor_and_a_cut_one_has(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    for age in (30, 20, 10):
        _add_run(session, env, principal, age=age)
    service = QueryService(session, SUBMITTER)
    assert service.list_queries_page(principal, "dev", limit=3)[1] is None
    items, cursor = service.list_queries_page(principal, "dev", limit=2)
    assert len(items) == 2
    assert cursor is not None
    rest, last = service.list_queries_page(principal, "dev", limit=2, cursor=cursor)
    assert (len(rest), last) == (1, None)


def test_a_new_query_while_paging_neither_repeats_nor_skips(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    first = [_add_run(session, env, principal, age=age) for age in (40, 30, 20, 10)]
    service = QueryService(session, SUBMITTER)
    page, cursor = service.list_queries_page(principal, "dev", limit=2)
    newest = _add_run(session, env, principal, age=0)  # the queue takes it last
    seen = _ids(page)
    while cursor is not None:
        page, cursor = service.list_queries_page(principal, "dev", limit=2, cursor=cursor)
        seen += _ids(page)
    assert seen == [*(r.id for r in first), newest.id]


def test_the_cursor_of_another_list_is_refused(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _env(session, principal, tmp_path, "dev")
    other = encode_cursor("runs", _ago(5), uuid.uuid4())
    with pytest.raises(ValidationError, match="came from the `runs` list"):
        QueryService(session, SUBMITTER).list_queries_page(principal, "dev", cursor=other)


def test_origin_narrows_the_rows_you_may_already_see(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    bob = _persisted(session, "bob")
    written = _add_run(session, env, principal, age=30)
    task = _add_run(session, env, principal, age=20, origin=QueryOrigin.TASK)
    preview = _add_run(session, env, principal, age=10, origin=QueryOrigin.PREVIEW)
    _add_run(session, env, bob, age=5, origin=QueryOrigin.TASK)
    service = QueryService(session, SUBMITTER)
    assert _ids(service.list_queries_page(principal, "dev", origin=["task"])[0]) == [task.id]
    both = service.list_queries_page(principal, "dev", origin=["written", "preview"])[0]
    assert _ids(both) == [written.id, preview.id]
    rows = service.list_queries_page(principal, "dev")[0]
    assert [(row.environment, row.origin) for row in rows] == [
        ("dev", "written"),
        ("dev", "task"),
        ("dev", "preview"),
    ]
    # bob's task run stays absent: the filter is one more condition on what the caller sees
    assert service.list_queries_page(principal, "dev", origin=["task"], submitter="bob") == (
        [],
        None,
    )


def test_an_unknown_origin_is_a_validation_error_naming_the_choices(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _env(session, principal, tmp_path, "dev")
    with pytest.raises(
        ValidationError,
        match=re.escape(
            "Unknown origin 'typed': `origin` must be one of written, preview, check, write, "
            "maintenance, task."
        ),
    ):
        QueryService(session, SUBMITTER).list_queries_page(principal, "dev", origin=["typed"])


def test_since_lists_ended_runs_older_than_a_day(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    old = _add_run(
        session, env, principal, QueryRunStatus.SUCCEEDED, age=300_000, finished_at=_ago(259_200)
    )
    older = _add_run(
        session, env, principal, QueryRunStatus.FAILED, age=900_000, finished_at=_ago(864_000)
    )
    service = QueryService(session, SUBMITTER)
    assert service.list_queries_page(principal, "dev", include_ended=True) == ([], None)
    week = service.list_queries_page(principal, "dev", include_ended=True, since=_ago(604_800))
    assert _ids(week[0]) == [old.id]
    failed = service.list_queries_page(principal, "dev", status=["failed"], since=_ago(1_000_000))
    assert _ids(failed[0]) == [older.id]


def test_since_without_a_time_zone_is_refused(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _env(session, principal, tmp_path, "dev")
    with pytest.raises(ValidationError, match="`since` has no time zone"):
        QueryService(session, SUBMITTER).list_queries_page(
            principal, "dev", include_ended=True, since=datetime(2026, 10, 18, 9, 30)
        )


def test_a_sort_orders_the_same_rows_across_the_parts_one_a_page(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    running = _add_run(session, env, principal, QueryRunStatus.RUNNING, age=300, started_at=_ago(5))
    queued = _add_run(session, env, principal, age=100)
    failed = _add_run(session, env, principal, QueryRunStatus.FAILED, age=500, finished_at=_ago(30))
    done = _add_run(
        session, env, principal, QueryRunStatus.SUCCEEDED, age=200, finished_at=_ago(60)
    )
    service = QueryService(session, SUBMITTER)

    def walk(**sort: str) -> list[uuid.UUID]:
        """Read every query one a page.

        Parameters
        ----------
        **sort
            `sort` and `order`.

        Returns
        -------
        list of uuid.UUID
            The ids in the order the pages gave them.
        """
        seen: list[uuid.UUID] = []
        cursor: str | None = None
        while True:
            items, cursor = service.list_queries_page(
                principal, "dev", include_ended=True, limit=1, cursor=cursor, **sort
            )
            seen += _ids(items)
            if cursor is None:
                return seen

    assert walk(sort="created") == [queued.id, done.id, running.id, failed.id]
    assert walk(sort="created", order="asc") == [failed.id, running.id, done.id, queued.id]
    assert walk(sort="status") == [failed.id, queued.id, running.id, done.id]
    assert sorted(walk(order="desc")) == sorted(walk())
    _, by_state = service.list_queries_page(principal, "dev", limit=1, sort="status")
    with pytest.raises(ValidationError, match="came from the list sorted by `status:asc`"):
        service.list_queries_page(principal, "dev", sort="created", cursor=by_state)
    with pytest.raises(ValidationError, match="in its default order"):
        service.list_queries_page(principal, "dev", cursor=by_state)
    _, own = service.list_queries_page(principal, "dev", limit=1)
    with pytest.raises(ValidationError, match="came from the list in its default order"):
        service.list_queries_page(principal, "dev", sort="status", cursor=own)
    with pytest.raises(ValidationError, match="`sort` must be one of: created, status; got"):
        service.list_queries_page(principal, "dev", sort="sql")
    every, _ = service.list_all_queries_page(principal, include_ended=True, sort="created")
    assert _ids(every) == [queued.id, done.id, running.id, failed.id]
