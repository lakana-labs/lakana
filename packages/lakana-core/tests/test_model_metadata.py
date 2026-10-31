"""The model a deploy records on a table row, and its references as rows."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import DateType, LongType, NestedField
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from test_projects_dag import make_asset

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.descriptions import sync_project_descriptions
from lakana_core.catalog.model_metadata import (
    TableModel,
    clear_removed_models,
    incoming_references,
    measure_id,
    model_view,
    read_table_model,
    write_model_entry,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.texts import TextChooser
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    DeclaredMeasure,
    DeclaredReference,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Namespace,
    Repository,
    RepositoryKind,
    Table,
    TableType,
)
from lakana_core.projects.asset_model import KEY_NONE, ResolvedReference
from lakana_core.projects.dag import build_graph
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.manifest import build_manifest
from lakana_core.projects.measures import MeasureEntry
from lakana_core.projects.project_config import ResolvedProjectConfig

_GOLD = NamespacePath.from_parts(["gold"])
_FACT = TableRef("dev", _GOLD, "fact_sales")
_SCHEMA = Schema(
    NestedField(1, "date", DateType(), required=False),
    NestedField(2, "store_id", LongType(), required=False),
)
_REF = ResolvedReference("store_id", "gold.dim_store", "store_id")
_GRAIN = "One row per store and day."


def _manifest(key: tuple[str, ...] | str = ("date", "store_id")) -> dict:
    """Build a version 9 manifest with the fact and its one reference.

    Parameters
    ----------
    key
        The fact's key columns, or ``"none"``.

    Returns
    -------
    dict
        The manifest.
    """
    asset = make_asset(
        "gold.fact_sales",
        metadata=AssetMetadata(
            materialize="table",
            kind="fact",
            grain=_GRAIN,
            key=key,
            references={"store_id": "gold.dim_store"},
        ),
    )
    return build_manifest(
        [asset],
        build_graph([asset]),
        ResolvedProjectConfig(),
        references={"gold.fact_sales": (_REF,)},
    )


def _env_id(session: Session) -> uuid.UUID:
    """Find the id of `dev`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    return session.scalars(select(Environment.id).where(Environment.name == "dev")).one()


@pytest.fixture
def catalog(session: Session, tmp_path: Path) -> CatalogService:
    """Provide an allow-all catalog with `dev` and the namespace `gold`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_GOLD])


_EXPECTED = TableModel("fact", _GRAIN, ("date", "store_id"), False, (_REF,))


def test_a_deploy_writes_the_model_and_the_references(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    catalog.create_table(principal, _FACT, schema=_SCHEMA)
    row = session.scalars(select(Table)).one()
    sync_project_descriptions(session, _env_id(session), _manifest())
    session.flush()
    assert read_table_model(session, row) == _EXPECTED
    assert incoming_references(session, _env_id(session), "gold.dim_store") == [
        (row.id, "store_id", "store_id")
    ]


def test_a_redeploy_replaces_and_a_removed_asset_clears(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    catalog.create_table(principal, _FACT, schema=_SCHEMA)
    row = session.scalars(select(Table)).one()
    env_id = _env_id(session)
    first = _manifest()
    sync_project_descriptions(session, env_id, first)
    session.flush()
    reference_id = session.scalars(select(DeclaredReference.id)).one()
    second = _manifest(key=("store_id",))
    sync_project_descriptions(session, env_id, second)
    session.flush()
    model = read_table_model(session, row)
    assert model is not None and model.key == ("store_id",)
    assert session.scalars(select(DeclaredReference.id)).one() == reference_id
    clear_removed_models(session, env_id, second, {"version": 9, "assets": [], "checks": []})
    session.flush()
    assert read_table_model(session, row) is None
    assert incoming_references(session, env_id, "gold.dim_store") == []


def test_a_table_created_by_its_first_run_gets_its_model(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`sync_created_table` writes the model of the current deployment on the pending row."""
    env_id = _env_id(session)
    repo = Repository(id=uuid.uuid4(), name="r", kind=RepositoryKind.HTTPS, url="https://h/r")
    session.add(repo)
    session.commit()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env_id,
        commit_sha="a" * 40,
        manifest_json=_manifest(),
        code_uri="file:///x",
        code_sha256="0" * 64,
        deployed_by=principal.id,
    )
    session.add(version)
    session.commit()
    session.add(
        EnvironmentDeployment(
            environment_id=env_id, repository_id=repo.id, current_job_version_id=version.id
        )
    )
    session.commit()

    catalog.create_table(principal, _FACT, schema=_SCHEMA)

    row = session.scalars(select(Table)).one()
    assert read_table_model(session, row) == _EXPECTED
    assert incoming_references(session, env_id, "gold.dim_store") == [
        (row.id, "store_id", "store_id")
    ]


def test_an_artifact_row_gets_no_model(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A deploy that names an artifact leaves its row alone, as it does for descriptions."""
    catalog.create_table(principal, _FACT, schema=_SCHEMA)
    row = session.scalars(select(Table)).one()
    row.table_type = TableType.ARTIFACT
    session.commit()
    sync_project_descriptions(session, _env_id(session), _manifest())
    session.flush()
    assert read_table_model(session, row) is None
    assert session.scalars(select(DeclaredReference)).all() == []


def test_key_none_is_stored_as_the_string_none(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`key: none` reads back as no key columns with `key_none` set."""
    catalog.create_table(principal, _FACT, schema=_SCHEMA)
    row = session.scalars(select(Table)).one()
    sync_project_descriptions(session, _env_id(session), _manifest(key=KEY_NONE))
    session.flush()
    assert row.model_key == KEY_NONE
    assert read_table_model(session, row) == TableModel("fact", _GRAIN, (), True, (_REF,))


def test_a_reference_the_header_drops_loses_its_row_and_a_new_one_gets_one(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A changed reference column is a removal plus an addition; a changed target updates."""
    catalog.create_table(principal, _FACT, schema=_SCHEMA)
    row = session.scalars(select(Table)).one()
    env_id = _env_id(session)
    sync_project_descriptions(session, env_id, _manifest())
    session.flush()
    old_id = session.scalars(select(DeclaredReference.id)).one()
    manifest = _manifest()
    entry = manifest["assets"][0]
    entry["model"]["references"] = [
        {"column": "date", "target": "gold.dim_date", "target_column": "date"},
    ]
    sync_project_descriptions(session, env_id, manifest)
    session.flush()
    model = read_table_model(session, row)
    assert model is not None
    assert model.references == (ResolvedReference("date", "gold.dim_date", "date"),)
    assert session.scalars(select(DeclaredReference.id)).one() != old_id
    assert incoming_references(session, env_id, "gold.dim_store") == []
    entry["model"]["references"] = [
        {"column": "date", "target": "gold.dim_day", "target_column": "day"},
    ]
    sync_project_descriptions(session, env_id, manifest)
    session.flush()
    assert incoming_references(session, env_id, "gold.dim_day") == [(row.id, "date", "day")]


def test_an_asset_without_a_model_clears_what_an_earlier_deploy_wrote(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """An entry whose header no longer declares a model leaves no model on the row."""
    catalog.create_table(principal, _FACT, schema=_SCHEMA)
    row = session.scalars(select(Table)).one()
    env_id = _env_id(session)
    sync_project_descriptions(session, env_id, _manifest())
    session.flush()
    plain = make_asset("gold.fact_sales", metadata=AssetMetadata(materialize="table"))
    sync_project_descriptions(
        session, env_id, build_manifest([plain], build_graph([plain]), ResolvedProjectConfig())
    )
    session.flush()
    assert (row.model_kind, row.model_grain, row.model_key) == (None, None, None)
    assert read_table_model(session, row) is None
    assert session.scalars(select(DeclaredReference)).all() == []


def test_clearing_keeps_a_table_the_project_still_produces_and_skips_a_missing_one(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Only a table that left the manifest and has a row in the environment is cleared."""
    catalog.create_table(principal, _FACT, schema=_SCHEMA)
    row = session.scalars(select(Table)).one()
    env_id = _env_id(session)
    current = _manifest()
    sync_project_descriptions(session, env_id, current)
    session.flush()
    assets = [make_asset("gold.never_built"), make_asset("gold.fact_sales")]
    previous = build_manifest(assets, build_graph(assets), ResolvedProjectConfig())
    clear_removed_models(session, env_id, previous, current)
    clear_removed_models(session, env_id, None, current)
    clear_removed_models(session, env_id, {"assets": [{"table": "not a name"}]}, None)
    session.flush()
    assert read_table_model(session, row) == _EXPECTED


def test_clearing_leaves_a_table_of_the_same_name_in_another_environment(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The environment scopes the clear: another environment's model stays."""
    catalog.create_table(principal, _FACT, schema=_SCHEMA)
    row = session.scalars(select(Table)).one()
    env_id = _env_id(session)
    sync_project_descriptions(session, env_id, _manifest())
    session.flush()
    clear_removed_models(session, uuid.uuid4(), _manifest(), None)
    session.flush()
    assert read_table_model(session, row) == _EXPECTED
    assert incoming_references(session, uuid.uuid4(), "gold.dim_store") == []


def _pending_fact(session: Session, catalog: CatalogService) -> Table:
    """Add a new, unflushed `gold.fact_sales` row, as a run's creation of the table does.

    Parameters
    ----------
    session
        Database session.
    catalog
        The catalog, which created `gold`.

    Returns
    -------
    Table
        The pending row.
    """
    namespace = session.scalars(select(Namespace).where(Namespace.path == "gold")).one()
    row = Table(
        id=uuid.uuid4(), namespace_id=namespace.id, name="fact_sales", table_type=TableType.ICEBERG
    )
    session.add(row)
    return row


def _statements_while(session: Session, write: Callable[[], object]) -> list[str]:
    """Run a write and list the statements it issued.

    Parameters
    ----------
    session
        Database session.
    write
        A callable without arguments.

    Returns
    -------
    list of str
        The statements.
    """
    seen: list[str] = []

    def record(conn: object, cursor: object, statement: str, *rest: object) -> None:
        seen.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        with session.no_autoflush:
            write()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return seen


def test_a_new_table_without_a_model_costs_no_statement(
    session: Session, catalog: CatalogService
) -> None:
    """A run creating a table that declares no model reads nothing for it: the row is new."""
    row = _pending_fact(session, catalog)
    assert _statements_while(session, lambda: write_model_entry(session, row, {})) == []
    session.commit()
    assert read_table_model(session, row) is None


def test_a_new_table_with_a_model_gets_it_on_creation(
    session: Session, catalog: CatalogService
) -> None:
    """The model and its reference rows are written although nothing old was read."""
    row = _pending_fact(session, catalog)
    entry = _manifest()["assets"][0]
    seen = _statements_while(session, lambda: write_model_entry(session, row, entry))
    assert not any("table_references" in statement for statement in seen)
    session.commit()
    assert read_table_model(session, row) == _EXPECTED
    assert incoming_references(session, _env_id(session), "gold.dim_store") == [
        (row.id, "store_id", "store_id")
    ]


def test_a_source_dropped_while_references_are_read_is_counted_not_an_error(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incoming rows and source names are two reads; a source gone between them is hidden."""
    from lakana_core.catalog import model_metadata

    catalog.create_table(principal, _FACT, schema=_SCHEMA)
    row = session.scalars(select(Table)).one()
    gone = uuid.uuid4()
    monkeypatch.setattr(
        model_metadata, "incoming_references", lambda *a: [(gone, "store_id", "store_id")]
    )
    found = model_metadata.references_view(session, row, _FACT, lambda _: True)
    assert (found.incoming, found.hidden_incoming) == ((), 1)


@pytest.fixture
def fact_row(session: Session, catalog: CatalogService, principal: PrincipalRef) -> Table:
    """Create `gold.fact_sales` in `dev` and return its row.

    Parameters
    ----------
    session
        Database session.
    catalog
        The allow-all catalog with `gold`.
    principal
        Who creates it.

    Returns
    -------
    Table
        The row.
    """
    catalog.create_table(principal, _FACT, schema=_SCHEMA)
    return session.scalars(select(Table)).one()


_AMOUNT = {
    "column": "amount",
    "aggregation": "sum",
    "additivity": "additive",
    "not_over": [],
    "unit": None,
    "currency": "EUR",
    "label": "Amount",
    "description": None,
}
_STOCK = {
    "column": "stock",
    "aggregation": "sum",
    "additivity": "semi",
    "not_over": ["date"],
    "unit": "count",
    "currency": None,
    "label": None,
    "description": None,
}


def _entry(*measures: dict) -> dict:
    """Build a manifest entry of a fact with the given measures.

    Parameters
    ----------
    *measures
        Stored measure mappings.

    Returns
    -------
    dict
        The entry.
    """
    return {
        "table": "gold.fact_sales",
        "key": ["date", "store_id"],
        "model": {
            "kind": "fact",
            "grain": _GRAIN,
            "key_none": False,
            "references": [],
            "checks": {},
            "measures": list(measures),
        },
    }


def test_write_model_entry_keeps_updates_and_deletes_measure_rows(
    session: Session, fact_row: Table
) -> None:
    write_model_entry(session, fact_row, _entry(_AMOUNT, _STOCK))
    session.commit()
    kept_id = session.scalars(
        select(DeclaredMeasure.id).where(DeclaredMeasure.column == "amount")
    ).one()
    write_model_entry(session, fact_row, _entry({**_AMOUNT, "label": "Net amount"}))
    session.commit()
    rows = session.scalars(select(DeclaredMeasure)).all()
    assert [(r.id, r.column, r.label) for r in rows] == [(kept_id, "amount", "Net amount")]
    model = read_table_model(session, fact_row)
    assert model is not None
    assert model.measures == (
        MeasureEntry("amount", "sum", "additive", (), None, "EUR", "Net amount", None),
    )
    write_model_entry(session, fact_row, {"table": "gold.fact_sales"})
    session.commit()
    assert session.scalars(select(DeclaredMeasure)).all() == []


def test_a_damaged_measure_entry_is_not_written(session: Session, fact_row: Table) -> None:
    write_model_entry(session, fact_row, _entry({**_AMOUNT, "currency": "E" * 50}))
    session.commit()
    assert session.scalars(select(DeclaredMeasure)).all() == []


def test_clear_removed_models_deletes_the_measures(session: Session, fact_row: Table) -> None:
    write_model_entry(session, fact_row, _entry(_AMOUNT))
    session.commit()
    clear_removed_models(session, _env_id(session), {"assets": [_entry(_AMOUNT)]}, {"assets": []})
    session.commit()
    assert session.scalars(select(DeclaredMeasure)).all() == []


def test_a_new_table_with_measures_gets_them_on_creation(
    session: Session, catalog: CatalogService
) -> None:
    """A run creating a fact writes its measure rows without reading any; the namespace once."""
    row = _pending_fact(session, catalog)
    entry = _entry(_AMOUNT, _STOCK)
    entry["model"]["references"] = [
        {"column": "store_id", "target": "gold.dim_store", "target_column": "store_id"}
    ]
    seen = _statements_while(session, lambda: write_model_entry(session, row, entry))
    assert [s for s in seen if "table_measures" in s or "table_references" in s] == []
    assert len([s for s in seen if "FROM namespaces" in s]) <= 1
    session.commit()
    model = read_table_model(session, row)
    assert model is not None
    assert [m.column for m in model.measures] == ["amount", "stock"]
    assert model.measures[1].not_over == ("date",)
    assert model.references == (_REF,)


def test_model_view_shows_every_measure_with_its_id(session: Session, fact_row: Table) -> None:
    write_model_entry(session, fact_row, _entry(_STOCK, _AMOUNT))
    session.commit()
    view = model_view(
        session, fact_row, _FACT, lambda target: False, lambda *_: False, TextChooser("en", [], [])
    )
    assert view is not None
    assert [(m.id, m.additivity, m.not_over) for m in view.measures] == [
        ("gold.fact_sales/measure_amount", "additive", ()),
        ("gold.fact_sales/measure_stock", "semi", ("date",)),
    ]
    assert measure_id("gold.fact_sales", "amount") == "gold.fact_sales/measure_amount"


def _dimension(*measures: dict) -> dict:
    """Build the entry of `_entry`, declared a dimension.

    Parameters
    ----------
    *measures
        Stored measure mappings; a deploy refuses any on a dimension.

    Returns
    -------
    dict
        The entry.
    """
    entry = _entry(*measures)
    entry["model"]["kind"] = "dimension"
    return entry


def _measure_reads(session: Session, call: Callable[[], object]) -> list[str]:
    """List the statements on `table_measures` that a call issues.

    Parameters
    ----------
    session
        Database session.
    call
        A callable without arguments.

    Returns
    -------
    list of str
        The statements that name `table_measures`.
    """
    return [s for s in _statements_while(session, call) if "table_measures" in s]


@pytest.mark.parametrize("entry", [{"table": "gold.fact_sales"}, _dimension()])
def test_a_stored_table_that_cannot_carry_measures_never_reads_them(
    session: Session, fact_row: Table, catalog: CatalogService, principal: PrincipalRef, entry: dict
) -> None:
    """Only a fact or a wide table has measure rows; describe, deploy, clear and drop know it."""
    write_model_entry(session, fact_row, entry)
    session.commit()
    assert _measure_reads(session, lambda: write_model_entry(session, fact_row, entry)) == []
    session.commit()
    assert _measure_reads(session, lambda: read_table_model(session, fact_row)) == []
    assert (
        _measure_reads(
            session,
            lambda: clear_removed_models(
                session, _env_id(session), {"assets": [entry]}, {"assets": []}
            ),
        )
        == []
    )
    session.rollback()
    assert _measure_reads(session, lambda: catalog.drop_table(principal, _FACT)) == []


def test_a_fact_that_becomes_a_dimension_loses_its_measures(
    session: Session, fact_row: Table
) -> None:
    write_model_entry(session, fact_row, _entry(_AMOUNT))
    session.commit()
    write_model_entry(session, fact_row, _dimension())
    session.commit()
    assert session.scalars(select(DeclaredMeasure)).all() == []
    model = read_table_model(session, fact_row)
    assert model is not None and (model.kind, model.measures) == ("dimension", ())


def test_a_fact_still_reads_and_writes_its_measures(session: Session, fact_row: Table) -> None:
    write_model_entry(session, fact_row, _entry(_AMOUNT))
    session.commit()
    assert _measure_reads(session, lambda: write_model_entry(session, fact_row, _entry(_STOCK)))
    session.commit()
    seen: list[TableModel | None] = []
    assert _measure_reads(session, lambda: seen.append(read_table_model(session, fact_row)))
    assert seen[0] is not None and [m.column for m in seen[0].measures] == ["stock"]


@pytest.mark.parametrize(
    "entry",
    [_dimension(_AMOUNT), {"table": "gold.fact_sales", "model": {"measures": [_AMOUNT]}}],
)
def test_measures_on_a_kind_that_cannot_carry_them_are_not_written(
    session: Session, fact_row: Table, entry: dict
) -> None:
    """A damaged entry writes no row, so writing it again cannot collide (no 500)."""
    for _ in range(2):
        write_model_entry(session, fact_row, entry)
        session.commit()
    assert session.scalars(select(DeclaredMeasure)).all() == []


def test_a_fact_rewritten_as_a_dimension_with_measures_loses_them(
    session: Session, fact_row: Table
) -> None:
    write_model_entry(session, fact_row, _entry(_AMOUNT))
    session.commit()
    for _ in range(2):
        write_model_entry(session, fact_row, _dimension(_AMOUNT))
        session.commit()
    assert session.scalars(select(DeclaredMeasure)).all() == []
