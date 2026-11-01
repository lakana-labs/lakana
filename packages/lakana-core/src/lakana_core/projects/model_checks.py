"""The checks an asset's header generates: ``key_*``, ``ref_<column>`` and ``measures``.

``key_unique`` and ``key_not_null`` come from a key, ``ref_<column>`` from each reference, and
``measures`` from `measures:`; a history dimension's ``key_unique`` checks versions, and
``surrogate_key_unique`` comes from `write: history`.

A deploy turns each declared key and each resolved reference into an ordinary `ParsedCheck`, so the
generated checks run, count, block and show exactly like hand-written ones. The SQL quotes every
identifier, so a column named like a keyword, or with a space or a quote in it, still runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from lakana_core.identifiers import shown
from lakana_core.projects.asset_model import ParsedAsset, ResolvedReference, key_columns
from lakana_core.projects.checks import GeneratedFrom, ParsedCheck
from lakana_core.projects.discovery import _fold
from lakana_core.projects.history_columns import IS_CURRENT, VALID_FROM, VALID_TO, surrogate_column
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.measures import MEASURES_CHECK

KEY_UNIQUE = "key_unique"
"""The check that returns the rows whose key appears more than once."""

SURROGATE_KEY_UNIQUE = "surrogate_key_unique"
"""The check that returns the versions of a history dimension whose surrogate key repeats."""

KEY_NOT_NULL = "key_not_null"
"""The check that returns the rows with a null in any key column."""

REF_PREFIX = "ref_"
"""The prefix of a reference check: ``ref_<column>``."""

GENERATED_MARK = " (generated)"
"""What the CLI shows after a generated check's name; no hand-written check's name may end in it."""


def _quote(identifier: str) -> str:
    """Quote one identifier for DuckDB.

    Parameters
    ----------
    identifier
        A table part or a column name.

    Returns
    -------
    str
        The identifier in double quotes, inner quotes doubled.
    """
    return '"' + identifier.replace('"', '""') + '"'


def _table(dotted: str) -> str:
    """Quote a dotted table name part by part.

    Parameters
    ----------
    dotted
        ``ns.table`` or a deeper namespace.

    Returns
    -------
    str
        The quoted name.
    """
    return ".".join(_quote(part) for part in dotted.split("."))


def key_unique_sql(table: str, key: Sequence[str]) -> str:
    """Select the rows whose key appears more than once.

    Parameters
    ----------
    table
        The checked table.
    key
        Its key columns.

    Returns
    -------
    str
        One SELECT.
    """
    columns = ", ".join(_quote(c) for c in key)
    return f"SELECT * FROM {_table(table)} QUALIFY count(*) OVER (PARTITION BY {columns}) > 1"  # nosec B608 -- identifiers quoted


def key_not_null_sql(table: str, key: Sequence[str]) -> str:
    """Select the rows with a null in any key column.

    Parameters
    ----------
    table
        The checked table.
    key
        Its key columns.

    Returns
    -------
    str
        One SELECT.
    """
    nulls = " OR ".join(f"{_quote(c)} IS NULL" for c in key)
    return f"SELECT * FROM {_table(table)} WHERE {nulls}"  # nosec B608 -- identifiers quoted


def reference_sql(table: str, column: str, target: str, target_column: str) -> str:
    """Select the rows whose non-null value is missing from the target's column.

    Parameters
    ----------
    table
        The checked table.
    column
        Its referencing column.
    target
        The referenced table.
    target_column
        The referenced column.

    Returns
    -------
    str
        One SELECT: a ``LEFT JOIN … WHERE target IS NULL``.
    """
    own, other = _quote(column), _quote(target_column)
    source = f"SELECT s.* FROM {_table(table)} AS s"  # nosec B608 -- identifiers quoted
    join = f" LEFT JOIN {_table(target)} AS t ON s.{own} = t.{other}"
    return f"{source}{join} WHERE s.{own} IS NOT NULL AND t.{other} IS NULL"


def valid_at_reference_sql(
    table: str, column: str, target: str, target_column: str, at: str
) -> str:
    """Select the rows whose key has no version of the target valid at the row's time.

    The target keeps history: a version is valid at a time `t` when ``valid_from <= t`` and
    ``valid_to`` is NULL or later than `t` (half-open). A row with a key and a NULL time fails;
    a date reads as midnight and a timestamp as UTC, because a check runs with `TimeZone` UTC.

    Parameters
    ----------
    table
        The checked table.
    column
        Its referencing column.
    target
        The referenced history table.
    target_column
        The referenced column: the natural key, or the surrogate key.
    at
        The checked table's time column.

    Returns
    -------
    str
        One SELECT: a ``LEFT JOIN … WHERE target IS NULL``.
    """
    own, other = _quote(column), _quote(target_column)
    start, end = _quote(VALID_FROM), _quote(VALID_TO)
    moment = f"CAST(s.{_quote(at)} AS TIMESTAMPTZ)"
    source = f"SELECT s.* FROM {_table(table)} AS s"  # nosec B608 -- identifiers quoted
    join = (
        f" LEFT JOIN {_table(target)} AS t ON s.{own} = t.{other} AND t.{start} <= {moment}"
        f" AND (t.{end} IS NULL OR {moment} < t.{end})"
    )
    return f"{source}{join} WHERE s.{own} IS NOT NULL AND t.{other} IS NULL"


def measures_sql(table: str) -> str:
    """Select no rows of a table: the `measures` check exists for its pre-check.

    Parameters
    ----------
    table
        The checked table.

    Returns
    -------
    str
        One SELECT that returns nothing; the worker's pre-check decides the outcome.
    """
    return f"SELECT * FROM {_table(table)} WHERE false"  # nosec B608 -- identifiers quoted


def _generated(
    asset: ParsedAsset,
    check: str,
    sql: str,
    source: GeneratedFrom,
    description: str,
    default_on_failure: str,
) -> ParsedCheck:
    """Build one generated check of an asset.

    Parameters
    ----------
    asset
        The asset whose header generates it.
    check
        The check's name.
    sql
        Its SELECT.
    source
        The header field it comes from.
    description
        Its description.
    default_on_failure
        The project's default, for a family the header sets no mode for.

    Returns
    -------
    ParsedCheck
        The check. Its `path` is the asset's file and the header's line, ``file.sql:<line>``.
    """
    line = asset.block_line or asset.body_line
    family = source.family
    tables = {asset.table} if source.target is None else {asset.table, source.target}
    return ParsedCheck(
        name=f"{asset.table}/{check}",
        table=asset.table,
        check=check,
        path=f"{asset.path}:{line}",
        on_failure=asset.metadata.checks.get(family, default_on_failure),
        description=description,
        sql=sql,
        references=tuple(sorted(tables)),
        body_line=line,
        from_default=family not in asset.metadata.checks and default_on_failure != "block",
        block_line=line,
        generated=source,
    )


def history_key_unique_sql(table: str, key: Sequence[str]) -> str:
    """Select the versions that break "one version per key at any time".

    A version is returned when the next version of its key starts before it ends (two open
    versions, or an overlap), when it ends at or before it starts, or when `is_current`
    disagrees with `valid_to`.

    Parameters
    ----------
    table
        The history table.
    key
        Its natural key's columns.

    Returns
    -------
    str
        One SELECT.
    """
    columns = ", ".join(_quote(c) for c in key)
    start, end, current = _quote(VALID_FROM), _quote(VALID_TO), _quote(IS_CURRENT)
    return (
        f"SELECT * FROM {_table(table)} QUALIFY lead({start}) OVER (PARTITION BY {columns} "  # nosec B608 -- identifiers quoted
        f"ORDER BY {start}) < coalesce({end}, 'infinity'::TIMESTAMPTZ) OR {end} <= {start} "
        f"OR {current} IS DISTINCT FROM ({end} IS NULL)"
    )


def surrogate_key_unique_sql(table: str) -> str:
    """Select the versions of a history table whose surrogate key appears more than once.

    Parameters
    ----------
    table
        The history table.

    Returns
    -------
    str
        One SELECT.
    """
    return key_unique_sql(table, [surrogate_column(table)])


def generated_checks(
    assets: Sequence[ParsedAsset],
    references: Mapping[str, Sequence[ResolvedReference]],
    *,
    default_on_failure: str,
) -> list[ParsedCheck]:
    """Generate the checks every asset's header declares.

    Parameters
    ----------
    assets
        The parsed assets.
    references
        Table → its resolved references (`resolve_model_references`); a reference that did not
        resolve generates nothing, and the deploy fails on its issue anyway.
    default_on_failure
        The project's `checks.on_failure`.

    Returns
    -------
    list of ParsedCheck
        ``key_not_null`` and ``key_unique`` for a declared key (none for ``key: none``; a history
        dimension's ``key_unique`` checks versions, and it has ``surrogate_key_unique`` too),
        ``ref_<column>`` per resolved reference, and one ``measures`` for an asset that
        declares measures; sorted by name.
    """
    checks: list[ParsedCheck] = []
    for asset in sorted(assets, key=lambda a: a.table):
        columns = key_columns(asset.metadata.key)
        if columns:
            history = asset.metadata.write == "history"
            source = GeneratedFrom("key", columns, history=history)
            keyed: list[tuple[str, str, str]] = [
                (KEY_NOT_NULL, key_not_null_sql(asset.table, columns), "generated from `key:`"),
                (
                    (
                        KEY_UNIQUE,
                        history_key_unique_sql(asset.table, columns),
                        "generated from `key:` (one version per key at any time)",
                    )
                    if history
                    else (KEY_UNIQUE, key_unique_sql(asset.table, columns), "generated from `key:`")
                ),
            ]
            if history:
                keyed.append(
                    (
                        SURROGATE_KEY_UNIQUE,
                        surrogate_key_unique_sql(asset.table),
                        "generated from `write: history` (one row per version key)",
                    )
                )
            for check, sql, description in keyed:
                checks.append(
                    _generated(asset, check, sql, source, description, default_on_failure)
                )
        for ref in references.get(asset.table, ()):
            sql = (
                reference_sql(asset.table, ref.column, ref.target, ref.target_column)
                if ref.at is None
                else valid_at_reference_sql(
                    asset.table, ref.column, ref.target, ref.target_column, ref.at
                )
            )
            checks.append(
                _generated(
                    asset,
                    f"{REF_PREFIX}{ref.column}",
                    sql,
                    GeneratedFrom(
                        "references", (ref.column,), ref.target, ref.target_column, at=ref.at
                    ),
                    f"generated from `references: {ref.column}`",
                    default_on_failure,
                )
            )
        measures = asset.metadata.measures
        if measures:
            ordered = sorted(measures)
            source = GeneratedFrom(
                "measures",
                tuple(ordered),
                not_over=tuple(sorted({c for spec in measures.values() for c in spec.not_over})),
                aggregations=tuple(str(measures[column].aggregation) for column in ordered),
            )
            checks.append(
                _generated(
                    asset,
                    MEASURES_CHECK,
                    measures_sql(asset.table),
                    source,
                    "generated from `measures:`",
                    default_on_failure,
                )
            )
    return sorted(checks, key=lambda c: c.name)


def generated_name_issues(
    hand: Sequence[ParsedCheck], generated: Sequence[ParsedCheck]
) -> list[ProjectIssue]:
    """Refuse a hand-written check that takes a generated name or looks generated.

    A hand-written check named ``key_unique`` or ``ref_x`` is fine as long as the header of its
    table generates no check of that name. Names are compared as discovery compares two check
    files (`_fold`), so ``Key_Unique.sql`` collides with a generated ``key_unique`` too. A name
    ending in `GENERATED_MARK`, in any case, is refused everywhere: the CLI would show it as a
    generated check.

    Parameters
    ----------
    hand
        The hand-written checks.
    generated
        The generated checks.

    Returns
    -------
    list of ProjectIssue
        One per refused hand-written check, at its file, in path order.
    """
    names = {_fold(check.name) for check in generated}
    mark = _fold(GENERATED_MARK)
    issues: list[ProjectIssue] = []
    for check in sorted(hand, key=lambda c: c.path):
        if _fold(check.check).endswith(mark):
            issues.append(
                ProjectIssue(
                    check.path,
                    None,
                    f"A check name may not end in `{GENERATED_MARK}`, which marks a check "
                    f"generated from an asset's header; rename `{check.path}`.",
                )
            )
        elif _fold(check.name) in names:
            issues.append(
                ProjectIssue(
                    check.path,
                    None,
                    f"A check named `{shown(check.check)}` is generated for "
                    f"`{shown(check.table)}`; rename `{check.path}`.",
                )
            )
    return issues
