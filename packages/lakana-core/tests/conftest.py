"""Shared test fixtures for lakana-core.

Plain helper functions and classes live in `core_test_helpers.py`, not here
— see the module docstring there for why.
"""

import datetime
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from core_test_helpers import Clock, _prepared_catalog, half_jitter
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from scripts.test_support.ports import release_closed_ports
from sqlalchemy.orm import Session
from test_projects_dag import make_asset
from test_projects_deploys import _deployed, _repo

import lakana_core.runs.tasks as tasks_module
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Base, Principal, PrincipalType
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService
from lakana_core.runs.store import scheduler_tick
from lakana_core.storage.resolver import StorageResolver

if TYPE_CHECKING:
    from graph_test_helpers import StarWorld


@pytest.fixture
def session() -> Iterator[Session]:
    """Provide a session against a fresh in-memory SQLite database.

    The engine comes from `lakana_core.db.create_engine`, the same factory the
    server uses, so foreign key enforcement here is the enforcement that
    ships rather than a fixture-only approximation of it.

    Yields
    ------
    Session
        A session whose schema has been created from the models.
    """
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Provide the acting principal, persisted as a `Principal` row.

    Persisted, not just a bare `PrincipalRef`:
    `CatalogService.create_environment`/`delete_environment`/
    `update_environment_storage`/`set_environment_protection` now each
    record an `AuditEvent` in the same transaction, and
    `AuditEvent.principal_id` is a foreign key onto `principals.id`. The
    column is nullable, but SQLite (with `PRAGMA foreign_keys=ON`, which
    `lakana_core.db.create_engine` sets) still enforces the constraint for
    any non-`NULL` value, so a principal that exists only as a
    `PrincipalRef` would fail every one of those calls with a foreign-key
    `IntegrityError` the moment it tried to audit itself.

    Parameters
    ----------
    session
        Database session to persist the row into.

    Returns
    -------
    PrincipalRef
        A reference to a principal that actually exists as a row.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.commit()
    return ref


@pytest.fixture
def catalog(session: Session) -> CatalogService:
    """Provide a catalog service wired to allow-all authorization.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    CatalogService
        The service under test.
    """
    return CatalogService(session, StorageResolver(session), AllowAllAuthorization())


@pytest.fixture(scope="session")
def test_ca_pem() -> str:
    """Provide one self-signed CA certificate in PEM, valid for one day.

    Returns
    -------
    str
        The certificate, ``-----BEGIN CERTIFICATE-----`` included.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Lakana test CA")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


@pytest.fixture
def star(session: Session, tmp_path: Path, principal: PrincipalRef) -> "StarWorld":
    """Provide the star schema of `graph_test_helpers`.

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
    from graph_test_helpers import build_star

    return build_star(session, tmp_path, principal)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Pin `db_now` as seen by the task functions.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.

    Returns
    -------
    Clock
        The clock; set `.now` to move time.
    """
    pinned = Clock()
    monkeypatch.setattr(tasks_module, "db_now", lambda session: pinned.now)
    return pinned


@pytest.fixture
def run_id(session: Session, tmp_path: Path, principal: PrincipalRef, clock: Clock) -> uuid.UUID:
    """Start a run whose two independent tasks are ready.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The admin.
    clock
        The pinned database clock.

    Returns
    -------
    uuid.UUID
        The run's id.
    """
    bronze = NamespacePath.from_parts(["bronze"])
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[bronze],
        tables=[TableRef("dev", bronze, "events")],
    )
    _deployed(
        session,
        principal,
        _repo(session),
        "a" * 40,
        make_asset("silver.a", ("bronze.events",)),
        make_asset("silver.b", ("bronze.events",)),
    )
    run = RunService(session, AllowAllAuthorization()).start(
        principal, "dev", "analytics", Selection()
    )
    scheduler_tick(session, clock.now, half_jitter)
    return run.id


@pytest.fixture(autouse=True)
def _release_closed_ports() -> Iterator[None]:
    """Free the ports `closed_port` reserved for the test.

    Yields
    ------
    None
        Control to the test.
    """
    yield
    release_closed_ports()
