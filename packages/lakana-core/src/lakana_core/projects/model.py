"""Resolve an asset's reference targets, and check its key and reference columns.

Targets are resolved against the project and, through a `lookup` the caller hands in, against
the environment; this module imports nothing from `deploys`. Key and reference columns are
checked against an explicit SELECT; a star or a Python asset leaves them to the run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from lakana_core.projects.asset_model import ParsedAsset, ResolvedReference, key_columns
from lakana_core.projects.issues import ProjectIssue

_MODELLED = {"fact": "a fact", "dimension": "a dimension", "wide": "a wide table"}
"""The kinds that may not reference a staging table, with the article their message uses."""


@dataclass(frozen=True)
class TargetInfo:
    """What a deploy knows about a table a reference may point at.

    Parameters
    ----------
    kind
        The target's declared `kind`, or `None`.
    key
        Its declared key columns; empty without a key and for `key: none`.
    key_none
        Whether it declares `key: none`.
    columns
        Its columns, or `None` when they are not known at deploy.
    artifact
        Whether it is an artifact: a versioned file with no rows, which nothing can point at.
    """

    kind: str | None
    key: tuple[str, ...]
    key_none: bool
    columns: tuple[str, ...] | None
    artifact: bool = False


class AmbiguousReference(Exception):
    """A target string that names a table and also a column of a shorter table name.

    Parameters
    ----------
    prefix
        The shorter table name.
    column
        The last part, read as a column of `prefix`.
    """

    def __init__(self, prefix: str, column: str) -> None:
        """Keep the two readings.

        Parameters
        ----------
        prefix
            The shorter table name.
        column
            The column of it.
        """
        super().__init__(prefix, column)
        self.prefix = prefix
        self.column = column


def split_reference(raw: str, is_table: Callable[[str], bool]) -> tuple[str, str | None] | None:
    """Read a reference target as a table (short form) or a table and a column (long form).

    Parameters
    ----------
    raw
        The target as written.
    is_table
        Whether a dotted name is a table the reference may point at.

    Returns
    -------
    tuple or None
        ``(table, None)`` or ``(table, column)``; `None` when neither reading names a table.

    Raises
    ------
    AmbiguousReference
        When both readings name a table.
    """
    prefix, _, last = raw.rpartition(".")
    whole = is_table(raw)
    long = "." in prefix and is_table(prefix)
    if whole and long:
        raise AmbiguousReference(prefix, last)
    if whole:
        return raw, None
    return (prefix, last) if long else None


def project_reference_target(
    raw: str, produced: Mapping[str, tuple[str, ...] | None]
) -> str | None:
    """Find the project table a reference target names, for the DAG.

    It reads the name as `_split` does where the project alone decides: a table part whose known
    columns lack the last part is not the target, because the deploy then reads the whole name as
    a table outside the project, or refuses the reference.

    Parameters
    ----------
    raw
        The target as written.
    produced
        The tables the project builds, each with its columns (`None` when not known).

    Returns
    -------
    str or None
        `raw` when the project builds it; else its table part (long form) when the project
        builds that and it may have the column; else `None`.
    """
    if raw in produced:
        return raw
    prefix, _, column = raw.rpartition(".")
    if prefix not in produced:
        return None
    known = produced[prefix]
    return prefix if known is None or column in known else None


def _missing(raw: str, environment: str) -> str:
    """Say that a target is missing or unreadable, without saying which.

    Parameters
    ----------
    raw
        The target as written.
    environment
        The environment's name.

    Returns
    -------
    str
        The combined text.
    """
    return (
        f"Reference target `{raw}` does not exist in environment `{environment}`, or you may "
        "not read it; build it first, or ask for READ_TABLE on it."
    )


def _split(
    column: str,
    raw: str,
    lookup: Callable[[str], TargetInfo | None],
    is_table: Callable[[str], bool],
    environment: str,
) -> tuple[str, str | None] | str:
    """Read one reference's target, settling a name that reads two ways when it can.

    Parameters
    ----------
    column
        The referencing column.
    raw
        The target as written.
    lookup
        Finds a target's info.
    is_table
        Whether a dotted name is a table the reference may point at.
    environment
        The environment's name.

    Returns
    -------
    tuple or str
        ``(table, column or None)``, or the message of the problem.
    """
    try:
        split = split_reference(raw, is_table)
    except AmbiguousReference as exc:
        shorter = lookup(exc.prefix)
        known = shorter.columns if shorter is not None else None
        if known is not None and exc.column not in known:
            return raw, None
        reading = (
            f"which has a column `{exc.column}`"
            if known is not None
            else f"which would read `{exc.column}` as its column"
        )
        return (
            f"Reference `{column}: {raw}`: `{raw}` is a table, and so is `{exc.prefix}`, "
            f"{reading}; rename one of them so that the reference names one thing."
        )
    return split if split is not None else _missing(raw, environment)


def _resolve_one(
    asset: ParsedAsset,
    column: str,
    raw: str,
    lookup: Callable[[str], TargetInfo | None],
    is_table: Callable[[str], bool],
    environment: str,
) -> tuple[ResolvedReference | None, list[str]]:
    """Resolve one reference of an asset.

    Parameters
    ----------
    asset
        The referencing asset.
    column
        The referencing column.
    raw
        The target as written.
    lookup
        Finds a target's info.
    is_table
        Whether a dotted name is a table the reference may point at.
    environment
        The environment's name.

    Returns
    -------
    tuple
        The resolved reference (`None` when it has a problem) and the messages of its problems.
    """
    split = _split(column, raw, lookup, is_table, environment)
    if isinstance(split, str):
        return None, [split]
    target, target_column = split
    info = lookup(target)
    if info is None:
        return None, [_missing(raw, environment)]
    if info.artifact:
        # The lookup describes only a project asset or a table the deployer may read, so naming
        # the artifact reveals nothing.
        return None, [
            f"Reference `{column}: {raw}`: `{target}` is an artifact, not a table, so no column "
            "can point at it; point the reference at a table."
        ]
    messages: list[str] = []
    kind = asset.metadata.kind
    if kind in _MODELLED and info.kind == "staging":
        messages.append(
            f"`{asset.table}` ({_MODELLED[kind]}) references `{target}`, a staging table; "
            "point it at a modelled table."
        )
    where = f"Reference `{column}: {raw}`: `{target}`"
    if target_column is None:
        if info.key_none or not info.key:
            # `<column>`, never one of the target's columns: the deploy's error reaches every
            # reader of the environment, not only those who may read the target.
            messages.append(
                f"{where} declares no key. Declare `key:` in its asset, or name the column: "
                f"`{column}: {target}.<column>`."
            )
            return None, messages
        if len(info.key) > 1:
            messages.append(
                f"{where} has a key of {len(info.key)} columns; name the column: "
                f"`{column}: {target}.<column>`."
            )
            return None, messages
        target_column = info.key[0]
    elif info.columns is not None and target_column not in info.columns:
        messages.append(
            f"{where} has no column `{target_column}`; name one of its columns, or use the short "
            "form to point at its key."
        )
    if messages:
        return None, messages
    return ResolvedReference(column, target, target_column), []


def resolve_references(
    assets: Sequence[ParsedAsset],
    lookup: Callable[[str], TargetInfo | None],
    *,
    environment: str,
    is_table: Callable[[str], bool] | None = None,
) -> tuple[dict[str, tuple[ResolvedReference, ...]], list[ProjectIssue]]:
    """Resolve every asset's references against what `lookup` knows.

    Parameters
    ----------
    assets
        The parsed assets.
    lookup
        Finds the info of a table a reference may point at; `None` for a name that is no such
        table, or one the deployer may not read.
    environment
        The environment's name, for the messages.
    is_table
        Whether a dotted name is a table a reference may point at; by default, whether `lookup`
        finds it. The deploy passes the test its `lookup` applies.

    Returns
    -------
    tuple
        Table → its resolved references ordered by column (only tables that have one), and one
        issue per problem, at the asset's block, in path and column order.
    """
    test = is_table if is_table is not None else (lambda name: lookup(name) is not None)
    resolved: dict[str, tuple[ResolvedReference, ...]] = {}
    issues: list[ProjectIssue] = []
    for asset in sorted(assets, key=lambda a: a.path):
        line = asset.block_line or asset.body_line
        found: list[ResolvedReference] = []
        for column, raw in sorted(asset.metadata.references.items()):
            reference, messages = _resolve_one(asset, column, raw, lookup, test, environment)
            issues.extend(ProjectIssue(asset.path, line, message) for message in messages)
            if reference is not None:
                found.append(reference)
        if found:
            resolved[asset.table] = tuple(found)
    return resolved, issues


def project_model_issues(assets: Sequence[ParsedAsset]) -> list[ProjectIssue]:
    """Check that key and reference columns are columns of an explicit SELECT.

    Parameters
    ----------
    assets
        The parsed assets.

    Returns
    -------
    list of ProjectIssue
        One per key column, then per reference column, that the asset's explicit SELECT does
        not return, in path order; nothing for a star or a Python asset, whose columns are
        checked at the run.
    """
    issues: list[ProjectIssue] = []
    for asset in sorted(assets, key=lambda a: a.path):
        if asset.columns is None:
            continue
        line = asset.block_line or asset.body_line
        for column in key_columns(asset.metadata.key):
            if column not in asset.columns:
                issues.append(
                    ProjectIssue(
                        asset.path,
                        line,
                        f"Key column `{column}` is not a column of `{asset.table}`'s SELECT; "
                        "add it to the SELECT, or change `key:`.",
                    )
                )
        for column in sorted(asset.metadata.references):
            if column not in asset.columns:
                issues.append(
                    ProjectIssue(
                        asset.path,
                        line,
                        f"Reference column `{column}` is not a column of `{asset.table}`'s "
                        "SELECT; add it to the SELECT, or remove the reference.",
                    )
                )
    return issues
