"""A deploy that removes a table or column warns of what other repositories declare (#374, Q5)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from graph_test_helpers import Grants, StarWorld, build_star, point, stores_manifest
from sqlalchemy.orm import Session
from test_projects_deploys import _claimed, _principal

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.glossary import write_glossary
from lakana_core.catalog.graph_service import GraphService
from lakana_core.errors import ValidationError
from lakana_core.models import DeployKind, DeployRun, Repository, RepositoryKind
from lakana_core.projects.deploys import DeployService
from lakana_core.projects.diff import DeployDiff

_COLUMN = (
    "this deploy removes the column `store_id` of `gold.dim_store`, which other repositories' "
    "declarations use; tell their owners, or keep it, and see everything it affects with: lakana "
    "graph impact gold.dim_store --column store_id --env dev — the declarations: check "
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

    Returns
    -------
    DeployRun
        The recorded run.
    """
    service = DeployService(star.session, grants)
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
        "impact gold.dim_store --env dev — the declarations: "
    )
    assert (
        "reference `gold.fact_sales.store_id → gold.dim_store.store_id` (sales)"
        in (entry["warning"])
    )
    assert "glossary term `shop` (glossary)" in entry["warning"]
    assert "`store` (stores)" not in entry["warning"]  # its own repository's term is not another's


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
        "repositories; see each with: lakana graph impact TABLE --env dev"
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
            "warning": "this deploy removes or stops building 1 tables or columns, and Lakana "
            "could not check in time what other repositories declare on them; see it with: "
            "lakana graph impact gold.dim_store --column store_id --env dev",
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
    ]
    star.session.commit()
    assert DeployService(star.session, _everything()).get(p, "dev", run.id).warnings == ["kept"]


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
    assert named["table", "gold.fact_sales"] == ()
