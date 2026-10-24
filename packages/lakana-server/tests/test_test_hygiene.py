"""Guards: server tests never share a fixed storage root or a catalog database."""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from lakana_core.db import create_engine
from lakana_core.models import Base
from lakana_server.settings import Settings


def test_no_server_test_hard_codes_a_tmp_storage_root() -> None:
    """Every storage root comes from `tmp_path`, so runs can't interfere or leak files."""
    tests = Path(__file__).parent
    offenders = [
        f"{path.relative_to(tests)}:{n}"
        for path in tests.rglob("*.py")
        if path.name != Path(__file__).name
        for n, line in enumerate(path.read_text().splitlines(), start=1)
        if "file:///tmp/lakana-test" in line
    ]

    assert not offenders, offenders


@pytest.mark.parametrize("attempt", ["first", "second"])
def test_every_test_gets_a_catalog_of_its_own_with_the_full_schema(
    attempt: str, settings: Settings, catalog_template: Path, tmp_path: Path
) -> None:
    """A test's database is a fresh copy: the schema is complete and nothing leaks in or out.

    Both parametrized runs first check that no earlier test's write is visible, then write,
    so whichever runs second (in any order, in any process) would see a leak.
    """
    database = Path(settings.database.url.removeprefix("sqlite:///"))
    assert database.parent == tmp_path
    assert database != catalog_template
    engine = create_engine(settings.database)
    try:
        assert set(inspect(engine).get_table_names()) >= set(Base.metadata.tables)
        with engine.begin() as connection:
            assert not inspect(connection).has_table("leak_probe")
            connection.execute(text("CREATE TABLE leak_probe (attempt TEXT)"))
            connection.execute(text("INSERT INTO leak_probe VALUES (:a)"), {"a": attempt})
    finally:
        engine.dispose()
    with sqlite3.connect(f"file:{catalog_template}?mode=ro", uri=True) as template:
        names = {row[0] for row in template.execute("SELECT name FROM sqlite_master")}
    assert "leak_probe" not in names


def test_the_catalog_template_cannot_be_written(catalog_template: Path) -> None:
    """The shared template is read-only, so a test that opened it by mistake fails loudly."""
    assert not catalog_template.stat().st_mode & 0o222


def test_a_test_database_skips_fsync_but_keeps_every_production_pragma(
    settings: Settings,
) -> None:
    """Commits in tests skip the disk flush; WAL, foreign keys and busy timeout stay as shipped.

    A unit test cannot observe durability across a power cut, so flushing every commit to
    disk only costs time. Everything a test can observe comes from the production pragmas.
    """
    engine = create_engine(settings.database)
    try:
        with engine.connect() as connection:
            pragma = connection.exec_driver_sql
            assert pragma("PRAGMA synchronous").scalar() == 0
            assert pragma("PRAGMA journal_mode").scalar() == "wal"
            assert pragma("PRAGMA foreign_keys").scalar() == 1
            assert pragma("PRAGMA busy_timeout").scalar_one() > 0
    finally:
        engine.dispose()
