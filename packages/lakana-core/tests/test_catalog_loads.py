"""The bronze rule of `lakana load` and the load route's pre-check (`check_can_load`)."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from core_test_helpers import ActionAllowList
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.loads import require_bronze
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import WriteMode
from lakana_core.storage.resolver import StorageResolver

_IDS = pa.schema([pa.field("id", pa.int64())])


def _ref(dotted: str) -> TableRef:
    """Name a table of `dev`.

    Parameters
    ----------
    dotted
        The dotted name.

    Returns
    -------
    TableRef
        The table.
    """
    return TableRef.from_dotted("dev", dotted)


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide `dev` with `bronze.orders` holding one row.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        An allow-all catalog.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    rows = pa.table({"id": pa.array([1], pa.int64())})
    catalog.write_batches(
        principal,
        _ref("bronze.orders"),
        mode=WriteMode.CREATE,
        schema=rows.schema,
        batches=rows.to_batches(),
    )
    return catalog


@pytest.mark.parametrize("dotted", ["bronze.x", "bronze.raw.x", "bronze.a.b.x"])
def test_require_bronze_accepts_the_bronze_layer_at_any_depth(dotted: str) -> None:
    """A first namespace segment `bronze` is the whole rule."""
    require_bronze(_ref(dotted))


@pytest.mark.parametrize(
    ("dotted", "target"),
    [("silver.x", "silver.x"), ("x_bronze.y", "x_bronze.y"), ("gold.bronze.z", "gold.bronze.z")],
)
def test_require_bronze_refuses_every_other_layer_with_the_fixed_text(
    dotted: str, target: str
) -> None:
    """The text names the bronze table to load into and the asset that makes the target."""
    name = target.rsplit(".", 1)[1]
    with pytest.raises(ValidationError) as caught:
        require_bronze(_ref(dotted))
    assert str(caught.value) == (
        f"`lakana load` loads into bronze only: load into `bronze.{name}` and transform it into "
        f"`{target}` with an asset."
    )


def test_a_first_load_would_create_the_table(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`none` on a missing bronze table: the load creates it."""
    created = catalog.check_can_load(
        principal, _ref("bronze.customers"), write="replace", expected_snapshot_id="none"
    )
    assert created is True


def test_a_load_outside_bronze_is_refused_before_any_right_is_asked(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The bronze rule comes first: even a caller without rights gets its text."""
    denying = CatalogService(session, StorageResolver(session), ActionAllowList())
    with pytest.raises(ValidationError, match="loads into bronze only"):
        denying.check_can_load(
            principal, _ref("silver.orders"), write="replace", expected_snapshot_id="none"
        )


def test_a_load_needs_create_table_for_a_missing_table(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`WRITE_TABLE` alone does not create a table."""
    writer = CatalogService(session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE))
    with pytest.raises(PermissionDenied):
        writer.check_can_load(
            principal, _ref("bronze.customers"), write="append", expected_snapshot_id="none"
        )


def test_a_missing_namespace_names_the_command_that_creates_it(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A deeper bronze namespace that does not exist is a 404 with the next step."""
    with pytest.raises(NotFoundError) as caught:
        catalog.check_can_load(
            principal, _ref("bronze.raw.customers"), write="replace", expected_snapshot_id="none"
        )
    assert str(caught.value) == (
        "Namespace `bronze.raw` does not exist in 'dev': create it with "
        "`lakana ns create bronze.raw --env dev`, then load again."
    )


def test_a_missing_table_without_a_pin_names_none_as_the_next_step(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Without `none`, a first load is a 404 that says how to create the table."""
    for write in ("replace", "append"):
        with pytest.raises(NotFoundError) as caught:
            catalog.check_can_load(
                principal, _ref("bronze.customers"), write=write, expected_snapshot_id=None
            )
        assert str(caught.value) == (
            "Table `bronze.customers` does not exist in 'dev': a first load passes "
            "expected_snapshot_id=none, which creates it."
        )


def test_a_missing_namespace_without_a_pin_names_the_namespace_command(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The namespace is the first thing missing, so its command is the next step."""
    with pytest.raises(NotFoundError, match=r"`lakana ns create bronze.raw --env dev`"):
        catalog.check_can_load(
            principal, _ref("bronze.raw.customers"), write="replace", expected_snapshot_id=None
        )


def test_a_missing_bronze_namespace_names_lakana_namespace_create_bronze(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An environment whose `bronze` was dropped gets the command that makes it again."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    catalog.drop_namespace(principal, "dev", NamespacePath.from_parts(["bronze"]))
    with pytest.raises(NotFoundError, match=r"`lakana ns create bronze --env dev`"):
        catalog.check_can_load(
            principal, _ref("bronze.customers"), write="replace", expected_snapshot_id="none"
        )


def test_an_existing_table_with_none_is_a_conflict(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A first load that finds the table already there does not overwrite it."""
    with pytest.raises(ConflictError):
        catalog.check_can_load(
            principal, _ref("bronze.orders"), write="replace", expected_snapshot_id="none"
        )


def test_a_fitting_schema_passes_for_append_and_replace(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The same schema fits both writes; nothing is created."""
    ref = _ref("bronze.orders")
    snapshot = catalog.load_table(principal, ref).metadata.current_snapshot_id
    assert snapshot is not None
    for write in ("append", "replace"):
        created = catalog.check_can_load(
            principal, ref, write=write, expected_snapshot_id=snapshot, schema=_IDS
        )
        assert created is False


def test_an_append_with_another_schema_is_refused_before_the_body(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A column the table does not have is refused by the pre-check, naming it."""
    ref = _ref("bronze.orders")
    snapshot = catalog.load_table(principal, ref).metadata.current_snapshot_id
    other = pa.schema([pa.field("id", pa.int64()), pa.field("extra", pa.string())])
    with pytest.raises(ValidationError, match="extra"):
        catalog.check_can_load(
            principal, ref, write="append", expected_snapshot_id=snapshot, schema=other
        )


def test_a_replace_with_an_incompatible_schema_is_refused_before_the_body(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A replace does not evolve the schema on a load: the difference is shown."""
    ref = _ref("bronze.orders")
    snapshot = catalog.load_table(principal, ref).metadata.current_snapshot_id
    other = pa.schema([pa.field("id", pa.string())])
    with pytest.raises(ValidationError):
        catalog.check_can_load(
            principal, ref, write="replace", expected_snapshot_id=snapshot, schema=other
        )


def test_the_check_writes_nothing(catalog: CatalogService, principal: PrincipalRef) -> None:
    """A dry run leaves the table's snapshot as it was and creates no table."""
    ref = _ref("bronze.orders")
    before = catalog.load_table(principal, ref).metadata.current_snapshot_id
    catalog.check_can_load(principal, ref, write="append", expected_snapshot_id=before, schema=_IDS)
    catalog.check_can_load(
        principal, _ref("bronze.new"), write="replace", expected_snapshot_id="none", schema=_IDS
    )
    assert catalog.load_table(principal, ref).metadata.current_snapshot_id == before
    assert not catalog.table_exists(principal, _ref("bronze.new"))


def test_a_stale_pin_is_a_conflict_before_the_body(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A snapshot that is no longer current is refused as the write would refuse it."""
    ref = _ref("bronze.orders")
    snapshot = catalog.load_table(principal, ref).metadata.current_snapshot_id
    assert snapshot is not None
    with pytest.raises(ConflictError):
        catalog.check_can_load(principal, ref, write="append", expected_snapshot_id=snapshot + 1)


def test_an_append_pinned_to_a_snapshot_of_a_missing_table_is_not_found(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A pin names a table that is there; the 404 is the append's own, not the `none` hint."""
    with pytest.raises(NotFoundError) as caught:
        catalog.check_can_load(
            principal, _ref("bronze.customers"), write="append", expected_snapshot_id=41
        )
    assert "expected_snapshot_id=none" not in str(caught.value)
