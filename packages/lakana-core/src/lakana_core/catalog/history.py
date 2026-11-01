"""History: keep every version of a dimension's rows (SCD type 2), in one governed commit.

A history write receives the current state of every row of a dimension (the batch) and compares
it with the table's open versions, the rows whose `is_current` is true. A key the table has no
open version of opens one; a key whose values changed closes its open version (`valid_to` set,
`is_current` false) and opens a new one; a key whose values are the same is left alone; with
`close_missing` a key the batch lacks has its open version closed at the run clock. Nothing is
deleted. Each version carries `valid_from`, `valid_to`, `is_current` and a surrogate key,
`<table>_key`, a hash of the natural key and `valid_from` that stays the same across releases.

The comparison and the hash run in this process with pyarrow; `stage_history` stages the closed
and the opened rows on a `StaticTable` and folds pyiceberg's snapshots into one with merge's fold,
so `CatalogService.history` commits one snapshot through the governed compare-and-swap.
"""

from __future__ import annotations

import decimal
import functools
import hashlib
import json
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pyarrow.compute as _compute
from pyiceberg.expressions import And, EqualTo, GreaterThan, In
from pyiceberg.io import FileIO
from pyiceberg.schema import Schema
from pyiceberg.table import DataScan
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.update import TableRequirement, TableUpdate
from pyiceberg.types import BooleanType, StringType, TimestamptzType
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import RunContext
from lakana_core.catalog.append import validate_arrow_schema
from lakana_core.catalog.attempt_stamp import TASK_RUN_PROPERTY
from lakana_core.catalog.merge import (
    SDK_ADVICE,
    MergeAdvice,
    _delete_staged,
    _repeated_rows,
    _shown,
    _static,
    check_merge_batch,
    fold_snapshots,
)
from lakana_core.catalog.schema_compat import arrow_to_iceberg_schema, arrow_to_iceberg_type
from lakana_core.catalog.writes import max_rows_message
from lakana_core.errors import ValidationError
from lakana_core.models import Run, TaskRun
from lakana_core.projects.history_columns import (
    IS_CURRENT,
    VALID_FROM,
    VALID_TO,
    added_columns,
    reserved_name_message,
    surrogate_column,
    valid_from_column_message,
    valid_from_key_message,
)

pc: Any = _compute
# pyarrow ships no stubs; its compute functions are registered at import time, which ty cannot see.
# One alias for the ~30 calls here is a deliberate departure from the per-line ty-ignore
# precedent (unresolved-attribute) in `lakana/cli/load.py` and `lakana/cli/_files.py`: it reads
# better, at the price of unchecked arguments to `pc` functions.

HISTORY_KEY_PROPERTY = "lakana.history-key"
"""The table property a history table's create sets: its key columns, as a JSON list."""

UTC_MICROS = pa.timestamp("us", tz="UTC")
"""The Arrow type of `valid_from` and `valid_to`: Iceberg's `timestamptz`."""

KEY_TYPE_MESSAGE = (
    "key column `{column}` has type {type}; a history key must be a string, an integer, a "
    "decimal, a date, a timestamp, a boolean or binary"
)
"""A natural key column the surrogate key cannot encode."""

UUID_KEY_MESSAGE = (
    "key column `{column}` is a uuid, which a history key cannot be; cast it in the SELECT, for "
    "example `CAST({column} AS VARCHAR)`."
)
"""A uuid key column: pyarrow can neither group nor filter on it (the spec's tag `u` is unused)."""

RUN_WROTE_MESSAGE = (
    "This run already wrote this table's history (the current snapshot of {table} is its write), "
    "and a run opens at most one version per key. Start a new run to record the newer values."
)
"""A second history write of one run that would change the table again (decision 15)."""

_KEY_PREFIX = b"lakana-history-key-v1\x00"
"""The bytes every surrogate key's input starts with; `v1` names the encoding."""

_ROW = "__lakana_row"
"""The batch's row number in the diff's join (made unique among the key columns by `_unused`)."""

_OPEN = "__lakana_open"
"""The open version's row number in the diff's join (made unique by `_unused`)."""

_END = "__lakana_end"
"""A returning key's last `valid_to` in its join (made unique by `_unused`)."""


def shown_type(kind: pa.DataType) -> str:
    """Name an Arrow type the way the catalog names the column's type.

    Parameters
    ----------
    kind
        The type.

    Returns
    -------
    str
        The Iceberg name (``double``, ``string``, ``decimal(10, 2)``), ``list<…>``, ``map<…>`` or
        ``struct<…>`` for a nested type, or Arrow's own name for a type a table cannot store.
    """
    if pa.types.is_list(kind) or pa.types.is_large_list(kind):
        return f"list<{shown_type(kind.value_type)}>"
    if pa.types.is_map(kind):
        return f"map<{shown_type(kind.key_type)}, {shown_type(kind.item_type)}>"
    if pa.types.is_struct(kind):
        return "struct<" + ", ".join(f"{f.name}: {shown_type(f.type)}" for f in kind) + ">"
    try:
        return str(arrow_to_iceberg_type(kind, ""))
    except ValidationError:
        return str(kind)


def shown_clock(clock: datetime) -> str:
    """Write the run clock the way the messages show it.

    Parameters
    ----------
    clock
        The run clock, UTC.

    Returns
    -------
    str
        ``2026-10-31T06:00:00Z``.
    """
    return clock.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(count: int) -> str:
    """Say a count of rows, singular for one.

    Parameters
    ----------
    count
        The number of rows.

    Returns
    -------
    str
        ``1 row`` or ``3 rows``.
    """
    return f"{count} row" if count == 1 else f"{count} rows"


def _plain(column: pa.Array) -> pa.Array:
    """Drop an extension type (a uuid) to its storage, which pyarrow compares and joins.

    Parameters
    ----------
    column
        One column, contiguous.

    Returns
    -------
    pa.Array
        The column, or its storage.
    """
    return column.storage if isinstance(column, pa.ExtensionArray) else column


def _unused(name: str, taken: Collection[str]) -> str:
    """Pick a name for an internal column that no key column carries.

    Parameters
    ----------
    name
        The preferred name.
    taken
        The names of the columns it joins.

    Returns
    -------
    str
        `name`, with underscores put in front until it is not in `taken`.
    """
    while name in taken:
        name = f"_{name}"
    return name


def _decimal_text(value: decimal.Decimal, scale: int) -> bytes:
    """Write a decimal as plain ASCII digits with exactly its column's scale.

    The text is made from the unscaled integer, never from pyarrow's formatting (which switches
    to an exponent for small values). A negative scale has no fractional digits: the value is
    written as the integer it stands for (`12300` in `decimal(5, -2)`).

    Parameters
    ----------
    value
        The value, as pyarrow reads it from the column.
    scale
        The column's scale.

    Returns
    -------
    bytes
        ``-12.5000000000`` for -12.5 at scale 10; ``0.0000000001`` for 1E-10 at scale 10; ``0``
        for zero at scale 0.
    """
    sign, digits, exponent = value.as_tuple()
    unscaled = int("".join(map(str, digits))) * 10 ** max(int(exponent) + scale, 0)
    if scale <= 0:
        text = str(unscaled * 10 ** (-scale))
    else:
        whole, fraction = divmod(unscaled, 10**scale)
        text = f"{whole}.{fraction:0{scale}d}"
    return (("-" if sign and unscaled else "") + text).encode("ascii")


def _payloads(column: pa.Array, name: str) -> tuple[bytes, list[bytes]]:
    """Encode a natural key column's values as the surrogate key's parts take them.

    Parameters
    ----------
    column
        The column, contiguous, never null (checked before).
    name
        Its name, for the message.

    Returns
    -------
    tuple
        The tag byte and each row's payload.

    Raises
    ------
    ValidationError
        For a type the encoding has no tag for (`KEY_TYPE_MESSAGE`).
    """
    kind = column.type
    if kind == pa.uuid():
        return b"u", _plain(column).to_pylist()
    if pa.types.is_string(kind) or pa.types.is_large_string(kind) or pa.types.is_string_view(kind):
        return b"s", [value.encode("utf-8") for value in column.to_pylist()]
    if pa.types.is_decimal(kind):
        return b"n", [_decimal_text(value, kind.scale) for value in column.to_pylist()]
    for tag, matches in (
        (b"i", pa.types.is_integer),
        (b"d", pa.types.is_date),
        (b"b", pa.types.is_boolean),
    ):
        if matches(kind):
            # pyarrow writes integers without leading zeros, a date as YYYY-MM-DD and a boolean
            # as true or false.
            as_text = pc.cast(column, pa.string()).to_pylist()
            return tag, [value.encode("ascii") for value in as_text]
    if pa.types.is_timestamp(kind):
        # Whatever the column's unit, the payload is microseconds since the epoch (UTC; a
        # timestamp without zone is read as UTC), so one instant gives one key.
        try:
            micros = column.cast(pa.timestamp("us", tz=kind.tz)).cast(pa.int64()).to_pylist()
        except pa.ArrowInvalid:
            raise ValidationError(
                f"key column `{name}` has timestamps finer than microseconds, which a key cannot "
                f"hold; cast it in the SELECT, for example `CAST({name} AS TIMESTAMP)`."
            ) from None
        return b"t", [str(value).encode("ascii") for value in micros]
    if pa.types.is_binary(kind) or pa.types.is_large_binary(kind):
        return b"x", column.to_pylist()
    if pa.types.is_fixed_size_binary(kind):
        return b"x", column.to_pylist()
    raise ValidationError(KEY_TYPE_MESSAGE.format(column=name, type=shown_type(kind)))


def surrogate_keys(data: pa.Table, key: Sequence[str], starts: pa.Array) -> pa.Array:
    """Compute each row's surrogate key from its natural key and its `valid_from`.

    The input is `_KEY_PREFIX`, then one part per key column in `key` order, then one part for
    `valid_from`; a part is a tag byte, the payload's length as 4 bytes big-endian, and the
    payload. The key is the first 16 bytes of the input's SHA-256, as 32 lowercase hex
    characters.

    Parameters
    ----------
    data
        The rows; carries every key column, none null.
    key
        The natural key's columns.
    starts
        Each row's `valid_from`, `UTC_MICROS`.

    Returns
    -------
    pa.Array
        One string per row.

    Raises
    ------
    ValidationError
        For a key column of a type the encoding has no tag for.
    """
    parts = [_payloads(data.column(column).combine_chunks(), column) for column in key]
    micros = [str(value).encode("ascii") for value in pc.cast(starts, pa.int64()).to_pylist()]
    keys: list[str] = []
    for row, start in enumerate(micros):
        digest = hashlib.sha256(_KEY_PREFIX)
        for tag, values in parts:
            payload = values[row]
            digest.update(tag + len(payload).to_bytes(4, "big") + payload)
        digest.update(b"t" + len(start).to_bytes(4, "big") + start)
        keys.append(digest.hexdigest()[:32])
    return pa.array(keys, pa.string())


def unchanged_rows(new: pa.Table, old: pa.Table, columns: Sequence[str]) -> pa.Array:
    """Say, row by row, whether two tables of the same types hold the same values.

    NULL equals NULL and differs from any value; a float's NaN equals NaN and `-0.0` equals `0.0`;
    every other type compares by value as stored (a decimal at its scale, a string byte for
    byte).

    Parameters
    ----------
    new
        The batch's rows, cast to the table's types.
    old
        The open versions they are compared with, row for row.
    columns
        The columns to compare.

    Returns
    -------
    pa.Array
        One boolean per row, never null; true where every column is the same.
    """
    same = pa.repeat(pa.scalar(True), new.num_rows)
    for column in columns:
        ours = _plain(new.column(column).combine_chunks())
        theirs = _plain(old.column(column).combine_chunks())
        equal = pc.or_(
            pc.fill_null(pc.equal(ours, theirs), False),
            pc.and_(pc.is_null(ours), pc.is_null(theirs)),
        )
        if pa.types.is_floating(ours.type):
            both_nan = pc.fill_null(pc.and_(pc.is_nan(ours), pc.is_nan(theirs)), False)
            equal = pc.or_(equal, both_nan)
        same = pc.and_(same, equal)
    return same


def check_history_batch(
    data: pa.Table,
    key: Sequence[str],
    *,
    table: str,
    valid_from: str | None,
    close_missing: bool,
    advice: MergeAdvice = SDK_ADVICE,
) -> None:
    """Refuse a batch a history write cannot apply, before anything is read; counts, never values.

    Parameters
    ----------
    data
        The asset's result, metadata stripped, with its `valid_from:` column if it has one.
    key
        The natural key's columns.
    table
        The table's dotted name, for the messages.
    valid_from
        The `valid_from:` column, or `None` for the run clock.
    close_missing
        Whether keys the batch lacks are closed.
    advice
        How a repeated key is explained (`SQL_ADVICE` for a SQL asset's run).

    Raises
    ------
    ValidationError
        For an empty batch with `close_missing`; a column named like an added column (in any
        case); a `valid_from` column that is missing or a key column; a column a table cannot
        store; a key column that is missing, nested, null, repeated, a uuid or of a type the
        surrogate key cannot encode; or a nested column.
    """
    if close_missing and data.num_rows == 0:
        raise ValidationError(
            "The asset's result is empty, and `close_missing: true` would close every open "
            f"version of {table}; nothing was written. If the source really is empty, remove "
            "`close_missing:` for this run."
        )
    reserved = {name.casefold() for name in added_columns(table)}
    for name in data.column_names:
        if name.casefold() in reserved:
            raise ValidationError(reserved_name_message(name, table))
    if valid_from is not None:
        if valid_from in key:
            raise ValidationError(valid_from_key_message(valid_from))
        if valid_from not in data.column_names:
            raise ValidationError(valid_from_column_message(valid_from, table))
    for field in data.schema:
        if pa.types.is_nested(field.type):
            raise ValidationError(
                f"Column `{field.name}` has type {shown_type(field.type)}; a history write "
                "compares every column and cannot compare this type. Turn it into a string in "
                f"the SELECT, for example with `to_json({field.name})`."
            )
    stored = data.drop_columns([valid_from]) if valid_from is not None else data
    arrow_to_iceberg_schema(stored.schema)
    for column in key:
        if column in data.column_names and data.schema.field(column).type == pa.uuid():
            raise ValidationError(UUID_KEY_MESSAGE.format(column=column))
    check_merge_batch(
        data,
        key,
        table_columns=data.column_names,
        table_name=table,
        advice=advice,
        composite_cap=False,
    )
    for column in key:
        _payloads(data.column(column).combine_chunks().slice(0, 0), column)


def version_starts(data: pa.Table, valid_from: str | None, clock: datetime) -> pa.Array:
    """Compute when each row's new version would start.

    Parameters
    ----------
    data
        The batch, with its `valid_from:` column if it has one.
    valid_from
        That column, or `None` for the run clock.
    clock
        The run clock.

    Returns
    -------
    pa.Array
        One `UTC_MICROS` value per row: the column's value (a date at midnight UTC, a timestamp
        without zone read as UTC), or the clock.

    Raises
    ------
    ValidationError
        For a column that is neither a date nor a timestamp, or holds a timestamp finer than
        microseconds.
    """
    if valid_from is None:
        return pa.repeat(pa.scalar(clock, UTC_MICROS), data.num_rows)
    column = data.column(valid_from).combine_chunks()
    if not (pa.types.is_date(column.type) or pa.types.is_timestamp(column.type)):
        raise ValidationError(
            f"valid_from column `{valid_from}` has type {shown_type(column.type)}; it must be a "
            "date or a timestamp"
        )
    try:
        return pc.cast(column, UTC_MICROS)
    except pa.ArrowInvalid:
        raise ValidationError(
            f"valid_from column `{valid_from}` has timestamps finer than microseconds, which a "
            f"version cannot start at; cast it in the SELECT, for example "
            f"`CAST({valid_from} AS TIMESTAMP)`."
        ) from None


def first_versions(data: pa.Table, *, key: Sequence[str], table: str, starts: pa.Array) -> pa.Table:
    """Turn a batch into a new history table's rows: every row an open version.

    Parameters
    ----------
    data
        The batch without its `valid_from:` column.
    key
        The natural key's columns.
    table
        The table's dotted name.
    starts
        Each row's `valid_from`.

    Returns
    -------
    pa.Table
        The batch's columns, then the surrogate key (required), `valid_from` (required),
        `valid_to` (NULL) and `is_current` (required, true).
    """
    count = data.num_rows
    added = pa.schema(
        [
            pa.field(surrogate_column(table), pa.string(), nullable=False),
            pa.field(VALID_FROM, UTC_MICROS, nullable=False),
            pa.field(VALID_TO, UTC_MICROS),
            pa.field(IS_CURRENT, pa.bool_(), nullable=False),
        ]
    )
    columns = [
        surrogate_keys(data, key, starts),
        starts,
        pa.nulls(count, UTC_MICROS),
        pa.repeat(pa.scalar(True), count),
    ]
    schema = pa.schema([*data.schema, *added])
    return pa.Table.from_arrays([*data.columns, *columns], schema=schema)


def run_clock(session: Session, context: RunContext | None) -> datetime:
    """Read the time a history write opens and closes versions at.

    Parameters
    ----------
    session
        Database session; only reads.
    context
        The acting principal's run context (set by authentication or the worker, never from a
        request), or `None`.

    Returns
    -------
    datetime
        The `started_at` of the run the context's task run belongs to: one clock for every task
        and attempt of a run. The server's UTC time when there is no context, or the run has no
        start recorded.
    """
    if context is not None:
        started = session.execute(
            select(Run.started_at)
            .join(TaskRun, TaskRun.run_id == Run.id)
            .where(TaskRun.id == context.task_run_id)
        ).scalar_one_or_none()
        if started is not None:
            return started.astimezone(UTC)
    return datetime.now(UTC)


@dataclass(frozen=True)
class HistoryPlan:
    """What a history write changes, as rows ready to stage.

    Parameters
    ----------
    closed
        The open versions it closes, with `valid_to` set and `is_current` false, in the table's
        Arrow schema.
    opened
        The versions it opens, with their surrogate keys, in the table's Arrow schema.
    closed_missing
        How many of `closed` are keys the batch no longer returns.
    """

    closed: pa.Table
    opened: pa.Table
    closed_missing: int

    @property
    def versions_opened(self) -> int:
        """Count the versions opened.

        Returns
        -------
        int
            The rows of `opened`.
        """
        return self.opened.num_rows

    @property
    def versions_closed(self) -> int:
        """Count the versions closed.

        Returns
        -------
        int
            The rows of `closed`.
        """
        return self.closed.num_rows

    @property
    def changed(self) -> bool:
        """Say whether the write changes anything.

        Returns
        -------
        bool
            False when it opens and closes nothing: nothing is committed then.
        """
        return bool(self.versions_opened or self.versions_closed)


def _numbered(data: pa.Table, key: Sequence[str], name: str) -> pa.Table:
    """Keep a table's key columns and number its rows, for a join.

    Parameters
    ----------
    data
        The table.
    key
        Its key columns.
    name
        The row number column's name.

    Returns
    -------
    pa.Table
        The key columns (extension types as their storage) and the row numbers.
    """
    columns = {column: _plain(data.column(column).combine_chunks()) for column in key}
    return pa.table({**columns, name: pa.array(range(data.num_rows), pa.int64())})


def _count(mask: pa.Array) -> int:
    """Count the true values of a boolean array.

    Parameters
    ----------
    mask
        The array; null counts as false.

    Returns
    -------
    int
        The count.
    """
    return int(pc.sum(pc.cast(pc.fill_null(mask, False), pa.int64())).as_py() or 0)


def check_starts(starts: pa.Array, clock: datetime, valid_from: str | None) -> None:
    """Refuse new versions without a start or with a start after the run clock.

    Parameters
    ----------
    starts
        The `valid_from` of every version the write would open.
    clock
        The run clock.
    valid_from
        The `valid_from:` column; nothing to check for the run clock.

    Raises
    ------
    ValidationError
        For a null start, or one later than the clock.
    """
    if valid_from is None:
        return
    if starts.null_count:
        raise ValidationError(
            f"valid_from column `{valid_from}` is null in {_rows(starts.null_count)}; every "
            "version needs a start"
        )
    future = _count(pc.greater(starts, pa.scalar(clock, UTC_MICROS)))
    if future:
        raise ValidationError(
            f"valid_from column `{valid_from}` is later than the run's start time "
            f"({shown_clock(clock)}) in {_rows(future)}; a version cannot start in the future."
        )


def _check_returning(
    data: pa.Table,
    new_rows: pa.Array,
    starts: pa.Array,
    key: Sequence[str],
    valid_from: str | None,
    last_ends: Callable[[pa.Table, datetime], pa.Table],
    *,
    table: str,
    clock: datetime,
) -> None:
    """Refuse a key that comes back with a start before its last version ended.

    Parameters
    ----------
    data
        The batch.
    new_rows
        The batch rows whose key has no open version.
    starts
        Every batch row's new `valid_from`.
    key
        The natural key's columns.
    valid_from
        The `valid_from:` column, or `None` when every start is the run clock; for the message.
    last_ends
        Reads the key columns and `valid_to` of the closed versions of the keys it is given (first
        argument) whose `valid_to` is after the lower bound it is given (second argument; an end
        at or before it cannot conflict, so the read may leave those out). The bound is the
        earliest start among the rows that open a key again: the run clock, or the smallest
        declared `valid_from`.
    table
        The table's dotted name, for the message.
    clock
        The run clock, for the message.

    Raises
    ------
    ValidationError
        If a returning key's start is before the latest `valid_to` of its closed versions. With
        the run clock as the start, that means a newer run closed the key.
    """
    wanted = data.select(list(key)).take(new_rows)
    earliest = pc.min(starts.take(new_rows)).as_py()
    ends = last_ends(wanted, earliest)
    if ends.num_rows == 0:
        return
    latest = ends.group_by(list(key)).aggregate([(VALID_TO, "max")])
    row_name = _unused(_ROW, key)
    end_name = _unused(_END, [*key, row_name])
    met = _numbered(wanted, key, row_name).join(
        pa.table(
            {
                **{column: _plain(latest.column(column).combine_chunks()) for column in key},
                end_name: latest.column(f"{VALID_TO}_max"),
            }
        ),
        keys=list(key),
        join_type="inner",
    )
    returning = starts.take(new_rows).take(met.column(row_name).combine_chunks())
    overlap = _count(pc.less(returning, met.column(end_name).combine_chunks()))
    if overlap and valid_from is None:
        opens = "row that opens" if overlap == 1 else "rows that open"
        raise ValidationError(
            f"{overlap} {opens} a key again would start at this run's start time "
            f"({shown_clock(clock)}), before the end of the key's last version; a newer run "
            f"already changed {table}. Start a new run."
        )
    if overlap:
        opens = "row that opens" if overlap == 1 else "rows that open"
        raise ValidationError(
            f"valid_from column `{valid_from}` is before the end of the key's last version in "
            f"{overlap} {opens} a key again; versions of one key must not overlap."
        )


def plan_history(
    data: pa.Table,
    open_versions: pa.Table,
    *,
    key: Sequence[str],
    table: str,
    schema: pa.Schema,
    starts: pa.Array,
    clock: datetime,
    valid_from: str | None,
    close_missing: bool,
    last_ends: Callable[[pa.Table, datetime], pa.Table],
) -> HistoryPlan:
    """Diff a batch against the open versions and build the rows to close and to open.

    A batch row whose key has no open version opens one; one whose values (every column but
    the key) differ from its open version closes it at the new start and opens a new version;
    one whose values are the same is dropped before its start is looked at. With
    `close_missing`, an open version whose key the batch lacks is closed at `clock`.

    Parameters
    ----------
    data
        The batch without its `valid_from:` column, cast to the table's types.
    open_versions
        The table's open versions, every column, in `schema`.
    key
        The natural key's columns.
    table
        The table's dotted name.
    schema
        The table's Arrow schema.
    starts
        Each batch row's new `valid_from` (`version_starts`).
    clock
        The run clock.
    valid_from
        The `valid_from:` column, or `None` for the run clock.
    close_missing
        Whether keys the batch lacks are closed.
    last_ends
        Reads the closed versions of keys that come back, as `_check_returning` documents
        (the keys, and the earliest start among them as a lower bound for `valid_to`); called
        only for batch keys without an open version.

    Returns
    -------
    HistoryPlan
        The rows to close and to open.

    Raises
    ------
    ValidationError
        For an opened version without a start or starting after `clock` (with `valid_from:`),
        a changed key whose new start is at or before its open version's, an open version that
        `clock` would close or replace although it starts at or after it, and a returning key
        that would start before its last version ended (with the run clock too: a newer run
        closed it), and a key with more than one open version.
    """
    values = [column for column in data.column_names if column not in key]
    row_name = _unused(_ROW, key)
    open_name = _unused(_OPEN, [*key, row_name])
    numbered_open = _numbered(open_versions, key, open_name)
    repeated = numbered_open.group_by(list(key)).aggregate([(open_name, "count")])
    twice = _count(pc.greater(repeated.column(f"{open_name}_count"), 1))
    if twice:
        keys = "key of" if twice == 1 else "keys of"
        have = "has" if twice == 1 else "have"
        raise ValidationError(
            f"{twice} {keys} {table} {have} more than one open version; there must be one per "
            "key, so nothing was written. Close all but the newest version of each such key, "
            "then run again."
        )
    joined = _numbered(data, key, row_name).join(
        numbered_open,
        keys=list(key),
        join_type="full outer" if close_missing else "left outer",
    )
    rows = joined.column(row_name).combine_chunks()
    opens = joined.column(open_name).combine_chunks()
    both = pc.and_(pc.is_valid(rows), pc.is_valid(opens))
    new_rows = pc.filter(rows, pc.and_(pc.is_valid(rows), pc.is_null(opens)))
    pair_rows, pair_opens = pc.filter(rows, both), pc.filter(opens, both)
    same = unchanged_rows(data.take(pair_rows), open_versions.take(pair_opens), values)
    changed_rows, changed_opens = (
        pc.filter(pair_rows, pc.invert(same)),
        pc.filter(pair_opens, pc.invert(same)),
    )
    missing = pc.filter(opens, pc.is_null(rows)) if close_missing else pa.array([], pa.int64())
    opening = pa.concat_arrays([new_rows, changed_rows])
    check_starts(starts.take(opening), clock, valid_from)
    old_starts = open_versions.column(VALID_FROM).combine_chunks()
    at_clock = pa.scalar(clock, UTC_MICROS)
    if valid_from is not None:
        early = _count(pc.less_equal(starts.take(changed_rows), old_starts.take(changed_opens)))
        if early:
            raise ValidationError(
                f"valid_from column `{valid_from}` is at or before the open version's valid_from "
                f"in {_rows(early)} whose values changed; a new version must start after the one "
                "it closes. Fix the times in the source, or leave `valid_from:` out to use the "
                "run's start time."
            )
        replaced = missing
    else:
        replaced = pa.concat_arrays([changed_opens, missing])
    late = _count(pc.greater_equal(old_starts.take(replaced), at_clock))
    if late:
        raise ValidationError(
            f"{late} open versions of {table} start at or after this run's start time "
            f"({shown_clock(clock)}); a run that started earlier cannot replace or close them. "
            "Start a new run."
        )
    if len(new_rows):
        _check_returning(
            data, new_rows, starts, key, valid_from, last_ends, table=table, clock=clock
        )
    opened_starts = starts.take(opening)
    opened = first_versions(data.take(opening), key=key, table=table, starts=opened_starts)
    closing = pa.concat_arrays([changed_opens, missing])
    ends = pa.concat_arrays(
        [starts.take(changed_rows), pa.repeat(pa.scalar(clock, UTC_MICROS), len(missing))]
    )
    closed = open_versions.take(closing)
    closed = closed.set_column(closed.schema.get_field_index(VALID_TO), VALID_TO, ends)
    closed = closed.set_column(
        closed.schema.get_field_index(IS_CURRENT),
        IS_CURRENT,
        pa.repeat(pa.scalar(False), len(closing)),
    )
    return HistoryPlan(
        closed=closed.select(schema.names).cast(schema),
        opened=opened.select(schema.names).cast(schema),
        closed_missing=len(missing),
    )


def not_history_message(table: str, environment: str) -> str:
    """Refuse a history write into a table its first history run did not create.

    Parameters
    ----------
    table
        The table's dotted name.
    environment
        The environment, for the command.

    Returns
    -------
    str
        The message, naming the command that drops the table.
    """
    return (
        f"{table} exists without the columns `write: history` adds; a history table is created "
        f"by its first history run. Drop it (`lakana table delete {table} --env {environment}`) "
        "so that the next run creates it with every row as an open version, or build the history "
        "into a new asset."
    )


def key_changed_message(table: str, stored: Sequence[str], declared: Sequence[str]) -> str:
    """Refuse a history write whose key is not the key the table was built with.

    Parameters
    ----------
    table
        The table's dotted name.
    stored
        The key in the table's `lakana.history-key` property.
    declared
        The key the asset declares now.

    Returns
    -------
    str
        The message.
    """
    return (
        f"{table} keeps its history by {_shown(stored)}, and the asset now declares `key: "
        f"[{', '.join(declared)}]`; a history table keeps its key. Declare the old key again, or "
        "build the new key into a new asset (its history starts with its first run)."
    )


_ADDED_TYPES = {
    VALID_FROM: (TimestamptzType, True),
    VALID_TO: (TimestamptzType, False),
    IS_CURRENT: (BooleanType, True),
}
"""The type and requiredness of the added columns besides the surrogate key (a required string)."""


def check_history_table(
    schema: Schema,
    properties: Mapping[str, str],
    *,
    key: Sequence[str],
    table: str,
    environment: str,
) -> None:
    """Refuse a history write into a table that is not a history table of this key.

    Parameters
    ----------
    schema
        The table's current schema.
    properties
        The table's properties.
    key
        The key the asset declares.
    table
        The table's dotted name.
    environment
        The environment, for the command in the message.

    Raises
    ------
    ValidationError
        If an added column is missing or has another type or requiredness, the key property is
        missing or unreadable (`not_history_message`), or it names another key.
    """
    fields = {field.name: field for field in schema.fields}
    wanted = {surrogate_column(table): (StringType, True), **_ADDED_TYPES}
    for name, (kind, required) in wanted.items():
        field = fields.get(name)
        if field is None or not isinstance(field.field_type, kind) or field.required != required:
            raise ValidationError(not_history_message(table, environment))
    try:
        stored = json.loads(properties.get(HISTORY_KEY_PROPERTY, ""))
    except ValueError:
        stored = None
    if not isinstance(stored, list) or not stored or not all(isinstance(c, str) for c in stored):
        raise ValidationError(not_history_message(table, environment))
    if stored != list(key):
        raise ValidationError(key_changed_message(table, stored, key))


def conform_batch(schema: Schema, data: pa.Table, *, table: str) -> pa.Table:
    """Check a batch against a history table's columns and cast it to their types.

    Parameters
    ----------
    schema
        The table's current schema.
    data
        The batch without its `valid_from:` column.
    table
        The table's dotted name.

    Returns
    -------
    pa.Table
        The batch in the table's column order and Arrow types (the added columns left out).

    Raises
    ------
    ValidationError
        If the batch adds a column the table does not have or lacks one it has (named, with the
        added column's type), or a type does not fit (`validate_arrow_schema`'s texts).
    """
    added = set(added_columns(table))
    stored = Schema(*[field for field in schema.fields if field.name not in added])
    names = set(stored.column_names)
    extra = [name for name in data.column_names if name not in names]
    gone = [name for name in stored.column_names if name not in data.column_names]
    if extra or gone:
        changes = [f"added {n} ({shown_type(data.schema.field(n).type)})" for n in extra]
        changes += [f"removed {n}" for n in gone]
        raise ValidationError(
            f"The result does not match {table}'s columns ({', '.join(changes)}); a history "
            f"table keeps its columns. Make the SELECT return the columns and types {table} "
            "has; to add a column, build a new history asset (its history starts with its first "
            "run)."
        )
    validate_arrow_schema(stored, data)
    target = stored.as_arrow()
    return data.select(target.names).cast(target)


def _read_capped(scan: DataScan, max_rows: int | None, message: Callable[[int], str]) -> pa.Table:
    """Read a scan batch by batch, refusing as soon as it passes a row limit.

    Parameters
    ----------
    scan
        The scan.
    max_rows
        Most rows to read, or `None`.
    message
        Builds the refusal from `max_rows`.

    Returns
    -------
    pa.Table
        The rows.

    Raises
    ------
    ValidationError
        For more rows than `max_rows`, with `message`'s text, before the rest is read.
    """
    reader = scan.to_arrow_batch_reader()
    batches: list[pa.RecordBatch] = []
    rows = 0
    for batch in reader:
        rows += batch.num_rows
        if max_rows is not None and rows > max_rows:
            raise ValidationError(message(max_rows))
        batches.append(batch)
    return pa.Table.from_batches(batches, schema=reader.schema)


def read_open_versions(
    metadata: TableMetadata,
    file_io: FileIO,
    *,
    key: Sequence[str],
    table: str,
    max_rows: int | None,
) -> pa.Table:
    """Read a history table's open versions, every column.

    One scan filtered on `is_current`: a data file whose rows are all closed has `false` as
    both bounds of the column and is skipped, so a long history costs nothing here.

    Parameters
    ----------
    metadata
        The table's current metadata.
    file_io
        The environment's `FileIO`.
    key
        The natural key's columns.
    table
        The table's dotted name, for the messages.
    max_rows
        Most open versions to read (`QueryLimitsConfig.max_write_rows`), or `None`.

    Returns
    -------
    pa.Table
        The open versions, in the table's Arrow schema.

    Raises
    ------
    ValidationError
        For more open versions than `max_rows` (`max_rows_message`, refused while reading), or a
        key or a surrogate key with more than one open version.
    """
    schema = metadata.schema().as_arrow()
    if metadata.current_snapshot_id is None:
        return schema.empty_table()
    scan = DataScan(
        table_metadata=metadata, io=file_io, row_filter=EqualTo(term=IS_CURRENT, value=True)
    )
    found = _read_capped(scan, max_rows, max_rows_message).cast(schema)
    repeated = max(_repeated_rows(found, key), _repeated_rows(found, [surrogate_column(table)]))
    if repeated:
        raise ValidationError(
            f"key {_shown(key)} has more than one open version in {repeated} rows of {table}; a "
            "history table has one open version per key. Drop it and let the next run create it "
            "again."
        )
    return found


def read_last_ends(
    metadata: TableMetadata,
    file_io: FileIO,
    wanted: pa.Table,
    after: datetime,
    *,
    key: Sequence[str],
    table: str,
    max_rows: int | None,
) -> pa.Table:
    """Read the key columns and `valid_to` of the closed versions of some keys that end late.

    Only closed versions that end after `after` are read: an earlier or equal end cannot
    overlap a version starting at or after `after`, so a long history of a returning key costs
    no more than its recent versions (files whose `valid_to` bounds are all at or before it are
    skipped).

    Parameters
    ----------
    metadata
        The table's current metadata.
    file_io
        The environment's `FileIO`.
    wanted
        The keys, one column per key column.
    after
        The earliest start among the rows that open these keys again (`plan_history`'s lower
        bound): the run clock, or the smallest declared `valid_from`.
    key
        The natural key's columns.
    table
        The table's dotted name, for the message.
    max_rows
        Most closed versions to read (`QueryLimitsConfig.max_write_rows`), or `None`.

    Returns
    -------
    pa.Table
        The closed versions ending after `after` whose every key column is among the wanted
        values: a superset of the exact keys for a key of several columns, which
        `plan_history` joins exactly.

    Raises
    ------
    ValidationError
        For more such closed versions than `max_rows`, refused while reading.
    """
    columns = (*key, VALID_TO)
    if metadata.current_snapshot_id is None or wanted.num_rows == 0:
        return metadata.schema().select(*columns).as_arrow().empty_table()
    terms = [
        In(term=column, literals=list(dict.fromkeys(wanted.column(column).to_pylist())))
        for column in key
    ]
    closed = And(EqualTo(term=IS_CURRENT, value=False), GreaterThan(term=VALID_TO, value=after))
    scan = DataScan(
        table_metadata=metadata,
        io=file_io,
        row_filter=functools.reduce(And, terms, closed),
        selected_fields=columns,
    )
    return _read_capped(
        scan,
        max_rows,
        lambda cap: (
            f"The keys this run opens again have more than {cap} closed versions in {table} "
            f"that end after {shown_clock(after)}, more than one write reads (max_write_rows); "
            "nothing was committed. Start the returning versions later (a later `valid_from`), "
            "or ask an operator to raise max_write_rows."
        ),
    )


def written_by_run(metadata: TableMetadata, context: RunContext | None) -> bool:
    """Tell whether the table's current snapshot is a write of the acting principal's run.

    The catalog stamps every snapshot a claim-form run context commits with its task run
    (`attempt_stamp.stamp_snapshots`); no client can set the stamp.

    Parameters
    ----------
    metadata
        The table's current metadata.
    context
        The acting principal's run context, or `None` (no stamp to find).

    Returns
    -------
    bool
        Whether the current snapshot carries `context`'s task run as `lakana.task-run-id`.
    """
    if context is None or metadata.current_snapshot_id is None:
        return False
    snapshot = metadata.snapshot_by_id(metadata.current_snapshot_id)
    summary = snapshot.summary if snapshot is not None else None
    return summary is not None and summary.get(TASK_RUN_PROPERTY) == str(context.task_run_id)


@dataclass(frozen=True)
class StagedHistory:
    """A history write staged against a table, ready for `CatalogService.commit_table`.

    Parameters
    ----------
    requirements
        What must still hold at the commit.
    updates
        One `AddSnapshotUpdate` and the move of `main` to it.
    discarded
        Files pyiceberg wrote for the snapshots that were folded into one; deleted whether or
        not the commit lands.
    """

    requirements: Sequence[TableRequirement]
    updates: Sequence[TableUpdate]
    discarded: Sequence[str] = ()


def stage_history(
    metadata: TableMetadata,
    metadata_location: str,
    file_io: FileIO,
    plan: HistoryPlan,
    *,
    table: str,
) -> StagedHistory:
    """Stage a history write as one snapshot, without a catalog behind it.

    The open versions to close are rewritten copy-on-write with an overwrite whose filter
    matches exactly them (open, and one of their surrogate keys), which adds them back closed;
    then the new versions are appended. pyiceberg stages up to three snapshots; `fold_snapshots`
    turns them into one on the table's current snapshot.

    Parameters
    ----------
    metadata
        The table's current metadata.
    metadata_location
        Its location; an identifier here.
    file_io
        The environment's `FileIO`.
    plan
        What the write changes; `plan.changed` is true.
    table
        The table's dotted name.

    Returns
    -------
    StagedHistory
        What to commit and the files to delete afterwards.
    """
    surrogate = surrogate_column(table)
    tx = _static(metadata, metadata_location, file_io).transaction()
    try:
        if plan.versions_closed:
            closing = plan.closed.column(surrogate).to_pylist()
            tx.overwrite(
                plan.closed,
                overwrite_filter=And(
                    EqualTo(term=IS_CURRENT, value=True), In(term=surrogate, literals=closing)
                ),
            )
        if plan.versions_opened:
            tx.append(plan.opened)
        requirements, updates, discarded = fold_snapshots(tx, metadata, metadata_location, file_io)
    except Exception:
        _delete_staged(tx, file_io)
        raise
    return StagedHistory(requirements=requirements, updates=updates, discarded=discarded)


@dataclass(frozen=True)
class HistoryResult:
    """What `CatalogService.history` committed.

    Parameters
    ----------
    snapshot_id
        The new current snapshot; `None` when nothing changed.
    versions_opened
        Versions opened: one per new key and per key whose values changed.
    versions_closed
        Open versions closed: one per key whose values changed and per missing key closed.
    closed_missing
        How many of `versions_closed` were keys the batch no longer returns.
    created
        Whether the write created the table.
    table_id
        The table's id; `None` only where a caller built the result itself.
    """

    snapshot_id: int | None
    versions_opened: int
    versions_closed: int
    closed_missing: int
    created: bool = False
    table_id: uuid.UUID | None = None
