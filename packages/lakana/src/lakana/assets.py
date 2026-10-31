"""The `@asset` decorator: the contract of a Python asset.

One file under `assets/` holds one decorated function. Deploy reads the
decorator with `ast` and never imports the file; at run time the harness
(`lakana.runner`) imports it, finds the function carrying `AssetSpec` and
calls it with one `polars.DataFrame` per declared input. A parameter named
``ctx`` is not an input: it is reserved for the asset's context. An asset returns a
`polars.DataFrame`; with ``output="artifact"`` it writes files into
`output_dir` and returns a `pathlib.Path` inside that directory instead.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., object])

ASSET_ATTRIBUTE = "__lakana_asset__"
"""The attribute `asset` sets on the decorated function."""

MAX_KEEP_VERSIONS = 2**31 - 1
"""The largest `keep_versions`; deploy refuses the same values (a parity test pins both)."""

DECIMALS_MODES = ("float", "exact")
"""The values of `decimals`; deploy accepts the same two (a parity test pins both)."""

CONNECTION_NAME = re.compile(r"\A[a-z][a-z0-9-]{0,62}\Z")
"""A connection's name; deploy checks the same pattern (a parity test pins both)."""

MAX_CONNECTIONS = 16
"""The most connections one asset declares; deploy refuses more (a parity test pins both)."""

WRITE_CHOICES = ("replace", "append", "merge")
"""The values of `write`; deploy accepts the same three (a parity test pins both)."""

MODEL_KINDS = ("fact", "dimension", "wide", "staging")
"""The values of `kind`; the same as `lakana_core.projects.asset_model.MODEL_KINDS` (a parity
test pins both)."""

AGGREGATIONS = ("sum", "count", "min", "max", "avg", "count_distinct", "none")
"""A measure's aggregations; the same as `lakana_core.projects.measures.AGGREGATIONS` (a parity
test pins both)."""

ADDITIVITIES = ("additive", "semi", "non")
"""A measure's additivities (a parity test pins them)."""

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
"""A measure's units (a parity test pins them)."""

# jscpd:ignore-start
# The same two lists as `lakana_core.projects.measures`; the SDK may not import core.
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
CURRENT_CURRENCIES = frozenset(_CURRENT_TEXT.split())
"""ISO 4217 List One as of 2026, without XTS and XXX (a parity test pins them)."""

_WITHDRAWN_TEXT = (
    "ANG ATS AZM BEF BGN BYR CUC CYP DEM EEK ESP FIM FRF GHC GRD HRK IEP ITL LTL LUF LVL MRO "
    "MTL NLG PTE ROL SIT SKK SLL STD TMM TRL VEB VEF ZMK ZWL"
)
WITHDRAWN_CURRENCIES = frozenset(_WITHDRAWN_TEXT.split())
"""Codes withdrawn since 1999 that historic data still carries (a parity test pins them)."""
# jscpd:ignore-end

CURRENCY_CODES = CURRENT_CURRENCIES | WITHDRAWN_CURRENCIES
"""The currencies a measure may declare, in capitals."""

MAX_MEASURES = 100
"""The most measures one asset declares (a parity test pins the number)."""

_MEASURE_KEYS = (
    "aggregation",
    "additivity",
    "not_over",
    "unit",
    "currency",
    "label",
    "description",
)
_MAX_LABEL_CHARS = 100
_FREE_TEXT_MESSAGE = (
    "must not contain a surrogate, a control character other than a newline or tab, "
    "a bidirectional control or a line separator"
)
_MAX_DESCRIPTION_CHARS = 4000
_IDENTIFIER_MESSAGE = (
    "must not contain a control, format or surrogate character or a line separator"
)
_IDENTIFIER_CATEGORIES = ("Cc", "Cf", "Cs", "Zl", "Zp")
_MAX_KEY_CHARS = 80
_NOT_A_TEXT = (
    "must be a text; in YAML, quote a value that reads as a number, a date, `yes` or `null`"
)

_CHECK_FAMILIES = ("key", "references", "measures")
_CHECK_MODES = ("block", "keep", "warn")
_MAX_GRAIN_CHARS = 300
_KEY_NONE = "none"

EVOLVE_NEEDS_REPLACE = (
    'evolve_schema=True works with write="replace" (the default) only; remove evolve_schema, '
    "or replace the table instead of appending or merging"
)
"""Why `evolve_schema` is refused with an append or a merge; deploy says the same (a parity
test pins both)."""

_UNSAFE_CATEGORIES = ("Cc", "Cf", "Cs", "Zl", "Zp")
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e") | frozenset(
    chr(code) for code in range(0x2066, 0x206A)
)
_IDENTIFIER_MESSAGE = (
    "must not contain a control, format or surrogate character or a line separator"
)

_PARENTHESES = "write @asset(...) with parentheses"
_NO_OUTPUT_DIR = (
    'lakana.output_dir() is available only while an asset declared with output="artifact" '
    "runs as a task"
)
_output_dir: Path | None = None


def output_dir() -> Path:
    """Return the directory an artifact asset writes its files into.

    The function writes a file or a tree here and returns its path: a file
    inside this directory, or the directory itself (or a subdirectory).

    The directory is available from the import of the asset's module until the
    upload has ended, so a call at module level works too; it is meant for the
    asset function.

    Returns
    -------
    pathlib.Path
        An empty, private directory of this task attempt.

    Raises
    ------
    RuntimeError
        Outside a task, and in a task whose asset produces a table.
    """
    if _output_dir is None:
        raise RuntimeError(_NO_OUTPUT_DIR)
    return _output_dir


def _set_output_dir(path: Path | None) -> None:
    """Set what `output_dir` returns; called by the harness only.

    Parameters
    ----------
    path
        The attempt's output directory, or `None` to unset it.
    """
    global _output_dir
    _output_dir = path


@dataclass(frozen=True)
class AssetSpec:
    """What `@asset` was given, mirroring SQL front-matter.

    Parameters
    ----------
    inputs
        Parameter name → dotted table name. Every parameter of the function
        receives that table as a `polars.DataFrame`.
    description
        Human description; deploy falls back to the docstring's first paragraph.
    owner
        Owning team or person.
    tags
        Free-form tags.
    materialize
        Always ``table``.
    columns
        Column name → description, or ``{"label": ..., "description": ...}``.
    retries
        Retries after a failed attempt.
    retry_delay
        First retry delay: seconds, or a duration like ``30s``.
    timeout
        Wall-clock limit of one attempt: seconds, or a duration like ``10m``.
    output
        ``table`` (the function returns a `polars.DataFrame`) or ``artifact``.
    keep_versions
        For an artifact asset, how many live versions to keep; `None` keeps all.
    decimals
        ``float`` (the default): a SQL ``DECIMAL`` column of an input arrives as
        ``Float64`` (values beyond 2**53, or with more than about 15 significant digits, are
        rounded). ``exact``: it arrives as ``Decimal``, as stored.
    connections
        Names of connections of the environment the asset reads from; checked at deploy.
    write
        ``replace``, ``append`` or ``merge``: what a run does to the table.
    key
        The columns that identify a row (for ``merge`` too), or ``"none"`` for a fact or wide
        table whose rows no columns identify.
    watermark
        The column whose largest value the next run starts after, for ``append`` (and
        optionally ``merge``).
    evolve_schema
        Whether a ``replace`` lets the table take the schema the function returns.
    kind
        ``fact``, ``dimension``, ``wide`` or ``staging``; `None` when not declared.
    grain
        One line saying what one row is; `None` when not declared.
    references
        Column of this table → the table it refers to (``ns.table`` or ``ns.table.column``).
    checks
        Generated-check family (``key``, ``references``, ``measures``) → what a failure does
        (``block``, ``keep`` or ``warn``).
    measures
        Column → its measure, as given, with ``not_over`` as a list.
    label
        A short name for the table in the project's main language.
    """

    inputs: dict[str, str]
    description: str | None
    owner: str | None
    tags: tuple[str, ...]
    materialize: str
    columns: dict[str, str | dict[str, str]] | None
    retries: int | None
    retry_delay: int | str | None
    timeout: int | str | None
    output: str = "table"
    keep_versions: int | None = None
    decimals: str = "float"
    connections: tuple[str, ...] = ()
    write: str = "replace"
    key: tuple[str, ...] | str = ()
    watermark: str | None = None
    evolve_schema: bool = False
    kind: str | None = None
    grain: str | None = None
    references: dict[str, str] = field(default_factory=dict)
    checks: dict[str, str] = field(default_factory=dict)
    measures: dict[str, dict[str, object]] = field(default_factory=dict)
    label: str | None = None


def _unsafe(text: str) -> bool:
    """Say whether a column name has a character deploy refuses.

    Parameters
    ----------
    text
        The name.

    Returns
    -------
    bool
        True for a control, format or surrogate character or a line or paragraph separator.
    """
    return any(unicodedata.category(char) in _UNSAFE_CATEGORIES for char in text)


def _check_write(
    write: object, key: object, watermark: object
) -> tuple[str, tuple[str, ...] | str, str | None]:
    """Read the decorator's write model with the rules deploy applies.

    Parameters
    ----------
    write
        ``replace``, ``append`` or ``merge``; `None` means ``replace``.
    key
        The key: a list or tuple of strings, one string (a key of one column), or ``"none"``;
        `None` means none.
    watermark
        The watermark column, or `None`.

    Returns
    -------
    tuple
        `write`, `key` and `watermark`.

    Raises
    ------
    ValueError
        For a value of the wrong type or with a character deploy refuses, a `write` outside
        `WRITE_CHOICES`, an empty key column, a key column listed twice, a key of more than 8
        columns, ``append``
        without a watermark, ``merge`` without a key or with ``key="none"``, and ``replace``
        with a watermark.
    """
    mode = "replace" if write is None else write
    if not isinstance(mode, str):
        raise ValueError("write must be a string")
    if mode not in WRITE_CHOICES:
        shown = ascii(mode)[1:-1][:200]
        raise ValueError(f"write must be one of {', '.join(WRITE_CHOICES)}; got '{shown}'")
    key_none = key == _KEY_NONE
    columns = () if key is None or key_none else (key,) if isinstance(key, str) else key
    if not isinstance(columns, (list, tuple)) or not all(
        isinstance(column, str) for column in columns
    ):
        raise ValueError("key must be a list of strings or one string")
    if watermark is not None and not isinstance(watermark, str):
        raise ValueError("watermark must be a string")
    if any(_unsafe(column) for column in columns):
        raise ValueError(f"a key column {_IDENTIFIER_MESSAGE}")
    if any(not column for column in columns):
        raise ValueError("a key column must not be empty; write the name of a column of the asset")
    if watermark is not None and _unsafe(watermark):
        raise ValueError(f"watermark {_IDENTIFIER_MESSAGE}")
    seen: set[str] = set()
    for column in columns:
        if column in seen:
            raise ValueError(f"key column `{column}` is listed twice")
        seen.add(column)
    if len(columns) > 8:
        raise ValueError(
            f"a key has at most 8 columns, and `key:` lists {len(columns)}; shorten it to the "
            "columns that identify one row"
        )
    if mode == "append" and watermark is None:
        raise ValueError(
            "`write: append` needs `watermark:` — the column whose largest value the next run "
            "starts after"
        )
    if mode == "merge" and key_none:
        raise ValueError(
            "`write: merge` needs key columns; `key: none` says that no columns identify a row, "
            "so there is nothing to merge on"
        )
    if mode == "merge" and not columns:
        raise ValueError("`write: merge` needs `key:` — the columns that identify a row")
    if mode == "replace" and watermark is not None:
        raise ValueError(
            "`write: replace` takes no `watermark:`; to add only new rows use `write: append`"
        )
    return mode, _KEY_NONE if key_none else tuple(columns), watermark


def _component_ok(part: str) -> bool:
    """Say whether core's `validate_path_component` would accept one name part.

    Parameters
    ----------
    part
        A namespace or table part, or ``ref_<column>``.

    Returns
    -------
    bool
        False for an empty or blank part, and for one holding a NUL byte, the unit
        separator, ``/`` or ``.``.
    """
    return bool(part.strip()) and not any(c in part for c in ("\x00", "\x1f", "/", "."))


def _unsafe_free_text(text: str) -> bool:
    """Say whether a grain is unsafe to store or render.

    Parameters
    ----------
    text
        The grain.

    Returns
    -------
    bool
        True for a surrogate, a control character other than a newline or tab, a
        bidirectional control or a line or paragraph separator (core's rule).
    """
    for char in text:
        category = unicodedata.category(char)
        if category == "Cc":
            if char not in ("\n", "\t"):
                return True
        elif category in ("Cs", "Zl", "Zp") or char in _BIDI_CONTROLS:
            return True
    return False


def _check_model(
    kind: object, grain: object, key: tuple[str, ...] | str, references: object, checks: object
) -> tuple[str | None, str | None, dict[str, str], dict[str, str]]:
    """Read the decorator's model fields with the rules deploy applies.

    Parameters
    ----------
    kind
        ``fact``, ``dimension``, ``wide`` or ``staging``; `None` for none.
    grain
        One line saying what one row is; `None` for none.
    key
        `_check_write`'s key: its columns, ``"none"`` or empty.
    references
        Column → target table; `None` for none.
    checks
        Check family → failure mode; `None` for none.

    Returns
    -------
    tuple
        `kind`, the stripped `grain`, `references` and `checks` as dicts.

    Raises
    ------
    ValueError
        For a value of the wrong type, a `kind` that is not in `MODEL_KINDS`, a `grain` that
        is empty, long, several lines or unsafe, a reference column or target deploy refuses,
        a `checks` family or mode that does not exist, a fact, dimension or wide table
        without what its kind needs, ``key="none"`` where the kind does not allow it, and a
        `checks` family for which the asset declares nothing.
    """
    if kind is not None and not isinstance(kind, str):
        raise ValueError("kind must be a string")
    if grain is not None and not isinstance(grain, str):
        raise ValueError("grain must be a string")
    for name, mapping in (("references", references), ("checks", checks)):
        if mapping is not None and (
            not isinstance(mapping, Mapping)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items())
        ):
            raise ValueError(f"{name} must be a dict of strings")
    if kind is not None and kind not in MODEL_KINDS:
        shown = ascii(kind)[1:-1][:200]
        raise ValueError(f"kind must be one of {', '.join(MODEL_KINDS)}; got '{shown}'")
    text = None if grain is None else grain.strip()
    if text is not None:
        if not text:
            raise ValueError("grain must not be empty: say in one sentence what one row is")
        if "\n" in text:
            raise ValueError("grain must be one line")
        if len(text) > _MAX_GRAIN_CHARS:
            raise ValueError(f"grain must be at most {_MAX_GRAIN_CHARS} characters")
        if _unsafe_free_text(grain or ""):
            raise ValueError(
                "grain must not contain a surrogate, a control character other than a newline "
                "or tab, a bidirectional control or a line separator"
            )
    refs = dict(references) if isinstance(references, Mapping) else {}
    for column, target in refs.items():
        if not column:
            raise ValueError("a reference column must not be empty")
        if _unsafe(column):
            raise ValueError(f"a reference column {_IDENTIFIER_MESSAGE}")
        if _unsafe(target):
            raise ValueError(f"reference `{column}` names a target that {_IDENTIFIER_MESSAGE}")
        if not _component_ok(f"ref_{column}"):
            raise ValueError(
                f"reference column `{column}` cannot name its check `ref_{column}`: a check "
                "name must not contain '.', '/', a NUL byte or the unit separator"
            )
        parts = target.split(".")
        if len(parts) < 2 or not all(_component_ok(part) for part in parts):
            raise ValueError(
                f"reference `{column}: {ascii(target)[1:-1][:200]}` must name a table as "
                "`ns.table` or `ns.table.column`"
            )
    modes = dict(checks) if isinstance(checks, Mapping) else {}
    for family, mode in modes.items():
        if family not in _CHECK_FAMILIES:
            raise ValueError("checks takes only key, references and measures")
        if mode not in _CHECK_MODES:
            raise ValueError(f"checks: {family} must be block, keep or warn")
    _check_kind_rules(kind, text, key, refs, modes)
    return kind, text, refs, modes


def _check_kind_rules(
    kind: str | None,
    grain: str | None,
    key: tuple[str, ...] | str,
    references: dict[str, str],
    checks: dict[str, str],
) -> None:
    """Apply the rules of each kind to fields that are valid on their own.

    Parameters
    ----------
    kind
        The kind, or `None`.
    grain
        The stripped grain, or `None`.
    key
        The key's columns, ``"none"`` or empty.
    references
        Column → target table.
    checks
        Check family → failure mode.

    Raises
    ------
    ValueError
        As `_check_model` says, for the rules between fields.
    """
    article = {"fact": "a fact", "wide": "a wide table", "dimension": "a dimension"}.get(kind or "")
    if article and not grain:
        raise ValueError(f"{article} needs grain=: say in one sentence what one row is")
    if kind == "dimension" and key == _KEY_NONE:
        raise ValueError(
            'a dimension needs a real key=; key="none" is not allowed for a dimension, '
            "because nothing could refer to it"
        )
    if article and not key:
        raise ValueError(
            f"{article} needs key=: list the columns that identify one row, or write "
            'key="none" and say in grain why no columns are unique'
        )
    if key == _KEY_NONE and kind not in ("fact", "wide"):
        raise ValueError(
            "`key: none` is for a fact or a wide table whose rows no columns identify; "
            "declare `kind: fact` or `kind: wide`, or remove `key: none`"
        )
    if "key" in checks and (not key or key == _KEY_NONE):
        raise ValueError(
            "checks: key sets how the key checks fail, but the asset declares no key; declare "
            "key=, or remove checks: key"
        )
    if "references" in checks and not references:
        raise ValueError(
            "checks: references sets how the reference checks fail, but the asset declares "
            "no references; declare references=, or remove checks: references"
        )


def _check_label(where: str, value: object) -> str:
    """Apply the label rule deploy applies.

    Parameters
    ----------
    where
        The field, for the text (``label``, ``columns.c.label``, ``measures.m.label``).
    value
        The label.

    Returns
    -------
    str
        The stripped label.

    Raises
    ------
    ValueError
        With deploy's text for the first rule it breaks.
    """
    if not isinstance(value, str):
        raise ValueError(f"{where}: {_NOT_A_TEXT}")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{where}: must not be empty")
    if "\n" in stripped:
        raise ValueError(f"{where}: must be one line")
    if len(stripped) > _MAX_LABEL_CHARS:
        raise ValueError(f"{where}: must be at most {_MAX_LABEL_CHARS} characters")
    if _unsafe_free_text(value):
        raise ValueError(f"{where}: {_FREE_TEXT_MESSAGE}")
    return stripped


def _shown(name: object) -> str:
    """Escape a name for a message as core's `shown` does.

    Parameters
    ----------
    name
        A column name as given.

    Returns
    -------
    str
        The ASCII-escaped text, at most 200 characters; the type's name for anything that is
        not a string.
    """
    if not isinstance(name, str):
        return type(name).__name__
    return ascii(name)[1:-1][:200]


def _loc(name: object) -> str:
    """Render a name inside a field path as core's `shown_key` does.

    Parameters
    ----------
    name
        A column name as given.

    Returns
    -------
    str
        One line of at most 80 characters: each non-printable character escaped, cut with a
        trailing ``…``, never inside an escape.
    """
    pieces: list[str] = []
    length = 0
    for char in str(name):
        piece = char if char.isprintable() else ascii(char)[1:-1]
        pieces.append(piece)
        length += len(piece)
        if length > _MAX_KEY_CHARS:
            kept, used = [], 0
            for piece in pieces:
                if used + len(piece) > _MAX_KEY_CHARS - 1:
                    break
                kept.append(piece)
                used += len(piece)
            return "".join(kept) + "…"
    return "".join(pieces)


def _check_columns(columns: object) -> dict[str, str | dict[str, str]] | None:
    """Read the decorator's `columns` with the rules deploy applies to its long form.

    Parameters
    ----------
    columns
        Column → description, or column → ``{"label": ..., "description": ...}``; `None` for none.

    Returns
    -------
    dict or None
        The columns as given, labels stripped.

    Raises
    ------
    ValueError
        With deploy's text for a value of neither form, an empty or unknown-keyed long form, or a
        bad label.
    """
    if columns is None:
        return None
    if not isinstance(columns, Mapping):
        raise ValueError("columns must be a dict of strings or dicts")
    result: dict[str, str | dict[str, str]] = {}
    for name, given in columns.items():
        if not isinstance(given, (str, Mapping)):
            raise ValueError(
                f"columns: `{_shown(name)}` must be a description or a mapping such as "
                "`{label: Store, description: The store that sold.}`"
            )
        if not isinstance(name, str):
            raise ValueError(f"columns.{_loc(name)}: {_NOT_A_TEXT}")
        if isinstance(given, str):
            if _unsafe_free_text(given):
                raise ValueError(f"columns: a column description {_FREE_TEXT_MESSAGE}")
            result[name] = given
            continue
        where = f"columns.{_loc(name)}"
        for key in given:
            if key not in ("label", "description"):
                raise ValueError(f"{where}.{_loc(key)}: Extra inputs are not permitted")
        texts = {key: value for key, value in given.items() if value is not None}  # as core
        if not texts:
            raise ValueError(
                f"{where}: needs `label:` or `description:`; write a description alone "
                "as a plain string"
            )
        description = texts.get("description")
        if description is not None:
            if not isinstance(description, str):
                raise ValueError(f"{where}.description: {_NOT_A_TEXT}")
            if _unsafe_free_text(description):
                raise ValueError(f"{where}.description: {_FREE_TEXT_MESSAGE}")
        if "label" in texts:
            texts["label"] = _check_label(f"{where}.label", texts["label"])
        result[name] = texts
    if any(
        unicodedata.category(char) in _IDENTIFIER_CATEGORIES
        for name in result
        for char in str(name)
    ):
        raise ValueError(f"columns: a column name {_IDENTIFIER_MESSAGE}")
    return result


def _check_measure(column: str, spec: Mapping[object, object]) -> dict[str, Any]:
    """Check one measure with deploy's field rules, in deploy's order and words.

    Parameters
    ----------
    column
        The measure's column.
    spec
        Its mapping.

    Returns
    -------
    dict
        A copy, `not_over` as a list (a string is one column).

    Raises
    ------
    ValueError
        With the text the deploy prints for the same measure.
    """
    where = f"measures.{column}"
    for key in spec:
        if key not in _MEASURE_KEYS:
            raise ValueError(f"{where}.{key}: Extra inputs are not permitted")
    given: dict[str, Any] = {str(key): value for key, value in spec.items() if value is not None}
    for key in ("aggregation", "additivity", "unit", "currency", "label", "description"):
        if key in given and not isinstance(given[key], str):
            raise ValueError(f"{where}.{key}: {_NOT_A_TEXT}")
    aggregation, additivity = given.get("aggregation"), given.get("additivity")
    if aggregation is not None and aggregation not in AGGREGATIONS:
        raise ValueError(
            f"{where}.aggregation: must be one of {', '.join(AGGREGATIONS)}; "
            f"got '{ascii(aggregation)[1:-1][:200]}'"
        )
    if additivity is not None and additivity not in ADDITIVITIES:
        raise ValueError(
            f"{where}.additivity: must be one of {', '.join(ADDITIVITIES)}; "
            f"got '{ascii(additivity)[1:-1][:200]}'"
        )
    raw_not_over = given.get("not_over", [])
    not_over = [raw_not_over] if isinstance(raw_not_over, str) else raw_not_over
    if not isinstance(not_over, list | tuple) or not all(isinstance(c, str) for c in not_over):
        raise ValueError(f"{where}.not_over: must be a column name or a list of column names")
    seen: set[str] = set()
    for other in not_over:
        if not other:
            raise ValueError(
                f"{where}.not_over: a column must not be empty; write the name of a column of "
                "the asset"
            )
        if _unsafe(other):
            raise ValueError(f"{where}.not_over: a column {_IDENTIFIER_MESSAGE}")
        if other in seen:
            raise ValueError(f"{where}.not_over: column `{other}` is listed twice")
        seen.add(other)
    unit, currency = given.get("unit"), given.get("currency")
    if unit is not None and unit not in UNITS:
        raise ValueError(
            f"{where}.unit: must be one of {', '.join(UNITS)}; got '{ascii(unit)[1:-1][:200]}'. "
            "For money use `currency:`; to have a unit added, open an issue"
        )
    if currency is not None and currency not in CURRENCY_CODES:
        raise ValueError(
            f"{where}.currency: must be an ISO 4217 currency code in capitals, such as EUR, USD "
            f"or CHF; got '{ascii(currency)[1:-1][:200]}'"
        )
    if "label" in given:
        _check_label(f"{where}.label", given["label"])
    text = given.get("description")
    if text is not None:
        stripped = text.strip()
        if not stripped:
            raise ValueError(f"{where}.description: must not be empty")
        if len(stripped) > _MAX_DESCRIPTION_CHARS:
            raise ValueError(
                f"{where}.description: must be at most {_MAX_DESCRIPTION_CHARS} characters"
            )
        if _unsafe_free_text(text):
            raise ValueError(f"{where}.description: {_FREE_TEXT_MESSAGE}")
    if aggregation is None:
        raise ValueError(
            f"{where}: needs `aggregation:`: one of sum, count, min, max, avg, count_distinct or "
            "none (none for a value that is never aggregated)"
        )
    sums = aggregation in ("sum", "count")
    if additivity is not None and (additivity == "non") is sums:
        raise ValueError(
            f"{where}: `aggregation: {aggregation}` adds values up, so `additivity` must be "
            "`additive` or `semi`; for a value that is never summed, use `aggregation: avg`, "
            "`min`, `max` or `none`"
            if sums
            else f"{where}: `aggregation: {aggregation}` does not add values up, so `additivity` "
            "must be `non`; leave `additivity:` out, or change `aggregation:`"
        )
    if additivity == "semi" and not not_over:
        raise ValueError(
            f"{where}: `additivity: semi` needs `not_over:`: the columns the measure must not be "
            "summed over, such as the date of a balance"
        )
    if additivity != "semi" and not_over:
        raise ValueError(
            f"{where}: `not_over:` is only for `additivity: semi`; remove `not_over:`, or "
            "declare `additivity: semi`"
        )
    if unit is None and currency is None:
        raise ValueError(
            f"{where}: needs `unit:` or `currency:`: `currency:` for money (an ISO 4217 code "
            "such as EUR), `unit:` for anything else (for a number of things, `unit: count`)"
        )
    if unit is not None and currency is not None:
        raise ValueError(
            f"{where}: takes `unit:` or `currency:`, not both; money has a currency, every other "
            "value a unit"
        )
    return {**given, **({"not_over": list(not_over)} if "not_over" in given else {})}


def _check_measures(
    measures: object, kind: str | None, key: tuple[str, ...] | str, checks: dict[str, str]
) -> dict[str, dict[str, object]]:
    """Read the decorator's `measures` with the rules deploy applies.

    Parameters
    ----------
    measures
        Column → its measure; `None` for none.
    kind
        The checked kind, or `None`.
    key
        The checked key: its columns, ``"none"`` or empty.
    checks
        The checked check families.

    Returns
    -------
    dict
        Column → `_check_measure`'s copy.

    Raises
    ------
    ValueError
        With deploy's text for a field rule, and in decorator words for a rule between fields:
        measures on a kind other than fact or wide, a key column as a measure, and
        `checks: measures` without measures.
    """
    if measures is not None and not isinstance(measures, Mapping):
        raise ValueError("measures must be a dict of dicts")
    given = dict(measures) if isinstance(measures, Mapping) else {}
    if len(given) > MAX_MEASURES:
        raise ValueError(
            f"measures: at most {MAX_MEASURES} measures per table, and `measures:` lists "
            f"{len(given)}; keep the ones people aggregate, and describe the others in `columns:`"
        )
    checked: dict[str, dict[str, Any]] = {}
    for column, spec in given.items():
        if not isinstance(column, str):
            raise ValueError("measures must be a dict of dicts")
        if not isinstance(spec, Mapping):
            raise ValueError(
                f"measures: `{ascii(column)[1:-1][:200]}` must be a mapping such as "
                "`{aggregation: sum, currency: EUR}`"
            )
        if not column:
            raise ValueError("measures: a measure column must not be empty")
        if _unsafe(column):
            raise ValueError(f"measures: a measure column {_IDENTIFIER_MESSAGE}")
        size = len(column.encode("utf-8"))
        if size > 1024:
            raise ValueError(
                f"measures: measure column `{column[:100]}…` has {size} bytes; a measure column "
                "may have at most 1024 bytes"
            )
        if not _component_ok(f"measure_{column}"):
            raise ValueError(
                f"measures: measure column `{column}` cannot name its id `measure_{column}`: a "
                "measure id must not contain '.', '/', a NUL byte or the unit separator; rename "
                "the column in the asset"
            )
        checked[column] = _check_measure(column, spec)
        if column in checked[column].get("not_over", []):
            raise ValueError(
                f"measures: measure `{column}` names itself in `not_over`; list the other "
                "columns it must not be summed over, such as a date"
            )
    if "measures" in checks and not checked:
        raise ValueError(
            "checks: measures sets how the measure checks fail, but the asset declares no "
            "measures; declare measures=, or remove checks: measures"
        )
    if checked and kind not in ("fact", "wide"):
        raise ValueError(
            'measures belong on a fact or wide table; declare kind="fact" or kind="wide", or '
            "remove measures="
        )
    keyed = set() if isinstance(key, str) else set(key)
    for column in sorted(checked):
        if column in keyed:
            raise ValueError(
                f"measure `{column}` is a key column; a key identifies a row and is not a value "
                "to aggregate. Remove it from measures="
            )
    return checked


def _check_connections(connections: object) -> tuple[str, ...]:
    """Read the decorator's `connections` with the rules deploy applies.

    Parameters
    ----------
    connections
        The decorator's argument; `None` declares none.

    Returns
    -------
    tuple of str
        The names, in the order given.

    Raises
    ------
    ValueError
        For a value that is not a list or tuple of strings, a name that breaks
        `CONNECTION_NAME`, a name listed twice, or more than `MAX_CONNECTIONS` names.
    """
    if connections is None:
        return ()
    if not isinstance(connections, (list, tuple)) or not all(
        isinstance(name, str) for name in connections
    ):
        raise ValueError("connections must be a list of strings")
    names = tuple(connections)
    if len(names) > MAX_CONNECTIONS:
        raise ValueError(f"an asset declares at most {MAX_CONNECTIONS} connections")
    seen: set[str] = set()
    for name in names:
        if not CONNECTION_NAME.match(name):
            shown = ascii(name)[1:-1][:200]
            raise ValueError(
                "connection names are lower-case letters, digits and '-', starting with a "
                f"letter, at most 63; got '{shown}'"
            )
        if name in seen:
            raise ValueError(f"connection `{name}` is listed twice")
        seen.add(name)
    return names


def _check_keep_versions(keep_versions: object) -> None:
    """Refuse a `keep_versions` that is not an integer from 1 to `MAX_KEEP_VERSIONS`.

    Parameters
    ----------
    keep_versions
        The decorator's argument; `None` (keep all) is accepted.

    Raises
    ------
    ValueError
        With the rule; the message never prints a number of more than 64 bits,
        because converting a huge integer to text raises in Python.
    """
    if keep_versions is None:
        return
    if isinstance(keep_versions, int) and not isinstance(keep_versions, bool):
        if keep_versions > MAX_KEEP_VERSIONS:
            raise ValueError(
                f"keep_versions must be at most {MAX_KEEP_VERSIONS}; "
                "remove it to keep every version"
            )
        if keep_versions >= 1:
            return
        shown = repr(keep_versions) if keep_versions.bit_length() <= 64 else "a negative number"
    else:
        shown = repr(keep_versions)
    raise ValueError(f"keep_versions must be an integer of at least 1, got {shown}")


def asset(
    function: object = None,
    /,
    *,
    inputs: Mapping[str, str] | None = None,
    description: str | None = None,
    label: str | None = None,
    owner: str | None = None,
    tags: Iterable[str] = (),
    materialize: str | None = None,
    columns: Mapping[str, str | Mapping[str, str]] | None = None,
    retries: int | None = None,
    retry_delay: int | str | None = None,
    timeout: int | str | None = None,
    output: str = "table",
    keep_versions: int | None = None,
    decimals: str = "float",
    connections: Iterable[str] = (),
    write: str = "replace",
    key: Iterable[str] | str = (),
    watermark: str | None = None,
    evolve_schema: bool | None = False,
    kind: str | None = None,
    grain: str | None = None,
    references: Mapping[str, str] | None = None,
    checks: Mapping[str, str] | None = None,
    measures: Mapping[str, Mapping[str, object]] | None = None,
) -> Callable[[F], F]:
    """Declare a Python asset; returns the function unchanged with its metadata attached.

    Every argument must be a literal in the source file: deploy reads the
    decorator without running the file.

    Parameters
    ----------
    function
        Never pass this. It only catches a bare ``@asset``.
    inputs
        Parameter name → dotted table name.
    description
        Human description.
    label
        A short name for the table in the project's main language: one line, at most 100
        characters.
    owner
        Owning team or person.
    tags
        Free-form tags.
    materialize
        ``table`` (the only supported value); not allowed with ``output="artifact"``. An asset
        that appends says ``write="append"`` instead.
    columns
        Column name → its description, or `{"label": ..., "description": ...}`.
    retries
        Retries after a failed attempt.
    retry_delay
        First retry delay.
    timeout
        Wall-clock limit of one attempt.
    output
        ``table`` (the default): the function returns a `polars.DataFrame` that
        becomes the table. ``artifact``: the function writes files into
        `output_dir` and returns a path inside it; each run stores one new,
        numbered version of the artifact, which another asset can read.
    keep_versions
        Only with ``output="artifact"``: how many live versions to keep, from 1
        to 2147483647. After each run the oldest versions beyond it are
        deleted. Leave it out to keep every version.
    decimals
        ``float`` (the default): a SQL ``DECIMAL`` column of an input reaches
        the function as a ``Float64`` column, so ``value * 0.5`` works; decimals
        inside lists and structs too. ``exact``: it arrives as a ``Decimal``
        column of ``decimal.Decimal`` values, for money or any calculation that
        must not round. A float holds about 15 significant digits, so with ``float`` a value
        beyond 2**53 or with more digits arrives rounded, without an error: use ``exact`` for a
        decimal identifier or a wide sum. What the function returns is stored as it is.
    connections
        Names of connections of the environment the asset reads from; each must exist when
        you deploy. Inside the task, `lakana.ingest.connections.get` reads one, with its
        secret. At most 16, each lower-case letters, digits and ``-``, starting with a letter.
    write
        ``replace`` (the default): every run replaces the table's rows with what the
        function returns.
        ``append``: the first run creates the table; each later run adds the rows it returns,
        and needs `watermark`.
        ``merge``: rows whose `key` matches a row of the table update it, the others are
        added; nothing is deleted. Needs `key`.
    key
        The columns that identify a row: a list, or one string for a key of one column. It is
        also the key of ``write="merge"``. ``"none"`` says that no columns identify a row; only
        for ``kind="fact"`` or ``kind="wide"``, never with ``merge``.
    watermark
        The column whose largest value in the table the next run starts after: needed by
        ``write="append"``, optional for ``merge``.
    evolve_schema
        ``False`` (the default): a run whose result has other columns or types than the table
        fails, and the table stays as it is. ``True``: each run's replace lets the table take
        the schema the function returns: new columns are added, a column whose type widens
        (``Int32`` to ``Int64``, ``Float32`` to ``Float64``) keeps its data, a column whose
        type changes otherwise becomes a new column (its earlier values read as null), and a
        column the result no longer has is removed from the table. Only with
        ``write="replace"``; `None` means ``False``.
    kind
        ``fact``, ``dimension``, ``wide`` or ``staging``. A fact and a wide table need
        `grain` and `key`; a dimension needs `grain` and a real `key`; a staging table needs
        nothing. Not allowed with ``output="artifact"``.
    grain
        One line, at most 300 characters, saying what one row is.
    references
        Column of this table → the table it refers to, as ``ns.table`` (that table's single
        key column) or ``ns.table.column``.
    checks
        Generated-check family (``key``, ``references``, ``measures``) → ``block``, ``keep`` or
        ``warn``: what a failure does.
    measures
        Column → its measure: ``aggregation`` (required), ``additivity``, ``not_over``,
        ``unit`` or ``currency``, ``label``, ``description``; only with ``kind`` ``fact`` or
        ``wide``. Not allowed with ``output="artifact"``.

    Returns
    -------
    callable
        A decorator that sets `ASSET_ATTRIBUTE` on the function.

    Raises
    ------
    TypeError
        For ``@asset`` without parentheses.
    ValueError
        For an `output` other than ``table`` or ``artifact``, a `materialize`
        other than ``table``, `materialize` or `columns` on an artifact asset,
        and a `keep_versions` that is not an integer from 1 to 2147483647 or
        that is given without ``output="artifact"``, a `decimals` other than
        ``float`` or ``exact``, a `connections` that is not a list of valid,
        distinct names or holds more than 16, a `write`, `key` or `watermark` deploy
        would refuse (also any of them on an artifact asset), an `evolve_schema` that is
        not a boolean, or is ``True`` on an artifact asset or with an append or a merge, and a
        `kind`, `grain`, `references`, `checks` or `measures` deploy would refuse (also any of them
        on an artifact asset).
    """
    if function is not None:
        raise TypeError(_PARENTHESES)
    if output not in ("table", "artifact"):
        shown = (
            "a number too large to show"
            if isinstance(output, int) and output.bit_length() > 64
            else repr(output)
        )
        raise ValueError(f"output must be 'table' or 'artifact', got {shown}")
    if decimals not in DECIMALS_MODES:
        shown = (
            "a number too large to show"
            if isinstance(decimals, int) and decimals.bit_length() > 64
            else repr(decimals)
        )
        raise ValueError(
            "decimals must be 'float' (Python floats, the default) or 'exact' "
            f"(decimal.Decimal values), got {shown}"
        )
    evolves = False if evolve_schema is None else evolve_schema
    if not isinstance(evolves, bool):
        raise ValueError("evolve_schema must be True or False")
    if output == "artifact":
        for name, given in (
            ("materialize", materialize),
            ("label", label),
            ("columns", columns),
            ("write", None if write in (None, "replace") else write),
            ("key", key or None),
            ("watermark", watermark),
            ("evolve_schema", evolves or None),
            ("kind", kind),
            ("grain", grain),
            ("references", references),
            ("checks", checks),
            ("measures", measures),
        ):
            if given is not None:
                raise ValueError(f"{name} does not apply to an artifact asset; remove it")
        _check_keep_versions(keep_versions)
    elif keep_versions is not None:
        raise ValueError('keep_versions applies only to an asset with output="artifact"')
    elif materialize == "incremental":
        raise ValueError(
            'a Python asset appends with write="append" and watermark="<column>", '
            "not materialize='incremental'"
        )
    elif materialize not in (None, "table"):
        raise ValueError(f"a Python asset supports only 'table', got {materialize!r}")
    declared = _check_connections(connections)
    mode, key_columns, watermark_column = _check_write(write, key, watermark)
    model = _check_model(kind, grain, key_columns, references, checks)
    measured = _check_measures(measures, model[0], key_columns, model[3])
    checked_label = None if label is None else _check_label("label", label)
    checked_columns = _check_columns(columns)
    if evolves and mode != "replace":
        raise ValueError(EVOLVE_NEEDS_REPLACE)
    spec = AssetSpec(
        inputs=dict(inputs) if inputs is not None else {},
        description=description,
        owner=owner,
        tags=tuple(tags),
        materialize="table",
        columns=checked_columns,
        retries=retries,
        retry_delay=retry_delay,
        timeout=timeout,
        output=output,
        keep_versions=keep_versions,
        decimals=decimals,
        connections=declared,
        write=mode,
        key=key_columns,
        watermark=watermark_column,
        evolve_schema=evolves,
        kind=model[0],
        grain=model[1],
        references=model[2],
        checks=model[3],
        measures=measured,
        label=checked_label,
    )

    def decorate(target: F) -> F:
        """Attach the spec.

        Parameters
        ----------
        target
            The asset function.

        Returns
        -------
        callable
            `target`, unchanged.
        """
        setattr(target, ASSET_ATTRIBUTE, spec)
        return target

    return decorate
