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
from typing import TypeVar

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

_CHECK_FAMILIES = ("key", "references")
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
        Column name → description.
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
        Generated-check family (``key``, ``references``) → what a failure does (``block``,
        ``keep`` or ``warn``).
    """

    inputs: dict[str, str]
    description: str | None
    owner: str | None
    tags: tuple[str, ...]
    materialize: str
    columns: dict[str, str] | None
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
            raise ValueError("checks takes only key and references")
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
    owner: str | None = None,
    tags: Iterable[str] = (),
    materialize: str | None = None,
    columns: Mapping[str, str] | None = None,
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
    owner
        Owning team or person.
    tags
        Free-form tags.
    materialize
        ``table`` (the only supported value); not allowed with ``output="artifact"``. An asset
        that appends says ``write="append"`` instead.
    columns
        Column name → description.
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
        Generated-check family (``key``, ``references``) → ``block``, ``keep`` or ``warn``:
        what a failure does.

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
        `kind`, `grain`, `references` or `checks` deploy would refuse (also any of them on an
        artifact asset).
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
            ("columns", columns),
            ("write", None if write in (None, "replace") else write),
            ("key", key or None),
            ("watermark", watermark),
            ("evolve_schema", evolves or None),
            ("kind", kind),
            ("grain", grain),
            ("references", references),
            ("checks", checks),
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
    if evolves and mode != "replace":
        raise ValueError(EVOLVE_NEEDS_REPLACE)
    spec = AssetSpec(
        inputs=dict(inputs) if inputs is not None else {},
        description=description,
        owner=owner,
        tags=tuple(tags),
        materialize="table",
        columns=dict(columns) if columns is not None else None,
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
