"""Shared pieces of the tests of linking people to the identity provider (#32, PR 2)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from lakana_core.identity.oidc_flows import ProviderIdentity
from lakana_core.models import Passkey, Principal

ISSUER = "https://idp.example.com/realms/lakana"
"""The provider's issuer."""

RP_ID = "localhost"
"""The relying-party id the passkeys are stored for."""


def identity(subject: str = "sub-1") -> ProviderIdentity:
    """Build a verified provider identity.

    Parameters
    ----------
    subject
        The ``sub``.

    Returns
    -------
    ProviderIdentity
        With the test issuer, an e-mail address and a name.
    """
    return ProviderIdentity(
        issuer=ISSUER, subject=subject, email=f"{subject}@example.com", name=subject, auth_time=None
    )


def store_passkey(
    session: Session, row: Principal, now: datetime, *, disabled: bool = False
) -> Passkey:
    """Store a passkey for a person and commit.

    Parameters
    ----------
    session
        Database session.
    row
        The person.
    now
        Its creation time.
    disabled
        Whether it was disabled as a copy.

    Returns
    -------
    Passkey
        The row.
    """
    key = Passkey(
        id=uuid.uuid4(),
        principal_id=row.id,
        credential_id=uuid.uuid4().hex,
        public_key=b"k",
        sign_count=0,
        rp_id=RP_ID,
        name="Passkey",
        backup_eligible=False,
        created_at=now,
        created_user_agent="pytest",
        disabled_at=now if disabled else None,
    )
    session.add(key)
    session.commit()
    return key
