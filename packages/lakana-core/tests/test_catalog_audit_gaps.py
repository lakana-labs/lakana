"""Namespace creates and drops, external-table creates and upload withdrawals are audited.

Each event is written in the transaction of its change, with the actor, the securable and a
fixed set of names, ids and counts; a refused change writes none.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import (
    ARTIFACT_REF,
    _prepared_catalog,
    artifact_document,
    artifact_service,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.errors import AlreadyExistsError
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import AuditEvent, AuditEventType, Principal, PrincipalType

_SCRATCH = NamespacePath.from_parts(["scratch"])
_RAW = NamespacePath.from_parts(["raw"])


def _events(session: Session, event_type: AuditEventType) -> list[AuditEvent]:
    """Return the audit events of one type, oldest first.

    Parameters
    ----------
    session
        Database session.
    event_type
        The type.

    Returns
    -------
    list of AuditEvent
        The rows.
    """
    return list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type == event_type)
            .order_by(AuditEvent.occurred_at)
        ).scalars()
    )


def _securable(event: AuditEvent) -> tuple[object, ...]:
    """Return an event's securable columns.

    Parameters
    ----------
    event
        The row.

    Returns
    -------
    tuple
        Kind, environment, namespace and table.
    """
    return (
        event.securable_kind,
        event.securable_environment,
        event.securable_namespace,
        event.securable_table,
    )


def test_namespace_create_and_drop_are_audited(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Each records the dotted namespace; the drop also counts the grants it revoked."""
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    nested = NamespacePath.from_parts(["scratch", "events"])
    catalog.create_namespace(principal, "dev", _SCRATCH)
    catalog.create_namespace(principal, "dev", nested)
    session.add(Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="alice", is_active=True))
    session.commit()
    GrantService(session, AllowAllAuthorization()).grant(
        principal,
        principal="alice",
        role=Role.VIEWER,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace=nested.joined(),
    )

    catalog.drop_namespace(principal, "dev", nested)

    created = _events(session, AuditEventType.NAMESPACE_CREATED)
    assert [(e.principal_id, e.detail) for e in created] == [
        (principal.id, {"namespace": "scratch"}),
        (principal.id, {"namespace": "scratch.events"}),
    ]
    assert _securable(created[1]) == (SecurableKind.NAMESPACE, "dev", nested.joined(), None)
    (dropped,) = _events(session, AuditEventType.NAMESPACE_DROPPED)
    assert dropped.principal_id == principal.id
    assert dropped.detail == {"namespace": "scratch.events", "revoked_grants": 1}
    assert _securable(dropped) == (SecurableKind.NAMESPACE, "dev", nested.joined(), None)


def test_an_external_table_create_is_audited(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The event names the table's id, its kind, its location pattern and its format."""
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_RAW])
    sample = tmp_path / "raw" / "events" / "a.parquet"
    sample.parent.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), sample)
    ref = TableRef("dev", _RAW, "events")

    row = catalog.create_external_table(
        principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
    )

    (event,) = _events(session, AuditEventType.TABLE_CREATED)
    assert event.principal_id == principal.id
    assert event.detail == {
        "table_id": str(row.id),
        "kind": "EXTERNAL",
        "location": "raw/events/*.parquet",
        "format": "parquet",
    }
    assert _securable(event) == (SecurableKind.TABLE, "dev", _RAW.joined(), "events")


def test_withdrawing_an_upload_is_audited(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The event names the artifact and the upload withdrawn."""
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(
        principal, ARTIFACT_REF, artifact_document({"model.bin": b"weights"})
    )

    service.abort_upload(principal, ARTIFACT_REF, upload.upload_id)

    (event,) = _events(session, AuditEventType.ARTIFACT_UPLOAD_ABORTED)
    assert event.principal_id == principal.id
    assert event.detail == {"artifact": "models.churn", "upload_id": str(upload.upload_id)}
    assert _securable(event) == (
        SecurableKind.TABLE,
        "dev",
        ARTIFACT_REF.namespace.joined(),
        "churn",
    )


def test_a_refused_create_writes_no_event(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A namespace that already exists is refused, and only the first create is audited."""
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    catalog.create_namespace(principal, "dev", _SCRATCH)

    with pytest.raises(AlreadyExistsError):
        catalog.create_namespace(principal, "dev", _SCRATCH)

    session.rollback()
    assert len(_events(session, AuditEventType.NAMESPACE_CREATED)) == 1


def test_a_create_that_loses_the_insert_race_writes_no_event(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate the check above the insert misses fails at the database, and its event goes too.

    The lookup is made to miss, as it does for the loser of a race between two servers, so the
    insert reaches the unique constraint: the event must be rolled back with the row, which
    holds only while it is written in the same transaction as the insert.
    """
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    catalog.create_namespace(principal, "dev", _SCRATCH)
    monkeypatch.setattr(catalog, "_lookup_namespace", lambda env, path: None)

    with pytest.raises(AlreadyExistsError):
        catalog.create_namespace(principal, "dev", _SCRATCH)

    session.rollback()
    assert len(_events(session, AuditEventType.NAMESPACE_CREATED)) == 1
