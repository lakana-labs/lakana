"""Jobs and connections in the lineage walk: leaves that cost no depth, and who sees them (#374)."""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from graph_test_helpers import CONNECTION, Grants, StarWorld, add_ingest, point, python_entry
from graph_test_helpers import graph_service_for as _service
from sqlalchemy import select

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, TaskScope
from lakana_core.catalog import graph_service, lineage_graph
from lakana_core.catalog.graph_model import (
    NO_NODE,
    EdgeKind,
    GraphPage,
    LineageWay,
    NodeKind,
    asset_node,
    connection_node,
    job_node,
)
from lakana_core.connections import service as connections_service
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.models import Repository, RepositoryKind

_INGEST = asset_node("ingest", "bronze.sales_raw")
_PG = connection_node(CONNECTION)
_LOAD = job_node("ingest", "load")
_NIGHTLY = job_node("sales", "nightly")


def _seen(page: GraphPage) -> set[tuple[NodeKind, str, int]]:
    """Flatten a page's nodes.

    Parameters
    ----------
    page
        The page.

    Returns
    -------
    set
        ``(kind, name, distance)`` per node.
    """
    return {(n.kind, n.name, n.distance) for n in page.nodes}


def _edges(page: GraphPage) -> set[tuple[EdgeKind, str, str]]:
    """Flatten a page's edges.

    Parameters
    ----------
    page
        The page.

    Returns
    -------
    set
        ``(kind, source, target)`` per edge.
    """
    return {(e.kind, e.source, e.target) for e in page.edges}


def _task(principal: PrincipalRef) -> PrincipalRef:
    """Act as `principal` with a task token.

    Parameters
    ----------
    principal
        The principal.

    Returns
    -------
    PrincipalRef
        The same principal, bound to a task.
    """
    scope = TaskScope(environment="dev", asset_scope=["bronze.sales_raw"], token_id=uuid.uuid4())
    return dataclasses.replace(principal, task_scope=scope)


def _deploy(star: StarWorld, name: str, manifest: dict[str, object]) -> None:
    """Deploy one more repository into `dev`.

    Parameters
    ----------
    star
        The world.
    name
        The repository's name.
    manifest
        Its manifest.
    """
    repo = Repository(
        id=uuid.uuid4(), name=name, kind=RepositoryKind.HTTPS, url=f"https://h/{name}"
    )
    star.session.add(repo)
    star.session.flush()
    point(star.session, star.env_id, repo, manifest)
    star.session.commit()


def test_lineage_adds_the_jobs_that_run_each_asset(
    star: StarWorld, principal: PrincipalRef
) -> None:
    page = _service(star).lineage(principal, "dev", "gold.dim_store", depth=1)
    (job,) = [n for n in page.nodes if n.kind is NodeKind.JOB]
    assert (job.id, job.name, job.distance, job.repository) == (
        _NIGHTLY,
        "sales/nightly",
        1,
        "sales",
    )
    assert (EdgeKind.RUNS, _NIGHTLY, asset_node("sales", "gold.fact_sales")) in _edges(page)
    down = _service(star).lineage(
        principal, "dev", "bronze.sales_raw", direction=LineageWay.DOWN, depth=2
    )
    assert (NodeKind.JOB, "sales/nightly", 2) in _seen(down)


def test_lineage_adds_the_connections_an_asset_uses(
    star: StarWorld, principal: PrincipalRef
) -> None:
    add_ingest(star, principal)
    page = _service(star).lineage(
        principal, "dev", "bronze.sales_raw", direction=LineageWay.UP, depth=1
    )
    (connection,) = [n for n in page.nodes if n.kind is NodeKind.CONNECTION]
    assert (connection.id, connection.name, connection.distance) == (_PG, "pg-sales", 1)
    assert (connection.connection_kind, connection.repository) == ("postgresql", None)
    assert _edges(page) >= {(EdgeKind.USES, _INGEST, _PG), (EdgeKind.RUNS, _LOAD, _INGEST)}
    assert (NodeKind.JOB, "ingest/load", 1) in _seen(page)
    start = _service(star).lineage(principal, "dev", _INGEST, depth=1)
    assert {(NodeKind.CONNECTION, "pg-sales", 0), (NodeKind.JOB, "ingest/load", 0)} <= _seen(start)


def test_a_declared_connection_without_a_row_has_no_kind(
    star: StarWorld, principal: PrincipalRef
) -> None:
    add_ingest(star, principal, with_row=False)
    page = _service(star).lineage(principal, "dev", "bronze.sales_raw", depth=1)
    (connection,) = [n for n in page.nodes if n.kind is NodeKind.CONNECTION]
    assert (connection.id, connection.connection_kind) == (_PG, None)


def test_jobs_and_connections_cost_no_depth_and_are_not_walked(
    star: StarWorld, principal: PrincipalRef
) -> None:
    add_ingest(star, principal)
    _deploy(star, "crm", {"version": 13, "assets": [python_entry("bronze.crm", [CONNECTION])]})
    page = _service(star).lineage(principal, "dev", "bronze.sales_raw", depth=1)
    assert _seen(page) == {
        (NodeKind.TABLE, "bronze.sales_raw", 0),
        (NodeKind.ASSET, "bronze.sales_raw", 1),
        (NodeKind.CONNECTION, "pg-sales", 1),
        (NodeKind.JOB, "ingest/load", 1),
        (NodeKind.ASSET, "silver.sales", 1),
        (NodeKind.TABLE, "silver.sales", 1),
    }
    # The job `nightly` runs `gold.dim_date` too, which the walk does not reach at depth 1.
    near = _service(star).lineage(principal, "dev", "gold.dim_store", depth=1)
    assert asset_node("sales", "gold.dim_date") not in {n.id for n in near.nodes}
    assert {e.target for e in near.edges if e.kind is EdgeKind.RUNS} == {
        asset_node("sales", "gold.fact_sales")
    }


def test_a_reader_without_read_environment_sees_no_job_or_connection(
    star: StarWorld, principal: PrincipalRef
) -> None:
    add_ingest(star, principal)
    assert _PG in {n.id for n in _service(star).neighbours(principal, "dev", _INGEST).nodes}
    reader = _service(star, Grants(environment=False))
    page = reader.lineage(principal, "dev", "bronze.sales_raw", depth=3)
    assert {n.kind for n in page.nodes} == {NodeKind.TABLE}
    assert (page.edges, page.next_cursor) == ((), None)
    for start in (_PG, _LOAD):
        with pytest.raises(NotFoundError) as caught:
            reader.neighbours(principal, "dev", start)
        assert str(caught.value) == NO_NODE.format(node=start, env="dev")


def test_a_task_token_never_sees_a_connection_node(
    star: StarWorld, principal: PrincipalRef
) -> None:
    add_ingest(star, principal)
    task = _task(principal)
    full = _service(star).lineage(principal, "dev", "bronze.sales_raw", depth=2)
    page = _service(star).lineage(task, "dev", "bronze.sales_raw", depth=2)
    assert NodeKind.CONNECTION in {n.kind for n in full.nodes}
    assert (NodeKind.JOB, "ingest/load", 1) in _seen(page)
    assert NodeKind.CONNECTION not in {n.kind for n in page.nodes}
    assert EdgeKind.USES not in {e.kind for e in page.edges}
    assert page.nodes == tuple(n for n in full.nodes if n.kind is not NodeKind.CONNECTION)
    assert page.edges == tuple(e for e in full.edges if e.kind is not EdgeKind.USES)
    exact = _service(star).lineage(task, "dev", "bronze.sales_raw", depth=2, limit=len(page.nodes))
    assert exact.next_cursor is None


def test_a_task_token_never_sees_a_connection_through_neighbours(
    star: StarWorld, principal: PrincipalRef
) -> None:
    add_ingest(star, principal)
    task = _task(principal)
    assert _PG in {n.id for n in _service(star).neighbours(principal, "dev", _INGEST).nodes}
    near = _service(star).neighbours(task, "dev", _INGEST)
    assert _PG not in {n.id for n in near.nodes}
    assert EdgeKind.USES not in {e.kind for e in near.edges}
    assert _LOAD in {n.id for n in near.nodes}
    only = _service(star).neighbours(task, "dev", _INGEST, kinds=["uses"])
    assert ([n.id for n in only.nodes], only.edges) == ([_INGEST], ())


def test_a_hidden_connection_start_reads_like_an_undeclared_name(
    star: StarWorld, principal: PrincipalRef
) -> None:
    add_ingest(star, principal)
    task = _task(principal)
    assert _service(star).neighbours(principal, "dev", _PG).nodes[0].id == _PG
    errors = []
    for node in (_PG, "connection:nobody-declared"):
        with pytest.raises(NotFoundError) as caught:
            _service(star).neighbours(task, "dev", node)
        errors.append(str(caught.value))
    hidden, undeclared = errors
    assert hidden == undeclared.replace("nobody-declared", CONNECTION)
    assert hidden == NO_NODE.format(node=_PG, env="dev")


def test_a_connection_node_is_a_valid_start(star: StarWorld, principal: PrincipalRef) -> None:
    add_ingest(star, principal)
    near = _service(star).neighbours(principal, "dev", _PG)
    root = near.nodes[0]
    assert (root.id, root.kind, root.name, root.distance, root.connection_kind) == (
        _PG,
        NodeKind.CONNECTION,
        "pg-sales",
        0,
        "postgresql",
    )
    assert _edges(near) == {(EdgeKind.USES, _INGEST, _PG)}
    with pytest.raises(ValidationError) as caught:
        _service(star).lineage(principal, "dev", _PG)
    assert "`connection:pg-sales` is a connection" in str(caught.value)


def test_a_job_shows_only_its_edges_to_visible_assets(
    star: StarWorld, principal: PrincipalRef
) -> None:
    every_but_dim_date = {
        "bronze.sales_raw",
        "bronze.stores_raw",
        "silver.sales",
        "gold.dim_store",
        "gold.fact_sales",
    }
    reader = _service(star, Grants(every_but_dim_date))
    page = reader.lineage(principal, "dev", "silver.sales", direction=LineageWay.DOWN, depth=1)
    assert (NodeKind.JOB, "sales/nightly", 1) in _seen(page)
    assert {e for e in _edges(page) if e[0] is EdgeKind.RUNS} == {
        (EdgeKind.RUNS, _NIGHTLY, asset_node("sales", "gold.fact_sales"))
    }
    assert all("dim_date" not in n.name for n in page.nodes)
    everything = _service(star).lineage(
        principal, "dev", "silver.sales", direction=LineageWay.DOWN, depth=1
    )
    assert {e.target for e in everything.edges if e.kind is EdgeKind.RUNS} == {
        asset_node("sales", "gold.fact_sales"),
        asset_node("sales", "gold.dim_date"),
    }


@pytest.mark.parametrize(
    "connections",
    [["../x"], ["a" * 300], [5], ["a\u202eb"], ["pg_sales"], "crm-db"],
    ids=["traversal", "long", "number", "bidi", "underscore", "bare-string"],
)
def test_an_untrusted_connection_name_is_dropped(
    star: StarWorld, principal: PrincipalRef, connections: object
) -> None:
    add_ingest(star, principal, connections=connections)
    page = _service(star).lineage(principal, "dev", "bronze.sales_raw", depth=1)
    assert NodeKind.CONNECTION not in {n.kind for n in page.nodes}
    assert EdgeKind.USES not in {e.kind for e in page.edges}
    for letter in "crm-db":
        with pytest.raises(NotFoundError):
            _service(star).neighbours(principal, "dev", f"connection:{letter}")


def test_impact_and_join_path_are_unchanged_by_connections(
    star: StarWorld, principal: PrincipalRef
) -> None:
    add_ingest(star, principal, connections=[])
    before = (
        _service(star).impact(principal, "dev", "bronze.sales_raw"),
        _service(star).join_path(principal, "dev", "gold.fact_sales", "gold.dim_store"),
    )
    ingest = star.session.scalars(select(Repository).where(Repository.name == "ingest")).one()
    used = {"version": 13, "assets": [python_entry("bronze.sales_raw", [CONNECTION])]}
    point(
        star.session,
        star.env_id,
        ingest,
        {**used, "jobs": {"load": {"select": ["bronze.sales_raw"]}}},
    )
    _deploy(star, "crm", {"version": 13, "assets": [python_entry("bronze.crm", [CONNECTION])]})
    assert _PG in {n.id for n in _service(star).neighbours(principal, "dev", _INGEST).nodes}
    after = (
        _service(star).impact(principal, "dev", "bronze.sales_raw"),
        _service(star).join_path(principal, "dev", "gold.fact_sales", "gold.dim_store"),
    )
    assert after == before
    assert NodeKind.CONNECTION not in {n.kind for n in after[0].nodes}


def test_neighbours_filter_accepts_uses(star: StarWorld, principal: PrincipalRef) -> None:
    add_ingest(star, principal)
    near = _service(star).neighbours(principal, "dev", _INGEST, kinds=["uses"])
    assert [n.id for n in near.nodes] == [_INGEST, _PG]
    assert _edges(near) == {(EdgeKind.USES, _INGEST, _PG)}


def test_lineage_graph_and_connection_service_share_the_task_rule(
    star: StarWorld, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_ingest(star, principal)
    asked: list[PrincipalRef] = []

    def every_caller_is_a_task(p: PrincipalRef) -> bool:
        """Treat every caller as a task, and remember who was asked about.

        Parameters
        ----------
        p
            The caller.

        Returns
        -------
        bool
            Always `True`.
        """
        asked.append(p)
        return True

    for module in (lineage_graph, graph_service, connections_service):
        monkeypatch.setattr(module, "acts_for_a_task", every_caller_is_a_task, raising=False)
    lineage = lineage_graph.LineageService(star.session, AllowAllAuthorization())
    assert lineage._may_see_connections(principal, "dev") is False
    with pytest.raises(PermissionDenied):
        connections_service._refuse_task(principal)
    page = _service(star).lineage(principal, "dev", "bronze.sales_raw", depth=1)
    assert NodeKind.CONNECTION not in {n.kind for n in page.nodes}
    assert asked == [principal, principal, principal]
