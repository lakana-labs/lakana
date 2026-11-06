"""How a listing shows a policy: its current column names, and whether it is bound (#120)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from core_test_helpers import _config, _RecordThenDeny
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef, Securable
from lakana_core.catalog import current_fields
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import PermissionDenied
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import ColumnMask, RowFilter, Table, TableType
from lakana_core.policy.field_binding import BY_NAME, FIELD_IDS, PENDING, UNBOUND
from lakana_core.policy.shown import ShownPolicy, shown_policies
from lakana_core.storage.resolver import StorageResolver

_TABLE = uuid.UUID(int=1)


def _fields(*named: tuple[int, str]) -> tuple[NestedField, ...]:
    """Build string fields from (id, name) pairs.

    Parameters
    ----------
    *named
        The fields.

    Returns
    -------
    tuple of NestedField
        The fields.
    """
    return tuple(NestedField(i, n, StringType(), required=False) for i, n in named)


def _filter(
    expression: str = "dept = 'a'", binding: str = FIELD_IDS, ids: dict[str, Any] | None = None
) -> RowFilter:
    """Build a row filter of the table, not stored.

    Parameters
    ----------
    expression
        The expression.
    binding
        The binding state.
    ids
        The stored field ids; `{"dept": 2}` unless given.

    Returns
    -------
    RowFilter
        The row.
    """
    return RowFilter(
        id=uuid.uuid4(),
        table_id=_TABLE,
        expression=expression,
        binding=binding,
        field_ids={"dept": 2} if ids is None else ids,
    )


def _mask(binding: str = FIELD_IDS, column_field_id: int | None = 3) -> ColumnMask:
    """Build a mask on `ssn` (field 3), not stored.

    Parameters
    ----------
    binding
        The binding state.
    column_field_id
        The mask column's stored field id.

    Returns
    -------
    ColumnMask
        The row.
    """
    return ColumnMask(
        id=uuid.uuid4(),
        table_id=_TABLE,
        column="ssn",
        expression="'***'",
        binding=binding,
        field_ids={"ssn": 3},
        column_field_id=column_field_id,
    )


def _renamed(table_id: uuid.UUID) -> Sequence[NestedField]:
    """Report a table whose `dept` (2) is now `team` and `ssn` (3) is now `tax_id`.

    Parameters
    ----------
    table_id
        The table.

    Returns
    -------
    Sequence of NestedField
        The fields.
    """
    return _fields((1, "id"), (2, "team"), (3, "tax_id"))


def test_a_bound_policy_is_shown_with_the_current_names() -> None:
    row = _filter()

    assert shown_policies([row], _renamed) == {row.id: ShownPolicy("\"team\" = 'a'", None, True)}


def test_a_bound_policy_whose_columns_kept_their_names_is_shown_as_written() -> None:
    row = _filter("dept = 'a' AND id > 1", ids={"dept": 2, "id": 1})
    mask = _mask()

    found = shown_policies([row, mask], lambda _: _fields((1, "id"), (2, "dept"), (3, "ssn")))

    assert found[row.id] == ShownPolicy("dept = 'a' AND id > 1", None, True)
    assert found[mask.id] == ShownPolicy("'***'", "ssn", True)


def test_a_case_only_difference_is_shown_as_written() -> None:
    row = _filter("DEPT = 'a'", ids={"DEPT": 2})

    found = shown_policies([row], lambda _: _fields((2, "dept")))

    assert found[row.id] == ShownPolicy("DEPT = 'a'", None, True)


def test_a_bound_mask_is_shown_on_its_current_column() -> None:
    row = _mask()

    assert shown_policies([row], _renamed) == {row.id: ShownPolicy("'***'", "tax_id", True)}


def test_a_policy_whose_field_is_gone_is_shown_as_written() -> None:
    row, mask = _filter(), _mask()

    found = shown_policies([row, mask], lambda _: _fields((1, "id")))

    assert found[row.id] == ShownPolicy("dept = 'a'", None, True)
    assert found[mask.id] == ShownPolicy("'***'", "ssn", True)


def test_an_unbound_or_pending_policy_is_shown_as_written_and_not_bound() -> None:
    rows = [_filter(binding=PENDING), _filter(binding=UNBOUND), _mask(binding=PENDING)]

    found = shown_policies(rows, _renamed)

    assert [(found[r.id].expression, found[r.id].bound) for r in rows] == [
        ("dept = 'a'", False),
        ("dept = 'a'", False),
        ("'***'", False),
    ]
    assert found[rows[2].id].column == "ssn"


def test_a_raw_tables_policy_is_shown_as_written_and_bound() -> None:
    row = _filter(binding=BY_NAME, ids={})

    assert shown_policies([row], lambda _: None) == {row.id: ShownPolicy("dept = 'a'", None, True)}


def test_unreadable_metadata_shows_the_text_as_written() -> None:
    bound, pending = _filter(), _mask(binding=PENDING)

    found = shown_policies([bound, pending], lambda _: None)

    assert found[bound.id] == ShownPolicy("dept = 'a'", None, True)
    assert found[pending.id] == ShownPolicy("'***'", "ssn", False)


def test_a_bound_row_whose_stored_ids_are_unreadable_is_shown_as_written() -> None:
    row = _filter(ids={"dept": "x"})

    assert shown_policies([row], _renamed)[row.id] == ShownPolicy("dept = 'a'", None, True)


def test_the_fields_of_each_table_are_read_once() -> None:
    calls: list[uuid.UUID] = []

    def fields_of(table_id: uuid.UUID) -> Sequence[NestedField]:
        calls.append(table_id)
        return _renamed(table_id)

    shown_policies([_filter(), _filter(), _mask()], fields_of)

    assert calls == [_TABLE]


_GOLD = NamespacePath.from_parts(["gold"])


@pytest.fixture
def gold(catalog: CatalogService, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide `dev` with `gold.orders (id long, status string)`.

    Parameters
    ----------
    catalog
        The allow-all catalog.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_table(
        principal,
        TableRef("dev", _GOLD, "orders"),
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "status", StringType(), required=False),
        ),
    )
    return catalog


def _table_id(session: Session, name: str) -> uuid.UUID:
    """Find a table's id.

    Parameters
    ----------
    session
        Database session.
    name
        The table's name.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    return session.scalars(select(Table.id).where(Table.name == name)).one()


def test_the_catalog_reads_the_current_fields_of_an_iceberg_table(
    session: Session, principal: PrincipalRef, gold: CatalogService
) -> None:
    fields = gold.policy_fields(principal, _table_id(session, "orders"))

    assert fields is not None
    assert [(f.field_id, f.name) for f in fields] == [(1, "id"), (2, "status")]


def test_the_catalog_has_no_fields_for_what_is_not_an_iceberg_table_with_metadata(
    session: Session, principal: PrincipalRef, gold: CatalogService
) -> None:
    gold.create_or_replace_view(
        principal,
        TableRef("dev", _GOLD, "v"),
        sql="SELECT 1 AS a",
        references=[],
        columns=[ColumnSpec("a", "long", True)],
    )
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=session.scalars(select(Table.namespace_id)).first(),
            name="bare",
            table_type=TableType.ICEBERG,
        )
    )
    session.commit()

    assert gold.policy_fields(principal, _table_id(session, "v")) is None
    assert gold.policy_fields(principal, _table_id(session, "bare")) is None
    assert gold.policy_fields(principal, uuid.uuid4()) is None


def test_the_catalog_logs_the_class_only_when_the_metadata_cannot_be_read(
    session: Session,
    principal: PrincipalRef,
    gold: CatalogService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with mock.patch.object(current_fields, "read_metadata", side_effect=OSError("secret path")):
        assert gold.policy_fields(principal, _table_id(session, "orders")) is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "(OSError)" in warnings[0].getMessage()
    assert "secret path" not in caplog.text


def test_the_catalog_refuses_the_fields_to_a_principal_who_may_not_manage_policies(
    session: Session, principal: PrincipalRef, gold: CatalogService
) -> None:
    denier = _RecordThenDeny()
    refusing = CatalogService(session, StorageResolver(session), denier)

    with pytest.raises(PermissionDenied):
        refusing.policy_fields(principal, _table_id(session, "orders"))

    assert denier.seen == (Action.MANAGE_POLICY, Securable.instance_scope())
