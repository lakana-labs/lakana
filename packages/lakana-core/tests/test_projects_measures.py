"""The `measures` header field: its values, its rules and their fixed texts (#255)."""

from __future__ import annotations

import pydantic
import pytest
from test_projects_dag import make_asset

from lakana_core.projects.asset_model import model_issues
from lakana_core.projects.frontmatter import AssetMetadata, parse_front_matter
from lakana_core.projects.issues import ProjectValidationError
from lakana_core.projects.measures import (
    CURRENCY_CODES,
    UNITS,
    MeasureEntry,
    MeasureSpec,
    default_additivity,
    measure_entries,
    measure_issues,
    read_measure_entries,
)
from lakana_core.projects.model import project_model_issues
from lakana_core.projects.project_config import (
    FREE_TEXT_MESSAGE,
    IDENTIFIER_MESSAGE,
    pydantic_issues,
)

_T = "gold.fact_sales"
_FACT = {"kind": "fact", "grain": "One row per sale.", "key": "none"}


def _texts(measures: object) -> list[str]:
    """Validate a fact with `measures` and return the issue texts.

    Parameters
    ----------
    measures
        The raw `measures` value.

    Returns
    -------
    list of str
        One text per pydantic error, as a deploy prints it; empty when valid.
    """
    try:
        AssetMetadata.model_validate({**_FACT, "measures": measures})
    except pydantic.ValidationError as exc:
        return [issue.message for issue in pydantic_issues(exc, path="a.sql", line=1)]
    return []


def test_a_valid_block_reads_into_measure_specs() -> None:
    text = (
        "/* lakana\nkind: fact\ngrain: One row per store and day.\nkey: [date, store_id]\n"
        "measures:\n"
        "  net_amount: {aggregation: sum, currency: EUR, label: Net revenue}\n"
        "  stock_level: {aggregation: sum, additivity: semi, not_over: date, unit: count}\n"
        "*/\nSELECT 1"
    )
    measures = parse_front_matter(text, path="a.sql").metadata.measures
    assert measures["net_amount"] == MeasureSpec(
        aggregation="sum", currency="EUR", label="Net revenue"
    )
    assert measures["net_amount"].resolved_additivity == "additive"
    assert measures["stock_level"].not_over == ("date",)
    assert measures["stock_level"].resolved_additivity == "semi"


@pytest.mark.parametrize(
    ("aggregation", "expected"),
    [
        ("sum", "additive"),
        ("count", "additive"),
        ("min", "non"),
        ("max", "non"),
        ("avg", "non"),
        ("count_distinct", "non"),
        ("none", "non"),
    ],
)
def test_the_default_additivity_follows_the_aggregation(aggregation: str, expected: str) -> None:
    assert default_additivity(aggregation) == expected


@pytest.mark.parametrize(
    ("measures", "expected"),
    [
        (
            {"m": "sum"},
            "measures: `m` must be a mapping such as `{aggregation: sum, currency: EUR}`",
        ),
        (
            {"": {"aggregation": "sum", "unit": "count"}},
            "measures: a measure column must not be empty",
        ),
        (
            {"a/b": {"aggregation": "sum", "unit": "count"}},
            "measures: measure column `a/b` cannot name its id `measure_a/b`: ",
        ),
        (
            {"x" * 1100: {"aggregation": "sum", "unit": "count"}},
            f"measures: measure column `{'x' * 100}…` has 1100 bytes; a measure column may have at "
            "most 1024 bytes",
        ),
        (
            {f"c{i}": {"aggregation": "sum", "unit": "count"} for i in range(101)},
            "measures: at most 100 measures per table, and `measures:` lists 101; keep the ones "
            "people aggregate, and describe the others in `columns:`",
        ),
        (
            {"s": {"aggregation": "sum", "additivity": "semi", "not_over": ["s"], "unit": "count"}},
            "measures: measure `s` names itself in `not_over`; list the other columns it must not "
            "be summed over, such as a date",
        ),
        (
            {"m": {"aggregation": "sum", "additivity": "semi", "not_over": 3, "unit": "count"}},
            "measures.m.not_over: must be a column name or a list of column names",
        ),
        (
            {"m": {"aggregation": "sum", "additivity": "semi", "not_over": [1], "unit": "count"}},
            "measures.m.not_over: must be a column name or a list of column names",
        ),
        (
            {"m": {"aggregation": "sum", "additivity": "semi", "not_over": None, "unit": "count"}},
            "measures.m: `additivity: semi` needs `not_over:`: the columns the measure must not "
            "be summed over, such as the date of a balance",
        ),
        (
            {"a.b": {"aggregation": "sum", "unit": "count"}},
            "measures: measure column `a.b` cannot name its id `measure_a.b`: a measure id must "
            "not contain '.', '/', a NUL byte or the unit separator; rename the column in the "
            "asset",
        ),
        (
            {"m": {"unit": "count"}},
            "measures.m: needs `aggregation:`: one of sum, count, min, max, avg, count_distinct "
            "or none (none for a value that is never aggregated)",
        ),
        (
            {"m": {"aggregation": "total", "unit": "count"}},
            "measures.m.aggregation: must be one of sum, count, min, max, avg, count_distinct, "
            "none; got 'total'",
        ),
        (
            {"m": {"aggregation": "sum", "additivity": "full", "unit": "count"}},
            "measures.m.additivity: must be one of additive, semi, non; got 'full'",
        ),
        (
            {"m": {"aggregation": "avg", "additivity": "additive", "currency": "EUR"}},
            "measures.m: `aggregation: avg` does not add values up, so `additivity` must be "
            "`non`; leave `additivity:` out, or change `aggregation:`",
        ),
        (
            {"m": {"aggregation": "sum", "additivity": "non", "currency": "EUR"}},
            "measures.m: `aggregation: sum` adds values up, so `additivity` must be `additive` "
            "or `semi`; for a value that is never summed, use `aggregation: avg`, `min`, `max` "
            "or `none`",
        ),
        (
            {"m": {"aggregation": "sum", "additivity": "semi", "unit": "count"}},
            "measures.m: `additivity: semi` needs `not_over:`: the columns the measure must not "
            "be summed over, such as the date of a balance",
        ),
        (
            {"m": {"aggregation": "sum", "not_over": "date", "unit": "count"}},
            "measures.m: `not_over:` is only for `additivity: semi`; remove `not_over:`, or "
            "declare `additivity: semi`",
        ),
        (
            {
                "m": {
                    "aggregation": "sum",
                    "additivity": "semi",
                    "not_over": ["d", "d"],
                    "unit": "count",
                }
            },
            "measures.m.not_over: column `d` is listed twice",
        ),
        (
            {"m": {"aggregation": "sum", "additivity": "semi", "not_over": [""], "unit": "count"}},
            "measures.m.not_over: a column must not be empty; write the name of a column of the "
            "asset",
        ),
        (
            {"m": {"aggregation": "sum"}},
            "measures.m: needs `unit:` or `currency:`: `currency:` for money (an ISO 4217 code "
            "such as EUR), `unit:` for anything else (for a number of things, `unit: count`)",
        ),
        (
            {"m": {"aggregation": "sum", "unit": "count", "currency": "EUR"}},
            "measures.m: takes `unit:` or `currency:`, not both; money has a currency, every "
            "other value a unit",
        ),
        (
            {"m": {"aggregation": "sum", "unit": "count", "label": "  "}},
            "measures.m.label: must not be empty",
        ),
        (
            {"m": {"aggregation": "sum", "unit": "count", "label": "a\nb"}},
            "measures.m.label: must be one line",
        ),
        (
            {"m": {"aggregation": "sum", "unit": "count", "label": "x" * 101}},
            "measures.m.label: must be at most 100 characters",
        ),
        (
            {"m": {"aggregation": "sum", "unit": "count", "description": "x" * 4001}},
            "measures.m.description: must be at most 4000 characters",
        ),
        (
            {"m": {"aggregation": "sum", "unit": "count", "unitt": "x"}},
            "measures.m.unitt: Extra inputs are not permitted",
        ),
    ],
)
def test_each_field_rule_has_its_fixed_text(measures: object, expected: str) -> None:
    (text,) = _texts(measures)
    assert text.startswith(expected) if expected.endswith(": ") else text == expected


def test_a_currency_in_lower_case_and_an_unknown_unit_are_refused_with_the_fix() -> None:
    assert _texts({"m": {"aggregation": "sum", "currency": "eur"}}) == [
        "measures.m.currency: must be an ISO 4217 currency code in capitals, such as EUR, USD "
        "or CHF; got 'eur'"
    ]
    assert _texts({"m": {"aggregation": "sum", "unit": "kg"}}) == [
        "measures.m.unit: must be one of count, percent, ratio, millisecond, second, minute, "
        "hour, day, byte, gram, kilogram, tonne, metre, kilometre, litre, kilowatt_hour, "
        "degree_celsius; got 'kg'. For money use `currency:`; to have a unit added, open an issue"
    ]


def test_current_and_listed_withdrawn_currencies_are_accepted() -> None:
    for code in ("EUR", "USD", "CHF", "JPY", "XCG", "ZWG", "BGN", "DEM", "HRK"):
        assert code in CURRENCY_CODES
    assert "XTS" not in CURRENCY_CODES and "XXX" not in CURRENCY_CODES
    assert all(len(code) == 3 and code.isupper() and code.isascii() for code in CURRENCY_CODES)
    assert UNITS[0] == "count" and len(UNITS) == 17


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"kind": "dimension", "grain": "One row per store.", "key": "store_id"},
            "Asset `gold.fact_sales` is a dimension and declares `measures:`; measures belong on "
            "a fact or wide table. Move them to the fact or wide table that holds these values, "
            "or remove `measures:`.",
        ),
        (
            {"kind": "staging"},
            "Asset `gold.fact_sales` is a staging table and declares `measures:`; measures belong "
            "on a fact or wide table. Move them to the fact or wide table that holds these "
            "values, or remove `measures:`.",
        ),
        (
            {},
            "Asset `gold.fact_sales` declares `measures:` but no `kind:`; measures belong on a "
            "fact or wide table. Declare `kind: fact` or `kind: wide`, or remove `measures:`.",
        ),
        (
            {"kind": "fact", "grain": "One row per sale.", "key": "m"},
            "Measure `m` is a key column of `gold.fact_sales`; a key identifies a row and is not "
            "a value to aggregate. Remove it from `measures:`.",
        ),
    ],
)
def test_the_rules_between_fields(fields: dict[str, object], expected: str) -> None:
    metadata = AssetMetadata.model_validate(
        {**fields, "measures": {"m": {"aggregation": "sum", "unit": "count"}}}
    )
    assert model_issues(metadata, table=_T) == [expected]


def test_a_wide_table_and_a_fact_with_key_none_may_declare_measures() -> None:
    for fields in (_FACT, {"kind": "wide", "grain": "One row per order.", "key": "id"}):
        metadata = AssetMetadata.model_validate(
            {**fields, "measures": {"m": {"aggregation": "sum", "unit": "count"}}}
        )
        assert model_issues(metadata, table=_T) == []


def test_checks_measures_needs_measures_and_the_family_is_known() -> None:
    metadata = AssetMetadata.model_validate({**_FACT, "checks": {"measures": "warn"}})
    assert model_issues(metadata, table=_T) == [
        "`checks: measures` sets how the measure checks fail, but the asset declares no "
        "`measures:`; declare `measures:`, or remove `checks: measures`"
    ]
    with pytest.raises(ProjectValidationError) as exc:
        parse_front_matter("/* lakana\nchecks:\n  rows: warn\n*/\nSELECT 1", path="a.sql")
    assert exc.value.issues[0].message == "checks: takes only key, references and measures"


def test_measure_and_not_over_columns_must_be_in_an_explicit_select() -> None:
    metadata = AssetMetadata.model_validate(
        {
            **_FACT,
            "measures": {
                "amount": {"aggregation": "sum", "currency": "EUR"},
                "stock": {
                    "aggregation": "sum",
                    "additivity": "semi",
                    "not_over": "day",
                    "unit": "count",
                },
            },
        }
    )
    asset = make_asset(_T, columns=("stock",), metadata=metadata)
    assert [issue.message for issue in project_model_issues([asset])] == [
        "Measure column `amount` is not a column of `gold.fact_sales`'s SELECT; add it to the "
        "SELECT, or remove it from `measures:`.",
        "Column `day` in `not_over` of measure `stock` is not a column of `gold.fact_sales`'s "
        "SELECT; add it to the SELECT, or change `not_over:`.",
    ]
    star = make_asset(_T, columns=None, metadata=metadata)
    assert project_model_issues([star]) == []


def test_unsafe_characters_are_refused_in_every_measure_text() -> None:
    bad = "a\u202eb"
    spec = {"aggregation": "sum", "unit": "count"}
    assert _texts({bad: spec}) == [f"measures: a measure column {IDENTIFIER_MESSAGE}"]
    assert _texts(
        {"m": {**spec, "aggregation": "sum", "additivity": "semi", "not_over": [bad]}}
    ) == [f"measures.m.not_over: a column {IDENTIFIER_MESSAGE}"]
    assert _texts({"m": {**spec, "label": bad}}) == [f"measures.m.label: {FREE_TEXT_MESSAGE}"]
    assert _texts({"m": {**spec, "description": bad}}) == [
        f"measures.m.description: {FREE_TEXT_MESSAGE}"
    ]
    assert _texts({"m": {**spec, "description": " "}}) == [
        "measures.m.description: must not be empty"
    ]


def test_measures_that_is_not_a_mapping_is_refused() -> None:
    assert _texts(["m"])[0].startswith("measures: must be a mapping of keys to values")


def test_a_not_over_column_of_the_select_passes() -> None:
    metadata = AssetMetadata.model_validate(
        {
            **_FACT,
            "measures": {
                "stock": {
                    "aggregation": "sum",
                    "additivity": "semi",
                    "not_over": "day",
                    "unit": "count",
                }
            },
        }
    )
    assert project_model_issues([make_asset(_T, columns=("stock", "day"), metadata=metadata)]) == []


def test_a_null_label_and_a_padded_description_are_read() -> None:
    metadata = AssetMetadata.model_validate(
        {
            **_FACT,
            "measures": {
                "m": {
                    "aggregation": "sum",
                    "unit": "count",
                    "label": None,
                    "description": "  Items sold.  ",
                }
            },
        }
    )
    assert metadata.measures["m"].label is None
    assert metadata.measures["m"].description == "Items sold."
    assert (
        AssetMetadata.model_validate(
            {
                **_FACT,
                "measures": {"m": {"aggregation": "sum", "unit": "count", "description": None}},
            }
        )
        .measures["m"]
        .description
        is None
    )


def test_measure_entries_round_trip_with_the_resolved_additivity() -> None:
    spec = MeasureSpec(aggregation="avg", currency="EUR", label="Price")
    written = measure_entries({"price": spec})
    assert written == [
        {
            "column": "price",
            "aggregation": "avg",
            "additivity": "non",
            "not_over": [],
            "unit": None,
            "currency": "EUR",
            "label": "Price",
            "description": None,
        }
    ]
    assert read_measure_entries(written) == (
        MeasureEntry("price", "avg", "non", (), None, "EUR", "Price", None),
    )


def test_read_measure_entries_drops_what_it_cannot_store() -> None:
    good = {
        "column": "a",
        "aggregation": "sum",
        "additivity": "additive",
        "not_over": [],
        "unit": "count",
        "currency": None,
        "label": None,
        "description": None,
    }
    damaged = [
        {**good, "column": "b", "currency": "E" * 50, "unit": None},
        {**good, "column": "x" * 2000},
        {**good, "column": "c", "aggregation": "median"},
        {**good, "column": "d", "additivity": ["non"]},
        {**good, "column": "e", "not_over": "day"},
        {**good, "column": "f", "unit": "parsec"},
        {**good, "column": "g", "label": 3},
        {**good, "column": "dup"},
        {**good, "column": "dup"},
        "not a mapping",
    ]
    assert [m.column for m in read_measure_entries([good, *damaged])] == ["a"]
    assert read_measure_entries("not a list") == ()


def test_measures_are_refused_on_every_kind_but_fact_and_wide() -> None:
    """A kind added later is refused until it is allowed, as the SDK refuses it."""
    spec = {"m": MeasureSpec(aggregation="sum", unit="count")}
    assert measure_issues("fact", "none", spec, table="gold.t") == []
    assert measure_issues("wide", "none", spec, table="gold.t") == []
    assert measure_issues("bridge", "none", spec, table="gold.t") == [
        "Asset `gold.t` is a bridge table and declares `measures:`; measures belong on a fact or "
        "wide table. Move them to the fact or wide table that holds these values, or remove "
        "`measures:`."
    ]
