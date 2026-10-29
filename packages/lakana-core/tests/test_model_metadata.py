"""The model a deploy records on a table row, and its references as rows."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import DateType, LongType, NestedField
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_projects_dag import make_asset

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.descriptions import sync_project_descriptions
from lakana_core.catalog.model_metadata import (
    TableModel,
    clear_removed_models,
    incoming_references,
    read_table_model,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    DeclaredReference,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Repository,
    RepositoryKind,
    Table,
    TableType,
)
from lakana_core.projects.asset_model import KEY_NONE, ResolvedReference
from lakana_core.projects.dag import build_graph
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.manifest import build_manifest
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
