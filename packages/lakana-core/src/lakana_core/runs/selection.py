"""Which assets a run covers: patterns, upstream/downstream closures, resume.

Patterns are case-sensitive `fnmatch` globs over dotted table names. ``*`` also
matches dots, so ``gold.*`` selects every table under ``gold`` including nested
namespaces (``gold.sub.t``) and ``*.daily`` matches ``a.b.daily``. The docs task
must carry this into the user documentation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase

from lakana_core.errors import ValidationError

MAX_PATTERNS = 100
"""Most `--select` patterns one run may name."""

MAX_PATTERN_LENGTH = 255
"""Longest single pattern."""


@dataclass(frozen=True)
class Selection:
    """What the caller asked to run.

    Parameters
    ----------
    patterns
        Case-sensitive shell globs over dotted table names; empty means all.
    upstream
        Add every ancestor of the matched assets.
    downstream
        Add every descendant of the matched assets.
    """

    patterns: tuple[str, ...] = ()
    upstream: bool = False
    downstream: bool = False

    def to_json(self) -> dict[str, object]:
        """Serialize for `runs.selection_json`.

        Returns
        -------
        dict
            ``patterns``, ``upstream``, ``downstream``.
        """
        return {
            "patterns": list(self.patterns),
            "upstream": self.upstream,
            "downstream": self.downstream,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> Selection:
        """Read a stored selection, tolerating a malformed value.

        Parameters
        ----------
        data
            A `runs.selection_json` value.

        Returns
        -------
        Selection
            The selection; fields of the wrong type read as their defaults.
        """
        patterns = data.get("patterns")
        upstream = data.get("upstream")
        downstream = data.get("downstream")
        return cls(
            patterns=(
                tuple(p for p in patterns if isinstance(p, str))
                if isinstance(patterns, list)
                else ()
            ),
            upstream=upstream if isinstance(upstream, bool) else False,
            downstream=downstream if isinstance(downstream, bool) else False,
        )


def selection_of(entry: Mapping[str, object]) -> Selection:
    """Read the selection of a manifest's job entry.

    Parameters
    ----------
    entry
        The job's entry (`lakana_core.projects.manifest.manifest_jobs`); untrusted.

    Returns
    -------
    Selection
        Its ``select`` patterns that are strings, and ``upstream``/``downstream`` when they
        are `True`.
    """
    patterns = entry.get("select")
    return Selection(
        patterns=tuple(x for x in patterns if isinstance(x, str))
        if isinstance(patterns, list)
        else (),
        upstream=entry.get("upstream") is True,
        downstream=entry.get("downstream") is True,
    )


@dataclass(frozen=True)
class ManifestGraph:
    """The DAG of one project version, read from its manifest.

    Parameters
    ----------
    assets
        Every asset's dotted table name, sorted.
    edges
        ``(upstream, downstream)`` pairs between assets.
    """

    assets: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, object]) -> ManifestGraph:
        """Build the graph, skipping entries of the wrong shape.

        Parameters
        ----------
        manifest
            A project version's `manifest_json`.

        Returns
        -------
        ManifestGraph
            Assets with a string ``table``; edges between two such assets.
        """
        raw_assets = manifest.get("assets")
        tables = sorted(
            {
                entry["table"]
                for entry in (raw_assets if isinstance(raw_assets, list) else [])
                if isinstance(entry, dict) and isinstance(entry.get("table"), str)
            }
        )
        known = set(tables)
        raw_edges = manifest.get("edges")
        edges = tuple(
            (edge[0], edge[1])
            for edge in (raw_edges if isinstance(raw_edges, list) else [])
            if isinstance(edge, list)
            and len(edge) == 2
            and isinstance(edge[0], str)
            and isinstance(edge[1], str)
            and edge[0] in known
            and edge[1] in known
        )
        return cls(assets=tuple(tables), edges=edges)

    def upstream_of(self, asset: str) -> frozenset[str]:
        """Return the direct upstream assets of `asset`.

        Parameters
        ----------
        asset
            A dotted table name.

        Returns
        -------
        frozenset of str
            Its direct upstreams.
        """
        return frozenset(up for up, down in self.edges if down == asset)

    def downstream_of(self, asset: str) -> frozenset[str]:
        """Return the direct downstream assets of `asset`.

        Parameters
        ----------
        asset
            A dotted table name.

        Returns
        -------
        frozenset of str
            Its direct downstreams.
        """
        return frozenset(down for up, down in self.edges if up == asset)


def _closure(seeds: Iterable[str], step: Callable[[str], frozenset[str]]) -> set[str]:
    """Follow `step` from `seeds` until nothing new is reached.

    Parameters
    ----------
    seeds
        Start nodes (included in the result).
    step
        ``ManifestGraph.upstream_of`` or ``.downstream_of``, bound.

    Returns
    -------
    set of str
        Every node reached.
    """
    seen: set[str] = set()
    frontier = list(seeds)
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        frontier.extend(step(node))
    return seen


def _validate_patterns(patterns: tuple[str, ...]) -> None:
    """Refuse an unusable pattern list before matching anything.

    Parameters
    ----------
    patterns
        The requested patterns.

    Raises
    ------
    ValidationError
        For more than `MAX_PATTERNS` patterns, or an empty, blank or overlong one.
    """
    if len(patterns) > MAX_PATTERNS:
        raise ValidationError(f"at most {MAX_PATTERNS} --select patterns are allowed")
    for pattern in patterns:
        if not pattern.strip():
            raise ValidationError("a --select pattern must not be empty")
        if len(pattern) > MAX_PATTERN_LENGTH:
            raise ValidationError(
                f"a --select pattern must be at most {MAX_PATTERN_LENGTH} characters"
            )


def resolve_selection(graph: ManifestGraph, selection: Selection) -> tuple[str, ...]:
    """Resolve a selection to the sorted assets the run covers.

    Parameters
    ----------
    graph
        The project version's DAG.
    selection
        Patterns and closure flags.

    Returns
    -------
    tuple of str
        Sorted dotted table names, never empty.

    Raises
    ------
    ValidationError
        If the project version has no assets, the pattern list is malformed, or a
        pattern matches no asset (with a hint when it names a namespace).
    """
    if not graph.assets:
        raise ValidationError("the deployed project version has no assets to run")
    _validate_patterns(selection.patterns)
    if not selection.patterns:
        return graph.assets
    matched: set[str] = set()
    for pattern in selection.patterns:
        hits = {asset for asset in graph.assets if fnmatchcase(asset, pattern)}
        if not hits:
            message = f"--select {pattern!r} matches no asset"
            if any(asset.startswith(f"{pattern}.") for asset in graph.assets):
                message += f"; did you mean '{pattern}.*'?"
            raise ValidationError(message)
        matched |= hits
    result = set(matched)
    if selection.upstream:
        result |= _closure(matched, graph.upstream_of)
    if selection.downstream:
        result |= _closure(matched, graph.downstream_of)
    return tuple(sorted(result))


def downstream_closure(graph: ManifestGraph, seeds: Iterable[str]) -> tuple[str, ...]:
    """Return `seeds` that are assets plus everything downstream of them.

    Parameters
    ----------
    graph
        The project version's DAG.
    seeds
        Assets to start from; names not in the graph are ignored.

    Returns
    -------
    tuple of str
        Sorted.
    """
    known = set(graph.assets)
    return tuple(sorted(_closure((s for s in seeds if s in known), graph.downstream_of)))


def selected_upstreams(graph: ManifestGraph, selected: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Map each selected asset to its direct upstreams inside the selection.

    Parameters
    ----------
    graph
        The project version's DAG.
    selected
        The run's assets.

    Returns
    -------
    dict
        Asset → sorted selected upstreams (`task_runs.upstream_json`).
    """
    chosen = set(selected)
    return {asset: tuple(sorted(graph.upstream_of(asset) & chosen)) for asset in sorted(chosen)}


def unselected_upstreams(
    graph: ManifestGraph, asset: str, selected: Iterable[str]
) -> tuple[str, ...]:
    """Return the direct upstream assets of `asset` that the run does not build.

    Parameters
    ----------
    graph
        The project version's DAG.
    asset
        A selected asset.
    selected
        The run's assets.

    Returns
    -------
    tuple of str
        Sorted; each must already exist when the task runs.
    """
    return tuple(sorted(graph.upstream_of(asset) - set(selected)))
