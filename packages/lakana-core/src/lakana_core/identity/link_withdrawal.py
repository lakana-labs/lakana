"""Withdrawing the open sign-in links bound to a person.

Apart from `lakana_core.identity.links`, so that a module it imports
(`lakana_core.identity.oidc_links`, whose unlink withdraws a person's links) can use it too.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.models import AuditEventType, SignInLink


def withdraw_open_links(
    session: Session, principal_id: uuid.UUID, *, now: datetime, reason: str
) -> int:
    """Withdraw every open link bound to a person, each with one audit event; the caller commits.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The person.
    now
        The withdrawal time.
    reason
        Why, for the audit log (``deactivated``, ``reactivated``, ``oidc_unlinked``).

    Returns
    -------
    int
        How many links this call withdrew.
    """
    withdrawn = session.execute(
        update(SignInLink)
        .where(
            SignInLink.principal_id == principal_id,
            SignInLink.used_at.is_(None),
            SignInLink.withdrawn_at.is_(None),
        )
        .values(withdrawn_at=now, reserved_name=None)
        .returning(SignInLink.id, SignInLink.kind)
        .execution_options(synchronize_session="fetch")
    ).all()
    audit = AuditService(session)
    for link_id, kind in sorted(withdrawn, key=lambda r: str(r[0])):
        audit.record(
            principal_id,
            AuditEventType.SIGN_IN_LINK_WITHDRAWN,
            detail={"kind": kind, "link_id": str(link_id), "reason": reason},
        )
    return len(withdrawn)
