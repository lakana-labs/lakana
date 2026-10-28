"""The connection check of a `postgresql` connection against a real PostgreSQL server."""

from __future__ import annotations

import gc
import warnings

import pytest
from testcontainers.community.postgres import PostgresContainer

from lakana.ingest import _check

pytestmark = pytest.mark.integration


def test_the_postgresql_check_logs_in_and_leaves_nothing_open() -> None:
    """A login and `SELECT 1` succeed, and no cursor is left to the garbage collector (#10)."""
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
    assert [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)] == []
