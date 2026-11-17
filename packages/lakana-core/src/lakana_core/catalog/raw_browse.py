"""Browsing `raw/` one folder at a time, to register an external table (#672).

New external table asks for a location under `raw/`; this lists what lies there, one page of
one folder per call, so a person picks the location instead of typing it.

- **Who may browse.** Whoever may register an external table in some namespace of the
  environment (`CREATE_TABLE` on the namespace, the dialog's own right), asked in one
  `permits_each` batch; anyone else is refused before storage is read.
- **Claims.** An entry inside an external table's claim (the claimed folder itself, a folder
  below it, or a file in one) cannot be chosen and says so; it names the table only to a reader
  who may read it (`READ_TABLE`), asked once per page. A folder above a claim stays open: a free
  folder beside the claimed one must stay reachable, and registering the folder above is refused
  by `check_claim_free` as before.
- **Audit.** Each folder read writes `ACTIVITY_VIEWED` (`View.RAW_FILES`, environment scope,
  targeting key ``{"path": folder}``) through `record_view`, once per reader and folder within
  the window, before the answer is returned; it fails closed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, permits_each
from lakana_core.catalog.external_location import ExternalClaim, claim_covers
from lakana_core.errors import StorageRefusedError, StorageUnavailableError
from lakana_core.identifiers import NamespacePath, shown
from lakana_core.log_events import LogEvent, log_event
from lakana_core.models import Environment, Namespace
from lakana_core.storage.base import ChildEntry, Storage
from lakana_core.storage.unavailable import (
    RAW_STORAGE_REFUSED,
    RAW_STORAGE_UNREACHABLE,
    storage_failure,
)

logger = logging.getLogger(__name__)

RAW_CURSOR_KIND = "raw"
"""The list a cursor of `CatalogService.list_raw` belongs to."""


@dataclass(frozen=True)
class RawEntry:
    """One folder or file directly in a browsed folder.

    Parameters
    ----------
    name
        Its name in the folder.
    kind
        ``"folder"`` or ``"file"``.
    size
        A file's size in bytes; `None` for a folder.
    claimed
        Whether it lies in an external table's claim, so it cannot be chosen.
    claimed_by
        The dotted name of the claiming table, when the reader may read it; else `None`.
    """

    name: str
    kind: Literal["folder", "file"]
    size: int | None
    claimed: bool
    claimed_by: str | None


@dataclass(frozen=True)
class RawPage:
    """One page of a browsed folder.

    Parameters
    ----------
    path
        The folder, such as ``"raw/orders"``.
    items
        Its entries, folders and files in key order (a folder's key is its name and ``/``).
    next_cursor
        The cursor of the next page; `None` on the last.
    """

    path: str
    items: list[RawEntry]
    next_cursor: str | None


def may_browse_raw(
    session: Session, authz: AuthorizationService, p: PrincipalRef, environment: str
) -> bool:
    """Tell whether `p` may register an external table in some namespace of an environment.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service.
    p
        The reader.
    environment
        The environment's name.

    Returns
    -------
    bool
        Whether `CREATE_TABLE` is permitted on at least one of its namespaces. Writes no audit
        row; the caller refuses through `authorize` when this is `False`.
    """
    paths = session.scalars(
        select(Namespace.path)
        .join(Environment, Namespace.environment_id == Environment.id)
        .where(Environment.name == environment, Environment.deleted_at.is_(None))
    ).all()
    questions = [
        (Action.CREATE_TABLE, Securable.namespace_scope(environment, NamespacePath.parse(path)))
        for path in paths
    ]
    return any(permits_each(authz, p, questions)) if questions else False


def read_children(
    storage: Storage, environment: str, folder: str, after: str | None, limit: int
) -> Sequence[ChildEntry]:
    """Read one page of a folder, with a storage failure turned into its own error.

    Parameters
    ----------
    storage
        The environment's storage.
    environment
        The environment's name, for the message.
    folder
        The folder.
    after
        The key to start after, or `None`.
    limit
        Most entries.

    Returns
    -------
    list of ChildEntry
        The page.

    Raises
    ------
    StorageRefusedError
        If the storage refused the credentials (`RAW_STORAGE_REFUSED`).
    StorageUnavailableError
        If the storage did not answer (`RAW_STORAGE_UNREACHABLE`).
    Exception
        Any other error, as it was raised.
    """
    try:
        return storage.list_children_page(folder, after=after, limit=limit)
    except Exception as exc:
        failure = storage_failure(exc)
        if failure is None:
            raise
        refused = failure == "refused"
        logger.warning(
            "The storage of environment %s %s while raw/ was listed (%s).",
            shown(environment),
            "refused access" if refused else "could not be reached",
            type(exc).__name__,
            extra=log_event(LogEvent.STORAGE_REFUSED if refused else LogEvent.STORAGE_UNREACHABLE),
        )
        if refused:
            raise StorageRefusedError(
                RAW_STORAGE_REFUSED.format(environment=shown(environment))
            ) from None
        raise StorageUnavailableError(
            RAW_STORAGE_UNREACHABLE.format(environment=shown(environment))
        ) from None


def raw_entries(
    authz: AuthorizationService,
    p: PrincipalRef,
    environment: str,
    folder: str,
    children: Sequence[ChildEntry],
    claims: Sequence[ExternalClaim],
) -> list[RawEntry]:
    """Describe a page of entries, each with the claim it lies in.

    Parameters
    ----------
    authz
        The authorization service.
    p
        The reader.
    environment
        The environment's name.
    folder
        The folder the entries are in.
    children
        The entries.
    claims
        The environment's claims (`external_claims`).

    Returns
    -------
    list of RawEntry
        The entries; whether each claiming table may be read is asked once per page.
    """
    own = next((c for c in claims if claim_covers(c.claim, folder)), None)
    holders = [
        next((c for c in claims if claim_covers(c.claim, f"{folder}/{child.name}")), None)
        if child.is_directory
        else own
        for child in children
    ]
    distinct = list(dict.fromkeys(h for h in holders if h is not None))
    readable = permits_each(
        authz,
        p,
        [
            (Action.READ_TABLE, Securable.table_scope(environment, h.namespace, h.name))
            for h in distinct
        ],
    )
    named = {
        h: f"{h.namespace.joined()}.{h.name}"
        for h, ok in zip(distinct, readable, strict=True)
        if ok
    }
    return [
        RawEntry(
            name=child.name,
            kind="folder" if child.is_directory else "file",
            size=child.size,
            claimed=holder is not None,
            claimed_by=named.get(holder) if holder is not None else None,
        )
        for child, holder in zip(children, holders, strict=True)
    ]
