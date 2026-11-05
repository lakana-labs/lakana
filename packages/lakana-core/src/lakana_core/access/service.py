"""Export and plan access files; from PR 2 on, apply them and report managed scopes (#46).

Every public method needs the instance `ADMIN` role (`MANAGE_GRANT` through
`authorize_admin_only`) plus `MANAGE_POLICY` and `MANAGE_PRINCIPAL`, checked before the file is
parsed or the live state read: a file can name any access there is, so only someone who could
make every change in it one route at a time may see or plan it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from functools import partial

from sqlalchemy.orm import Session

from lakana_core.access.checks import check_entries, normalized_entry
from lakana_core.access.export import export_document
from lakana_core.access.file import (
    AccessFile,
    AccessFileError,
    parse_access_file,
    validate_file_name,
)
from lakana_core.access.live import LiveAccess, read_live
from lakana_core.access.lockout import admins_after, lockout_problems
from lakana_core.access.model import INSTANCE, AccessState, Scopes, ServiceAccountEntry, fingerprint
from lakana_core.access.plan import AccessPlan, PlannedChange, diff, drift
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.ci_oidc.rules import CiTrustRuleService
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.grants.admin import authorize_admin_only
from lakana_core.grants.service import GrantService
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.columns import ColumnSource
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.projects.lookups import environment_by_name


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
            that does not exist.
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
            For scopes `_export_scopes` refuses.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GRANT)
        self._authorize(p)
        wanted = self._export_scopes(scopes)
        return export_document(self._live(p).state, wanted)

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
        validate_file_name(file_name)
        try:
            parsed = parse_access_file(text, file_name=file_name)
        except AccessFileError as exc:
            sha256 = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
            nothing = fingerprint(AccessState(frozenset()))
            return AccessPlan(file_name, sha256, (), (), (), (), exc.problems, nothing)
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
        problems = self._missing_environments(parsed)
        live = self._live(p)
        desired, found = check_entries(
            parsed,
            live,
            scopes,
            grants=GrantService(self._session, self._authz),
            row_filters=RowFilterService(self._session, self._authz, self._catalog),
            column_masks=ColumnMaskService(self._session, self._authz, self._catalog),
            ci_rules=CiTrustRuleService(self._session, self._authz, issuers=self._issuers),
            session=self._session,
            p=p,
            issuers=self._issuers,
        )
        problems += found
        changes = diff(desired, live, scopes)
        after = admins_after(
            AccessState(live.state.outside(scopes).entries | desired.state.entries),
            _active_after(live, changes),
        )
        problems += lockout_problems(
            file_name=parsed.file_name, after=after, caller=p.name, allow_lockout=allow_lockout
        )
        # PR 2 records managed scopes; until then no scope is managed and nothing is released.
        return AccessPlan(
            file_name=parsed.file_name,
            file_sha256=parsed.sha256,
            manages=parsed.manages,
            changes=changes,
            releases=(),
            drift=drift(changes, scopes, frozenset()),
            problems=tuple(problems),
            fingerprint=fingerprint(live.state.owned(scopes)),
        )


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
        The active principals, without the service accounts removed and with those created.
    """
    removed: set[str] = set()
    created: set[str] = set()
    for change in changes:
        if isinstance(change.entry, ServiceAccountEntry):
            (removed if change.op == "remove" else created).add(change.entry.name)
    return frozenset((live.active - removed) | created)
