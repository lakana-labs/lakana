"""A run that retypes a column warns of what other repositories use it for (#446)."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pyarrow as pa
import pytest
from core_test_helpers import _prepared_catalog, python_task_run
from graph_test_helpers import TABLES, Grants, StarWorld, point, sales_manifest
from sqlalchemy import select

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, RunContext
from lakana_core.catalog.graph_service import GraphService
from lakana_core.catalog.retype_impact import ImpactUseView, ImpactWarningView
from lakana_core.errors import GraphBusyError
from lakana_core.identifiers import TableRef
from lakana_core.models import Environment, Run, TaskRun, WriteMode
from lakana_core.policy.schema_change import COLUMNS_CHANGED_NOTE
from lakana_core.runs.service import RunCaps, RunService

_DIM = "gold.dim_store"


def _retype_the_store_name(star: StarWorld, principal: PrincipalRef, tmp_path: Path) -> uuid.UUID:
    """Let `sales` read `store_name`, then retype it (string to long) in a run of `stores`.

    `gold.fact_sales` lists its columns and traces `net_amount` to `gold.dim_store.store_name`,
    so its asset, its measure `net_amount` and the term `net_revenue` on that column use it.

    Parameters
    ----------
    star
        The world.
    principal
        The run principal.
    tmp_path
        The storage root.

    Returns
    -------
    uuid.UUID
        The run.
    """
    sales = sales_manifest()
    sales["assets"][2]["lineage"] = {
        "net_amount": [{"table": _DIM, "column": "store_name"}],
    }
    point(star.session, star.env_id, star.repos["sales"], sales)
    catalog = _prepared_catalog(star.session, tmp_path, AllowAllAuthorization())
    star.session.scalars(select(Environment)).one().is_protected = False
    star.session.commit()
    task = python_task_run(star.session, principal.id, asset=_DIM)
    run = star.session.get(Run, task.run_id)
    assert run is not None
    run.repository_id = star.repos["stores"].id
    star.session.commit()
    bound = dataclasses.replace(principal, run_context=RunContext(task_run_id=task.id, claim=1))
    rows = pa.table(
        {"store_id": pa.array([1], pa.int64()), "store_name": pa.array([7], pa.int64())}
    )
    catalog.write_batches(
        bound,
        TableRef.from_dotted("dev", _DIM),
        mode=WriteMode.REPLACE,
        schema=rows.schema,
        batches=rows.to_batches(),
        evolve_schema=True,
    )
    return task.run_id


def _warnings(
    star: StarWorld, principal: PrincipalRef, run_id: uuid.UUID, grants: Grants
) -> tuple[str | None, tuple[ImpactWarningView, ...]]:
    """Read the retyped task's note and impact warnings as a reader with `grants`.

    Parameters
    ----------
    star
        The world.
    principal
        The reader.
    run_id
        The run.
    grants
        What the reader may do.

    Returns
    -------
    tuple
        The task's `schema_note` and `impact_warnings`.
    """
    star.session.expire_all()
    service = RunService(star.session, grants, caps=RunCaps(max_retries=5, max_timeout_seconds=600))
    (task,) = [t for t in service.get(principal, "dev", run_id).tasks if t.asset == _DIM]
    return task.schema_note, task.impact_warnings


def test_a_retyping_run_names_what_other_repositories_use_the_column_for(
    star: StarWorld, principal: PrincipalRef, tmp_path: Path
) -> None:
    run_id = _retype_the_store_name(star, principal, tmp_path)

    note, (warning,) = _warnings(star, principal, run_id, Grants(others=True))
    assert note == COLUMNS_CHANGED_NOTE
    assert (warning.table, warning.column) == (_DIM, "store_name")
    assert warning.uses == (
        ImpactUseView("asset", "gold.fact_sales", "sales"),
        ImpactUseView("term", "net_revenue", "sales"),
        ImpactUseView("measure", "gold.fact_sales.net_amount", "sales"),
    )
    assert warning.message == (
        "This run changed the type of column `store_name` of `gold.dim_store`, which other "
        "repositories use; tell their owners, and see everything it affects with: lakana graph "
        "impact --env dev gold.dim_store --column store_name — what uses it: asset "
        "`gold.fact_sales` (sales), glossary term `net_revenue` (sales), measure "
        "`gold.fact_sales.net_amount` (sales)"
    )


@pytest.mark.parametrize(
    "grants",
    [
        Grants(set(TABLES) - {"gold.fact_sales"}, others=True),
        Grants(others=True, environment=False),
    ],
    ids=["no_read_of_the_fact", "no_read_environment"],
)
def test_the_warning_is_withheld_from_a_reader_who_may_not_see_what_it_names(
    star: StarWorld, principal: PrincipalRef, tmp_path: Path, grants: Grants
) -> None:
    run_id = _retype_the_store_name(star, principal, tmp_path)

    note, warnings = _warnings(star, principal, run_id, grants)
    assert note == COLUMNS_CHANGED_NOTE  # the note is everyone's; only the uses are withheld
    assert warnings == ()


@pytest.mark.parametrize("error", [RuntimeError("storage gone"), GraphBusyError("busy")])
def test_a_run_whose_impact_cannot_be_computed_still_writes_and_says_so(
    star: StarWorld,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(GraphService, "breakage", fail)
    run_id = _retype_the_store_name(star, principal, tmp_path)

    task = star.session.scalars(select(TaskRun).where(TaskRun.run_id == run_id)).one()
    assert task.schema_change_json is not None  # the write landed with its note
    _, (warning,) = _warnings(star, principal, run_id, Grants(others=True))
    assert (warning.table, warning.column, warning.uses) == (_DIM, "store_name", ())
    assert warning.message == (
        "This run changed the type of column `store_name` of `gold.dim_store`, and Lakana could "
        "not check in time what other repositories use it; see it with: lakana graph impact "
        "--env dev gold.dim_store --column store_name"
    )


def test_a_write_outside_a_run_computes_no_impact(
    star: StarWorld, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[object] = []
    monkeypatch.setattr(GraphService, "breakage", lambda *args, **_kw: asked.append(args) or {})
    catalog = _prepared_catalog(star.session, tmp_path, AllowAllAuthorization())
    star.session.scalars(select(Environment)).one().is_protected = False
    star.session.commit()
    rows = pa.table(
        {"store_id": pa.array([1], pa.int64()), "store_name": pa.array([7], pa.int64())}
    )
    catalog.write_batches(
        principal,
        TableRef.from_dotted("dev", _DIM),
        mode=WriteMode.REPLACE,
        schema=rows.schema,
        batches=rows.to_batches(),
        evolve_schema=True,
    )
    assert asked == []
