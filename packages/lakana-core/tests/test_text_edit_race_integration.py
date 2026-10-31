"""Two sessions on a real PostgreSQL: edits of one table's texts run one after the other."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.translations import lock_table
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Base, Principal, PrincipalType, Table, TextTranslation
from lakana_core.storage.resolver import StorageResolver

pytestmark = pytest.mark.integration

_REF = TableRef("dev", NamespacePath.from_parts(["silver"]), "orders")
_WAIT_SECONDS = 0.5
_JOIN_SECONDS = 60.0


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Start one Postgres and create the schema.

    Yields
    ------
    Engine
        An engine on it.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        yield engine
        engine.dispose()


def test_an_edit_waits_for_the_writer_that_holds_the_table(engine: Engine, tmp_path: Path) -> None:
    """While one session holds the table, another's edit waits, then lists both languages."""
    principal = PrincipalRef(id=uuid.uuid4(), name="owner", type=PrincipalType.USER)
    with Session(engine) as setup:
        setup.add(Principal(id=principal.id, type=principal.type, name="owner", is_active=True))
        setup.commit()
        catalog = _prepared_catalog(
            setup, tmp_path, AllowAllAuthorization(), namespaces=[_REF.namespace]
        )
        catalog.create_table(principal, _REF, schema=Schema(NestedField(1, "id", LongType())))
        catalog.set_table_description(principal, _REF, label="Bestellungen", language="de")

    done = threading.Event()
    failure: list[BaseException] = []

    def second() -> None:
        """Add a French label in a session of its own."""
        try:
            with Session(engine) as other:
                CatalogService(
                    other, StorageResolver(other), AllowAllAuthorization()
                ).set_table_description(principal, _REF, label="Commandes", language="fr")
        except BaseException as exc:
            failure.append(exc)
        finally:
            done.set()

    with Session(engine) as first:
        row = first.scalars(select(Table).where(Table.name == "orders")).one()
        lock_table(first, row)
        thread = threading.Thread(target=second)
        thread.start()
        assert not done.wait(_WAIT_SECONDS), "the edit did not wait for the lock"
        CatalogService(
            first, StorageResolver(first), AllowAllAuthorization()
        ).set_table_description(principal, _REF, label=None, language="de")
    thread.join(_JOIN_SECONDS)
    assert failure == []
    with Session(engine) as check:
        row = check.scalars(select(Table).where(Table.name == "orders")).one()
        assert row.text_languages == ["fr"]
        assert [r.language for r in check.scalars(select(TextTranslation))] == ["fr"]
