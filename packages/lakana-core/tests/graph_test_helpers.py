"""A star schema across two repositories, read by the metadata graph's tests (#374).

`stores` builds `gold.dim_store` from `bronze.stores_raw`: a dimension with the key `store_id`,
its two generated key checks, and the glossary term `store`. `sales` builds `silver.sales` from
`bronze.sales_raw`, `gold.dim_date` from `silver.sales`, and `gold.fact_sales` from `silver.sales`
and `gold.dim_store`. The fact references both dimensions, has the measure `net_amount`, one
hand-written check, two generated `ref_` checks, the glossary term `net_revenue` (French name
`CA net`) and the job `nightly` (`gold.*`). A row filter lies on the fact and a column mask on
`gold.dim_store.store_name`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core_test_helpers import _prepared_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import DateType, DoubleType, LongType, NestedField, StringType
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable
from lakana_core.catalog.descriptions import sync_project_descriptions
from lakana_core.catalog.glossary import write_glossary
from lakana_core.catalog.graph_model import MAX_GRAPH_STEPS, NodeKind, row_node
from lakana_core.catalog.graph_service import GraphService
from lakana_core.errors import PermissionDenied
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    ColumnMask,
    DeclaredMeasure,
    DeclaredReference,
    Environment,
    EnvironmentDeployment,
    GlossaryTerm,
    JobVersion,
    Namespace,
    Repository,
    RepositoryKind,
    RowFilter,
    Table,
)
from lakana_core.projects.model_checks import KEY_NOT_NULL, KEY_UNIQUE

_TYPES = {"long": LongType(), "string": StringType(), "date": DateType(), "double": DoubleType()}

TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    "bronze.sales_raw": (("date", "date"), ("store_id", "long"), ("amount", "double")),
    "bronze.stores_raw": (("store_id", "long"), ("store_name", "string")),
    "silver.sales": (("date", "date"), ("store_id", "long"), ("net_amount", "double")),
    "gold.dim_store": (("store_id", "long"), ("store_name", "string")),
    "gold.dim_date": (("date", "date"), ("year", "long")),
    "gold.fact_sales": (("date", "date"), ("store_id", "long"), ("net_amount", "double")),
}
"""Every table of the star, with its columns and their types."""

KEY_IMPACT: frozenset[tuple[NodeKind, str, str | None, bool]] = frozenset(
    {
        (NodeKind.TABLE, "gold.dim_store", None, False),
        (NodeKind.COLUMN, "gold.dim_store.store_id", "a key column of gold.dim_store", False),
        (
            NodeKind.REFERENCE,
            "gold.fact_sales.store_id → gold.dim_store.store_id",
            "references gold.dim_store.store_id",
            False,
        ),
        (
            NodeKind.CHECK,
            "gold.fact_sales/ref_store_id",
            "checks the reference gold.fact_sales.store_id",
            False,
        ),
        (NodeKind.CHECK, "gold.dim_store/key_unique", "checks the key of gold.dim_store", False),
        (NodeKind.CHECK, "gold.dim_store/key_not_null", "checks the key of gold.dim_store", False),
        (NodeKind.TERM, "store", "links gold.dim_store.store_id", False),
        (
            NodeKind.ROW_FILTER,
            "row filter on gold.dim_store",
            "filters gold.dim_store; may use its key",
            True,
        ),
        (NodeKind.ASSET, "gold.dim_store", "builds gold.dim_store", False),
        (NodeKind.ASSET, "gold.fact_sales", "downstream of gold.dim_store", True),
        (
            NodeKind.COLUMN,
            "gold.fact_sales.store_id",
            "references gold.dim_store.store_id",
            False,
        ),
        (NodeKind.TABLE, "gold.fact_sales", "references gold.dim_store.store_id", False),
        (NodeKind.JOB, "sales/nightly", "runs gold.fact_sales", True),
    }
)
"""`(kind, name, reason, may_use)` of every node of the impact of `gold.dim_store`'s key,
once `StarWorld.add_row_filter` has put a row filter on it."""

_NET = {
    "column": "net_amount",
    "aggregation": "sum",
    "additivity": "additive",
    "not_over": [],
    "unit": None,
    "currency": "EUR",
    "label": None,
    "description": None,
}


def _model(kind: str, grain: str) -> dict[str, Any]:
    """Build a manifest entry's `model` without references or measures.

    Parameters
    ----------
    kind
        The model kind.
    grain
        The grain.

    Returns
    -------
    dict
        The model.
    """
    return {"kind": kind, "grain": grain, "key_none": False, "references": [], "checks": {}}


def asset_entry(
    table: str,
    reads: tuple[str, ...],
    owner: str,
    *,
    key: tuple[str, ...] = (),
    model: dict[str, Any] | None = None,
    columns: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build a SQL asset's manifest entry, in the shape `build_manifest` writes.

    Parameters
    ----------
    table
        The table it builds.
    reads
        The tables its SQL reads.
    owner
        The header's `owner:`.
    key
        The declared key columns.
    model
        The `model` section, if any.
    columns
        Its explicit columns; the table's columns of `TABLES` when `None`.

    Returns
    -------
    dict
        The entry.
    """
    entry: dict[str, Any] = {
        "table": table,
        "path": f"assets/{table.replace('.', '/')}.sql",
        "kind": "sql",
        "write": "replace",
        "key": list(key),
        "references": list(reads),
        "columns": list(columns) if columns is not None else [c for c, _ in TABLES[table]],
        "metadata": {"description": None, "owner": owner, "tags": [], "columns": {}},
    }
    if model is not None:
        entry["model"] = model
    return entry


def check_entry(
    table: str,
    check: str,
    *,
    family: str | None = None,
    columns: tuple[str, ...] = (),
    target: str | None = None,
    target_column: str | None = None,
) -> dict[str, Any]:
    """Build a check's manifest entry, in the shape `manifest._check_entry` writes.

    Parameters
    ----------
    table
        The checked table.
    check
        The check's name.
    family
        ``key``, ``references`` or ``measures`` for a generated check; `None` if hand-written.
    columns
        The generated check's columns.
    target
        A ``references`` check's target table.
    target_column
        Its target column.

    Returns
    -------
    dict
        The entry.
    """
    entry: dict[str, Any] = {
        "name": f"{table}/{check}",
        "table": table,
        "check": check,
        "path": f"checks/{check}.sql",
        "on_failure": "block",
        "description": None,
        "sql": f"SELECT 1 FROM {table} WHERE false",
        "references": [table],
    }
    if family is not None:
        entry["generated"] = {
            "family": family,
            "columns": list(columns),
            "target": target,
            "target_column": target_column,
        }
    return entry


def stores_manifest() -> dict[str, Any]:
    """Build the current manifest of `stores`.

    Returns
    -------
    dict
        Version 13, with `gold.dim_store`, its key checks and the term `store`.
    """
    dim = asset_entry(
        "gold.dim_store",
        ("bronze.stores_raw",),
        "stores-team",
        key=("store_id",),
        model=_model("dimension", "One row per store."),
    )
    return {
        "version": 13,
        "main_language": "en",
        "assets": [dim],
        "edges": [],
        "external_inputs": ["bronze.stores_raw"],
        "checks": [
            check_entry("gold.dim_store", KEY_UNIQUE, family="key", columns=("store_id",)),
            check_entry("gold.dim_store", KEY_NOT_NULL, family="key", columns=("store_id",)),
        ],
        "glossary": {
            "store": {
                "name": "Store",
                "definition": "A shop that sells.",
                "owner": "stores-team",
                "links": {"gold.dim_store": ["store_id"]},
                "related": [],
            }
        },
    }


def sales_manifest(
    *, fact_columns: tuple[str, ...] = ("date", "store_id", "net_amount")
) -> dict[str, Any]:
    """Build the current manifest of `sales`.

    Parameters
    ----------
    fact_columns
        The explicit columns of `gold.fact_sales`; a test of a removed column passes fewer.

    Returns
    -------
    dict
        Version 13, with `silver.sales`, `gold.dim_date`, `gold.fact_sales`, their checks, the job
        `nightly` and the term `net_revenue`.
    """
    fact_model = {
        **_model("fact", "One row per store and day."),
        "references": [
            {"column": "store_id", "target": "gold.dim_store", "target_column": "store_id"},
            {"column": "date", "target": "gold.dim_date", "target_column": "date"},
        ],
        "measures": [dict(_NET)],
    }
    return {
        "version": 13,
        "main_language": "en",
        "assets": [
            asset_entry("silver.sales", ("bronze.sales_raw",), "sales-team"),
            asset_entry(
                "gold.dim_date",
                ("silver.sales",),
                "sales-team",
                key=("date",),
                model=_model("dimension", "One row per day."),
            ),
            asset_entry(
                "gold.fact_sales",
                ("silver.sales", "gold.dim_store"),
                "sales-team",
                key=("date", "store_id"),
                model=fact_model,
                columns=fact_columns,
            ),
        ],
        "edges": [["silver.sales", "gold.dim_date"], ["silver.sales", "gold.fact_sales"]],
        "external_inputs": ["bronze.sales_raw", "gold.dim_store"],
        "checks": [
            check_entry("gold.fact_sales", "positive_amount"),
            check_entry(
                "gold.fact_sales",
                "ref_date",
                family="references",
                columns=("date",),
                target="gold.dim_date",
                target_column="date",
            ),
            check_entry(
                "gold.fact_sales",
                "ref_store_id",
                family="references",
                columns=("store_id",),
                target="gold.dim_store",
                target_column="store_id",
            ),
        ],
        "jobs": {"nightly": {"select": ["gold.*"]}},
        "glossary": {
            "net_revenue": {
                "name": "Net revenue",
                "definition": "Revenue after returns.",
                "owner": "finance",
                "links": {"gold.fact_sales": ["net_amount"]},
                "related": [],
                "translations": {"fr": {"name": "CA net"}},
            }
        },
    }


def point(
    session: Session, env_id: uuid.UUID, repo: Repository, manifest: dict[str, Any]
) -> JobVersion:
    """Store `manifest` as a project version of `repo` and make it the current one.

    Parameters
    ----------
    session
        Database session; flushed, not committed.
    env_id
        The environment.
    repo
        The repository.
    manifest
        The manifest.

    Returns
    -------
    JobVersion
        The new current version.
    """
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env_id,
        commit_sha=uuid.uuid4().hex + "00000000",
        manifest_json=manifest,
        code_uri="file:///code.tar.gz",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    pointer = session.get(EnvironmentDeployment, (env_id, repo.id))
    if pointer is None:
        session.add(
            EnvironmentDeployment(
                environment_id=env_id, repository_id=repo.id, current_job_version_id=version.id
            )
        )
    else:
        pointer.current_job_version_id = version.id
    session.flush()
    return version


class Grants:
    """Permits `READ_TABLE` on the listed tables, and the two other rights the graph asks.

    Parameters
    ----------
    tables
        The dotted tables the reader may read; `None` for every table.
    environment
        Whether the reader holds `READ_ENVIRONMENT`.
    policies
        Whether the reader holds `MANAGE_POLICY`.
    """

    def __init__(
        self,
        tables: Collection[str] | None = None,
        *,
        environment: bool = True,
        policies: bool = False,
    ) -> None:
        self.tables = None if tables is None else frozenset(tables)
        self.environment = environment
        self.policies = policies

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer from the lists.

        Parameters
        ----------
        principal
            Ignored.
        action
            The action asked.
        securable
            The object.

        Returns
        -------
        bool
            Whether it is permitted.
        """
        if action is Action.READ_TABLE:
            assert securable.namespace is not None and securable.table is not None
            dotted = TableRef(securable.environment or "", securable.namespace, securable.table)
            return self.tables is None or dotted.dotted() in self.tables
        if action is Action.READ_ENVIRONMENT:
            return self.environment
        if action is Action.MANAGE_POLICY:
            return self.policies
        return False

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse what `permits` refuses.

        Parameters
        ----------
        principal
            Ignored.
        action
            The action asked.
        securable
            The object.

        Raises
        ------
        PermissionDenied
            If it is not permitted.
        """
        if not self.permits(principal, action, securable):
            raise PermissionDenied(f"may not {action.value}")


@dataclass(frozen=True)
class StarWorld:
    """The star schema, and how a test finds its rows.

    Parameters
    ----------
    session
        Database session.
    env_id
        The id of `dev`.
    repos
        Repository name → row.
    """

    session: Session
    env_id: uuid.UUID
    repos: dict[str, Repository]

    def table_id(self, dotted: str) -> uuid.UUID:
        """Find a table's id.

        Parameters
        ----------
        dotted
            Its dotted name.

        Returns
        -------
        uuid.UUID
            Its id.
        """
        ref = TableRef.from_dotted("dev", dotted)
        return self.session.scalars(
            select(Table.id)
            .join(Namespace, Namespace.id == Table.namespace_id)
            .where(
                Namespace.environment_id == self.env_id,
                Namespace.path == ref.namespace.joined(),
                Table.name == ref.name,
            )
        ).one()

    def reference(self, table: str, column: str) -> str:
        """Find a reference's node id.

        Parameters
        ----------
        table
            The table that declares it.
        column
            Its referencing column.

        Returns
        -------
        str
            ``reference:<uuid>``.
        """
        row_id = self.session.scalars(
            select(DeclaredReference.id).where(
                DeclaredReference.source_table_id == self.table_id(table),
                DeclaredReference.source_column == column,
            )
        ).one()
        return row_node(NodeKind.REFERENCE, row_id)

    def measure(self, table: str, column: str) -> str:
        """Find a measure's node id.

        Parameters
        ----------
        table
            Its table.
        column
            Its column.

        Returns
        -------
        str
            ``measure:<uuid>``.
        """
        row_id = self.session.scalars(
            select(DeclaredMeasure.id).where(
                DeclaredMeasure.table_id == self.table_id(table), DeclaredMeasure.column == column
            )
        ).one()
        return row_node(NodeKind.MEASURE, row_id)

    def term(self, key: str) -> str:
        """Find a glossary term's node id.

        Parameters
        ----------
        key
            The term's id in the glossary.

        Returns
        -------
        str
            ``term:<uuid>``.
        """
        row_id = self.session.scalars(
            select(GlossaryTerm.id).where(GlossaryTerm.term_key == key)
        ).one()
        return row_node(NodeKind.TERM, row_id)

    def policy(self, kind: NodeKind) -> str:
        """Find the star's one row filter or column mask.

        Parameters
        ----------
        kind
            `NodeKind.ROW_FILTER` or `NodeKind.COLUMN_MASK`.

        Returns
        -------
        str
            Its node id.
        """
        model = RowFilter if kind is NodeKind.ROW_FILTER else ColumnMask
        return row_node(kind, self.session.scalars(select(model.id)).one())

    def add_row_filter(self, principal: PrincipalRef, dotted: str) -> str:
        """Put a row filter on a table of the star; flushed, not committed.

        Parameters
        ----------
        principal
            The principal it filters and that creates it.
        dotted
            The table.

        Returns
        -------
        str
            Its node id.
        """
        ref = TableRef.from_dotted("dev", dotted)
        row = RowFilter(
            id=uuid.uuid4(),
            principal_id=principal.id,
            environment="dev",
            namespace=ref.namespace.joined(),
            table=ref.name,
            environment_id=self.env_id,
            table_id=self.table_id(dotted),
            created_by=principal.id,
            created_at=datetime.now(UTC),
            expression="store_id > 0",
        )
        self.session.add(row)
        self.session.flush()
        return row_node(NodeKind.ROW_FILTER, row.id)


def graph_service_for(
    star: StarWorld,
    grants: Grants | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    max_steps: int = MAX_GRAPH_STEPS,
) -> GraphService:
    """Build the graph service over the star.

    Parameters
    ----------
    star
        The world.
    grants
        What the reader may do; everything, policies included, when `None`.
    clock
        The service's clock.
    max_steps
        The step budget.

    Returns
    -------
    GraphService
        The service.
    """
    authz = grants or Grants(policies=True)
    return GraphService(star.session, authz, "en", clock=clock, max_steps=max_steps)


def build_star(session: Session, tmp_path: Path, principal: PrincipalRef) -> StarWorld:
    """Create the star in `dev`: tables, both deployments, model rows, terms and policies.

    Parameters
    ----------
    session
        Database session; committed.
    tmp_path
        Storage root.
    principal
        The principal that creates the tables and policies.

    Returns
    -------
    StarWorld
        The world.
    """
    catalog = _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[NamespacePath.from_parts([n]) for n in ("bronze", "silver", "gold")],
    )
    for dotted, columns in TABLES.items():
        fields = (
            NestedField(i + 1, name, _TYPES[kind], required=False)
            for i, (name, kind) in enumerate(columns)
        )
        catalog.create_table(principal, TableRef.from_dotted("dev", dotted), schema=Schema(*fields))
    env_id = session.scalars(select(Environment.id).where(Environment.name == "dev")).one()
    repos: dict[str, Repository] = {}
    for name, manifest in (("sales", sales_manifest()), ("stores", stores_manifest())):
        repo = Repository(
            id=uuid.uuid4(), name=name, kind=RepositoryKind.HTTPS, url=f"https://h/{name}"
        )
        session.add(repo)
        session.flush()
        point(session, env_id, repo, manifest)
        sync_project_descriptions(session, env_id, manifest)
        write_glossary(session, env_id, repo.id, manifest)
        repos[name] = repo
    session.flush()
    world = StarWorld(session, env_id, repos)
    world.add_row_filter(principal, "gold.fact_sales")
    session.add(
        ColumnMask(
            id=uuid.uuid4(),
            principal_id=principal.id,
            environment="dev",
            namespace=NamespacePath.from_parts(["gold"]).joined(),
            table="dim_store",
            column="store_name",
            environment_id=env_id,
            table_id=world.table_id("gold.dim_store"),
            created_by=principal.id,
            created_at=datetime.now(UTC),
            expression="NULL",
        )
    )
    session.commit()
    return world
