"""The query engine's error text is replaced on a read under a row filter or column mask.

A policy expression runs on every stored value, so when it fails the engine's text quotes a
value the policy hides. These tests run real DuckDB with real policy resolution and pin that a
failure of the kinds that quote a value is recorded as one fixed message, with and without a
read through a field-id map, and that a query with no policy keeps the engine's own text.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.ipc
import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.query_service import QueryService, WriteTarget
from lakana_core.catalog.service import CatalogService
from lakana_core.config import QueryLimitsConfig, StorageConfig
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    ColumnMask,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    RowFilter,
    WriteMode,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.field_binding import FIELD_IDS, UNBOUND
from lakana_core.policy.read_errors import policy_read_failure
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import executor as executor_module
from lakana_worker import query_child
from lakana_worker.executor import (
    _KINDS,
    INTERNAL_ADVICE,
    KILLED_FROM_OUTSIDE_MESSAGE,
    DuckDBInternalError,
    OutOfMemoryQuery,
    TransientQueryError,
)
from lakana_worker.pipeline import _without_the_value, failure_cause, run_one
from lakana_worker.query_process import ChildFailed, StopReason
from lakana_worker.query_protocol import MEMORY_LIMIT_ADVICE, STORE_CERTIFICATE_UNVERIFIED

_SECRET = "HIDDEN-X"
_BIG = 2_000_000_000


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Provide the acting principal, with a persisted row.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        The administrator, named ``admin``.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name="admin"))
    session.commit()
    return PrincipalRef(id=principal_id, name="admin", type=PrincipalType.USER)


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide a catalog with a `dev` environment rooted in `tmp_path`, with two tables.

    `gold.people` holds one row (`secret` text, `num` 2 000 000 000, `code` text);
    `gold.plain` holds the same row.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The environment's storage root.

    Returns
    -------
    CatalogService
        The catalog; it allows everything.
    """
    service = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    service.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table(
        {
            "id": pa.array([1], pa.int64()),
            "secret": pa.array([_SECRET], pa.string()),
            "num": pa.array([_BIG], pa.int32()),
            "code": pa.array([_SECRET], pa.string()),
            "low": pa.array([-(2**31)], pa.int32()),
        }
    )
    for name in ("people", "plain"):
        service.write_batches(
            principal,
            TableRef(
                environment="dev",
                namespace=NamespacePath.from_parts(["gold"]),
                name=name,
            ),
            mode=WriteMode.CREATE,
            schema=data.schema,
            batches=data.to_batches(),
        )
    return service


def _mask(
    session: Session, principal: PrincipalRef, catalog: CatalogService, column: str, sql: str
) -> None:
    """Put a column mask on `gold.people` for ``admin``, with any expression.

    The service refuses the expressions these tests need (a cast, a product), so the mask is
    created with ``NULL`` and the stored expression is then replaced, as an edit in the database
    can do. It is stored as a mask of an earlier release, not bound to field ids: a bound mask
    whose text names a column outside its binding is refused as unreadable.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    catalog
        The catalog.
    column
        The masked column.
    sql
        The mask expression, stored as it is.
    """
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="people",
        column=column,
        expression="NULL",
    )
    session.execute(
        update(ColumnMask).values(
            expression=sql, binding=UNBOUND, field_ids=None, column_field_id=None
        )
    )
    session.commit()


def _filter(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    table: str,
    sql: str,
    *,
    stored: bool = True,
) -> None:
    """Put a row filter on a table of `gold` for ``admin``.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    catalog
        The catalog.
    table
        The table's name.
    sql
        The filter expression.
    stored
        Create the filter with ``id > 0`` and then replace the expression, as `_mask` does, so an
        expression the service refuses can be stored like an old policy (not bound to field
        ids); `False` creates the
        filter with `sql` itself, bound to field ids.
    """
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table=table,
        expression="id > 0" if stored else sql,
    )
    if stored:
        session.execute(
            update(RowFilter)
            .where(RowFilter.table == table)
            .values(expression=sql, binding=UNBOUND, field_ids=None)
        )
        session.commit()


def _query(session: Session, principal: PrincipalRef, sql: str) -> QueryRun:
    """Submit a query and run it in the worker.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    sql
        The query.

    Returns
    -------
    QueryRun
        The finished run, refreshed.
    """
    run = QueryService(session, AllowAllAuthorization()).submit_query(principal, "dev", sql)
    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)
    return run


def _assert_fixed(run: QueryRun, table: str | None, output: str) -> None:
    """Assert that a run failed with the fixed message and shows no stored value anywhere.

    Parameters
    ----------
    run
        The finished run.
    table
        The dotted table the message names, or `None` for the variant without one.
    output
        Everything captured from the process's output streams and its log.
    """
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == policy_read_failure(table)
    assert run.error_cause == "invalid_query"
    for text in (run.error_message, output):
        assert _SECRET not in text
        assert str(_BIG) not in text


_MASKS = [
    ("secret", "strptime(\"secret\", '%Y')"),
    ("num", '"num" * 10'),
    ("secret", 'CAST("secret" AS INTEGER)'),
]


@pytest.mark.parametrize(("column", "expression"), _MASKS)
def test_a_failing_column_mask_shows_the_fixed_message(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    column: str,
    expression: str,
) -> None:
    """The failure of a mask on a stored value names the table and not the value."""
    caplog.set_level(logging.DEBUG)
    _mask(session, principal, catalog, column, expression)

    run = _query(session, principal, "SELECT * FROM gold.people")

    captured = capfd.readouterr()
    _assert_fixed(run, "gold.people", captured.out + captured.err + caplog.text)


def test_a_failing_row_filter_shows_the_fixed_message(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cast in a row filter fails on a stored value the same way."""
    caplog.set_level(logging.DEBUG)
    _filter(session, principal, catalog, "people", "CAST(code AS INTEGER) > 0")

    run = _query(session, principal, "SELECT id FROM gold.people")

    captured = capfd.readouterr()
    _assert_fixed(run, "gold.people", captured.out + captured.err + caplog.text)


def test_a_failing_bound_row_filter_shows_the_fixed_message(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M2: a filter bound to field ids, rewritten to quoted names, fails the same way."""
    caplog.set_level(logging.DEBUG)
    _filter(session, principal, catalog, "people", "code = 'x'", stored=False)
    session.execute(update(RowFilter).values(expression="CAST(code AS INTEGER) > 0"))
    session.commit()
    stored = session.scalars(select(RowFilter)).one()
    assert (stored.binding, stored.field_ids) == (FIELD_IDS, {"code": 4})

    run = _query(session, principal, "SELECT id FROM gold.people")

    captured = capfd.readouterr()
    _assert_fixed(run, "gold.people", captured.out + captured.err + caplog.text)


@pytest.mark.parametrize("expression", ["code = 5", "code = num", "code", "NOT code"])
def test_a_row_filter_stored_before_the_type_check_can_fail_on_a_stored_value(
    session: Session, principal: PrincipalRef, catalog: CatalogService, expression: str
) -> None:
    """A text column compared with a number, or used as a condition, fails on the value.

    The service refuses these now (#126); a filter made by an earlier release is not checked
    again, so the test stores the expression as the database would hold it.
    """
    _filter(session, principal, catalog, "people", expression)

    run = _query(session, principal, "SELECT id FROM gold.people")

    _assert_fixed(run, "gold.people", "")


def test_a_negation_that_overflowed_on_duckdb_1_5_now_keeps_the_row(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Characterisation: `-low > 0` on the smallest 32-bit integer.

    DuckDB 1.5 failed on the negation (``Overflow in negation``), which the run reported with
    the fixed message. DuckDB 2.0 rewrites the comparison so that nothing overflows and keeps
    the row, which is arithmetically right: the negation of -2147483648 is positive. No value
    reaches the reader that the filter would hide.
    """
    _filter(session, principal, catalog, "people", "-low > 0")

    run = _query(session, principal, "SELECT id FROM gold.people")

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.result_location is not None
    with pyarrow.ipc.open_file(tmp_path / run.result_location) as reader:
        assert reader.read_all().column("id").to_pylist() == [1]


def test_a_column_mask_stored_before_the_type_check_can_fail_on_a_stored_value(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A mask that compares a text column with a number fails on the stored value."""
    _mask(session, principal, catalog, "secret", '"secret" = "num"')

    run = _query(session, principal, "SELECT secret FROM gold.people")

    _assert_fixed(run, "gold.people", "")


def test_an_asset_run_under_a_failing_filter_records_the_fixed_message(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A write that reads a policy-bound table records the same message."""
    _filter(session, principal, catalog, "people", "code = 5")
    run = QueryService(session, AllowAllAuthorization()).submit_write(
        principal,
        "dev",
        "SELECT id FROM gold.people",
        WriteTarget(table="gold.copy", mode=WriteMode.CREATE),
    )

    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)

    _assert_fixed(run, "gold.people", "")


def test_two_tables_under_a_policy_use_the_message_without_a_table(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """With more than one policy-bound table the message cannot name the failing one."""
    _mask(session, principal, catalog, "secret", 'CAST("secret" AS INTEGER)')
    _filter(session, principal, catalog, "plain", "id > 0", stored=False)

    run = _query(
        session,
        principal,
        "SELECT p.secret FROM gold.people p JOIN gold.plain q ON p.id = q.id",
    )

    _assert_fixed(run, None, "")
    assert "on one of the tables" in (run.error_message or "")


def test_a_cast_of_the_readers_own_query_under_a_policy_shows_the_fixed_message(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The reader's cast of a value the policy hides is told the same way."""
    _filter(session, principal, catalog, "people", "id > 0", stored=False)

    run = _query(session, principal, "SELECT CAST(code AS INTEGER) FROM gold.people")

    _assert_fixed(run, "gold.people", "")


def test_a_conversion_error_on_a_table_without_a_policy_keeps_the_text_of_the_field_id_read(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The data is the reader's own: the text is the field-id read's, as without this fix."""
    _mask(session, principal, catalog, "secret", "NULL")

    run = _query(session, principal, "SELECT CAST(code AS INTEGER) FROM gold.plain")

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.error_message.startswith("Query failed: a value could not be converted")


def test_a_binder_error_under_a_policy_shows_the_fixed_message(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Errors on reads under a row filter or column mask always show the fixed message."""
    _mask(session, principal, catalog, "secret", "NULL")

    run = _query(session, principal, "SELECT nope FROM gold.people")

    _assert_fixed(run, "gold.people", "")
    assert "Binder" not in (run.error_message or "")


def test_a_catalog_error_under_a_policy_shows_the_fixed_message(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A function that does not exist is told the same way."""
    _mask(session, principal, catalog, "secret", "NULL")

    run = _query(session, principal, "SELECT no_such_function(id) FROM gold.people")

    _assert_fixed(run, "gold.people", "")


def test_a_parser_failure_on_the_text_of_a_row_filter_shows_the_fixed_message(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression for DuckDB 2.0: its parser quotes the token it fails on (`at or near "x"`).

    A row filter's text is spliced into the statement, so the token can come from the policy;
    the run shows the fixed message and the token appears nowhere.
    """
    caplog.set_level(logging.DEBUG)
    _filter(session, principal, catalog, "people", "id IS HIDDENTOKEN")

    run = _query(session, principal, "SELECT id FROM gold.people")

    captured = capfd.readouterr()
    output = captured.out + captured.err + caplog.text
    _assert_fixed(run, "gold.people", output)
    for text in (run.error_message or "", output):
        assert "HIDDENTOKEN" not in text


def test_a_conversion_failure_on_a_row_filters_literal_over_a_csv_shows_the_fixed_message(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression for DuckDB 2.0: a conversion error quotes the literal it could not convert.

    A raw CSV table is read without a field-id map, so nothing in the query process replaces
    the text; the pipeline does, because the read is under a row filter.
    """
    caplog.set_level(logging.DEBUG)
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    (tmp_path / "raw" / "events" / "part-0.csv").write_text("id\n1\n2\n")
    catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events"),
        location_pattern="raw/events/*.csv",
        format="csv",
    )
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="raw",
        table="events",
        expression="id > 0",
    )
    session.execute(
        update(RowFilter).where(RowFilter.table == "events").values(expression=f"id = '{_SECRET}'")
    )
    session.commit()

    run = _query(session, principal, "SELECT id FROM raw.events")

    captured = capfd.readouterr()
    _assert_fixed(run, "raw.events", captured.out + captured.err + caplog.text)


_ENGINE_TEXTS = [
    'Parser Error: syntax error at or near "x"\n\nLINE 1: SELECT HIDDEN-X FROM t',
    'Binder Error: Referenced column "HIDDEN-X" not found\n\nLINE 1: SELECT 1',
    "Catalog Error: Scalar Function with name HIDDEN-X does not exist!",
    "Conversion Error: Could not convert string 'HIDDEN-X' to INT32",
    "Out of Range Error: Overflow in addition of INT32 (HIDDEN-X)",
    "Invalid Input Error: HIDDEN-X",
    "Constraint Error: HIDDEN-X",
    "Not implemented Error: HIDDEN-X",
    "Invalid Error: HIDDEN-X",
    "Mismatch Type Error: HIDDEN-X",
    "Something else entirely: HIDDEN-X",
]


@pytest.mark.parametrize("engine_text", _ENGINE_TEXTS)
def test_every_class_of_engine_error_under_a_policy_gets_the_fixed_message(
    engine_text: str,
) -> None:
    """No class of the engine's text escapes the fixed message on a read under a policy."""
    raw = ValidationError(f"Query failed: {engine_text}")

    replaced = _without_the_value(raw, frozenset({"gold.people"}))
    several = _without_the_value(raw, frozenset({"gold.people", "gold.plain"}))
    untouched = _without_the_value(raw, frozenset())

    assert str(replaced) == policy_read_failure("gold.people")
    assert str(several) == policy_read_failure(None)
    assert "HIDDEN-X" not in str(replaced) + str(several)
    assert untouched is raw


def test_the_wording_for_a_file_that_stores_another_type_is_replaced_under_a_policy() -> None:
    """A reader can write this wording from their own SQL, so a policy read never shows it."""
    raw = ValidationError(
        'Query failed: a file of a table this query reads stores column "SECRET-VALUE" as a, '
        "which cannot be read as b. Ask the owner of that table to write its rows again."
    )

    replaced = _without_the_value(raw, frozenset({"gold.people"}))

    assert str(replaced) == policy_read_failure("gold.people")
    assert "SECRET-VALUE" not in str(replaced)
    assert _without_the_value(raw, frozenset()) is raw


def test_a_forged_cast_report_in_the_readers_own_query_gets_the_fixed_message(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """An `error(...)` text shaped like the read's cast report shows nothing of itself."""
    _mask(session, principal, catalog, "secret", "NULL")

    run = _query(
        session,
        principal,
        "SELECT error('failed to cast column \"SECRET-VALUE\" from type a to b: :') "
        "FROM gold.people",
    )

    _assert_fixed(run, "gold.people", "")
    assert "SECRET-VALUE" not in (run.error_message or "")


def _through_the_child(exc: BaseException) -> ValidationError:
    """Classify an engine failure in the query child and build the pipeline's error from it.

    Parameters
    ----------
    exc
        The failure DuckDB raised.

    Returns
    -------
    ValidationError
        The error the parent receives, of the type the document's kind maps to.
    """
    document = query_child.failure(exc, threading.Event(), spill=True)
    return _KINDS[document.kind](document.message)


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (duckdb.IOException("IO Error: HIDDEN-X"), TransientQueryError),
        (OSError("HTTP Error: HTTP GET error on 'HIDDEN-X' (HTTP 503)"), TransientQueryError),
        (OSError("connection reset by HIDDEN-X"), TransientQueryError),
    ],
)
def test_a_transient_or_memory_error_under_a_policy_keeps_its_class_and_shows_the_fixed_message(
    exc: BaseException, kind: type[ValidationError]
) -> None:
    """The class decides retries and memory handling; the text is the fixed message."""
    raw = _through_the_child(exc)
    assert type(raw) is kind

    replaced = _without_the_value(raw, frozenset({"gold.people"}))

    assert type(replaced) is kind
    assert str(replaced) == policy_read_failure("gold.people")
    assert failure_cause(replaced) == failure_cause(raw)
    assert _without_the_value(raw, frozenset()) is raw


def test_the_workers_own_text_for_a_killed_query_stays_under_a_policy() -> None:
    """A query process ended from outside has Lakana's wording, with no engine text in it."""
    raw = TransientQueryError(KILLED_FROM_OUTSIDE_MESSAGE.format(signal=9))

    assert _without_the_value(raw, frozenset({"gold.people"})) is raw


def test_an_internal_failure_under_a_policy_gets_a_fixed_text_and_stays_internal() -> None:
    """The engine's internal text can format a value; the kind stays, so retries are unchanged."""
    raw = DuckDBInternalError(
        f"Query failed: Information loss on cast of {_SECRET}. {INTERNAL_ADVICE}"
    )

    replaced = _without_the_value(raw, frozenset({"gold.people"}))
    untouched = _without_the_value(raw, frozenset())

    assert isinstance(replaced, DuckDBInternalError)
    assert str(replaced) == f"Query failed: the query engine failed on its own. {INTERNAL_ADVICE}"
    assert failure_cause(replaced) == failure_cause(raw)
    assert untouched is raw


def test_the_store_certificate_message_stays_under_a_policy() -> None:
    """Lakana words it, from the request's own host and bundle: no engine text to hide (#277)."""
    raw = DuckDBInternalError(
        f"{STORE_CERTIFICATE_UNVERIFIED} at store.example:8333 against the certificate "
        "authorities in /etc/ca.pem."
    )

    assert _without_the_value(raw, frozenset({"gold.people"})) is raw


def _typeof_id(session: Session, principal: PrincipalRef, tmp_path: Path) -> object:
    """Run a query that reports the type the reader gets for `gold.people.id`.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    tmp_path
        The environment's storage root.

    Returns
    -------
    object
        The type name DuckDB reports.
    """
    run = _query(session, principal, "SELECT typeof(id) AS t FROM gold.people")
    assert run.result_location is not None, run.error_message
    with pyarrow.ipc.open_file(tmp_path / run.result_location) as reader:
        return reader.read_all().column("t")[0].as_py()


def test_a_mask_created_now_is_cast_to_the_columns_type_when_read(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """`0` on the BIGINT column `id` reaches the reader as a BIGINT."""
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="people",
        column="id",
        expression="0",
    )

    assert _typeof_id(session, principal, tmp_path) == "BIGINT"


def test_a_mask_stored_before_the_type_check_reads_as_it_always_did(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """The same mask with `type_checked` false is used as written: an INTEGER."""
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="people",
        column="id",
        expression="0",
    )
    session.execute(update(ColumnMask).values(type_checked=False))
    session.commit()

    assert _typeof_id(session, principal, tmp_path) == "INTEGER"


def test_a_database_out_of_memory_error_under_a_policy_keeps_the_advice_and_no_engine_text() -> (
    None
):
    """The class stays; the text is the fixed message plus the advice, none of the engine's."""
    raw = _through_the_child(duckdb.OutOfMemoryException("Out of Memory Error: HIDDEN-X"))
    assert type(raw) is OutOfMemoryQuery

    replaced = _without_the_value(raw, frozenset({"gold.people"}))

    assert type(replaced) is OutOfMemoryQuery
    assert str(replaced) == f"{policy_read_failure('gold.people')} {MEMORY_LIMIT_ADVICE}"
    assert "HIDDEN-X" not in str(replaced)
    assert failure_cause(replaced) == failure_cause(raw)


def test_the_memory_watchdogs_own_text_stays_under_a_policy() -> None:
    """The watchdog's stop is Lakana's wording, built with no engine output."""
    failed = ChildFailed(stop=StopReason.OUT_OF_MEMORY, killed=True, detail="Query failed: over")
    raw = executor_module._translate(failed, QueryLimitsConfig(), False)
    assert isinstance(raw, OutOfMemoryQuery)

    assert _without_the_value(raw, frozenset({"gold.people"})) is raw
