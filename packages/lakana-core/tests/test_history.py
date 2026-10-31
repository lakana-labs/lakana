"""History's pure parts: the surrogate key, change detection, the batch rules and the diff."""

from __future__ import annotations

import decimal
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime

import pyarrow as pa
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from lakana_core.auth import RunContext
from lakana_core.catalog.history import (
    IS_CURRENT,
    UTC_MICROS,
    VALID_FROM,
    VALID_TO,
    HistoryPlan,
    added_columns,
    check_history_batch,
    first_versions,
    plan_history,
    run_clock,
    shown_type,
    surrogate_column,
    surrogate_keys,
    unchanged_rows,
    version_starts,
)
from lakana_core.catalog.merge import SQL_ADVICE, check_merge_batch
from lakana_core.errors import ValidationError
from lakana_core.models import Run, RunKind, RunStatus, TaskRun, TaskStatus

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 2, 1, tzinfo=UTC)
T2 = datetime(2026, 3, 1, tzinfo=UTC)
TABLE = "gold.dim_customer"

_SCHEMA = pa.schema(
    [
        pa.field("customer_id", pa.string()),
        pa.field("region", pa.string()),
        pa.field("score", pa.float64()),
        pa.field("dim_customer_key", pa.string(), nullable=False),
        pa.field(VALID_FROM, UTC_MICROS, nullable=False),
        pa.field(VALID_TO, UTC_MICROS),
        pa.field(IS_CURRENT, pa.bool_(), nullable=False),
    ]
)
"""A history table's Arrow schema: two data columns besides the key, then the four added."""

Row = tuple[str, str | None, float | None]


def _batch(rows: Sequence[Row]) -> pa.Table:
    """Build an asset's result: `customer_id`, `region`, `score`.

    Parameters
    ----------
    rows
        ``(customer_id, region, score)`` tuples.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table(
        {
            "customer_id": pa.array([r[0] for r in rows], pa.string()),
            "region": pa.array([r[1] for r in rows], pa.string()),
            "score": pa.array([r[2] for r in rows], pa.float64()),
        }
    )


def _open(rows: Sequence[Row], start: datetime = T0) -> pa.Table:
    """Build the open versions of a history table.

    Parameters
    ----------
    rows
        ``(customer_id, region, score)`` tuples; the surrogate key is the id in lower case.
    start
        Every version's `valid_from`.

    Returns
    -------
    pa.Table
        The rows, in `_SCHEMA`.
    """
    count = len(rows)
    return pa.table(
        {
            **{name: _batch(rows).column(name) for name in ("customer_id", "region", "score")},
            "dim_customer_key": pa.array([r[0].lower() for r in rows], pa.string()),
            VALID_FROM: pa.array([start] * count, UTC_MICROS),
            VALID_TO: pa.nulls(count, UTC_MICROS),
            IS_CURRENT: pa.array([True] * count, pa.bool_()),
        },
        schema=_SCHEMA,
    )


def _no_ends(wanted: pa.Table) -> pa.Table:
    """Find no closed version for any key.

    Parameters
    ----------
    wanted
        The keys asked for.

    Returns
    -------
    pa.Table
        No rows.
    """
    return pa.table({"customer_id": pa.array([], pa.string()), VALID_TO: pa.array([], UTC_MICROS)})


def _plan(
    batch: pa.Table,
    open_versions: pa.Table,
    *,
    clock: datetime = T1,
    starts: pa.Array | None = None,
    valid_from: str | None = None,
    close_missing: bool = False,
    last_ends: object = _no_ends,
) -> HistoryPlan:
    """Diff a batch against open versions with the defaults of a run without `valid_from:`.

    Parameters
    ----------
    batch
        The asset's result, without its `valid_from:` column.
    open_versions
        The table's open versions.
    clock
        The run clock.
    starts
        Each row's new `valid_from`; the clock for every row by default.
    valid_from
        The `valid_from:` column's name, or `None`.
    close_missing
        Whether missing keys are closed.
    last_ends
        Finds the closed versions of keys that come back.

    Returns
    -------
    HistoryPlan
        The plan.
    """
    return plan_history(
        batch,
        open_versions,
        key=["customer_id"],
        table=TABLE,
        schema=_SCHEMA,
        starts=starts if starts is not None else pa.array([clock] * batch.num_rows, UTC_MICROS),
        clock=clock,
        valid_from=valid_from,
        close_missing=close_missing,
        last_ends=last_ends,  # ty: ignore[invalid-argument-type]
    )


def _micros(value: int) -> pa.Array:
    """Make one `valid_from` from microseconds since the epoch.

    Parameters
    ----------
    value
        The microseconds.

    Returns
    -------
    pa.Array
        One UTC timestamp.
    """
    return pa.array([value], pa.int64()).cast(UTC_MICROS)


def test_the_added_columns_follow_the_last_part_of_the_name() -> None:
    assert surrogate_column("gold.dim_customer") == "dim_customer_key"
    assert surrogate_column("gold.crm.dim_customer") == "dim_customer_key"
    assert added_columns(TABLE) == ("dim_customer_key", VALID_FROM, VALID_TO, IS_CURRENT)


def test_the_surrogate_key_matches_the_specs_vectors() -> None:
    one = pa.table({"customer_id": ["C-001"]})
    assert surrogate_keys(one, ["customer_id"], _micros(1793404800000000)).to_pylist() == [
        "7e18e0a16407926388ca6000694864ce"
    ]
    two = pa.table({"id": pa.array([42, 42], pa.int32()), "country": ["DE", "DE"]})
    starts = pa.array([1793428200123456, 1793404800000000], pa.int64()).cast(UTC_MICROS)
    assert surrogate_keys(two, ["id", "country"], starts).to_pylist() == [
        "47db62ab4aac79789ff09d32bb80c4d5",
        "5be9d9efdd38001ca57fd5358c7f578f",
    ]


def test_every_key_type_has_its_tag_and_widening_an_integer_keeps_the_key() -> None:
    start = _micros(1793404800000000)
    narrow = surrogate_keys(pa.table({"k": pa.array([7], pa.int16())}), ["k"], start)
    wide = surrogate_keys(pa.table({"k": pa.array([7], pa.int64())}), ["k"], start)
    text_seven = surrogate_keys(pa.table({"k": ["7"]}), ["k"], start)
    assert narrow == wide
    assert narrow != text_seven
    columns = {
        "s": pa.array(["Ada "], pa.large_string()),
        "n": pa.array([decimal.Decimal("12.5")], pa.decimal128(10, 2)),
        "d": pa.array([date(2026, 10, 31)], pa.date32()),
        "t": pa.array([datetime(2026, 10, 31, 6, 30)], pa.timestamp("us")),
        "b": pa.array([True]),
        "x": pa.array([b"\x00\x01"], pa.binary()),
        "u": pa.ExtensionArray.from_storage(
            pa.uuid(), pa.array([uuid.UUID(int=1).bytes], pa.binary(16))
        ),
    }
    keys = {
        name: surrogate_keys(pa.table({name: c}), [name], start)[0].as_py()
        for name, c in columns.items()
    }
    assert len(set(keys.values())) == len(keys)
    assert all(len(value) == 32 and value == value.lower() for value in keys.values())
    # A timestamp without zone is read as UTC: the same instant with a zone gives the same key.
    aware = pa.array([datetime(2026, 10, 31, 6, 30, tzinfo=UTC)], UTC_MICROS)
    assert keys["t"] == surrogate_keys(pa.table({"t": aware}), ["t"], start)[0].as_py()


@pytest.mark.parametrize(
    ("column", "message"),
    [
        (
            pa.array([1.5]),
            "key column `k` has type double; a history key must be a string, an "
            "integer, a decimal, a date, a timestamp, a boolean, binary or a uuid",
        ),
        (pa.array([1.5], pa.float32()), "key column `k` has type float;"),
    ],
)
def test_a_key_of_another_type_is_refused(column: pa.Array, message: str) -> None:
    with pytest.raises(ValidationError, match=message.replace("(", r"\(")):
        check_history_batch(
            pa.table({"k": column, "v": ["a"]}),
            ["k"],
            table=TABLE,
            valid_from=None,
            close_missing=False,
        )


def test_change_detection_reads_values_as_the_table_stores_them() -> None:
    new = pa.table(
        {
            "s": pa.array([None, "x", "Ada ", None, "a"]),
            "f": pa.array([float("nan"), -0.0, 1.0, 1.0, 1.0]),
            "n": pa.array([decimal.Decimal("12.50")] * 5, pa.decimal128(10, 2)),
        }
    )
    old = pa.table(
        {
            "s": pa.array([None, None, "Ada", "", "a"]),
            "f": pa.array([float("nan"), 0.0, 1.0, 1.0, 2.0]),
            "n": pa.array([decimal.Decimal("12.5")] * 5, pa.decimal128(10, 2)),
        }
    )
    assert unchanged_rows(new, old, ["s", "f", "n"]).to_pylist() == [
        True,
        False,
        False,
        False,
        False,
    ]


def test_a_uuid_column_is_compared_by_its_bytes() -> None:
    storage = pa.array([uuid.UUID(int=1).bytes, uuid.UUID(int=2).bytes], pa.binary(16))
    other = pa.array([uuid.UUID(int=1).bytes, uuid.UUID(int=3).bytes], pa.binary(16))
    new = pa.table({"u": pa.ExtensionArray.from_storage(pa.uuid(), storage)})
    old = pa.table({"u": pa.ExtensionArray.from_storage(pa.uuid(), other)})
    assert unchanged_rows(new, old, ["u"]).to_pylist() == [True, False]


def test_a_run_opens_closes_and_leaves_alone() -> None:
    plan = _plan(
        _batch([("A", "south", 1.0), ("B", "north", 2.0), ("D", "north", None)]),
        _open([("A", "north", 1.0), ("B", "north", 2.0), ("C", "north", 3.0)]),
    )
    assert plan.opened.column("customer_id").to_pylist() == ["D", "A"]
    assert plan.opened.column(VALID_FROM).to_pylist() == [T1, T1]
    assert plan.opened.column(VALID_TO).to_pylist() == [None, None]
    assert plan.opened.column(IS_CURRENT).to_pylist() == [True, True]
    assert plan.closed.column("customer_id").to_pylist() == ["A"]
    assert plan.closed.column(VALID_TO).to_pylist() == [T1]
    assert plan.closed.column(IS_CURRENT).to_pylist() == [False]
    assert (plan.versions_opened, plan.versions_closed, plan.closed_missing) == (2, 1, 0)
    assert plan.opened.schema == _SCHEMA
    assert plan.closed.schema == _SCHEMA


def test_close_missing_closes_at_the_clock_and_deletes_nothing() -> None:
    plan = _plan(
        _batch([("A", "north", 1.0)]),
        _open([("A", "north", 1.0), ("C", "north", 3.0)]),
        close_missing=True,
    )
    assert plan.opened.num_rows == 0
    assert plan.closed.column("customer_id").to_pylist() == ["C"]
    assert plan.closed.column(VALID_TO).to_pylist() == [T1]
    assert (plan.versions_opened, plan.versions_closed, plan.closed_missing) == (0, 1, 1)


def test_nothing_changed_is_a_plan_without_changes() -> None:
    plan = _plan(_batch([("A", "north", 1.0)]), _open([("A", "north", 1.0)]))
    assert not plan.changed
    assert (plan.versions_opened, plan.versions_closed) == (0, 0)


def test_only_the_valid_from_column_moving_opens_nothing_and_refuses_nothing() -> None:
    # The source restamped the row (its `valid_from` is later than the open version's, and also
    # later than the clock); its values are the same, so the rules of section 4 never see it.
    plan = _plan(
        _batch([("A", "north", 1.0)]),
        _open([("A", "north", 1.0)]),
        starts=pa.array([T2], UTC_MICROS),
        valid_from="changed_at",
    )
    assert not plan.changed


def test_a_changed_row_starting_at_or_before_its_open_version_is_refused() -> None:
    with pytest.raises(ValidationError) as info:
        _plan(
            _batch([("A", "south", 1.0), ("B", "south", 2.0)]),
            _open([("A", "north", 1.0), ("B", "north", 2.0)], start=T1),
            clock=T2,
            starts=pa.array([T1, T0], UTC_MICROS),
            valid_from="changed_at",
        )
    assert str(info.value) == (
        "valid_from column `changed_at` is at or before the open version's valid_from in 2 rows "
        "whose values changed; a new version must start after the one it closes. Fix the times "
        "in the source, or leave `valid_from:` out to use the run's start time."
    )


def test_an_older_run_cannot_replace_or_close_newer_versions() -> None:
    with pytest.raises(ValidationError) as info:
        _plan(
            _batch([("A", "south", 1.0)]),
            _open([("A", "north", 1.0), ("C", "north", 3.0)], start=T1),
            clock=T1,
            close_missing=True,
        )
    assert str(info.value) == (
        "2 open versions of gold.dim_customer start at or after this run's start time "
        "(2026-02-01T00:00:00Z); a run that started earlier cannot replace or close them. Start "
        "a new run."
    )


def test_a_returning_key_may_not_start_before_its_last_version_ended() -> None:
    def ends(wanted: pa.Table) -> pa.Table:
        assert wanted.column("customer_id").to_pylist() == ["D"]
        return pa.table({"customer_id": ["D", "D"], VALID_TO: pa.array([T0, T2], UTC_MICROS)})

    with pytest.raises(ValidationError) as info:
        _plan(
            _batch([("D", "north", 1.0)]),
            _open([("A", "north", 1.0)]),
            clock=T2,
            starts=pa.array([T1], UTC_MICROS),
            valid_from="changed_at",
            last_ends=ends,
        )
    assert str(info.value) == (
        "valid_from column `changed_at` is before the end of the key's last version in 1 row "
        "that opens a key again; versions of one key must not overlap."
    )


def test_a_returning_key_starting_at_its_last_end_opens() -> None:
    def ends(wanted: pa.Table) -> pa.Table:
        return pa.table({"customer_id": ["D"], VALID_TO: pa.array([T1], UTC_MICROS)})

    plan = _plan(
        _batch([("D", "north", 1.0)]),
        _open([]),
        clock=T2,
        starts=pa.array([T1], UTC_MICROS),
        valid_from="changed_at",
        last_ends=ends,
    )
    assert plan.opened.column(VALID_FROM).to_pylist() == [T1]


@pytest.mark.parametrize(
    ("starts", "message"),
    [
        (
            pa.array([None, T1], UTC_MICROS),
            "valid_from column `changed_at` is null in 1 row; every version needs a start",
        ),
        (
            pa.array([T2, T2], UTC_MICROS),
            "valid_from column `changed_at` is later than the run's start time "
            "(2026-02-01T00:00:00Z) in 2 rows; a version cannot start in the future.",
        ),
    ],
)
def test_an_opening_row_needs_a_start_that_is_not_in_the_future(
    starts: pa.Array, message: str
) -> None:
    with pytest.raises(ValidationError) as info:
        _plan(
            _batch([("A", "south", 1.0), ("D", "north", 1.0)]),
            _open([("A", "north", 1.0)], start=T0),
            starts=starts,
            valid_from="changed_at",
        )
    assert str(info.value) == message


def test_version_starts_reads_dates_timestamps_and_the_clock() -> None:
    clock = datetime(2026, 10, 31, 6, tzinfo=UTC)
    data = pa.table(
        {
            "d": pa.array([date(2026, 10, 31)], pa.date32()),
            "t": pa.array([datetime(2026, 10, 31, 5)], pa.timestamp("us")),
            "z": pa.array(
                [datetime(2026, 10, 31, 5, tzinfo=UTC)], pa.timestamp("us", tz="Europe/Berlin")
            ),
        }
    )
    assert version_starts(data, "d", clock).to_pylist() == [datetime(2026, 10, 31, tzinfo=UTC)]
    assert version_starts(data, "t", clock).to_pylist() == [datetime(2026, 10, 31, 5, tzinfo=UTC)]
    assert version_starts(data, "z", clock).to_pylist() == [datetime(2026, 10, 31, 5, tzinfo=UTC)]
    assert version_starts(data, None, clock).to_pylist() == [clock]
    assert version_starts(data, None, clock).type == UTC_MICROS


def test_version_starts_refuses_text_and_nanoseconds() -> None:
    with pytest.raises(ValidationError) as info:
        version_starts(pa.table({"c": ["2026-10-31"]}), "c", T1)
    assert str(info.value) == (
        "valid_from column `c` has type string; it must be a date or a timestamp"
    )
    nanos = pa.table({"c": pa.array([1793404800000000001], pa.timestamp("ns"))})
    with pytest.raises(ValidationError) as info:
        version_starts(nanos, "c", T1)
    assert str(info.value) == (
        "valid_from column `c` has timestamps finer than microseconds, which a version cannot "
        "start at; cast it in the SELECT, for example `CAST(c AS TIMESTAMP)`."
    )


def test_first_versions_open_every_row_with_the_four_columns() -> None:
    rows = first_versions(
        _batch([("A", "north", 1.0)]),
        key=["customer_id"],
        table=TABLE,
        starts=pa.array([T1], UTC_MICROS),
    )
    assert rows.column_names == [
        "customer_id",
        "region",
        "score",
        "dim_customer_key",
        VALID_FROM,
        VALID_TO,
        IS_CURRENT,
    ]
    assert [rows.schema.field(n).nullable for n in rows.column_names[3:]] == [
        False,
        False,
        True,
        False,
    ]
    assert rows.column(IS_CURRENT).to_pylist() == [True]


@pytest.mark.parametrize(
    "name", ["valid_from", "Valid_From", "IS_CURRENT", "dim_customer_key", "valid_to"]
)
def test_a_column_with_a_name_history_adds_is_refused_in_any_case(name: str) -> None:
    data = pa.table({"customer_id": ["A"], name: ["x"]})
    with pytest.raises(ValidationError) as info:
        check_history_batch(
            data, ["customer_id"], table=TABLE, valid_from=None, close_missing=False
        )
    assert str(info.value) == (
        f"Column `{name}` of `gold.dim_customer`'s SELECT has a name `write: history` adds itself "
        "(valid_from, valid_to, is_current, dim_customer_key); rename it in the SELECT, for "
        f"example `AS source_{name}`."
    )


def test_the_valid_from_column_must_be_returned_and_not_be_a_key_column() -> None:
    data = pa.table({"customer_id": ["A"], "region": ["n"]})
    with pytest.raises(ValidationError) as info:
        check_history_batch(
            data, ["customer_id"], table=TABLE, valid_from="changed_at", close_missing=False
        )
    assert str(info.value) == (
        "Column `changed_at` in `valid_from:` is not a column of `gold.dim_customer`'s SELECT; "
        "add it to the SELECT, or change `valid_from:`."
    )
    with pytest.raises(ValidationError) as info:
        check_history_batch(
            data, ["customer_id"], table=TABLE, valid_from="customer_id", close_missing=False
        )
    assert str(info.value) == (
        "`valid_from: customer_id` names a key column; name the column that says when the row's "
        "values took effect, or leave `valid_from:` out to use the run's start time"
    )


def test_a_nested_column_is_refused_with_the_cast_to_write() -> None:
    data = pa.table({"customer_id": ["A"], "tags": pa.array([["x"]], pa.list_(pa.string()))})
    with pytest.raises(ValidationError) as info:
        check_history_batch(
            data, ["customer_id"], table=TABLE, valid_from=None, close_missing=False
        )
    assert str(info.value) == (
        "Column `tags` has type list<string>; a history write compares every column and cannot "
        "compare this type. Turn it into a string in the SELECT, for example with `to_json(tags)`."
    )
    assert shown_type(pa.list_(pa.string())) == "list<string>"


def test_an_empty_result_with_close_missing_is_refused() -> None:
    with pytest.raises(ValidationError) as info:
        check_history_batch(
            _batch([]), ["customer_id"], table=TABLE, valid_from=None, close_missing=True
        )
    assert str(info.value) == (
        "The asset's result is empty, and `close_missing: true` would close every open version "
        "of gold.dim_customer; nothing was written. If the source really is empty, remove "
        "`close_missing:` for this run."
    )


def test_null_and_repeated_keys_use_the_merge_texts_without_the_composite_cap() -> None:
    with pytest.raises(ValidationError, match="key column `customer_id` is null in 1 rows"):
        check_history_batch(
            pa.table({"customer_id": pa.array(["A", None])}),
            ["customer_id"],
            table=TABLE,
            valid_from=None,
            close_missing=False,
        )
    with pytest.raises(ValidationError, match="repeats in 2 rows of the asset's result"):
        check_history_batch(
            pa.table({"customer_id": ["A", "A"]}),
            ["customer_id"],
            table=TABLE,
            valid_from=None,
            close_missing=False,
            advice=SQL_ADVICE,
        )
    many = pa.table({"a": list(range(6000)), "b": ["x"] * 6000})
    check_history_batch(many, ["a", "b"], table=TABLE, valid_from=None, close_missing=False)
    with pytest.raises(ValidationError, match="at most 5,000 rows"):
        check_merge_batch(many, ["a", "b"], table_columns=["a", "b"], table_name=TABLE)


def test_the_run_clock_is_the_runs_start_or_the_servers_time(session: Session) -> None:
    started = datetime(2026, 10, 31, 6, tzinfo=UTC)
    run_id, task_run_id = uuid.uuid4(), uuid.uuid4()
    session.execute(text("PRAGMA foreign_keys=OFF"))
    session.add(
        Run(
            id=run_id,
            environment_id=uuid.uuid4(),
            repository_id=uuid.uuid4(),
            job_version_id=uuid.uuid4(),
            kind=RunKind.FULL,
            status=RunStatus.RUNNING,
            started_at=started,
        )
    )
    session.add(
        TaskRun(
            id=task_run_id,
            run_id=run_id,
            asset=TABLE,
            status=TaskStatus.RUNNING,
            max_attempts=1,
            timeout_seconds=60,
            retry_delay_seconds=0,
        )
    )
    session.flush()
    assert run_clock(session, RunContext(task_run_id=task_run_id, claim=1)) == started
    before = datetime.now(UTC)
    assert before <= run_clock(session, None) <= datetime.now(UTC)
    assert before <= run_clock(session, RunContext(task_run_id=uuid.uuid4(), claim=1))
