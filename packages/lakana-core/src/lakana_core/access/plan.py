"""Plan an access file: the changes an apply makes, in order, and the drift (#46).

A plan is a set difference between the file's entries and the live entries of the scopes the
file manages (R5). Only CI trust rules change in place (R6); every other difference is a
removal plus a create.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from lakana_core.access.file import AccessFile
from lakana_core.access.live import LiveAccess
from lakana_core.access.model import (
    ENTRY_ORDER,
    INSTANCE,
    CiTrustEntry,
    Entry,
    GroupEntry,
    Scopes,
    ServiceAccountEntry,
    describe,
    environment_of,
    sort_key,
)

_OPS = ("create", "change", "remove")


@dataclass(frozen=True)
class PlannedChange:
    """One change an apply makes.

    Parameters
    ----------
    op
        ``create``, ``change`` (a CI trust rule's settings) or ``remove``.
    entry
        The entry created, the rule as it becomes, or the entry removed.
    path
        Where the file lists the entry, such as ``grants[3]``; `None` for a removal.
    before
        For a change, the live rule it replaces.
    ids
        The row ids a change or removal acts on: every live copy of the entry.
    """

    op: str
    entry: Entry
    path: str | None
    before: Entry | None = None
    ids: tuple[uuid.UUID, ...] = ()

    @property
    def text(self) -> str:
        """Say the change in one line.

        Returns
        -------
        str
            The entry as `describe` says it; for a change, the rule before and after.
        """
        if self.before is None:
            return describe(self.entry)
        return f"{describe(self.before)} becomes {describe(self.entry)}"


@dataclass(frozen=True)
class Drift:
    """Access in a managed scope that did not come from the file.

    Parameters
    ----------
    scope
        The scope it lies in.
    text
        What it is and what apply does with it.
    by
        Who made it, when known (an override).
    reason
        Why, when known (an override).
    at
        When, when known (an override).
    """

    scope: str
    text: str
    by: str | None = None
    reason: str | None = None
    at: datetime | None = None


@dataclass(frozen=True)
class AccessPlan:
    """What applying a file would change, and every problem that stops it.

    Parameters
    ----------
    file_name
        The base name the messages use.
    file_sha256
        Hash of the file's text.
    manages
        The scopes the file manages; empty when the file did not parse.
    changes
        Every change: creates, then changes, then removals, each sorted.
    releases
        Managed scopes the file no longer lists (from PR 2 on; none before).
    starts
        Scopes the file manages that no apply manages yet, without the environments a managed
        ``instance`` already covers; from the apply on, changes made by hand there are refused.
    drift
        Access in the scopes that did not come from the file.
    problems
        Every problem; an apply refuses while there is one.
    fingerprint
        Hash of the live state of the managed scopes, which apply checks again.
    """

    file_name: str
    file_sha256: str
    manages: tuple[str, ...]
    changes: tuple[PlannedChange, ...]
    releases: tuple[str, ...]
    starts: tuple[str, ...]
    drift: tuple[Drift, ...]
    problems: tuple[str, ...]
    fingerprint: str

    def _of(self, op: str) -> tuple[PlannedChange, ...]:
        """Keep the changes of one kind.

        Parameters
        ----------
        op
            ``create``, ``change`` or ``remove``.

        Returns
        -------
        tuple of PlannedChange
            Those changes, in plan order.
        """
        return tuple(change for change in self.changes if change.op == op)

    @property
    def creates(self) -> tuple[PlannedChange, ...]:
        """List the entries the apply creates.

        Returns
        -------
        tuple of PlannedChange
            The creates.
        """
        return self._of("create")

    @property
    def changed(self) -> tuple[PlannedChange, ...]:
        """List the CI trust rules the apply changes in place.

        Returns
        -------
        tuple of PlannedChange
            The in-place changes.
        """
        return self._of("change")

    @property
    def removals(self) -> tuple[PlannedChange, ...]:
        """List the entries the apply removes.

        Returns
        -------
        tuple of PlannedChange
            The removals.
        """
        return self._of("remove")

    @property
    def is_empty(self) -> bool:
        """Say whether an apply would change nothing.

        Returns
        -------
        bool
            True without changes, releases and starts.
        """
        return not self.changes and not self.releases and not self.starts


def diff(desired: AccessFile, live: LiveAccess, scopes: Scopes) -> tuple[PlannedChange, ...]:
    """Compare a file with the live entries of the scopes it manages.

    Parameters
    ----------
    desired
        The checked file: issuers resolved, expressions normalized.
    live
        The live facts, normalized the same way.
    scopes
        The scopes the file manages.

    Returns
    -------
    tuple of PlannedChange
        Creates, changes and removals, each sorted by `sort_key`. A live CI trust rule the file
        lists with other settings is one change carrying the rule's row ids; a removal carries
        the ids of every live copy.
    """
    want = {entry: path for path, entry in desired.entries}
    owned = live.state.owned(scopes).entries
    spare: dict[tuple[object, ...], list[CiTrustEntry]] = {}
    for entry in sorted(owned - want.keys(), key=sort_key):
        if isinstance(entry, CiTrustEntry):
            spare.setdefault(entry.key, []).append(entry)
    changes: list[PlannedChange] = []
    replaced: set[Entry] = set()
    for entry, path in want.items():
        if entry in owned:
            continue
        candidates = spare.get(entry.key, []) if isinstance(entry, CiTrustEntry) else []
        if candidates:
            before = candidates.pop(0)
            replaced.add(before)
            changes.append(PlannedChange("change", entry, path, before, live.ids.get(before, ())))
        else:
            changes.append(PlannedChange("create", entry, path))
    for entry in owned - want.keys() - replaced:
        changes.append(PlannedChange("remove", entry, None, ids=live.ids.get(entry, ())))
    return tuple(sorted(changes, key=lambda c: (_OPS.index(c.op), sort_key(c.entry))))


def drift(
    changes: Sequence[PlannedChange], scopes: Scopes, managed: frozenset[str]
) -> tuple[Drift, ...]:
    """Name the access an apply removes from scopes no file manages yet.

    Before a scope is managed, everything in it was made outside a file, so each removal there
    is drift. In a managed scope a removal is a plain removal: there only open overrides are
    drift (PR 2).

    Parameters
    ----------
    changes
        The plan's changes.
    scopes
        The scopes the file manages.
    managed
        The scopes an earlier apply recorded as managed.

    Returns
    -------
    tuple of Drift
        One per removal in a scope not yet managed, in plan order.
    """
    found: list[Drift] = []
    for change in changes:
        if change.op != "remove":
            continue
        scope = INSTANCE if scopes.instance else str(environment_of(change.entry))
        if scope not in managed:
            text = f"{describe(change.entry)} exists outside the file; apply removes it"
            found.append(Drift(scope, text))
    return tuple(found)


def apply_order(changes: Sequence[PlannedChange]) -> list[PlannedChange]:
    """Order changes so each one finds what it depends on.

    Parameters
    ----------
    changes
        Any changes.

    Returns
    -------
    list of PlannedChange
        Creates and changes with groups and service accounts first, before the memberships and
        grants that name them, then the rest in `ENTRY_ORDER`; removals in the reverse order
        (what names a group or a service account before it).
    """
    ups = sorted((c for c in changes if c.op != "remove"), key=lambda c: _apply_key(c.entry))
    downs = sorted(
        (c for c in changes if c.op == "remove"),
        key=lambda c: (-_APPLY_ORDER.index(type(c.entry)), sort_key(c.entry)[1]),
    )
    return ups + downs


_APPLY_ORDER: tuple[type, ...] = (
    GroupEntry,
    ServiceAccountEntry,
    *(kind for kind in ENTRY_ORDER if kind not in (GroupEntry, ServiceAccountEntry)),
)
"""`ENTRY_ORDER` with service accounts moved before memberships, which may name them."""


def _apply_key(entry: Entry) -> tuple[int, str]:
    """Order an entry for creating it.

    Parameters
    ----------
    entry
        Any entry.

    Returns
    -------
    tuple of (int, str)
        Its kind's place in `_APPLY_ORDER` and its canonical JSON text.
    """
    return _APPLY_ORDER.index(type(entry)), sort_key(entry)[1]
