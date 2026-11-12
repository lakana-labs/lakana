"""The definition routes of a table: where it is defined, and the file behind the content gate."""

from __future__ import annotations

import sys
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, PrincipalRef, Securable
from lakana_core.errors import PermissionDenied
from lakana_core.models import AuditEvent, AuditEventType, ProjectFile
from lakana_core.projects.definition import (
    NOT_RECORDED_MESSAGE,
    RESTRICTED_MESSAGE,
    TOO_LARGE_MESSAGE,
    DefinitionReason,
    TableDefinition,
    TableDefinitionFile,
)
from lakana_core.projects.source_map import SOURCE_FILE_MAX_BYTES
from lakana_server.deps import get_grant_authorization
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_table, deploy_file, person

DEF_PATH = "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/definition"
"""The location route as OpenAPI spells it; the file route adds ``/file``."""

LOCATION_KEYS = (
    "repository",
    "path",
    "line",
    "commit",
    "project_version_id",
    "deployed_at",
    "browse_url",
)
"""The location fields, every one `null` for a table no deployment defines."""

FILE_KEYS = ("content", "bytes", "reason", "message")
"""The fields the file route adds to the location."""

F_TEXT = "SELECT a FROM silver.a\n"
"""The text of the file that defines `gold.f`."""

VERSION_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
"""A project version id for the mapping test."""

DEPLOYED_AT = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
"""A deploy time for the mapping test."""


def url(dotted: str) -> str:
    """Build the location route of a table in `dev`.

    Parameters
    ----------
    dotted
        ``<namespace>.<table>``.

    Returns
    -------
    str
        The URL.
    """
    namespace, _, name = dotted.rpartition(".")
    return f"/api/v1/environments/dev/namespaces/{namespace}/tables/{name}/definition"


def table_reader(client: TestClient, *tables: str) -> dict[str, str]:
    """Create a user with `READ_TABLE` on each table and nothing else, and return its header.

    Parameters
    ----------
    client
        Test client for the API.
    *tables
        Dotted ``<namespace>.<table>`` names in `dev`.

    Returns
    -------
    dict of str to str
        The user's Authorization header.
    """
    name = f"reader-{uuid.uuid4().hex[:8]}"
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    for dotted in tables:
        namespace, _, table = dotted.rpartition(".")
        granted = client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={
                "principal": name,
                "action": "READ_TABLE",
                "kind": "TABLE",
                "environment": "dev",
                "namespace": namespace,
                "table": table,
            },
        )
        assert granted.status_code == 201, granted.text
    token = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _session(client: TestClient) -> Session:
    """Open a session on the app's database.

    Parameters
    ----------
    client
        Test client for the API.

    Returns
    -------
    Session
        A new session; the caller closes it.
    """
    return Session(client.app.state.engine)


@pytest.fixture
def defs(client: TestClient) -> TestClient:
    """Create `dev` with four tables, and deploy the file that defines `gold.f`.

    Parameters
    ----------
    client
        Test client for the API.

    Returns
    -------
    TestClient
        The same client.
    """
    create_environment(client)
    for dotted in ("gold.f", "silver.a", "gold.secret", "gold.other"):
        namespace, _, table = dotted.rpartition(".")
        create_table(client, "dev", namespace, table)
    with _session(client) as session:
        deploy_file(session, "dev", "gold.f", F_TEXT, gate_tables=("gold.f", "silver.a"))
    return client


class _Denying:
    """An authorization stand-in that refuses everything and runs no statement itself.

    Parameters
    ----------
    refused
        Each refused action is appended here.
    """

    def __init__(self, refused: list[str]) -> None:
        self._refused = refused

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse the action.

        Parameters
        ----------
        principal
            The caller.
        action
            The action asked for.
        securable
            Its target.

        Raises
        ------
        PermissionDenied
            Always.
        """
        self._refused.append(action.value)
        raise PermissionDenied("refused by the test")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Permit nothing.

        Parameters
        ----------
        principal
            The caller.
        action
            The action asked for.
        securable
            Its target.

        Returns
        -------
        bool
            Always `False`.
        """
        return False


_RECORD = AuditService.record
"""The real `AuditService.record`, which `_record_refusing_source_viewed` calls for other events."""


def _record_refusing_source_viewed(
    self: AuditService,
    principal_id: uuid.UUID | None,
    event_type: AuditEventType,
    *,
    securable: Securable | None = None,
    detail: dict | None = None,
    commit: bool = False,
) -> AuditEvent:
    """Fail a `SOURCE_VIEWED` write as a database that refuses it does; write any other event.

    Parameters
    ----------
    self
        The audit service.
    principal_id
        The actor.
    event_type
        The event.
    securable
        The event's object.
    detail
        The event's detail.
    commit
        Whether the write commits.

    Returns
    -------
    AuditEvent
        The event, for any type but `SOURCE_VIEWED`.

    Raises
    ------
    RuntimeError
        For `SOURCE_VIEWED`.
    """
    if event_type == AuditEventType.SOURCE_VIEWED:
        raise RuntimeError("audit write failed")
    return _RECORD(
        self, principal_id, event_type, securable=securable, detail=detail, commit=commit
    )


_OPENED: list[tuple[str, object]] | None = None
"""The file-system events the audit hook collects while a test arms it; `None` when unarmed."""

_WATCHDOG = "pytest-memory-watchdog"
"""The thread of `scripts/pytest_memory_limit.py`, which reads `/proc` on its own schedule."""

_FILE_EVENTS = frozenset({"open", "os.listdir", "os.scandir", "glob.glob", "shutil.copyfile"})
"""The audit events that read or list the file system."""


def _hook(name: str, args: tuple[object, ...]) -> None:
    """Collect file-system events while armed; an audit hook cannot be removed, so it idles.

    The test suite's memory watchdog thread is left out: it measures the process from outside
    any request.

    Parameters
    ----------
    name
        The audit event.
    args
        Its arguments.
    """
    if (
        _OPENED is not None
        and name in _FILE_EVENTS
        and threading.current_thread().name != _WATCHDOG
    ):
        _OPENED.append((name, args[0] if args else None))


sys.addaudithook(_hook)


@pytest.fixture
def opened() -> Iterator[list[tuple[str, object]]]:
    """Arm the audit hook for the test's body.

    Yields
    ------
    list of tuple
        Every file-system event while armed.
    """
    global _OPENED
    events: list[tuple[str, object]] = []
    _OPENED = events
    try:
        yield events
    finally:
        _OPENED = None


def test_definition_has_no_path_commit_or_version_parameter(defs: TestClient) -> None:
    for route in (DEF_PATH, DEF_PATH + "/file"):
        op = defs.app.openapi()["paths"][route]["get"]
        assert "requestBody" not in op
        assert {(p["name"], p["in"]) for p in op["parameters"]} == {
            ("env", "path"),
            ("path", "path"),
            ("name", "path"),
            ("authorization", "header"),
        }


def test_definition_answers_the_same_for_a_missing_and_an_unreadable_table(
    defs: TestClient,
) -> None:
    h = table_reader(defs, "gold.other")
    for suffix in ("", "/file"):
        assert defs.get(url("gold.other") + suffix, headers=h).status_code == 200
        secret = defs.get(url("gold.secret") + suffix, headers=h)
        nothing = defs.get(url("gold.nothing") + suffix, headers=h)
        assert (secret.status_code, nothing.status_code) == (403, 403)
        assert secret.content == nothing.content


def test_no_statement_runs_after_a_refused_authorize(defs: TestClient) -> None:
    ann = person(defs, "ann", "ANALYST")
    engine = defs.app.state.engine
    refused: list[str] = []
    after: list[str] = []

    def listen(*args: object) -> None:
        """Keep each statement's text once a refusal happened.

        Parameters
        ----------
        *args
            `before_cursor_execute`'s arguments; the third is the statement.
        """
        if refused:
            after.append(str(args[2]))

    defs.app.dependency_overrides[get_grant_authorization] = lambda: _Denying(refused)
    event.listen(engine, "before_cursor_execute", listen)
    try:
        for suffix in ("", "/file"):
            refused.clear()
            assert defs.get(url("gold.f") + suffix, headers=ann).status_code == 403
            assert refused == ["READ_TABLE"]
    finally:
        event.remove(engine, "before_cursor_execute", listen)
        defs.app.dependency_overrides.pop(get_grant_authorization)
    assert after == []


def test_the_flat_rest_shape_maps_every_field_of_the_core_answer() -> None:
    from lakana_server.api.platform.tables import _definition_file_out

    core = TableDefinitionFile(
        definition=TableDefinition(
            True,
            "analytics",
            "assets/gold/f.sql",
            "a" * 40,
            1,
            VERSION_ID,
            DEPLOYED_AT,
            "https://github.com/x",
        ),
        content=None,
        bytes=None,
        reason=DefinitionReason.RESTRICTED,
        message=RESTRICTED_MESSAGE,
    )
    assert _definition_file_out(core).model_dump() == {
        "defined": True,
        "repository": "analytics",
        "path": "assets/gold/f.sql",
        "line": 1,
        "commit": "a" * 40,
        "project_version_id": str(VERSION_ID),
        "deployed_at": DEPLOYED_AT,
        "browse_url": "https://github.com/x",
        "content": None,
        "bytes": None,
        "reason": "restricted",
        "message": RESTRICTED_MESSAGE,
    }


def test_a_reader_gets_the_location_and_an_environment_reader_the_content(
    defs: TestClient,
) -> None:
    loc = defs.get(url("gold.f"), headers=table_reader(defs, "gold.f", "silver.a")).json()
    assert loc["defined"] is True
    assert (loc["repository"], loc["path"], loc["line"], loc["commit"]) == (
        "analytics",
        "assets/gold/f.sql",
        1,
        "a" * 40,
    )
    assert loc["browse_url"].startswith("https://github.com/acme/analytics/")
    assert set(loc) == {"defined", *LOCATION_KEYS}
    body = defs.get(url("gold.f") + "/file", headers=person(defs, "ann", "ANALYST")).json()
    assert (body["content"], body["bytes"], body["reason"], body["message"]) == (
        F_TEXT,
        len(F_TEXT.encode()),
        None,
        None,
    )
    assert {k: body[k] for k in LOCATION_KEYS} == {k: loc[k] for k in LOCATION_KEYS}
    with _session(defs) as session:
        viewed = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.SOURCE_VIEWED)
        )
    assert viewed == 1


def test_a_table_reader_gets_restricted_without_content(defs: TestClient) -> None:
    body = defs.get(
        url("gold.f") + "/file", headers=table_reader(defs, "gold.f", "silver.a")
    ).json()
    assert body["path"] == "assets/gold/f.sql"
    assert (body["content"], body["bytes"], body["reason"], body["message"]) == (
        None,
        None,
        "restricted",
        RESTRICTED_MESSAGE,
    )


def test_an_old_gate_version_answers_not_recorded(defs: TestClient) -> None:
    with _session(defs) as session:
        session.execute(update(ProjectFile).values(gate_version=0))
        session.commit()
    body = defs.get(url("gold.f") + "/file", headers=person(defs, "ann", "ANALYST")).json()
    assert (body["content"], body["reason"], body["message"]) == (
        None,
        "not_recorded",
        NOT_RECORDED_MESSAGE,
    )


def test_a_file_over_the_limit_answers_too_large(defs: TestClient) -> None:
    create_table(defs, "dev", "gold", "big")
    text = "SELECT 1\n" + "-" * SOURCE_FILE_MAX_BYTES
    with _session(defs) as session:
        deploy_file(session, "dev", "gold.big", text, gate_tables=("gold.big",))
    body = defs.get(url("gold.big") + "/file", headers=person(defs, "ann", "ANALYST")).json()
    assert (body["content"], body["bytes"], body["reason"], body["message"]) == (
        None,
        None,
        "too_large",
        TOO_LARGE_MESSAGE,
    )


def test_a_failed_audit_write_is_503_with_no_content(
    defs: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ann = person(defs, "ann", "ANALYST")
    monkeypatch.setattr(AuditService, "record", _record_refusing_source_viewed)
    response = defs.get(url("gold.f") + "/file", headers=ann)
    assert response.status_code == 503
    assert "SELECT" not in response.text
    assert "Retry-After" in response.headers


@pytest.mark.parametrize("forged", ["../x", "/etc/passwd", "%2e%2e/%2e%2e/etc/passwd"])
def test_a_forged_path_in_project_files_is_never_opened(
    defs: TestClient, opened: list[tuple[str, object]], forged: str
) -> None:
    ann = person(defs, "ann", "ANALYST")
    with _session(defs) as session:
        session.execute(update(ProjectFile).values(path=forged))
        session.commit()
    # A first answer loads whatever the route imports lazily; only the second is watched.
    assert defs.get(url("gold.f") + "/file", headers=ann).status_code == 200
    opened.clear()
    response = defs.get(url("gold.f") + "/file", headers=ann)
    events = list(opened)
    body = response.json()
    assert response.status_code == 200
    assert (body["path"], body["content"]) == (forged, F_TEXT)
    assert body["browse_url"] is None or forged not in body["browse_url"]
    assert events == []


def test_definition_of_a_dropped_table_is_404_for_a_reader(defs: TestClient) -> None:
    dropped = defs.delete("/api/v1/environments/dev/namespaces/gold/tables/f", headers=AUTH_HEADER)
    assert dropped.status_code == 204, dropped.text
    for suffix in ("", "/file"):
        assert defs.get(url("gold.f") + suffix, headers=AUTH_HEADER).status_code == 404


def test_definition_in_an_unknown_environment_is_404(defs: TestClient) -> None:
    missing = "/api/v1/environments/nope/namespaces/gold/tables/f/definition"
    for suffix in ("", "/file"):
        assert defs.get(missing + suffix, headers=AUTH_HEADER).status_code == 404


def test_definition_of_a_table_no_project_defines_is_not_defined(defs: TestClient) -> None:
    assert defs.get(url("gold.other"), headers=AUTH_HEADER).json() == {
        "defined": False,
        **{k: None for k in LOCATION_KEYS},
    }
    assert defs.get(url("gold.other") + "/file", headers=AUTH_HEADER).json() == {
        "defined": False,
        **{k: None for k in LOCATION_KEYS},
        **{k: None for k in FILE_KEYS},
    }
