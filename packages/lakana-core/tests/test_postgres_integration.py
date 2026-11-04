"""Integration tests against a real PostgreSQL instance."""

import io
import sys
import uuid
from pathlib import Path

import httpx
import pyarrow as pa
import pytest
from core_test_helpers import SeaweedFsContainer
from pyiceberg.schema import Schema
from pyiceberg.types import DoubleType, LongType, NestedField
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.identifiers import NAMESPACE_SEPARATOR, NamespacePath, TableRef
from lakana_core.models import Base, Environment, TableHealthSignal
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import StorageResolver

pytestmark = pytest.mark.integration


def test_schema_and_uniqueness_hold_on_postgres() -> None:
    """The models create cleanly on Postgres and the name constraint bites.

    The engine comes from `lakana_core.db.create_engine`, which also proves
    its SQLite pragma listener is inert against a real Postgres connection.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(
                Environment(
                    id=uuid.uuid4(),
                    name="dev",
                    storage_root="file:///tmp/x",
                    storage_options={},
                    is_protected=False,
                    properties={},
                )
            )
            session.commit()
            assert session.query(Environment).count() == 1


def test_list_namespaces_orders_hierarchically_under_a_real_glibc_collation(
    tmp_path: Path,
) -> None:
    r"""`list_namespaces` sorts by parts, not by the stored joined string.

    The Debian-based `postgres:16` image (unlike `postgres:16-alpine`, which
    is musl and collates like C, the same as SQLite's BINARY collation)
    defaults to a glibc `en_US.utf8`-style collation in which U+001F, the
    namespace separator, is collation-ignorable. Under that collation,
    ``ORDER BY path`` in SQL on ``gold``, ``golda`` and ``gold\x1fsales``
    returns ``gold``, ``golda``, ``gold\x1fsales`` — interleaving ``golda``
    between a namespace and its own child. This is exactly the divergence
    `packages/lakana-core/tests/test_namespaces.py`'s SQLite-only ordering
    test cannot exercise, since SQLite's collation already orders U+001F
    below every namespace-legal ASCII character.

    The probe below checks *behaviour*, not a label: `pg_database.datcollate`
    reads `en_US.utf8` on `postgres:16-alpine` too, because musl's `initdb`
    records the `LANG` it was given, even though musl's `strcoll` still
    collates like C — so a check of `datcollate != "C"` would pass on
    exactly the image whose behaviour would make this test vacuous. Instead,
    the probe runs the same `ORDER BY` the production code no longer uses
    over the three literal strings and asserts the database interleaves
    them, which is the collation-ignorable behaviour this test depends on
    to demonstrate the bug it exists to catch.
    """
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            probe = (
                session.execute(
                    text("SELECT val FROM (VALUES (:a), (:b), (:c)) AS t(val) ORDER BY val"),
                    {"a": "gold", "b": f"gold{NAMESPACE_SEPARATOR}sales", "c": "golda"},
                )
                .scalars()
                .all()
            )
            assert probe == ["gold", "golda", f"gold{NAMESPACE_SEPARATOR}sales"], (
                f"Container's ORDER BY returned {probe!r}, not the interleaved "
                "order this test depends on to demonstrate the bug — its "
                "collation cannot discriminate the two orderings, so this "
                "test would be vacuous here."
            )

            catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
            # Persisted, not a bare `PrincipalRef`:
            # `create_environment` records an `AuditEvent` whose
            # `principal_id` is a foreign key onto `principals.id`, and
            # Postgres enforces it unconditionally (unlike SQLite, only with
            # the pragma this engine happens to also set).
            principal = ensure_bootstrap_principal(session, "admin")
            catalog.create_environment(
                principal,
                name="dev",
                storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
            )
            for parts in (["golda"], ["gold", "sales"]):
                catalog.create_namespace(principal, "dev", NamespacePath.from_parts(parts))

            listed = catalog.list_namespaces(principal, "dev")

    assert [p.parts for p in listed] == [
        ("bronze",),
        ("gold",),
        ("gold", "sales"),
        ("golda",),
        ("silver",),
    ]


def test_list_tables_orders_by_name_on_postgres(tmp_path: Path) -> None:
    """`list_tables`'s `ORDER BY Table.name` is load-bearing on a real database.

    On SQLite, `sqlite_autoindex_tables_2` is a covering index on
    `(namespace_id, name)`, so rows already come back name-sorted whether or
    not `list_tables` asks for it — dropping the `ORDER BY` there leaves every
    test green. Postgres carries the same `(namespace_id, name)` unique index
    (`Table.__table_args__`), and for a namespace this small its planner also
    satisfies `WHERE namespace_id = ...` with a scan of that index, which
    would return name-sorted rows even without `ORDER BY` — so the test below
    forces a sequential scan (see the comment at the call site) to make the
    assertion depend on the `ORDER BY` clause rather than on which scan the
    planner happens to pick. Tables are inserted in an order that disagrees
    with name order (``charlie``, ``alpha``, ``bravo``) so a plan that merely
    preserves insertion order also fails the assertion.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
            # Persisted, not a bare `PrincipalRef` —
            # see the comment in the test above.
            principal = ensure_bootstrap_principal(session, "admin")
            catalog.create_environment(
                principal,
                name="dev",
                storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
            )
            ns = NamespacePath.from_parts(["gold"])
            schema = Schema(NestedField(1, "id", LongType(), required=True))
            for name in ("charlie", "alpha", "bravo"):
                catalog.create_table(
                    principal, TableRef(environment="dev", namespace=ns, name=name), schema=schema
                )

            # Force the sequential scan the docstring above explains.
            session.execute(text("SET LOCAL enable_indexscan = off"))
            session.execute(text("SET LOCAL enable_bitmapscan = off"))
            listed = catalog.list_tables(principal, "dev", ns)

    assert [ref.name for ref in listed] == ["alpha", "bravo", "charlie"]


def test_a_table_with_extreme_bounds_keeps_taking_appends_on_postgres(tmp_path: Path) -> None:
    """Postgres `json` refuses Infinity: a range band that would overflow must not reach it (#340).

    Eight appends holding `-DBL_MAX` give the history whose band overflows; the ninth append
    must land, as must every one before it.

    Parameters
    ----------
    tmp_path
        The storage root.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
            principal = ensure_bootstrap_principal(session, "admin")
            catalog.create_environment(
                principal,
                name="dev",
                storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
            )
            ref = TableRef(
                environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="prices"
            )
            catalog.create_table(
                principal, ref, schema=Schema(NestedField(1, "price", DoubleType()))
            )
            rows = pa.table({"price": pa.array([-sys.float_info.max, 5.0])})
            for _ in range(9):
                catalog.append(principal, ref, rows)
            states = session.scalars(
                select(TableHealthSignal.state).where(TableHealthSignal.signal == "range")
            ).all()

    assert len(states) == 9
    assert "no_metrics" in states


def test_resolve_read_url_against_real_seaweedfs() -> None:
    """A presigned URL from a real SeaweedFS container is independently fetchable.

    This is the first test to exercise `FsspecStorage` against a real S3
    backend at all (Storage.list and Storage.resolve_read_url were never
    called against one before this task) — it is also the test that catches
    a regression in `_translate_s3_options` silently building an anonymous,
    endpoint-less connection instead of the configured one.
    """
    with SeaweedFsContainer() as seaweedfs:
        client = seaweedfs.get_client()
        client.make_bucket("lakana")
        client.put_object("lakana", "dev/raw/x.parquet", io.BytesIO(b"hello"), length=5)
        endpoint = f"http://{seaweedfs.get_config()['endpoint']}"
        storage = FsspecStorage(
            "s3://lakana/dev",
            {
                "s3.endpoint": endpoint,
                "s3.access-key-id": seaweedfs.access_key,
                "s3.secret-access-key": seaweedfs.secret_key,
            },
        )

        url = storage.resolve_read_url("raw/x.parquet", ttl_seconds=60)
        response = httpx.get(url)

        assert response.status_code == 200
        assert response.content == b"hello"
        # boto3/s3fs sign this URL with the legacy SigV2 query params
        # (AWSAccessKeyId/Signature/Expires) rather than SigV4's
        # X-Amz-Signature, since no region is configured here. "Signature="
        # matches both schemes, so this assertion holds regardless of which
        # one a given boto3/region configuration produces.
        assert "Signature=" in url
        assert httpx.get(url.split("?")[0]).status_code == 403


def test_a_read_prefix_against_real_seaweedfs_is_the_start_of_every_file_url() -> None:
    """Path style against a real store: the prefix is what the query process may open.

    The prefix is cut from a signed URL of a sentinel name; every presigned URL of a file
    below the directory starts with it, and nothing of a signature is in it.
    """
    with SeaweedFsContainer() as seaweedfs:
        client = seaweedfs.get_client()
        client.make_bucket("lakana")
        client.put_object("lakana", "dev/raw/in/x.parquet", io.BytesIO(b"hello"), length=5)
        endpoint = f"http://{seaweedfs.get_config()['endpoint']}"
        storage = FsspecStorage(
            "s3://lakana/dev",
            {
                "s3.endpoint": endpoint,
                "s3.access-key-id": seaweedfs.access_key,
                "s3.secret-access-key": seaweedfs.secret_key,
            },
        )

        prefix = storage.read_url_prefix("raw/in")
        url = storage.resolve_read_url("raw/in/x.parquet", ttl_seconds=60)

        assert prefix == f"{endpoint}/lakana/dev/raw/in/"
        assert url.startswith(prefix)
        assert httpx.get(url).content == b"hello"


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING"])
def test_a_cancel_behind_a_locked_run_row_is_a_conflict_within_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """A cancel behind the run row's lock gives up instead of hanging.

    A second connection holds ``SELECT ... FOR UPDATE`` on the run: a RUNNING one whose worker is
    alive (inside its commit), or a QUEUED one (dequeued, frozen before its start). The cancel runs
    in a thread so that a regression fails the join instead of hanging the test. After the
    refusal the cancel's session still executes statements: an aborted PostgreSQL transaction
    would refuse them, so this pins the rollback.
    """
    import threading

    from sqlalchemy import select

    from lakana_core.catalog import query_service
    from lakana_core.catalog.query_service import QueryService
    from lakana_core.db import db_now
    from lakana_core.errors import ConflictError
    from lakana_core.models import QueryRun, QueryRunStatus, Worker

    monkeypatch.setattr(query_service, "CANCEL_LOCK_TIMEOUT_SECONDS", 0.3)
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            principal = ensure_bootstrap_principal(session, "admin")
            CatalogService(
                session, StorageResolver(session), AllowAllAuthorization()
            ).create_environment(
                principal,
                name="dev",
                storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
            )
            run = QueryService(session, AllowAllAuthorization()).submit_query(
                principal, "dev", "SELECT 1"
            )
            if status == "RUNNING":
                now = db_now(session)
                session.add(Worker(id="w", host="h", version="1", started_at=now, heartbeat_at=now))
                run.status = QueryRunStatus.RUNNING
                run.worker_id = "w"
            session.commit()
            run_id = run.id

            outcome: list[BaseException | str] = []

            def cancel() -> None:
                with Session(engine) as other:
                    try:
                        QueryService(other, AllowAllAuthorization()).cancel_query(
                            principal, "dev", run_id
                        )
                        outcome.append("returned")
                    except ConflictError as error:
                        outcome.append(error)
                        outcome.append(str(other.execute(select(1)).scalar_one()))
                    except BaseException as error:
                        outcome.append(error)

            with Session(engine) as holder:
                holder.execute(select(QueryRun).where(QueryRun.id == run_id).with_for_update())
                thread = threading.Thread(target=cancel)
                thread.start()
                thread.join(timeout=15)
                hung = thread.is_alive()
                holder.rollback()
            thread.join(timeout=15)

    assert not hung, "the cancel waited for the row lock instead of giving up"
    error, after = outcome
    assert isinstance(error, ConflictError)
    assert f"lakana query status {run_id} --env dev" in str(error)
    assert after == "1", "the session refused a statement after the conflict"


def test_a_pooled_connection_the_server_ended_is_replaced_without_an_error() -> None:
    """An ended backend leaves a dead pooled connection; the next checkout must not fail.

    A terminated backend closes the socket cleanly, so pg8000 reports a driver error here;
    the raw `ConnectionResetError` of a cut connection is covered by the unit tests.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        try:
            victim = engine.connect()
            pid = victim.execute(text("SELECT pg_backend_pid()")).scalar_one()
            victim.rollback()
            with engine.connect() as admin:  # a second backend, while the first is checked out
                admin.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
                admin.commit()
            victim.close()  # back into the pool, dead
            with engine.connect() as connection:
                assert connection.execute(text("SELECT 1")).scalar_one() == 1
                assert connection.execute(text("SELECT pg_backend_pid()")).scalar_one() != pid
        finally:
            engine.dispose()


class _ResettingSocket:
    """A driver socket whose reads fail the way a connection the peer reset does."""

    def __init__(self, inner: object) -> None:
        """Wrap a socket file.

        Parameters
        ----------
        inner
            The driver's socket file, which keeps writing normally.
        """
        self._inner = inner

    def read(self, size: int) -> bytes:
        """Fail like a reset connection.

        Parameters
        ----------
        size
            Ignored.

        Raises
        ------
        ConnectionResetError
            Always: pg8000 reads the first bytes of an answer outside its error handling.
        """
        raise ConnectionResetError("Connection reset by peer")

    def __getattr__(self, name: str) -> object:
        """Pass everything else on to the wrapped socket file.

        Parameters
        ----------
        name
            The attribute.

        Returns
        -------
        object
            The wrapped socket file's attribute.
        """
        return getattr(self._inner, name)


def test_a_pooled_connection_that_the_driver_reports_reset_is_replaced_without_an_error() -> None:
    """The real driver's raw `ConnectionResetError` on the first read must not reach a request.

    Without the engine's classification the next checkout raises it from the pre-ping.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        try:
            with engine.connect() as connection:
                pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
                raw = connection.connection.dbapi_connection
                assert raw is not None
            raw._sock = _ResettingSocket(raw._sock)
            with engine.connect() as connection:
                assert connection.execute(text("SELECT 1")).scalar_one() == 1
                assert connection.execute(text("SELECT pg_backend_pid()")).scalar_one() != pid
        finally:
            engine.dispose()
