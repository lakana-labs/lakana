"""On Postgres the sorted lists page exactly as on SQLite: every row once, in the order asked.

Postgres compares text under the database's collation (glibc's ``en_US.utf8`` in the Debian
image), which ignores letter case and punctuation in ways Python does not; the name keys are
collated ``"C"`` so a cursor's key compares as it sorted. A correlated subquery (the last test of a
connection) and ``NULLS LAST`` in both directions are exercised as well.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from home_test_helpers import deployed_job, environment, finished_run, grant, person
from saved_query_test_helpers import LISTS, build_world, new_pages
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_saved_query_lists import _sorted_walk
from testcontainers.postgres import PostgresContainer

from lakana_core.audit.service import AuditCursor, AuditService
from lakana_core.auth import AllowAllAuthorization
from lakana_core.config import DatabaseConfig
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.connections.service import _connection_page
from lakana_core.db import create_engine
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import (
    AuditEventType,
    Base,
    Connection,
    ConnectionTest,
    ConnectionTestOutcome,
    ConnectionTestState,
    PrincipalType,
    RunStatus,
)
from lakana_core.pagination import ListSort, Page
from lakana_core.runs.service import RunService

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
_NAMES = ("Revenue", "rêve", "Zeta", "alpha", "a-b", "a_b", "AB", "Ω", "zz", "bob")
"""Names a collation and code points order differently."""


def _walk(read: Callable[[str | None], Page]) -> list:
    """Read every page of a list, one row a page.

    Parameters
    ----------
    read
        Reads the page after a cursor, `None` for the first.

    Returns
    -------
    list
        The rows, in the order the pages gave them.
    """
    rows: list = []
    cursor = None
    while True:
        page = read(cursor)
        rows += page.items
        cursor = page.next_cursor
        if cursor is None:
            return rows


def _principals(session: Session) -> None:
    """Check that principals sort by name in any letter case by code point, and the active first.

    Parameters
    ----------
    session
        A session on Postgres.
    """
    admin = person(session, "root")
    service = PrincipalService(session, AllowAllAuthorization())
    for name in _NAMES:
        service.create_principal(admin, name=name, type=PrincipalType.USER)
    service.deactivate_principal(admin, "alpha")
    every = [p.name for p in service.list_principals(admin, limit=200).items]
    for order, reverse in (("asc", False), ("desc", True)):
        got = _walk(
            lambda c, o=order: service.list_principals(
                admin, limit=1, cursor=c, sort="name", order=o
            )
        )
        names = [p.name for p in got]
        assert [n.lower() for n in names] == sorted((n.lower() for n in every), reverse=reverse)
        assert sorted(names) == sorted(every)
    by_state = [
        p.name
        for p in _walk(lambda c: service.list_principals(admin, limit=1, cursor=c, sort="status"))
    ]
    assert by_state[-1] == "alpha" and len(by_state) == len(every)


def _runs(session: Session) -> None:
    """Check that runs sort by name (the job's, else the repository's) and by state, every run once.

    Parameters
    ----------
    session
        A session on Postgres.
    """
    admin = person(session, "ada")
    grant(session, admin, role=Role.ADMIN)
    repo, version = deployed_job(session, "dev", "Shop", "nightly")
    made = []
    for minute, job in enumerate(("b-job", None, "A_job", "b-job", None, "Ω")):
        made.append(
            finished_run(
                session,
                repo,
                version,
                job=job,
                status=RunStatus.FAILED if minute % 2 else RunStatus.SUCCEEDED,
                finished_at=_T0 + timedelta(minutes=minute),
            )
        )
    service = RunService(session, GrantAuthorization(session))
    # `duration` measures on Postgres by its epoch difference, `tasks` by a correlated count.
    for sort in ("name", "status", "created", "environment", "trigger", "duration", "tasks"):
        for order in ("asc", "desc"):
            seen: list[uuid.UUID] = []
            cursor = None
            while True:
                page, cursor = service.list_all_runs_page(
                    admin, limit=1, cursor=cursor, sort=sort, order=order
                )
                seen += [v.id for v in page]
                if cursor is None:
                    break
            whole, _ = service.list_all_runs_page(admin, limit=200, sort=sort, order=order)
            assert seen == [v.id for v in whole], (sort, order)
            assert sorted(seen) == sorted(r.id for r in made)
    names = [
        (v.job or v.repository).lower()
        for v in service.list_all_runs_page(admin, limit=200, sort="name")[0]
    ]
    assert names == sorted(names)


def _connections(session: Session) -> None:
    """Check that connections sort by their last test, never tested last in either direction.

    Parameters
    ----------
    session
        A session on Postgres.
    """
    env_id = environment(session, "prod")
    owner = person(session, "owner")
    # Two share each tested state and two were never tested, so a page ends on a tie and on a
    # null, and the next page resumes from there.
    names = ("bad-a", "bad-b", "open", "good", "never-a", "never-b")
    ids = {}
    for name in names:
        row = Connection(
            id=uuid.uuid4(),
            environment_id=env_id,
            name=name,
            kind=ConnectionKind.DIRECTORY,
            address={"root": "r", "path": name},
            settings={},
            created_by=owner.id,
        )
        session.add(row)
        ids[name] = row.id
    session.flush()
    tests = {
        "bad-a": (ConnectionTestState.DONE, ConnectionTestOutcome.TIMEOUT),
        "bad-b": (ConnectionTestState.DONE, ConnectionTestOutcome.DNS),
        "open": (ConnectionTestState.QUEUED, None),
        "good": (ConnectionTestState.DONE, ConnectionTestOutcome.OK),
    }
    for name, (state, outcome) in tests.items():
        session.add(
            ConnectionTest(
                id=uuid.uuid4(),
                connection_id=ids[name],
                environment_id=env_id,
                requested_by=owner.id,
                state=state,
                outcome=outcome,
                created_at=_T0,
            )
        )
    session.commit()
    for descending, expected in (
        (False, ["bad-a", "bad-b", "open", "good", "never-a", "never-b"]),
        (True, ["good", "open", "bad-a", "bad-b", "never-a", "never-b"]),
    ):
        for limit in (1, 2):
            rows = _walk(
                lambda c, d=descending, n=limit: _connection_page(
                    session,
                    select(Connection),
                    kind="all-connections",
                    sort=ListSort("status", d),
                    limit=n,
                    cursor=c,
                )
            )
            assert [r.name for r in rows] == expected, (descending, limit)


def _audit(session: Session) -> None:
    """Check that the audit log pages the oldest first, ties on the time by id.

    Parameters
    ----------
    session
        A session on Postgres.
    """
    admin = person(session, "auditor")
    service = AuditService(session, AllowAllAuthorization())
    for _ in range(4):
        event = service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)
        event.occurred_at = _T0
    session.commit()
    newest = [e.id for e in service.query(admin, limit=500).items]
    seen: list[uuid.UUID] = []
    cursor: AuditCursor | None = None
    while True:
        page = service.query(admin, limit=1, cursor=cursor, order="asc")
        seen += [e.id for e in page.items]
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert seen == newest[::-1]
    for sort in ("event", "who", "on"):
        seen = []
        cursor = None
        while True:
            page = service.query(admin, limit=1, cursor=cursor, sort=sort, until=_T0)
            seen += [e.id for e in page.items]
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert sorted(seen) == sorted(newest), sort


def _grants(session: Session) -> None:
    """Check that grants sort by whom (a subquery of two names) and by place, every grant once.

    Parameters
    ----------
    session
        A session on Postgres.
    """
    admin = person(session, "root")
    grant(session, admin, role=Role.ADMIN)
    for name in _NAMES:
        grant(session, person(session, name), role=Role.ANALYST)
    service = GrantService(session, GrantAuthorization(session))
    every = sorted(g.id for g in service.list_grants(admin, limit=200).items)
    for sort in ("who", "what", "on", "granted"):
        for order in ("asc", "desc"):
            got = _walk(
                lambda c, s=sort, o=order: service.list_grants(
                    admin, limit=1, cursor=c, sort=s, order=o
                )
            )
            assert sorted(g.id for g in got) == every, (sort, order)


def test_the_sorted_lists_page_every_row_once_on_postgres(tmp_path: Path) -> None:
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        for check in (_principals, _runs, _connections, _audit, _grants):
            Base.metadata.drop_all(engine)
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                check(session)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            callers = build_world(session, tmp_path)
            for caller in callers:
                for environment_name in LISTS:
                    expected = new_pages(session, caller, environment_name, 200)
                    for sort, order in (("updated", "desc"), ("name", "desc")):
                        got = _sorted_walk(session, caller.ref, environment_name, 3, sort, order)
                        if isinstance(expected, str):
                            assert got == expected
                            continue
                        assert not isinstance(got, str)
                        assert sorted(v.id for v in got) == sorted(
                            item[0] for page, _ in expected for item in page
                        )
        engine.dispose()
