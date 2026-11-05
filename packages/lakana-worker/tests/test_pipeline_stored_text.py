"""Stored and generated SQL never reaches a reader's message; the reader's own SQL does."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlglot.errors
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_pipeline_views import _grant_read, _principal, _query, _view, admin, catalog  # ruff: ignore[unused-import]

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.models import QueryRunStatus, RowFilter, Table
from lakana_core.policy.field_binding import UNREADABLE_POLICY_MESSAGE
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.runs.tasks import FailureKind
from lakana_worker import pipeline
from lakana_worker.task_outcome import classify

_TAMPERED = [
    "SELECT id FROM bronze.payroll_secret WHERE ssn_hash = ((( +",
    "SELECT id FROM bronze.orders |> 'bronze.payroll_secret join key is ssn'",
    "SELECT 'unterminated FROM bronze.payroll_secret",
]


def _tamper(session: Session, sql: str) -> None:
    """Replace the stored SQL of view `gold.v` directly in the database.

    Parameters
    ----------
    session
        Database session.
    sql
        The text to store.
    """
    view = session.execute(select(Table).where(Table.name == "v")).scalar_one()
    view.view_definition = {**(view.view_definition or {}), "sql": sql}
    session.commit()


@pytest.mark.parametrize("stored", _TAMPERED)
def test_a_stored_view_that_stops_parsing_names_the_view_and_none_of_its_sql(
    session: Session,
    admin: PrincipalRef,  # ruff: ignore[redefined-while-unused]
    catalog: CatalogService,  # ruff: ignore[redefined-while-unused]
    tmp_path: Path,
    stored: str,
) -> None:
    """A reader with READ on the view only is told which view, not what it says."""
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")
    _tamper(session, stored)
    bob = _principal(session, "bob")
    _grant_read(session, admin, "bob", "gold.v")

    run, rows = _query(session, bob, tmp_path, "SELECT * FROM gold.v")

    assert run.status == QueryRunStatus.FAILED and rows == []
    assert run.error_message == ("View 'gold.v' could not be read. Ask its owner to recreate it.")
    assert "syntax" not in run.error_message.lower()


def test_a_broken_view_inside_another_is_refused_before_its_sql_is_read(
    session: Session,
    admin: PrincipalRef,  # ruff: ignore[redefined-while-unused]
    catalog: CatalogService,  # ruff: ignore[redefined-while-unused]
    tmp_path: Path,
) -> None:
    """A reader of the outer view only never reaches the inner view's SQL or its name."""
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")
    _view(session, admin, "gold.w", "SELECT id FROM gold.v")
    _tamper(session, _TAMPERED[0])
    bob = _principal(session, "bob")
    _grant_read(session, admin, "bob", "gold.w")

    run, rows = _query(session, bob, tmp_path, "SELECT * FROM gold.w")

    assert run.status == QueryRunStatus.FAILED and rows == []
    assert run.error_message == "Resolving the tables this query reads failed (PermissionDenied)."


def test_a_reader_s_own_syntax_error_against_a_healthy_view_keeps_the_syntax_message(
    session: Session,
    admin: PrincipalRef,  # ruff: ignore[redefined-while-unused]
    catalog: CatalogService,  # ruff: ignore[redefined-while-unused]
    tmp_path: Path,
) -> None:
    """The reader's SQL is parsed first, so their mistake is not taken for the view's."""
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")
    bob = _principal(session, "bob")
    _grant_read(session, admin, "bob", "gold.v")

    run, _ = _query(session, bob, tmp_path, "SELEC * FROM gold.v")

    assert run.error_cause == "invalid_query"
    assert run.error_message is not None
    assert run.error_message.startswith("SQL syntax error at line 1, column ")


@pytest.mark.parametrize("function", ["extract_table_refs", "rewrite_query"])
def test_a_reparse_of_expanded_text_names_the_view_not_its_sql(
    session: Session,
    admin: PrincipalRef,  # ruff: ignore[redefined-while-unused]
    catalog: CatalogService,  # ruff: ignore[redefined-while-unused]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    function: str,
) -> None:
    """If the expanded statement, which holds view SQL, stops parsing, the view is named."""
    _view(session, admin, "gold.v", "SELECT id FROM bronze.orders")
    real = getattr(pipeline, function)
    calls: list[str] = []

    def failing(sql: str, *args: str) -> object:
        calls.append(sql)
        if len(calls) > (1 if function == "extract_table_refs" else 0):
            raise sqlglot.errors.ParseError(
                "x",
                errors=[{"description": "d", "line": 1, "col": 2, "highlight": "SECRETVIEWTEXT"}],
            )
        return real(sql, *args)

    monkeypatch.setattr(pipeline, function, failing)

    run, _ = _query(
        session, _principal(session, "bob"), tmp_path, "SELECT * FROM gold.v", granted=False
    )

    assert run.error_message == "View 'gold.v' could not be read. Ask its owner to recreate it."
    assert "SECRETVIEWTEXT" not in (run.error_message or "")


def test_a_reparse_failure_without_a_view_is_not_blamed_on_one(
    session: Session,
    admin: PrincipalRef,  # ruff: ignore[redefined-while-unused]
    catalog: CatalogService,  # ruff: ignore[redefined-while-unused]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no view in the statement the syntax error is the reader's own and says so."""
    real = pipeline.extract_table_refs
    calls: list[str] = []

    def failing(sql: str, *args: str) -> list[str]:
        calls.append(sql)
        if len(calls) > 1:
            raise sqlglot.errors.ParseError("x", errors=[{"description": "d", "line": 1, "col": 2}])
        return real(sql, *args)

    monkeypatch.setattr(pipeline, "extract_table_refs", failing)

    run, _ = _query(session, admin, tmp_path, "SELECT * FROM bronze.orders", granted=False)

    assert run.error_message is not None
    assert run.error_message.startswith("SQL syntax error")


def test_a_deeply_nested_query_is_invalid_and_permanent(
    session: Session,
    admin: PrincipalRef,  # ruff: ignore[redefined-while-unused]
    catalog: CatalogService,  # ruff: ignore[redefined-while-unused]
    tmp_path: Path,
) -> None:
    """B: 400 nested parentheses overflow the parser; that is the query's fault, not retryable."""
    run, _ = _query(session, admin, tmp_path, "SELECT " + "(" * 400 + "1" + ")" * 400)

    assert run.status == QueryRunStatus.FAILED
    assert run.error_cause == "invalid_query"
    assert run.error_message is not None
    assert "nested too deeply" in run.error_message
    assert "Unexpected error" not in run.error_message
    assert classify(RecursionError()) is FailureKind.PERMANENT


def test_a_broken_stored_policy_on_the_read_path_gives_the_unreadable_policy_message(
    session: Session,
    admin: PrincipalRef,  # ruff: ignore[redefined-while-unused]
    catalog: CatalogService,  # ruff: ignore[redefined-while-unused]
    tmp_path: Path,
) -> None:
    """C: an unreadable policy is named with a fixed text that carries none of its own text."""
    bob = _principal(session, "bob")
    _grant_read(session, admin, "bob", "bronze.orders")
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        admin,
        principal="bob",
        environment="dev",
        namespace="bronze",
        table="orders",
        expression="id > 1",
    )
    row = session.execute(select(RowFilter)).scalar_one()
    row.expression = "region = 'SECRETWORD' AND ((( +"
    session.commit()

    run, rows = _query(session, bob, tmp_path, "SELECT * FROM bronze.orders")

    assert rows == []
    assert run.error_message == UNREADABLE_POLICY_MESSAGE
    assert "SECRETWORD" not in run.error_message


def test_the_not_published_text_carries_no_storage_location(
    session: Session,
    admin: PrincipalRef,  # ruff: ignore[redefined-while-unused]
    catalog: CatalogService,  # ruff: ignore[redefined-while-unused]
    tmp_path: Path,
) -> None:
    """The text holds the name the query used, a check name and a run id, and nothing else."""
    run_id = uuid.uuid4()
    session.execute(update(Table).where(Table.name == "orders").values(unpublished_run_id=run_id))
    session.commit()
    bob = _principal(session, "bob")
    _grant_read(session, admin, "bob", "bronze.orders")

    run, rows = _query(session, bob, tmp_path, "SELECT * FROM bronze.orders")

    assert run.status == QueryRunStatus.FAILED and rows == []
    assert run.error_message == (
        "`bronze.orders` has not been published: its first build did not finish, "
        f"or failed a check (run {run_id}). It is published by the next run that builds it "
        "and passes its checks."
    )
    assert str(tmp_path) not in run.error_message
    assert "file://" not in run.error_message
    assert ".parquet" not in run.error_message and "metadata" not in run.error_message
