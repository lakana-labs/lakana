"""`lakana.ingest.connections`: a helper's lookup of a declared connection inside a task."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from lakana import context
from lakana.errors import PermissionDenied
from lakana.ingest import (
    CapExceeded,
    ConnectionChanged,
    ConnectionNotDeclared,
    ConnectionRefused,
    IngestError,
    SourceError,
    _connections,
    connections,
)

_PASSWORD = "fetched-s3cr3t"
_BODY: dict[str, object] = {
    "name": "crm-db",
    "kind": "postgresql",
    "address": {"host": "db.example.com", "port": 5432, "database": "crm"},
    "settings": {"user": "reader", "tls": "verify-full"},
    "endpoints": [{"host": "db.example.com", "port": 5432, "role": "service"}],
    "secret": {"password": _PASSWORD},
}


class _TaskConnections:
    """A fake `client.task_connections` that counts its requests.

    Parameters
    ----------
    answer
        The body to return, or an exception to raise.
    """

    def __init__(self, answer: dict[str, object] | Exception) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def get(self, name: str) -> dict[str, object]:
        """Answer one fetch.

        Parameters
        ----------
        name
            The connection's name.

        Returns
        -------
        dict
            The scripted body.

        Raises
        ------
        Exception
            The scripted exception.
        """
        self.asked.append(name)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@dataclass(frozen=True)
class _Client:
    """A fake SDK client with only `task_connections`.

    Parameters
    ----------
    task_connections
        The fake resource.
    """

    task_connections: _TaskConnections


def _state(
    answer: dict[str, object] | Exception, routing: dict[str, object] | None = None
) -> _TaskConnections:
    """Set the task state of an attempt of `gold.scores` that declares `crm-db`.

    Parameters
    ----------
    answer
        What the fake server answers.
    routing
        The task description's entry for `crm-db`; only its kind when `None`.

    Returns
    -------
    _TaskConnections
        The fake resource, to count its requests.
    """
    fake = _TaskConnections(answer)
    context._set(
        context._TaskState(
            client=_Client(fake),  # ty: ignore[invalid-argument-type]
            environment="dev",
            asset="gold.scores",
            declared=frozenset({"crm-db"}),
            connections={"crm-db": routing if routing is not None else {"kind": "postgresql"}},
        )
    )
    return fake


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    """Leave no task state and no fetched connection behind.

    Yields
    ------
    None
        While the test runs.
    """
    yield
    context._set(None)
    _connections.clear()


def test_a_declared_connection_is_fetched_once_per_attempt() -> None:
    fake = _state(_BODY)

    first = connections.get("crm-db")
    second = connections.get("crm-db")

    assert fake.asked == ["crm-db"]
    assert second == first
    assert (first.name, first.kind) == ("crm-db", "postgresql")
    assert first.address == {"host": "db.example.com", "port": 5432, "database": "crm"}
    assert first.settings == {"user": "reader", "tls": "verify-full"}
    assert [(e.host, e.port, e.role) for e in first.endpoints] == [
        ("db.example.com", 5432, "service")
    ]
    assert first.secret is not None and first.secret["password"] == _PASSWORD
    assert dict(first.secret) == {"password": _PASSWORD}
    assert (first.proxy_url, first.relay_address, first.root_path) == (None, None, None)


def test_the_secret_never_shows_in_a_repr_or_a_str() -> None:
    _state(_BODY)
    found = connections.get("crm-db")
    assert found.secret is not None
    assert repr(found.secret) == "{'password': '***'}"
    assert str(found.secret) == "{'password': '***'}"
    assert f"{found.secret}" == "{'password': '***'}"
    assert _PASSWORD not in repr(found)
    assert (len(found.secret), list(found.secret)) == (1, ["password"])


def test_every_secret_value_is_registered_with_the_harness() -> None:
    _state({**_BODY, "secret": {"user": "loader-user", "password": 'p"w\\d'}})
    connections.get("crm-db")
    hidden = context.current().secrets
    assert {"loader-user", 'p"w\\d'} <= hidden
    # The forms a value takes in a repr, in JSON and in a URL are hidden too.
    assert {'p"w\\\\d', 'p\\"w\\\\d', "p%22w%5Cd"} <= hidden


def test_a_connection_without_a_secret_has_none() -> None:
    _state({**_BODY, "secret": None})
    assert connections.get("crm-db").secret is None


def test_an_undeclared_name_is_refused_before_any_request() -> None:
    fake = _state(_BODY)
    with pytest.raises(ConnectionNotDeclared) as refused:
        connections.get("files")
    assert str(refused.value) == (
        "`files` is not declared by this asset: add it to @asset(connections=[...]) and deploy."
    )
    assert fake.asked == []


def test_a_refusal_of_the_server_keeps_its_text() -> None:
    _state(PermissionDenied("connection `crm-db` is not in environment `dev`", status=403))
    with pytest.raises(ConnectionRefused) as refused:
        connections.get("crm-db")
    assert str(refused.value) == "connection `crm-db` is not in environment `dev`"
    assert refused.value.__cause__ is None


def test_an_answer_that_is_not_a_connection_is_refused_without_its_values() -> None:
    _state({**_BODY, "secret": {"password": 7}, "endpoints": "x"})
    with pytest.raises(IngestError) as refused:
        connections.get("crm-db")
    assert "7" not in str(refused.value)
    assert "crm-db" in str(refused.value)


def test_outside_a_task_the_lookup_says_so() -> None:
    with pytest.raises(RuntimeError) as outside:
        connections.get("crm-db")
    assert "only inside a Lakana task" in str(outside.value)


def test_the_errors_say_whether_another_attempt_could_help() -> None:
    """Read by the harness: a refusal repeats, a changed connection does not."""
    assert ConnectionNotDeclared.retryable is False
    assert ConnectionRefused.retryable is False
    assert CapExceeded.retryable is False
    assert ConnectionChanged.retryable is True
    assert SourceError.retryable is None
    for kind in (ConnectionNotDeclared, ConnectionRefused, CapExceeded, ConnectionChanged):
        assert issubclass(kind, IngestError)


_GRANT: dict[str, object] = {"kind": "postgresql", "grant": [["db.example.com", 5432]]}


def test_a_connection_changed_since_the_grant_fails_transiently() -> None:
    """Review Focus 4: the fetched endpoints differ from the grant the attempt started with."""
    moved = {**_BODY, "endpoints": [{"host": "db2.example.com", "port": 5432, "role": "service"}]}
    fake = _state(moved, routing=_GRANT)

    with pytest.raises(ConnectionChanged) as caught:
        connections.get("crm-db")
    assert str(caught.value) == (
        "connection `crm-db` changed after this attempt started; the next attempt uses the new "
        "address"
    )
    assert caught.value.retryable is True
    with pytest.raises(ConnectionChanged):
        connections.get("crm-db")
    assert fake.asked == ["crm-db", "crm-db"]
    assert _PASSWORD in context.current().secrets


def test_a_connection_that_matches_its_grant_is_kept() -> None:
    fake = _state(_BODY, routing=_GRANT)
    connections.get("crm-db")
    connections.get("crm-db")
    assert fake.asked == ["crm-db"]


def test_a_grant_that_is_not_a_list_reads_as_a_change() -> None:
    """A broken description fails safe: the attempt is retried rather than run unchecked."""
    _state(_BODY, routing={"kind": "postgresql", "grant": "db.example.com"})
    with pytest.raises(ConnectionChanged):
        connections.get("crm-db")


def test_get_fills_the_routing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker routing: a relay address for a database, the proxy for HTTPS, a path for a root."""
    proxy = "http://lakana:tok@egress:3128"
    monkeypatch.setenv("LAKANA_EGRESS_PROXY", proxy)
    _state(_BODY, routing={**_GRANT, "relay_port": 15002})
    db = connections.get("crm-db")
    assert (db.relay_address, db.proxy_url, db.root_path) == ("lakana-relay:15002", None, None)

    _connections.clear()
    api_body = {
        **_BODY,
        "kind": "http",
        "address": {"base_url": "https://api.example.com"},
        "settings": {},
        "endpoints": [{"host": "api.example.com", "port": 443, "role": "service"}],
    }
    _state(api_body, routing={"kind": "http", "grant": [["api.example.com", 443]]})
    api = connections.get("crm-db")
    assert (api.relay_address, api.proxy_url, api.root_path) == (None, proxy, None)

    _connections.clear()
    files_body = {
        **_BODY,
        "kind": "directory",
        "address": {"root": "shared", "path": "in"},
        "settings": {},
        "endpoints": [],
        "secret": None,
    }
    _state(
        files_body,
        routing={
            "kind": "directory",
            "root_dir": "/ingest/shared",
            "path": "in",
            "root_path": "/ingest/shared/in",
        },
    )
    files = connections.get("crm-db")
    assert (files.relay_address, files.proxy_url, files.root_path) == (
        None,
        None,
        "/ingest/shared/in",
    )


def test_without_routing_fields_they_stay_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local runner: no relay port, no proxy in the environment; connect to `address`."""
    monkeypatch.delenv("LAKANA_EGRESS_PROXY", raising=False)
    _state(
        {
            **_BODY,
            "kind": "http",
            "endpoints": [{"host": "api.example.com", "port": 443, "role": "service"}],
        },
        routing={"kind": "http", "grant": [["api.example.com", 443]]},
    )
    found = connections.get("crm-db")
    assert (found.proxy_url, found.relay_address, found.root_path) == (None, None, None)
