"""The connection check of a `postgresql` connection against a real PostgreSQL server."""

from __future__ import annotations

import gc
import warnings

import adbc_driver_manager.dbapi as adbc
import pytest
from testcontainers.community.postgres import PostgresContainer

from lakana.ingest import _check

pytestmark = pytest.mark.integration


def test_the_postgresql_check_logs_in_and_leaves_nothing_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login and `SELECT 1` succeed, and no cursor is left to the garbage collector (#10)."""
    opened: list[adbc.Cursor] = []
    real_cursor = adbc.Connection.cursor

    def recording_cursor(self: adbc.Connection, *args: object, **kwargs: object) -> adbc.Cursor:
        cursor = real_cursor(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]
        opened.append(cursor)
        return cursor

    # `Connection.close` closes the cursors still open, so what matters is their state when the
    # check closes the connection.
    open_at_close: list[int] = []
    real_close = adbc.Connection.close

    def recording_close(self: adbc.Connection) -> None:
        open_at_close.append(sum(not cursor._closed for cursor in opened))
        real_close(self)

    monkeypatch.setattr(adbc.Connection, "cursor", recording_cursor)
    monkeypatch.setattr(adbc.Connection, "close", recording_close)
    with PostgresContainer("postgres:16-alpine", driver=None) as server:
        port = int(server.get_exposed_port(5432))
        body = {
            "name": "crm",
            "kind": "postgresql",
            "address": {"host": "localhost", "port": port, "database": server.dbname},
            "settings": {"user": server.username, "tls": "disable"},
            "endpoints": [{"host": "localhost", "port": port, "role": "service"}],
            "secret": {"password": server.password},
        }
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            outcome, message = _check.check(body, {}, timeout=30.0)
            gc.collect()

    assert outcome == "ok", message
    # Directly: the check closed every cursor it opened before it closed the connection.
    assert opened
    assert open_at_close == [0]
    assert [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)] == []
