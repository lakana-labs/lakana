"""The parsed form of one asset, SQL or Python: what DAG validation and the manifest read."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

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

WRITE_CHOICES = ("replace", "append", "merge")
"""What a run does to an asset's table: replace every row, append the rows past the watermark, or
merge the rows on a key. `lakana.assets.WRITE_CHOICES` is the same (a parity test pins both)."""

APPEND_NEEDS_WATERMARK = (
    "`write: append` needs `watermark:` — the column whose largest value the next run starts after"
)
MERGE_NEEDS_KEY = "`write: merge` needs `key:` — the columns that identify a row"
APPEND_TAKES_NO_KEY = "`write: append` takes no `key:`; to merge rows on a key use `write: merge`"
REPLACE_TAKES_NEITHER = "`write: replace` takes neither `key:` nor `watermark:`"
VIEW_TAKES_NO_WRITE = "`materialize: view` cannot take `write:`"
USE_ONE_SPELLING = "use one: `write: append` with `watermark:` replaces `materialize: incremental`"
EVOLVE_NEEDS_APPEND = "`evolve_schema` works with `write: append` only"
KEY_COLUMN_MESSAGE = f"a key column {IDENTIFIER_MESSAGE}"
WATERMARK_MESSAGE = f"watermark {IDENTIFIER_MESSAGE}"


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


def key_issues(key: Sequence[str]) -> list[str]:
    """Check the columns of a merge key on their own.

    Parameters
    ----------
    key
        The key columns as given.

    Returns
    -------
    list of str
        One message for a column with an unsafe character (the first), and one per column
        listed twice; empty when the key is fine.
    """
    issues = [KEY_COLUMN_MESSAGE] if any(unsafe_identifier(column) for column in key) else []
    seen: set[str] = set()
    for column in key:
        if column in seen and not unsafe_identifier(column):
            issues.append(f"key column `{column}` is listed twice")
        seen.add(column)
    return issues


def write_rule_issues(
    write: str | None,
    key: Sequence[str],
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
        The merge key; empty when not given.
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
    if materialize == "view":
        issues = [VIEW_TAKES_NO_WRITE] if write is not None else []
        return [*issues, EVOLVE_NEEDS_APPEND] if evolve_schema is not None else issues
    if materialize == "incremental":
        return [USE_ONE_SPELLING] if write is not None or key or watermark is not None else []
    issues: list[str] = []
    mode = write or "replace"
    if mode == "append" and watermark is None:
        issues.append(APPEND_NEEDS_WATERMARK)
    if mode == "append" and key:
        issues.append(APPEND_TAKES_NO_KEY)
    if mode == "merge" and not key:
        issues.append(MERGE_NEEDS_KEY)
    if mode == "replace" and (key or watermark is not None):
        issues.append(REPLACE_TAKES_NEITHER)
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
        ``incremental_key: k`` is ``("append", (), k, <hint>)``, the hint saying how it is now
        written; a `write` given is returned as written with no hint; neither is
        ``("replace", (), None, None)``.
    """
    if metadata.write is not None:
        return metadata.write, tuple(metadata.key), metadata.watermark, None
    if metadata.materialize == "incremental":
        column = metadata.incremental_key
        hint = (
            f"`materialize: incremental` is now written `write: append` with "
            f"`watermark: {column}`; both work"
        )
        return "append", (), column, hint
    return "replace", (), None, None


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
