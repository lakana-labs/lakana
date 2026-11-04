"""A deploy that removes a table or column warns of what other repositories declare (#374, Q5)."""

from __future__ import annotations

import dataclasses
import shlex
import time
import uuid
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from typing import Any

import pytest
from graph_test_helpers import Grants, StarWorld, asset_entry, build_star, point, stores_manifest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session
from test_graph_real_grants import _statements
from test_projects_deploys import _claimed, _principal

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    CiScope,
    PrincipalRef,
    Securable,
    SecurableKind,
    TaskScope,
)
from lakana_core.catalog import graph_service
from lakana_core.catalog.glossary import write_glossary
from lakana_core.catalog.graph_model import GraphNode
from lakana_core.catalog.graph_service import GraphService, _Context, _Found
from lakana_core.errors import ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    DeployKind,
    DeployRun,
    DeployStatus,
    Namespace,
    Repository,
    RepositoryKind,
    Table,
    TableColumn,
    TextTranslation,
)
from lakana_core.models.table import TableType
from lakana_core.projects.deploys import IMPACT_WARNING, DeployService, _removed_items
from lakana_core.projects.diff import DeployDiff

_COLUMN = (
    "this deploy removes the column `store_id` of `gold.dim_store`, which other repositories' "
    "declarations use; tell their owners, or keep it, and see everything it affects with: lakana "
    "graph impact --env dev gold.dim_store --column store_id — the declarations: check "
    "`gold.fact_sales/ref_store_id` (sales), glossary term `shop` (glossary), reference "
    "`gold.fact_sales.store_id → gold.dim_store.store_id` (sales)"
)


@pytest.fixture
def star(session: Session, tmp_path: Path, principal: PrincipalRef) -> StarWorld:
    """Provide the star, and a repository `glossary` whose term `shop` links `dim_store.store_id`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The creating principal.

    Returns
    -------
    StarWorld
        The world.
    """
    world = build_star(session, tmp_path, principal)
    repo = Repository(
        id=uuid.uuid4(), name="glossary", kind=RepositoryKind.HTTPS, url="https://h/g"
    )
    session.add(repo)
    session.flush()
    manifest = {
        "version": 13,
        "main_language": "en",
        "assets": [],
        "glossary": {
            "shop": {
                "name": "Shop",
                "definition": "A store.",
                "owner": "x",
                "links": {"gold.dim_store": ["store_id"]},
                "related": [],
            }
        },
    }
    point(session, world.env_id, repo, manifest)
    write_glossary(session, world.env_id, repo.id, manifest)
    session.commit()
    world.repos["glossary"] = repo
    return world


def _stores_without(*, column: bool) -> dict[str, Any]:
    """Build `stores`' next manifest: `gold.dim_store` without `store_id`, or without the asset.

    Parameters
    ----------
    column
        Remove only the column (`True`) or the whole asset (`False`).

    Returns
    -------
    dict
        The manifest.
    """
    manifest = stores_manifest()
    if column:
        manifest["assets"][0]["columns"] = ["store_name"]
    else:
        manifest["assets"] = []
    return manifest


def _deploy(
    star: StarWorld,
    p: PrincipalRef,
    grants: Grants,
    manifest: dict[str, Any],
    kind: DeployKind = DeployKind.DEPLOY,
    clock: Callable[[], float] = time.monotonic,
) -> DeployRun:
    """Submit and record a deploy (or dry run) of `stores` with `manifest`, as `p`.

    Parameters
    ----------
    star
        The world.
    p
        The deployer.
    grants
        What the deployer may do.
    manifest
        The new manifest.
    kind
        `DeployKind.DEPLOY` or `DeployKind.DRY_RUN`.
    clock
        The clock of the graph check.

    Returns
    -------
    DeployRun
        The recorded run.
    """
    service = DeployService(star.session, grants, graph_clock=clock)
    view = service.submit(p, "dev", repository="stores", kind=kind, ref="main")
    run = _claimed(star.session, star.session.get(DeployRun, view.id))
    run.resolved_sha = uuid.uuid4().hex + "00000000"  # each deploy its own commit
    if kind is DeployKind.DRY_RUN:
        service.record_dry_run(p, run, diff=DeployDiff(), worker_id="w1", manifest=manifest)
    else:
        current = service.current_job_version(star.env_id, star.repos["stores"].id)
        service.record_deploy(
            p,
            run,
            manifest=manifest,
            code_uri="u",
            code_sha256="0" * 64,
            diff=DeployDiff(),
            expected_job_version_id=current.id if current else None,
            worker_id="w1",
        )
    return run


def _stored(run: DeployRun) -> list[Any]:
    """Read a run's stored warnings, which a recorded run always has.

    Parameters
    ----------
    run
        The recorded run.

    Returns
    -------
    list
        Its `warnings_json`.
    """
    assert run.warnings_json is not None
    return run.warnings_json


def _everything() -> Grants:
    """Let the deployer do and read everything.

    Returns
    -------
    Grants
        The grants.
    """
    return Grants(others=True)


def test_removing_a_referenced_column_warns_with_what_other_repositories_declare(
    star: StarWorld,
) -> None:
    p = _principal(star.session, "deployer")
    run = _deploy(star, p, _everything(), _stores_without(column=True))
    assert run.warnings_json == [
        {"warning": _COLUMN, "tables": ["gold.dim_store", "gold.fact_sales"]}
    ]


def test_a_table_no_longer_built_warns_with_the_whole_tables_dependents(star: StarWorld) -> None:
    p = _principal(star.session, "deployer")
    run = _deploy(star, p, _everything(), _stores_without(column=False))
    (entry,) = _stored(run)
    assert entry["warning"].startswith(
        "this deploy no longer builds `gold.dim_store`, which other repositories' declarations "
        "use; tell their owners, or keep it, and see everything it affects with: lakana graph "
        "impact --env dev gold.dim_store — the declarations: "
    )
    assert (
        "reference `gold.fact_sales.store_id → gold.dim_store.store_id` (sales)"
        in (entry["warning"])
    )
    assert "glossary term `shop` (glossary)" in entry["warning"]
    assert "(stores)" not in entry["warning"]  # its own repository's declarations are not another's


def test_the_deployers_visibility_leaves_out_what_it_may_not_see(star: StarWorld) -> None:
    p = _principal(star.session, "deployer")
    limited = Grants({"gold.dim_store", "bronze.stores_raw"}, others=True)
    (entry,) = _stored(_deploy(star, p, limited, _stores_without(column=True)))
    assert "fact_sales" not in entry["warning"]
    assert "glossary term `shop` (glossary)" in entry["warning"]
    assert entry["tables"] == ["gold.dim_store"]


def test_a_reader_sees_a_stored_warning_only_when_it_may_read_every_table_it_names(
    star: StarWorld,
) -> None:
    p = _principal(star.session, "deployer")
    run = _deploy(star, p, _everything(), _stores_without(column=True))
    run.warnings_json = ["a job warning", *_stored(run)]
    star.session.commit()

    def warnings_for(reader: PrincipalRef, grants: Grants) -> list[str]:
        return DeployService(star.session, grants).get(reader, "dev", run.id).warnings

    assert warnings_for(p, Grants(set(), others=True)) == ["a job warning", _COLUMN]
    other = _principal(star.session, "reader")
    assert warnings_for(other, Grants({"gold.dim_store"}, others=True)) == ["a job warning"]
    assert warnings_for(other, Grants(others=True)) == ["a job warning", _COLUMN]
    # A glossary term needs READ_ENVIRONMENT; a reader admitted by DEPLOY alone is not shown it.
    assert warnings_for(other, Grants(others=True, environment=False)) == ["a job warning"]


def test_a_dry_run_and_a_rollback_warn_too(star: StarWorld) -> None:
    p = _principal(star.session, "deployer")
    dry = _deploy(star, p, _everything(), _stores_without(column=True), DeployKind.DRY_RUN)
    assert [e["warning"] for e in _stored(dry) if isinstance(e, dict)] == [_COLUMN]
    removed = _deploy(star, p, _everything(), _stores_without(column=True))
    _deploy(star, p, _everything(), stores_manifest())  # the column is back
    service = DeployService(star.session, _everything())
    view = service.submit(
        p, "dev", repository="stores", kind=DeployKind.ROLLBACK, to_sha=removed.resolved_sha
    )
    rollback = _claimed(star.session, star.session.get(DeployRun, view.id))
    service.execute_rollback(p, rollback, worker_id="w1")
    assert [e["warning"] for e in _stored(rollback) if isinstance(e, dict)] == [_COLUMN]


def test_an_unknown_column_list_warns_of_no_column(star: StarWorld) -> None:
    manifest = stores_manifest()
    manifest["assets"][0]["columns"] = None
    p = _principal(star.session, "deployer")
    assert _deploy(star, p, _everything(), manifest).warnings_json == []


def test_more_than_twenty_removals_add_one_line(star: StarWorld) -> None:
    p = _principal(star.session, "deployer")
    wide = stores_manifest()
    wide["assets"][0]["columns"] = ["store_id", "store_name", *(f"c{i}" for i in range(24))]
    _deploy(star, p, _everything(), wide)
    run = _deploy(star, p, _everything(), _stores_without(column=True))
    lines = [e["warning"] for e in _stored(run)]
    assert lines[0] == _COLUMN
    assert lines[1:] == [
        "this deploy also removes 5 more tables or columns that were not checked against other "
        "repositories; see each with: lakana graph impact --env dev TABLE"
    ]
    reader = _principal(star.session, "reader")
    assert DeployService(star.session, _everything()).get(reader, "dev", run.id).warnings == lines


def test_a_check_past_its_budget_leaves_one_line_and_still_deploys(
    star: StarWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    budgets: list[float] = []

    def refuse(self: GraphService, *args: object, **kwargs: object) -> None:
        budgets.append(self._budget_seconds)
        raise ValidationError("too large")

    monkeypatch.setattr(GraphService, "breakage", refuse)
    p = _principal(star.session, "deployer")
    run = _deploy(star, p, _everything(), _stores_without(column=True))
    assert run.warnings_json == [
        {
            "warning": "this deploy removes or stops building 1 table or column, and Lakana "
            "could not check in time what other repositories declare on it; see it with: "
            "lakana graph impact --env dev gold.dim_store --column store_id",
            "tables": ["gold.dim_store"],
        }
    ]
    assert run.job_version_id is not None
    assert budgets == [2.0]  # short, as the walk runs while the deploy holds its locks


def test_malformed_stored_warnings_are_skipped(star: StarWorld) -> None:
    p = _principal(star.session, "deployer")
    run = _deploy(star, p, _everything(), stores_manifest())
    run.warnings_json = [
        "kept",
        7,
        {"warning": 1, "tables": []},
        {"warning": "x", "tables": "gold.dim_store"},
        {"warning": "y", "tables": [{"not": "a name"}]},
        {"warning": "z", "tables": ["not a table name"]},
    ]
    star.session.commit()
    assert DeployService(star.session, _everything()).get(p, "dev", run.id).warnings == [
        "kept",
        "y",  # the deployer is shown what it stored, whatever the tables say
        "z",
    ]
    reader = _principal(star.session, "reader")
    assert DeployService(star.session, _everything()).get(reader, "dev", run.id).warnings == [
        "kept"
    ]


def test_breakage_names_a_references_and_a_ref_checks_target_table(star: StarWorld) -> None:
    p = _principal(star.session, "deployer")
    service = GraphService(star.session, _everything())
    nodes = ["column:gold.fact_sales/date", "gold.nothing", "column:gold.fact_sales/net_amount"]
    answers = service.breakage(p, "dev", nodes)
    assert set(answers) == {nodes[0], nodes[2]}
    measures = {g.name: tables for g, tables in answers[nodes[2]] if g.kind.value == "measure"}
    assert measures == {"gold.fact_sales.net_amount": ("gold.fact_sales",)}
    named = {(g.kind.value, g.name): tables for g, tables in answers["column:gold.fact_sales/date"]}
    assert named["check", "gold.fact_sales/ref_date"] == ("gold.fact_sales", "gold.dim_date")
    assert named["reference", "gold.fact_sales.date → gold.dim_date.date"] == (
        "gold.fact_sales",
        "gold.dim_date",
    )
    # Only declarations that name the column: not its table, nothing downstream.
    assert {kind for kind, _ in named} <= {"reference", "measure", "check", "term"}


def test_a_slow_node_build_ends_in_the_unchecked_line_within_the_budget(
    star: StarWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    read = graph_service._translation_rows

    def slow_rows(
        session: Session, table_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, list[TextTranslation]]:
        now[0] += 3.0  # building the nodes takes longer than the whole 2 s budget
        return read(session, table_ids)

    monkeypatch.setattr(graph_service, "_translation_rows", slow_rows)
    p = _principal(star.session, "deployer")
    run = _deploy(star, p, _everything(), _stores_without(column=True), clock=lambda: now[0])
    assert [e["warning"] for e in _stored(run)] == [
        "this deploy removes or stops building 1 table or column, and Lakana could not check in "
        "time what other repositories declare on it; see it with: lakana graph impact "
        "--env dev gold.dim_store --column store_id"
    ]
    assert run.job_version_id is not None


def test_a_name_the_graph_cannot_read_gets_its_own_line(star: StarWorld) -> None:
    p = _principal(star.session, "deployer")
    odd = "a\u200eb"  # a format character, which no node name may hold
    wide = stores_manifest()
    wide["assets"][0]["columns"] = ["store_id", "store_name", odd]
    _deploy(star, p, _everything(), wide)
    run = _deploy(star, p, _everything(), _stores_without(column=True))
    lines = [e["warning"] for e in _stored(run)]
    assert lines == [
        _COLUMN,
        "this deploy removes the column `a\\u200eb` of `gold.dim_store`, whose name holds a "
        "control or format character or is too long to look up in the metadata graph, so Lakana "
        "did not check what other repositories declare on it; rename it, or see what "
        "depends on its table with: lakana graph impact --env dev gold.dim_store",
    ]


def test_several_removals_past_the_check_are_counted_in_the_plural(star: StarWorld) -> None:
    p = _principal(star.session, "deployer")
    wide = stores_manifest()
    wide["assets"][0]["columns"] = ["store_id", "store_name", *(f"c{i}" for i in range(20))]
    _deploy(star, p, _everything(), wide)
    run = _deploy(star, p, _everything(), _stores_without(column=True))
    assert [e["warning"] for e in _stored(run)][-1] == (
        "this deploy also removes 1 more table or column that was not checked against other "
        "repositories; see each with: lakana graph impact --env dev TABLE"
    )


def _service_account(star: StarWorld, admin: PrincipalRef) -> PrincipalRef:
    """Persist ``ci-dev`` as the documented CI setup has it: `DEPLOY` and `VIEWER` on `dev`.

    Parameters
    ----------
    star
        The world.
    admin
        Who grants.

    Returns
    -------
    PrincipalRef
        The account, without any token scope.
    """
    p = _principal(star.session, "ci-dev")
    grants = GrantService(star.session, AllowAllAuthorization())
    grants.grant(
        admin,
        principal="ci-dev",
        action=Action.DEPLOY,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    grants.grant(
        admin,
        principal="ci-dev",
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    return p


def _ci_token(star: StarWorld, p: PrincipalRef, run: DeployRun) -> PrincipalRef:
    """Make `p`'s CI token, as the one that queued `run`.

    Parameters
    ----------
    star
        The world.
    p
        The service account.
    run
        The run; its `ci_token_id` is set, so the token may read it.

    Returns
    -------
    PrincipalRef
        The token's principal.
    """
    scope = CiScope(
        token_id=uuid.uuid4(),
        rule_id=uuid.uuid4(),
        environment="dev",
        environment_id=star.env_id,
        repository="stores",
        repository_id=star.repos["stores"].id,
        kinds=frozenset({"DEPLOY"}),
        sha=None,
    )
    run.ci_token_id = scope.token_id
    star.session.commit()
    return dataclasses.replace(p, ci_scope=scope)


_OTHERS = ("fact_sales", "ref_store_id", "shop", "(sales)", "(glossary)", "store_id")
"""What the warning names of other repositories, and of the removed column."""


def test_a_ci_token_is_shown_no_impact_warning_its_principals_grants_would_show(
    star: StarWorld, principal: PrincipalRef
) -> None:
    p = _service_account(star, principal)
    run = _deploy(star, p, _everything(), _stores_without(column=True))
    token = _ci_token(star, p, run)
    service = DeployService(star.session, GrantAuthorization(star.session))
    assert service.get(p, "dev", run.id).warnings == [_COLUMN]  # the account itself sees it
    shown = " ".join(service.get(token, "dev", run.id).warnings)
    for name in _OTHERS:
        assert name not in shown


def test_a_task_token_of_the_deployer_is_shown_no_impact_warning_outside_its_scope(
    star: StarWorld, principal: PrincipalRef
) -> None:
    p = _service_account(star, principal)
    run = _deploy(star, p, _everything(), _stores_without(column=True))
    task = dataclasses.replace(
        p, task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4())
    )
    service = DeployService(star.session, GrantAuthorization(star.session))
    shown = " ".join(service.get(task, "dev", run.id).warnings)
    for name in _OTHERS:
        assert name not in shown


def test_a_ci_token_is_shown_a_failed_deploys_error_as_an_outside_reader_is(
    star: StarWorld, principal: PrincipalRef
) -> None:
    p = _service_account(star, principal)
    run = _deploy(star, p, _everything(), stores_manifest())
    run.status = DeployStatus.FAILED
    run.error_message = "the deploy failed\nreferences `gold.secret.id`, which is missing"
    run.error_withheld_json = [{"index": 1, "withheld": "", "targets": ["gold.secret"]}]
    token = _ci_token(star, p, run)
    service = DeployService(star.session, GrantAuthorization(star.session))
    assert service.get(p, "dev", run.id).error_message == run.error_message
    assert service.get(token, "dev", run.id).error_message == "the deploy failed"


_EXTRA = [f"c{i}" for i in range(19)]
"""Columns of `gold.dim_store` that a deploy removes together with `store_id`: 20 removals."""


def _wide_dim_store(star: StarWorld, p: PrincipalRef) -> None:
    """Give `gold.dim_store` the columns `_EXTRA` in the catalog and in `stores`' manifest.

    Parameters
    ----------
    star
        The world.
    p
        The deployer.
    """
    table = star.session.get(Table, star.table_id("gold.dim_store"))
    for i, name in enumerate(_EXTRA):
        star.session.add(
            TableColumn(id=uuid.uuid4(), table=table, name=name, type="long", position=10 + i)
        )
    star.session.commit()
    wide = stores_manifest()
    wide["assets"][0]["columns"] = ["store_id", "store_name", *_EXTRA]
    _deploy(star, p, _everything(), wide)


def _downstream(star: StarWorld, count: int, tag: str) -> None:
    """Deploy `count` assets of a new repository that each read `gold.dim_store`.

    Each builds its own table, which exists, so everything is downstream of `gold.dim_store`.
    The rows are inserted in bulk, to keep the test short.

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
    ids = [uuid.uuid4() for _ in range(count)]
    names = [f"x{tag}_{i}" for i in range(count)]
    star.session.execute(
        insert(Table),
        [
            {"id": i, "namespace_id": gold, "name": n, "table_type": TableType.ICEBERG}
            for i, n in zip(ids, names, strict=True)
        ],
    )
    star.session.execute(
        insert(TableColumn),
        [
            {"id": uuid.uuid4(), "table_id": i, "name": "a", "type": "long", "position": 0}
            for i in ids
        ],
    )
    assets = [asset_entry(f"gold.{n}", ("gold.dim_store",), "o", columns=("a",)) for n in names]
    repo = Repository(
        id=uuid.uuid4(), name=f"fan{tag}", kind=RepositoryKind.HTTPS, url="https://h/f"
    )
    star.session.add(repo)
    star.session.flush()
    point(star.session, star.env_id, repo, {"version": 13, "assets": assets})
    star.session.commit()


@pytest.mark.parametrize("count", [2_000, 5_500])
def test_twenty_removals_with_much_downstream_report_the_declarations_well_within_the_budget(
    star: StarWorld, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    p = _principal(star.session, "deployer")
    _wide_dim_store(star, p)
    _downstream(star, count, "d")
    spent: list[float] = []
    built: list[int] = []
    breakage, nodes = GraphService.breakage, GraphService._nodes

    def timed(
        self: GraphService, p: PrincipalRef, environment: str, removed: Sequence[str]
    ) -> dict[str, tuple[tuple[GraphNode, tuple[str, ...]], ...]]:
        started = time.perf_counter()
        try:
            return breakage(self, p, environment, removed)
        finally:
            spent.append(time.perf_counter() - started)

    def counted(
        self: GraphService,
        ctx: _Context,
        entries: Sequence[tuple[str, _Found]],
        languages: Sequence[str],
    ) -> list[GraphNode]:
        built.append(len(entries))
        return nodes(self, ctx, entries, languages)

    monkeypatch.setattr(GraphService, "breakage", timed)
    monkeypatch.setattr(GraphService, "_nodes", counted)
    run = _deploy(star, p, _everything(), _stores_without(column=True))
    assert [e["warning"] for e in _stored(run)] == [_COLUMN]  # never the "could not check" line
    assert spent[0] < 0.5  # a quarter of the 2 s budget, which the deploy's locks wait on
    # Only the declarations naming `store_id` are built, nothing downstream: the three of other
    # repositories, and `stores`' own two key checks and term.
    assert built == [6]


def test_the_deploy_check_costs_the_same_statements_however_much_lies_downstream(
    star: StarWorld,
) -> None:
    p = _principal(star.session, "deployer")
    _wide_dim_store(star, p)
    nodes = [f"column:gold.dim_store/{c}" for c in ("store_id", *_EXTRA)]

    def check() -> object:
        return GraphService(star.session, _everything()).breakage(p, "dev", nodes)

    _downstream(star, 20, "s")
    few = _statements(star, check)
    _downstream(star, 400, "m")
    assert _statements(star, check) == few


def _removed_column(column: str) -> tuple[str, str, str, str]:
    """Read the item of a manifest change that removes one column of `gold.t`.

    Parameters
    ----------
    column
        The removed column.

    Returns
    -------
    tuple
        The item, as `_removed_items` lists it.
    """
    old = {"version": 13, "assets": [{"table": "gold.t", "columns": ["id", column]}]}
    new = {"version": 13, "assets": [{"table": "gold.t", "columns": ["id"]}]}
    (item,) = _removed_items(old, new)
    return item


@pytest.mark.parametrize("column", ["größe", "a b", "it's", "$(x)"])
def test_the_suggested_command_runs_as_pasted_and_keeps_non_ascii_names(column: str) -> None:
    node, what, _, command = _removed_column(column)
    assert node == f"column:gold.t/{column}"
    assert what == f"removes the column `{column}` of `gold.t`"  # größe stays größe
    assert command == f"gold.t --column {shlex.quote(column)}"  # `$(x)` quoted, never run
    assert shlex.split(command) == ["gold.t", "--column", column]


def test_a_long_column_name_never_pushes_the_environment_past_the_cli_cut() -> None:
    column = "c" * 2_000
    _, what, _, command = _removed_column(column)
    assert command == "gold.t"  # too long to type: the command asks about the table
    text = IMPACT_WARNING.format(
        what=what, items="reference `x` (sales)", command=command, env="production"
    )
    assert text.index("--env production") + len("--env production") < 500  # the CLI cuts at 500


def test_a_name_the_graph_cannot_read_suggests_its_table_only() -> None:
    _, what, _, command = _removed_column("a\u200eb")
    assert what == "removes the column `a\\u200eb` of `gold.t`"
    assert command == "gold.t"


def test_a_table_name_that_cannot_be_pasted_is_shown_escaped() -> None:
    old = {"version": 13, "assets": [{"table": "gold.a\u200eb", "columns": None}]}
    ((_, what, _, command),) = _removed_items(old, {"version": 13, "assets": []})
    assert what == "no longer builds `gold.a\\u200eb`"
    assert command == "gold.a\\u200eb"


class _Counted(Grants):
    """`Grants` that answers in batches and counts the table questions it is asked.

    Parameters
    ----------
    tables
        The dotted tables the reader may read; `None` for every table.
    """

    def __init__(self, tables: Collection[str] | None = None) -> None:
        super().__init__(tables, others=True)
        self.asked: list[str] = []

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `Grants`, noting each table asked about.

        Parameters
        ----------
        principal
            The reader.
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
            self.asked.append(f"{securable.namespace}.{securable.table}")
        return super().permits(principal, action, securable)

    def permits_each(
        self, principal: PrincipalRef, questions: Sequence[tuple[Action, Securable]]
    ) -> list[bool]:
        """Answer each question as `permits` does.

        Parameters
        ----------
        principal
            The reader.
        questions
            ``(action, securable)`` pairs.

        Returns
        -------
        list of bool
            The answers.
        """
        return [self.permits(principal, a, s) for a, s in questions]


def test_a_listing_asks_about_each_table_its_stored_warnings_name_once(star: StarWorld) -> None:
    p = _principal(star.session, "deployer")
    tables = [f"gold.t{i}" for i in range(300)]
    for _ in range(3):
        run = _deploy(star, p, _everything(), stores_manifest())
        run.warnings_json = [
            {"warning": "more", "tables": tables},
            {"warning": "x", "tables": tables},
        ]
    star.session.commit()
    reader = _principal(star.session, "reader")
    every = _Counted()
    views = DeployService(star.session, every).history(reader, "dev")
    assert sorted(every.asked) == sorted(tables)  # 300 questions for 3 runs of 2 warnings each
    assert [v.warnings for v in views if v.warnings] == [["more", "x"]] * 3
    some = _Counted(tables[:-1])
    views = DeployService(star.session, some).history(reader, "dev")
    assert [v.warnings for v in views if v.warnings] == []


def test_a_removed_item_the_graph_cannot_read_escapes_its_tables_name(star: StarWorld) -> None:
    p = _principal(star.session, "deployer")
    table = "gold.a b\x1b[2J"
    old = {"version": 13, "assets": [{"table": table, "columns": ["id", "c"]}]}
    new = {"version": 13, "assets": [{"table": table, "columns": ["id"]}]}
    (entry,) = DeployService(star.session, _everything())._impact_warnings(
        p, "dev", "stores", old, new
    )
    text = str(entry["warning"])
    assert "\x1b" not in text
    assert "`gold.a b\\x1b[2J`" in text
    assert text.endswith("lakana graph impact --env dev gold.a b\\x1b[2J")
    assert entry["tables"] == [table]
