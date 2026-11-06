"""Run an access apply in one transaction, and guard the scopes it manages (#46).

`applying` links every audit event of the apply's changes to one id and refuses any commit
until `finish_apply`, so a failed apply leaves nothing behind. `lock_access` serializes an
apply with the guarded changes that run beside it. `guard_change` is called by every access
service method that changes something an access file can own: in a scope a file manages it
refuses the change, unless an instance administrator overrides it with a reason, which is
audited and shown as drift until the next apply.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from lakana_core.access.model import INSTANCE
from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.service import ACCESS_APPLY_KEY, AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import ConflictError, PermissionDenied, ValidationError
from lakana_core.grants.admin import holds_instance_admin
from lakana_core.models import AccessManagedScope, AccessOverride, AuditEventType
from lakana_core.runs.lockdown import validate_break_glass_reason

ACCESS_LOCK_KEY = int.from_bytes(b"lkaccess", "big", signed=True)
"""The Postgres advisory lock key: apply exclusive, every guarded change shared."""

_FINAL = "lakana.access_apply_final"
_RECOVERY = "lakana.access_lockout_recovery"
_CHANGE_LENGTH = 255

RECOVERY_REASON = "Lockout recovery at server start: no active instance administrator"
"""The reason recorded for the one change the guard lets through without an override."""


class ManagedScopeError(ConflictError):
    """A change outside the access file in a scope the file manages (409)."""


@contextmanager
def applying(session: Session, apply_id: uuid.UUID) -> Generator[None]:
    """Run an apply's changes in the session's one transaction, linked to the apply.

    Parameters
    ----------
    session
        The request's session.
    apply_id
        The apply's id; `AuditService.record` adds it to every event as `access_apply_id`.

    Yields
    ------
    None
        While the changes run; a commit other than `finish_apply`'s raises.
    """

    def refuse(_: Session) -> None:
        """Refuse any commit before `finish_apply`.

        Parameters
        ----------
        _
            The committing session.

        Raises
        ------
        RuntimeError
            Always, unless `finish_apply` is committing.
        """
        if not session.info.get(_FINAL):
            raise RuntimeError("an access apply commits once, at its end")

    session.info.pop(_FINAL, None)
    session.info[ACCESS_APPLY_KEY] = apply_id
    event.listen(session, "before_commit", refuse)
    try:
        yield
    finally:
        event.remove(session, "before_commit", refuse)
        session.info.pop(ACCESS_APPLY_KEY, None)
        session.info.pop(_FINAL, None)


def finish_apply(session: Session) -> None:
    """Commit an apply: the one commit `applying` allows.

    Parameters
    ----------
    session
        The session inside `applying`.

    Raises
    ------
    RuntimeError
        Outside `applying`, where the flag would let a later apply commit half-way.
    """
    if ACCESS_APPLY_KEY not in session.info:
        raise RuntimeError("finish_apply commits only inside an access apply")
    session.info[_FINAL] = True
    session.commit()


def lock_access(session: Session, *, shared: bool) -> None:
    """Take the access advisory lock until the transaction ends (Postgres only).

    Parameters
    ----------
    session
        The request's session.
    shared
        `True` for a guarded change, which can run beside others; `False` for an apply,
        which runs alone.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    session.execute(text(f"SELECT {function}(:key)"), {"key": ACCESS_LOCK_KEY})


@contextmanager
def lockout_recovery(session: Session) -> Generator[None]:
    """Let the server's lockout recovery pass the guard: the one exemption.

    `lakana_core.bootstrap.bootstrap_if_needed` grants the bootstrap principal ADMIN when no
    active administrator exists. Recovering from a lockout must never depend on the access
    file, so inside this block `guard_change` lets a change in a managed scope through. It still
    records it, as `MANAGED_SCOPE_OVERRIDDEN` with `RECOVERY_REASON` and an open override, so
    the next plan shows the grant as drift.

    Parameters
    ----------
    session
        The bootstrap's session.

    Yields
    ------
    None
        While the recovery grant is made.
    """
    session.info[_RECOVERY] = True
    try:
        yield
    finally:
        session.info.pop(_RECOVERY, None)


def guard_change(
    session: Session,
    p: PrincipalRef,
    *,
    environment: str | None,
    change: str,
    authz: AuthorizationService | None = None,
    action: Action | None = None,
) -> None:
    """Refuse a change outside the access file in a scope a file manages, or record its override.

    Called by each guarded service method right after its own authorization. Inside an apply
    it does nothing: the apply holds the exclusive lock and is the file. Otherwise it takes the
    shared access lock, so the change waits for a running apply.

    Parameters
    ----------
    session
        The request's session; the override's event and row join its transaction.
    p
        The acting principal; `p.managed_override_reason` asks for an override.
    environment
        The environment the change is in, managed by a file that lists it or `instance`; or
        `None` for an instance-wide object (a group, a membership, a service account, an
        instance grant), which only `instance` manages.
    change
        What is changed, for the audit log and the drift; cut to 255 characters.
    authz
        The calling service's authorization, to record a refused override as
        `AUTHORIZATION_DENIED` (through its limiter). `None` where the caller's own check
        already asked for the `ADMIN` role, so the override cannot be refused for lack of it.
    action
        The action the calling service authorized, recorded with a refused override.

    Raises
    ------
    ManagedScopeError
        If a file manages the scope and no override is asked for.
    PermissionDenied
        If an override is asked for by a principal without the `ADMIN` role on the instance.
    ValidationError
        If an override is asked for in a scope no file manages, or its reason is not 10 to 500
        characters on one line.
    """
    if ACCESS_APPLY_KEY in session.info:
        return
    lock_access(session, shared=True)
    managed = session.get(AccessManagedScope, environment) if environment is not None else None
    if managed is None:
        managed = session.get(AccessManagedScope, INSTANCE)
    scope = managed.scope if managed is not None else environment or INSTANCE
    reason = p.managed_override_reason
    if session.info.get(_RECOVERY):
        if managed is not None:
            _record_override(session, p, scope, change, RECOVERY_REASON)
        return
    if managed is None:
        if reason is not None:
            raise ValidationError(
                f"`{scope}` is not managed by an access file; repeat without `--override-managed`"
            )
        return
    if reason is None:
        applied = managed.applied_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
        raise ManagedScopeError(
            f"`{scope}` is managed by an access file (last applied {applied}); change the file "
            "and run `lakana access apply`, or, in an emergency, repeat with "
            "`--override-managed --reason …`"
        )
    if not holds_instance_admin(session, p.id):
        if isinstance(authz, AuditingAuthorization) and action is not None:
            authz.record_denial(p, action, _securable(scope), reason="managed_override_not_admin")
        raise PermissionDenied(
            "Only an instance administrator may override a scope an access file manages; ask "
            "one, or change the file"
        )
    checked = validate_break_glass_reason(reason, what="An override reason")
    _record_override(session, p, scope, change, checked)


def _securable(scope: str) -> Securable:
    """Name a managed scope as a securable.

    Parameters
    ----------
    scope
        ``instance`` or an environment's name.

    Returns
    -------
    Securable
        The instance, or the environment.
    """
    return Securable.instance_scope() if scope == INSTANCE else Securable.environment_scope(scope)


def _record_override(
    session: Session, p: PrincipalRef, scope: str, change: str, reason: str
) -> None:
    """Record an override: its audit event and its open row, in the change's transaction.

    Parameters
    ----------
    session
        The request's session; nothing is committed here.
    p
        Who overrides.
    scope
        The managed scope.
    change
        What is changed.
    reason
        Why, already checked.
    """
    if len(change) > _CHANGE_LENGTH:
        change = change[: _CHANGE_LENGTH - 1] + "…"
    recorded = AuditService(session).record(
        p.id,
        AuditEventType.MANAGED_SCOPE_OVERRIDDEN,
        securable=_securable(scope),
        detail={"scope": scope, "change": change, "reason": reason},
    )
    session.add(
        AccessOverride(
            id=uuid.uuid4(),
            scope=scope,
            change=change,
            reason=reason,
            principal_id=p.id,
            created_at=datetime.now(UTC),
            audit_event_id=recorded.id,
        )
    )
