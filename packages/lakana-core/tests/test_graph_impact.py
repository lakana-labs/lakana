"""What depends on a table, a column or a key: the metadata graph's impact (#374, Q4)."""

from __future__ import annotations

import uuid

import pytest
from graph_test_helpers import KEY_IMPACT, Grants, StarWorld, asset_entry, point, sales_manifest
from graph_test_helpers import graph_service_for as _service
from sqlalchemy import event

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.glossary import write_glossary
from lakana_core.catalog.graph_model import GraphPage, NodeKind, asset_node
from lakana_core.errors import ValidationError
from lakana_core.models import DeclaredReference, Repository, RepositoryKind

_T, _A, _C = NodeKind.TABLE, NodeKind.ASSET, NodeKind.CHECK


def _listed(page: GraphPage) -> set[tuple[NodeKind, str, str | None, bool]]:
    """Flatten an impact page.

    Parameters
    ----------
    page
        The page.

    Returns
    -------
    set
        ``(kind, name, reason, may_use)`` per node.
    """
    return {(n.kind, n.name, n.reason, n.may_use) for n in page.nodes}


def _repository(star: StarWorld, name: str) -> Repository:
    """Add a repository without a deployment.

    Parameters
    ----------
    star
        The world.
    name
        Its name.

    Returns
    -------
    Repository
        The row; flushed.
    """
    repo = Repository(
        id=uuid.uuid4(), name=name, kind=RepositoryKind.HTTPS, url=f"https://h/{name}"
    )
    star.session.add(repo)
    star.session.flush()
    return repo


def _reference(star: StarWorld, source: str, column: str, target_column: str) -> None:
    """Declare a reference from a column of the star to a column of `gold.dim_store`.

    Parameters
    ----------
    star
        The world.
    source
        The referencing table.
    column
        Its referencing column.
    target_column
        The column of `gold.dim_store` it points at.
    """
    star.session.add(
        DeclaredReference(
            id=uuid.uuid4(),
            environment_id=star.env_id,
            source_table_id=star.table_id(source),
            source_column=column,
            target_name="gold.dim_store",
            target_column=target_column,
        )
    )
    star.session.flush()


def test_impact_of_a_dimension_key_lists_every_dependent_with_its_reason(
    star: StarWorld, principal: PrincipalRef
) -> None:
    star.add_row_filter(principal, "gold.dim_store")
    page = _service(star).impact(principal, "dev", "gold.dim_store", key=True)
    assert _listed(page) == KEY_IMPACT
    # A table that references the key without reading the dimension is listed by its reference.
    _reference(star, "gold.dim_date", "year", "store_id")
    via = "references gold.dim_store.store_id"
    page = _service(star).impact(principal, "dev", "gold.dim_store", key=True)
    assert _listed(page) == KEY_IMPACT | {
        (NodeKind.REFERENCE, "gold.dim_date.year → gold.dim_store.store_id", via, False),
        (NodeKind.COLUMN, "gold.dim_date.year", via, False),
        (_T, "gold.dim_date", via, False),
    }
    ids = {n.id for n in page.nodes}
    assert all(e.source in ids and e.target in ids for e in page.edges)
    distances = {n.name: n.distance for n in page.nodes if n.kind is not _A}
    assert distances["gold.fact_sales/ref_store_id"] == 3
    assert distances["sales/nightly"] == 2


def test_impact_of_a_fact_table_lists_its_measure_policy_checks_terms_and_job(
    star: StarWorld, principal: PrincipalRef
) -> None:
    page = _service(star).impact(principal, "dev", "gold.fact_sales")
    measure = "a measure on gold.fact_sales.net_amount"
    assert {
        (NodeKind.MEASURE, "gold.fact_sales.net_amount", measure, False),
        (NodeKind.ROW_FILTER, "row filter on gold.fact_sales", "filters gold.fact_sales", False),
        (_C, "gold.fact_sales/positive_amount", "checks gold.fact_sales", False),
        (_C, "gold.fact_sales/ref_date", "checks the reference gold.fact_sales.date", False),
        (NodeKind.TERM, "net_revenue", "links gold.fact_sales.net_amount", False),
        (
            NodeKind.REFERENCE,
            "gold.fact_sales.store_id → gold.dim_store.store_id",
            "declared on gold.fact_sales.store_id",
            False,
        ),
        (_A, "gold.fact_sales", "builds gold.fact_sales", False),
        (NodeKind.JOB, "sales/nightly", "runs gold.fact_sales", False),
    } <= _listed(page)
    # The generated check hangs on the table as well as on its reference: the shorter way counts.
    assert next(n for n in page.nodes if n.name == "gold.fact_sales/ref_date").distance == 1
    whole = _service(star).impact(principal, "dev", "gold.dim_store").nodes
    # The reference hangs on the table too (distance 1), so its check is one further, not three.
    assert next(n for n in whole if n.name == "gold.fact_sales/ref_store_id").distance == 2
    mask = {(n.kind, n.name, n.reason, n.may_use) for n in whole}
    assert (
        NodeKind.COLUMN_MASK,
        "column mask on gold.dim_store.store_name",
        "masks gold.dim_store.store_name",
        False,
    ) in mask


def test_impact_of_a_table_lists_a_term_on_it_and_a_reference_to_a_missing_column(
    star: StarWorld, principal: PrincipalRef
) -> None:
    shops = _repository(star, "shops")
    glossary = {
        "shop": {
            "name": "Shop",
            "definition": "A store.",
            "owner": "o",
            "links": {"gold.dim_store": []},
            "related": [],
        }
    }
    write_glossary(
        star.session,
        star.env_id,
        shops.id,
        {"version": 13, "main_language": "en", "glossary": glossary},
    )
    _reference(star, "silver.sales", "store_id", "nope")
    nope = (
        NodeKind.REFERENCE,
        "silver.sales.store_id → gold.dim_store.nope",
        "references gold.dim_store.nope",
        False,
    )
    shop = (NodeKind.TERM, "shop", "links gold.dim_store", False)
    whole = _listed(_service(star).impact(principal, "dev", "gold.dim_store"))
    via = "references gold.dim_store.nope"
    assert {
        nope,
        shop,
        (NodeKind.COLUMN, "silver.sales.store_id", via, False),
        (_T, "silver.sales", via, False),
    } <= whole
    key = _listed(_service(star).impact(principal, "dev", "gold.dim_store", key=True))
    assert not {n for n in key if n[1] in (nope[1], shop[1])}


def test_impact_of_a_column_marks_what_only_may_use_it(
    star: StarWorld, principal: PrincipalRef
) -> None:
    page = _service(star).impact(principal, "dev", "column:gold.fact_sales/net_amount")
    may = "; may use net_amount"
    assert _listed(page) == {
        (NodeKind.COLUMN, "gold.fact_sales.net_amount", None, False),
        (_T, "gold.fact_sales", None, False),
        (
            NodeKind.MEASURE,
            "gold.fact_sales.net_amount",
            "a measure on gold.fact_sales.net_amount",
            False,
        ),
        (NodeKind.TERM, "net_revenue", "links gold.fact_sales.net_amount", False),
        (_C, "gold.fact_sales/positive_amount", f"checks gold.fact_sales{may}", True),
        (
            NodeKind.ROW_FILTER,
            "row filter on gold.fact_sales",
            f"filters gold.fact_sales{may}",
            True,
        ),
        (_A, "gold.fact_sales", "builds gold.fact_sales", False),
        (NodeKind.JOB, "sales/nightly", "runs gold.fact_sales", False),
    }


def test_impact_of_a_column_marks_everything_downstream_as_may_use(
    star: StarWorld, principal: PrincipalRef
) -> None:
    page = _service(star).impact(principal, "dev", "column:silver.sales/store_id")
    downstream = {(n.kind, n.name): n for n in page.nodes if n.reason and "downstream" in n.reason}
    assert set(downstream) == {
        (_A, "gold.dim_date"),
        (_T, "gold.dim_date"),
        (_A, "gold.fact_sales"),
        (_T, "gold.fact_sales"),
    }
    assert all(n.may_use and n.reason == "downstream of silver.sales" for n in downstream.values())
    job = next(n for n in page.nodes if n.kind is NodeKind.JOB)
    assert (job.reason, job.may_use) == ("runs gold.dim_date", True)


def test_impact_is_cut_at_what_the_reader_may_not_see(
    star: StarWorld, principal: PrincipalRef
) -> None:
    reader = _service(star, Grants({"gold.dim_store", "bronze.stores_raw"}))
    nodes = reader.impact(principal, "dev", "gold.dim_store", key=True).nodes
    names = {n.name for n in nodes}
    assert "store" in names
    assert not {n for n in names if "fact_sales" in n}
    assert not [n.reason for n in nodes if "fact_sales" in (n.reason or "")]
    assert NodeKind.JOB not in {n.kind for n in nodes}  # it runs only hidden assets


def test_impact_of_a_cycle_ends(star: StarWorld, principal: PrincipalRef) -> None:
    loop = _repository(star, "loop")
    manifest = {
        "version": 13,
        "assets": [asset_entry("bronze.sales_raw", ("gold.fact_sales",), "x")],
    }
    point(star.session, star.env_id, loop, manifest)
    page = _service(star).impact(principal, "dev", "bronze.sales_raw")
    ids = [n.id for n in page.nodes]
    assert len(ids) == len(set(ids)) and asset_node("loop", "bronze.sales_raw") in ids
    root = next(n for n in page.nodes if n.distance == 0)
    assert (root.name, root.reason) == ("bronze.sales_raw", None)


@pytest.mark.parametrize(
    ("node", "key", "message"),
    [
        (
            "term:store",
            False,
            "Impact is asked of a table or a column, and `term:store` is a glossary term. Ask for "
            "its neighbours instead: lakana graph neighbours term:store --env dev",
        ),
        (
            "column:gold.dim_store/store_id",
            True,
            "key=true asks for the impact of a table's key, and `column:gold.dim_store/store_id` "
            "is a column. Ask for the column without --key, or for its table with --key.",
        ),
        (
            "bronze.sales_raw",
            True,
            "`bronze.sales_raw` declares no key, so its key has no impact to show. Declare one in "
            "the asset header (key: [column, ...]), or ask for the whole table: lakana graph "
            "impact bronze.sales_raw --env dev",
        ),
    ],
)
def test_impact_refusals_name_the_next_step(
    star: StarWorld, principal: PrincipalRef, node: str, key: bool, message: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        _service(star).impact(principal, "dev", node, key=key)
    assert str(caught.value) == message


def test_impact_pages_like_the_other_questions(star: StarWorld, principal: PrincipalRef) -> None:
    service = _service(star)
    whole = service.impact(principal, "dev", "gold.dim_store")
    nodes, edges, cursor = [], [], None
    while True:
        page = service.impact(principal, "dev", "gold.dim_store", limit=3, cursor=cursor)
        nodes += page.nodes
        edges += page.edges
        cursor = page.next_cursor
        if cursor is None:
            break
    assert nodes == list(whole.nodes) and len(nodes) > 3
    assert sorted(edges) == sorted(whole.edges) and len(edges) == len(set(edges))
    lineage = service.lineage(principal, "dev", "gold.dim_store", limit=1)
    with pytest.raises(ValidationError) as caught:
        service.impact(principal, "dev", "gold.dim_store", cursor=lineage.next_cursor)
    assert "not valid for the `graph-impact` list" in str(caught.value)


def test_column_readers_cost_one_preload_query_however_many_readers(
    star: StarWorld, principal: PrincipalRef
) -> None:
    manifest = sales_manifest()
    for entry in manifest["assets"]:
        if entry["table"] in {"gold.dim_date", "gold.fact_sales"}:
            entry["lineage"] = {
                c: [{"table": "silver.sales", "column": "store_id"}] for c in entry["columns"]
            }
    point(star.session, star.env_id, star.repos["sales"], manifest)
    star.session.commit()
    statements: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        if "table_columns" in statement:
            statements.append(statement)

    engine = star.session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        answers = _service(star).breakage(principal, "dev", ["column:silver.sales/store_id"])
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert {g.name for g, _ in answers["column:silver.sales/store_id"]} >= {
        "gold.dim_date",
        "gold.fact_sales",
    }
    # One for the start column's own table, one for both readers' built tables together.
    assert len(statements) == 2, "\n---\n".join(statements)
