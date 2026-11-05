"""The entries of an access file and the live access state, as one set of types (#46).

An entry is one fact of access: a group, a membership, a service account, a grant, a row filter,
a column mask or a CI trust rule. The parser (`access.file`) and the live reader produce these
same types, so a plan is a set difference and a fingerprint is a hash of a sorted set.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from lakana_core.auth import SecurableKind
from lakana_core.identifiers import NamespacePath, environment_name_problem

INSTANCE = "instance"
"""The scope that covers every environment and the instance-wide objects."""

SUBJECT_KINDS = ("user", "group", "service")


@dataclass(frozen=True, order=True)
class Subject:
    """Whom an entry gives access to.

    Parameters
    ----------
    kind
        ``user``, ``group`` or ``service``.
    name
        The principal's or group's name.
    """

    kind: str
    name: str

    def __str__(self) -> str:
        """Write the subject as the file does.

        Returns
        -------
        str
            ``kind:name``.
        """
        return f"{self.kind}:{self.name}"


@dataclass(frozen=True, order=True)
class Target:
    """What an entry applies to: the instance, an environment, a namespace or a table.

    Parameters
    ----------
    environment
        The environment; `None` for the instance.
    namespace
        The namespace's parts, outermost first; empty above namespace level.
    table
        The table; `None` above table level.
    """

    environment: str | None = None
    namespace: tuple[str, ...] = ()
    table: str | None = None

    @property
    def kind(self) -> SecurableKind:
        """Say which tier of the securable hierarchy this is.

        Returns
        -------
        SecurableKind
            INSTANCE, ENVIRONMENT, NAMESPACE or TABLE.
        """
        if self.environment is None:
            return SecurableKind.INSTANCE
        if not self.namespace:
            return SecurableKind.ENVIRONMENT
        return SecurableKind.NAMESPACE if self.table is None else SecurableKind.TABLE

    @property
    def namespace_joined(self) -> str | None:
        """Give the namespace in the storage form the services take.

        Returns
        -------
        str or None
            `NamespacePath.joined()`; `None` without a namespace.
        """
        return NamespacePath.from_parts(self.namespace).joined() if self.namespace else None

    def __str__(self) -> str:
        """Write the target as the file does.

        Returns
        -------
        str
            ``instance``, ``prod``, ``prod.gold.sales`` or ``prod.gold.orders``.
        """
        if self.environment is None:
            return INSTANCE
        return ".".join((self.environment, *self.namespace, *((self.table,) if self.table else ())))


@dataclass(frozen=True, order=True)
class GroupEntry:
    """A group.

    Parameters
    ----------
    name
        The group's name.
    """

    name: str


@dataclass(frozen=True, order=True)
class MembershipEntry:
    """A user's membership of a group.

    Parameters
    ----------
    group
        The group's name.
    member
        The member's name.
    """

    group: str
    member: str


@dataclass(frozen=True, order=True)
class ServiceAccountEntry:
    """A service account.

    Parameters
    ----------
    name
        The account's name.
    """

    name: str


@dataclass(frozen=True, order=True)
class GrantEntry:
    """A role or an action granted to a subject on a target.

    Parameters
    ----------
    to
        Who receives it.
    role
        The role, or `None` when an action is granted.
    action
        The action, or `None` when a role is granted.
    on
        Where it applies.
    """

    to: Subject
    role: str | None
    action: str | None
    on: Target


@dataclass(frozen=True, order=True)
class RowFilterEntry:
    """A row filter on a table for a subject.

    Parameters
    ----------
    to
        Whose reads it filters.
    table
        The table.
    expression
        The SQL predicate.
    """

    to: Subject
    table: Target
    expression: str


@dataclass(frozen=True, order=True)
class ColumnMaskEntry:
    """A column mask on a table column for a subject.

    Parameters
    ----------
    to
        Whose reads it masks.
    table
        The table.
    column
        The column.
    expression
        The SQL expression shown instead of the value.
    """

    to: Subject
    table: Target
    column: str
    expression: str


@dataclass(frozen=True, order=True)
class CiTrustEntry:
    """A rule that lets a CI job act as a service account.

    Parameters
    ----------
    issuer
        The OIDC issuer, as the file names it.
    repository
        The repository the job runs for.
    as_
        The service account the job acts as.
    on
        The environment it may act in.
    conditions
        Claim name and required value pairs, sorted.
    kinds
        What the job may do: ``deploy``, ``dry_run``; sorted.
    enabled
        Whether the rule is in force.
    start_on_deploy_jobs
        Whether a deploy through this rule may start the project's on-deploy jobs.
    """

    issuer: str
    repository: str
    as_: str
    on: str
    conditions: tuple[tuple[str, str], ...]
    kinds: tuple[str, ...] = ("deploy",)
    enabled: bool = True
    start_on_deploy_jobs: bool = False

    @property
    def key(self) -> tuple[object, ...]:
        """Identify the rule apart from its settings.

        Returns
        -------
        tuple
            Issuer, repository, account, environment and conditions.
        """
        return (self.issuer, self.repository, self.as_, self.on, self.conditions)


Entry = (
    GroupEntry
    | MembershipEntry
    | ServiceAccountEntry
    | GrantEntry
    | RowFilterEntry
    | ColumnMaskEntry
    | CiTrustEntry
)

ENTRY_ORDER: tuple[type, ...] = (
    GroupEntry,
    MembershipEntry,
    ServiceAccountEntry,
    GrantEntry,
    RowFilterEntry,
    ColumnMaskEntry,
    CiTrustEntry,
)


def entry_json(entry: Entry) -> dict[str, object]:
    """Give an entry as JSON-ready data, the input of `fingerprint` and of the audit counts.

    Parameters
    ----------
    entry
        Any entry.

    Returns
    -------
    dict
        ``type`` plus every field; subjects and targets as their file text.
    """
    data: dict[str, object] = {"type": type(entry).__name__}
    for item in dataclasses.fields(entry):
        value = getattr(entry, item.name)
        data[item.name] = str(value) if isinstance(value, Subject | Target) else value
    return data


def sort_key(entry: Entry) -> tuple[int, str]:
    """Order entries by kind, then by their canonical JSON.

    Parameters
    ----------
    entry
        Any entry.

    Returns
    -------
    tuple of (int, str)
        The kind's place in `ENTRY_ORDER` and the entry's canonical JSON text.
    """
    return ENTRY_ORDER.index(type(entry)), json.dumps(entry_json(entry), sort_keys=True)


def environment_of(entry: Entry) -> str | None:
    """Say which environment an entry belongs to.

    Parameters
    ----------
    entry
        Any entry.

    Returns
    -------
    str or None
        The environment; `None` for groups, memberships, service accounts and instance grants.
    """
    match entry:
        case GrantEntry(on=on):
            return on.environment
        case RowFilterEntry(table=table) | ColumnMaskEntry(table=table):
            return table.environment
        case CiTrustEntry(on=on):
            return on
        case _:
            return None


def unwritable_environments(state: AccessState) -> list[str]:
    """Name each environment the state has access on whose name no access file can write.

    Parameters
    ----------
    state
        The entries to check.

    Returns
    -------
    list of str
        One sentence per environment, sorted by name, each saying the cause and the next step.
    """
    names = sorted({e for entry in state.entries if (e := environment_of(entry)) is not None})
    found = []
    for name in names:
        if (problem := environment_name_problem(name)) is not None:
            found.append(
                f"environment '{_line(name)}' has access that an access file cannot name: its "
                f"name {problem[0]}. Create the environment again under another name with "
                "lakana env create and move its access there, or leave it out of this scope"
            )
    return found


@dataclass(frozen=True)
class Scopes:
    """The scopes one file manages.

    Parameters
    ----------
    names
        ``{"instance"}`` or environment names.
    """

    names: frozenset[str]

    @property
    def instance(self) -> bool:
        """Say whether the whole instance is managed.

        Returns
        -------
        bool
            True when ``instance`` is one of the names.
        """
        return INSTANCE in self.names

    def owns(self, entry: Entry) -> bool:
        """Say whether an entry lies inside these scopes.

        Parameters
        ----------
        entry
            Any entry.

        Returns
        -------
        bool
            True with ``instance``; else when the entry's environment is one of the names.
        """
        if self.instance:
            return True
        environment = environment_of(entry)
        return environment is not None and environment in self.names

    def union(self, other: Scopes) -> Scopes:
        """Join two sets of scopes.

        Parameters
        ----------
        other
            More scopes.

        Returns
        -------
        Scopes
            Both.
        """
        return Scopes(self.names | other.names)


@dataclass(frozen=True)
class AccessState:
    """A set of entries: what a file says or what the instance holds.

    Parameters
    ----------
    entries
        The entries.
    """

    entries: frozenset[Entry]

    def owned(self, scopes: Scopes) -> AccessState:
        """Keep the entries inside some scopes.

        Parameters
        ----------
        scopes
            The scopes.

        Returns
        -------
        AccessState
            The entries `scopes` owns.
        """
        return AccessState(frozenset(e for e in self.entries if scopes.owns(e)))

    def outside(self, scopes: Scopes) -> AccessState:
        """Keep the entries outside some scopes.

        Parameters
        ----------
        scopes
            The scopes.

        Returns
        -------
        AccessState
            The entries `scopes` does not own.
        """
        return AccessState(frozenset(e for e in self.entries if not scopes.owns(e)))

    def of_type(self, kind: type) -> list:
        """List the entries of one type in a stable order.

        Parameters
        ----------
        kind
            One of `ENTRY_ORDER`.

        Returns
        -------
        list
            The entries of that type, sorted by `sort_key`.
        """
        return sorted((e for e in self.entries if isinstance(e, kind)), key=sort_key)


def _line(text: str) -> str:
    """Make a value fit on one line, escaping non-printable characters.

    Parameters
    ----------
    text
        Any text from a file or the instance.

    Returns
    -------
    str
        The text with each non-printable character ASCII-escaped; never cut.
    """
    return "".join(c if c.isprintable() else ascii(c)[1:-1] for c in text)


_TARGET_WORD = {
    SecurableKind.INSTANCE: "",
    SecurableKind.ENVIRONMENT: "environment ",
    SecurableKind.NAMESPACE: "namespace ",
    SecurableKind.TABLE: "table ",
}


def describe(entry: Entry) -> str:
    """Say an entry in one line, as a plan lists it.

    Parameters
    ----------
    entry
        Any entry.

    Returns
    -------
    str
        For example ``grant role ANALYST on environment prod to group:finance``.
    """
    match entry:
        case GroupEntry(name):
            return f"group {_line(name)}"
        case MembershipEntry(group, member):
            return f"member {_line(member)} of group {_line(group)}"
        case ServiceAccountEntry(name):
            return f"service account {_line(name)}"
        case GrantEntry(to, role, action, on):
            given = f"role {role}" if role else str(action)
            what = f"{given} on {_TARGET_WORD[on.kind]}{on}"
            return f"grant {what} to {_line(str(to))}"
        case RowFilterEntry(to, table, expression):
            return f"row filter on {_line(str(table))} for {_line(str(to))}: {_line(expression)}"
        case ColumnMaskEntry(to, table, column, expression):
            return (
                f"column mask on {_line(str(table))}.{_line(column)} for {_line(str(to))}: "
                f"{_line(expression)}"
            )
        case _:
            details = [
                ", ".join(f"{_line(k)}={_line(v)}" for k, v in entry.conditions),
                "+".join(entry.kinds),
            ]
            if not entry.enabled:
                details.append("disabled")
            if entry.start_on_deploy_jobs:
                details.append("starts on-deploy jobs")
            return (
                f"CI trust {_line(entry.issuer)} {_line(entry.repository)} → "
                f"service:{_line(entry.as_)} on {_line(entry.on)} ({'; '.join(details)})"
            )


def fingerprint(state: AccessState, managed: Sequence[tuple[str, str]] = ()) -> str:
    """Hash a state and the managed-scope records, independent of order.

    Parameters
    ----------
    state
        The live entries of the scopes a plan covers.
    managed
        ``(scope, file_sha256)`` of each managed scope (empty before apply records any).

    Returns
    -------
    str
        SHA-256, hex.
    """
    payload = {
        "entries": sorted(json.dumps(entry_json(e), sort_keys=True) for e in state.entries),
        "managed": sorted(managed),
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()
