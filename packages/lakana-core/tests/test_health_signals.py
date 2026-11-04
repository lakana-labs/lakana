"""The pure part of table health signals: bands, metrics, signals, effects and texts."""

# ruff: file-ignore[ambiguous-unicode-character-string]

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pyiceberg.conversions import to_bytes
from pyiceberg.manifest import DataFile
from pyiceberg.schema import Schema
from pyiceberg.types import (
    DateType,
    DecimalType,
    DoubleType,
    LongType,
    NestedField,
    StringType,
    TimestampType,
)

from lakana_core.catalog.health_signals import (
    MIN_HISTORY,
    Band,
    ColumnMetrics,
    HealthEffects,
    Observation,
    alert_text,
    apply_effects,
    column_metrics,
    columns_of,
    learn_band,
    null_rate_signal,
    range_signal,
    schema_signal,
    volume_signal,
)

_AT = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
_SCHEMA = Schema(
    NestedField(1, "id", LongType(), required=True),
    NestedField(2, "note", StringType(), required=False),
    NestedField(3, "day", DateType(), required=False),
    NestedField(4, "price", DecimalType(10, 2), required=False),
    NestedField(5, "score", DoubleType(), required=False),
    NestedField(6, "at", TimestampType(), required=False),
)


def _file(**fields: object) -> DataFile:
    """Build a data file with only the given metrics.

    Parameters
    ----------
    **fields
        `DataFile` fields, such as ``value_counts={1: 3}``.

    Returns
    -------
    DataFile
        The file.
    """
    return DataFile.from_args(record_count=fields.pop("record_count", 3), **fields)  # ty: ignore[invalid-argument-type]


def _volumes(*added: int) -> list[dict[str, int]]:
    """Build past volume observations.

    Parameters
    ----------
    *added
        The rows each past snapshot added.

    Returns
    -------
    list of dict
        Observed values as stored.
    """
    return [{"added": n, "total": n} for n in added]


def test_no_band_before_eight_observations() -> None:
    assert learn_band([100.0] * (MIN_HISTORY - 1), floor=10, share=0.1) is None


def test_the_band_is_the_median_plus_minus_four_mads() -> None:
    history = [100.0, 102, 98, 101, 99, 100, 103, 97, 100, 1_000]
    band = learn_band(history, floor=0, share=0)
    assert band == Band(low=94.0, high=106.0, median=100.0)  # MAD 1.5
    assert band is not None and band.holds(94.0) and not band.holds(106.5)


def test_a_steady_history_is_widened_by_the_floor() -> None:
    band = learn_band([10_000.0] * 30, floor=10, share=0.10)
    assert band is not None and (band.low, band.high) == (9_000.0, 11_000.0)
    small = learn_band([3.0] * 30, floor=10, share=0.10)
    assert small is not None and (small.low, small.high) == (-7.0, 13.0)


def test_volume_fires_when_the_rows_collapse() -> None:
    fired = volume_signal({"added-records": "120", "total-records": "120"}, _volumes(*[10_000] * 8))
    assert (fired.state, fired.observed) == ("fired", {"added": 120, "total": 120})
    assert fired.band == {"low": 9_000.0, "high": 11_000.0, "median": 10_000.0}
    steady = volume_signal({"added-records": "10100"}, _volumes(*[10_000] * 8))
    assert (steady.state, steady.observed) == ("ok", {"added": 10_100, "total": None})


@pytest.mark.parametrize(
    ("history", "state"),
    [
        (_volumes(*[100] * 7), "no_history"),
        (_volumes(*range(100, 1_000, 30)), "ok"),  # rising
        (_volumes(5, 500, 50, 400, 90, 300, 20, 600, 70, 450), "ok"),  # noisy
    ],
)
def test_volume_with_short_rising_and_noisy_histories(history: list, state: str) -> None:
    assert volume_signal({"added-records": "500"}, history).state == state


def test_volume_without_a_count_has_no_metrics() -> None:
    missing = volume_signal({"total-records": "9"}, _volumes(*[9] * 8))
    assert (missing.state, missing.observed) == ("no_metrics", {"reason": "not_recorded"})
    assert volume_signal(None, []).state == "no_metrics"
    assert volume_signal({"added-records": "-3"}, []).state == "no_metrics"
    assert volume_signal({"added-records": "many"}, []).state == "no_metrics"


def test_volume_history_ignores_entries_without_a_count() -> None:
    history = [{"added": 10_000}] * 8 + [{"added": None}, {}]
    assert volume_signal({"added-records": "10000"}, history).state == "ok"


def test_column_metrics_add_up_the_added_files() -> None:
    files = [
        _file(value_counts={1: 3, 2: 3}, null_value_counts={1: 0, 2: 1}),
        _file(value_counts={1: 2, 2: 2}, null_value_counts={1: 0, 2: 2}, record_count=2),
    ]
    metrics = column_metrics(files, _SCHEMA)
    assert metrics["note"] == ColumnMetrics(nulls=3, values=5, lower=None, upper=None, kind=None)


def test_a_file_without_a_count_leaves_the_column_without_metrics() -> None:
    files = [_file(value_counts={2: 3}, null_value_counts={2: 0}), _file(value_counts={})]
    assert column_metrics(files, _SCHEMA)["note"].nulls is None
    nothing = column_metrics([_file()], _SCHEMA)["note"]
    assert (nothing.nulls, nothing.values) == (None, None)


def test_column_metrics_read_dates_decimals_and_all_null_files() -> None:
    day, price = DateType(), DecimalType(10, 2)
    files = [
        _file(
            value_counts={3: 3, 4: 3},
            null_value_counts={3: 0, 4: 0},
            lower_bounds={3: to_bytes(day, 20_000), 4: to_bytes(price, Decimal("1.50"))},
            upper_bounds={3: to_bytes(day, 20_030), 4: to_bytes(price, Decimal("99.00"))},
        ),
        # Only nulls: no bounds, and it must not make the bounds unknown.
        _file(value_counts={3: 2, 4: 2}, null_value_counts={3: 2, 4: 2}, record_count=2),
    ]
    metrics = column_metrics(files, _SCHEMA)
    assert metrics["day"] == ColumnMetrics(2, 5, 20_000, 20_030, "date")
    assert metrics["price"] == ColumnMetrics(2, 5, 1.5, 99.0, "number")


def test_a_file_with_values_but_no_bounds_makes_the_bounds_unknown() -> None:
    day = DateType()
    files = [
        _file(
            value_counts={3: 3},
            null_value_counts={3: 0},
            lower_bounds={3: to_bytes(day, 1)},
            upper_bounds={3: to_bytes(day, 2)},
        ),
        _file(value_counts={3: 3}, null_value_counts={3: 0}),
    ]
    metrics = column_metrics(files, _SCHEMA)["day"]
    assert (metrics.lower, metrics.upper, metrics.kind) == (None, None, "date")


def test_only_nulls_leave_the_bounds_unknown() -> None:
    files = [_file(value_counts={3: 2}, null_value_counts={3: 2}, record_count=2)]
    metrics = column_metrics(files, _SCHEMA)["day"]
    assert (metrics.lower, metrics.upper) == (None, None)


def test_a_damaged_bound_leaves_the_bounds_unknown() -> None:
    files = [
        _file(
            value_counts={3: 3},
            null_value_counts={3: 0},
            lower_bounds={3: b"\x01"},
            upper_bounds={3: b"\x02"},
        )
    ]
    metrics = column_metrics(files, _SCHEMA)["day"]
    assert (metrics.lower, metrics.upper) == (None, None)


def test_column_metrics_read_timestamps_as_integers() -> None:
    stamp = TimestampType()
    files = [
        _file(
            value_counts={6: 3},
            null_value_counts={6: 0},
            lower_bounds={6: to_bytes(stamp, 10)},
            upper_bounds={6: to_bytes(stamp, 20)},
        )
    ]
    assert column_metrics(files, _SCHEMA)["at"] == ColumnMetrics(0, 3, 10, 20, "timestamp")


def test_only_the_first_columns_up_to_the_cap_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lakana_core.catalog.health_signals.MAX_HEALTH_COLUMNS", 2)
    assert list(column_metrics([_file(value_counts={})], _SCHEMA)) == ["id", "note"]


def test_null_rate_fires_when_a_column_fills_with_nulls() -> None:
    history = [{"nulls": 0, "values": 100, "rate": 0.0}] * 8
    fired = null_rate_signal("note", ColumnMetrics(38, 100, None, None, None), history)
    assert fired.observed is not None
    assert (fired.state, fired.observed["rate"]) == ("fired", 0.38)
    assert fired.band == {"low": -0.02, "high": 0.02, "median": 0.0}
    ok = null_rate_signal("note", ColumnMetrics(1, 100, None, None, None), history)
    assert ok.state == "ok"
    short = null_rate_signal("note", ColumnMetrics(1, 100, None, None, None), history[:3])
    assert (short.state, short.band) == ("no_history", None)


def test_null_rate_without_counts_or_rows_has_no_metrics() -> None:
    assert null_rate_signal("note", ColumnMetrics(None, 9, None, None, None), []).observed == {
        "reason": "not_recorded"
    }
    assert null_rate_signal("note", ColumnMetrics(0, 0, None, None, None), []).observed == {
        "reason": "no_rows"
    }


def test_range_fires_when_a_bound_leaves_the_learned_range() -> None:
    history = [{"lower": 0, "upper": 120, "kind": "integer"}] * 8
    fired = range_signal("qty", ColumnMetrics(0, 9, 0, 99_000, "integer"), history)
    assert fired is not None and fired.state == "fired"
    assert fired.band == {"min": 0, "max": 120, "low": -1.2, "high": 121.2}
    inside = range_signal("qty", ColumnMetrics(0, 9, 3, 121, "integer"), history)
    assert inside is not None and inside.state == "ok"
    low = range_signal("qty", ColumnMetrics(0, 9, -50, 100, "integer"), history)
    assert low is not None and low.state == "fired"


def test_a_rising_date_column_does_not_fire_every_day() -> None:
    history = [{"lower": 20_000 + d, "upper": 20_000 + d, "kind": "date"} for d in range(30)]
    today = range_signal("day", ColumnMetrics(0, 9, 20_030, 20_030, "date"), history)
    assert today is not None and today.state == "ok"


def test_a_date_range_uses_the_span_not_the_magnitude() -> None:
    # Days since 1970 are ~20,000, so 1 % of |max| would be a 200-day tolerance.
    history = [{"lower": 20_000, "upper": 20_030, "kind": "date"}] * 8
    fired = range_signal("day", ColumnMetrics(0, 9, 19_900, 20_030, "date"), history)
    assert fired is not None and fired.state == "fired"
    assert fired.band == {"min": 20_000, "max": 20_030, "low": 19_999.0, "high": 20_031.0}
    close = range_signal("day", ColumnMetrics(0, 9, 19_999, 20_030, "date"), history)
    assert close is not None and close.state == "ok"


def test_range_does_not_apply_to_strings_and_needs_bounds() -> None:
    assert range_signal("note", ColumnMetrics(0, 3, None, None, None), []) is None
    unknown = range_signal("qty", ColumnMetrics(0, 3, None, 5, "integer"), [])
    assert unknown is not None and unknown.state == "no_metrics"


def test_range_with_a_short_history_has_no_history() -> None:
    history = [{"lower": 0, "upper": 5, "kind": "integer"}] * 3 + [{"lower": None}, {}]
    short = range_signal("qty", ColumnMetrics(0, 3, 1, 5, "integer"), history)
    assert short is not None and (short.state, short.band) == ("no_history", None)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_a_bound_that_is_not_finite_counts_as_no_bound(bad: float) -> None:
    for metrics in (ColumnMetrics(0, 3, bad, 5, "number"), ColumnMetrics(0, 3, 1, bad, "number")):
        unknown = range_signal("score", metrics, [])
        assert unknown is not None and unknown.state == "no_metrics"
    history = [{"lower": 0, "upper": 5, "kind": "number"}] * 7 + [{"lower": bad, "upper": bad}]
    short = range_signal("score", ColumnMetrics(0, 3, 1, 5, "number"), history)
    assert short is not None and short.state == "no_history"


def test_schema_fires_on_added_dropped_and_retyped_columns() -> None:
    before = [["id", "long"], ["price", "double"], ["qty", "int"]]
    after = [["id", "long"], ["qty", "long"], ["discount", "double"]]
    fired = schema_signal(before, after)
    assert fired.state == "fired"
    assert fired.observed == {
        "columns": after,
        "added": [["discount", "double"]],
        "dropped": ["price"],
        "retyped": [["qty", "int", "long"]],
    }
    assert schema_signal(None, after).state == "ok"
    assert schema_signal(after, after).state == "ok"
    assert columns_of(_SCHEMA)[3] == ["price", "decimal(10, 2)"]


def test_effects_default_to_warn_and_read_columns() -> None:
    effects = HealthEffects.from_flat({"volume": "block", "range": "off", "null_rate.note": "off"})
    assert effects.of("schema") == "warn"
    assert (effects.of("volume"), effects.of("range", "price")) == ("block", "off")
    assert (effects.of("null_rate", "note"), effects.of("null_rate", "day")) == ("off", "warn")
    assert HealthEffects.from_flat({"volume": "loud", 3: "off", "x.y.z": "off"}).flat() == {}
    assert HealthEffects.from_flat({"volume.x": "off", "range.": "off"}).flat() == {}
    assert HealthEffects.from_flat("nonsense") == HealthEffects.default()
    assert effects.flat() == {"null_rate.note": "off", "range": "off", "volume": "block"}


def test_apply_effects_records_off_once_per_signal_or_column() -> None:
    observations = [
        Observation("volume", None, "fired", {"added": 1, "total": 1}, None),
        Observation("range", "price", "ok", {"lower": 1, "upper": 2, "kind": "number"}, None),
        Observation("range", "day", "ok", {"lower": 1, "upper": 2, "kind": "date"}, None),
        Observation("null_rate", "note", "ok", {"nulls": 0, "values": 1, "rate": 0.0}, None),
    ]
    effects = HealthEffects.from_flat({"range": "off", "null_rate.note": "off", "volume": "block"})
    applied = [
        (o.signal, o.column, o.state, effect) for o, effect in apply_effects(observations, effects)
    ]
    assert applied == [
        ("volume", None, "fired", "block"),
        ("range", None, "off", "off"),
        ("null_rate", "note", "off", "off"),
    ]


def test_a_column_entry_still_evaluates_when_the_whole_signal_is_off() -> None:
    observations = [
        Observation("range", "price", "fired", {"lower": 1, "upper": 2, "kind": "number"}, None),
        Observation("range", "day", "ok", {"lower": 1, "upper": 2, "kind": "date"}, None),
    ]
    effects = HealthEffects.from_flat({"range": "off", "range.price": "block"})
    applied = [
        (o.signal, o.column, o.state, effect) for o, effect in apply_effects(observations, effects)
    ]
    assert applied == [
        ("range", "price", "fired", "block"),
        ("range", None, "off", "off"),
    ]


def test_the_alert_texts_name_value_band_snapshot_and_next_step() -> None:
    volume = alert_text(
        "gold.fact_sales",
        "volume",
        None,
        "fired",
        {"added": 120, "total": 120},
        {"low": 9_800.0, "high": 10_400.0, "median": 10_100.0},
        snapshot_id=4711,
        observed_at=_AT,
    )
    assert volume == (
        "gold.fact_sales: volume 120 rows, usually 9,800–10,400 (snapshot 4711, 03 Nov 06:00 UTC); "
        "check the source extract, or set health: {volume: off} if this table now loads less"
    )
    more = alert_text(
        "gold.fact_sales",
        "volume",
        None,
        "fired",
        {"added": 99_000, "total": 99_000},
        {"low": 9_800.0, "high": 10_400.0, "median": 10_100.0},
        snapshot_id=4711,
        observed_at=_AT,
    )
    assert more is not None and more.endswith("if this table now loads more")
    nulls = alert_text(
        "gold.fact_sales",
        "null_rate",
        "price",
        "fired",
        {"nulls": 38, "values": 100, "rate": 0.38},
        {"low": -0.02, "high": 0.02, "median": 0.0},
        snapshot_id=4711,
        observed_at=_AT,
    )
    assert nulls == (
        "gold.fact_sales: null rate of price 38.0 %, usually 0.0–2.0 % (snapshot 4711, "
        "03 Nov 06:00 UTC); check what fills price upstream, or set "
        "health: {null_rate: {price: off}} if nulls are now expected"
    )
    ranged = alert_text(
        "gold.fact_sales",
        "range",
        "day",
        "fired",
        {"lower": 19_000, "upper": 20_030, "kind": "date"},
        {"min": 20_000, "max": 20_030, "low": 19_999, "high": 20_031},
        snapshot_id=4711,
        observed_at=_AT,
    )
    assert ranged == (
        "gold.fact_sales: range of day 2022-01-08–2024-11-03, usually 2024-10-04–2024-11-03 "
        "(snapshot 4711, 03 Nov 06:00 UTC); check the source for wrong units or test rows, or "
        "set health: {range: {day: off}} if the values really changed"
    )
    schema = alert_text(
        "gold.fact_sales",
        "schema",
        None,
        "fired",
        {
            "columns": [],
            "added": [["discount", "double"]],
            "dropped": ["price"],
            "retyped": [["qty", "int", "long"]],
        },
        None,
        snapshot_id=4711,
        observed_at=_AT,
    )
    assert schema == (
        "gold.fact_sales: schema changed: added discount (double), dropped price, retyped qty "
        "from int to long (snapshot 4711, 03 Nov 06:00 UTC); check that what reads "
        "gold.fact_sales still works, or set health: {schema: off} if such changes are expected"
    )
    assert (
        alert_text("t.t", "volume", None, "ok", {"added": 1}, None, snapshot_id=1, observed_at=_AT)
        is None
    )


def test_alert_texts_format_integer_number_and_timestamp_bounds() -> None:
    band = {"min": 0, "max": 120, "low": -1.2, "high": 121.2}
    integer = alert_text(
        "t.t",
        "range",
        "qty",
        "fired",
        {"lower": 0, "upper": 99_000, "kind": "integer"},
        band,
        snapshot_id=1,
        observed_at=_AT,
    )
    assert integer is not None and "range of qty 0–99,000, usually 0–120 " in integer
    number = alert_text(
        "t.t",
        "range",
        "score",
        "fired",
        {"lower": 0.5, "upper": 1234567.891, "kind": "number"},
        {"min": 0.5, "max": 2.5, "low": 0.4, "high": 2.6},
        snapshot_id=1,
        observed_at=_AT,
    )
    assert number is not None and "0.5–1.23457e+06, usually 0.5–2.5 " in number
    stamp = alert_text(
        "t.t",
        "range",
        "at",
        "fired",
        {"lower": 0, "upper": 86_400_000_000, "kind": "timestamp"},
        {"min": 0, "max": 0, "low": 0, "high": 3_600_000_000},
        snapshot_id=1,
        observed_at=_AT,
    )
    assert stamp is not None
    assert "1970-01-01 00:00 UTC–1970-01-02 00:00 UTC, usually 1970-01-01 00:00 UTC–" in stamp


def test_alert_texts_escape_names() -> None:
    text = alert_text(
        "gold.t",
        "null_rate",
        "a\nb",
        "fired",
        {"nulls": 1, "values": 1, "rate": 1.0},
        {"low": 0.0, "high": 0.02, "median": 0.0},
        snapshot_id=1,
        observed_at=_AT,
    )
    assert text is not None and "\n" not in text


def test_a_fired_row_without_its_band_has_no_text() -> None:
    assert (
        alert_text(
            "t.t", "volume", None, "fired", {"added": 1}, None, snapshot_id=1, observed_at=_AT
        )
        is None
    )


def test_a_bound_that_is_not_finite_is_not_stored() -> None:
    stamp = DoubleType()
    files = [
        _file(
            value_counts={5: 3},
            null_value_counts={5: 0},
            lower_bounds={5: to_bytes(stamp, float("-inf"))},
            upper_bounds={5: to_bytes(stamp, 1.0)},
        )
    ]
    metrics = column_metrics(files, _SCHEMA)["score"]
    assert (metrics.lower, metrics.upper, metrics.kind) == (None, None, "number")


def test_an_unknown_signal_has_no_text() -> None:
    assert (
        alert_text(
            "t.t", "freshness", None, "fired", {}, {"low": 0}, snapshot_id=1, observed_at=_AT
        )
        is None
    )


def test_a_schema_text_names_only_what_changed() -> None:
    text = alert_text(
        "t.t",
        "schema",
        None,
        "fired",
        {"columns": [], "added": [["a", "int"], ["b", "long"]], "dropped": [], "retyped": []},
        None,
        snapshot_id=1,
        observed_at=_AT,
    )
    assert text is not None and "schema changed: added a (int) and b (long) (snapshot" in text
