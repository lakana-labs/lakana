"""Resolve an asset's reference targets, and check its key and reference columns.

Targets are resolved against the project and, through a `lookup` the caller hands in, against
the environment; this module imports nothing from `deploys`. Key and reference columns are
checked against an explicit SELECT; a star or a Python asset leaves them to the run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from lakana_core.projects.asset_model import ParsedAsset, ResolvedReference, key_columns
from lakana_core.projects.history_columns import (
    added_columns,
    reserved_name_message,
    valid_from_column_message,
)
from lakana_core.projects.issues import ProjectIssue

_MODELLED = {"fact": "a fact", "dimension": "a dimension", "wide": "a wide table"}
"""The kinds that may not reference a staging table, with the article their message uses."""

MAX_REFERENCE_BYTES = 1024
"""The most UTF-8 bytes of each part of a stored reference: column, target table, target column.

`table_references` stores each in a column of 1024 characters; counting bytes also keeps the
index on the target's name within Postgres's limit for one index row.
"""


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
    history
        Whether it keeps history (`write: history`): a reference to it must say `at:`.
    """

    kind: str | None
    key: tuple[str, ...]
    key_none: bool
    columns: tuple[str, ...] | None
    artifact: bool = False
    history: bool = False


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


def missing_target_message(raw: str, environment: str) -> str:
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


@dataclass(frozen=True)
class _Told:
    """The message of one problem of a reference, and the tables it tells something of.

    Parameters
    ----------
    message
        The message.
    targets
        The tables it says something of (empty when it says nothing a missing table would
        not).
    """

    message: str
    targets: tuple[str, ...] = ()


def _split(
    column: str,
    raw: str,
    lookup: Callable[[str], TargetInfo | None],
    is_table: Callable[[str], bool],
    environment: str,
) -> tuple[str, str | None] | _Told:
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
    tuple or _Told
        ``(table, column or None)``, or the problem.
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
        return _Told(
            f"Reference `{column}: {raw}`: `{raw}` is a table, and so is `{exc.prefix}`, "
            f"{reading}; rename one of them so that the reference names one thing.",
            (raw, exc.prefix),
        )
    return split if split is not None else _Told(missing_target_message(raw, environment))


def _resolve_one(
    asset: ParsedAsset,
    column: str,
    raw: str,
    lookup: Callable[[str], TargetInfo | None],
    is_table: Callable[[str], bool],
    environment: str,
) -> tuple[ResolvedReference | None, list[_Told]]:
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
        The resolved reference (`None` when it has a problem) and its problems. Every problem
        found once the lookup described the target tells of it.
    """
    split = _split(column, raw, lookup, is_table, environment)
    if isinstance(split, _Told):
        return None, [split]
    target, target_column = split
    info = lookup(target)
    if info is None:
        return None, [_Told(missing_target_message(raw, environment))]
    at = asset.metadata.reference_times().get(column)
    messages = _target_problems(asset, column, raw, target, target_column, info, at)
    if messages:
        return None, [_Told(message, (target,)) for message in messages]
    return ResolvedReference(column, target, target_column or info.key[0], at), []


def _target_problems(
    asset: ParsedAsset,
    column: str,
    raw: str,
    target: str,
    target_column: str | None,
    info: TargetInfo,
    at: str | None,
) -> list[str]:
    """Find what is wrong with a reference to a target the lookup described.

    Parameters
    ----------
    asset
        The referencing asset.
    column
        The referencing column.
    raw
        The target as written.
    target
        The target table.
    target_column
        The column the long form names, or `None` for the short form.
    info
        What the lookup knows of the target.
    at
        The reference's `at:` column, or `None`.

    Returns
    -------
    list of str
        The messages; empty when the reference resolves. For the short form, the target has
        exactly one key column then.
    """
    if info.artifact:
        # The lookup describes only a project asset or a table the deployer may read; other
        # readers of the deploy are shown the missing text instead (`ProjectIssue.withheld`).
        return [
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
    if info.history and at is None:
        messages.append(
            f"`{asset.table}` references `{target}` (`{column}`), which keeps the history of its "
            "rows (`write: history`); say which version each row means with the row's time: "
            f"`{column}: {{to: {raw}, at: <column>}}`"
        )
    if not info.history and at is not None:
        messages.append(
            f"`at:` in reference `{column}` is for a target that keeps history (`write: "
            f"history`), and `{target}` does not; remove `at:`"
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
            return messages
        if len(info.key) > 1:
            messages.append(
                f"{where} has a key of {len(info.key)} columns; name the column: "
                f"`{column}: {target}.<column>`."
            )
            return messages
    elif info.columns is not None and target_column not in info.columns:
        messages.append(
            f"{where} has no column `{target_column}`; name one of its columns, or use the short "
            "form to point at its key."
        )
    if messages:
        return messages
    too_long = _too_long(column, target, target_column or info.key[0])
    if too_long is not None:
        return [
            f"Reference `{column}: {raw}` is too long to store: {too_long}, and each of the "
            f"column, the target table and the target column may have at most {MAX_REFERENCE_BYTES}"
            " bytes; shorten that name."
        ]
    return []


def _too_long(column: str, target: str, target_column: str) -> str | None:
    """Name the first part of a reference that the catalog cannot store.

    Parameters
    ----------
    column
        The referencing column.
    target
        The target table's dotted name.
    target_column
        The target column.

    Returns
    -------
    str or None
        For example ``the column `…` has 1025 bytes``; `None` when every part fits.
    """
    for label, value in (
        ("the column", column),
        ("the target table", target),
        ("the target column", target_column),
    ):
        size = len(value.encode("utf-8"))
        if size > MAX_REFERENCE_BYTES:
            return f"{label} `{value}` has {size} bytes"
    return None


def resolve_references(
    assets: Sequence[ParsedAsset],
    lookup: Callable[[str], TargetInfo | None],
    *,
    environment: str,
    is_table: Callable[[str], bool] | None = None,
    refusal: Callable[[str], str | None] | None = None,
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
    refusal
        Why a resolved reference's target may not be used after all (the bound run principal
        may not read it), or `None` when it may. Its issue takes the reference's place in the
        order, so that a reader shown a missing target's line instead finds it where a missing
        target's would be.

    Returns
    -------
    tuple
        Table → its resolved references ordered by column (only tables that have one), and one
        issue per problem, at the asset's block, in path and column order. An issue that tells
        of a target the lookup described carries it (`ProjectIssue.targets`) and the text of a
        missing target (`ProjectIssue.withheld`); the second and later problems of one
        reference carry an empty text, as a missing target makes one line.
    """
    test = is_table if is_table is not None else (lambda name: lookup(name) is not None)
    resolved: dict[str, tuple[ResolvedReference, ...]] = {}
    issues: list[ProjectIssue] = []
    for asset in sorted(assets, key=lambda a: a.path):
        line = asset.block_line or asset.body_line
        found: list[ResolvedReference] = []
        for column, raw in sorted(asset.metadata.reference_targets().items()):
            reference, problems = _resolve_one(asset, column, raw, lookup, test, environment)
            # A reader who may not read the target is shown what a missing target shows: one
            # line per reference, so the first problem's line becomes it and the others go.
            issues.extend(
                ProjectIssue(
                    asset.path,
                    line,
                    told.message,
                    targets=told.targets,
                    withheld=(
                        (missing_target_message(raw, environment) if n == 0 else "")
                        if told.targets
                        else None
                    ),
                )
                for n, told in enumerate(problems)
            )
            refused = None if reference is None or refusal is None else refusal(reference.target)
            if refused is not None and reference is not None:
                issues.append(
                    ProjectIssue(
                        asset.path,
                        line,
                        refused,
                        targets=(reference.target,),
                        withheld=missing_target_message(raw, environment),
                    )
                )
            elif reference is not None:
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
        One per key column, then per reference column, then per `at:` column that the SELECT
        (with a history asset's added columns) does not return, then per measure column and
        `not_over` column, that the asset's explicit SELECT does not return, in path order;
        for a history asset, also one per SELECT column named like an added column and one for a
        `valid_from:` column the SELECT does not return; nothing for a star or a Python asset,
        whose columns are checked at the run.
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
        if asset.metadata.write == "history":
            reserved = {name.casefold() for name in added_columns(asset.table)}
            issues.extend(
                ProjectIssue(asset.path, line, reserved_name_message(column, asset.table))
                for column in asset.columns
                if column.casefold() in reserved
            )
            valid_from = asset.metadata.valid_from
            if valid_from is not None and valid_from not in asset.columns:
                spelled = next(
                    (c for c in asset.columns if c.casefold() == valid_from.casefold()), None
                )
                issues.append(
                    ProjectIssue(
                        asset.path,
                        line,
                        valid_from_column_message(valid_from, asset.table, spelled=spelled),
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
        available = set(asset.columns)
        if asset.metadata.write == "history":
            available |= set(added_columns(asset.table))
            available.discard(asset.metadata.valid_from or "")
        for column, at in sorted(asset.metadata.reference_times().items()):
            if at not in available:
                issues.append(
                    ProjectIssue(
                        asset.path,
                        line,
                        f"Column `{at}` in `at:` of reference `{column}` is not a column of "
                        f"`{asset.table}`'s SELECT; add it to the SELECT, or name another column.",
                    )
                )
        for column, spec in sorted(asset.metadata.measures.items()):
            if column not in asset.columns:
                issues.append(
                    ProjectIssue(
                        asset.path,
                        line,
                        f"Measure column `{column}` is not a column of `{asset.table}`'s SELECT; "
                        "add it to the SELECT, or remove it from `measures:`.",
                    )
                )
            for other in spec.not_over:
                if other not in asset.columns:
                    issues.append(
                        ProjectIssue(
                            asset.path,
                            line,
                            f"Column `{other}` in `not_over` of measure `{column}` is not a column "
                            f"of `{asset.table}`'s SELECT; add it to the SELECT, or change "
                            "`not_over:`.",
                        )
                    )
    return issues
