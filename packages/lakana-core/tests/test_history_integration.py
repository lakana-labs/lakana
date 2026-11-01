"""History writes on real infrastructure: Postgres for the catalog, SeaweedFS for the files."""

from __future__ import annotations

import dataclasses
import decimal
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from unittest import mock

import pyarrow as pa
import pytest
from core_test_helpers import SeaweedFsContainer
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog import service as service_module
from lakana_core.catalog.history import HistoryResult, read_last_ends
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import AuditEvent, AuditEventType, Base
from lakana_core.storage.resolver import StorageResolver

pytestmark = pytest.mark.integration

T1 = datetime(2026, 2, 1, tzinfo=UTC)
T2 = datetime(2026, 3, 1, tzinfo=UTC)
T3 = datetime(2026, 4, 1, tzinfo=UTC)
BETWEEN = datetime(2026, 2, 15, tzinfo=UTC)
REF = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="dim_customer")


@dataclasses.dataclass
class Stack:
    """A catalog on Postgres with the environment `dev` on SeaweedFS.

    Parameters
    ----------
    engine
        The Postgres engine.
    admin
        The bootstrap principal.
    """

    engine: Engine
    admin: PrincipalRef

    def catalog(self, session: Session) -> CatalogService:
        """Build an allow-all catalog on a session.

        Parameters
        ----------
        session
            The session.

        Returns
        -------
        CatalogService
            The catalog.
        """
        return CatalogService(session, StorageResolver(session), AllowAllAuthorization())


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    """Start Postgres and SeaweedFS once and create `dev` (with bronze, silver and gold).

    Yields
    ------
    Stack
        The engine and the principal.
    """
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        seaweedfs.get_client().make_bucket("lakana")
        with Session(engine) as session:
            admin = ensure_bootstrap_principal(session, "admin")
            catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
            catalog.create_environment(
                admin,
                name="dev",
                storage=StorageConfig(
                    scheme="s3",
                    root="s3://lakana/dev",
                    options={
                        "s3.endpoint": f"http://{seaweedfs.get_config()['endpoint']}",
                        "s3.access-key-id": seaweedfs.access_key,
                        "s3.secret-access-key": seaweedfs.secret_key,
                    },
                ),
            )
        yield Stack(engine=engine, admin=admin)
        engine.dispose()


def _batch(rows: list[tuple[str, str]]) -> pa.Table:
    """Build an asset's result.

    Parameters
    ----------
    rows
        ``(customer_id, region)`` tuples.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table({"customer_id": [r[0] for r in rows], "region": [r[1] for r in rows]})


def _write(
    stack: Stack, rows: list[tuple[str, str]], clock: datetime, **options: object
) -> HistoryResult:
    """Apply one history write in a session of its own.

    Parameters
    ----------
    stack
        The stack.
    rows
        The batch.
    clock
        The run clock.
    **options
        More keywords for `CatalogService.history`.

    Returns
    -------
    HistoryResult
        What it committed.
    """
    with (
        Session(stack.engine) as session,
        mock.patch.object(service_module, "run_clock", return_value=clock),
    ):
        return stack.catalog(session).history(
            stack.admin,
            REF,
            _batch(rows),
            key=["customer_id"],
            **options,  # ty: ignore[invalid-argument-type]
        )


def test_a_dimension_keeps_its_versions_on_postgres_and_seaweedfs(stack: Stack) -> None:
    created = _write(stack, [("A", "north"), ("B", "north")], T1, expected_snapshot_id="none")
    changed = _write(stack, [("A", "south")], T2, close_missing=True)
    same = _write(stack, [("A", "south")], T3, close_missing=True)
    back = _write(stack, [("A", "south"), ("B", "east")], T3, close_missing=True)

    assert created.created
    assert (changed.versions_opened, changed.versions_closed, changed.closed_missing) == (1, 2, 1)
    assert same.snapshot_id is None
    assert (back.versions_opened, back.versions_closed) == (1, 0)
    with Session(stack.engine) as session:
        catalog = stack.catalog(session)
        loaded = catalog.load_table(stack.admin, REF)
        snapshots = [s.snapshot_id for s in loaded.metadata.snapshots]
        assert snapshots[-2:] == [changed.snapshot_id, back.snapshot_id]
        modes = session.scalars(
            select(AuditEvent.detail).where(AuditEvent.event_type == AuditEventType.TABLE_WRITE)
        ).all()
        assert [d["mode"] for d in modes] == ["create", "history", "history"]


def test_a_writer_that_loses_the_race_on_postgres_conflicts_and_leaves_the_winner(
    stack: Stack,
) -> None:
    ref = dataclasses.replace(REF, name="dim_race")
    with (
        Session(stack.engine) as session,
        mock.patch.object(service_module, "run_clock", return_value=T1),
    ):
        stack.catalog(session).history(
            stack.admin,
            ref,
            _batch([("A", "north")]),
            key=["customer_id"],
            expected_snapshot_id="none",
        )
    real = service_module.stage_history
    calls = {"n": 0}

    def racing(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        staged = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        if calls["n"] == 1:
            with Session(stack.engine) as other:
                stack.catalog(other).history(
                    stack.admin, ref, _batch([("A", "east")]), key=["customer_id"]
                )
        return staged

    with (
        Session(stack.engine) as session,
        mock.patch.object(service_module, "run_clock", return_value=T2),
        mock.patch.object(service_module, "stage_history", side_effect=racing),
        pytest.raises(ConflictError, match="changed during the write"),
    ):
        stack.catalog(session).history(
            stack.admin, ref, _batch([("A", "south")]), key=["customer_id"]
        )

    with Session(stack.engine) as session:
        metadata = stack.catalog(session).load_table(stack.admin, ref).metadata
        assert len(metadata.snapshots) == 2  # the create and the winner; the loser left none


@dataclasses.dataclass(frozen=True)
class KeyCase:
    """A natural key of one or more non-string columns, with two distinct key values.

    Parameters
    ----------
    name
        The case's name, also the table's suffix.
    columns
        The key columns' names and Arrow types.
    first
        The values of the key that is closed and comes back, one per key column.
    second
        The values of the key that stays open, one per key column.
    """

    name: str
    columns: tuple[tuple[str, pa.DataType], ...]
    first: tuple[object, ...]
    second: tuple[object, ...]

    def batch(self, keys: Sequence[tuple[object, ...]], starts: Sequence[datetime]) -> pa.Table:
        """Build a result: the key columns, `region` and the `changed_at` start of each row.

        Parameters
        ----------
        keys
            The key values of each row.
        starts
            The `changed_at` of each row.

        Returns
        -------
        pa.Table
            The rows, `region` always `north`.
        """
        columns = {
            name: pa.array([k[i] for k in keys], kind)
            for i, (name, kind) in enumerate(self.columns)
        }
        return pa.table(
            {
                **columns,
                "region": pa.array(["north"] * len(keys), pa.string()),
                "changed_at": pa.array(starts, pa.timestamp("us", tz="UTC")),
            }
        )


KEY_CASES = [
    KeyCase(
        "decimal",
        (("k", pa.decimal128(10, 2)),),
        (decimal.Decimal("12.50"),),
        (decimal.Decimal("-0.01"),),
    ),
    KeyCase("date", (("k", pa.date32()),), (date(2025, 12, 31),), (date(2026, 1, 1),)),
    KeyCase(
        "timestamp",
        (("k", pa.timestamp("us", tz="UTC")),),
        (datetime(2025, 6, 1, 12, 0, 0, 123456, tzinfo=UTC),),
        (datetime(2025, 6, 1, 12, 0, 0, 123457, tzinfo=UTC),),
    ),
    KeyCase("binary", (("k", pa.binary()),), (b"\x00\xffA",), (b"\x00\xffB",)),
    KeyCase("int", (("k", pa.int64()),), (-(2**40),), (7,)),
    KeyCase(
        "composite",
        (("id", pa.int32()), ("country", pa.string())),
        (42, "DE"),
        (42, "FR"),
    ),
]


@pytest.mark.parametrize("case", KEY_CASES, ids=[c.name for c in KEY_CASES])
def test_a_key_of_any_type_comes_back_through_the_real_scan(stack: Stack, case: KeyCase) -> None:
    ref = dataclasses.replace(REF, name=f"dim_{case.name}")
    key = [name for name, _ in case.columns]

    def run(
        keys: list[tuple[object, ...]], starts: list[datetime], clock: datetime, **options: object
    ) -> HistoryResult:
        with (
            Session(stack.engine) as session,
            mock.patch.object(service_module, "run_clock", return_value=clock),
        ):
            return stack.catalog(session).history(
                stack.admin,
                ref,
                case.batch(keys, starts),
                key=key,
                valid_from="changed_at",
                close_missing=True,
                **options,  # ty: ignore[invalid-argument-type]
            )

    run([case.first, case.second], [T1, T1], T1, expected_snapshot_id="none")
    run([case.second], [T1], T2)  # the first key is closed at the run clock, T2

    with Session(stack.engine) as session:
        loaded = stack.catalog(session).load_table(stack.admin, ref)
        file_io = StorageResolver(session).for_environment("dev").file_io
        wanted = case.batch([case.first], [T1]).select(key)
        late = read_last_ends(
            loaded.metadata, file_io, wanted, T1, key=key, table=ref.dotted(), max_rows=None
        )
        at_end = read_last_ends(
            loaded.metadata, file_io, wanted, T2, key=key, table=ref.dotted(), max_rows=None
        )
    assert late.to_pylist() == [{**dict(zip(key, case.first, strict=True)), "valid_to": T2}]
    assert at_end.num_rows == 0  # `valid_to > after`: an end equal to the start is not read

    with pytest.raises(ValidationError, match="before the end of the key's last version"):
        run([case.first, case.second], [BETWEEN, T1], T3)
    back = run([case.first, case.second], [T2, T1], T3)

    assert (back.versions_opened, back.versions_closed, back.closed_missing) == (1, 0, 0)
    with Session(stack.engine) as session:
        metadata = stack.catalog(session).load_table(stack.admin, ref).metadata
        assert len(metadata.snapshots) == 3  # the create, the close and the return


def test_a_uuid_key_is_refused_on_postgres_and_seaweedfs(stack: Stack) -> None:
    ref = dataclasses.replace(REF, name="dim_uuid")
    ids = pa.array([uuid.UUID(int=1).bytes], pa.binary(16))
    data = pa.table(
        {"id": pa.ExtensionArray.from_storage(pa.uuid(), ids), "region": pa.array(["north"])}
    )

    with Session(stack.engine) as session, pytest.raises(ValidationError) as info:
        stack.catalog(session).history(
            stack.admin, ref, data, key=["id"], expected_snapshot_id="none"
        )

    assert str(info.value) == (
        "key column `id` is a uuid, which a history key cannot be; cast it in the SELECT, for "
        "example `CAST(id AS VARCHAR)`."
    )
    with Session(stack.engine) as session:
        assert not stack.catalog(session).table_exists(stack.admin, ref)
