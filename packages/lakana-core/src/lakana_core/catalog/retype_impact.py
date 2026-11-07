"""What other repositories use a column a run retyped, recorded at that run (#446).

A deploy cannot tell that a column's type changes: a manifest records no types. The run that
replaces a column (its type changed in a way that does not widen, `policy.schema_change.REPLACED`)
can. When it does in a task run, the write asks the metadata graph what names or reads each such
column (`GraphService.breakage`: references, measures, checks, glossary terms, and assets whose
lineage reads it, with what names the columns they take from it) and stores what belongs to other
repositories than the run's on the task run, beside the policies of its schema change
(`TaskRun.schema_change_json`, key ``impact``).

The graph is asked seeing every table and asset (`_SeesTheGraph`, which permits no action),
and each use is stored with the tables it names: what a
reader is shown is decided when the run is read (`impact_for_reader`), by the deploy warning's rule
(`READ_ENVIRONMENT`, and `READ_TABLE` on the retyped table and every table a use names). A use a
reader may not see is left out without a count, and a column whose every use is left out gets no
warning, so nothing tells such a reader that it exists.

The write never fails for this: the computation has a 2 s budget, runs in a savepoint, and any
error or a budget run out records, per column, that the impact could not be computed.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef, Securable
from lakana_core.catalog.graph_model import NodeKind, parse_node
from lakana_core.catalog.graph_service import GraphService
from lakana_core.catalog.warning_text import named, typed
from lakana_core.errors import PermissionDenied, ValidationError
from lakana_core.identifiers import shown
from lakana_core.models import Repository, Run, TaskRun

logger = logging.getLogger(__name__)

RETYPE_BUDGET_SECONDS = 2.0
"""The time budget of the computation: it runs inside a write, so it stays short."""

USE_WORDS = {
    NodeKind.ASSET.value: "asset",
    NodeKind.REFERENCE.value: "reference",
    NodeKind.MEASURE.value: "measure",
    NodeKind.CHECK.value: "check",
    NodeKind.TERM.value: "glossary term",
}
"""The kinds of use stored, with the word a warning names each by (`graph_impact.WARNING_KINDS`)."""

RETYPE_WARNING = (
    "This run changed the type of column `{column}` of `{table}`, which other repositories use; "
    "tell their owners, and see everything it affects with: lakana graph impact --env {env} "
    "{command} — what uses it: {items}"
)
"""A retyped column that other repositories use. The next step comes before the list, which has
no bound, as in the deploy's `IMPACT_WARNING`."""

RETYPE_UNCHECKED = (
    "This run changed the type of column `{column}` of `{table}`, and Lakana could not check in "
    "time what other repositories use it; see it with: lakana graph impact --env {env} {command}"
)
"""The computation failed or ran past its budget: the write went on, and this says so."""

RETYPE_UNREADABLE = (
    "This run changed the type of column `{column}` of `{table}`, whose name holds a control or "
    "format character or is too long to look up in the metadata graph, so Lakana did not check "
    "what other repositories use it; rename the column, or see what depends on its table with: "
    "lakana graph impact --env {env} {command}"
)
"""The column's name is no graph node's: it alone goes unchecked, as in the deploy's
`IMPACT_UNREADABLE`."""


_SEEN = frozenset({Action.READ_TABLE, Action.READ_ENVIRONMENT})
"""What `_SeesTheGraph` permits: the two rights the graph's visibility asks besides policies."""


class _SeesTheGraph:
    """Let the metadata graph see every table, asset, job and term, and permit nothing else.

    The uses are filtered per reader when the run is read, so they are found with all of them in
    sight. Only `READ_TABLE` and `READ_ENVIRONMENT` are permitted; `MANAGE_POLICY` is not, so no
    policy node is reached (the warning lists none), and `authorize` refuses everything, so this
    service can never let an action through.
    """

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer a visibility question.

        Parameters
        ----------
        principal
            Ignored.
        action
            The action asked.
        securable
            Ignored.

        Returns
        -------
        bool
            Whether `action` is one of `_SEEN`.
        """
        return action in _SEEN

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse every action: this service only answers what the graph may see.

        Parameters
        ----------
        principal
            The actor.
        action
            The action requested.
        securable
            The object.

        Raises
        ------
        PermissionDenied
            Always.
        """
        raise PermissionDenied(f"the retype check may not {action.value}")


@dataclass(frozen=True)
class ImpactUseView:
    """One use of a retyped column by another repository.

    Parameters
    ----------
    kind
        The node kind: ``asset``, ``reference``, ``measure``, ``check`` or ``term``.
    name
        Its name, as the metadata graph shows it.
    repository
        Its repository.
    """

    kind: str
    name: str
    repository: str


@dataclass(frozen=True)
class ImpactWarningView:
    """A retyped column and what other repositories use it for, as one reader is shown it.

    Parameters
    ----------
    table
        The retyped column's table.
    column
        The retyped column.
    uses
        What the reader may see of its uses, by kind, name and repository; empty when the
        impact could not be computed.
    message
        One paragraph: what happened, the next step, and the uses.
    """

    table: str
    column: str
    uses: tuple[ImpactUseView, ...]
    message: str


def retyped_columns(fields: Iterable[str], changed: Mapping[str, str], replaced: str) -> list[str]:
    """Name the columns a schema change replaced, as the table spelled them before it.

    Parameters
    ----------
    fields
        The names of the table's fields before the change, in schema order.
    changed
        `policy.schema_change.changed_columns`: case-folded name to its change.
    replaced
        The change that is a retype (`REPLACED`).

    Returns
    -------
    list of str
        The replaced columns, in schema order.
    """
    return [name for name in fields if changed.get(name.casefold()) == replaced]


def run_repository(session: Session, task_run_id: uuid.UUID) -> str | None:
    """Name the repository of the run a task run belongs to.

    Parameters
    ----------
    session
        Database session.
    task_run_id
        The task run a write is executed for.

    Returns
    -------
    str or None
        The repository's name; `None` when no such task run exists.
    """
    return session.scalar(
        select(Repository.name)
        .join(Run, Run.repository_id == Repository.id)
        .join(TaskRun, TaskRun.run_id == Run.id)
        .where(TaskRun.id == task_run_id)
    )


def retype_impact(
    session: Session,
    p: PrincipalRef,
    environment: str,
    table: str,
    columns: Sequence[str],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, object], ...]:
    """Find what other repositories than the run's use each retyped column, to store it.

    Never raises: an error, or the budget run out, gives one ``unchecked`` entry per column.
    The graph is read in a savepoint, so a database error inside it leaves the write's
    transaction usable.

    Parameters
    ----------
    session
        Database session.
    p
        The writer, in a task run. The graph is asked seeing every table (`_SeesTheGraph`), and
        the stored entries are filtered per reader when the run is read.
    environment
        The environment's name.
    table
        The retyped columns' table, dotted.
    columns
        The retyped columns.
    clock
        Seconds, monotonic; a test passes its own.

    Returns
    -------
    tuple of dict
        Per use ``{"table", "column", "kind", "name", "repository", "tables"}``, by column,
        then by the word for its kind and its name; per column that could not be checked
        ``{"table", "column", "unchecked": true}``; empty outside a run or without a column.
    """
    if p.run_context is None or not columns:
        return ()
    nodes = {column: f"column:{table}/{column}" for column in columns}
    readable = {}
    unreadable = set()
    for column, node in nodes.items():
        try:
            parse_node(node)
            readable[column] = node
        except ValidationError:
            unreadable.add(column)
    try:
        with session.begin_nested():
            own = run_repository(session, p.run_context.task_run_id)
            graph = GraphService(
                session,
                _SeesTheGraph(),
                clock=clock,
                budget_seconds=RETYPE_BUDGET_SECONDS,
            )
            answers = graph.breakage(p, environment, list(readable.values())) if readable else {}
    except Exception as exc:  # a run never fails for a warning
        logger.warning(
            "What other repositories use the retyped columns of %s in %s could not be checked "
            "(%s).",
            shown(table),
            shown(environment),
            type(exc).__name__,
        )
        return tuple(
            {"table": table, "column": c, "unchecked": True}
            | ({"unreadable": True} if c in unreadable else {})
            for c in columns
        )
    entries: list[dict[str, object]] = []
    for column in columns:
        if column not in readable:
            entries.append(
                {"table": table, "column": column, "unchecked": True, "unreadable": True}
            )
            continue
        uses = sorted(
            (USE_WORDS[g.kind.value], g.kind.value, g.name, g.repository, sorted({table, *tables}))
            for g, tables in answers.get(readable[column], ())
            if g.repository is not None and g.repository != own and g.kind.value in USE_WORDS
        )
        entries.extend(
            {
                "table": table,
                "column": column,
                "kind": kind,
                "name": name,
                "repository": repository,
                "tables": tables,
            }
            for _, kind, name, repository, tables in uses
        )
    return tuple(entries)


@dataclass(frozen=True)
class _Entry:
    """One stored entry, read back.

    Parameters
    ----------
    table
        The retyped column's table.
    column
        The column.
    use
        The use; `None` for an entry that says the impact could not be computed.
    tables
        The tables the entry names, the retyped one included.
    unreadable
        For an entry without a use: the column's name is no graph node's, rather than the check
        having run out of time.
    """

    table: str
    column: str
    use: ImpactUseView | None
    tables: tuple[str, ...]
    unreadable: bool = False


def stored_entries(stored: object) -> list[_Entry]:
    """Read the impact entries of a task's stored schema change; anything else is skipped.

    Parameters
    ----------
    stored
        `TaskRun.schema_change_json`.

    Returns
    -------
    list
        The entries, in stored order.
    """
    raw = stored.get("impact") if isinstance(stored, Mapping) else None
    found: list[_Entry] = []
    for entry in raw if isinstance(raw, list) else ():
        if not isinstance(entry, Mapping):
            continue
        table, column = entry.get("table"), entry.get("column")
        if not (isinstance(table, str) and isinstance(column, str)):
            continue
        if entry.get("unchecked") is True:
            found.append(_Entry(table, column, None, (table,), entry.get("unreadable") is True))
            continue
        kind, name, repository = entry.get("kind"), entry.get("name"), entry.get("repository")
        tables = entry.get("tables")
        if (
            kind in USE_WORDS
            and isinstance(name, str)
            and isinstance(repository, str)
            and isinstance(tables, list)
            and all(isinstance(t, str) for t in tables)
        ):
            use = ImpactUseView(kind, name, repository)
            found.append(_Entry(table, column, use, (table, *tables)))
    return found


def impact_for_reader(
    entries: Sequence[_Entry],
    environment: str,
    may_read: Callable[[str], bool],
    *,
    reads_environment: bool,
) -> tuple[ImpactWarningView, ...]:
    """Show a task's retyped columns' uses to one reader.

    Parameters
    ----------
    entries
        `stored_entries` of the task.
    environment
        The environment's name, for the hint.
    may_read
        Whether the reader may read a dotted table.
    reads_environment
        Whether the reader holds `READ_ENVIRONMENT`; without it nothing is shown (a use names
        a repository, a term or an asset).

    Returns
    -------
    tuple of ImpactWarningView
        One per retyped column with a use the reader may see, or that could not be checked, in
        stored order; empty for a reader who may see none.
    """
    if not reads_environment:
        return ()
    grouped: dict[tuple[str, str], list[ImpactUseView]] = {}
    unchecked: set[tuple[str, str]] = set()
    unreadable: set[tuple[str, str]] = set()
    for entry in entries:
        if not all(may_read(t) for t in entry.tables):
            continue
        key = (entry.table, entry.column)
        grouped.setdefault(key, [])
        if entry.use is None:
            unchecked.add(key)
            if entry.unreadable:
                unreadable.add(key)
        else:
            grouped[key].append(entry.use)
    views: list[ImpactWarningView] = []
    for (table, column), uses in grouped.items():
        names = {"column": named(column), "table": named(table), "env": environment}
        command = typed(table, column)
        if (table, column) in unchecked:
            if (table, column) in unreadable:
                message = RETYPE_UNREADABLE.format(command=command, **names)
            else:
                message = RETYPE_UNCHECKED.format(command=command, **names)
            views.append(ImpactWarningView(table, column, (), message))
            continue
        items = ", ".join(f"{USE_WORDS[u.kind]} `{u.name}` ({u.repository})" for u in uses)
        message = RETYPE_WARNING.format(command=command, items=items, **names)
        views.append(ImpactWarningView(table, column, tuple(uses), message))
    return tuple(views)
