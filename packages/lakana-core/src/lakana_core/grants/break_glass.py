"""The break-glass count: administrators who can sign in without the identity provider (#32).

An *unlinked administrator* is an active principal who holds ADMIN on the instance (directly or
through a group), is not linked to the identity provider, and has at least one passkey that is
not disabled. While some administrator signs in through the provider, at least one unlinked
administrator must remain, so that an outage at the provider, or a provider that refuses
everyone, cannot lock the instance out (the sign-in design, "Break-glass").

Every change that can lower the count asks `leaves_no_unlinked_admin` under the
administrator-count lock (`lakana_core.grants.admin_lock.lock_admin_count`), in the order that
lock documents: linking a person (`lakana_core.identity.oidc_links.OidcLinkService.link`),
deleting a passkey (`lakana_core.identity.passkeys.PasskeyService.remove`), and every change
`lakana_core.grants.service.assert_admin_headroom` guards (revoking ADMIN, removing a member of a
group that holds it, deactivating, an access apply). A passkey disabled as a copy and the server
operator's recovery are not refused: the first is a security response, the second is the
break-glass path itself.

The rule refuses only a change that takes the count from one or more to none while a linked
administrator remains. An instance that links nobody keeps today's rules, and a count already at
zero blocks nothing, as `assert_admin_headroom` lets every change through on an instance without
administrators.

The functions take the administrator ids from their caller, so this module needs nothing of
`lakana_core.grants.service` and both may use it.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from lakana_core.models import Passkey, Principal

LAST_UNLINKED_ADMIN_MESSAGE = (
    "This would leave no administrator who can sign in with a passkey: every other "
    "administrator signs in through the identity provider, and an outage there would lock "
    "everyone out. Add a passkey for another administrator who is not linked first, or unlink "
    "one."
)
"""The 409 detail of a change that would take the last unlinked administrator away."""


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
        Those of `admin_ids` who are not linked and have a passkey that is not disabled.
    """
    if not admin_ids:
        return set()
    working = select(Passkey.id).where(
        Passkey.principal_id == Principal.id, Passkey.disabled_at.is_(None)
    )
    if excluding_passkey_id is not None:
        working = working.where(Passkey.id != excluding_passkey_id)
    return set(
        session.execute(
            select(Principal.id).where(
                Principal.id.in_(admin_ids),
                Principal.oidc_subject.is_(None),
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
        Those of `ids` with a provider identity.
    """
    if not ids:
        return set()
    return set(
        session.execute(
            select(Principal.id).where(Principal.id.in_(ids), Principal.oidc_subject.is_not(None))
        ).scalars()
    )


def leaves_no_unlinked_admin(
    session: Session,
    *,
    before: Collection[uuid.UUID],
    after: Collection[uuid.UUID],
    linking: uuid.UUID | None = None,
    excluding_passkey_id: uuid.UUID | None = None,
) -> bool:
    """Say whether a change would take the last unlinked administrator while a linked one remains.

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
        Whether there is at least one unlinked administrator now, none would remain, and an
        administrator linked to the provider would.
    """
    if not unlinked_admin_ids(session, before):
        return False
    remaining = unlinked_admin_ids(session, after, excluding_passkey_id=excluding_passkey_id)
    linked = _linked_ids(session, after)
    if linking is not None:
        remaining.discard(linking)
        linked.add(linking)
    return not remaining and bool(linked)
