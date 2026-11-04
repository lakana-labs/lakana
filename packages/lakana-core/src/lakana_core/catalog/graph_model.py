"""The metadata graph's vocabulary: node and edge kinds, node ids, answers, limits and texts.

A node id is stable, and clients may store it and link to it (#374, spec §2): ``table:<uuid>``,
``column:<uuid>/<name>``, ``asset:<repository>/<name>``, ``reference:<uuid>``,
``measure:<uuid>``, ``term:<uuid>``, ``check:<uuid>/<name>``, ``row_filter:<uuid>``,
``column_mask:<uuid>`` and ``job:<repository>/<name>``. A caller may also name a node by name
(`parse_node`: a bare dotted table, ``table:gold.dim_store``, ``column:gold.dim_store/store_id``,
``check:gold.fact_sales/ref_store_id``, ``term:net_revenue``); answers always carry the ids.
"""

from __future__ import annotations

import enum
import unicodedata
import uuid
from dataclasses import dataclass

from lakana_core.errors import ValidationError
from lakana_core.identifiers import shown

DEFAULT_GRAPH_DEPTH = 3
"""Steps `lineage` walks when the caller names none."""

MAX_GRAPH_DEPTH = 10
"""The most steps `lineage` walks."""

MAX_GRAPH_PAGE = 500
"""The most nodes of one page, and the default page size."""

MAX_GRAPH_STEPS = 200_000
"""The most edges to visible nodes one request may follow (R8); past it, a `422`."""

GRAPH_TIME_BUDGET_SECONDS = 10.0
"""The longest one request may load and walk (R8); past it, a `503`."""

MAX_NODE_ID = 2048
"""The longest text a caller may send as a node."""

MAX_JOIN_HOPS = 4
"""The most references one join path follows (Q3)."""

MAX_JOIN_PATHS = 10
"""The most join paths one answer returns (Q3)."""


class NodeKind(enum.StrEnum):
    """What a node is; the value is its id's prefix."""

    TABLE = "table"
    COLUMN = "column"
    ASSET = "asset"
    REFERENCE = "reference"
    MEASURE = "measure"
    TERM = "term"
    CHECK = "check"
    ROW_FILTER = "row_filter"
    COLUMN_MASK = "column_mask"
    JOB = "job"


class EdgeKind(enum.StrEnum):
    """What an edge says, from its source to its target."""

    BUILDS = "builds"
    CHECKS = "checks"
    FILTERS = "filters"
    HAS_COLUMN = "has_column"
    LINKS = "links"
    MASKS = "masks"
    MEASURES = "measures"
    READS = "reads"
    REFERENCES = "references"
    RUNS = "runs"


class LineageWay(enum.StrEnum):
    """Which way `lineage` walks from its start."""

    UP = "up"
    DOWN = "down"
    BOTH = "both"


KIND_WORDS: dict[NodeKind, str] = {
    NodeKind.TABLE: "table",
    NodeKind.COLUMN: "column",
    NodeKind.ASSET: "asset",
    NodeKind.REFERENCE: "reference",
    NodeKind.MEASURE: "measure",
    NodeKind.TERM: "glossary term",
    NodeKind.CHECK: "check",
    NodeKind.ROW_FILTER: "row filter",
    NodeKind.COLUMN_MASK: "column mask",
    NodeKind.JOB: "job",
}
"""How a refusal names a kind of node."""

BAD_NODE = (
    "`{node}` is not a node id. Give a table's dotted name, such as gold.dim_store, or a node id "
    "from an answer of lakana graph: it starts with table:, column:, asset:, reference:, "
    "measure:, term:, check:, row_filter:, column_mask: or job:."
)
NO_NODE = (
    "There is no node `{node}` in environment `{env}` that you may see. Give a table as "
    "gold.dim_store and a column as column:gold.dim_store/store_id (--column store_id in the "
    "CLI); find a table with: lakana search TEXT --env {env}"
)
"""The answer for a node that does not exist and for one the caller may not see: the same."""
NO_ENVIRONMENT = "Environment {env!r} does not exist. List them with 'lakana env list'."
"""Search's text, for a caller who may read environments."""
BAD_DEPTH = "depth must be between 1 and 10; got {depth}. Ask for fewer steps, such as --depth 3."
BAD_LIMIT = (
    "The page size must be between 1 and 500 (`limit` in the REST API, `page_size` in the SDK); "
    "got {limit}."
)
BAD_EDGE_KIND = (
    "`{kind}` is not an edge kind. Use one of: builds, checks, filters, has_column, links, "
    "masks, measures, reads, references, runs."
)
LINEAGE_KINDS = (
    "Lineage starts at a table, a column or an asset, and `{node}` is a {word}. Ask for its "
    "neighbours instead: lakana graph neighbours {node} --env {env}"
)
TOO_LARGE = (
    "This question reaches more of the graph than one request may walk ({steps} edges). Ask a "
    "narrower one: a smaller --depth or one --direction for lineage, or --kind for neighbours; "
    "instead of impact, lineage --direction down with a small --depth."
)
BUSY = (
    "The graph walk took longer than {seconds:g} s and was stopped. Try again in a moment; if it "
    "keeps happening, ask a narrower question (a smaller --depth, one --direction, or --kind)."
)
IMPACT_KINDS = (
    "Impact is asked of a table or a column, and `{node}` is a {word}. Ask for its neighbours "
    "instead: lakana graph neighbours {node} --env {env}"
)
KEY_NEEDS_TABLE = (
    "key=true asks for the impact of a table's key, and `{node}` is a column. Ask for the column "
    "without --key, or for its table with --key."
)
NO_KEY = (
    "`{table}` declares no key, so its key has no impact to show. Declare one in the asset header "
    "(key: [column, ...]), or ask for the whole table: lakana graph impact {table} --env {env}"
)
JOIN_KINDS = (
    "A join path runs between two tables, and `{node}` is a {word}. Give the table's dotted name, "
    "such as gold.fact_sales."
)
SAME_TABLE = (
    "from and to are the same table, `{table}`, and a join path joins two tables. To see what it "
    "joins to, ask: lakana graph neighbours {table} --kind references --env {env}"
)
NO_PATH = (
    "No declared path of at most 4 references joins `{start}` and `{goal}`. Declare the reference "
    "in the asset header of one of them (references: {{column: table}}), or check the names with: "
    "lakana graph neighbours {start} --kind references --env {env}"
)
"""The answer when no path exists and when every path runs through a hidden node: the same."""

_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_PREFIXES = {kind.value: kind for kind in NodeKind}


@dataclass(frozen=True)
class NodeRef:
    """A node as a request names it, before it is looked up.

    Parameters
    ----------
    kind
        The kind its prefix names; `NodeKind.TABLE` for a bare dotted name.
    value
        What follows the prefix: an id's parts or a named form.
    """

    kind: NodeKind
    value: str


def parse_node(text: str) -> NodeRef:
    """Read the node a caller sent, without looking it up.

    Parameters
    ----------
    text
        A node id, a named form, or a bare dotted table name.

    Returns
    -------
    NodeRef
        Its kind and the rest.

    Raises
    ------
    ValidationError
        If it is empty, longer than `MAX_NODE_ID`, has a control, format or surrogate character
        or a line separator, or has an unknown prefix or nothing after one (`BAD_NODE`).
    """
    refused = ValidationError(BAD_NODE.format(node=shown(text)))
    if not text or len(text) > MAX_NODE_ID:
        raise refused
    if any(unicodedata.category(c) in _UNSAFE for c in text):
        raise refused
    prefix, colon, value = text.partition(":")
    if not colon:
        return NodeRef(NodeKind.TABLE, text)
    kind = _PREFIXES.get(prefix)
    if kind is None or not value:
        raise refused
    return NodeRef(kind, value)


def node_kind(node_id: str) -> NodeKind:
    """Read the kind of a canonical node id.

    Parameters
    ----------
    node_id
        An id this module built.

    Returns
    -------
    NodeKind
        Its kind.
    """
    return NodeKind(node_id.partition(":")[0])


def table_part(node_id: str) -> uuid.UUID:
    """Read the table of a canonical table, column or check id.

    Parameters
    ----------
    node_id
        ``table:<uuid>``, ``column:<uuid>/<name>`` or ``check:<uuid>/<name>``.

    Returns
    -------
    uuid.UUID
        The table's id.
    """
    return uuid.UUID(node_id.partition(":")[2].partition("/")[0])


def table_node(table_id: uuid.UUID) -> str:
    """Build a table's id.

    Parameters
    ----------
    table_id
        The table.

    Returns
    -------
    str
        ``table:<uuid>``.
    """
    return f"table:{table_id}"


def column_node(table_id: uuid.UUID, column: str) -> str:
    """Build a column's id.

    Parameters
    ----------
    table_id
        Its table.
    column
        Its name.

    Returns
    -------
    str
        ``column:<uuid>/<name>``.
    """
    return f"column:{table_id}/{column}"


def asset_node(repository: str, name: str) -> str:
    """Build an asset's id.

    Parameters
    ----------
    repository
        The repository whose current manifest declares it.
    name
        The asset's name, the dotted table it builds.

    Returns
    -------
    str
        ``asset:<repository>/<name>``.
    """
    return f"asset:{repository}/{name}"


def row_node(kind: NodeKind, row_id: uuid.UUID) -> str:
    """Build the id of a node stored as a row: a reference, measure, term or policy.

    Parameters
    ----------
    kind
        Its kind.
    row_id
        The row's id.

    Returns
    -------
    str
        ``<kind>:<uuid>``.
    """
    return f"{kind.value}:{row_id}"


def check_node(table_id: uuid.UUID, check: str) -> str:
    """Build a check's id.

    Parameters
    ----------
    table_id
        The checked table.
    check
        The check's name.

    Returns
    -------
    str
        ``check:<uuid>/<name>``.
    """
    return f"check:{table_id}/{check}"


def job_node(repository: str, name: str) -> str:
    """Build a job's id.

    Parameters
    ----------
    repository
        The repository whose current manifest declares it.
    name
        The job's name.

    Returns
    -------
    str
        ``job:<repository>/<name>``.
    """
    return f"job:{repository}/{name}"


@dataclass(frozen=True)
class GraphNode:
    """A node as an answer shows it, with the attributes its reader may see.

    Parameters
    ----------
    id
        Its stable id.
    kind
        What it is.
    name
        A name a person reads: a dotted table, ``<table>.<column>``, a term's key.
    distance
        Steps from the start (`lineage`: assets crossed; `neighbours`: 0 or 1; `impact`: hops).
    table_type
        A table's ``managed``, ``external``, ``view`` or ``artifact``.
    model_kind
        A table's declared kind (``fact``, ``dimension``, ...).
    grain
        A table's grain, in the reader's language.
    key
        A table's declared key columns.
    owner
        A table's (its asset's; `None` without ``READ_ENVIRONMENT``), asset's or term's owner.
    repository
        The repository that declares an asset, job, check or term, or builds a reference's or
        measure's table.
    label
        A table's label, or a term's name, in the reader's language.
    column_type
        A column's type.
    reason
        Why `impact` lists it.
    may_use
        Whether `impact` lists it because it may use a column, not because it names it.
    """

    id: str
    kind: NodeKind
    name: str
    distance: int
    table_type: str | None = None
    model_kind: str | None = None
    grain: str | None = None
    key: tuple[str, ...] | None = None
    owner: str | None = None
    repository: str | None = None
    label: str | None = None
    column_type: str | None = None
    reason: str | None = None
    may_use: bool = False


@dataclass(frozen=True, order=True)
class GraphEdge:
    """An edge between two nodes.

    Parameters
    ----------
    kind
        What it says.
    source
        The node it leaves.
    target
        The node it reaches.
    """

    kind: EdgeKind
    source: str
    target: str


@dataclass(frozen=True)
class GraphPage:
    """One page of an answer.

    Parameters
    ----------
    nodes
        The nodes, sorted by distance, then id.
    edges
        The edges whose later endpoint is on this page, sorted.
    next_cursor
        The cursor of the next page; `None` on the last.
    """

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    next_cursor: str | None
