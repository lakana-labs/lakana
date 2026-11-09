"""Export, plan and apply access files, and report the scopes files manage (#46).

Every public method needs the instance `ADMIN` role (`MANAGE_GRANT` through
`authorize_admin_only`) plus `MANAGE_POLICY` and `MANAGE_PRINCIPAL`, checked before the file is
parsed or the live state read: a file can name any access there is, so only someone who could
make every change in it one route at a time may see or plan it.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from pyiceberg.types import NestedField
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.access.apply import AccessApplier
from lakana_core.access.checks import check_entries, normalized_entry
from lakana_core.access.export import export_document
from lakana_core.access.file import (
    AccessFile,
    AccessFileError,
    cap_problems,
    parse_access_file,
    validate_file_name,
)
from lakana_core.access.live import LiveAccess, read_live
from lakana_core.access.lockout import admins_after, lockout_problems
from lakana_core.access.managed import applying, finish_apply, lock_access
from lakana_core.access.model import (
    INSTANCE,
    AccessState,
    Scopes,
    ServiceAccountEntry,
    Subject,
    fingerprint,
    unwritable_environments,
)
from lakana_core.access.plan import AccessPlan, Drift, PlannedChange, diff, drift
from lakana_core.audit.authorization import record_deferred_denials
from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.ci_oidc.rules import CiTrustRuleService
from lakana_core.errors import ConflictError, NotFoundError, ValidationError
from lakana_core.grants.admin import authorize_admin_only
from lakana_core.grants.service import GrantService, active_instance_admin_ids
from lakana_core.identifiers import TableRef, environment_name_problem
from lakana_core.identity.sessions import require_step_up
from lakana_core.models import (
    AccessManagedScope,
    AccessOverride,
    AuditEventType,
    Principal,
    PrincipalType,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.columns import ColumnSource
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.projects.lookups import environment_by_name

if TYPE_CHECKING:
    from lakana_core.catalog.service import TableDescription

_COMMIT = re.compile(r"[0-9a-f]{7,64}")
"""A commit hash: 7 to 64 hex digits (SHA-1 or SHA-256, full or abbreviated)."""


@dataclass(frozen=True)
class AccessApplyResult:
    """What an apply did.

    Parameters
    ----------
    apply_id
        The apply's id; every audit event of the apply carries it as ``access_apply_id``.
    file_sha256
        Hash of the file's text.
    source_commit
        The commit the file came from, lower case; `None` when not given.
    created
        How many entries were created.
    changed
        How many CI trust rules were changed in place.
    removed
        How many entries were removed.
    released
        The scopes no file manages any longer.
    applied_at
        When the apply committed.
    """

    apply_id: uuid.UUID
    file_sha256: str
    source_commit: str | None
    created: int
    changed: int
    removed: int
    released: tuple[str, ...]
    applied_at: datetime


@dataclass(frozen=True)
class ManagedScopeView:
    """A scope an access file manages, and its last apply.

    Parameters
    ----------
    scope
        ``instance`` or an environment's name.
    file_name
        The name of the file that last applied it.
    file_sha256
        That file's hash.
    source_commit
        The commit the file came from, when given.
    applied_by
        Who applied it; `None` once that principal is deleted.
    applied_at
        When.
    """

    scope: str
    file_name: str
    file_sha256: str
    source_commit: str | None
    applied_by: str | None
    applied_at: datetime


@dataclass(frozen=True)
class OverrideView:
    """A change made by hand in a managed scope that no apply has taken into account yet.

    Parameters
    ----------
    scope
        The managed scope.
    change
        What was changed.
    reason
        Why.
    by
        Who changed it; `None` once that principal is deleted.
    at
        When.
    """

    scope: str
    change: str
    reason: str
    by: str | None
    at: datetime


@dataclass(frozen=True)
class AccessStatus:
    """The scopes access files manage, and the open overrides.

    Parameters
    ----------
    scopes
        Every managed scope, sorted by name.
    overrides
        Every open override, oldest first.
    """

    scopes: tuple[ManagedScopeView, ...]
    overrides: tuple[OverrideView, ...]


def _valid_commit(source_commit: str | None) -> str | None:
    """Check the commit an apply names.

    Parameters
    ----------
    source_commit
        The commit, as the caller gives it.

    Returns
    -------
    str or None
        The commit in lower case; `None` when not given.

    Raises
    ------
    ValidationError
        When it is not 7 to 64 hex digits.
    """
    if source_commit is None:
        return None
    commit = source_commit.lower()
    if _COMMIT.fullmatch(commit) is None:
        raise ValidationError(
            "--source-commit takes a commit hash of 7 to 64 hex digits, such as the output of "
            "git rev-parse HEAD; leave it out when the file is not in git"
        )
    return commit


def _caller(p: PrincipalRef) -> Subject:
    """Give the acting principal as a subject.

    Parameters
    ----------
    p
        The acting principal.

    Returns
    -------
    Subject
        ``service:`` for a service account, else ``user:``.
    """
    return Subject("service" if p.type is PrincipalType.SERVICE_ACCOUNT else "user", p.name)


class _PlanColumns:
    """A column source that reads each table once for the length of one plan.

    A planned policy create is checked twice (`validate_for_table`, then the column check), and
    a file can hold many policies on one table; each would read the table's metadata again.

    Parameters
    ----------
    source
        The real column source.
    """

    def __init__(self, source: ColumnSource) -> None:
        self._source = source
        self._described: dict[TableRef, TableDescription] = {}

    def describe_table(self, p: PrincipalRef, ref: TableRef) -> TableDescription:
        """Describe a table, reading it from the source the first time only.

        Parameters
        ----------
        p
            The acting principal; one principal makes the whole plan.
        ref
            The table reference.

        Returns
        -------
        TableDescription
            The table, including its columns.
        """
        if ref not in self._described:
            self._described[ref] = self._source.describe_table(p, ref)
        return self._described[ref]

    def policy_fields(self, p: PrincipalRef, table_id: uuid.UUID) -> tuple[NestedField, ...] | None:
        """Read a table's current top-level fields.

        Parameters
        ----------
        p
            The acting principal.
        table_id
            The table's id.

        Returns
        -------
        tuple of NestedField or None
            The fields; `None` when there are none to read.
        """
        return self._source.policy_fields(p, table_id)


class AccessService:
    """Export, plan (and, from PR 2, apply) access files; status of managed scopes.

    Parameters
    ----------
    session
        Database session.
    authz
        The request's authorization service.
    catalog
        The column source for policy checks and current column names (#120).
    issuers
        The configured CI OIDC issuers.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        catalog: ColumnSource,
        *,
        issuers: Sequence[str],
    ) -> None:
        self._session = session
        self._authz = authz
        self._catalog = catalog
        self._issuers = list(issuers)

    def _authorize(self, p: PrincipalRef) -> None:
        """Require the actions an apply uses besides MANAGE_GRANT.

        Each public method first calls `authorize_admin_only` for MANAGE_GRANT in its own body
        (the call-site test requires it there), then this. The ADMIN role holds MANAGE_GROUP and
        MANAGE_TOKEN too, and a task or CI token is refused by the first check: their scopes
        narrow every check. Authorizing everything first means no service call inside an apply
        can be denied half-way.

        Parameters
        ----------
        p
            The acting principal.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_POLICY` or `MANAGE_PRINCIPAL` on the instance.
        """
        self._authz.authorize(p, Action.MANAGE_POLICY, Securable.instance_scope())
        self._authz.authorize(p, Action.MANAGE_PRINCIPAL, Securable.instance_scope())

    def _live(self, p: PrincipalRef) -> LiveAccess:
        """Read the live facts with every policy expression spelled one way.

        Parameters
        ----------
        p
            The acting principal, already authorized (it reads the tables' current columns).

        Returns
        -------
        LiveAccess
            The live facts after `normalized_entry`, ids of copies made equal merged.
        """
        return read_live(self._session, partial(self._catalog.policy_fields, p)).mapped(
            normalized_entry
        )

    def _export_scopes(self, scopes: Sequence[str]) -> Scopes:
        """Check the scopes an export names.

        Parameters
        ----------
        scopes
            ``instance`` alone, or environment names.

        Returns
        -------
        Scopes
            The scopes.

        Raises
        ------
        ValidationError
            Without a scope, with ``instance`` beside another scope, or naming an environment
            that does not exist or whose name no access file can write.
        """
        names = frozenset(scopes)
        if not names:
            raise ValidationError(
                "name at least one scope to export: instance, or one or more environments"
            )
        if INSTANCE in names and len(names) > 1:
            raise ValidationError(
                "instance already covers every environment; export instance alone, or list "
                "environments only"
            )
        for name in sorted(names - {INSTANCE}):
            try:
                environment_by_name(self._session, name)
            except NotFoundError as exc:
                raise ValidationError(f"no environment {name}; lakana env list shows them") from exc
            if (problem := environment_name_problem(name)) is not None:
                raise ValidationError(
                    f"environment '{name}' cannot be exported: its name {problem[0]}. Create the "
                    "environment again under another name with lakana env create"
                )
        return Scopes(names)

    def export(self, p: PrincipalRef, scopes: Sequence[str]) -> str:
        """Write the live access of some scopes as an access file.

        Parameters
        ----------
        p
            The acting principal.
        scopes
            ``instance`` alone, or environment names.

        Returns
        -------
        str
            The file's text; planning it unedited plans nothing.

        Raises
        ------
        PermissionDenied
            Without the instance `ADMIN` role, `MANAGE_POLICY` or `MANAGE_PRINCIPAL`.
        ValidationError
            For scopes `_export_scopes` refuses, or live access on an environment whose name
            no access file can write (`unwritable_environments`).
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GRANT)
        self._authorize(p)
        wanted = self._export_scopes(scopes)
        state = self._live(p).state
        if unwritable := unwritable_environments(state.owned(wanted)):
            raise ValidationError("; ".join(unwritable))
        return export_document(state, wanted)

    def plan(
        self,
        p: PrincipalRef,
        text: str,
        *,
        file_name: str = "access.yaml",
        allow_lockout: bool = False,
    ) -> AccessPlan:
        """Say what applying a file would change, and every problem that stops it.

        Parameters
        ----------
        p
            The acting principal.
        text
            The file's content.
        file_name
            Its base name, for the messages.
        allow_lockout
            Accept a file that drops the caller's own ADMIN role while another administrator
            remains.

        Returns
        -------
        AccessPlan
            The changes, drift and problems; a file that does not parse gives only its
            problems.

        Raises
        ------
        PermissionDenied
            Without the instance `ADMIN` role, `MANAGE_POLICY` or `MANAGE_PRINCIPAL`; before the
            file is read.
        ValidationError
            If `file_name` is not a plain base name.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GRANT)
        self._authorize(p)
        return self._plan_text(p, text, file_name=file_name, allow_lockout=allow_lockout)

    def _plan_text(
        self, p: PrincipalRef, text: str, *, file_name: str, allow_lockout: bool
    ) -> AccessPlan:
        """Parse and plan a file; the caller has authorized.

        Parameters
        ----------
        p
            The acting principal.
        text
            The file's content.
        file_name
            Its base name.
        allow_lockout
            Accept a file that drops the caller's own ADMIN role.

        Returns
        -------
        AccessPlan
            The plan; a file that does not parse gives only its problems.
        """
        validate_file_name(file_name)
        try:
            parsed = parse_access_file(text, file_name=file_name)
        except AccessFileError as exc:
            sha256 = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
            nothing = fingerprint(AccessState(frozenset()))
            return AccessPlan(file_name, sha256, (), (), (), (), (), exc.problems, nothing)
        return self._plan(p, parsed, allow_lockout=allow_lockout)

    def _missing_environments(self, parsed: AccessFile) -> list[str]:
        """Name each environment the file manages that does not exist.

        Parameters
        ----------
        parsed
            The file.

        Returns
        -------
        list of str
            One problem per missing environment.
        """
        problems = []
        for name in parsed.manages:
            if name == INSTANCE:
                continue
            try:
                environment_by_name(self._session, name)
            except NotFoundError:
                problems.append(
                    f"{parsed.file_name}: manages (line {parsed.line_of('manages')}): no "
                    f"environment {name}; create it with lakana env create, or remove it from "
                    "manages"
                )
        return problems

    def _plan(self, p: PrincipalRef, parsed: AccessFile, *, allow_lockout: bool) -> AccessPlan:
        """Plan a parsed file; the caller has authorized.

        Parameters
        ----------
        p
            The acting principal.
        parsed
            The file.
        allow_lockout
            Accept a file that drops the caller's own ADMIN role.

        Returns
        -------
        AccessPlan
            The plan.
        """
        scopes = Scopes(frozenset(parsed.manages))
        managed = {
            row.scope: row.file_sha256 for row in self._session.scalars(select(AccessManagedScope))
        }
        columns = _PlanColumns(self._catalog)
        problems = self._missing_environments(parsed)
        live = self._live(p)
        desired, found = check_entries(
            parsed,
            live,
            scopes,
            grants=GrantService(self._session, self._authz),
            row_filters=RowFilterService(self._session, self._authz, columns),
            column_masks=ColumnMaskService(self._session, self._authz, columns),
            ci_rules=CiTrustRuleService(self._session, self._authz, issuers=self._issuers),
            session=self._session,
            p=p,
            issuers=self._issuers,
        )
        problems += found
        problems += [
            f"{parsed.file_name}: {problem}"
            for problem in unwritable_environments(live.state.owned(scopes))
        ]
        changes = diff(desired, live, scopes)
        after = admins_after(
            AccessState(live.state.outside(scopes).entries | desired.state.entries),
            _active_after(live, changes),
        )
        problems += lockout_problems(
            file_name=parsed.file_name,
            after=after,
            caller=_caller(p),
            allow_lockout=allow_lockout,
        )
        if any(c.op == "remove" and c.entry == ServiceAccountEntry(p.name) for c in changes):
            problems.append(
                f"{parsed.file_name}: service_accounts: the file removes service:{p.name}, the "
                "account applying it, which would end its access part-way through the apply; "
                "have another administrator apply this file"
            )
        covered = scopes.union(Scopes(frozenset(managed)))
        return AccessPlan(
            file_name=parsed.file_name,
            file_sha256=parsed.sha256,
            manages=parsed.manages,
            changes=changes,
            releases=tuple(
                sorted(n for n in managed if n not in scopes.names and not _covered(n, scopes))
            ),
            starts=tuple(
                sorted(n for n in scopes.names if n not in managed and not _under(n, managed))
            ),
            drift=drift(changes, scopes, frozenset(managed)) + self._override_drift(covered),
            problems=tuple(cap_problems(parsed.file_name, problems)),
            fingerprint=fingerprint(
                live.state.owned(covered), sorted(managed.items()), parsed.sha256
            ),
        )

    def _names(self, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
        """Name some principals.

        Parameters
        ----------
        ids
            Their ids; `None` is ignored.

        Returns
        -------
        dict
            Name by id, for the principals that exist.
        """
        found = select(Principal.id, Principal.name).where(Principal.id.in_(ids - {None}))
        return {row.id: row.name for row in self._session.execute(found)}

    def _open_overrides(self) -> list[AccessOverride]:
        """List the overrides no apply has taken into account yet.

        Returns
        -------
        list of AccessOverride
            Oldest first.
        """
        found = select(AccessOverride).where(AccessOverride.resolved_at.is_(None))
        return list(self._session.scalars(found.order_by(AccessOverride.created_at)))

    def _override_drift(self, scopes: Scopes) -> tuple[Drift, ...]:
        """Name the open overrides an apply of the file would resolve.

        Parameters
        ----------
        scopes
            The scopes the file manages and those managed before it.

        Returns
        -------
        tuple of Drift
            One per open override, oldest first; the next apply reverts each unless the file
            now lists it.
        """
        rows = [row for row in self._open_overrides() if row.scope in scopes.names]
        names = self._names({row.principal_id for row in rows})
        return tuple(
            Drift(
                row.scope,
                f"{row.change} (override)",
                names.get(row.principal_id),
                row.reason,
                row.created_at,
            )
            for row in rows
        )

    def apply(
        self,
        p: PrincipalRef,
        text: str,
        *,
        file_name: str = "access.yaml",
        plan_fingerprint: str,
        source_commit: str | None = None,
        allow_lockout: bool = False,
    ) -> AccessApplyResult:
        """Make a file's changes in one audited transaction, or none of them.

        The plan is made again under the exclusive access lock: what the caller saw is never
        trusted, only compared through its fingerprint.

        Parameters
        ----------
        p
            The acting principal.
        text
            The file's content.
        file_name
            Its base name, for the messages and the record.
        plan_fingerprint
            The fingerprint of the plan the caller reviewed.
        source_commit
            The commit the file came from.
        allow_lockout
            Accept a file that drops the caller's own ADMIN role while another administrator
            remains.

        Returns
        -------
        AccessApplyResult
            The apply's id and counts.

        Raises
        ------
        PermissionDenied
            Without the instance `ADMIN` role, `MANAGE_POLICY` or `MANAGE_PRINCIPAL`.
        ValidationError
            For a bad `file_name` or `source_commit`.
        ConflictError
            When access changed since the plan, or the changes as made leave no active
            administrator or drop the caller's own ADMIN role.
        AccessFileError
            When the plan has problems; every one is listed.
        StepUpRequired
            In a browser session whose last passkey touch is older than 15 minutes.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GRANT)
        self._authorize(p)
        require_step_up(p)
        commit = _valid_commit(source_commit)
        try:
            lock_access(self._session, shared=False)
            plan = self._plan_text(p, text, file_name=file_name, allow_lockout=allow_lockout)
            if plan.fingerprint != plan_fingerprint:
                raise ConflictError(
                    "the file or the access changed since your plan (fingerprint "
                    f"{plan_fingerprint[:12]}…); nothing was applied. Run lakana access plan "
                    "again and review the changes"
                )
            if plan.problems:
                raise AccessFileError(plan.problems)
            apply_id = uuid.uuid4()
            with applying(self._session, apply_id):
                applier = AccessApplier(
                    self._session, self._authz, self._catalog, issuers=self._issuers
                )
                applier.run(p, plan.changes)
                self._session.flush()
                self._recheck_lockout(p, plan, allow_lockout=allow_lockout)
                result = self._record(p, plan, apply_id, commit)
                finish_apply(self._session)
            record_deferred_denials(self._session)
        except BaseException:
            self._session.rollback()
            record_deferred_denials(self._session)
            raise
        return result

    def _recheck_lockout(self, p: PrincipalRef, plan: AccessPlan, *, allow_lockout: bool) -> None:
        """Check the administrators the changes as made leave, inside the transaction.

        Parameters
        ----------
        p
            The acting principal.
        plan
            The plan just made; a service account it creates never counts.
        allow_lockout
            Whether the caller accepts losing their own ADMIN role.

        Raises
        ------
        ConflictError
            When no active administrator remains, or the caller's own role goes without
            `allow_lockout`.
        """
        created = {
            change.entry.name
            for change in plan.creates
            if isinstance(change.entry, ServiceAccountEntry)
        }
        names = self._names(set(active_instance_admin_ids(self._session)))
        problems = lockout_problems(
            file_name=plan.file_name,
            after=frozenset(names.values()) - created,
            caller=_caller(p),
            allow_lockout=allow_lockout,
        )
        if problems:
            raise ConflictError("\n".join(problems))

    def _record(
        self, p: PrincipalRef, plan: AccessPlan, apply_id: uuid.UUID, commit: str | None
    ) -> AccessApplyResult:
        """Record the managed scopes, resolve their overrides and audit the apply.

        Parameters
        ----------
        p
            The acting principal.
        plan
            The plan applied.
        apply_id
            The apply's id.
        commit
            The checked source commit.

        Returns
        -------
        AccessApplyResult
            The result.
        """
        now = datetime.now(UTC)
        for scope in plan.manages:
            row = self._session.get(AccessManagedScope, scope) or AccessManagedScope(scope=scope)
            row.file_sha256 = plan.file_sha256
            row.file_name = plan.file_name
            row.source_commit = commit
            row.apply_id = apply_id
            row.applied_by = p.id
            row.applied_at = now
            self._session.add(row)
        touched = set(plan.manages)
        for row in self._session.scalars(select(AccessManagedScope)).all():
            touched.add(row.scope)
            if row.scope not in plan.manages:
                self._session.delete(row)
        for override in self._open_overrides():
            if override.scope in touched:
                override.resolved_at = now
                override.resolved_by_apply = apply_id
        result = AccessApplyResult(
            apply_id=apply_id,
            file_sha256=plan.file_sha256,
            source_commit=commit,
            created=len(plan.creates),
            changed=len(plan.changed),
            removed=len(plan.removals),
            released=plan.releases,
            applied_at=now,
        )
        AuditService(self._session).record(
            p.id,
            AuditEventType.ACCESS_APPLIED,
            securable=Securable.instance_scope(),
            detail={
                "file_name": plan.file_name,
                "file_sha256": plan.file_sha256,
                "source_commit": commit,
                "created": result.created,
                "changed": result.changed,
                "removed": result.removed,
                "scopes": list(plan.manages),
                "released": list(plan.releases),
            },
        )
        return result

    def status(self, p: PrincipalRef) -> AccessStatus:
        """Report the scopes access files manage and the open overrides.

        Parameters
        ----------
        p
            The acting principal.

        Returns
        -------
        AccessStatus
            The scopes and overrides.

        Raises
        ------
        PermissionDenied
            Without the instance `ADMIN` role.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GRANT)
        rows = list(
            self._session.scalars(select(AccessManagedScope).order_by(AccessManagedScope.scope))
        )
        overrides = self._open_overrides()
        names = self._names(
            {row.applied_by for row in rows} | {row.principal_id for row in overrides}
        )
        return AccessStatus(
            scopes=tuple(
                ManagedScopeView(
                    row.scope,
                    row.file_name,
                    row.file_sha256,
                    row.source_commit,
                    names.get(row.applied_by) if row.applied_by else None,
                    row.applied_at,
                )
                for row in rows
            ),
            overrides=tuple(
                OverrideView(
                    row.scope, row.change, row.reason, names.get(row.principal_id), row.created_at
                )
                for row in overrides
            ),
        )


def _under(scope: str, managed: Mapping[str, str]) -> bool:
    """Say whether a managed ``instance`` already covers a scope.

    Parameters
    ----------
    scope
        A scope the file manages.
    managed
        The scopes managed before.

    Returns
    -------
    bool
        True for an environment under a managed ``instance``.
    """
    return scope != INSTANCE and INSTANCE in managed


def _covered(scope: str, scopes: Scopes) -> bool:
    """Say whether the file's scopes still cover a scope managed before.

    Parameters
    ----------
    scope
        A scope managed before.
    scopes
        The scopes the file manages.

    Returns
    -------
    bool
        True for an environment under a managed ``instance``.
    """
    return scope != INSTANCE and scopes.instance


def _active_after(live: LiveAccess, changes: Sequence[PlannedChange]) -> frozenset[str]:
    """Name the principals active after an apply.

    Parameters
    ----------
    live
        The live facts.
    changes
        The plan's changes.

    Returns
    -------
    frozenset of str
        The principals already active, without the service accounts the file removes. A service
        account the file creates is not counted: it has no token yet and nobody would be left to
        issue one.
    """
    removed = {
        change.entry.name
        for change in changes
        if change.op == "remove" and isinstance(change.entry, ServiceAccountEntry)
    }
    return frozenset(live.active - removed)
