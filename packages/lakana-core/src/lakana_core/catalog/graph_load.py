"""Loading an environment's metadata graph: every node and edge, unfiltered, for one request.

`load_graph` reads, in a fixed number of statements whatever the environment's size, the
environment's tables, declared references and measures, glossary terms and their links, active
row filters and column masks, and the current manifest of every deployed repository. The
manifests give the assets, what each reads (its SQL's `references` and a Python asset's
`inputs`), the checks and the jobs. A column is read only when a walk asks for its table
(`columns_of`, `preload_columns`, in chunks).

Nothing here asks who may see what: `graph_service._Visibility` filters every node before a walk
reaches it. An edge to a column is made from what names the column, and `exists` says whether the
column is there. A stored manifest is untrusted. What cannot be read is left out, never an error
(`manifest_assets`, `read_manifest_checks`). When two repositories' manifests declare one check,
the first repository by name wins. Each asset that builds a table gets its own `builds` edge.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from pyiceberg.types import NestedField
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.catalog.graph_model import (
    EdgeKind,
    GraphEdge,
    NodeKind,
    NodeRef,
    asset_node,
    check_node,
    column_node,
    job_node,
    node_kind,
    row_node,
    table_node,
    table_part,
)
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    ColumnMask,
    DeclaredMeasure,
    DeclaredReference,
    GlossaryTerm,
    GlossaryTermLink,
    Namespace,
    Repository,
    RowFilter,
    Table,
    TableColumn,
    TableType,
)
from lakana_core.policy.shown import shown_policies
from lakana_core.projects.checks import read_manifest_checks
from lakana_core.projects.lookups import current_manifests
from lakana_core.projects.manifest import entry_inputs, manifest_assets, manifest_jobs
from lakana_core.runs.selection import ManifestGraph, Selection, resolve_selection

_CHUNK = 500
"""The most table ids in one `IN` list."""

_TABLE_TYPES = {
    TableType.ICEBERG: "managed",
    TableType.EXTERNAL: "external",
    TableType.VIEW: "view",
    TableType.ARTIFACT: "artifact",
}


@dataclass(frozen=True)
class TableRecord:
    """A table, view or artifact row.

    Parameters
    ----------
    id
        Its id.
    ref
        How it is addressed.
    table_type
        ``managed``, ``external``, ``view`` or ``artifact``.
    model_kind
        Its declared kind.
    grain
        Its declared grain, in the main language.
    key
        Its declared key columns.
    label_project
        The header's label.
    label_edited
        The label edited through the API.
    text_language
        Its main language, or `None` for the instance's.
    """

    id: uuid.UUID
    ref: TableRef
    table_type: str
    model_kind: str | None
    grain: str | None
    key: tuple[str, ...]
    label_project: str | None
    label_edited: str | None
    text_language: str | None


@dataclass(frozen=True)
class AssetRecord:
    """An asset of a repository's current manifest.

    Parameters
    ----------
    id
        Its node id.
    repository
        The repository.
    name
        The dotted table it builds.
    owner
        The header's `owner:`.
    reads
        The dotted tables it reads, sorted, without itself.
    lineage
        ``(its column, source table, source column)`` per source its manifest traces each of its
        columns to, sorted; empty unless the entry lists its columns (a `SELECT` with named
        columns), so only then is it known which columns of a table it reads (#451).
    """

    id: str
    repository: str
    name: str
    owner: str | None
    reads: tuple[str, ...]
    lineage: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class ReferenceRecord:
    """A declared reference (`table_references`).

    Parameters
    ----------
    id
        Its node id.
    source_table_id
        The table that declares it.
    source_column
        The referencing column.
    target_name
        The dotted table it points at, as the header names it.
    target_column
        The target's column.
    """

    id: str
    source_table_id: uuid.UUID
    source_column: str
    target_name: str
    target_column: str


@dataclass(frozen=True)
class MeasureRecord:
    """A declared measure (`table_measures`).

    Parameters
    ----------
    id
        Its node id.
    table_id
        Its table.
    column
        Its column.
    """

    id: str
    table_id: uuid.UUID
    column: str


@dataclass(frozen=True)
class CheckRecord:
    """A check of a repository's current manifest, on an existing table.

    Parameters
    ----------
    id
        Its node id.
    table_id
        The checked table.
    check
        The check's name.
    repository
        The repository whose manifest declares it.
    on_failure
        ``block``, ``keep`` or ``warn``.
    family
        ``key``, ``references`` or ``measures`` for a generated check; `None` otherwise.
    columns
        A generated check's columns.
    target
        A ``references`` check's target table; its visibility gates the check (R3).
    """

    id: str
    table_id: uuid.UUID
    check: str
    repository: str
    on_failure: str
    family: str | None
    columns: tuple[str, ...]
    target: str | None


@dataclass(frozen=True)
class TermRecord:
    """A glossary term.

    Parameters
    ----------
    id
        Its node id.
    row_id
        Its row's id.
    key
        Its id in the glossary.
    owner
        Who answers for it.
    repository
        The repository that owns it.
    """

    id: str
    row_id: uuid.UUID
    key: str
    owner: str
    repository: str


@dataclass(frozen=True)
class PolicyNode:
    """An active row filter or column mask.

    Parameters
    ----------
    id
        Its node id.
    kind
        `NodeKind.ROW_FILTER` or `NodeKind.COLUMN_MASK`.
    table_id
        The table it binds.
    column
        A mask's column; `None` for a row filter.
    """

    id: str
    kind: NodeKind
    table_id: uuid.UUID
    column: str | None


@dataclass(frozen=True)
class JobRecord:
    """A job of a repository's current manifest.

    Parameters
    ----------
    id
        Its node id.
    repository
        The repository.
    name
        The job's name.
    assets
        The assets its selection runs, sorted; empty when it cannot be resolved.
    """

    id: str
    repository: str
    name: str
    assets: tuple[str, ...]


@dataclass(frozen=True)
class ManifestCheck:
    """A check as a manifest declares it, before its table is looked up.

    Parameters
    ----------
    table
        The dotted checked table.
    check
        Its name.
    on_failure
        ``block``, ``keep`` or ``warn``.
    family
        A generated check's family; `None` for a hand-written one.
    columns
        A generated check's columns.
    target
        A ``references`` check's target.
    """

    table: str
    check: str
    on_failure: str
    family: str | None
    columns: tuple[str, ...]
    target: str | None


@dataclass(frozen=True)
class ManifestPart:
    """What one repository's current manifest adds to the graph.

    It depends only on the project version, which never changes, so it may be kept per version
    id (`load_graph`'s `part`).

    Parameters
    ----------
    repository
        The repository.
    assets
        Its assets, by name.
    checks
        Its readable checks.
    jobs
        Its jobs, by name.
    """

    repository: str
    assets: tuple[AssetRecord, ...]
    checks: tuple[ManifestCheck, ...]
    jobs: tuple[JobRecord, ...]


Record = (
    AssetRecord
    | ReferenceRecord
    | MeasureRecord
    | CheckRecord
    | TermRecord
    | PolicyNode
    | JobRecord
)


def _asset(repository: str, name: str, entry: Mapping[str, Any]) -> AssetRecord:
    """Read one asset entry.

    Parameters
    ----------
    repository
        The repository.
    name
        The asset's table.
    entry
        Its manifest entry; untrusted.

    Returns
    -------
    AssetRecord
        The asset.
    """
    raw = entry.get("references")
    reads = {r for r in raw if isinstance(r, str)} if isinstance(raw, list) else set()
    reads |= set(entry_inputs(entry).values())
    metadata = entry.get("metadata")
    owner = metadata.get("owner") if isinstance(metadata, Mapping) else None
    return AssetRecord(
        asset_node(repository, name),
        repository,
        name,
        owner if isinstance(owner, str) else None,
        tuple(sorted(reads - {name})),
        _lineage(entry),
    )


def _lineage(entry: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """Read which source columns an asset entry traces each of its columns to.

    Parameters
    ----------
    entry
        Its manifest entry; untrusted.

    Returns
    -------
    tuple of tuple
        ``(column, source table, source column)``, sorted; empty when the entry does not list
        its columns, or holds no lineage.
    """
    lineage = entry.get("lineage")
    if not isinstance(entry.get("columns"), list) or not isinstance(lineage, Mapping):
        return ()
    found: set[tuple[str, str, str]] = set()
    for column, sources in lineage.items():
        if not isinstance(column, str) or not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            table, read = source.get("table"), source.get("column")
            if isinstance(table, str) and isinstance(read, str):
                found.add((column, table, read))
    return tuple(sorted(found))


def _job(repository: str, name: str, entry: Mapping[str, Any], dag: ManifestGraph) -> JobRecord:
    """Read one job entry and resolve what it runs.

    Parameters
    ----------
    repository
        The repository.
    name
        The job's name.
    entry
        Its manifest entry; untrusted.
    dag
        The manifest's asset graph.

    Returns
    -------
    JobRecord
        The job; it runs nothing when its selection matches no asset or is malformed.
    """
    patterns = entry.get("select")
    selection = Selection(
        patterns=tuple(p for p in patterns if isinstance(p, str))
        if isinstance(patterns, list)
        else (),
        upstream=entry.get("upstream") is True,
        downstream=entry.get("downstream") is True,
    )
    try:
        assets = resolve_selection(dag, selection) if selection.patterns else ()
    except ValidationError:
        assets = ()
    return JobRecord(job_node(repository, name), repository, name, assets)


def manifest_part(repository: str, manifest: object) -> ManifestPart:
    """Read what one repository's current manifest adds to the graph.

    Parameters
    ----------
    repository
        The repository.
    manifest
        Its current manifest; untrusted.

    Returns
    -------
    ManifestPart
        Its assets, checks and jobs; empty for anything that is not a manifest.
    """
    entries = manifest_assets(manifest)
    checks = tuple(
        ManifestCheck(
            spec.table,
            spec.check,
            spec.on_failure,
            spec.generated.family if spec.generated else None,
            spec.generated.columns if spec.generated else (),
            spec.generated.target if spec.generated else None,
        )
        for spec in read_manifest_checks(manifest).specs
    )
    dag = (
        ManifestGraph.from_manifest(manifest)
        if isinstance(manifest, Mapping)
        else ManifestGraph(assets=(), edges=())
    )
    return ManifestPart(
        repository,
        tuple(_asset(repository, name, entry) for name, entry in sorted(entries.items())),
        checks,
        tuple(_job(repository, n, e, dag) for n, e in sorted(manifest_jobs(manifest).items())),
    )


def _uuid(text: str) -> uuid.UUID | None:
    """Read a UUID, or nothing.

    Parameters
    ----------
    text
        Anything a caller sent.

    Returns
    -------
    uuid.UUID or None
        The UUID, or `None` when `text` is not one.
    """
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _splits(value: str) -> Iterator[tuple[str, str]]:
    """Split a ``<table>/<name>`` value at each ``/``, leftmost first.

    Parameters
    ----------
    value
        The value.

    Yields
    ------
    tuple of (str, str)
        The table part and the name part, both non-empty.
    """
    for i, char in enumerate(value):
        if char == "/" and 0 < i < len(value) - 1:
            yield value[:i], value[i + 1 :]


class EnvironmentGraph:
    """Every node and edge of one environment, unfiltered.

    Parameters
    ----------
    session
        Database session, kept for the column reads.
    environment_id
        The environment.
    environment
        Its name.
    """

    def __init__(self, session: Session, environment_id: uuid.UUID, environment: str) -> None:
        self.environment = environment
        self.environment_id = environment_id
        self.tables: dict[uuid.UUID, TableRecord] = {}
        self.table_ids: dict[str, uuid.UUID] = {}
        self.records: dict[str, Record] = {}
        self.builder_of: dict[uuid.UUID, AssetRecord] = {}
        self.column_readers: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
        """``(dotted table, case-folded column)`` → the assets whose lineage reads it."""
        self._term_keys: dict[str, str] = {}
        self._references_by_source: dict[tuple[uuid.UUID, str], str] = {}
        self._adjacent: defaultdict[str, list[GraphEdge]] = defaultdict(list)
        self._columns: dict[uuid.UUID, dict[str, str]] = {}
        self._session = session

    def _link(self, kind: EdgeKind, source: str, target: str) -> None:
        """Add an edge to both of its ends.

        Parameters
        ----------
        kind
            What it says.
        source
            Its source.
        target
            Its target.
        """
        edge = GraphEdge(kind, source, target)
        self._adjacent[source].append(edge)
        self._adjacent[target].append(edge)

    def add_table(self, record: TableRecord) -> None:
        """Add a table.

        Parameters
        ----------
        record
            The table.
        """
        self.tables[record.id] = record
        self.table_ids[record.ref.dotted()] = record.id

    def add_reference(self, record: ReferenceRecord) -> None:
        """Add a reference and its edges; tables first.

        A reference from a table the load did not read (created while the graph loaded) is
        left out, as if it had been created after.

        Parameters
        ----------
        record
            The reference.
        """
        if record.source_table_id not in self.tables:
            return
        self.records[record.id] = record
        self._references_by_source[record.source_table_id, record.source_column] = record.id
        self._link(
            EdgeKind.REFERENCES,
            record.id,
            column_node(record.source_table_id, record.source_column),
        )
        target = self.table_ids.get(record.target_name)
        if target is not None:
            self._link(EdgeKind.REFERENCES, record.id, table_node(target))
            self._link(EdgeKind.REFERENCES, record.id, column_node(target, record.target_column))

    def add_measure(self, record: MeasureRecord) -> None:
        """Add a measure and its edge.

        A measure of a table the load did not read is left out, as a reference is.

        Parameters
        ----------
        record
            The measure.
        """
        if record.table_id not in self.tables:
            return
        self.records[record.id] = record
        self._link(EdgeKind.MEASURES, record.id, column_node(record.table_id, record.column))

    def add_term(self, record: TermRecord, links: list[tuple[str, str | None]]) -> None:
        """Add a term and an edge per link to an existing table.

        Parameters
        ----------
        record
            The term.
        links
            ``(dotted table, column or None)`` per link.
        """
        self.records[record.id] = record
        self._term_keys[record.key] = record.id
        for table, column in links:
            table_id = self.table_ids.get(table)
            if table_id is not None:
                target = table_node(table_id) if column is None else column_node(table_id, column)
                self._link(EdgeKind.LINKS, record.id, target)

    def add_policy(self, record: PolicyNode) -> None:
        """Add a row filter or column mask and its edge.

        A policy on a table the load did not read is left out, as a reference is.

        Parameters
        ----------
        record
            The policy.
        """
        if record.table_id not in self.tables:
            return
        self.records[record.id] = record
        if record.column is None:
            self._link(EdgeKind.FILTERS, record.id, table_node(record.table_id))
        else:
            self._link(EdgeKind.MASKS, record.id, column_node(record.table_id, record.column))

    def add_part(self, part: ManifestPart) -> None:
        """Add one repository's assets, checks and jobs; tables and references first.

        Parameters
        ----------
        part
            The repository's part.
        """
        for asset in part.assets:
            self.records[asset.id] = asset
            built = self.table_ids.get(asset.name)
            if built is not None:
                self.builder_of.setdefault(built, asset)
                self._link(EdgeKind.BUILDS, asset.id, table_node(built))
            for _, source, column in asset.lineage:
                self.column_readers[source, column.casefold()].add(asset.id)
            for read in asset.reads:
                read_id = self.table_ids.get(read)
                if read_id is not None:
                    self._link(EdgeKind.READS, asset.id, table_node(read_id))
        for check in part.checks:
            table_id = self.table_ids.get(check.table)
            if table_id is None or check_node(table_id, check.check) in self.records:
                continue
            record = CheckRecord(
                check_node(table_id, check.check),
                table_id,
                check.check,
                part.repository,
                check.on_failure,
                check.family,
                check.columns,
                check.target if check.family == "references" else None,
            )
            self.records[record.id] = record
            self._link(EdgeKind.CHECKS, record.id, table_node(table_id))
            if check.family == "key":
                for column in check.columns:
                    self._link(EdgeKind.CHECKS, record.id, column_node(table_id, column))
            reference = (
                self._references_by_source.get((table_id, check.columns[0]))
                if check.family == "references" and check.columns
                else None
            )
            if reference is not None:
                self._link(EdgeKind.CHECKS, record.id, reference)
        for job in part.jobs:
            self.records[job.id] = job
            for name in job.assets:
                self._link(EdgeKind.RUNS, job.id, asset_node(part.repository, name))

    def preload_columns(self, table_ids: Collection[uuid.UUID]) -> None:
        """Read the columns of the tables not read yet, in chunks.

        Parameters
        ----------
        table_ids
            The tables.
        """
        missing = sorted({t for t in table_ids if t not in self._columns and t in self.tables})
        for start in range(0, len(missing), _CHUNK):
            chunk = missing[start : start + _CHUNK]
            for table_id in chunk:
                self._columns[table_id] = {}
            rows = self._session.execute(
                select(TableColumn.table_id, TableColumn.name, TableColumn.type)
                .where(TableColumn.table_id.in_(chunk))
                .order_by(TableColumn.table_id, TableColumn.position)
            )
            for table_id, name, kind in rows:
                self._columns[table_id][name] = kind

    def columns_of(self, table_id: uuid.UUID) -> dict[str, str]:
        """Return a table's columns, reading them on first use.

        Parameters
        ----------
        table_id
            The table.

        Returns
        -------
        dict
            Name → type, in schema order; empty for an unknown table or one without stored
            columns.
        """
        self.preload_columns([table_id])
        return self._columns.get(table_id, {})

    def edges(self, node_id: str, kinds: Collection[EdgeKind] = ()) -> list[GraphEdge]:
        """List the edges touching a node, in both directions, unfiltered by visibility.

        Parameters
        ----------
        node_id
            A canonical node id.
        kinds
            The edge kinds the caller wants; every kind when empty. Only the table's
            `has_column` edges, which read its columns, are left out by it: the caller filters
            the rest.

        Returns
        -------
        list of GraphEdge
            Its edges; an edge's other end may not exist (`exists`).
        """
        found = list(self._adjacent.get(node_id, ()))
        kind = node_kind(node_id)
        if kind is NodeKind.TABLE and (not kinds or EdgeKind.HAS_COLUMN in kinds):
            table_id = table_part(node_id)
            found += [
                GraphEdge(EdgeKind.HAS_COLUMN, node_id, column_node(table_id, column))
                for column in self.columns_of(table_id)
            ]
        elif kind is NodeKind.COLUMN:
            found.append(GraphEdge(EdgeKind.HAS_COLUMN, table_node(table_part(node_id)), node_id))
        return found

    def exists(self, node_id: str) -> bool:
        """Say whether a canonical node id names something that is there.

        Parameters
        ----------
        node_id
            A canonical node id.

        Returns
        -------
        bool
            Whether it exists.
        """
        kind = node_kind(node_id)
        if kind is NodeKind.TABLE:
            return table_part(node_id) in self.tables
        if kind is NodeKind.COLUMN:
            table_id = table_part(node_id)
            return table_id in self.tables and node_id.partition("/")[2] in self.columns_of(
                table_id
            )
        return node_id in self.records

    def _table(self, text: str) -> uuid.UUID | None:
        """Find a table by its id or its dotted name.

        Parameters
        ----------
        text
            A UUID or a dotted name.

        Returns
        -------
        uuid.UUID or None
            Its id, or `None`.
        """
        table_id = _uuid(text)
        if table_id is not None:
            return table_id if table_id in self.tables else None
        return self.table_ids.get(text)

    def resolve(self, ref: NodeRef) -> str | None:
        """Find the node a caller named, by id or by a named form (R5).

        Parameters
        ----------
        ref
            What `parse_node` read.

        Returns
        -------
        str or None
            The canonical id of an existing node, or `None`.
        """
        if ref.kind is NodeKind.TABLE:
            table_id = self._table(ref.value)
            return None if table_id is None else table_node(table_id)
        if ref.kind in (NodeKind.COLUMN, NodeKind.CHECK):
            build = column_node if ref.kind is NodeKind.COLUMN else check_node
            for head, tail in _splits(ref.value):
                table_id = self._table(head)
                if table_id is not None and self.exists(build(table_id, tail)):
                    return build(table_id, tail)
            return None
        if ref.kind in (NodeKind.ASSET, NodeKind.JOB):
            node_id = f"{ref.kind.value}:{ref.value}"
            return node_id if node_id in self.records else None
        if ref.kind is NodeKind.TERM and ref.value in self._term_keys:
            return self._term_keys[ref.value]
        row_id = _uuid(ref.value)
        node_id = None if row_id is None else row_node(ref.kind, row_id)
        return node_id if node_id is not None and node_id in self.records else None


def load_graph(
    session: Session,
    environment_id: uuid.UUID,
    environment: str,
    *,
    part: Callable[[uuid.UUID, str, object], ManifestPart] | None = None,
    fields_of: Callable[[uuid.UUID], Sequence[NestedField] | None] | None = None,
    check: Callable[[], None] | None = None,
) -> EnvironmentGraph:
    """Load an environment's graph.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    environment
        Its name.
    part
        Reads one repository's part from ``(project version id, repository, manifest)``;
        `manifest_part` when `None`.
    fields_of
        A table's current top-level fields, or `None` when they cannot be read; called once per
        table that has a column mask bound to field ids. A mask is linked to its column's
        current name, resolved through the same field-id binding a query uses
        (`policy.shown.shown_policies`); with no reader, an unbound mask, or a column that is
        gone, it stays linked to the name it was made with.
    check
        Called after the database rows are read and after each repository's part, so a time
        budget can end the load between those steps (it raises); `None` checks nothing.

    Returns
    -------
    EnvironmentGraph
        The graph.
    """
    graph = EnvironmentGraph(session, environment_id, environment)
    rows = session.execute(
        select(
            Table.id,
            Namespace.path,
            Table.name,
            Table.table_type,
            Table.model_kind,
            Table.model_grain,
            Table.model_key,
            Table.label_project,
            Table.label_edited,
            Table.text_language,
        )
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(Namespace.environment_id == environment_id)
    )
    for row in rows:
        graph.add_table(
            TableRecord(
                row.id,
                TableRef(environment, NamespacePath.parse(row.path), row.name),
                _TABLE_TYPES.get(row.table_type, "managed"),
                row.model_kind,
                row.model_grain,
                tuple(row.model_key) if isinstance(row.model_key, list) else (),
                row.label_project,
                row.label_edited,
                row.text_language,
            )
        )
    for ref in session.scalars(
        select(DeclaredReference).where(DeclaredReference.environment_id == environment_id)
    ):
        graph.add_reference(
            ReferenceRecord(
                row_node(NodeKind.REFERENCE, ref.id),
                ref.source_table_id,
                ref.source_column,
                ref.target_name,
                ref.target_column,
            )
        )
    for measure_id, table_id, column in session.execute(
        select(DeclaredMeasure.id, DeclaredMeasure.table_id, DeclaredMeasure.column).where(
            DeclaredMeasure.environment_id == environment_id
        )
    ):
        graph.add_measure(MeasureRecord(row_node(NodeKind.MEASURE, measure_id), table_id, column))
    links: defaultdict[uuid.UUID, list[tuple[str, str | None]]] = defaultdict(list)
    for term_id, table, column in session.execute(
        select(GlossaryTermLink.term_id, GlossaryTermLink.table_name, GlossaryTermLink.column_name)
        .join(GlossaryTerm, GlossaryTerm.id == GlossaryTermLink.term_id)
        .where(GlossaryTerm.environment_id == environment_id)
        .order_by(GlossaryTermLink.term_id, GlossaryTermLink.position)
    ):
        links[term_id].append((table, column))
    for term_id, key, owner, repository in session.execute(
        select(GlossaryTerm.id, GlossaryTerm.term_key, GlossaryTerm.owner, Repository.name)
        .join(Repository, Repository.id == GlossaryTerm.repository_id)
        .where(GlossaryTerm.environment_id == environment_id)
    ):
        graph.add_term(
            TermRecord(row_node(NodeKind.TERM, term_id), term_id, key, owner, repository),
            links[term_id],
        )
    for filter_id, table_id in session.execute(
        select(RowFilter.id, RowFilter.table_id).where(
            RowFilter.environment_id == environment_id,
            RowFilter.revoked_at.is_(None),
            RowFilter.table_id.is_not(None),
        )
    ):
        graph.add_policy(
            PolicyNode(
                row_node(NodeKind.ROW_FILTER, filter_id),
                NodeKind.ROW_FILTER,
                cast(uuid.UUID, table_id),
                None,
            )
        )
    masks = list(
        session.scalars(
            select(ColumnMask).where(
                ColumnMask.environment_id == environment_id,
                ColumnMask.revoked_at.is_(None),
                ColumnMask.table_id.is_not(None),
            )
        )
    )
    current = shown_policies(masks, fields_of) if fields_of is not None else {}
    for mask in masks:
        shown_mask = current.get(mask.id)
        graph.add_policy(
            PolicyNode(
                row_node(NodeKind.COLUMN_MASK, mask.id),
                NodeKind.COLUMN_MASK,
                cast(uuid.UUID, mask.table_id),
                shown_mask.column if shown_mask and shown_mask.column else mask.column,
            )
        )
    read = part or (lambda _version, repository, manifest: manifest_part(repository, manifest))
    looked = check or (lambda: None)
    looked()
    for version_id, repository, manifest in current_manifests(session, environment_id):
        graph.add_part(read(version_id, repository, manifest))
        looked()
    return graph
