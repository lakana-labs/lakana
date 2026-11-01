"""Data quality checks of a project: ``checks/<namespace>/<table>/<name>.sql``.

A check is one ``SELECT`` that returns the rows that are wrong. This module finds the files,
applies the deploy-time rules, and reads the checks back out of a stored manifest. Nothing here
runs a check.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pydantic
import sqlglot
import sqlglot.errors
from sqlglot import exp

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef, shown, validate_path_component
from lakana_core.projects.asset_model import (
    CHECK_FAMILIES,
    CHECK_MODES,
    ParsedAsset,
    normalized_write,
)
from lakana_core.projects.dag import ProjectGraph
from lakana_core.projects.discovery import (
    CHECKS_DIR,
    _decode,
    _display,
    _fold,
    _is_unsafe,
    _read,
)
from lakana_core.projects.frontmatter import split_front_matter
from lakana_core.projects.issues import ProjectIssue, ProjectValidationError
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.manifest import CHECKS_VERSION, LISTS_CHECKS, manifest_assets
from lakana_core.projects.measures import AGGREGATIONS
from lakana_core.projects.parse import _sql_issue
from lakana_core.projects.project_config import SafeTextModel, pydantic_issues
from lakana_core.query_rewrite import check_blacklist, extract_table_refs
from lakana_core.statement_limit import statement_too_long_message

ON_FAILURE: tuple[str, ...] = CHECK_MODES
"""What a failed check does.

``block`` rolls the table back and stops the run, ``keep`` fails the task and leaves the run's
data in the table, ``warn`` only reports.
"""

_DIALECT = "duckdb"
_KEYS = ("description", "on_failure")
_ONE_SELECT = "a check is one SELECT that returns the rows that are wrong"
_LAYOUT_MESSAGE = (
    "a check must live in a directory named after its table: checks/<namespace>/<table>/<name>.sql"
)
_PYTHON_MESSAGE = (
    "a check is a SQL file; a Python file under checks/ is not run. "
    "Write the check as a SELECT that returns the rows that are wrong"
)
_LINK_MESSAGE = "symlinks are not allowed under checks/"
_LISTED_TABLES = 10
_KEEP_REFUSED: dict[str, str] = {
    "incremental": (
        "{setting} cannot be used on `{table}`: it is an incremental asset, and building "
        "it again only adds rows, so the rows that failed would stay. Use {fix}: "
        "it takes the failed increment out again."
    ),
    "merge": (
        "{setting} cannot be used on `{table}`: it is written with `write: merge`, and "
        "building it again never deletes a row, so the rows that failed would stay. Use "
        "{fix}: it takes the failed merge out again."
    ),
    "history": (
        "{setting} cannot be used on `{table}`: it is written with `write: history`, and "
        "building it again never removes a version, so the rows that failed would stay. Use "
        "{fix}: it takes the failed run's versions out again."
    ),
    "view": (
        "{setting} cannot be used on `{table}`: it is a view and holds no data to keep. "
        "Use {fix}: for a view it stops what reads it in that run."
    ),
}
"""Why a check of an asset cannot say ``on_failure: keep``, by how the asset is built
(`_built_as`).

``{setting}`` is ``on_failure: keep`` for a hand-written check and ``checks: <family>: keep``
for a generated one; ``{fix}`` is the same setting with ``block``.

A kept table is cleared by building it again and passing its checks. An incremental build
(``write: append``) only adds rows, a merge never deletes one and a history write never
removes a version, so the rows that failed would
still be there; a view holds no data. An asset of
``materialize: table`` (SQL or Python) is replaced by a build, and a table the project only
reads gets no note at all, so neither is listed.
"""
RESERVED_NAMES: tuple[str, ...] = ("(none)", "(unreadable)")
"""Names no check can have: a task's result is called so when a check cannot be given."""


def _built_as(asset: ParsedAsset) -> str | None:
    """Name how a build writes an asset, as `_KEEP_REFUSED` is keyed.

    Parameters
    ----------
    asset
        The parsed asset.

    Returns
    -------
    str or None
        ``view``, ``incremental`` (``write: append`` in either spelling), ``merge``, ``history``,
        or the asset's ``materialize`` otherwise.
    """
    if asset.metadata.materialize == "view":
        return "view"
    write = normalized_write(asset.metadata)[0]
    if write == "append":
        return "incremental"
    if write in ("merge", "history"):
        return write
    return asset.metadata.materialize


@dataclass(frozen=True)
class GeneratedFrom:
    """The header field a generated check comes from.

    Parameters
    ----------
    family
        ``key``, ``references`` or ``measures`` (`CHECK_FAMILIES`).
    columns
        The key's columns, the one referencing column, or the measure columns, sorted.
    target
        The referenced table; `None` for a key or measures check.
    target_column
        The referenced column; `None` for a key or measures check.
    not_over
        For ``measures``, every column a semi-additive measure must not be summed over,
        distinct and sorted; empty otherwise.
    aggregations
        For ``measures``, each column's aggregation, in the order of `columns`; empty
        otherwise.
    history
        For ``key``, whether the key is a history dimension's natural key: its checks are
        about versions, and the pre-check requires the columns `write: history` adds;
        `False` otherwise.
    at
        For ``references``, the referencing table's time column of a valid-at check; `None`
        otherwise.
    """

    family: str
    columns: tuple[str, ...]
    target: str | None = None
    target_column: str | None = None
    not_over: tuple[str, ...] = ()
    aggregations: tuple[str, ...] = ()
    history: bool = False
    at: str | None = None


@dataclass(frozen=True)
class DiscoveredCheck:
    """One check file as discovery found it.

    Parameters
    ----------
    path
        Repository-relative POSIX path.
    table
        The dotted name of the table the path names.
    check
        The check's name (the file name without ``.sql``).
    text
        The decoded content, BOM removed and line endings normalized.
    """

    path: str
    table: str
    check: str
    text: str


@dataclass(frozen=True)
class ParsedCheck:
    """One check, parsed and ready for the deploy rules and the manifest.

    Parameters
    ----------
    name
        ``<table>/<check>``.
    table
        The dotted name of the checked table.
    check
        The check's name.
    path
        Repository-relative path.
    on_failure
        ``block``, ``keep`` or ``warn``.
    description
        The block's description, if any.
    sql
        The SELECT, stripped.
    references
        Every distinct table the SELECT reads, sorted.
    body_line
        Line on which the SQL starts.
    from_default
        Whether `on_failure` is the project's default from ``lakana.yaml``: the check stated
        none itself and the default is not ``block``.
    block_line
        Line on which the check's ``/* lakana */`` settings block starts; `None` when it has
        none. For a generated check, the line of the asset's header.
    generated
        Where a generated check comes from; `None` for a hand-written one. A generated check's
        `path` is ``<asset file>:<header line>``.
    """

    name: str
    table: str
    check: str
    path: str
    on_failure: str
    description: str | None
    sql: str
    references: tuple[str, ...]
    body_line: int
    from_default: bool = False
    block_line: int | None = None
    generated: GeneratedFrom | None = None


@dataclass(frozen=True)
class CheckSpec:
    """One check as a stored manifest says it, which is all a run needs.

    Parameters
    ----------
    name
        ``<table>/<check>``.
    table
        The dotted name of the checked table.
    check
        The check's name.
    path
        Repository-relative path, for messages.
    on_failure
        ``block``, ``keep`` or ``warn``.
    description
        The description, if any.
    sql
        The SELECT.
    generated
        Where a generated check comes from; `None` for a hand-written one.
    """

    name: str
    table: str
    check: str
    path: str
    on_failure: str
    description: str | None
    sql: str
    generated: GeneratedFrom | None = None


class CheckMetadata(SafeTextModel):
    """What a check's ``/* lakana */`` block may say.

    Parameters
    ----------
    description
        Human description.
    on_failure
        ``block`` (the default), ``keep`` or ``warn``.
    """

    description: str | None = None
    on_failure: str = "block"


def discover_checks(
    root: Path, *, limits: DeployLimits
) -> tuple[list[DiscoveredCheck], list[ProjectIssue]]:
    """Find every check file under ``checks/``.

    A symlink is refused by name and never followed or read, as under ``assets/``.

    Parameters
    ----------
    root
        The extracted checkout.
    limits
        Supplies the file size limit.

    Returns
    -------
    tuple of (list of DiscoveredCheck, list of ProjectIssue)
        The checks sorted by path, and every problem found. A project without a ``checks/``
        directory has neither.
    """
    checks_root = root / CHECKS_DIR
    issues: list[ProjectIssue] = []
    found: list[DiscoveredCheck] = []
    if checks_root.is_symlink():
        return [], [ProjectIssue(CHECKS_DIR, None, _LINK_MESSAGE)]
    if not checks_root.is_dir():
        return [], []
    seen: dict[str, str] = {}

    def _walk_error(exc: OSError) -> None:
        """Record a directory that could not be listed.

        Parameters
        ----------
        exc
            The error `os.walk` hit.
        """
        name = _display(Path(str(exc.filename)).relative_to(root).as_posix())
        issues.append(ProjectIssue(name, None, f"cannot be read: {exc.strerror}"))

    for directory, dirnames, filenames in os.walk(
        checks_root, onerror=_walk_error, followlinks=False
    ):
        dirnames.sort()
        here = Path(directory)
        for name in list(dirnames):
            if (here / name).is_symlink():
                relative = (here / name).relative_to(root).as_posix()
                issues.append(ProjectIssue(_display(relative), None, _LINK_MESSAGE))
                dirnames.remove(name)
        for name in sorted(filenames):
            path = here / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                issues.append(ProjectIssue(_display(relative), None, _LINK_MESSAGE))
                continue
            lowered = name.lower()
            if not lowered.endswith((".sql", ".py")):
                continue
            if _is_unsafe(relative):
                issues.append(
                    ProjectIssue(
                        _display(relative), None, "the path has a non-UTF-8 or control character"
                    )
                )
                continue
            if lowered.endswith(".py"):
                issues.append(ProjectIssue(relative, None, _PYTHON_MESSAGE))
                continue
            parts = list(path.relative_to(checks_root).parts)
            parts[-1] = parts[-1][: -len(".sql")]
            names = _names(parts, relative, issues)
            data = _read(path, relative, limits.sql_file_bytes, issues)
            text = _decode(data, relative, issues) if data is not None else None
            if names is None or text is None:
                continue
            table, check = names
            key = _fold(f"{table}/{check}")
            earlier = seen.get(key)
            if earlier is not None:
                issues.append(
                    ProjectIssue(
                        relative,
                        None,
                        f"{earlier} and {relative} are both the check '{table}/{check}'",
                    )
                )
                continue
            seen[key] = relative
            found.append(DiscoveredCheck(path=relative, table=table, check=check, text=text))
    return sorted(found, key=lambda check: check.path), issues


def _names(parts: list[str], relative: str, issues: list[ProjectIssue]) -> tuple[str, str] | None:
    """Validate a check path's components and name the table and the check.

    Parameters
    ----------
    parts
        Components below ``checks/``, the last one with ``.sql`` removed.
    relative
        The file's repository-relative path.
    issues
        Collects issues.

    Returns
    -------
    tuple of (str, str) or None
        The dotted table name and the check's name, or `None` after recording an issue.
    """
    if len(parts) < 3:
        issues.append(ProjectIssue(relative, None, _LAYOUT_MESSAGE))
        return None
    try:
        for component in parts[:-2]:
            validate_path_component(component, what="Namespace component")
        validate_path_component(parts[-2], what="Table name")
    except ValidationError as exc:
        issues.append(ProjectIssue(relative, None, str(exc)))
        return None
    try:
        validate_path_component(parts[-1], what="Check name")
    except ValidationError as exc:
        issues.append(ProjectIssue(relative, None, f"the check's name must be a valid name: {exc}"))
        return None
    if parts[-1] in RESERVED_NAMES:
        issues.append(
            ProjectIssue(
                relative,
                None,
                f"the check's name '{parts[-1]}' is reserved: it names the result of a check that "
                "cannot be read. Rename the file.",
            )
        )
        return None
    return ".".join(parts[:-1]), parts[-1]


def _one_select(sql: str) -> exp.Query:
    """Parse a check's SQL and return its one SELECT, or refuse with the rule it breaks.

    Parameters
    ----------
    sql
        The check's SQL.

    Returns
    -------
    exp.Query
        The one query.

    Raises
    ------
    ValidationError
        If there is no SQL, more than one statement, or a statement that is not a query.
    """
    trees = [tree for tree in sqlglot.parse(sql, dialect=_DIALECT) if tree is not None]
    if not trees:
        raise ValidationError("the check has no SQL")
    if len(trees) != 1:
        raise ValidationError(f"{_ONE_SELECT}; this file has {len(trees)} statements")
    if not isinstance(trees[0], exp.Query):
        raise ValidationError(
            f"{_ONE_SELECT}; this file is a {type(trees[0]).__name__.upper()} statement"
        )
    return trees[0]


def count_query(sql: str) -> str:
    """Wrap a check's SELECT into the query that counts its rows.

    Parameters
    ----------
    sql
        The check's SQL.

    Returns
    -------
    str
        ``SELECT COUNT(*) AS failing FROM (<the check>) AS lakana_check``.

    Raises
    ------
    ValidationError
        If the SQL is not one SELECT.
    """
    counted = exp.alias_(exp.Count(this=exp.Star()), "failing")
    return (
        exp.select(counted).from_(_one_select(sql).subquery("lakana_check")).sql(dialect=_DIALECT)
    )


def _metadata(
    text_path: str, raw: Mapping[str, object], block_line: int, default_on_failure: str
) -> tuple[CheckMetadata | None, list[ProjectIssue]]:
    """Validate a check's block.

    Parameters
    ----------
    text_path
        The file's path, for issues.
    raw
        The block's mapping.
    block_line
        The line of ``/* lakana``.
    default_on_failure
        The project's default, which a block that states no ``on_failure`` takes.

    Returns
    -------
    tuple of (CheckMetadata or None, list of ProjectIssue)
        The metadata, or `None` and the issues.
    """
    issues = [
        ProjectIssue(
            text_path,
            block_line,
            f"unknown key {shown(key)!r}; a check's /* lakana */ block takes: " + ", ".join(_KEYS),
        )
        for key in raw
        if key not in _KEYS
    ]
    on_failure = raw.get("on_failure", "block")
    if on_failure not in ON_FAILURE:
        issues.append(
            ProjectIssue(
                text_path,
                block_line,
                f"on_failure must be {', '.join(ON_FAILURE[:-1])} or {ON_FAILURE[-1]} "
                f"(got {shown(on_failure)!r})",
            )
        )
    if issues:
        return None, issues
    try:
        return CheckMetadata.model_validate({"on_failure": default_on_failure, **raw}), []
    except pydantic.ValidationError as exc:
        return None, pydantic_issues(exc, path=text_path, line=block_line)


def _parse_one(
    check: DiscoveredCheck, limits: DeployLimits, default_on_failure: str
) -> ParsedCheck | list[ProjectIssue]:
    """Parse one check, returning either the result or its issues.

    Parameters
    ----------
    check
        The discovered file.
    limits
        Size limits.
    default_on_failure
        The project's default for a check that states no ``on_failure``.

    Returns
    -------
    ParsedCheck or list of ProjectIssue
        The parsed check, or every issue found in it.
    """
    try:
        front = split_front_matter(check.text, path=check.path, limits=limits)
    except ProjectValidationError as exc:
        return list(exc.issues)
    metadata = CheckMetadata(on_failure=default_on_failure)
    issues: list[ProjectIssue] = []
    if front.block_line is not None:
        parsed, issues = _metadata(check.path, front.raw, front.block_line, default_on_failure)
        if parsed is not None:
            metadata = parsed
    sql = front.body.strip()
    if not sql:
        return [*issues, ProjectIssue(check.path, front.body_line, "the check has no SQL")]
    line = front.body_line + front.body[: len(front.body) - len(front.body.lstrip())].count("\n")
    if len(sql) > limits.statement_chars:
        message = statement_too_long_message("the SQL", len(sql), limits.statement_chars)
        return [*issues, ProjectIssue(check.path, line, message)]
    try:
        _one_select(sql)
        check_blacklist(sql, dialect=_DIALECT)
        references = tuple(sorted(extract_table_refs(sql, dialect=_DIALECT)))
    except RecursionError:
        return [*issues, ProjectIssue(check.path, line, "the SQL is nested too deeply")]
    except sqlglot.errors.SqlglotError as exc:
        issue = _sql_issue(check.path, line, exc, sql)
        if front.cut_hint is not None:
            issue = ProjectIssue(issue.path, issue.line, f"{issue.message} ({front.cut_hint})")
        return [*issues, issue]
    except ValidationError as exc:
        return [*issues, ProjectIssue(check.path, line, str(exc))]
    if check.table not in references:
        issues.append(
            ProjectIssue(
                check.path,
                line,
                f"the check does not read {check.table!r}, the table its path names. Select "  # nosec B608 -- a message, not a query
                "from that table, or move the file into the directory of the table it checks.",
            )
        )
    if issues:
        return issues
    return ParsedCheck(
        name=f"{check.table}/{check.check}",
        table=check.table,
        check=check.check,
        path=check.path,
        on_failure=metadata.on_failure,
        description=metadata.description,
        sql=sql,
        references=references,
        body_line=line,
        from_default=default_on_failure != "block" and "on_failure" not in front.raw,
        block_line=front.block_line,
    )


def parse_checks(
    checks: Sequence[DiscoveredCheck],
    *,
    limits: DeployLimits | None = None,
    default_on_failure: str = "block",
) -> list[ParsedCheck]:
    """Parse every check, collecting all issues before failing.

    Parameters
    ----------
    checks
        Discovered checks.
    limits
        Size limits; the defaults when `None`.
    default_on_failure
        What a check that states no ``on_failure`` takes: the project's default from
        ``lakana.yaml``.

    Returns
    -------
    list of ParsedCheck
        In input order.

    Raises
    ------
    ProjectValidationError
        With every issue from every check.
    """
    limits = limits or DeployLimits()
    parsed: list[ParsedCheck] = []
    issues: list[ProjectIssue] = []
    for check in checks:
        result = _parse_one(check, limits, default_on_failure)
        if isinstance(result, ParsedCheck):
            parsed.append(result)
        else:
            issues.extend(result)
    if issues:
        raise ProjectValidationError(issues)
    return parsed


def _listed(names: Iterable[str]) -> str:
    """List table names for a message: sorted, at most ten, then a count.

    Parameters
    ----------
    names
        The table names.

    Returns
    -------
    str
        The names, ``nothing`` for none.
    """
    ordered = sorted(names)
    if not ordered:
        return "nothing"
    text = ", ".join(ordered[:_LISTED_TABLES])
    if len(ordered) > _LISTED_TABLES:
        text += f" and {len(ordered) - _LISTED_TABLES} more"
    return text


def check_issues(
    checks: Sequence[ParsedCheck],
    assets: Sequence[ParsedAsset],
    graph: ProjectGraph,
    *,
    limits: DeployLimits | None = None,
) -> list[ProjectIssue]:
    """Check the checks against the project's assets.

    Parameters
    ----------
    checks
        The parsed checks.
    assets
        The parsed assets.
    graph
        Their DAG, whose external inputs are the tables the project reads.
    limits
        Supplies the most checks of one table; the defaults when `None`.

    Returns
    -------
    list of ProjectIssue
        A check of a table the project neither builds nor reads, a check of an artifact, a
        check with ``on_failure: keep`` (or a header's ``checks: <family>: keep``) of an
        incremental, merge, history or view asset (`_KEEP_REFUSED`), and a table with too many
        checks, generated checks counted.
    """
    limits = limits or DeployLimits()
    built = {asset.table for asset in assets}
    artifacts = {asset.table for asset in assets if asset.output == "artifact"}
    materialize = {asset.table: _built_as(asset) for asset in assets}
    reads = set(graph.external_inputs)
    issues: list[ProjectIssue] = []
    per_table: dict[str, list[ParsedCheck]] = {}
    refused_families: set[tuple[str, str]] = set()
    for check in sorted(checks, key=lambda c: c.path):
        per_table.setdefault(check.table, []).append(check)
        if check.table in artifacts:
            issues.append(
                ProjectIssue(
                    check.path,
                    check.body_line,
                    f"{check.table!r} is an artifact; a check needs a table. "
                    "Check a table the artifact is built from instead.",
                )
            )
        elif check.table not in built and check.table not in reads:
            issues.append(
                ProjectIssue(
                    check.path,
                    check.body_line,
                    f"table {check.table!r} is neither built nor read by this project. "
                    "A check belongs to a table that an asset of this project produces or "
                    f"selects from. This project builds: {_listed(built)}. "
                    f"It reads: {_listed(reads)}.",
                )
            )
        elif check.on_failure == "keep" and materialize.get(check.table) in _KEEP_REFUSED:
            # The project's default is never `keep`, so this is the check's own value.
            refusal = _KEEP_REFUSED[cast("str", materialize[check.table])]
            if check.generated is not None:
                # One header setting, however many checks its family generates: one issue.
                family = (check.table, check.generated.family)
                if family in refused_families:
                    continue
                refused_families.add(family)
            issues.append(_keep_refused(check, refusal))
    for table, members in per_table.items():
        if len(members) > limits.max_checks_per_table:
            generated = sum(1 for member in members if member.generated)
            hand = [member for member in members if member.generated is None]
            directory = (
                hand[0].path.rsplit("/", 1)[0] if hand else members[0].path.rpartition(":")[0]
            )
            head = f"more than {limits.max_checks_per_table} checks of table {table!r}"
            message = (
                f"{head} ({generated} of them generated from the asset's header); remove or "
                "combine hand-written checks, or split the table"
                if generated
                else f"{head}; combine checks or split the table"
            )
            issues.append(ProjectIssue(directory, None, message))
    return issues


def _keep_refused(check: ParsedCheck, refusal: str) -> ProjectIssue:
    """Refuse `keep` on one check, naming the setting that says it.

    Parameters
    ----------
    check
        The check, hand-written or generated.
    refusal
        The `_KEEP_REFUSED` text for how its table is built.

    Returns
    -------
    ProjectIssue
        At the check's settings block, or, for a generated check, at the asset's header, whose
        file is the check's `path` without its ``:<line>``.
    """
    table = shown(check.table)
    if check.generated is None:
        text = refusal.format(setting="on_failure: keep", fix="`on_failure: block`", table=table)
        return ProjectIssue(check.path, check.block_line, text)
    family = check.generated.family
    text = refusal.format(
        setting=f"`checks: {family}: keep`", fix=f"`checks: {family}: block`", table=table
    )
    return ProjectIssue(check.path.rpartition(":")[0], check.block_line, text)


def checks_listing(checks: Sequence[ParsedCheck]) -> dict[str, list[dict[str, str]]]:
    """List the checks per table, for a deploy's report.

    Parameters
    ----------
    checks
        The parsed checks.

    Returns
    -------
    dict
        Table to ``[{"check": name, "on_failure": ...}]``, tables and checks sorted. An entry
        whose value came from the project's default also has ``"from": "lakana.yaml"``; one the
        asset's header generated has ``"generated"``: ``"key"``, ``"references"`` or
        ``"measures"``.
    """
    listing: dict[str, list[dict[str, str]]] = {}
    for check in sorted(checks, key=lambda c: (c.table, c.check)):
        entry = {"check": check.check, "on_failure": check.on_failure}
        if check.from_default:
            entry["from"] = "lakana.yaml"
        if check.generated is not None:
            entry["generated"] = check.generated.family
        listing.setdefault(check.table, []).append(entry)
    return listing


@dataclass(frozen=True)
class ManifestChecks:
    """What a stored manifest says about checks: the ones that can be read and the ones that cannot.

    A stored manifest is untrusted. A deploy never writes an entry that cannot be read, but a
    hand-edited or corrupted manifest could hold one, and an entry that is skipped silently is a
    check that silently does not run. The reader therefore tells the two apart.

    Parameters
    ----------
    specs
        The checks that can be read, sorted by name.
    unreadable_tables
        The tables that have an entry which names its table and cannot be read otherwise.
    unplaced
        Whether something that cannot be read belongs to no task in particular: an entry that
        is not a mapping, whose ``table`` is not a string or is not a table of the manifest, a
        ``checks`` value that is not a list, or a manifest of the version that means "has checks"
        with no entry. It counts against every table.
    """

    specs: tuple[CheckSpec, ...]
    unreadable_tables: frozenset[str]
    unplaced: bool

    def of(self, table: str) -> tuple[CheckSpec, ...]:
        """Return the readable checks of one table.

        Parameters
        ----------
        table
            The dotted table name.

        Returns
        -------
        tuple of CheckSpec
            Its checks, sorted by name.
        """
        return tuple(spec for spec in self.specs if spec.table == table)

    def unreadable_for(self, table: str) -> bool:
        """Say whether the manifest holds a check entry that cannot be read and may be this table's.

        Parameters
        ----------
        table
            The dotted table name.

        Returns
        -------
        bool
            `True` for an unreadable entry that names the table, or one that names no table
            (it may be this one, and the safe reading is that it is).
        """
        return self.unplaced or table in self.unreadable_tables


def _known_tables(manifest: object) -> set[str]:
    """List the tables a manifest's assets build or read.

    Parameters
    ----------
    manifest
        A manifest; untrusted.

    Returns
    -------
    set of str
        The assets' tables and every table any asset's ``references`` names.
    """
    assets = manifest_assets(manifest)
    known = set(assets)
    for entry in assets.values():
        references = entry.get("references")
        if isinstance(references, list):
            known.update(r for r in references if isinstance(r, str))
    return known


def read_manifest_checks(manifest: object) -> ManifestChecks:
    """Read a stored manifest's checks, and note every entry that cannot be read.

    An entry is unreadable when a field is missing or not a string, when its ``name`` is not
    ``<table>/<check>`` (results are recorded and shown under the name), or when its ``name``
    repeats (the second result would replace the first's failure; both entries count). A
    ``checks`` key that is present and is not a list is damage, and so is a manifest of the
    version that means "has checks" without one entry: only an absent key means "written before
    checks". An entry whose table is not a table of the manifest belongs to no task, so it counts
    against every table, whether or not the entry itself can be read.

    Parameters
    ----------
    manifest
        A manifest; untrusted.

    Returns
    -------
    ManifestChecks
        The readable checks, and what could not be read. Empty for a manifest from before
        checks. An ``on_failure`` that is not ``keep`` or ``warn`` reads as ``block``: an
        unreadable value must not turn a blocking check into a warning. A damaged
        ``description`` is only dropped; it decides nothing. A ``generated`` key that
        `_generated_from` cannot read makes the entry unreadable: it says which header field the
        check stands for.
    """
    if not isinstance(manifest, Mapping):
        return ManifestChecks((), frozenset(), unplaced=False)
    raw = manifest.get("checks")
    version = manifest.get("version")
    # A version 4 manifest exists because it has checks; a version 5 to 10 one may have only
    # jobs, connections or the new write spelling, so it must carry the list but may leave it
    # empty.
    means_checks = version in LISTS_CHECKS
    if "checks" not in manifest and not means_checks:
        return ManifestChecks((), frozenset(), unplaced=False)
    if not isinstance(raw, list) or (version == CHECKS_VERSION and not raw):
        return ManifestChecks((), frozenset(), unplaced=True)
    candidates: list[CheckSpec] = []
    tables: set[str] = set()
    unplaced = False
    for entry in raw:
        if not isinstance(entry, dict):
            unplaced = True
            continue
        text = {key: entry.get(key) for key in ("name", "table", "check", "path", "sql")}
        table = entry.get("table")
        if not all(isinstance(value, str) for value in text.values()) or (
            text["name"] != f"{text['table']}/{text['check']}"
        ):
            if isinstance(table, str):
                tables.add(table)
            else:
                unplaced = True
            continue
        generated = None
        if "generated" in entry:
            generated = _generated_from(entry["generated"])
            if generated is None:
                tables.add(cast("str", table))
                continue
        description = entry.get("description")
        candidates.append(
            CheckSpec(
                **text,
                on_failure=value
                if (value := entry.get("on_failure")) in ("keep", "warn")
                else "block",
                description=description if isinstance(description, str) else None,
                generated=generated,
            )
        )
    names = [spec.name for spec in candidates]
    repeated = {name for name in names if names.count(name) > 1}
    specs = [spec for spec in candidates if spec.name not in repeated]
    tables.update(spec.table for spec in candidates if spec.name in repeated)
    if (tables | {spec.table for spec in specs}) - _known_tables(manifest):
        unplaced = True
    return ManifestChecks(
        tuple(sorted(specs, key=lambda spec: spec.name)), frozenset(tables), unplaced
    )


def _generated_from(raw: object) -> GeneratedFrom | None:
    """Read a stored entry's ``generated`` value.

    Parameters
    ----------
    raw
        The value; untrusted.

    Returns
    -------
    GeneratedFrom or None
        The source of a well-formed value, else `None`: a mapping whose ``family`` is in
        `CHECK_FAMILIES`, whose ``columns`` is a non-empty list of strings, and whose
        ``target`` and ``target_column`` are both strings for ``references`` and both absent
        or null for ``key``; for ``measures``, both target fields absent or null, ``not_over``
        absent or a list of strings, and ``aggregations`` a list of `AGGREGATIONS` as long as
        ``columns``. ``not_over`` or ``aggregations`` on another family is refused. ``history``
        is absent or a boolean, and true only for ``key``; ``at`` is absent or a non-empty
        string, and only for ``references``.
    """
    if not isinstance(raw, Mapping):
        return None
    family, columns = raw.get("family"), raw.get("columns")
    target, target_column = raw.get("target"), raw.get("target_column")
    if family not in CHECK_FAMILIES or not isinstance(columns, list) or not columns:
        return None
    if not all(isinstance(column, str) for column in columns):
        return None
    not_over, aggregations = raw.get("not_over"), raw.get("aggregations")
    history, at = raw.get("history"), raw.get("at")
    if history is not None and (not isinstance(history, bool) or family != "key"):
        return None
    if at is not None and (not isinstance(at, str) or not at or family != "references"):
        return None
    if family == "measures":
        if target is not None or target_column is not None:
            return None
        listed = [] if not_over is None else not_over
        if not isinstance(listed, list) or not all(isinstance(c, str) for c in listed):
            return None
        if not isinstance(aggregations, list) or len(aggregations) != len(columns):
            return None
        if not all(isinstance(a, str) and a in AGGREGATIONS for a in aggregations):
            return None
        return GeneratedFrom(
            "measures", tuple(columns), not_over=tuple(listed), aggregations=tuple(aggregations)
        )
    if not_over is not None or aggregations is not None:
        return None
    if family == "references":
        if not isinstance(target, str) or not isinstance(target_column, str):
            return None
        return GeneratedFrom("references", tuple(columns), target, target_column, at=at)
    if target is not None or target_column is not None:
        return None
    return GeneratedFrom("key", tuple(columns), history=history is True)


def manifest_checks(manifest: object) -> tuple[CheckSpec, ...]:
    """Read a stored manifest's readable checks; an entry that cannot be read is left out.

    Parameters
    ----------
    manifest
        A manifest; untrusted.

    Returns
    -------
    tuple of CheckSpec
        Sorted by name; empty for a manifest from before checks. A caller that must not run
        without a check that is there uses `read_manifest_checks`, which also says what was
        left out.
    """
    return read_manifest_checks(manifest).specs


def checks_of(manifest: object, table: str) -> tuple[CheckSpec, ...]:
    """Return the readable checks of one table, sorted by name.

    Parameters
    ----------
    manifest
        A manifest; untrusted.
    table
        The dotted table name.

    Returns
    -------
    tuple of CheckSpec
        Its checks.
    """
    return read_manifest_checks(manifest).of(table)


def check_sql_references(sql: object) -> tuple[str, ...] | None:
    """List the tables a check's SQL names, read again from the SQL itself.

    A stored manifest is untrusted, so its ``references`` never decide who may see a check's
    SQL; this reads the SQL the way the deploy did.

    Parameters
    ----------
    sql
        The check's SQL as a stored manifest holds it; untrusted.

    Returns
    -------
    tuple of str or None
        The dotted names, sorted. `None` when `sql` is not a string or its tables cannot be
        listed: such SQL is shown to nobody, since nobody can be checked against it.
    """
    if not isinstance(sql, str):
        return None
    try:
        return tuple(sorted(extract_table_refs(sql, dialect=_DIALECT)))
    except (sqlglot.errors.SqlglotError, RecursionError, ValidationError):
        return None


def may_read_every(
    authorization: AuthorizationService,
    p: PrincipalRef,
    environment: str,
    names: Iterable[str],
) -> bool:
    """Report whether a principal would be allowed to read every one of some table names.

    A name is judged by the READ_TABLE grants that cover it, existing or not, without view
    expansion: the same rule that decides whether an asset's SQL is shown.

    Parameters
    ----------
    authorization
        Authorization service.
    p
        The principal.
    environment
        Environment name.
    names
        Dotted table names.

    Returns
    -------
    bool
        `True` when a READ_TABLE grant covers each name; `False` as soon as one is not covered
        or is not a valid table name.
    """
    for dotted in names:
        try:
            ref = TableRef.from_dotted(environment, dotted)
        except ValidationError:
            return False
        securable = Securable.table_scope(environment, ref.namespace, ref.name)
        if not authorization.permits(p, Action.READ_TABLE, securable):
            return False
    return True


def source_check_tables(
    manifest: object, selected: Iterable[str], read: ManifestChecks | None = None
) -> dict[str, tuple[str, ...]]:
    """Map each checked table no asset produces to the selected assets that read it.

    Parameters
    ----------
    manifest
        A manifest; untrusted.
    selected
        The assets a run builds.
    read
        `read_manifest_checks` of `manifest`, when the caller has it already; read here
        otherwise.

    Returns
    -------
    dict
        Table to the sorted selected assets whose ``references`` name it; only tables with at
        least one selected reader. A table whose only check entry cannot be read is included: its
        source check task fails closed instead of the check being missing.
    """
    assets = manifest_assets(manifest)
    read = read if read is not None else read_manifest_checks(manifest)
    checked = ({spec.table for spec in read.specs} | read.unreadable_tables) - set(assets)
    readers: dict[str, list[str]] = {}
    for asset in sorted(set(selected) & set(assets)):
        references = assets[asset].get("references")
        for table in sorted(checked & set(references if isinstance(references, list) else [])):
            readers.setdefault(table, []).append(asset)
    return {table: tuple(names) for table, names in sorted(readers.items())}


def kept_check_tables(
    manifest: object, selected: Iterable[str], kept: Collection[str]
) -> dict[str, tuple[str, ...]]:
    """Map each kept table that a selected asset reads, and the run does not build, to its readers.

    A table that keeps data that failed a check (`on_failure: keep`) is checked before what
    reads it is built, unless the run rebuilds it. Such a table needs a check task whether or not
    this manifest still has a check for it: a table without a readable check gets a task that
    fails closed.

    Parameters
    ----------
    manifest
        A manifest; untrusted.
    selected
        The assets a run builds.
    kept
        The dotted names of the tables that carry the note.

    Returns
    -------
    dict
        Table to the sorted selected assets that read it, directly or through unselected assets
        of ``materialize: view`` (``references`` are followed through those only, transitively;
        a reference cycle ends the walk); only tables that are in `kept`, are assets of the
        manifest, are not in `selected` and have a selected reader.
    """
    assets = manifest_assets(manifest)
    chosen = set(selected)
    return _readers_through_views(assets, chosen, (set(kept) & set(assets)) - chosen)


def view_readers(manifest: object, selected: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Map each selected asset to the selected assets that read it through unselected views.

    A selected asset that reads another selected one only through an unselected view has no
    edge to it among the selected assets, so nothing would order it after the other: it could
    start while the other is rebuilt and read the table's previous state (or, for a kept
    table, be refused it). The run therefore lists the asset among that reader's upstreams.

    Parameters
    ----------
    manifest
        A manifest; untrusted.
    selected
        The assets a run builds.

    Returns
    -------
    dict
        Selected asset to the sorted selected assets that read it, directly or through
        unselected assets of ``materialize: view``, as `kept_check_tables` walks; never an
        asset as its own reader. A direct reader is named too: the manifest's edge orders it
        already.
    """
    assets = manifest_assets(manifest)
    chosen = set(selected)
    return _readers_through_views(assets, chosen, chosen & set(assets))


def _readers_through_views(
    assets: Mapping[str, Mapping[str, Any]], chosen: set[str], candidates: set[str]
) -> dict[str, tuple[str, ...]]:
    """Map each candidate table to the selected assets that read it, through unselected views.

    Parameters
    ----------
    assets
        The manifest's assets by table name.
    chosen
        The assets a run builds.
    candidates
        The tables to look for.

    Returns
    -------
    dict
        Candidate to its sorted selected readers; ``references`` are followed through
        unselected assets of ``materialize: view`` only, transitively, and a reference cycle
        ends the walk. A candidate is never its own reader.
    """
    readers: dict[str, list[str]] = {}
    for asset in sorted(chosen & set(assets)):
        # An unselected view holds no data: reading it reads the table it is defined over, so
        # the walk goes through it. An unselected table holds older, checked data: it stops.
        seen = {asset}
        hits: set[str] = set()
        pending = [asset]
        while pending:
            references = assets[pending.pop()].get("references")
            for name in references if isinstance(references, list) else []:
                if name in candidates and name != asset:
                    hits.add(name)
                elif (
                    name in assets
                    and name not in chosen
                    and name not in seen
                    and assets[name].get("materialize") == "view"
                ):
                    seen.add(name)
                    pending.append(name)
        for name in hits:
            readers.setdefault(name, []).append(asset)
    return {table: tuple(names) for table, names in sorted(readers.items())}
