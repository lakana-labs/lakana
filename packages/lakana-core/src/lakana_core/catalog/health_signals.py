"""Table health signals: what one snapshot says, the learned band, and the alert texts.

Pure: nothing here reads storage or the database. `health_record` gathers the inputs when a
snapshot is committed, `health_sweep` for freshness, and both store what these functions return
in `table_health_signals`. A band is learned from the last `HISTORY` observations of the same
signal and column; no signal but `schema` fires with fewer than `MIN_HISTORY`.
"""

from __future__ import annotations

import math
import statistics
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pyiceberg.conversions import from_bytes
from pyiceberg.manifest import DataFile
from pyiceberg.schema import Schema
from pyiceberg.types import (
    DateType,
    DecimalType,
    DoubleType,
    FloatType,
    IntegerType,
    LongType,
    PrimitiveType,
    TimestampType,
    TimestamptzType,
)

from lakana_core.identifiers import shown

SIGNALS = ("freshness", "volume", "null_rate", "range", "schema")
"""Every signal, in the order they are shown."""
COMMIT_SIGNALS = ("volume", "null_rate", "range", "schema")
"""The signals a commit records; `freshness` is the sweep's."""
COLUMN_SIGNALS = frozenset({"null_rate", "range"})
"""The signals recorded per column."""
STATES = ("ok", "fired", "no_history", "no_metrics", "off")
EFFECTS = ("warn", "block", "off")
DEFAULT_EFFECT = "warn"
HISTORY = 30
"""How many past observations a band is learned from."""
MIN_HISTORY = 8
"""Fewer past observations than this give no band, so no signal but `schema` fires."""
MAD_FACTOR = 4.0
VOLUME_FLOOR_ROWS = 10.0
VOLUME_FLOOR_SHARE = 0.10
NULL_RATE_FLOOR = 0.02
RANGE_FLOOR_SHARE = 0.01
MAX_HEALTH_COLUMNS = 200
"""The most top-level columns one commit evaluates, in schema order; the rest record nothing."""

_RANGE_KINDS: tuple[tuple[tuple[type[PrimitiveType], ...], str], ...] = (
    ((IntegerType, LongType), "integer"),
    ((FloatType, DoubleType, DecimalType), "number"),
    ((DateType,), "date"),
    ((TimestampType, TimestamptzType), "timestamp"),
)
_RANGE_UNIT = {"integer": 1, "number": 0, "date": 1, "timestamp": 3_600_000_000}
"""The least tolerance of a range bound per kind: one, nothing, a day, an hour (microseconds)."""
_SPAN_ONLY = frozenset({"date", "timestamp"})
"""Kinds whose values sit far from zero, so the tolerance follows the span alone."""
_EPOCH = date(1970, 1, 1)
_MAX_COUNT = 2**63 - 1
_MAX_COUNT_DIGITS = 19
_DASH = "\N{EN DASH}"
_DAMAGED = (ValueError, TypeError, ArithmeticError, struct.error)


@dataclass(frozen=True)
class Band:
    """The usual values of a signal.

    The median plus or minus `MAD_FACTOR` times the MAD, at least the floor.

    Parameters
    ----------
    low
        The lowest usual value.
    high
        The highest usual value.
    median
        The median of the history.
    """

    low: float
    high: float
    median: float

    def holds(self, value: float) -> bool:
        """Say whether a value is usual.

        Parameters
        ----------
        value
            The value.

        Returns
        -------
        bool
            Whether it lies within the band, ends included.
        """
        return self.low <= value <= self.high

    def to_json(self) -> dict[str, float]:
        """Give the band as it is stored.

        Returns
        -------
        dict
            ``low``, ``high`` and ``median``.
        """
        return {"low": self.low, "high": self.high, "median": self.median}


def learn_band(history: Sequence[float], *, floor: float, share: float) -> Band | None:
    """Learn the band of a history.

    Parameters
    ----------
    history
        Past values, newest first; at most `HISTORY` are read.
    floor
        The least half-width.
    share
        The least half-width as a share of the median's size.

    Returns
    -------
    Band or None
        `None` with fewer than `MIN_HISTORY` values.
    """
    values = list(history)[:HISTORY]
    if len(values) < MIN_HISTORY:
        return None
    middle = statistics.median(values)
    spread = statistics.median(abs(value - middle) for value in values)
    half = max(MAD_FACTOR * spread, floor, share * abs(middle))
    return Band(low=middle - half, high=middle + half, median=middle)


@dataclass(frozen=True)
class Observation:
    """What one signal saw in one snapshot.

    Parameters
    ----------
    signal
        One of `SIGNALS`.
    column
        The column for a column signal, else `None`.
    state
        One of `STATES`.
    observed
        The values seen, as stored; `None` for an `off` row.
    band
        The learned band, as stored; `None` when there was none.
    """

    signal: str
    column: str | None
    state: str
    observed: dict[str, Any] | None
    band: dict[str, Any] | None


@dataclass(frozen=True)
class ColumnMetrics:
    """What the files a snapshot added say about one column.

    Parameters
    ----------
    nulls
        The null values, or `None` when a file did not record them.
    values
        The values (nulls included), or `None` when a file did not record them.
    lower
        The smallest value, or `None` when unknown or not a range column.
    upper
        The largest value, or `None` when unknown or not a range column.
    kind
        The range kind (``integer``, ``number``, ``date``, ``timestamp``), or `None`.
    """

    nulls: int | None
    values: int | None
    lower: float | int | None
    upper: float | int | None
    kind: str | None


def _whole(value: object) -> int | None:
    """Read a summary entry as a count.

    Parameters
    ----------
    value
        The entry; the committing client wrote it, so it is not trusted to be a number.

    Returns
    -------
    int or None
        The value as a whole number from zero to 2**63 - 1, written in plain ASCII digits;
        `None` otherwise.
    """
    text = str(value)
    if not (text.isascii() and text.isdigit()) or len(text) > _MAX_COUNT_DIGITS:
        return None
    number = int(text)
    return number if number <= _MAX_COUNT else None


def _number(value: object) -> float | int | None:
    """Accept a stored value only when it is a finite number.

    Parameters
    ----------
    value
        What a history item holds.

    Returns
    -------
    float or int or None
        The value; `None` for anything that is not a finite number.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _numbers(history: Iterable[Mapping[str, Any]], key: str) -> list[float | int]:
    """Collect one number from every history item that has it.

    Parameters
    ----------
    history
        Past observed values.
    key
        The entry to read.

    Returns
    -------
    list
        The finite numbers, in order.
    """
    return [n for item in history if (n := _number(item.get(key))) is not None]


def _mad(values: Sequence[float]) -> float:
    """Give the median absolute deviation.

    Parameters
    ----------
    values
        The values.

    Returns
    -------
    float
        The median of the distances from the median.
    """
    middle = statistics.median(values)
    return statistics.median(abs(value - middle) for value in values)


def volume_signal(
    summary: Mapping[str, Any] | None, history: Sequence[Mapping[str, Any]]
) -> Observation:
    """Compare the rows a snapshot added with the usual.

    Parameters
    ----------
    summary
        The snapshot's summary.
    history
        Past observed values of this signal, newest first.

    Returns
    -------
    Observation
        `no_metrics` without an added-records count, `no_history` without a band.
    """
    added = _whole((summary or {}).get("added-records"))
    if added is None:
        return Observation("volume", None, "no_metrics", {"reason": "not_recorded"}, None)
    band = learn_band(_numbers(history, "added"), floor=VOLUME_FLOOR_ROWS, share=VOLUME_FLOOR_SHARE)
    state = "no_history" if band is None else "ok" if band.holds(added) else "fired"
    observed = {"added": added, "total": _whole((summary or {}).get("total-records"))}
    return Observation("volume", None, state, observed, band.to_json() if band else None)


def _kind(field_type: object) -> str | None:
    """Name the range kind of a column type.

    Parameters
    ----------
    field_type
        The Iceberg type.

    Returns
    -------
    str or None
        The kind; `None` for a type with no range signal.
    """
    for types, kind in _RANGE_KINDS:
        if isinstance(field_type, types):
            return kind
    return None


def _bound(field_type: PrimitiveType, raw: bytes) -> float | int:
    """Decode one stored bound.

    Parameters
    ----------
    field_type
        The column type.
    raw
        The bound as Iceberg stores it.

    Returns
    -------
    float or int
        A decimal as a float; dates and timestamps stay integers.

    Raises
    ------
    ValueError
        When the bytes do not decode to a finite number.
    """
    value = from_bytes(field_type, raw)
    number = float(value) if isinstance(value, Decimal) else value
    if not isinstance(number, int | float) or not math.isfinite(number):
        raise ValueError("bound is not a finite number")
    return number


def _total(files: Sequence[DataFile], attribute: str, field_id: int) -> int | None:
    """Add one per-column count over the files.

    Parameters
    ----------
    files
        The data files.
    attribute
        ``value_counts`` or ``null_value_counts``.
    field_id
        The column's field id.

    Returns
    -------
    int or None
        The sum; `None` as soon as one file did not record the count.
    """
    total = 0
    for data_file in files:
        counts = getattr(data_file, attribute)
        if counts is None or field_id not in counts:
            return None
        total += counts[field_id]
    return total


def _bounds(
    files: Sequence[DataFile], field_id: int, field_type: PrimitiveType
) -> tuple[float | int | None, float | int | None]:
    """Find the smallest and largest value over the files.

    Parameters
    ----------
    files
        The data files.
    field_id
        The column's field id.
    field_type
        The column type.

    Returns
    -------
    tuple
        ``(lower, upper)``; both `None` when a file with values has no bound, a bound is
        damaged or not finite, or every value is null.
    """
    lowers: list[float | int] = []
    uppers: list[float | int] = []
    for data_file in files:
        counts, nulls = data_file.value_counts, data_file.null_value_counts
        if counts and nulls and field_id in counts and counts[field_id] == nulls.get(field_id):
            continue
        low = (data_file.lower_bounds or {}).get(field_id)
        high = (data_file.upper_bounds or {}).get(field_id)
        if low is None or high is None:
            return None, None
        try:
            lowers.append(_bound(field_type, low))
            uppers.append(_bound(field_type, high))
        except _DAMAGED:
            return None, None
    if not lowers:
        return None, None
    return min(lowers), max(uppers)


def column_metrics(files: Sequence[DataFile], schema: Schema) -> dict[str, ColumnMetrics]:
    """Read the column metrics of the files a snapshot added.

    Parameters
    ----------
    files
        The data files the snapshot added.
    schema
        The table schema.

    Returns
    -------
    dict
        Metrics by column name, for the first `MAX_HEALTH_COLUMNS` top-level columns.
    """
    result: dict[str, ColumnMetrics] = {}
    for column in schema.fields[:MAX_HEALTH_COLUMNS]:
        kind = _kind(column.field_type)
        lower, upper = (
            _bounds(files, column.field_id, column.field_type)  # ty: ignore[invalid-argument-type]
            if kind
            else (None, None)
        )
        result[column.name] = ColumnMetrics(
            nulls=_total(files, "null_value_counts", column.field_id),
            values=_total(files, "value_counts", column.field_id),
            lower=lower,
            upper=upper,
            kind=kind,
        )
    return result


def null_rate_signal(
    column: str, metrics: ColumnMetrics, history: Sequence[Mapping[str, Any]]
) -> Observation:
    """Compare a column's share of nulls with the usual.

    Parameters
    ----------
    column
        The column name.
    metrics
        The column's metrics for this snapshot.
    history
        Past observed values of this column, newest first.

    Returns
    -------
    Observation
        `no_metrics` when the counts were not recorded or the files hold no rows.
    """
    if metrics.nulls is None or metrics.values is None:
        return Observation("null_rate", column, "no_metrics", {"reason": "not_recorded"}, None)
    if metrics.values == 0:
        return Observation("null_rate", column, "no_metrics", {"reason": "no_rows"}, None)
    rate = round(metrics.nulls / metrics.values, 6)
    band = learn_band(_numbers(history, "rate"), floor=NULL_RATE_FLOOR, share=0)
    state = "no_history" if band is None else "ok" if band.holds(rate) else "fired"
    observed = {"nulls": metrics.nulls, "values": metrics.values, "rate": rate}
    return Observation("null_rate", column, state, observed, band.to_json() if band else None)


def range_signal(
    column: str, metrics: ColumnMetrics, history: Sequence[Mapping[str, Any]]
) -> Observation | None:
    """Compare a column's smallest and largest value with the learned range.

    Parameters
    ----------
    column
        The column name.
    metrics
        The column's metrics for this snapshot.
    history
        Past observed values of this column, newest first.

    Returns
    -------
    Observation or None
        `None` for a column with no range signal (a string, say). `no_metrics` when a bound is
        unknown or not finite.
    """
    if metrics.kind is None:
        return None
    lower, upper = _number(metrics.lower), _number(metrics.upper)
    if lower is None or upper is None:
        return Observation("range", column, "no_metrics", {"reason": "not_recorded"}, None)
    observed = {"lower": lower, "upper": upper, "kind": metrics.kind}
    pairs = [
        (low, high)
        for item in history
        if (low := _number(item.get("lower"))) is not None
        and (high := _number(item.get("upper"))) is not None
    ][:HISTORY]
    if len(pairs) < MIN_HISTORY:
        return Observation("range", column, "no_history", observed, None)
    lowers = [low for low, _ in pairs]
    uppers = [high for _, high in pairs]
    low_min, high_max = min(lowers), max(uppers)
    size = high_max - low_min
    if metrics.kind not in _SPAN_ONLY:
        size = max(size, abs(low_min), abs(high_max))
    unit = _RANGE_UNIT[metrics.kind]
    tol_low = max(MAD_FACTOR * _mad(lowers), RANGE_FLOOR_SHARE * size, unit)
    tol_high = max(MAD_FACTOR * _mad(uppers), RANGE_FLOOR_SHARE * size, unit)
    band = {
        "min": low_min,
        "max": high_max,
        "low": low_min - tol_low,
        "high": high_max + tol_high,
    }
    fired = lower < band["low"] or upper > band["high"]
    return Observation("range", column, "fired" if fired else "ok", observed, band)


def columns_of(schema: Schema) -> list[list[str]]:
    """List a schema's top-level columns for the schema signal.

    Parameters
    ----------
    schema
        The table schema.

    Returns
    -------
    list
        ``[name, type]`` pairs in schema order.
    """
    return [[column.name, str(column.field_type)] for column in schema.fields]


def schema_signal(
    previous: Sequence[Sequence[str]] | None, current: Sequence[Sequence[str]]
) -> Observation:
    """Compare a snapshot's columns with the previous snapshot's.

    Parameters
    ----------
    previous
        The previous snapshot's ``[name, type]`` pairs, or `None` for the first snapshot.
    current
        This snapshot's pairs.

    Returns
    -------
    Observation
        `fired` when a column was added, dropped or changed its type.
    """
    columns = [list(pair) for pair in current]
    if previous is None:
        observed = {"columns": columns, "added": [], "dropped": [], "retyped": []}
        return Observation("schema", None, "ok", observed, None)
    before = {name: kind for name, kind in previous}
    after = {name: kind for name, kind in current}
    added = [list(pair) for pair in current if pair[0] not in before]
    dropped = [name for name, _ in previous if name not in after]
    retyped = [
        [name, before[name], kind] for name, kind in current if before.get(name, kind) != kind
    ]
    observed = {"columns": columns, "added": added, "dropped": dropped, "retyped": retyped}
    state = "fired" if added or dropped or retyped else "ok"
    return Observation("schema", None, state, observed, None)


@dataclass(frozen=True)
class HealthEffects:
    """What each signal does when it fires: warn, block or nothing at all.

    The keys of `entries` are a signal (the whole table) or ``signal.column`` for one column;
    a column named in ``health:`` never contains a dot, so the key is unambiguous.

    Parameters
    ----------
    entries
        The effect by key; a signal without one warns.
    """

    entries: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def default(cls) -> HealthEffects:
        """Give the effects of a table with no ``health:`` block.

        Returns
        -------
        HealthEffects
            Every signal warns.
        """
        return cls({})

    @classmethod
    def from_flat(cls, raw: object) -> HealthEffects:
        """Read the stored flat map, dropping anything that is not valid.

        Parameters
        ----------
        raw
            The stored map; not trusted.

        Returns
        -------
        HealthEffects
            The valid entries.
        """
        if not isinstance(raw, Mapping):
            return cls.default()
        return cls({k: v for k, v in raw.items() if _valid_entry(k, v)})

    def of(self, signal: str, column: str | None = None) -> str:
        """Give the effect of a signal, for one column when it has its own entry.

        Parameters
        ----------
        signal
            One of `SIGNALS`.
        column
            The column, for a column signal.

        Returns
        -------
        str
            One of `EFFECTS`; a freshness signal never blocks, so ``block`` reads as ``warn``.
        """
        if column is not None and f"{signal}.{column}" in self.entries:
            return self.entries[f"{signal}.{column}"]
        effect = self.entries.get(signal, DEFAULT_EFFECT)
        return DEFAULT_EFFECT if signal == "freshness" and effect == "block" else effect

    def flat(self) -> dict[str, str]:
        """Give the entries as they are stored.

        Returns
        -------
        dict
            A copy sorted by key.
        """
        return dict(sorted(self.entries.items()))


def _valid_entry(key: object, value: object) -> bool:
    """Say whether a stored effect entry is one this code understands.

    Parameters
    ----------
    key
        A signal or ``signal.column``.
    value
        An effect.

    Returns
    -------
    bool
        Whether both are valid.
    """
    if not isinstance(key, str) or value not in EFFECTS:
        return False
    if key in SIGNALS:
        return True
    signal, dot, column = key.partition(".")
    return bool(dot) and signal in COLUMN_SIGNALS and bool(column) and "." not in column


def apply_effects(
    observations: Sequence[Observation], effects: HealthEffects
) -> list[tuple[Observation, str]]:
    """Pair each observation with its effect and collapse what is switched off.

    A signal that is off for the whole table records one table-level ``off`` row; a column
    switched off records its own. A column with its own entry is still evaluated when the
    whole signal is off.

    Parameters
    ----------
    observations
        What the signals saw.
    effects
        The table's effects.

    Returns
    -------
    list
        ``(observation, effect)`` pairs; an off signal or column appears as an ``off`` row.
    """
    result: list[tuple[Observation, str]] = []
    table_off: set[str] = set()
    for observation in observations:
        effect = effects.of(observation.signal, observation.column)
        if effect != "off":
            result.append((observation, effect))
            continue
        own = (
            observation.column is not None
            and f"{observation.signal}.{observation.column}" in effects.entries
        )
        if own:
            result.append(
                (Observation(observation.signal, observation.column, "off", None, None), effect)
            )
        elif observation.signal not in table_off:
            table_off.add(observation.signal)
            result.append((Observation(observation.signal, None, "off", None, None), effect))
    return result


def _count(value: float) -> str:
    """Format a row count, never below zero.

    Parameters
    ----------
    value
        The count.

    Returns
    -------
    str
        With thousands separators.
    """
    return f"{round(max(value, 0)):,}"


def _bound_text(kind: str, value: float) -> str:
    """Format a range bound by its kind.

    Parameters
    ----------
    kind
        ``integer``, ``number``, ``date`` or ``timestamp``.
    value
        The bound; days since 1970 for a date, microseconds for a timestamp.

    Returns
    -------
    str
        The text.
    """
    if kind == "integer":
        return f"{int(value):,}"
    try:
        if kind == "date":
            return (_EPOCH + timedelta(days=int(value))).isoformat()
        if kind == "timestamp":
            return datetime.fromtimestamp(value / 1e6, UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        pass
    return format(value, ",.6g")


def _percent(value: float) -> str:
    """Format a share as a percentage.

    Parameters
    ----------
    value
        The share, clipped to 0 to 1.

    Returns
    -------
    str
        One decimal.
    """
    return f"{min(max(value, 0.0), 1.0) * 100:.1f}"


def _schema_changes(observed: Mapping[str, Any]) -> str:
    """Describe what a schema signal found.

    Parameters
    ----------
    observed
        The stored observed values.

    Returns
    -------
    str
        The changes, comma-separated.
    """
    groups = (
        ("added ", [f"{shown(n)} ({shown(t)})" for n, t in observed["added"]]),
        ("dropped ", [shown(n) for n in observed["dropped"]]),
        (
            "retyped ",
            [f"{shown(n)} from {shown(a)} to {shown(b)}" for n, a, b in observed["retyped"]],
        ),
    )
    return ", ".join(label + " and ".join(items) for label, items in groups if items)


def alert_text(
    table: str,
    signal: str,
    column: str | None,
    state: str,
    observed: Mapping[str, Any] | None,
    band: Mapping[str, Any] | None,
    *,
    snapshot_id: int | None,
    observed_at: datetime,
) -> str | None:
    """Write the alert of a fired signal.

    The text names the value, the usual band, the snapshot and the next step.

    Parameters
    ----------
    table
        The table's name.
    signal
        One of `SIGNALS`.
    column
        The column, for a column signal.
    state
        One of `STATES`.
    observed
        The stored observed values.
    band
        The stored band.
    snapshot_id
        The snapshot that fired.
    observed_at
        When the observation was recorded.

    Returns
    -------
    str or None
        `None` unless the signal fired, or for a signal with no text here.
    """
    if state != "fired" or observed is None or (band is None and signal != "schema"):
        return None
    usual: Mapping[str, Any] = band or {}
    name, col = shown(table), shown(column)
    at = f"snapshot {snapshot_id}, {observed_at.astimezone(UTC):%d %b %H:%M} UTC"
    if signal == "volume":
        direction = "less" if observed["added"] < usual["low"] else "more"
        return (
            f"{name}: volume {_count(observed['added'])} rows, usually "
            f"{_count(usual['low'])}{_DASH}{_count(usual['high'])} ({at}); "
            f"check the source extract, "
            f"or set health: {{volume: off}} if this table now loads {direction}"
        )
    if signal == "null_rate":
        return (
            f"{name}: null rate of {col} {_percent(observed['rate'])} %, usually "
            f"{_percent(usual['low'])}{_DASH}{_percent(usual['high'])} % ({at}); "
            f"check what fills {col} "
            f"upstream, or set health: {{null_rate: {{{col}: off}}}} if nulls are now expected"
        )
    if signal == "range":
        kind = observed["kind"]
        return (
            f"{name}: range of {col} {_bound_text(kind, observed['lower'])}{_DASH}"
            f"{_bound_text(kind, observed['upper'])}, usually "
            f"{_bound_text(kind, usual['min'])}{_DASH}"
            f"{_bound_text(kind, usual['max'])} ({at}); check the source for wrong units or test "
            f"rows, or set health: {{range: {{{col}: off}}}} if the values really changed"
        )
    if signal == "schema":
        return (
            f"{name}: schema changed: {_schema_changes(observed)} ({at}); check that what reads "
            f"{name} still works, or set health: {{schema: off}} if such changes are expected"
        )
    return None
