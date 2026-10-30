"""The parsed form of one asset, SQL or Python: what DAG validation and the manifest read."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lakana_core.errors import ValidationError
from lakana_core.identifiers import shown
from lakana_core.projects.measures import measure_issues
from lakana_core.projects.project_config import IDENTIFIER_MESSAGE, unsafe_identifier

if TYPE_CHECKING:
    from lakana_core.projects.frontmatter import AssetMetadata

MAX_KEEP_VERSIONS = 2**31 - 1
"""The largest `keep_versions` read from a manifest; a larger value reads as "keep all".

Public so that the deploy check of a later task refuses the same values. Defined here,
not in `manifest`, because discovery needs it and `manifest` imports discovery's package.
"""


DECIMALS_MODES = ("float", "exact")
"""The values of the decorator's `decimals`; `lakana.assets.DECIMALS_MODES` is the same (a
parity test pins both). Here for the same reason as `MAX_KEEP_VERSIONS`."""

MAX_CONNECTIONS = 16
"""The most connections one asset declares: each takes a relay port of the task.
`lakana.assets.MAX_CONNECTIONS` is the same (a parity test pins both)."""

DEFAULT_DECIMALS = "float"
"""What an asset without the option, and a manifest entry without the key, means."""

MODEL_KINDS = ("fact", "dimension", "wide", "staging")
"""What an asset's `kind:` may say. `lakana.assets.MODEL_KINDS` repeats it (a parity test
pins both, added with the decorator's arguments)."""


def require_model_kind(value: str) -> None:
    """Refuse a model kind a reader asks for that no header can declare.

    The one place the text lives: the table listing's `model_kind` filter and search's kind
    filter both call it.

    Parameters
    ----------
    value
        The kind asked for; untrusted.

    Raises
    ------
    ValidationError
        If `value` is not one of `MODEL_KINDS`; the message lists them and repeats the value.
    """
    if value not in MODEL_KINDS:
        raise ValidationError(
            f"model_kind must be one of {', '.join(MODEL_KINDS)}; got '{shown(value)}'"
        )


CHECK_FAMILIES = ("key", "references", "measures")
"""The generated checks an asset's `checks:` may set the failure mode of."""

CHECK_MODES = ("block", "keep", "warn")
"""What a failed check does; `lakana_core.projects.checks.ON_FAILURE` is this tuple."""

KEY_NONE = "none"
"""`key: none`: the explicit statement that no columns identify a row."""

MAX_KEY_COLUMNS = 8
"""The most columns a key lists; `lakana.assets` repeats the number, as it may not import core."""

MAX_GRAIN_CHARS = 300
"""The longest `grain:`, in characters."""

WRITE_CHOICES = ("replace", "append", "merge")
"""What a run does to an asset's table: replace every row, append the rows past the watermark, or
merge the rows on a key. `lakana.assets.WRITE_CHOICES` is the same (a parity test pins both)."""

APPEND_NEEDS_WATERMARK = (
    "`write: append` needs `watermark:` — the column whose largest value the next run starts after"
)
MERGE_NEEDS_KEY = "`write: merge` needs `key:` — the columns that identify a row"
VIEW_TAKES_NO_WRITE = "`materialize: view` cannot take `write:`"
USE_ONE_SPELLING = "use one: `write: append` with `watermark:` replaces `materialize: incremental`"
EVOLVE_NEEDS_APPEND = "`evolve_schema` works with `write: append` only"
EVOLVE_NEEDS_REPLACE = (
    'evolve_schema=True works with write="replace" (the default) only; remove evolve_schema, '
    "or replace the table instead of appending or merging"
)
"""A Python asset evolves its schema only when it replaces its table: its upload of an append
or a merge cannot change the schema."""
REPLACE_TAKES_NO_WATERMARK = (
    "`write: replace` takes no `watermark:`; to add only new rows use `write: append`"
)
MERGE_KEY_NONE = (
    "`write: merge` needs key columns; `key: none` says that no columns identify a row, so "
    "there is nothing to merge on"
)
KEY_NONE_NEEDS_KIND = (
    "`key: none` is for a fact or a wide table whose rows no columns identify; declare "
    "`kind: fact` or `kind: wide`, or remove `key: none`"
)
_NEEDS_GRAIN = (
    "Asset `{table}` is {article} and needs `grain:`: say in one sentence what one row is."
)
_NEEDS_KEY = (
    "Asset `{table}` is {article} and needs `key:`: list the columns that identify one row "
    "(usually the grain's columns), or write `key: none` and say in `grain` why no columns are "
    "unique."
)
_DIMENSION_KEY = (
    "Asset `{table}` is a dimension and needs `key:`; `key: none` is not allowed for a "
    "dimension, because nothing could refer to it."
)
_ARTICLE = {"fact": "a fact", "dimension": "a dimension", "wide": "a wide table"}
_FAMILY_UNUSED = (
    "`checks: {family}` sets how the {what} checks fail, but the asset declares no `{field}:`; "
    "declare `{field}:`, or remove `checks: {family}`"
)
EMPTY_KEY_COLUMN = "a key column must not be empty; write the name of a column of the asset"
KEY_COLUMN_MESSAGE = f"a key column {IDENTIFIER_MESSAGE}"
WATERMARK_MESSAGE = f"watermark {IDENTIFIER_MESSAGE}"


@dataclass(frozen=True)
class ResolvedReference:
    """One declared reference, with its target resolved.

    Parameters
    ----------
    column
        The referencing column of the asset.
    target
        The dotted name of the table it points at.
    target_column
        The column of the target it must match: the named one (long form), or the target's
        single key column (short form).
    """

    column: str
    target: str
    target_column: str


def key_columns(key: Sequence[str] | str) -> tuple[str, ...]:
    """Read a declared key as its columns.

    Parameters
    ----------
    key
        A tuple of columns, or `KEY_NONE`.

    Returns
    -------
    tuple of str
        The columns; empty for `KEY_NONE` and for no key.
    """
    return () if isinstance(key, str) else tuple(key)


def model_issues(metadata: AssetMetadata, *, table: str) -> list[str]:
    """Check an asset's model fields against the rules of its kind.

    Parameters
    ----------
    metadata
        The asset's merged metadata.
    table
        Its dotted table name, for the messages.

    Returns
    -------
    list of str
        The spec's fixed texts for a missing grain or key, a dimension's `key: none`, a
        `key: none` without a kind that allows it, a `checks:` family the asset has nothing
        for, and `measure_issues`' texts; empty when the fields agree.
    """
    kind, key = metadata.kind, metadata.key
    issues: list[str] = []
    if kind in _ARTICLE and not metadata.grain:
        issues.append(_NEEDS_GRAIN.format(table=table, article=_ARTICLE[kind]))
    if kind == "dimension" and key == KEY_NONE:
        issues.append(_DIMENSION_KEY.format(table=table))
    elif kind in _ARTICLE and not key:
        issues.append(_NEEDS_KEY.format(table=table, article=_ARTICLE[kind]))
    elif key == KEY_NONE and kind not in ("fact", "wide"):
        issues.append(KEY_NONE_NEEDS_KIND)
    if "key" in metadata.checks and not key_columns(key):
        issues.append(_FAMILY_UNUSED.format(family="key", what="key", field="key"))
    if "references" in metadata.checks and not metadata.references:
        issues.append(
            _FAMILY_UNUSED.format(family="references", what="reference", field="references")
        )
    if "measures" in metadata.checks and not metadata.measures:
        issues.append(_FAMILY_UNUSED.format(family="measures", what="measure", field="measures"))
    issues.extend(measure_issues(kind, key, metadata.measures, table=table))
    return issues


def write_choice_message(write: str) -> str:
    """Say that a `write` value is not one of `WRITE_CHOICES`.

    Parameters
    ----------
    write
        The value the file gave, already made safe to show.

    Returns
    -------
    str
        The message, naming the choices and the value.
    """
    return f"write must be one of {', '.join(WRITE_CHOICES)}; got '{write}'"


def key_issues(key: Sequence[str] | str) -> list[str]:
    """Check the columns of a key on their own.

    Parameters
    ----------
    key
        The key columns as given, or `KEY_NONE`.

    Returns
    -------
    list of str
        One message for a column with an unsafe character (the first), one for an empty column,
        one per column listed twice, and one for more than `MAX_KEY_COLUMNS` columns, a merge
        key's included (its generated checks partition by every column); empty when the key is
        fine.
    """
    if key == KEY_NONE:
        return []
    issues = [KEY_COLUMN_MESSAGE] if any(unsafe_identifier(column) for column in key) else []
    if any(not column for column in key):
        issues.append(EMPTY_KEY_COLUMN)
    seen: set[str] = set()
    for column in key:
        if column in seen and column and not unsafe_identifier(column):
            issues.append(f"key column `{column}` is listed twice")
        seen.add(column)
    if len(key) > MAX_KEY_COLUMNS:
        issues.append(
            f"a key has at most {MAX_KEY_COLUMNS} columns, and `key:` lists {len(key)}; shorten "
            "it to the columns that identify one row"
        )
    return issues


def write_rule_issues(
    write: str | None,
    key: Sequence[str] | str,
    watermark: str | None,
    *,
    materialize: str | None,
    evolve_schema: bool | None,
) -> list[str]:
    """Check an asset's write model against itself, for a SQL block and a decorator alike.

    The values themselves (a `write` outside `WRITE_CHOICES`, an unsafe or repeated column) are
    checked where they are read; this checks how they go together.

    Parameters
    ----------
    write
        ``replace``, ``append``, ``merge``, or `None` when the asset does not say.
    key
        The key columns, `KEY_NONE`, or empty when not given.
    watermark
        The watermark column, or `None`.
    materialize
        The asset's merged `materialize`.
    evolve_schema
        The asset's `evolve_schema`, or `None` when not given.

    Returns
    -------
    list of str
        The messages; empty when the model is consistent.
    """
    key_none = key == KEY_NONE
    columns = key_columns(key)
    if materialize == "view":
        issues = [VIEW_TAKES_NO_WRITE] if write is not None else []
        return [*issues, EVOLVE_NEEDS_APPEND] if evolve_schema is not None else issues
    if materialize == "incremental":
        return [USE_ONE_SPELLING] if write is not None or watermark is not None else []
    issues: list[str] = []
    mode = write or "replace"
    if mode == "append" and watermark is None:
        issues.append(APPEND_NEEDS_WATERMARK)
    if mode == "merge" and key_none:
        issues.append(MERGE_KEY_NONE)
    elif mode == "merge" and not columns:
        issues.append(MERGE_NEEDS_KEY)
    if mode == "replace" and watermark is not None:
        issues.append(REPLACE_TAKES_NO_WATERMARK)
    if evolve_schema is not None and mode != "append":
        issues.append(EVOLVE_NEEDS_APPEND)
    return issues


def normalized_write(
    metadata: AssetMetadata,
) -> tuple[str, tuple[str, ...], str | None, str | None]:
    """Read an asset's write model in the one spelling the manifest records.

    Parameters
    ----------
    metadata
        The asset's merged, consistent metadata.

    Returns
    -------
    tuple
        ``(write, key, watermark, hint)``. ``materialize: incremental`` with
        ``incremental_key: k`` is ``("append", key, k, <hint>)``, the hint saying how it is now
        written; a `write` given is returned as written with no hint; neither is
        ``("replace", key, None, None)``. `key` is the declared key's columns in every case.
    """
    if metadata.write is not None:
        return metadata.write, key_columns(metadata.key), metadata.watermark, None
    if metadata.materialize == "incremental":
        column = metadata.incremental_key
        hint = (
            f"`materialize: incremental` is now written `write: append` with "
            f"`watermark: {column}`; both work"
        )
        return "append", key_columns(metadata.key), column, hint
    return "replace", key_columns(metadata.key), None, None


def deploy_hints(assets: Sequence[ParsedAsset]) -> tuple[str, ...]:
    """Collect the hints a deploy prints about how its assets are written.

    Parameters
    ----------
    assets
        The parsed assets.

    Returns
    -------
    tuple of str
        ``<table>: <hint>`` for each asset that uses an older spelling, sorted by table.
    """
    hints = []
    for asset in sorted(assets, key=lambda asset: asset.table):
        hint = normalized_write(asset.metadata)[3]
        if hint is not None:
            hints.append(f"{asset.table}: {hint}")
    return tuple(hints)


@dataclass(frozen=True)
class ParsedAsset:
    """One asset, parsed and ready for DAG validation.

    Parameters
    ----------
    table
        Dotted table name.
    path
        Repository-relative path.
    block_line
        Line of the front-matter block (SQL) or of the decorator (Python), or `None`.
    body_line
        Line on which the SQL body starts, or of the `def` (Python).
    metadata
        Front-matter or decorator arguments merged over `lakana.yaml` defaults.
    sql
        The SQL body, stripped; empty for a Python asset.
    normalized_sql
        `sql` regenerated by sqlglot without comments, for the diff; for a
        Python asset ``python:sha256:<digest of the file>``.
    references
        Every distinct table the asset reads, sorted.
    columns
        Output column names of an explicit projection (``""`` for an
        unnamed expression), or `None` when unknown (a star, or Python).
    lineage
        Column lineage, `extract_lineage`'s shape; empty for a Python asset.
    kind
        ``sql`` or ``python``.
    inputs
        A Python asset's ``(parameter, table)`` pairs in parameter order.
    output
        ``table`` or, for a Python asset declared with ``output="artifact"``, ``artifact``.
    keep_versions
        An artifact asset's retention (live versions to keep); `None` keeps all.
    decimals
        ``float`` or ``exact``: how a SQL decimal reaches a Python asset's function.
    connections
        The connections of the environment a Python asset declares, in the order given.
    """

    table: str
    path: str
    block_line: int | None
    body_line: int
    metadata: AssetMetadata
    sql: str
    normalized_sql: str
    references: tuple[str, ...]
    columns: tuple[str, ...] | None
    lineage: dict[str, list[dict[str, str]]]
    kind: str = "sql"
    inputs: tuple[tuple[str, str], ...] = ()
    output: str = "table"
    keep_versions: int | None = None
    decimals: str = DEFAULT_DECIMALS
    connections: tuple[str, ...] = ()
