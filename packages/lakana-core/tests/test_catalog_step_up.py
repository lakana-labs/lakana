"""A browser session needs a fresh passkey touch before it drops data; a token never does.

Dropping a table, a namespace, an artifact or one artifact version cannot be undone from the
browser the way an ordinary edit can, so each asks a browser session for a passkey touch no
older than 15 minutes (`lakana_core.identity.sessions.require_step_up`), as an environment
purge already does. A token caller (the CLI, an agent) has no session and is never asked.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import (
    ARTIFACT_REF,
    _minimal_schema,
    _prepared_catalog,
    artifact_service,
    store_version,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SessionContext
from lakana_core.errors import StepUpRequired
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import ArtifactVersion, AuditEvent, Namespace, Table

_SILVER = NamespacePath.from_parts(["silver"])
_SCRATCH = NamespacePath.from_parts(["scratch"])
_CLEAN = TableRef("dev", _SILVER, "clean")


@dataclass(frozen=True)
class Drop:
    """One destructive call, ready to run, and how to tell that its object is still there.

    Parameters
    ----------
    run
        Performs the call as the given principal.
    intact
        Whether the object the call would remove still exists.
    """

    run: Callable[[PrincipalRef], object]
    intact: Callable[[], bool]


def _artifact_id(session: Session) -> uuid.UUID:
    """Find the id of the artifact `ARTIFACT_REF` names.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    return session.execute(select(Table.id).where(Table.name == ARTIFACT_REF.name)).scalar_one()


def _table_drop(session: Session, tmp_path: Path, p: PrincipalRef) -> Drop:
    """Create a managed table and offer its drop.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage root.
    p
        The principal that creates the table.

    Returns
    -------
    Drop
        The drop, and a check that the row is still there.
    """
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    table_id = catalog.create_table(p, _CLEAN, schema=_minimal_schema()).uuid
    return Drop(
        run=lambda who: catalog.drop_table(who, _CLEAN),
        intact=lambda: session.get(Table, table_id) is not None,
    )


def _namespace_drop(session: Session, tmp_path: Path, p: PrincipalRef) -> Drop:
    """Create an empty namespace and offer its drop.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage root.
    p
        The principal that creates the namespace.

    Returns
    -------
    Drop
        The drop, and a check that the namespace is still there.
    """
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    namespace_id = catalog.create_namespace(p, "dev", _SCRATCH).id
    return Drop(
        run=lambda who: catalog.drop_namespace(who, "dev", _SCRATCH),
        intact=lambda: session.get(Namespace, namespace_id) is not None,
    )


def _artifact_drop(session: Session, tmp_path: Path, p: PrincipalRef) -> Drop:
    """Store one version of an artifact no project declares, and offer the artifact's drop.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage root.
    p
        The principal that stores the version.

    Returns
    -------
    Drop
        The drop, and a check that the artifact's row is still there.
    """
    service = artifact_service(session, tmp_path)
    store_version(service, p, ARTIFACT_REF, {"model.bin": b"weights"})
    artifact_id = _artifact_id(session)
    return Drop(
        run=lambda who: service.drop_artifact(who, ARTIFACT_REF),
        intact=lambda: session.get(Table, artifact_id) is not None,
    )


def _version_delete(session: Session, tmp_path: Path, p: PrincipalRef) -> Drop:
    """Store one version of an artifact and offer that version's delete.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage root.
    p
        The principal that stores the version.

    Returns
    -------
    Drop
        The delete, and a check that the version is not marked deleted.
    """
    service = artifact_service(session, tmp_path)
    store_version(service, p, ARTIFACT_REF, {"model.bin": b"weights"})
    artifact_id = _artifact_id(session)

    def intact() -> bool:
        """Tell whether version 1 is still live.

        Returns
        -------
        bool
            Whether its row exists and is not marked deleted.
        """
        session.expire_all()
        row = session.execute(
            select(ArtifactVersion).where(
                ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.version == 1
            )
        ).scalar_one_or_none()
        return row is not None and row.deleted_at is None

    return Drop(run=lambda who: service.delete_version(who, ARTIFACT_REF, 1), intact=intact)


DROPS: dict[str, Callable[[Session, Path, PrincipalRef], Drop]] = {
    "catalog.drop_table": _table_drop,
    "catalog.drop_namespace": _namespace_drop,
    "artifacts.drop_artifact": _artifact_drop,
    "artifacts.delete_version": _version_delete,
}
"""Every call that asks a browser session for a fresh touch, by name."""


def _with_touch(p: PrincipalRef, *, minutes_ago: int) -> PrincipalRef:
    """Return `p` acting through a browser session last touched `minutes_ago` minutes ago.

    Parameters
    ----------
    p
        The principal.
    minutes_ago
        How long ago the last passkey touch was.

    Returns
    -------
    PrincipalRef
        The same principal with a session.
    """
    now = datetime.now(UTC)
    return PrincipalRef(
        id=p.id,
        name=p.name,
        type=p.type,
        session=SessionContext(
            session_id=uuid.uuid4(),
            key_id="k",
            via="passkey",
            created_at=now - timedelta(hours=1),
            last_touch_at=now - timedelta(minutes=minutes_ago),
            source_pat_id=None,
            verified_at=now,
        ),
    )


def _audit_rows(session: Session) -> int:
    """Count the audit rows.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    int
        How many there are.
    """
    return session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()


@pytest.mark.parametrize("call", sorted(DROPS))
def test_a_stale_session_is_asked_for_a_touch_and_nothing_changes(
    session: Session, tmp_path: Path, principal: PrincipalRef, call: str
) -> None:
    """A touch 16 minutes old is refused before anything is removed or audited."""
    drop = DROPS[call](session, tmp_path, principal)
    before = _audit_rows(session)

    with pytest.raises(StepUpRequired):
        drop.run(_with_touch(principal, minutes_ago=16))

    session.rollback()
    assert drop.intact()
    assert _audit_rows(session) == before


@pytest.mark.parametrize("who", ["fresh touch", "token"])
@pytest.mark.parametrize("call", sorted(DROPS))
def test_a_fresh_touch_and_a_token_both_pass(
    session: Session, tmp_path: Path, principal: PrincipalRef, call: str, who: str
) -> None:
    """A touch 14 minutes old passes, and so does a caller with no session at all."""
    drop = DROPS[call](session, tmp_path, principal)
    actor = _with_touch(principal, minutes_ago=14) if who == "fresh touch" else principal
    assert actor.session is not None or who == "token"

    drop.run(actor)

    assert not drop.intact()
