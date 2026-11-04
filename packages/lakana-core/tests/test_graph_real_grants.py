"""The metadata graph with the real grants: bounded authorization work and time (#374, R8)."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from graph_test_helpers import StarWorld, asset_entry, point
from sqlalchemy import event, select

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.catalog import graph_load
from lakana_core.catalog.graph_model import LineageWay, NodeKind
from lakana_core.catalog.graph_service import GraphService
from lakana_core.errors import GraphBusyError, NotFoundError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import (
    Grant,
    Namespace,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Table,
    TableColumn,
)
from lakana_core.models.table import TableType


def _reader(star: StarWorld) -> PrincipalRef:
    """Persist a reader without any grant.

    Parameters
    ----------
    star
        The world.

    Returns
    -------
    PrincipalRef
        The reader.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name="reader", type=PrincipalType.USER)
    star.session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    star.session.commit()
    return ref


def _grant(
    star: StarWorld, reader: PrincipalRef, action: Action, kind: SecurableKind, **where: object
) -> None:
    """Grant the reader one action in `dev`.

    Parameters
    ----------
    star
        The world.
    reader
        The reader.
    action
        The action.
    kind
        The grant's securable kind.
    **where
        Its namespace, table and table id, as the kind needs.
    """
    star.session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=reader.id,
            action=action,
            kind=kind,
            environment="dev",
            environment_id=star.env_id,
            granted_by=reader.id,
            granted_at=datetime.now(UTC),
            **where,
        )
    )
    star.session.commit()


def _fan(star: StarWorld, count: int, tag: str) -> None:
    """Deploy `count` assets that each read `gold.fact_sales` and build their own table.

    Parameters
    ----------
    star
        The world.
    count
        How many.
    tag
        Makes the names unique.
    """
    gold = star.session.scalars(
        select(Namespace.id).where(
            Namespace.environment_id == star.env_id, Namespace.path == "gold"
        )
    ).one()
    assets = []
    for i in range(count):
        table = Table(
            id=uuid.uuid4(), namespace_id=gold, name=f"x{tag}_{i}", table_type=TableType.ICEBERG
        )
        star.session.add(table)
        star.session.add(
            TableColumn(id=uuid.uuid4(), table=table, name="a", type="long", position=0)
        )
        assets.append(asset_entry(f"gold.x{tag}_{i}", ("gold.fact_sales",), "o", columns=("a",)))
    repo = Repository(
        id=uuid.uuid4(), name=f"fan{tag}", kind=RepositoryKind.HTTPS, url="https://h/f"
    )
    star.session.add(repo)
    star.session.flush()
    point(star.session, star.env_id, repo, {"version": 13, "assets": assets})
    star.session.commit()


def _service(star: StarWorld, clock: Callable[[], float] | None = None) -> GraphService:
    """Build the graph service as the server does: real grants, audited.

    Parameters
    ----------
    star
        The world.
    clock
        The service's clock; the real one when `None`.

    Returns
    -------
    GraphService
        The service.
    """
    authz = AuditingAuthorization(
        GrantAuthorization(star.session), star.session, DenialAuditLimiter(10_000)
    )
    if clock is None:
        return GraphService(star.session, authz, "en")
    return GraphService(star.session, authz, "en", clock=clock)


def _statements(star: StarWorld, ask: Callable[[], object]) -> int:
    """Count the SQL statements a question issues.

    Parameters
    ----------
    star
        The world.
    ask
        The question.

    Returns
    -------
    int
        How many statements reached the database.
    """
    seen: list[object] = []

    def count(*args: object) -> None:
        """Note one statement.

        Parameters
        ----------
        *args
            What SQLAlchemy passes.
        """
        seen.append(args)

    engine = star.session.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        ask()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    return len(seen)


@pytest.mark.parametrize("question", ["lineage", "impact"])
@pytest.mark.parametrize("scope", ["fact_only", "namespace"])
def test_the_grants_cost_the_same_statements_for_twenty_assets_or_two_hundred(
    star: StarWorld, scope: str, question: str
) -> None:
    reader = _reader(star)
    _grant(star, reader, Action.READ_ENVIRONMENT, SecurableKind.ENVIRONMENT)
    if scope == "namespace":
        _grant(star, reader, Action.READ_TABLE, SecurableKind.NAMESPACE, namespace="gold")
    else:
        fact = star.table_id("gold.fact_sales")
        where = {"namespace": "gold", "table": "fact_sales", "table_id": fact}
        _grant(star, reader, Action.READ_TABLE, SecurableKind.TABLE, **where)

    def walk() -> object:
        if question == "impact":
            # The whole answer on one page: every downstream table and asset is on it.
            return _service(star).impact(reader, "dev", "gold.fact_sales")
        return _service(star).lineage(
            reader, "dev", "gold.fact_sales", direction=LineageWay.DOWN, depth=1, limit=1
        )

    _fan(star, 20, "a")
    twenty = _statements(star, walk)
    _fan(star, 180, "b")
    assert _statements(star, walk) == twenty
    if question == "impact":
        seen = _service(star).impact(reader, "dev", "gold.fact_sales").nodes
        downstream = [n for n in seen if (n.reason or "").startswith("downstream")]
        assert len(downstream) == (0 if scope == "fact_only" else 400)
        return
    seen = _service(star).lineage(reader, "dev", "gold.fact_sales", direction=LineageWay.DOWN)
    assert len(seen.nodes) == (1 if scope == "fact_only" else 401)


def _clock(times: list[float]) -> Callable[[], float]:
    """Give a clock that reads `times`, then stays past every deadline.

    Parameters
    ----------
    times
        The first readings.

    Returns
    -------
    Callable
        The clock.
    """
    readings: Iterator[float] = itertools.chain(times, itertools.repeat(1000.0))
    return lambda: next(readings)


def test_the_time_budget_covers_the_visibility_batch_and_hidden_neighbours(
    star: StarWorld,
) -> None:
    reader = _reader(star)
    _grant(star, reader, Action.READ_ENVIRONMENT, SecurableKind.ENVIRONMENT)
    fact = star.table_id("gold.fact_sales")
    where = {"namespace": "gold", "table": "fact_sales", "table_id": fact}
    _grant(star, reader, Action.READ_TABLE, SecurableKind.TABLE, **where)
    _fan(star, 300, "t")
    # Started, loaded, then past the deadline once the visibility batch is answered.
    with pytest.raises(GraphBusyError):
        _service(star, _clock([0.0, 0.0])).neighbours(reader, "dev", "gold.fact_sales")
    # In time after the batch, past it while looking at 300 edges to hidden assets.
    with pytest.raises(GraphBusyError):
        _service(star, _clock([0.0, 0.0, 0.0])).neighbours(reader, "dev", "gold.fact_sales")
    page = _service(star, _clock([0.0] * 10)).neighbours(reader, "dev", "gold.fact_sales")
    assert page.nodes[0].name == "gold.fact_sales"


def test_what_belongs_to_a_table_the_load_missed_is_left_out(
    star: StarWorld, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table created while the graph loads: its reference, measure and policies are not loaded."""
    fact = star.table_id("gold.fact_sales")
    reference = star.reference("gold.fact_sales", "store_id")
    measure = star.measure("gold.fact_sales", "net_amount")
    row_filter = star.policy(NodeKind.ROW_FILTER)
    real = graph_load.EnvironmentGraph.add_table

    def skip_fact(graph: graph_load.EnvironmentGraph, record: graph_load.TableRecord) -> None:
        """Add every table but the fact, as the race leaves it.

        Parameters
        ----------
        graph
            The graph.
        record
            The table.
        """
        if record.id != fact:
            real(graph, record)

    monkeypatch.setattr(graph_load.EnvironmentGraph, "add_table", skip_fact)
    graph = graph_load.load_graph(star.session, star.env_id, "dev")
    assert {reference, measure, row_filter}.isdisjoint(graph.records)
    assert fact not in graph.tables
    service = GraphService(star.session, AllowAllAuthorization(), "en")
    near = service.neighbours(principal, "dev", "gold.dim_store")
    assert reference not in {n.id for n in near.nodes}
    for node in (reference, measure, row_filter):
        with pytest.raises(NotFoundError):
            service.neighbours(principal, "dev", node)
