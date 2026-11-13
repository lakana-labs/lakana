"""A preview, run by the worker: as the reader, only the object asked for, its masks recorded.

Real catalog, real files, real grants, row filters and column masks, and the query runs in a
query child.
"""

# ruff: file-ignore[redefined-while-unused]  (each test that takes the imported `admin` fixture)

from __future__ import annotations

import json
import uuid
from pathlib import Path

from policy_read_helpers import (
    ORDERS,
    admin,  # ruff: ignore[unused-import]
    catalog,
    create_view,
    drop_and_create,
    drop_and_create_view,
    events,
    filter_eu,
    grant_read,
    mask,
    orders,
    query_run,
    reader,
    reads_of,
    rename_column,
    run,
    run_preview,
    write,
)
from sqlalchemy import update
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.catalog.preview import PREVIEW_TABLE_CHANGED, submit_preview
from lakana_core.catalog.query_service import QueryService, WriteTarget
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEventType,
    ColumnMask,
    QueryErrorCause,
    QueryRunStatus,
    Table,
    WriteMode,
)
from lakana_core.policy.field_binding import UNBOUND
from lakana_core.policy.read_errors import policy_read_failure
from lakana_worker.pipeline import ReadOutcome, run_one

_VIEW = TableRef.from_dotted("dev", "gold.v")
_EU_ORDERS = TableRef.from_dotted("dev", "gold.sales.eu.orders")
_SALES_ORDERS = TableRef.from_dotted("dev", "gold.sales.orders")


def _person(session: Session, admin: PrincipalRef, name: str, *tables: str) -> PrincipalRef:
    """Create a person who may run queries in `dev` and read the named tables of `gold`.

    Parameters
    ----------
    session
        Database session.
    admin
        The granting principal.
    name
        The person's name.
    *tables
        The tables of `gold` to grant `READ_TABLE` on.

    Returns
    -------
    PrincipalRef
        The person.
    """
    person = reader(session, admin, name, *tables)
    GrantService(session, AllowAllAuthorization()).grant(
        admin,
        principal=name,
        action=Action.SUBMIT_QUERY,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    return person


def _two_sales_tables(session: Session, admin: PrincipalRef) -> PrincipalRef:
    """Write `gold.sales.eu.orders` (ids 1 and 2) and `gold.sales.orders` (id 9).

    The second is the table a name of four parts read when the namespace's middle was lost.

    Parameters
    ----------
    session
        Database session.
    admin
        The writer.

    Returns
    -------
    PrincipalRef
        A person who may run queries and read both tables, so that a read of the wrong one
        shows its rows instead of failing.
    """
    for parts in (["gold", "sales"], ["gold", "sales", "eu"]):
        catalog(session).create_namespace(admin, "dev", NamespacePath.from_parts(parts))
    write(session, admin, orders([1, 2], ["eu", "eu"]), WriteMode.CREATE, ref=_EU_ORDERS)
    write(session, admin, orders([9], ["us"]), WriteMode.CREATE, ref=_SALES_ORDERS)
    alice = _person(session, admin, "alice")
    for ref in (_EU_ORDERS, _SALES_ORDERS):
        grant_read(session, admin, "alice", ref.name, namespace=ref.namespace.joined())
    return alice


def _orders_id(session: Session) -> uuid.UUID:
    """Read the id of `gold.orders`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        The table's id.
    """
    table_id = resolve_table_id(session, "dev", "gold", "orders")
    assert table_id is not None
    return table_id


def test_a_preview_reads_as_the_caller_with_masks_and_filters(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """The reader's row filter and column mask apply; the rows they let through are shown."""
    write(session, admin, orders(list(range(150)), ["eu", "us"] * 75), WriteMode.CREATE)
    alice = _person(session, admin, "alice", "orders")
    filter_eu(session, admin, "alice")
    mask(session, admin, "alice", "region")

    finished, rows = run_preview(session, tmp_path, alice)

    assert finished.status is QueryRunStatus.SUCCEEDED, finished.error_message
    assert len(rows) == 75
    assert {row["region"] for row in rows} == {"***"}


def test_a_preview_without_a_filter_stops_at_100_rows(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """The server-built limit holds in the worker's read."""
    write(session, admin, orders(list(range(150)), ["eu"] * 150), WriteMode.CREATE)
    alice = _person(session, admin, "alice", "orders")

    assert len(run_preview(session, tmp_path, alice)[1]) == 100


def test_masked_columns_name_only_the_columns_masked_for_the_reader(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """Another person's mask on the same table is not named."""
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE)
    alice = _person(session, admin, "alice", "orders")
    reader(session, admin, "bob", "orders")
    mask(session, admin, "alice", "region")
    mask(session, admin, "bob", "email")

    finished, _ = run_preview(session, tmp_path, alice)

    assert finished.masked_columns == [{"table": "gold.orders", "column": "region"}]


def test_masked_columns_are_empty_without_a_mask(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """A stored read with no mask records `[]`, which says "none", not "unknown"."""
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE)
    alice = _person(session, admin, "alice", "orders")
    filter_eu(session, admin, "alice")

    finished, _ = run_preview(session, tmp_path, alice)

    assert finished.masked_columns == []


def test_masked_columns_carry_no_expression_or_policy_id(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """Only the table and the column are recorded."""
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE)
    alice = _person(session, admin, "alice", "orders")
    mask_id = mask(session, admin, "alice", "region")

    finished, _ = run_preview(session, tmp_path, alice)

    assert finished.masked_columns is not None
    assert [set(item) for item in finished.masked_columns] == [{"table", "column"}]
    recorded = json.dumps(finished.masked_columns)
    assert str(mask_id) not in recorded
    assert "***" not in recorded


def test_masked_columns_use_the_current_name_after_a_rename(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """The mask follows its field; the column is named as the read names it now."""
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE)
    alice = _person(session, admin, "alice", "orders")
    mask(session, admin, "alice", "region")
    rename_column(session, admin, "region", "area")

    finished, rows = run_preview(session, tmp_path, alice)

    assert finished.masked_columns == [{"table": "gold.orders", "column": "area"}]
    assert [row["area"] for row in rows] == ["***"]


def test_a_preview_of_a_view_names_the_base_table_and_column(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """Behind a view, the mask is named by the base table it applied on."""
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE)
    create_view(session, admin, "gold.v", "SELECT * FROM gold.orders")
    alice = _person(session, admin, "alice", "orders", "v")
    mask(session, admin, "alice", "region")

    finished, rows = run_preview(session, tmp_path, alice, "gold.v")

    assert finished.masked_columns == [{"table": "gold.orders", "column": "region"}]
    assert [row["region"] for row in rows] == ["***"]


def test_a_preview_error_on_a_masked_table_carries_no_value(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """A mask that fails on a value fails the preview with the fixed text, never the value.

    The mask service refuses a cast, so the mask is created and its stored text then replaced,
    as an edit in the database can do, as a mask of an earlier release (not bound to field
    ids).
    """
    write(session, admin, orders([1], ["secret-region"]), WriteMode.CREATE)
    alice = _person(session, admin, "alice", "orders")
    mask_id = mask(session, admin, "alice", "region")
    session.execute(
        update(ColumnMask)
        .where(ColumnMask.id == mask_id)
        .values(
            expression='CAST("region" AS INTEGER)',
            binding=UNBOUND,
            field_ids=None,
            column_field_id=None,
        )
    )
    session.commit()

    finished, rows = run_preview(session, tmp_path, alice)

    assert (finished.status, rows) == (QueryRunStatus.FAILED, [])
    assert finished.error_message is not None
    assert "secret-region" not in finished.error_message
    assert finished.error_message == policy_read_failure("gold.orders")
    assert finished.masked_columns is None


def test_a_preview_is_audited_and_counted_as_a_read(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """`QUERY_COMPLETED` says it was a preview, and the read counts like any other."""
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE)
    alice = _person(session, admin, "alice", "orders")
    table_id = _orders_id(session)

    finished, _ = run_preview(session, tmp_path, alice)

    [detail] = events(session, finished.id, AuditEventType.QUERY_COMPLETED)
    assert (detail["preview"], detail["status"]) == (True, "SUCCEEDED")
    assert reads_of(session, alice.id, table_id) == 1
    table = session.get(Table, table_id)
    assert table is not None
    assert table.last_read_at is not None


def test_a_preview_of_a_recreated_table_is_refused(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """A table dropped and created again under the name is another table: nothing is read.

    The reader may read the new table too, so only the id check refuses it.
    """
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE)
    alice = _person(session, admin, "alice", "orders")
    queued = submit_preview(session, GrantAuthorization(session), alice, ORDERS)
    drop_and_create(session, admin, "gold.orders")
    grant_read(session, admin, "alice", "orders")

    finished, rows = run(session, tmp_path, queued.id)

    assert (finished.status, finished.error_message, finished.error_cause) == (
        QueryRunStatus.FAILED,
        PREVIEW_TABLE_CHANGED,
        QueryErrorCause.NOT_FOUND.value,
    )
    assert rows == []
    assert reads_of(session, alice.id, _orders_id(session)) == 0


def test_a_preview_of_a_recreated_view_is_refused(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """A view dropped and created again under the name is another view: nothing is read."""
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE)
    create_view(session, admin, "gold.v", "SELECT * FROM gold.orders")
    alice = _person(session, admin, "alice", "orders", "v")
    queued = submit_preview(session, GrantAuthorization(session), alice, _VIEW)
    drop_and_create_view(session, admin, "gold.v", "SELECT * FROM gold.orders")
    grant_read(session, admin, "alice", "v")

    finished, rows = run(session, tmp_path, queued.id)

    assert (finished.status, finished.error_message, finished.error_cause) == (
        QueryRunStatus.FAILED,
        PREVIEW_TABLE_CHANGED,
        QueryErrorCause.NOT_FOUND.value,
    )
    assert rows == []


def test_a_preview_of_a_dropped_table_is_refused(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """A table dropped after the preview was queued leaves nothing to read: refused as changed."""
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE)
    alice = _person(session, admin, "alice", "orders")
    queued = submit_preview(session, GrantAuthorization(session), alice, ORDERS)
    catalog(session).drop_table(admin, ORDERS)

    finished, rows = run(session, tmp_path, queued.id)

    assert (finished.status, finished.error_message, finished.error_cause) == (
        QueryRunStatus.FAILED,
        PREVIEW_TABLE_CHANGED,
        QueryErrorCause.NOT_FOUND.value,
    )
    assert rows == []
    assert finished.masked_columns is None


def test_a_preview_of_a_table_recreated_unreadable_to_the_reader_is_refused(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """A table created again under the name, with no grant for the reader, is refused as changed.

    The answer is the same as for a table the reader may read, so it says nothing about the
    reader's grants on the new table.
    """
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE)
    alice = _person(session, admin, "alice", "orders")
    queued = submit_preview(session, GrantAuthorization(session), alice, ORDERS)
    drop_and_create(session, admin, "gold.orders")

    finished, rows = run(session, tmp_path, queued.id)

    assert (finished.status, finished.error_message, finished.error_cause) == (
        QueryRunStatus.FAILED,
        PREVIEW_TABLE_CHANGED,
        QueryErrorCause.NOT_FOUND.value,
    )
    assert rows == []
    assert finished.masked_columns is None
    assert reads_of(session, alice.id, _orders_id(session)) == 0


def test_a_preview_of_a_table_replaced_by_a_view_is_refused(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """A view created under the name of a dropped table is another object: nothing is read.

    The reader may read the view and the table under it, so only the id check refuses it.
    """
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE)
    write(
        session,
        admin,
        orders([2], ["us"]),
        WriteMode.CREATE,
        ref=TableRef.from_dotted("dev", "gold.other"),
    )
    alice = _person(session, admin, "alice", "orders")
    queued = submit_preview(session, GrantAuthorization(session), alice, ORDERS)
    catalog(session).drop_table(admin, ORDERS)
    create_view(session, admin, "gold.orders", "SELECT * FROM gold.other")
    grant_read(session, admin, "alice", "orders")
    grant_read(session, admin, "alice", "other")

    finished, rows = run(session, tmp_path, queued.id)

    assert (finished.status, finished.error_message, finished.error_cause) == (
        QueryRunStatus.FAILED,
        PREVIEW_TABLE_CHANGED,
        QueryErrorCause.NOT_FOUND.value,
    )
    assert rows == []
    assert finished.masked_columns is None


def test_a_preview_of_a_quoted_name_reads_that_table(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """A keyword namespace part and a mixed-case name read exactly that table.

    An Iceberg table lives under `bronze`, `silver` or `gold`, so the keyword is the second
    part: `gold.select.Mixed`.
    """
    namespace = NamespacePath.from_parts(["gold", "select"])
    catalog(session).create_namespace(admin, "dev", namespace)
    write(
        session,
        admin,
        orders([7], ["eu"]),
        WriteMode.CREATE,
        ref=TableRef("dev", namespace, "Mixed"),
    )
    alice = _person(session, admin, "alice")
    grant_read(session, admin, "alice", "Mixed", namespace=namespace.joined())

    finished, rows = run_preview(session, tmp_path, alice, "gold.select.Mixed")

    assert finished.status is QueryRunStatus.SUCCEEDED, finished.error_message
    assert [row["id"] for row in rows] == [7]


def test_a_preview_in_a_three_level_namespace_reads_that_table(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """`gold.sales.eu.orders` is read, never `gold.sales.orders`."""
    alice = _two_sales_tables(session, admin)

    finished, rows = run_preview(session, tmp_path, alice, "gold.sales.eu.orders")

    assert finished.status is QueryRunStatus.SUCCEEDED, finished.error_message
    assert [row["id"] for row in rows] == [1, 2]


def test_an_ordinary_query_of_a_three_level_namespace_reads_that_table(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """The fix is in the query path, not only in the preview's."""
    alice = _two_sales_tables(session, admin)
    queued = QueryService(session, GrantAuthorization(session)).submit_query(
        alice, "dev", "SELECT id FROM gold.sales.eu.orders"
    )

    finished, rows = run(session, tmp_path, queued.id)

    assert finished.status is QueryRunStatus.SUCCEEDED, finished.error_message
    assert [row["id"] for row in rows] == [1, 2]


def test_a_write_or_check_count_records_no_masked_columns(
    session: Session, tmp_path: Path, admin: PrincipalRef
) -> None:
    """Only a stored read result records the masks; a write and a count leave `None`."""
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE)
    mask(session, admin, "admin", "region")
    queries = QueryService(session, AllowAllAuthorization())
    write_id = queries.submit_write(
        admin,
        "dev",
        "SELECT * FROM gold.orders",
        WriteTarget(table="gold.copy", mode=WriteMode.CREATE),
    ).id

    run_one(session, write_id, AllowAllAuthorization())

    written = query_run(session, write_id)
    assert (written.status, written.masked_columns) == (QueryRunStatus.SUCCEEDED, None)
    count_id = queries.submit_query(admin, "dev", "SELECT count(*) AS n FROM gold.orders").id
    counted: list[ReadOutcome] = []

    run_one(session, count_id, AllowAllAuthorization(), on_result=counted.append)

    assert [outcome.restricted for outcome in counted] == [frozenset({"gold.orders"})]
    assert query_run(session, count_id).masked_columns is None
