"""Measures an asset header declares: their values, their rules and their stored form (#255).

A measure is metadata on a column of a fact or wide table: its default aggregation, its
additivity, its unit or currency, and a label and a description for people. Lakana computes
nothing from it. This module imports nothing from `asset_model` or `frontmatter`, which both
import it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import field_validator, model_validator

from lakana_core.identifiers import shown
from lakana_core.projects.project_config import (
    IDENTIFIER_MESSAGE,
    StrictModel,
    checked_text,
    unsafe_identifier,
)

AGGREGATIONS = ("sum", "count", "min", "max", "avg", "count_distinct", "none")
"""A measure's default aggregation; `none` for a value that is never aggregated."""

ADDITIVITIES = ("additive", "semi", "non")
"""Whether a measure sums over every dimension, over some (not `not_over`), or never."""

NUMERIC_AGGREGATIONS = ("sum", "avg")
"""The aggregations whose column must be a number; the `measures` check's pre-check reads this."""

ORDERED_AGGREGATIONS = ("min", "max")
"""The aggregations whose column must be a number, a date or a timestamp. Every other aggregation
(`count`, `count_distinct`, `none`) takes a column of any type."""

_SUMMING = frozenset({"sum", "count"})
"""The aggregations that add values up: they allow `additive` and `semi`, the others `non`."""

UNITS = (
    "count",
    "percent",
    "ratio",
    "millisecond",
    "second",
    "minute",
    "hour",
    "day",
    "byte",
    "gram",
    "kilogram",
    "tonne",
    "metre",
    "kilometre",
    "litre",
    "kilowatt_hour",
    "degree_celsius",
)
"""The units a measure may declare. The set grows here; `lakana.assets.UNITS` repeats it (a parity
test pins both) and `docs/site/reference/asset-front-matter.md` lists it."""

# jscpd:ignore-start
# `lakana.assets` repeats both lists: the SDK may not import core.
# `test_the_measure_values_are_the_same_in_core_and_the_sdk` pins them.
_CURRENT_TEXT = (
    "AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BHD BIF BMD BND BOB BOV BRL BSD BTN BWP "
    "BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN "
    "ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK "
    "JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD "
    "MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP "
    "PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP "
    "SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV "
    "WST XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU XUA YER ZAR ZMW ZWG"
)
_CURRENT_CURRENCIES = frozenset(_CURRENT_TEXT.split())
"""ISO 4217 List One as of 2026, without XTS (testing) and XXX (no currency)."""

_WITHDRAWN_TEXT = (
    "ANG ATS AZM BEF BGN BYR CUC CYP DEM EEK ESP FIM FRF GHC GRD HRK IEP ITL LTL LUF LVL MRO "
    "MTL NLG PTE ROL SIT SKK SLL STD TMM TRL VEB VEF ZMK ZWL"
)
_WITHDRAWN_CURRENCIES = frozenset(_WITHDRAWN_TEXT.split())
"""Codes withdrawn since 1999 that historic data still carries: the euro's predecessors, and
currencies replaced since (a table of 2015 sales in lev still says BGN)."""
# jscpd:ignore-end

CURRENCY_CODES = _CURRENT_CURRENCIES | _WITHDRAWN_CURRENCIES
"""The currencies a measure may declare, in capitals."""

MAX_MEASURES = 100
"""The most measures one table declares; `lakana.assets` repeats the number."""

MAX_LABEL_CHARS = 100
"""The longest `label:`, in characters."""

MAX_MEASURE_DESCRIPTION_CHARS = 4000
"""The longest `description:` of a measure: the limit of a column description."""

MAX_MEASURE_COLUMN_BYTES = 1024
"""The most UTF-8 bytes of a measure column: `table_measures.column` holds 1024 characters."""

MEASURE_PREFIX = "measure_"
"""The prefix of a measure's id: ``<ns.table>/measure_<column>``."""

MEASURES_CHECK = "measures"
"""The one check a table's `measures:` generates."""

NEEDS_AGGREGATION = (
    "needs `aggregation:`: one of sum, count, min, max, avg, count_distinct or none (none for a "
    "value that is never aggregated)"
)
NEEDS_UNIT = (
    "needs `unit:` or `currency:`: `currency:` for money (an ISO 4217 code such as EUR), "
    "`unit:` for anything else (for a number of things, `unit: count`)"
)
UNIT_AND_CURRENCY = (
    "takes `unit:` or `currency:`, not both; money has a currency, every other value a unit"
)
SEMI_NEEDS_NOT_OVER = (
    "`additivity: semi` needs `not_over:`: the columns the measure must not be summed over, "
    "such as the date of a balance"
)
NOT_OVER_NEEDS_SEMI = (
    "`not_over:` is only for `additivity: semi`; remove `not_over:`, or declare `additivity: semi`"
)
_ADDS_UP = (
    "`aggregation: {aggregation}` adds values up, so `additivity` must be `additive` or `semi`; "
    "for a value that is never summed, use `aggregation: avg`, `min`, `max` or `none`"
)
_DOES_NOT_ADD_UP = (
    "`aggregation: {aggregation}` does not add values up, so `additivity` must be `non`; leave "
    "`additivity:` out, or change `aggregation:`"
)
NOT_OVER_SHAPE = "must be a column name or a list of column names"
"""A `not_over` that is neither; `lakana.assets` repeats the text."""
MEASURE_ID_CHARACTERS = (
    "a measure id must not contain '.', '/', a NUL byte or the unit separator; rename the column "
    "in the asset"
)
"""Why a measure column cannot name its id; `lakana.assets` repeats the text."""
EMPTY_NOT_OVER = "a column must not be empty; write the name of a column of the asset"
_WRONG_KIND = (
    "Asset `{table}` is {article} and declares `measures:`; measures belong on a fact or wide "
    "table. Move them to the fact or wide table that holds these values, or remove `measures:`."
)
_NO_KIND = (
    "Asset `{table}` declares `measures:` but no `kind:`; measures belong on a fact or wide "
    "table. Declare `kind: fact` or `kind: wide`, or remove `measures:`."
)
_KEY_MEASURE = (
    "Measure `{column}` is a key column of `{table}`; a key identifies a row and is not a value "
    "to aggregate. Remove it from `measures:`."
)
_ARTICLE = {"dimension": "a dimension", "staging": "a staging table"}
MEASURED_KINDS = frozenset({"fact", "wide"})
"""The kinds that may declare measures; every other kind is refused, one added later too.

A table whose stored `model_kind` is not one of them has no `table_measures` rows, so the
catalog reads none for it.
"""


def default_additivity(aggregation: str) -> str:
    """Say what additivity an aggregation implies when the header gives none.

    Parameters
    ----------
    aggregation
        One of `AGGREGATIONS`.

    Returns
    -------
    str
        ``additive`` for an aggregation that adds values up, ``non`` otherwise.
    """
    return "additive" if aggregation in _SUMMING else "non"


def unit_message(value: str) -> str:
    """Say that a unit is not one of `UNITS`.

    Parameters
    ----------
    value
        The unit given, already made safe to show.

    Returns
    -------
    str
        The message, listing the units.
    """
    return (
        f"must be one of {', '.join(UNITS)}; got '{value}'. For money use `currency:`; to have a "
        "unit added, open an issue"
    )


def currency_message(value: str) -> str:
    """Say that a currency is not an ISO 4217 code in capitals.

    Parameters
    ----------
    value
        The currency given, already made safe to show.

    Returns
    -------
    str
        The message.
    """
    return f"must be an ISO 4217 currency code in capitals, such as EUR, USD or CHF; got '{value}'"


class MeasureSpec(StrictModel):
    """One measure of an asset's header, as `measures: {<column>: …}` declares it.

    Parameters
    ----------
    aggregation
        One of `AGGREGATIONS`; required (`None` only until the model check refuses it).
    additivity
        One of `ADDITIVITIES`, or `None` to take `default_additivity`.
    not_over
        The columns a `semi` measure must not be summed over.
    unit
        One of `UNITS`, or `None`.
    currency
        One of `CURRENCY_CODES`, or `None`.
    label
        One line for people, in the main language.
    description
        What the measure means, in the main language.
    """

    aggregation: str | None = None
    additivity: str | None = None
    not_over: tuple[str, ...] = ()
    unit: str | None = None
    currency: str | None = None
    label: str | None = None
    description: str | None = None

    @property
    def resolved_additivity(self) -> str:
        """The declared additivity, or the one the aggregation implies.

        Returns
        -------
        str
            One of `ADDITIVITIES`.
        """
        return self.additivity or default_additivity(self.aggregation or "none")

    @field_validator("aggregation")
    @classmethod
    def _aggregation_is_known(cls, value: str | None) -> str | None:
        """Refuse an aggregation that is not one of `AGGREGATIONS`.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        str or None
            `value`.

        Raises
        ------
        ValueError
            If it is not one of them.
        """
        if value is not None and value not in AGGREGATIONS:
            raise ValueError(f"must be one of {', '.join(AGGREGATIONS)}; got '{shown(value)}'")
        return value

    @field_validator("additivity")
    @classmethod
    def _additivity_is_known(cls, value: str | None) -> str | None:
        """Refuse an additivity that is not one of `ADDITIVITIES`.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        str or None
            `value`.

        Raises
        ------
        ValueError
            If it is not one of them.
        """
        if value is not None and value not in ADDITIVITIES:
            raise ValueError(f"must be one of {', '.join(ADDITIVITIES)}; got '{shown(value)}'")
        return value

    @field_validator("not_over", mode="before")
    @classmethod
    def _not_over_is_one_spelling(cls, value: object) -> object:
        """Read a single column name as a one-column list, and `None` as no columns.

        Parameters
        ----------
        value
            The field's raw value.

        Returns
        -------
        object
            A one-tuple for a string, an empty tuple for `None`, else `value`.

        Raises
        ------
        ValueError
            With `NOT_OVER_SHAPE` for anything but a string, `None` or a list of strings.
        """
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        if not isinstance(value, list | tuple) or not all(isinstance(c, str) for c in value):
            raise ValueError(NOT_OVER_SHAPE)
        return value

    @field_validator("not_over")
    @classmethod
    def _not_over_columns_are_usable(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Refuse an empty, unsafe or repeated `not_over` column.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        tuple of str
            `value`.

        Raises
        ------
        ValueError
            For the first such column.
        """
        seen: set[str] = set()
        for column in value:
            if not column:
                raise ValueError(EMPTY_NOT_OVER)
            if unsafe_identifier(column):
                raise ValueError(f"a column {IDENTIFIER_MESSAGE}")
            if column in seen:
                raise ValueError(f"column `{column}` is listed twice")
            seen.add(column)
        return value

    @field_validator("unit")
    @classmethod
    def _unit_is_known(cls, value: str | None) -> str | None:
        """Refuse a unit that is not one of `UNITS`, listing them.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        str or None
            `value`.

        Raises
        ------
        ValueError
            If it is not one of them.
        """
        if value is not None and value not in UNITS:
            raise ValueError(unit_message(shown(value)))
        return value

    @field_validator("currency")
    @classmethod
    def _currency_is_known(cls, value: str | None) -> str | None:
        """Refuse a currency that is not in `CURRENCY_CODES`.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        str or None
            `value`.

        Raises
        ------
        ValueError
            If it is not one of them; lower case included.
        """
        if value is not None and value not in CURRENCY_CODES:
            raise ValueError(currency_message(shown(value)))
        return value

    @field_validator("label")
    @classmethod
    def _label_is_one_line(cls, value: str | None) -> str | None:
        """Refuse a label that is empty, long, several lines or has an unsafe character.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        str or None
            `value` without surrounding whitespace.

        Raises
        ------
        ValueError
            If it breaks one of those rules.
        """
        if value is None:
            return value
        return checked_text(value, limit=MAX_LABEL_CHARS, one_line=True)

    @field_validator("description")
    @classmethod
    def _description_is_text(cls, value: str | None) -> str | None:
        """Refuse a description that is empty, too long or has an unsafe character.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        str or None
            `value` without surrounding whitespace.

        Raises
        ------
        ValueError
            If it breaks one of those rules.
        """
        if value is None:
            return value
        return checked_text(value, limit=MAX_MEASURE_DESCRIPTION_CHARS)

    @model_validator(mode="after")
    def _fields_agree(self) -> MeasureSpec:
        """Refuse a measure whose fields contradict each other or leave out a required one.

        Returns
        -------
        MeasureSpec
            `self`.

        Raises
        ------
        ValueError
            In the spec's order: no aggregation, an additivity the aggregation does not allow,
            `semi` without `not_over`, `not_over` without `semi`, neither or both of `unit` and
            `currency`.
        """
        aggregation = self.aggregation
        if aggregation is None:
            raise ValueError(NEEDS_AGGREGATION)
        if self.additivity is not None:
            sums = aggregation in _SUMMING
            if (self.additivity == "non") is sums:
                text = _ADDS_UP if sums else _DOES_NOT_ADD_UP
                raise ValueError(text.format(aggregation=aggregation))
        if self.additivity == "semi" and not self.not_over:
            raise ValueError(SEMI_NEEDS_NOT_OVER)
        if self.additivity != "semi" and self.not_over:
            raise ValueError(NOT_OVER_NEEDS_SEMI)
        if self.unit is None and self.currency is None:
            raise ValueError(NEEDS_UNIT)
        if self.unit is not None and self.currency is not None:
            raise ValueError(UNIT_AND_CURRENCY)
        return self


def measure_issues(
    kind: str | None,
    key: Sequence[str] | str,
    measures: Mapping[str, MeasureSpec],
    *,
    table: str,
) -> list[str]:
    """Check an asset's measures against its kind and its key.

    Parameters
    ----------
    kind
        The asset's declared kind, or `None`.
    key
        Its key columns, or ``none``.
    measures
        Column → its measure.
    table
        Its dotted table name, for the messages.

    Returns
    -------
    list of str
        The spec's texts: measures on a kind other than fact or wide (or on no kind), then one
        per measure that is a key column, in column order; empty when they agree.
    """
    if not measures:
        return []
    issues: list[str] = []
    if kind is None:
        issues.append(_NO_KIND.format(table=table))
    elif kind not in MEASURED_KINDS:
        article = _ARTICLE.get(kind, f"a {kind} table")
        issues.append(_WRONG_KIND.format(table=table, article=article))
    keyed = set() if isinstance(key, str) else set(key)
    issues.extend(
        _KEY_MEASURE.format(column=column, table=table)
        for column in sorted(measures)
        if column in keyed
    )
    return issues


@dataclass(frozen=True)
class MeasureEntry:
    """One measure as a manifest entry and the catalog hold it.

    Parameters
    ----------
    column
        The measure's column.
    aggregation
        One of `AGGREGATIONS`.
    additivity
        One of `ADDITIVITIES`, resolved.
    not_over
        The columns a `semi` measure must not be summed over.
    unit
        One of `UNITS`, or `None`.
    currency
        One of `CURRENCY_CODES`, or `None`.
    label
        The label, or `None`.
    description
        The description, or `None`.
    """

    column: str
    aggregation: str
    additivity: str
    not_over: tuple[str, ...]
    unit: str | None
    currency: str | None
    label: str | None
    description: str | None


def measure_entries(measures: Mapping[str, MeasureSpec]) -> list[dict[str, object]]:
    """Write an asset's measures as its manifest entry holds them.

    Parameters
    ----------
    measures
        Column → its validated measure.

    Returns
    -------
    list of dict
        One mapping per measure, sorted by column, with the additivity resolved.
    """
    return [
        {
            "column": column,
            "aggregation": spec.aggregation,
            "additivity": spec.resolved_additivity,
            "not_over": list(spec.not_over),
            "unit": spec.unit,
            "currency": spec.currency,
            "label": spec.label,
            "description": spec.description,
        }
        for column, spec in sorted(measures.items())
    ]


def _measure_entry(item: object) -> MeasureEntry | None:
    """Read one stored measure, or nothing when the catalog could not hold it.

    Parameters
    ----------
    item
        One element of a stored `measures` list; untrusted.

    Returns
    -------
    MeasureEntry or None
        `None` unless every value is what a deploy writes.
    """
    if not isinstance(item, Mapping):
        return None
    column, aggregation = item.get("column"), item.get("aggregation")
    additivity, not_over = item.get("additivity"), item.get("not_over")
    unit, currency = item.get("unit"), item.get("currency")
    label, description = item.get("label"), item.get("description")
    if not isinstance(column, str) or len(column.encode("utf-8")) > MAX_MEASURE_COLUMN_BYTES:
        return None
    if aggregation not in AGGREGATIONS or additivity not in ADDITIVITIES:
        return None
    if not isinstance(not_over, list) or not all(isinstance(c, str) for c in not_over):
        return None
    if not (unit is None or unit in UNITS) or not (currency is None or currency in CURRENCY_CODES):
        return None
    if not all(text is None or isinstance(text, str) for text in (label, description)):
        return None
    return MeasureEntry(
        column,
        str(aggregation),
        str(additivity),
        tuple(not_over),
        unit,
        currency,
        label,
        description,
    )


def read_measure_entries(raw: object) -> tuple[MeasureEntry, ...]:
    """Read a stored `measures` list, failing safe.

    Parameters
    ----------
    raw
        The value of ``entry["model"]["measures"]``; untrusted.

    Returns
    -------
    tuple of MeasureEntry
        The readable measures sorted by column; a column listed twice is left out entirely.
    """
    found: dict[str, MeasureEntry] = {}
    repeated: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        entry = _measure_entry(item)
        if entry is None:
            continue
        if entry.column in found:
            repeated.add(entry.column)
        found[entry.column] = entry
    return tuple(entry for column, entry in sorted(found.items()) if column not in repeated)
