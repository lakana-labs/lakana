"""Read the live access state of the instance as entries, with the row ids behind each (#46)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pyiceberg.types import NestedField
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.access.model import (
    AccessState,
    CiTrustEntry,
    ColumnMaskEntry,
    Entry,
    GrantEntry,
    GroupEntry,
    MembershipEntry,
    RowFilterEntry,
    ServiceAccountEntry,
    Subject,
    Target,
)
from lakana_core.identifiers import NamespacePath
from lakana_core.models import (
    CiTrustRule,
    ColumnMask,
    Environment,
    Grant,
    Group,
    GroupMembership,
    Principal,
    PrincipalType,
    Repository,
    RowFilter,
)
from lakana_core.policy.shown import shown_policies


@dataclass(frozen=True)
class LiveAccess:
    """The access facts of the instance.

    Parameters
    ----------
    state
        Every entry; a policy is in the names its table's columns have now.
    ids
        The row id of every live copy of a grant, row filter, column mask or CI trust rule,
        by entry. Two identical grants are one entry with two ids.
    active
        The names of the active principals.
    stored
        For a policy shown under a renamed column, the entry as stored (the older column name
        and text), by the shown entry: a file written before the rename still matches.
    """

    state: AccessState
    ids: Mapping[Entry, tuple[uuid.UUID, ...]]
    active: frozenset[str]
    stored: Mapping[Entry, tuple[Entry, ...]]

    def mapped(self, change: Callable[[Entry], Entry]) -> LiveAccess:
        """Rebuild the facts with every entry changed, such as to a normalized spelling.

        Entries the change makes equal become one entry with the row ids of all of them, so a
        removal still names every row; a stored form that becomes its shown entry is dropped.

        Parameters
        ----------
        change
            The change of one entry.

        Returns
        -------
        LiveAccess
            The same facts, keyed by the changed entries.
        """
        ids: defaultdict[Entry, list[uuid.UUID]] = defaultdict(list)
        for entry, found in self.ids.items():
            ids[change(entry)].extend(found)
        stored: defaultdict[Entry, list[Entry]] = defaultdict(list)
        for entry, forms in self.stored.items():
            shown = change(entry)
            for form in map(change, forms):
                if form != shown and form not in stored[shown]:
                    stored[shown].append(form)
        return LiveAccess(
            AccessState(frozenset(map(change, self.state.entries))),
            {entry: tuple(found) for entry, found in ids.items()},
            self.active,
            {entry: tuple(forms) for entry, forms in stored.items() if forms},
        )


def _target(environment: str | None, namespace: str | None, table: str | None) -> Target:
    """Build a target from stored names.

    Parameters
    ----------
    environment
        The environment; `None` for the instance.
    namespace
        The namespace in its joined storage form, or `None`.
    table
        The table, or `None`.

    Returns
    -------
    Target
        The target.
    """
    parts = NamespacePath.parse(namespace).parts if namespace else ()
    return Target(environment, tuple(parts), table)


def read_live(
    session: Session, fields_of: Callable[[uuid.UUID], Sequence[NestedField] | None]
) -> LiveAccess:
    """Read every access fact of the instance as entries, with the row ids behind each.

    Grants, policies, memberships and CI trust rules of a deactivated principal are left out
    (R4): they give nothing, and leaving them out keeps an exported file valid. Policies are
    read in the names of their table's current schema (`shown_policies`, #120).

    Parameters
    ----------
    session
        Database session.
    fields_of
        The current top-level fields of a table by id (`CatalogService.policy_fields`).

    Returns
    -------
    LiveAccess
        The state, the ids of every copy, the names of the active principals and the stored
        form of every policy shown under a renamed column.
    """
    principals = {
        row.id: row
        for row in session.execute(select(Principal).where(Principal.is_active.is_(True))).scalars()
    }
    groups = {row.id: row.name for row in session.execute(select(Group)).scalars()}
    entries: set[Entry] = set()
    ids: defaultdict[Entry, list[uuid.UUID]] = defaultdict(list)
    stored: defaultdict[Entry, list[Entry]] = defaultdict(list)

    def subject_of(principal_id: uuid.UUID | None, group_id: uuid.UUID | None) -> Subject | None:
        """Name the subject of a row, or nothing for a deactivated principal.

        Parameters
        ----------
        principal_id
            The row's principal, or `None`.
        group_id
            The row's group, or `None`.

        Returns
        -------
        Subject or None
            The group or the active principal; `None` when the principal is not active.
        """
        if group_id is not None:
            return Subject("group", groups[group_id])
        found = principals.get(principal_id) if principal_id is not None else None
        if found is None:
            return None
        return Subject("user" if found.type == PrincipalType.USER else "service", found.name)

    entries.update(GroupEntry(name) for name in groups.values())
    for row in principals.values():
        if row.type == PrincipalType.SERVICE_ACCOUNT:
            entries.add(ServiceAccountEntry(row.name))
    for member in session.execute(select(GroupMembership)).scalars():
        found = principals.get(member.principal_id)
        if found is not None:
            entries.add(MembershipEntry(groups[member.group_id], found.name))

    for grant in session.execute(select(Grant).where(Grant.revoked_at.is_(None))).scalars():
        to = subject_of(grant.principal_id, grant.group_id)
        if to is None:
            continue
        entry = GrantEntry(
            to,
            grant.role.value if grant.role else None,
            grant.action.value if grant.action else None,
            _target(grant.environment, grant.namespace, grant.table),
        )
        entries.add(entry)
        ids[entry].append(grant.id)

    filters = list(
        session.execute(select(RowFilter).where(RowFilter.revoked_at.is_(None))).scalars()
    )
    masks = list(
        session.execute(select(ColumnMask).where(ColumnMask.revoked_at.is_(None))).scalars()
    )
    shown = shown_policies([*filters, *masks], fields_of)
    for row in (*filters, *masks):
        to = subject_of(row.principal_id, row.group_id)
        if to is None:
            continue
        table = _target(row.environment, row.namespace, row.table)
        show = shown[row.id]
        if isinstance(row, RowFilter):
            entry = RowFilterEntry(to, table, show.expression)
            as_stored: Entry = RowFilterEntry(to, table, row.expression)
        else:
            entry = ColumnMaskEntry(to, table, show.column or row.column, show.expression)
            as_stored = ColumnMaskEntry(to, table, row.column, row.expression)
        entries.add(entry)
        ids[entry].append(row.id)
        if as_stored != entry and as_stored not in stored[entry]:
            stored[entry].append(as_stored)

    environments = {row.id: row.name for row in session.execute(select(Environment)).scalars()}
    repositories = {row.id: row.name for row in session.execute(select(Repository)).scalars()}
    for rule in session.execute(select(CiTrustRule)).scalars():
        account = principals.get(rule.principal_id)
        if account is None:
            continue
        entry = CiTrustEntry(
            rule.issuer,
            repositories[rule.repository_id],
            account.name,
            environments[rule.environment_id],
            tuple(sorted((str(k), str(v)) for k, v in rule.conditions.items())),
            tuple(sorted(rule.kinds)),
            rule.enabled,
            rule.start_on_deploy_jobs,
        )
        entries.add(entry)
        ids[entry].append(rule.id)

    return LiveAccess(
        AccessState(frozenset(entries)),
        {entry: tuple(found) for entry, found in ids.items()},
        frozenset(row.name for row in principals.values()),
        {entry: tuple(found) for entry, found in stored.items()},
    )
