"""Who counts as linked to the identity provider: the one definition (#809).

A person's link keeps the issuer it was made with (`Principal.oidc_issuer`). The server records
the issuer it is configured with (`LAKANA_OIDC__ISSUER`) at every start (`record_oidc_issuer`),
and only a link to that issuer counts: a link to another issuer, or any link while OIDC sign-in
is off, counts as no link (the sign-in design, "Settings": "Changing the issuer leaves the linked
people unlinked until they link again"). Their passkeys sign them in again, their personal tokens
have no window, and they may link to the new provider, which replaces the stale link.

Stale links are ignored, never cleared: an operator who starts the server once without the
variable, or with a mistyped issuer, and then corrects it, gets every link back as it was. A
stale link never signs anyone in: a provider identity is matched by issuer and subject together
(`lakana_core.identity.oidc_flows.principal_for`), and a stale link's issuer is not the one the
server verifies tokens of.

Every question "is this person linked" asks `is_linked`, or filters with `linked_clause` and
`unlinked_clause` in SQL: the passkey refusal, step-up's choice of ceremony, recovery links, the
profile and Access > a person, the counts of Settings > Sign-in, the break-glass rule, the token
window and the sessions a token started.

Before a server has recorded an issuer (a database no server of this release started), every
stored link counts, as it did before: the stricter reading, as a linked person has fewer ways in.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import ColumnElement, false, func, or_, select, true
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Securable
from lakana_core.grants.admin_lock import lock_admin_count
from lakana_core.models import AuditEventType, InstanceSetting, Principal

OIDC_ISSUER_KEY = "sign_in.oidc_issuer"
"""The instance setting that holds the issuer links count for, as ``{"issuer": <url or null>}``:
the server's `LAKANA_OIDC__ISSUER`, recorded at start-up (`record_oidc_issuer`); null while OIDC
sign-in is off."""


class _Unrecorded(enum.Enum):
    """The type of `UNRECORDED`."""

    UNRECORDED = "unrecorded"


UNRECORDED: Final = _Unrecorded.UNRECORDED
"""What `recorded_issuer` answers before any server recorded an issuer."""


@dataclass(frozen=True)
class IssuerChange:
    """A change of the issuer links count for, as `record_oidc_issuer` recorded it.

    Parameters
    ----------
    before
        The issuer recorded before; `None` while OIDC sign-in was off, `UNRECORDED` before any.
    after
        The issuer recorded now; `None` with OIDC sign-in off.
    not_counted
        Active people whose stored link no longer counts.
    """

    before: str | _Unrecorded | None
    after: str | None
    not_counted: int


def recorded_issuer(session: Session) -> str | _Unrecorded | None:
    """Read the issuer links count for.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    str, None or UNRECORDED
        The issuer; `None` while OIDC sign-in is off; `UNRECORDED` before any server recorded one.
    """
    row = session.get(InstanceSetting, OIDC_ISSUER_KEY)
    if row is None or not isinstance(row.value, dict):
        return UNRECORDED
    issuer = row.value.get("issuer")
    return issuer if isinstance(issuer, str) else None


def linked_clause(session: Session) -> ColumnElement[bool]:
    """Build the SQL condition of a principal who is linked (`is_linked`).

    Parameters
    ----------
    session
        Database session, to read the recorded issuer.

    Returns
    -------
    ColumnElement of bool
        A condition on `Principal`.
    """
    issuer = recorded_issuer(session)
    if issuer is UNRECORDED:
        return Principal.oidc_subject.is_not(None)
    if issuer is None:
        return false()
    return Principal.oidc_subject.is_not(None) & (Principal.oidc_issuer == issuer)


def unlinked_clause(session: Session) -> ColumnElement[bool]:
    """Build the SQL condition of a principal who is not linked: `linked_clause`'s opposite.

    Spelt out rather than negated, so a NULL column never makes both conditions unknown.

    Parameters
    ----------
    session
        Database session, to read the recorded issuer.

    Returns
    -------
    ColumnElement of bool
        A condition on `Principal`.
    """
    issuer = recorded_issuer(session)
    if issuer is UNRECORDED:
        return Principal.oidc_subject.is_(None)
    if issuer is None:
        return true()
    return or_(
        Principal.oidc_subject.is_(None),
        Principal.oidc_issuer.is_(None),
        Principal.oidc_issuer != issuer,
    )


def is_linked(session: Session, principal: Principal) -> bool:
    """Say whether a person signs in through the identity provider.

    Parameters
    ----------
    session
        Database session, to read the recorded issuer.
    principal
        The person.

    Returns
    -------
    bool
        Whether a provider identity is stored on them and its issuer is the one the server is
        configured with; `False` for every person while OIDC sign-in is off.
    """
    if principal.oidc_subject is None:
        return False
    issuer = recorded_issuer(session)
    if issuer is UNRECORDED:
        return True
    return issuer is not None and principal.oidc_issuer == issuer


def record_oidc_issuer(session: Session, issuer: str | None) -> IssuerChange | None:
    """Record the issuer links count for, at start-up, and audit a change; commits.

    Takes the administrator-count lock first (`lakana_core.grants.admin_lock`): a change of the
    issuer changes who counts as linked, so it takes turns with every break-glass decision, and
    the lock is the first this transaction takes. A change writes one `SIGN_IN_SETTINGS_CHANGED`
    event, attributed to nobody (the operator's configuration changed), with the issuers before
    and after and how many people's links stopped counting. The first record on a database
    writes one only when it stops some link from counting.

    Parameters
    ----------
    session
        Database session.
    issuer
        The configured issuer (`LAKANA_OIDC__ISSUER`); `None` with OIDC sign-in off.

    Returns
    -------
    IssuerChange or None
        The change; `None` when the issuer recorded was the same.
    """
    lock_admin_count(session)
    before = recorded_issuer(session)
    if before == issuer:
        session.commit()
        return None
    row = session.get(InstanceSetting, OIDC_ISSUER_KEY)
    if row is None:
        row = InstanceSetting(key=OIDC_ISSUER_KEY)
        session.add(row)
    row.value = {"issuer": issuer}
    row.updated_at = datetime.now(UTC)
    row.updated_by = None
    session.flush()
    not_counted = int(
        session.execute(
            select(func.count())
            .select_from(Principal)
            .where(
                Principal.oidc_subject.is_not(None),
                Principal.is_active.is_(True),
                unlinked_clause(session),
            )
        ).scalar_one()
    )
    if before is not UNRECORDED or not_counted:
        AuditService(session).record(
            None,
            AuditEventType.SIGN_IN_SETTINGS_CHANGED,
            securable=Securable.instance_scope(),
            detail={
                "source": "start-up",
                "before": {"oidc_issuer": None if before is UNRECORDED else before},
                "after": {"oidc_issuer": issuer},
                "links_not_counted": not_counted,
            },
        )
    session.commit()
    return IssuerChange(before=before, after=issuer, not_counted=not_counted)


def issuer_change_warning(
    change: IssuerChange, *, linked: int, unlinked_admins: int, rule_holds: bool
) -> str | None:
    """Word the start-up warning of an issuer change that stopped links from counting.

    Parameters
    ----------
    change
        The change (`record_oidc_issuer`).
    linked
        Active people linked now, to the new issuer.
    unlinked_admins
        Administrators who can sign in with a passkey, counted after the change.
    rule_holds
        Whether the break-glass rule holds after the change
        (`lakana_core.grants.break_glass.holds`): setting an earlier issuer back can count
        links of administrators again while none of them has a passkey left.

    Returns
    -------
    str or None
        The warning; `None` when no link stopped counting and the break-glass rule holds.
    """
    broken = not rule_holds
    if change.not_counted == 0 and not broken:
        return None
    now = (
        f"The identity provider's issuer is now {change.after}"
        if change.after is not None
        else "OIDC sign-in is off (LAKANA_OIDC__ISSUER is not set)"
    )
    text = (
        f"{now}: {change.not_counted} people linked to another issuer now count as unlinked. "
        "They sign in with a passkey and link again from their profile; someone without a "
        "passkey needs a recovery link. Their old links are kept and count again if the issuer "
        f"is set back. {linked} people are linked now, and {unlinked_admins} administrator(s) "
        "can sign in with a passkey."
    )
    if broken:
        text += (
            " No administrator who signs in with a passkey is left if the provider fails: issue "
            "one a recovery link with the server operator's bootstrap token (lakana recovery "
            "create NAME)."
        )
    return text
