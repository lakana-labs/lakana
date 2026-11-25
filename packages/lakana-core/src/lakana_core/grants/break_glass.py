"""The break-glass rule: administrators who can sign in without the identity provider (#32).

An *unlinked administrator* is an active principal who holds ADMIN on the instance (directly or
through a group), is not linked to the identity provider, and has at least one passkey that is
not disabled and is registered for the current relying party (`current_rp_id`): a passkey of an
earlier public address signs nobody in.

The rule is an invariant (`holds`): whenever at least one active administrator is linked, at
least one unlinked administrator remains, so that an outage at the provider, or a provider that
refuses everyone, cannot lock the instance out (the sign-in design, "Break-glass").

Every change that can break it is refused when it would (`breaks`), under the
administrator-count lock (`lakana_core.grants.admin_lock.lock_admin_count`), taken after the
access lock in the order that lock documents. That is both sides:

- changes that lower the unlinked side: deactivating, revoking ADMIN, removing a member from a
  group that holds it (all through `lakana_core.grants.service.assert_admin_headroom`), deleting a
  passkey (`lakana_core.identity.passkeys.PasskeyService.remove`), linking an administrator
  (`lakana_core.identity.oidc_links.OidcLinkService.link`);
- changes that raise the linked side: granting instance ADMIN to a linked person or to a group
  with a linked member, and adding a linked person to a group that holds it
  (`lakana_core.grants.service.assert_break_glass_on_raise`).

An access apply makes many such changes in one transaction; it checks the rule once, on the
state it leaves (`lakana_core.access.service.AccessService._recheck_lockout`), so the order of
its changes does not matter. Only the bootstrap principal is ever reactivated, and it is never
linked. A passkey disabled as a copy and the server operator's recovery are not refused: the
first is a security response, the second is the break-glass path itself.

A change is refused only when the rule held before it: on an instance where it is already broken
(a copy was disabled), refusing every unrelated change would help nobody, and the changes that
mend it (a passkey, an unlink) raise the unlinked side.

The functions take the administrator ids from their caller, so this module needs nothing of
`lakana_core.grants.service` and both may use it.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from lakana_core.identity.provider_issuer import linked_clause, unlinked_clause
from lakana_core.models import InstanceSetting, Passkey, Principal

RP_ID_KEY = "sign_in.rp_id"
"""The instance setting that holds the relying-party id passkeys sign in with: the host of the
public address, recorded by the server at start-up (`record_rp_id`)."""

LAST_UNLINKED_ADMIN_MESSAGE = (
    "This would leave only administrators who sign in through the identity provider, so an "
    "outage there would lock everyone out. Keep at least one administrator who is not linked "
    "and has a passkey at this address: add a passkey for one, or unlink one, first."
)
"""The 409 detail of a change that would break the break-glass rule."""


def record_rp_id(session: Session, rp_id: str) -> str | None:
    """Record the relying-party id passkeys sign in with; the caller commits.

    Every count afterwards reads it afresh (`unlinked_admin_ids`), so a change of the public
    address applies to the next check: passkeys made for the old address no longer count, as
    they no longer sign in. Two servers with different public addresses on one database each
    record their own at start-up, and the last one started decides; give them one address.

    Parameters
    ----------
    session
        Database session.
    rp_id
        The host of the public address (`Settings.public_origin().host`).

    Returns
    -------
    str or None
        The id recorded before, when it was another one; `None` when it was the same or none.
    """
    row = session.get(InstanceSetting, RP_ID_KEY)
    before = row.value if row is not None and isinstance(row.value, str) else None
    if before == rp_id:
        return None
    if row is None:
        row = InstanceSetting(key=RP_ID_KEY)
        session.add(row)
    row.value = rp_id
    row.updated_at = datetime.now(UTC)
    return before


def current_rp_id(session: Session) -> str | None:
    """Read the relying-party id passkeys sign in with.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    str or None
        The recorded id; `None` before a server with the web interface started, when every
        passkey that is not disabled counts.
    """
    row = session.get(InstanceSetting, RP_ID_KEY)
    return row.value if row is not None and isinstance(row.value, str) else None


def unlinked_admin_ids(
    session: Session,
    admin_ids: Collection[uuid.UUID],
    *,
    excluding_passkey_id: uuid.UUID | None = None,
) -> set[uuid.UUID]:
    """Pick the administrators who can sign in without the provider.

    Parameters
    ----------
    session
        Database session; each call reads committed rows afresh, never the session's cache.
    admin_ids
        The active instance administrators to choose from
        (`lakana_core.grants.service.active_instance_admin_ids`).
    excluding_passkey_id
        A passkey to treat as already deleted, for a removal not yet committed.

    Returns
    -------
    set of uuid.UUID
        Those of `admin_ids` who are not linked and have a passkey that is not disabled, for the
        current relying party (`current_rp_id`).
    """
    if not admin_ids:
        return set()
    working = select(Passkey.id).where(
        Passkey.principal_id == Principal.id, Passkey.disabled_at.is_(None)
    )
    rp_id = current_rp_id(session)
    if rp_id is not None:
        working = working.where(Passkey.rp_id == rp_id)
    if excluding_passkey_id is not None:
        working = working.where(Passkey.id != excluding_passkey_id)
    return set(
        session.execute(
            select(Principal.id).where(
                Principal.id.in_(admin_ids),
                unlinked_clause(session),
                exists(working),
            )
        ).scalars()
    )


def _linked_ids(session: Session, ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
    """Pick the people linked to the provider.

    Parameters
    ----------
    session
        Database session.
    ids
        The principals to choose from.

    Returns
    -------
    set of uuid.UUID
        Those of `ids` linked to the provider (`lakana_core.identity.provider_issuer`): a
        stored link to another issuer does not count.
    """
    if not ids:
        return set()
    return set(
        session.execute(
            select(Principal.id).where(Principal.id.in_(ids), linked_clause(session))
        ).scalars()
    )


def holds(
    session: Session,
    admin_ids: Collection[uuid.UUID],
    *,
    linking: uuid.UUID | None = None,
    excluding_passkey_id: uuid.UUID | None = None,
) -> bool:
    """Say whether a set of administrators keeps the break-glass rule.

    Parameters
    ----------
    session
        Database session.
    admin_ids
        The active instance administrators.
    linking
        A person to treat as linked, for a link not yet made.
    excluding_passkey_id
        A passkey to treat as deleted, for a removal not yet made.

    Returns
    -------
    bool
        Whether no administrator is linked, or at least one unlinked administrator remains.
    """
    linked = _linked_ids(session, admin_ids)
    if linking is not None and linking in admin_ids:
        linked.add(linking)
    if not linked:
        return True
    remaining = unlinked_admin_ids(session, admin_ids, excluding_passkey_id=excluding_passkey_id)
    if linking is not None:
        remaining.discard(linking)
    return bool(remaining)


def breaks(
    session: Session,
    *,
    before: Collection[uuid.UUID],
    after: Collection[uuid.UUID],
    linking: uuid.UUID | None = None,
    excluding_passkey_id: uuid.UUID | None = None,
) -> bool:
    """Say whether a change breaks the break-glass rule that held before it.

    Call it under `lakana_core.grants.admin_lock.lock_admin_count`.

    Parameters
    ----------
    session
        Database session.
    before
        The active instance administrators now.
    after
        The active instance administrators once the change is made.
    linking
        The person the change links to the provider, if it links one.
    excluding_passkey_id
        The passkey the change deletes, if it deletes one.

    Returns
    -------
    bool
        Whether the rule holds now (`holds`) and would not after the change.
    """
    return holds(session, before) and not holds(
        session, after, linking=linking, excluding_passkey_id=excluding_passkey_id
    )


def address_change_warning(
    before: str | None, after: str, *, linked: int, unlinked_admins: int
) -> str | None:
    """Word the start-up warning of a public address that changed while people are linked.

    Parameters
    ----------
    before
        The relying-party id recorded before (`record_rp_id`), or `None` for the same one.
    after
        The one recorded now.
    linked
        Active people linked to the identity provider.
    unlinked_admins
        Administrators who can sign in with a passkey at the new address, counted after the
        change (`unlinked_admin_ids`).

    Returns
    -------
    str or None
        The warning; `None` when the address did not change or nobody is linked.
    """
    if before is None or linked == 0:
        return None
    text = (
        f"The public address changed from {before} to {after}: passkeys registered for {before} "
        f"no longer sign in here. {unlinked_admins} administrator(s) can sign in with a passkey at "
        f"the new address while {linked} people sign in through the identity provider."
    )
    if unlinked_admins == 0:
        text += (
            " None is left to sign in if the provider fails: issue an administrator a recovery "
            "link with the server operator's bootstrap token (lakana recovery create NAME) and "
            "have them register a passkey at the new address."
        )
    return text
