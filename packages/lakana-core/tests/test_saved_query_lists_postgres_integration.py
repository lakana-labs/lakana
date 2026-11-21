"""On Postgres the saved-query lists give the pages of the old Python filter, cursor for cursor.

The Debian `postgres:16` image collates text under glibc's `en_US.utf8`, which orders ``dev-eu``
after ``deva`` and ``Straße`` apart from ``strasse``; Python, and so a cursor, orders by code point.
The probe below shows the collation would reorder the world's names if the lists used it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from saved_query_test_helpers import LIMITS, LISTS, build_world, new_pages, old_pages
from sqlalchemy import text
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.models import Base

pytestmark = pytest.mark.integration


def test_every_list_gives_each_kind_of_caller_the_pages_of_the_python_filter_on_postgres(
    tmp_path: Path,
) -> None:
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            probe = session.execute(
                text("SELECT v FROM (VALUES ('dev-eu'), ('deva')) AS t(v) ORDER BY v")
            ).scalars()
            assert list(probe) == ["deva", "dev-eu"], "the collation orders by code point"
            callers = build_world(session, tmp_path)
            for caller in callers:
                for environment in LISTS:
                    for limit in LIMITS:
                        expected = old_pages(session, caller, environment, limit)
                        got = new_pages(session, caller, environment, limit)
                        assert got == expected, (caller.label, environment, limit)
        engine.dispose()
