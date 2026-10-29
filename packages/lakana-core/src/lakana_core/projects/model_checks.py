"""The checks an asset's header generates: ``key_unique``, ``key_not_null`` and ``ref_<column>``.

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
from lakana_core.projects.issues import ProjectIssue

KEY_UNIQUE = "key_unique"
"""The check that returns the rows whose key appears more than once."""

KEY_NOT_NULL = "key_not_null"
"""The check that returns the rows with a null in any key column."""

REF_PREFIX = "ref_"
"""The prefix of a reference check: ``ref_<column>``."""


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
        ``key_not_null`` and ``key_unique`` for a declared key (none for ``key: none``), and
        ``ref_<column>`` per resolved reference; sorted by name.
    """
    checks: list[ParsedCheck] = []
    for asset in sorted(assets, key=lambda a: a.table):
        columns = key_columns(asset.metadata.key)
        if columns:
            source = GeneratedFrom("key", columns)
            for check, sql in (
                (KEY_NOT_NULL, key_not_null_sql(asset.table, columns)),
                (KEY_UNIQUE, key_unique_sql(asset.table, columns)),
            ):
                checks.append(
                    _generated(
                        asset, check, sql, source, "generated from `key:`", default_on_failure
                    )
                )
        for ref in references.get(asset.table, ()):
            checks.append(
                _generated(
                    asset,
                    f"{REF_PREFIX}{ref.column}",
                    reference_sql(asset.table, ref.column, ref.target, ref.target_column),
                    GeneratedFrom("references", (ref.column,), ref.target, ref.target_column),
                    f"generated from `references: {ref.column}`",
                    default_on_failure,
                )
            )
    return sorted(checks, key=lambda c: c.name)


def generated_name_issues(
    hand: Sequence[ParsedCheck], generated: Sequence[ParsedCheck]
) -> list[ProjectIssue]:
    """Refuse a hand-written check whose name its own table's header generates.

    A hand-written check named ``key_unique`` or ``ref_x`` is fine as long as the header of its
    table generates no check of that name. Names are compared as discovery compares two check
    files (`_fold`), so ``Key_Unique.sql`` collides with a generated ``key_unique`` too.

    Parameters
    ----------
    hand
        The hand-written checks.
    generated
        The generated checks.

    Returns
    -------
    list of ProjectIssue
        One per colliding hand-written check, at its file, in path order.
    """
    names = {_fold(check.name) for check in generated}
    return [
        ProjectIssue(
            check.path,
            None,
            f"A check named `{shown(check.check)}` is generated for `{shown(check.table)}`; "
            f"rename `{check.path}`.",
        )
        for check in sorted(hand, key=lambda c: c.path)
        if _fold(check.name) in names
    ]
