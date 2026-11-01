"""The asset DAG and the project-level checks that need no database."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise

from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.model import project_reference_target
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import ResolvedProjectConfig


@dataclass(frozen=True)
class ProjectGraph:
    """The DAG of one repository.

    Parameters
    ----------
    edges
        ``(upstream, downstream)`` pairs between assets of this repository,
        sorted. Self-references are omitted. A target of `references:` that the
        repository builds is an upstream too: it is built first.
    external_inputs
        Every referenced table this repository does not produce, sorted.
    """

    edges: tuple[tuple[str, str], ...]
    external_inputs: tuple[str, ...]


_REFERENCE_HINT = (
    " (a reference in `references:` counts as a dependency: the target is built first); remove "
    "one of the references, or check it with a hand-written check under `checks/`"
)
"""Appended to a cycle that only the header's references close."""


def _upstream(asset: ParsedAsset, produced: Mapping[str, tuple[str, ...] | None]) -> set[str]:
    """List what an asset must wait for: the tables it reads and the targets it references.

    Parameters
    ----------
    asset
        The asset.
    produced
        The tables the repository builds, each with its columns (`None` when not known).

    Returns
    -------
    set of str
        The tables, never the asset's own. A reference target the repository does not build adds
        nothing: it is no input of the SQL, and the deploy resolves it on its own.
    """
    targets = {
        target
        for raw in asset.metadata.reference_targets().values()
        if (target := project_reference_target(raw, produced)) is not None
    }
    return {*asset.references, *targets} - {asset.table}


def _produced(assets: Sequence[ParsedAsset]) -> dict[str, tuple[str, ...] | None]:
    """Map each table the repository builds to its columns.

    Parameters
    ----------
    assets
        The parsed assets.

    Returns
    -------
    dict
        Table → its columns, `None` for a star projection or a Python asset.
    """
    return {asset.table: asset.columns for asset in assets}


def build_graph(assets: Sequence[ParsedAsset]) -> ProjectGraph:
    """Split every asset's references into DAG edges and external inputs.

    Parameters
    ----------
    assets
        The parsed assets.

    Returns
    -------
    ProjectGraph
        Edges and external inputs.
    """
    produced = _produced(assets)
    edges: set[tuple[str, str]] = set()
    external: set[str] = set()
    for asset in assets:
        for reference in _upstream(asset, produced):
            if reference in produced:
                edges.add((reference, asset.table))
            else:
                external.add(reference)
    return ProjectGraph(edges=tuple(sorted(edges)), external_inputs=tuple(sorted(external)))


def find_cycle(assets: Sequence[ParsedAsset]) -> list[str] | None:
    """Find one dependency cycle, if any.

    Walks assets in table-name order, depth first along their upstreams in
    name order, so the reported cycle is deterministic.

    Parameters
    ----------
    assets
        The parsed assets.

    Returns
    -------
    list of str or None
        The cycle's tables, the first repeated at the end; `None` if acyclic.
    """
    produced = _produced(assets)
    upstream = {asset.table: sorted(_upstream(asset, produced)) for asset in assets}
    done: set[str] = set()
    for root in sorted(upstream):
        if root in done:
            continue
        path = [root]
        on_path = {root}
        frames = [iter(upstream[root])]
        while frames:
            for reference in frames[-1]:
                if reference in on_path:
                    return [*path[path.index(reference) :], reference]
                if reference in done or reference not in upstream:
                    continue
                path.append(reference)
                on_path.add(reference)
                frames.append(iter(upstream[reference]))
                break
            else:
                frames.pop()
                finished = path.pop()
                on_path.discard(finished)
                done.add(finished)
    return None


def _field_present(asset: ParsedAsset, field: str) -> bool:
    """Report whether a required field is set on merged metadata.

    Parameters
    ----------
    asset
        The asset.
    field
        ``description``, ``owner`` or ``tags``.

    Returns
    -------
    bool
        Whether it has a non-empty value.
    """
    return bool(getattr(asset.metadata, field))


def validate_project(
    assets: Sequence[ParsedAsset], config: ResolvedProjectConfig
) -> list[ProjectIssue]:
    """Run every check that needs only the repository itself.

    Parameters
    ----------
    assets
        The parsed assets.
    config
        `lakana.yaml` resolved for the target environment.

    Returns
    -------
    list of ProjectIssue
        Every problem: a cycle, missing required fields, documented columns
        the query does not produce, and undocumented columns under
        `column_docs`.
    """
    issues: list[ProjectIssue] = []
    by_table = {asset.table: asset for asset in assets}
    cycle = find_cycle(assets)
    if cycle is not None:
        first = by_table[cycle[0]]
        steps = pairwise(cycle)
        closed_by_reference = any(up not in by_table[down].references for down, up in steps)
        hint = _REFERENCE_HINT if closed_by_reference else ""
        issues.append(
            ProjectIssue(first.path, first.body_line, "cycle: " + " -> ".join(cycle) + hint)
        )
    for asset in sorted(assets, key=lambda a: a.path):
        line = asset.block_line or asset.body_line
        for field in config.requirements.fields:
            if not _field_present(asset, field):
                issues.append(ProjectIssue(asset.path, line, f"missing required field {field!r}"))
        if asset.columns is None:
            continue
        for documented in asset.metadata.columns:
            if documented not in asset.columns:
                issues.append(
                    ProjectIssue(
                        asset.path,
                        line,
                        f"documents column {documented!r}, which the query does not produce",
                    )
                )
        if config.requirements.column_docs:
            for position, column in enumerate(asset.columns, start=1):
                if not column:
                    issues.append(
                        ProjectIssue(
                            asset.path,
                            line,
                            f"column {position} has no name; alias it so it can be documented",
                        )
                    )
                elif column not in asset.metadata.columns:
                    issues.append(
                        ProjectIssue(asset.path, line, f"column {column!r} is not documented")
                    )
    return issues
