"""Check the entries of an access file against the instance, every problem at once (#46).

Every entry's subjects are checked (users, groups, service accounts). An entry the apply would
create or change is also checked by the very method its single route runs before writing
(`check_target`, `check_rule`), so a plan refuses what the route would refuse. An entry that is
already live is not validated again: it was accepted when it was made, and checking it against
today's columns would refuse an unedited export.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.access.file import AccessFile
from lakana_core.access.live import LiveAccess
from lakana_core.access.model import (
    CiTrustEntry,
    ColumnMaskEntry,
    Entry,
    GrantEntry,
    GroupEntry,
    MembershipEntry,
    RowFilterEntry,
    Scopes,
    ServiceAccountEntry,
    Subject,
    Target,
)
from lakana_core.auth import Action, PrincipalRef
from lakana_core.ci_oidc.rules import CiTrustRuleService, resolve_issuer
from lakana_core.errors import LakanaError, NotFoundError, ValidationError
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import Group, Principal, PrincipalType
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.expressions import normalized_expression
from lakana_core.policy.row_filters import RowFilterService

_USER_STEP = "check the name with lakana principal list, or let the person sign in first"


def normalized_entry(entry: Entry) -> Entry:
    """Spell a policy's expression one way, so two spellings of one policy are one entry.

    Parameters
    ----------
    entry
        Any entry.

    Returns
    -------
    Entry
        A row filter or column mask with `normalized_expression`; any other entry unchanged.
    """
    if isinstance(entry, RowFilterEntry | ColumnMaskEntry):
        return dataclasses.replace(entry, expression=normalized_expression(entry.expression))
    return entry


@dataclasses.dataclass(frozen=True)
class _Services:
    """The services whose checks a planned create or change must pass.

    Parameters
    ----------
    grants
        Checks grants.
    row_filters
        Checks row filters.
    column_masks
        Checks column masks.
    ci_rules
        Checks CI trust rules.
    p
        The acting principal, already authorized.
    """

    grants: GrantService
    row_filters: RowFilterService
    column_masks: ColumnMaskService
    ci_rules: CiTrustRuleService
    p: PrincipalRef

    def check(self, entry: Entry) -> None:
        """Run the check of an entry's single route.

        Parameters
        ----------
        entry
            A grant, policy or CI trust rule; other entries have no check of their own.
        """
        match entry:
            case GrantEntry(role=role, action=action, on=on):
                self.grants.check_target(
                    role=Role(role) if role else None,
                    action=Action(action) if action else None,
                    kind=on.kind,
                    environment=on.environment,
                    namespace=on.namespace_joined,
                    table=on.table,
                )
            case RowFilterEntry(table=table, expression=expression):
                self.row_filters.check_target(self.p, **_table(table), expression=expression)
            case ColumnMaskEntry(table=table, column=column, expression=expression):
                self.column_masks.check_target(
                    self.p, **_table(table), column=column, expression=expression
                )
            case CiTrustEntry():
                self.ci_rules.check_rule(
                    issuer=entry.issuer,
                    environment=entry.on,
                    repository=entry.repository,
                    kinds=entry.kinds,
                    conditions=dict(entry.conditions),
                )


def _table(target: Target) -> dict[str, str]:
    """Give a policy's table as the policy services take it.

    Parameters
    ----------
    target
        A table target (the parser makes every policy's target one).

    Returns
    -------
    dict
        ``environment``, ``namespace`` (joined storage form) and ``table``.
    """
    return {
        "environment": target.environment or "",
        "namespace": target.namespace_joined or "",
        "table": target.table or "",
    }


class _References:
    """Who and what an entry may name: live principals and groups, or the file's own.

    Parameters
    ----------
    session
        Database session.
    parsed
        The file.
    scopes
        The scopes it manages. With ``instance`` the file owns every group and service account,
        so it must list those it names; otherwise they must exist.
    """

    def __init__(self, session: Session, parsed: AccessFile, scopes: Scopes) -> None:
        rows = session.execute(select(Principal.name, Principal.type, Principal.is_active)).all()
        self.kinds = {name: (kind, active) for name, kind, active in rows}
        self.instance = scopes.instance
        listed = [entry for _, entry in parsed.entries]
        self.file_services = {e.name for e in listed if isinstance(e, ServiceAccountEntry)}
        self.groups = (
            {e.name for e in listed if isinstance(e, GroupEntry)}
            if self.instance
            else set(session.scalars(select(Group.name)))
        )

    def _is_service(self, name: str) -> bool:
        """Say whether a name is a service account's, live or listed.

        Parameters
        ----------
        name
            A principal's name.

        Returns
        -------
        bool
            True for a service account of any state, or one the file lists.
        """
        found = self.kinds.get(name)
        return name in self.file_services or (
            found is not None and found[0] is PrincipalType.SERVICE_ACCOUNT
        )

    def subject(self, subject: Subject) -> str | None:
        """Check the subject of an entry.

        Parameters
        ----------
        subject
            ``user:``, ``group:`` or ``service:``.

        Returns
        -------
        str or None
            The problem, or `None`.
        """
        name = subject.name
        if subject.kind == "group":
            if name in self.groups:
                return None
            if self.instance:
                return f"no group {name} in the file; list it under groups"
            return (
                f"no group {name}; create it with lakana group create, or manage instance and "
                "list it under groups"
            )
        found = self.kinds.get(name)
        if subject.kind == "user":
            if found == (PrincipalType.USER, True):
                return None
            if self._is_service(name):
                return f"{name} is a service account; write service:{name}"
            return f"no active user {name}; {_USER_STEP}"
        if found is not None and found[0] is PrincipalType.USER:
            return f"{name} is a person; write user:{name}"
        if self.instance:
            if name in self.file_services:
                return None
            return f"no service account {name} in the file; list it under service_accounts"
        if found == (PrincipalType.SERVICE_ACCOUNT, True):
            return None
        return (
            f"no active service account {name}; create it with lakana principal create {name} "
            "--type service-account, or manage instance and list it under service_accounts"
        )

    def member(self, name: str) -> str | None:
        """Check a group member: an active person, or a service account the file lists.

        Parameters
        ----------
        name
            The member's name.

        Returns
        -------
        str or None
            The problem, or `None`.
        """
        kind = "service" if self._is_service(name) else "user"
        return self.subject(Subject(kind, name))

    def account(self, name: str) -> str | None:
        """Check a service account the file lists: a new name, or an active account's.

        Parameters
        ----------
        name
            The account's name.

        Returns
        -------
        str or None
            The problem, or `None`.
        """
        found = self.kinds.get(name)
        if found is None or found == (PrincipalType.SERVICE_ACCOUNT, True):
            return None
        if found[0] is PrincipalType.USER:
            return f"{name} is a person; choose another name for the service account"
        return (
            f"service account {name} exists and is deactivated; a name cannot be used again, "
            "choose another"
        )

    def problem(self, entry: Entry) -> str | None:
        """Check every name an entry gives access to.

        Parameters
        ----------
        entry
            Any entry.

        Returns
        -------
        str or None
            The problem, or `None`.
        """
        match entry:
            case GrantEntry(to=to) | RowFilterEntry(to=to) | ColumnMaskEntry(to=to):
                return self.subject(to)
            case CiTrustEntry(as_=account):
                return self.subject(Subject("service", account))
            case MembershipEntry(member=member):
                return self.member(member)
            case ServiceAccountEntry(name=name):
                return self.account(name)
            case _:
                return None


def _where(parsed: AccessFile, path: str) -> str:
    """Start a problem with the file, the entry and its line.

    Parameters
    ----------
    parsed
        The file.
    path
        The entry's path.

    Returns
    -------
    str
        ``access.yaml: grants[3] (line 14)``.
    """
    line = parsed.line_of(path)
    return f"{parsed.file_name}: {path} (line {line})" if line else f"{parsed.file_name}: {path}"


def check_entries(
    parsed: AccessFile,
    live: LiveAccess,
    scopes: Scopes,
    *,
    grants: GrantService,
    row_filters: RowFilterService,
    column_masks: ColumnMaskService,
    ci_rules: CiTrustRuleService,
    session: Session,
    p: PrincipalRef,
    issuers: Sequence[str],
) -> tuple[AccessFile, list[str]]:
    """Check every entry of a file and bring it into the form the live state is compared in.

    Parameters
    ----------
    parsed
        The parsed file.
    live
        The live facts, with normalized expressions (`normalized_entry`).
    scopes
        The scopes the file manages.
    grants
        Checks grants the plan creates.
    row_filters
        Checks row filters the plan creates.
    column_masks
        Checks column masks the plan creates.
    ci_rules
        Checks CI trust rules the plan creates or changes.
    session
        Database session, to look up principals and groups.
    p
        The acting principal, already authorized.
    issuers
        The configured CI OIDC issuers.

    Returns
    -------
    tuple of (AccessFile, list of str)
        The file with each CI issuer resolved to its URL, each expression normalized and each
        policy written in the stored form of a live policy shown under a renamed column (#120)
        replaced by that policy's shown form; and every problem, each naming the file, the
        entry, its line, the cause and the next step.
    """
    references = _References(session, parsed, scopes)
    services = _Services(grants, row_filters, column_masks, ci_rules, p)
    shown = {
        form: entry
        for entry, forms in live.stored.items()
        for form in forms
        if form not in live.state.entries
    }
    problems: list[str] = []
    entries: list[tuple[str, Entry]] = []
    seen: dict[object, str] = {}
    for path, written in parsed.entries:
        entry = normalized_entry(written)
        entry = shown.get(entry, entry)
        checkable = entry not in live.state.entries
        if isinstance(entry, CiTrustEntry) and checkable:
            try:
                entry = dataclasses.replace(entry, issuer=resolve_issuer(entry.issuer, issuers))
            except ValidationError as exc:
                problems.append(f"{_where(parsed, path)}: {exc}")
                checkable = False
            else:
                checkable = entry not in live.state.entries
        identity = entry.key if isinstance(entry, CiTrustEntry) else entry
        first = seen.setdefault(identity, path)
        if first != path:
            problems.append(f"{_where(parsed, path)}: repeats {first}; remove one")
            continue
        problem = references.problem(entry)
        if problem is not None:
            problems.append(f"{_where(parsed, path)}: {problem}")
        if checkable:
            try:
                services.check(entry)
            except NotFoundError as exc:
                problems.append(f"{_where(parsed, path)}: {exc} Check the name in the file.")
            except LakanaError as exc:
                problems.append(f"{_where(parsed, path)}: {exc}")
        entries.append((path, entry))
    return dataclasses.replace(parsed, entries=tuple(entries)), problems
