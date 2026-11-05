"""Shared helpers for the lakana-core test suite.

The module name is deliberately repo-unique. pytest puts each test
directory's rootdir on ``sys.path``, so a bare top-level module name is
resolved first-wins across the whole repository: a second
``packages/*/tests/helpers.py`` would shadow this one and break collection
in whichever package sorts later. ``conftest.py`` is exempt from that —
pytest registers conftests by path and declines duplicates — which is why
the pytest *fixtures* stay there and only the plain helpers live here.
"""

import contextlib
import enum
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from minio import Minio
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import select
from sqlalchemy.orm import Session
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from lakana_core.artifacts.service import ArtifactService, ArtifactSource, CommitResult
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    Securable,
)
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog.identity import resolve_environment_id, resolve_table_id
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import CatalogService, TableDescription
from lakana_core.config import ArtifactsConfig, StorageConfig
from lakana_core.errors import AlreadyExistsError, PermissionDenied
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Namespace,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    Table,
    TableType,
    TaskRun,
    TaskStatus,
)
from lakana_core.secrets.crypto import EncryptedSecret
from lakana_core.storage.resolver import StorageResolver


def _config(tmp_path: Path) -> StorageConfig:
    """Build a local storage configuration.

    Parameters
    ----------
    tmp_path
        Temporary directory to root the environment at.

    Returns
    -------
    StorageConfig
        Local-filesystem configuration.
    """
    return StorageConfig(scheme="file", root=f"file://{tmp_path}", options={})


def _earlier_layout(catalog: CatalogService, principal: PrincipalRef, name: str, root: str) -> None:
    """Create an environment on a root that only an earlier release could give it.

    `CatalogService.create_environment` refuses a root at or inside another environment's
    ``managed/`` or unrecorded directories, but such environments may exist from before: this
    creates the environment elsewhere and then moves its row to `root`, for the tests of the
    checks that keep their files.

    Parameters
    ----------
    catalog
        The catalog service.
    principal
        The acting principal.
    name
        The environment's name.
    root
        Its storage root.
    """
    placeholder = f"file:///earlier-layout-{uuid.uuid4().hex}"
    env = catalog.create_environment(
        principal, name=name, storage=StorageConfig(scheme="file", root=placeholder, options={})
    )
    session = Session.object_session(env)
    assert session is not None
    env.storage_root = root
    session.commit()


def _minimal_schema() -> Schema:
    """Build a minimal schema for tables that exist only as fixtures.

    Used by `_prepared_catalog` to create setup tables whose schema is
    never itself under test.

    Returns
    -------
    Schema
        A schema with a single required id column.
    """
    return Schema(NestedField(1, "id", LongType(), required=True))


def ensure_table(
    session: Session, environment: str, namespace: str, table: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """Ensure the environment/namespace/table a policy row targets exist.

    Inserts bare `Environment`/`Namespace`/`Table` rows directly, bypassing
    `CatalogService`, for tests that only need *some* table for a row
    filter, column mask, or audit event to reference — `RowFilterService`,
    `ColumnMaskService`, and related code resolve these names to real ids
    and raise `NotFoundError` when they don't exist.

    Idempotent per `(environment, namespace, table)` within one session, so
    repeated calls with the same names (for example, two filters on the
    same table) reuse the same rows rather than colliding on a unique
    constraint.

    Parameters
    ----------
    session
        Database session.
    environment
        The environment's name.
    namespace
        The namespace, already unit-separator-joined.
    table
        The table's name.

    Returns
    -------
    tuple of (uuid.UUID, uuid.UUID)
        `(environment_id, table_id)`, resolved via `resolve_environment_id`/
        `resolve_table_id` — the same ids a real catalog write would have
        stored.
    """
    env = session.execute(
        select(Environment).where(Environment.name == environment)
    ).scalar_one_or_none()
    if env is None:
        env = Environment(
            id=uuid.uuid4(),
            name=environment,
            storage_root="file:///dev/null",
            storage_options={},
            properties={},
        )
        session.add(env)
        session.commit()
    ns = session.execute(
        select(Namespace).where(Namespace.environment_id == env.id, Namespace.path == namespace)
    ).scalar_one_or_none()
    if ns is None:
        ns = Namespace(id=uuid.uuid4(), environment_id=env.id, path=namespace, properties={})
        session.add(ns)
        session.commit()
    row = session.execute(
        select(Table).where(Table.namespace_id == ns.id, Table.name == table)
    ).scalar_one_or_none()
    if row is None:
        row = Table(id=uuid.uuid4(), namespace_id=ns.id, name=table, table_type=TableType.ICEBERG)
        session.add(row)
        session.commit()
    environment_id = resolve_environment_id(session, environment)
    table_id = resolve_table_id(session, environment, namespace, table)
    assert environment_id is not None  # nosec B101 -- just ensured above
    assert table_id is not None  # nosec B101 -- just ensured above
    return environment_id, table_id


def _bootstrap_principal(session: Session) -> PrincipalRef:
    """Provide a principal for setup steps that are not themselves a test, persisted.

    A plain module-level helper rather than the `principal` fixture, because
    it is called from plain functions such as `_catalog_with` and
    `_prepared_catalog`, which cannot request a pytest fixture directly.
    Delegates to `ensure_bootstrap_principal`, which is idempotent, rather
    than inserting a fresh `Principal` row on every call: `_prepared_catalog`
    calls this once per environment, namespace, and table it sets up, all
    against the same session, and `Principal.name` is unique. It must also
    actually persist a row:
    `CatalogService.create_environment` now records an `AuditEvent` in the
    same transaction, and `AuditEvent.principal_id` is a foreign key onto
    `principals.id` that SQLite enforces even though the column is nullable.

    Named ``"catalog-setup"``, not ``"admin"``: several tests in this suite
    (`test_grants_service.py`, for instance) persist their own actor under
    the name ``"admin"`` via a fresh `PrincipalRef`, and `Principal.name` is
    unique — colliding with that name here would break every test that
    calls both this helper and one of those.

    Parameters
    ----------
    session
        Database session to persist the principal into.

    Returns
    -------
    PrincipalRef
        A reference to a principal that actually exists as a row.
    """
    return ensure_bootstrap_principal(session, "catalog-setup")


def _catalog_with(session: Session, tmp_path: Path, authz: AuthorizationService) -> CatalogService:
    """Build a catalog whose environment already exists.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Directory backing the environment's storage.
    authz
        Authorization service under test.

    Returns
    -------
    CatalogService
        A catalog with a `dev` environment already created, so that the
        authorization assertions are about the namespace call and not about
        environment setup. Idempotent on `session`: a test that calls this
        more than once against the same session (for example, once per
        iteration of a loop over several operations) gets the same `dev`
        environment back rather than an `AlreadyExistsError`.
    """
    setup = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    with contextlib.suppress(AlreadyExistsError):
        setup.create_environment(
            _bootstrap_principal(session), name="dev", storage=_config(tmp_path)
        )
    return CatalogService(session, StorageResolver(session), authz)


def _prepared_catalog(
    session: Session,
    tmp_path: Path,
    authz: AuthorizationService,
    *,
    namespaces: Sequence[NamespacePath] = (),
    tables: Sequence[TableRef] = (),
) -> CatalogService:
    """Build a catalog whose environment, namespaces and tables already exist.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Directory backing the environment's storage.
    authz
        Authorization service under test.
    namespaces
        Namespace paths to create in the `dev` environment before the
        returned catalog is bound to `authz`.
    tables
        Table references to create, with a minimal schema, before the
        returned catalog is bound to `authz`. Each ref's namespace must
        already exist, whether from `namespaces` or an earlier call
        against the same session.

    Returns
    -------
    CatalogService
        A catalog with a `dev` environment, the given namespaces, and the
        given tables already created under allow-all authorization, then
        rebound to `authz`. This lets a test exercise authorization on an
        operation whose preconditions (the environment, a namespace, or a
        table it must already act on) are otherwise met, without ever
        assigning to `CatalogService`'s private `_authz` attribute.
        Idempotent on `session`: a test that calls this more than once
        against the same session (for example, once per iteration of a loop
        over several operations) gets the same `dev` environment,
        namespaces and tables back rather than an `AlreadyExistsError`.
    """
    setup = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    with contextlib.suppress(AlreadyExistsError):
        setup.create_environment(
            _bootstrap_principal(session), name="dev", storage=_config(tmp_path)
        )
    for namespace in namespaces:
        with contextlib.suppress(AlreadyExistsError):
            setup.create_namespace(_bootstrap_principal(session), "dev", namespace)
    for ref in tables:
        with contextlib.suppress(AlreadyExistsError):
            setup.create_table(_bootstrap_principal(session), ref, schema=_minimal_schema())
    return CatalogService(session, StorageResolver(session), authz)


class SeaweedFsContainer(DockerContainer):
    """A SeaweedFS container exposing an S3-compatible gateway.

    Mirrors the interface of testcontainers' S3 container class
    (`get_client`, `get_config`, `.access_key`/`.secret_key`) so integration
    tests can swap one for the other without touching call sites. Unlike
    some other S3 servers, SeaweedFS's S3 gateway refuses anonymous requests
    outright once an identity config is supplied, so a single-identity config file is
    generated per instance and copied into the container via the Docker API
    (`with_copy_into_container`, applied after the container is created but
    before its main process starts) rather than a host bind mount — the
    config never touches the host filesystem as a named path at all, which
    also sidesteps host/daemon path-translation issues a bind mount can hit
    in sandboxed or remote-daemon Docker setups.
    """

    def __init__(
        self,
        image: str = "chrislusf/seaweedfs:4.47",
        port: int = 8333,
        access_key: str = "lakanatest",
        secret_key: str = "lakanatestsecret12345",
    ) -> None:
        """Configure a SeaweedFS container; does not start it.

        Parameters
        ----------
        image
            Docker image to use.
        port
            Port the S3 gateway listens on inside the container.
        access_key
            Access key for the one identity this container is configured
            with.
        secret_key
            Secret key for that identity.
        """
        super().__init__(image, _wait_strategy=LogMessageWaitStrategy("Starting S3 API Server"))
        self.port = port
        self.access_key = access_key
        self.secret_key = secret_key

        identity_config = json.dumps(
            {
                "identities": [
                    {
                        "name": "lakanatest",
                        "credentials": [{"accessKey": access_key, "secretKey": secret_key}],
                        "actions": ["Admin", "Read", "Write"],
                    }
                ]
            }
        )
        self.with_exposed_ports(self.port)
        self.with_copy_into_container(identity_config.encode(), "/etc/seaweedfs/s3.json")
        self.with_command(
            f"server -s3 -s3.port={self.port} -s3.config=/etc/seaweedfs/s3.json "
            "-dir=/data -ip.bind=0.0.0.0"
        )

    def get_client(self) -> Minio:
        """Return a `minio`-SDK client for the running container.

        Returns
        -------
        Minio
            A client for this container's one configured identity. The
            `minio` SDK speaks plain S3, so it works against SeaweedFS's
            gateway the same way it works against any S3 server.
        """
        config = self.get_config()
        return Minio(
            endpoint=config["endpoint"],
            access_key=config["access_key"],
            secret_key=config["secret_key"],
            secure=False,
        )

    def get_config(self) -> dict[str, str]:
        """Return this container's endpoint and credentials.

        Returns
        -------
        dict[str, str]
            `endpoint`, `access_key`, `secret_key`.
        """
        host_ip = self.get_container_host_ip()
        exposed_port = self.get_exposed_port(self.port)
        return {
            "endpoint": f"{host_ip}:{exposed_port}",
            "access_key": self.access_key,
            "secret_key": self.secret_key,
        }


class _RecordThenDeny:
    """Records the first call it receives, then refuses it.

    Used to prove exactly which action and securable a single catalog
    operation authorizes, in isolation from anything a delegate call might
    also check.
    """

    def __init__(self) -> None:
        """Start with nothing recorded yet."""
        self.seen: tuple[Action, Securable] | None = None

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Record the request and refuse it.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.

        Raises
        ------
        PermissionDenied
            Always.
        """
        self.seen = (action, securable)
        raise PermissionDenied("denied")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would, as a boolean.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.

        Returns
        -------
        bool
            `False` if `authorize` raises `PermissionDenied`, else `True`.
        """
        try:
            self.authorize(principal, action, securable)
        except PermissionDenied:
            return False
        return True


class _RecordAll:
    """Records every authorization call without denying any.

    Used where an operation is expected to authorize more than once — the
    call sequence itself is the thing under test, not a refusal.
    """

    def __init__(self) -> None:
        """Start with nothing recorded yet."""
        self.seen: list[tuple[Action, Securable]] = []

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Record the call.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.
        """
        self.seen.append((action, securable))

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would, as a boolean.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.

        Returns
        -------
        bool
            `False` if `authorize` raises `PermissionDenied`, else `True`.
        """
        try:
            self.authorize(principal, action, securable)
        except PermissionDenied:
            return False
        return True


class ActionAllowList:
    """Permits exactly the listed actions, on any securable, and records every check.

    Parameters
    ----------
    *allowed
        The actions to permit.
    """

    def __init__(self, *allowed: Action) -> None:
        self.allowed = frozenset(allowed)
        self.seen: list[tuple[Action, Securable]] = []

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Record the check and refuse any action not in the allow list.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.

        Raises
        ------
        PermissionDenied
            If `action` is not allowed.
        """
        self.seen.append((action, securable))
        if action not in self.allowed:
            raise PermissionDenied(f"may not {action.value}")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would, without recording.

        Parameters
        ----------
        principal
            The actor the question is about.
        action
            The action in question.
        securable
            The object in question.

        Returns
        -------
        bool
            Whether `action` is allowed.
        """
        return action in self.allowed


PRE_M5A_ACTIONS = frozenset(action.value for action in Action) - {
    "MAINTAIN_TABLE",
    "DEPLOY",
    "MANAGE_REPOSITORY",
    "RUN",
    "MANAGE_PACKAGE_INDEX",
    "MANAGE_CONNECTION",
}
"""Every `Action` value the release before write runs (0.4.x) knows: write runs added
`MAINTAIN_TABLE`,
projects `DEPLOY` and `MANAGE_REPOSITORY`, the run engine `RUN`, package indexes
`MANAGE_PACKAGE_INDEX`, connections `MANAGE_CONNECTION`."""

_PreM5aAction = enum.Enum("_PreM5aAction", {value: value for value in sorted(PRE_M5A_ACTIONS)})


def seed_m5a_rows(connection: sa.Connection) -> uuid.UUID:
    """Insert, at revision 0012, one row of everything write runs introduced.

    A principal holding a `MAINTAIN_TABLE` grant and a older `READ_TABLE`
    grant, a managed table, a view row, a lineage edge and a write run with
    every write column set.

    Parameters
    ----------
    connection
        A connection inside a transaction, at revision 0012.

    Returns
    -------
    uuid.UUID
        The principal's id.
    """
    now = datetime.now(UTC)
    principal_id, environment_id, namespace_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    table_id, view_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stamps = {"created_at": now, "updated_at": now}

    def table(name: str, *columns: tuple[str, sa.types.TypeEngine]) -> sa.TableClause:
        """Describe just the columns this seed writes.

        Parameters
        ----------
        name
            The table.
        *columns
            ``(name, type)`` pairs.

        Returns
        -------
        sa.TableClause
            A lightweight table clause.
        """
        return sa.table(name, *(sa.column(column, kind) for column, kind in columns))

    uid, text, when, js = sa.Uuid(), sa.String(), sa.DateTime(timezone=True), sa.JSON()
    connection.execute(
        sa.insert(
            table(
                "principals",
                ("id", uid),
                ("type", text),
                ("name", text),
                ("is_active", sa.Boolean()),
                ("created_at", when),
                ("updated_at", when),
            )
        ).values(id=principal_id, type="USER", name="maintainer", is_active=True, **stamps)
    )
    connection.execute(
        sa.insert(
            table(
                "environments",
                ("id", uid),
                ("name", text),
                ("storage_root", text),
                ("storage_options", js),
                ("is_protected", sa.Boolean()),
                ("properties", js),
                ("created_at", when),
                ("updated_at", when),
            )
        ).values(
            id=environment_id,
            name="dev",
            storage_root="s3://bucket/dev",
            storage_options={},
            is_protected=False,
            properties={},
            **stamps,
        )
    )
    connection.execute(
        sa.insert(
            table(
                "namespaces",
                ("id", uid),
                ("environment_id", uid),
                ("path", text),
                ("properties", js),
            )
        ).values(id=namespace_id, environment_id=environment_id, path="gold", properties={})
    )
    tables = table(
        "tables",
        ("id", uid),
        ("namespace_id", uid),
        ("name", text),
        ("table_type", text),
        ("view_definition", js),
        ("created_at", when),
        ("updated_at", when),
    )
    connection.execute(
        sa.insert(tables).values(
            id=table_id, namespace_id=namespace_id, name="daily", table_type="ICEBERG", **stamps
        )
    )
    connection.execute(
        sa.insert(tables).values(
            id=view_id,
            namespace_id=namespace_id,
            name="recent",
            table_type="VIEW",
            view_definition={"sql": "SELECT * FROM gold.daily", "references": ["gold.daily"]},
            **stamps,
        )
    )
    grants = table(
        "grants",
        ("id", uid),
        ("principal_id", uid),
        ("action", text),
        ("kind", text),
        ("environment", text),
        ("namespace", text),
        ("table", text),
        ("environment_id", uid),
        ("table_id", uid),
        ("granted_by", uid),
        ("granted_at", when),
    )
    for action in ("MAINTAIN_TABLE", "READ_TABLE"):
        connection.execute(
            sa.insert(grants).values(
                id=uuid.uuid4(),
                principal_id=principal_id,
                action=action,
                kind="TABLE",
                environment="dev",
                namespace="gold",
                table="daily",
                environment_id=environment_id,
                table_id=table_id,
                granted_by=principal_id,
                granted_at=now,
            )
        )
    connection.execute(
        sa.insert(
            table(
                "query_runs",
                ("id", uid),
                ("environment_id", uid),
                ("principal_id", uid),
                ("sql_text", text),
                ("status", text),
                ("write_mode", text),
                ("target_name", text),
                ("target_table_id", uid),
                ("incremental_key", text),
                ("evolve_schema", sa.Boolean()),
                ("watermark", text),
                ("rows_written", sa.BigInteger()),
                ("snapshot_id", sa.BigInteger()),
                ("schema_diff", js),
                ("maintenance", js),
                ("created_at", when),
                ("updated_at", when),
            )
        ).values(
            id=run_id,
            environment_id=environment_id,
            principal_id=principal_id,
            sql_text="SELECT 1 AS id",
            status="SUCCEEDED",
            write_mode="APPEND",
            target_name="gold.daily",
            target_table_id=table_id,
            incremental_key="id",
            evolve_schema=False,
            watermark="long:1",
            rows_written=1,
            snapshot_id=1,
            schema_diff={"added": []},
            maintenance=None,
            **stamps,
        )
    )
    connection.execute(
        sa.insert(
            table(
                "lineage_edges",
                ("id", uid),
                ("environment_id", uid),
                ("source_table_id", uid),
                ("source_name", text),
                ("target_table_id", uid),
                ("target_name", text),
                ("query_run_id", uid),
                ("created_at", when),
            )
        ).values(
            id=uuid.uuid4(),
            environment_id=environment_id,
            source_table_id=view_id,
            source_name="gold.recent",
            target_table_id=table_id,
            target_name="gold.daily",
            query_run_id=run_id,
            created_at=now,
        )
    )
    return principal_id


def assert_loads_as_pre_m5a(connection: sa.Connection, principal_id: uuid.UUID) -> None:
    """Assert the older code path can load the principal's grants and the tables.

    Loads `grants.action` and `tables.table_type` through non-native enums
    holding only older values, as the 0.4.x ORM does: a leftover
    `MAINTAIN_TABLE` grant or `VIEW` row raises `LookupError` here.

    Parameters
    ----------
    connection
        A connection after `downgrade -1` from 0012.
    principal_id
        The principal `seed_m5a_rows` returned.
    """
    metadata = sa.MetaData()
    grants = sa.Table(
        "grants",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("principal_id", sa.Uuid()),
        sa.Column("action", sa.Enum(_PreM5aAction, native_enum=False), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    pre_m5a_table_type = enum.Enum(
        "pre_m5a_table_type", {"ICEBERG": "ICEBERG", "EXTERNAL": "EXTERNAL"}
    )
    tables = sa.Table(
        "tables",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("table_type", sa.Enum(pre_m5a_table_type, native_enum=False)),
    )
    actions = (
        connection.execute(sa.select(grants.c.action).where(grants.c.principal_id == principal_id))
        .scalars()
        .all()
    )
    assert [action.value for action in actions] == ["READ_TABLE"]
    raw = connection.execute(sa.text("SELECT action FROM grants")).scalars().all()
    assert set(raw) <= PRE_M5A_ACTIONS
    assert [t.value for t in connection.execute(sa.select(tables.c.table_type)).scalars()] == [
        "ICEBERG"
    ]


def python_task_run(
    session: Session,
    principal_id: uuid.UUID,
    *,
    asset: str = "gold.scores",
    inputs: dict[str, str] | None = None,
    environment: str = "dev",
    status: TaskStatus = TaskStatus.RUNNING,
    claim: int = 1,
    kind: str = "python",
    extra: dict[str, object] | None = None,
) -> TaskRun:
    """Insert a running run whose project version has one asset, and that asset's task run.

    Parameters
    ----------
    session
        Database session; commits.
    principal_id
        The run principal.
    asset
        The task's asset.
    inputs
        The asset's declared inputs; ``{"obs": "silver.weather", "stations":
        "bronze.stations"}`` when `None`.
    environment
        An existing environment's name.
    status
        The task run's status.
    claim
        Its claim counter.
    kind
        The manifest entry's kind.
    extra
        Keys merged into the manifest entry (``output``, ``keep_versions``,
        ``input_kinds``), as deployed or as a hostile repository might store them.

    Returns
    -------
    TaskRun
        The task run.
    """
    env = session.scalars(select(Environment).where(Environment.name == environment)).one()
    declared = (
        {"obs": "silver.weather", "stations": "bronze.stations"} if inputs is None else inputs
    )
    repo = Repository(
        id=uuid.uuid4(),
        name=f"r{uuid.uuid4().hex[:6]}",
        kind=RepositoryKind.HTTPS,
        url="https://h/r.git",
    )
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env.id,
        commit_sha="a" * 40,
        manifest_json={
            "version": 3,
            "assets": [
                {"table": asset, "kind": kind, "inputs": declared, "sql": "", **(extra or {})}
            ],
            "edges": [],
        },
        code_uri="file:///x",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    run = Run(
        id=uuid.uuid4(),
        environment_id=env.id,
        repository_id=repo.id,
        job_version_id=version.id,
        run_as_principal_id=principal_id,
        kind=RunKind.FULL,
        status=RunStatus.RUNNING,
        selection_json={},
    )
    session.add(run)
    session.flush()
    task = TaskRun(
        id=uuid.uuid4(),
        run_id=run.id,
        asset=asset,
        status=status,
        claim_count=claim,
        attempt=1,
        lease_owner="w1",
        max_attempts=3,
        timeout_seconds=60,
        retry_delay_seconds=0,
        upstream_json=[],
    )
    session.add(task)
    session.commit()
    return task


ARTIFACT_REF = TableRef("dev", NamespacePath.from_parts(["models"]), "churn")
"""The artifact most artifact tests use."""


def artifact_document(
    files: Mapping[str, bytes], *, is_directory: bool = True
) -> dict[str, object]:
    """Build the manifest document of a set of files.

    Parameters
    ----------
    files
        Path to contents.
    is_directory
        Whether the artifact is a directory.

    Returns
    -------
    dict
        The document, files sorted as the canonical form requires.
    """
    return {
        "format": 1,
        "is_directory": is_directory,
        "files": [
            {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in sorted(files.items(), key=lambda item: item[0].encode("utf-8"))
        ],
    }


def artifact_service(
    session: Session,
    tmp_path: Path,
    authz: AuthorizationService | None = None,
    *,
    config: ArtifactsConfig | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ArtifactService:
    """Build an artifact service on a `dev` environment that has the namespace `models`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Directory backing the environment's storage.
    authz
        Authorization under test; allow-all when `None`.
    config
        Limits and the upload lifetime; the defaults when `None`.
    clock
        The service's clock; the real one when `None`.

    Returns
    -------
    ArtifactService
        The service.
    """
    _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[ARTIFACT_REF.namespace]
    )
    return ArtifactService(
        session, StorageResolver(session), authz or AllowAllAuthorization(), config, clock=clock
    )


def store_version(
    service: ArtifactService,
    p: PrincipalRef,
    ref: TableRef,
    files: Mapping[str, bytes],
    *,
    is_directory: bool = True,
    sources: Sequence[ArtifactSource] = (),
) -> CommitResult:
    """Store one version through the three steps.

    Parameters
    ----------
    service
        The service.
    p
        The acting principal.
    ref
        The artifact.
    files
        Path to contents.
    is_directory
        Whether the artifact is a directory.
    sources
        What the version was made from.

    Returns
    -------
    CommitResult
        The committed version.
    """
    upload = service.begin_upload(p, ref, artifact_document(files, is_directory=is_directory))
    by_hash = {hashlib.sha256(data).hexdigest(): data for data in files.values()}
    for digest in upload.missing:
        service.put_blob(p, ref, upload.upload_id, digest, [by_hash[digest]])
    return service.commit_upload(p, ref, upload.upload_id, sources=sources)


COMMON_MASK_COLUMNS = ["id", "name", "email", "ssn", "region", "salary", "owner", "c"]
"""The columns `FakeColumnCatalog` reports unless a test says otherwise."""


class NoPolicyFields:
    """The `ColumnSource.policy_fields` of a fake catalog that has no table metadata."""

    def policy_fields(self, table_id: uuid.UUID) -> tuple[NestedField, ...] | None:
        """Report no fields: listings show the text as written.

        Parameters
        ----------
        table_id
            The table.

        Returns
        -------
        None
            Always.
        """
        return None


@dataclass
class FakeColumnCatalog(NoPolicyFields):
    """A `ColumnSource` that reports fixed columns, or raises, and records its calls.

    Parameters
    ----------
    names
        The columns every table has; `COMMON_MASK_COLUMNS` unless given.
    error
        Raised by `describe_table` instead, when set.
    schema_id
        The id of the current schema every table reports.
    types
        The declared type of a column by name; a column not listed is `string`.
    file_format
        When set, the tables are external (raw-zone) tables of this format (`"parquet"`,
        `"csv"`, `"json"`); `None` for Iceberg tables.
    field_ids
        The field id of each column by name; for an Iceberg table, 1, 2, ... in the order of
        `names` unless given, and none for an external table.
    calls
        Each `describe_table` call, as (principal, reference).
    """

    names: list[str] = field(default_factory=lambda: list(COMMON_MASK_COLUMNS))
    error: Exception | None = None
    schema_id: int | None = None
    types: dict[str, str] = field(default_factory=dict)
    file_format: str | None = None
    field_ids: dict[str, int] | None = None
    calls: list[tuple[PrincipalRef, TableRef]] = field(default_factory=list)

    def describe_table(self, p: PrincipalRef, ref: TableRef) -> TableDescription:
        """Describe a table as having `names` as its columns.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.

        Returns
        -------
        TableDescription
            A description with `names` as columns, `string` unless `types` says otherwise.

        Raises
        ------
        Exception
            `error`, when one was given.
        """
        self.calls.append((p, ref))
        if self.error is not None:
            raise self.error
        ids = self.field_ids
        if ids is None:
            ids = {n: i for i, n in enumerate(self.names, 1)} if self.file_format is None else {}
        return TableDescription(
            ref=ref,
            id=uuid.uuid4(),
            kind="TABLE" if self.file_format is None else "EXTERNAL",
            metadata_location=None,
            columns=[
                ColumnSpec(name=n, type=self.types.get(n, "string"), nullable=True)
                for n in self.names
            ],
            schema_id=self.schema_id,
            file_format=self.file_format,
            field_ids=ids,
        )


# A format-1 secret, produced by `encrypt_secret_values` exactly as `origin/main` had it
# (no associated data on either layer), under the master key `b"0" * 32`, for the values
# below. Generated by running that unchanged module before this change.
LEGACY_VALUES = {"access_key_id": "AKIA_LEGACY", "secret_access_key": "legacy-secret"}
LEGACY_SECRET = EncryptedSecret(
    ciphertext=bytes.fromhex(
        "7a003393fce4ce53f3ffbc81f48b3302887273e33e9ca6cbf8114b2aa3fdcb6849c428a43c6def9a"
        "d122c6c40418832eacc34b28b74b6537dafe6c4f40383243cead8a3b65b4109513f21a4066feefa5"
        "b3496a1d69ab"
    ),
    nonce=bytes.fromhex("b3c4707b1923617f39357f72"),
    wrapped_data_key=bytes.fromhex(
        "cf68357e2d99a9a8b7d0b4b4d1aa5ede55a502fd62a1063d3a1d217f2e60565db09b967ffdbbc7ec"
        "1eea31c2feda963709887f4d087131c899c206ee"
    ),
    format_version=1,
    key_id="env",
)


def deploy_manifest(
    session: Session, environment: str, repository: str, assets: Mapping[str, Sequence[str]]
) -> JobVersion:
    """Make a project version whose Python assets declare connections the current one.

    Stores a `JobVersion` whose manifest has one Python entry per asset, each with its
    `connections`, and points the environment's deployment of the repository at it. The
    repository is registered on first use.

    Parameters
    ----------
    session
        Database session; committed.
    environment
        The environment's name; it must exist.
    repository
        The repository's name.
    assets
        Asset (table) name to the connections it declares.

    Returns
    -------
    JobVersion
        The new current version.
    """
    env_id = session.execute(
        select(Environment.id).where(Environment.name == environment)
    ).scalar_one()
    repo = session.execute(
        select(Repository).where(Repository.name == repository)
    ).scalar_one_or_none()
    if repo is None:
        repo = Repository(
            id=uuid.uuid4(),
            name=repository,
            kind=RepositoryKind.HTTPS,
            url=f"https://git.example.org/{repository}.git",
        )
        session.add(repo)
        session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env_id,
        commit_sha=uuid.uuid4().hex + "00000000",
        manifest_json={
            "assets": [
                {"table": table, "kind": "python", "connections": list(names)}
                for table, names in assets.items()
            ]
        },
        code_uri="file:///code.tar.gz",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    pointer = session.get(EnvironmentDeployment, (env_id, repo.id))
    if pointer is None:
        session.add(
            EnvironmentDeployment(
                environment_id=env_id, repository_id=repo.id, current_job_version_id=version.id
            )
        )
    else:
        pointer.current_job_version_id = version.id
    session.commit()
    return version
