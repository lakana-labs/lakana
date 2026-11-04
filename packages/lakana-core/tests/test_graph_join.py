"""Join paths between two tables over declared references (#374, Q3)."""

from __future__ import annotations

import uuid

import pytest
from graph_test_helpers import Grants, StarWorld, asset_entry, point
from graph_test_helpers import graph_service_for as _service
from sqlalchemy import select

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.descriptions import sync_project_descriptions
from lakana_core.catalog.graph_join import JoinPaths, JoinStep
from lakana_core.catalog.graph_model import NO_PATH, table_node
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.models import (
    DeclaredReference,
    Namespace,
    Repository,
    RepositoryKind,
    Table,
    TableColumn,
    TableType,
)

_Refs = dict[str, list[tuple[str, str, str]]]


def _chain(star: StarWorld, repository: str, refs: _Refs) -> None:
    """Add catalog rows for new `gold` tables, and a repository whose assets declare references.

    No Iceberg table is written: the graph reads only catalog rows and manifests.

    Parameters
    ----------
    star
        The world.
    repository
        The new repository's name.
    refs
        Dotted `gold` table → its ``(column, target table, target column)`` references. A table
        the star does not have, target or not, gets `id` and one long column per referencing
        column; a star table keeps its own columns.
    """
    gold = star.session.scalars(
        select(Namespace.id).where(
            Namespace.environment_id == star.env_id, Namespace.path == "gold"
        )
    ).one()
    existing = set(star.session.scalars(select(Table.name).where(Table.namespace_id == gold)))
    names = set(refs) | {t for triples in refs.values() for _, t, _ in triples}
    for dotted in sorted(names):
        name = dotted.removeprefix("gold.")
        if name in existing:
            continue
        table = Table(id=uuid.uuid4(), namespace_id=gold, name=name, table_type=TableType.ICEBERG)
        star.session.add(table)
        columns = ["id", *(c for c, _, _ in refs.get(dotted, []))]
        for position, column in enumerate(columns):
            star.session.add(
                TableColumn(
                    id=uuid.uuid4(), table=table, name=column, type="long", position=position
                )
            )
    repo = Repository(
        id=uuid.uuid4(), name=repository, kind=RepositoryKind.HTTPS, url=f"https://h/{repository}"
    )
    star.session.add(repo)
    star.session.flush()
    assets = [
        asset_entry(
            dotted,
            (),
            "o",
            columns=("id", *(c for c, _, _ in triples)),
            model={
                "kind": "fact",
                "grain": "g",
                "key_none": False,
                "checks": {},
                "references": [
                    {"column": c, "target": t, "target_column": tc} for c, t, tc in triples
                ],
            },
        )
        for dotted, triples in sorted(refs.items())
    ]
    manifest = {"version": 13, "assets": assets}
    point(star.session, star.env_id, repo, manifest)
    sync_project_descriptions(star.session, star.env_id, manifest)
    star.session.commit()


def _sequences(answer: JoinPaths) -> list[tuple[str, ...]]:
    """List each path of an answer as its sequence of reference ids.

    Parameters
    ----------
    answer
        The answer.

    Returns
    -------
    list of tuple
        One tuple of reference ids per path, in the answer's order.
    """
    return [tuple(s.reference for s in p.steps) for p in answer.paths]


def test_a_fact_joins_a_dimension_along_its_reference(
    star: StarWorld, principal: PrincipalRef
) -> None:
    answer = _service(star).join_path(principal, "dev", "gold.fact_sales", "gold.dim_store")
    assert (answer.reason, answer.more_paths, len(answer.paths)) == (None, False, 1)
    (step,) = answer.paths[0].steps
    assert step == JoinStep(
        reference=star.reference("gold.fact_sales", "store_id"),
        from_table=table_node(star.table_id("gold.fact_sales")),
        from_name="gold.fact_sales",
        to_table=table_node(star.table_id("gold.dim_store")),
        to_name="gold.dim_store",
        from_columns=("store_id",),
        to_columns=("store_id",),
        direction="many_to_one",
        fans_out=False,
        from_model_kind="fact",
        from_grain="One row per store and day.",
        to_model_kind="dimension",
        to_grain="One row per store.",
    )


def test_the_other_way_fans_out_and_two_dimensions_join_through_the_fact(
    star: StarWorld, principal: PrincipalRef
) -> None:
    back = _service(star).join_path(principal, "dev", "gold.dim_store", "gold.fact_sales")
    (step,) = back.paths[0].steps
    assert (step.direction, step.fans_out, step.from_columns, step.to_columns) == (
        "one_to_many",
        True,
        ("store_id",),
        ("store_id",),
    )
    assert (step.from_model_kind, step.to_model_kind) == ("dimension", "fact")
    across = _service(star).join_path(principal, "dev", "gold.dim_store", "gold.dim_date")
    assert [(s.from_name, s.to_name, s.fans_out) for s in across.paths[0].steps] == [
        ("gold.dim_store", "gold.fact_sales", True),
        ("gold.fact_sales", "gold.dim_date", False),
    ]


def test_ties_are_all_returned_in_reference_order(star: StarWorld, principal: PrincipalRef) -> None:
    _chain(
        star,
        "bridge",
        {
            "gold.bridge": [
                ("store_id", "gold.dim_store", "store_id"),
                ("date", "gold.dim_date", "date"),
            ]
        },
    )
    answer = _service(star).join_path(principal, "dev", "gold.dim_store", "gold.dim_date")
    sequences = _sequences(answer)
    assert len(sequences) == 2 and sequences == sorted(sequences)
    assert {p.steps[0].to_name for p in answer.paths} == {"gold.fact_sales", "gold.bridge"}
    assert (answer.reason, answer.more_paths) == (None, False)


def test_four_hops_join_and_five_do_not(star: StarWorld, principal: PrincipalRef) -> None:
    refs: _Refs = {f"gold.c{i}": [("next", f"gold.c{i + 1}", "id")] for i in range(5)}
    refs["gold.c0"].append(("parent", "gold.c0", "id"))  # a self-reference is never a step
    _chain(star, "chain", refs)
    star.session.add(  # a reference to a table that does not exist is never a step either
        DeclaredReference(
            id=uuid.uuid4(),
            environment_id=star.env_id,
            source_table_id=star.table_id("gold.c1"),
            source_column="id",
            target_name="gold.ghost",
            target_column="id",
        )
    )
    star.session.commit()
    four = _service(star).join_path(principal, "dev", "gold.c0", "gold.c4")
    assert [(s.from_name, s.direction) for s in four.paths[0].steps] == [
        ("gold.c0", "many_to_one"),
        ("gold.c1", "many_to_one"),
        ("gold.c2", "many_to_one"),
        ("gold.c3", "many_to_one"),
    ]
    back = _service(star).join_path(principal, "dev", "gold.c4", "gold.c0")
    assert [s.direction for s in back.paths[0].steps] == ["one_to_many"] * 4
    five = _service(star).join_path(principal, "dev", "gold.c0", "gold.c5")
    assert five.paths == () and five.more_paths is False
    assert five.reason == (
        "No declared path of at most 4 references joins `gold.c0` and `gold.c5`. Declare the "
        "reference in the asset header of one of them (references: {column: table}), or check "
        "the names with: lakana graph neighbours gold.c0 --kind references --env dev"
    )


def test_more_than_ten_paths_are_cut_with_more_paths(
    star: StarWorld, principal: PrincipalRef
) -> None:
    refs: _Refs = {
        f"gold.m{i:02d}": [("a", "gold.a", "id"), ("b", "gold.b", "id")] for i in range(12)
    }
    _chain(star, "many", refs)
    answer = _service(star).join_path(principal, "dev", "gold.a", "gold.b")
    assert len(answer.paths) == 10 and answer.more_paths is True
    sequences = _sequences(answer)
    assert sequences == sorted(sequences)
    # Hiding two of the twelve leaves ten: no hint that more exist.
    seen = {"gold.a", "gold.b", *(f"gold.m{i:02d}" for i in range(10))}
    hidden = _service(star, Grants(seen)).join_path(principal, "dev", "gold.a", "gold.b")
    assert (len(hidden.paths), hidden.more_paths) == (10, False)
    assert {p.steps[0].to_name for p in hidden.paths} == seen - {"gold.a", "gold.b"}


def test_a_hidden_table_cuts_every_path_through_it_and_says_only_no_path(
    star: StarWorld, principal: PrincipalRef
) -> None:
    reader = _service(star, Grants({"gold.dim_store", "gold.dim_date"}))
    answer = reader.join_path(principal, "dev", "gold.dim_store", "gold.dim_date")
    assert answer.paths == () and answer.more_paths is False
    # The same text as for two tables no declaration joins at all.
    absent = _service(star).join_path(principal, "dev", "bronze.stores_raw", "gold.dim_date")
    assert absent.paths == () and absent.reason is not None
    assert answer.reason == NO_PATH.format(start="gold.dim_store", goal="gold.dim_date", env="dev")
    assert absent.reason == NO_PATH.format(
        start="bronze.stores_raw", goal="gold.dim_date", env="dev"
    )


def test_a_hidden_tie_is_cut_as_if_it_did_not_exist(
    star: StarWorld, principal: PrincipalRef
) -> None:
    _chain(
        star,
        "bridge",
        {
            "gold.bridge": [
                ("store_id", "gold.dim_store", "store_id"),
                ("date", "gold.dim_date", "date"),
            ]
        },
    )
    without_fact = Grants({"gold.dim_store", "gold.dim_date", "gold.bridge"})
    answer = _service(star, without_fact).join_path(
        principal, "dev", "gold.dim_store", "gold.dim_date"
    )
    assert [[s.to_name for s in p.steps] for p in answer.paths] == [
        ["gold.bridge", "gold.dim_date"]
    ]
    assert (answer.reason, answer.more_paths) == (None, False)
    # With `gold.dim_date` hidden, the references to it are no steps (R3): the one way left
    # from the fact to the bridge runs through `gold.dim_store`.
    no_date = Grants({"gold.dim_store", "gold.fact_sales", "gold.bridge"})
    near = _service(star, no_date).join_path(principal, "dev", "gold.fact_sales", "gold.bridge")
    assert [[s.to_name for s in p.steps] for p in near.paths] == [["gold.dim_store", "gold.bridge"]]


@pytest.mark.parametrize(
    ("start", "goal", "error", "message"),
    [
        (
            "gold.dim_store",
            "gold.dim_store",
            ValidationError,
            "from and to are the same table, `gold.dim_store`, and a join path joins two tables. "
            "To see what it joins to, ask: lakana graph neighbours gold.dim_store --kind "
            "references --env dev",
        ),
        (
            "term:store",
            "gold.dim_store",
            ValidationError,
            "A join path runs between two tables, and `term:store` is a glossary term. Give the "
            "table's dotted name, such as gold.fact_sales.",
        ),
        (
            "gold.dim_store",
            "column:gold.dim_store/store_id",
            ValidationError,
            "A join path runs between two tables, and `column:gold.dim_store/store_id` is a "
            "column.",
        ),
        (
            "gold.dim_store",
            "gold.nope",
            NotFoundError,
            "There is no node `gold.nope` in environment `dev` that you may see.",
        ),
    ],
)
def test_join_path_refusals_name_the_next_step(
    star: StarWorld,
    principal: PrincipalRef,
    start: str,
    goal: str,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error) as caught:
        _service(star).join_path(principal, "dev", start, goal)
    assert str(caught.value).startswith(message)


def test_the_same_table_by_id_and_by_name_is_refused(
    star: StarWorld, principal: PrincipalRef
) -> None:
    by_id = table_node(star.table_id("gold.dim_store"))
    with pytest.raises(ValidationError, match=r"same table, `gold\.dim_store`"):
        _service(star).join_path(principal, "dev", by_id, "gold.dim_store")
    hidden = _service(star, Grants({"gold.dim_date"}))
    with pytest.raises(NotFoundError):  # a hidden table twice reads as missing, not the same
        hidden.join_path(principal, "dev", "gold.dim_store", "gold.dim_store")
