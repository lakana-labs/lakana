"""Per-asset parsing: front-matter, exactly one query, references, columns, lineage."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

import sqlglot
import sqlglot.errors
from sqlglot import exp

from lakana_core.errors import ValidationError
from lakana_core.lineage import extract_lineage
from lakana_core.projects.asset_model import ParsedAsset, model_issues
from lakana_core.projects.discovery import DiscoveredAsset
from lakana_core.projects.frontmatter import (
    consistency_issues,
    merge_metadata,
    parse_front_matter,
)
from lakana_core.projects.issues import ProjectIssue, ProjectValidationError
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.project_config import AssetDefaults
from lakana_core.projects.python_assets import parse_python_asset
from lakana_core.query_rewrite import check_blacklist, extract_table_refs
from lakana_core.sql_syntax import syntax_problem
from lakana_core.statement_limit import statement_too_long_message

_DIALECT = "duckdb"


def normalize_sql(sql: str) -> str:
    """Regenerate SQL so formatting, comments and keyword case do not count as a change.

    Parameters
    ----------
    sql
        A single query that already passed `check_blacklist`.

    Returns
    -------
    str
        sqlglot's canonical DuckDB rendering.
    """
    return sqlglot.parse_one(sql, dialect=_DIALECT).sql(
        dialect=_DIALECT, normalize=True, comments=False
    )


def _is_star(projection: exp.Expr) -> bool:
    """Say whether an outermost projection expands to unknown columns.

    Parameters
    ----------
    projection
        One select-list expression.

    Returns
    -------
    bool
        True for `*`, `t.*` and DuckDB's `COLUMNS(...)`.
    """
    if isinstance(projection, exp.Star):
        return True
    if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
        return True
    return isinstance(projection, exp.Columns)


def _output_columns(sql: str) -> tuple[str, ...] | None:
    """List an explicit projection's output names.

    Parameters
    ----------
    sql
        A single query.

    Returns
    -------
    tuple of str or None
        The names, or `None` if any outermost projection is a star (`*`,
        `t.*` or `COLUMNS(...)`); a star nested in a call, like `count(*)`,
        does not count. `None` too for a bare `VALUES` list, whose columns
        DuckDB names itself (`col0`, `col1`, ...).
    """
    parsed = sqlglot.parse_one(sql, dialect=_DIALECT)
    if isinstance(parsed, exp.Values):
        return None
    query = cast("exp.Query", parsed)
    names: list[str] = []
    for projection in query.selects:
        if _is_star(projection):
            return None
        names.append(projection.alias_or_name)
    return tuple(names)


def _sql_issue(
    path: str, body_line: int, exc: sqlglot.errors.SqlglotError, sql: str
) -> ProjectIssue:
    """Describe a sqlglot error without echoing untrusted SQL or escape codes.

    Parameters
    ----------
    path
        Repository-relative path.
    body_line
        File line on which the SQL starts.
    exc
        The sqlglot error; only its structured fields are used, each cleaned and bounded.
    sql
        The SQL that was parsed, which places an unclosed quote.

    Returns
    -------
    ProjectIssue
        The issue, on the offending line when sqlglot reports one.
    """
    problem = syntax_problem(exc, sql)
    if problem is None or problem.line is None:
        reason = problem.reason if problem else "invalid SQL"
        return ProjectIssue(path, body_line, f"the SQL does not parse: {reason}")
    return ProjectIssue(
        path,
        body_line + problem.line - 1,
        f"the SQL does not parse: {problem.reason} (column {problem.column})",
    )


def _parse_one(
    asset: DiscoveredAsset, defaults: AssetDefaults, limits: DeployLimits
) -> ParsedAsset | list[ProjectIssue]:
    """Parse one asset, returning either the result or its issues.

    A Python asset is parsed by `parse_python_asset`.

    Parameters
    ----------
    asset
        The discovered file.
    defaults
        Resolved `lakana.yaml` defaults.
    limits
        Size limits.

    Returns
    -------
    ParsedAsset or list of ProjectIssue
        The parsed asset, or every issue found in it.
    """
    if asset.kind == "python":
        return parse_python_asset(asset, defaults)
    try:
        front = parse_front_matter(asset.text, path=asset.path, limits=limits)
    except ProjectValidationError as exc:
        return list(exc.issues)
    metadata = merge_metadata(defaults, front.metadata)
    issues = consistency_issues(metadata, path=asset.path, line=front.block_line)
    issues.extend(
        ProjectIssue(asset.path, front.block_line, message)
        for message in model_issues(metadata, table=asset.table)
    )
    sql = front.body.strip()
    line = front.body_line + front.body[: len(front.body) - len(front.body.lstrip())].count("\n")
    if not sql:
        return [*issues, ProjectIssue(asset.path, line, "the asset has no SQL")]
    if len(sql) > limits.statement_chars:
        return [
            *issues,
            ProjectIssue(
                asset.path,
                line,
                statement_too_long_message("the SQL", len(sql), limits.statement_chars),
            ),
        ]
    try:
        check_blacklist(sql, dialect=_DIALECT)
        references = tuple(sorted(extract_table_refs(sql, dialect=_DIALECT)))
        columns = _output_columns(sql)
        lineage = extract_lineage(sql, dialect=_DIALECT)
        normalized = normalize_sql(sql)
    except RecursionError:
        return [*issues, ProjectIssue(asset.path, line, "the SQL is nested too deeply")]
    except sqlglot.errors.SqlglotError as exc:
        issue = _sql_issue(asset.path, line, exc, sql)
        if front.cut_hint is not None:
            issue = ProjectIssue(issue.path, issue.line, f"{issue.message} ({front.cut_hint})")
        return [*issues, issue]
    except ValidationError as exc:
        return [*issues, ProjectIssue(asset.path, line, str(exc))]
    if issues:
        return issues
    return ParsedAsset(
        table=asset.table,
        path=asset.path,
        block_line=front.block_line,
        body_line=line,
        metadata=metadata,
        sql=sql,
        normalized_sql=normalized,
        references=references,
        columns=columns,
        lineage=lineage,
    )


def parse_assets(
    assets: Sequence[DiscoveredAsset],
    *,
    defaults: AssetDefaults,
    limits: DeployLimits | None = None,
) -> list[ParsedAsset]:
    """Parse every asset, collecting all issues before failing.

    Parameters
    ----------
    assets
        Discovered assets.
    defaults
        Resolved `lakana.yaml` defaults.
    limits
        Size limits; the defaults when `None`.

    Returns
    -------
    list of ParsedAsset
        In input order.

    Raises
    ------
    ProjectValidationError
        With every issue from every asset.
    """
    limits = limits or DeployLimits()
    parsed: list[ParsedAsset] = []
    issues: list[ProjectIssue] = []
    for asset in assets:
        result = _parse_one(asset, defaults, limits)
        if isinstance(result, ParsedAsset):
            parsed.append(result)
        else:
            issues.extend(result)
    if issues:
        raise ProjectValidationError(issues)
    return parsed
