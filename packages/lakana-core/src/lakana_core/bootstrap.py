"""Creation of the principal the platform starts with, and lockout recovery.

Architecture Constraint 9 puts a single bootstrap admin in M1 so that M3
replaces an authorization *implementation* rather than inserting call sites.
Until this principal exists there is no subject to own a namespace or a table,
and nothing for M3 to grant to.

`bootstrap_if_needed` is the only entry point `lakana_server.app`'s lifespan
calls. Before this was fixed, startup unconditionally re-granted instance ADMIN and
re-minted a PAT whenever the bootstrap principal had none of its own —
so deliberately revoking that principal's grant and PATs (the documented way
to retire the bootstrap credential once real admins exist) was silently
undone on the very next restart, with a fresh secret logged each time.
`bootstrap_if_needed` now acts only as lockout recovery: it does nothing at
all while `admin_exists` finds some other active admin, and otherwise
restores the bootstrap principal to admin from scratch.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.access.managed import lockout_recovery
from lakana_core.audit.service import AuditService
from lakana_core.auth import (
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    Securable,
    SecurableKind,
)
from lakana_core.errors import ConflictError, ValidationError
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService, active_instance_admin_ids
from lakana_core.identity.links import withdraw_open_links
from lakana_core.identity.pats import PatService
from lakana_core.identity.sessions import SessionService, utc_now
from lakana_core.models import (
    AuditEventType,
    Grant,
    Passkey,
    PersonalAccessToken,
    Principal,
    PrincipalType,
)


def _lookup_principal(session: Session, name: str) -> Principal | None:
    """Look up a principal by name.

    Parameters
    ----------
    session
        Database session.
    name
        The principal's name.

    Returns
    -------
    Principal or None
        The matching row, or `None` if no principal has that name.
    """
    return session.execute(select(Principal).where(Principal.name == name)).scalar_one_or_none()


def ensure_bootstrap_principal(session: Session, name: str) -> PrincipalRef:
    """Return the bootstrap principal, creating it if it does not exist.

    Idempotent: this runs on every server start, so a second call must return
    the existing principal rather than fail on the unique name constraint.
    "Two servers starting at once against one database" is the ordinary
    Docker Compose topology, not an exotic race: the loser's insert hits the
    real unique constraint on `Principal.name`, and that `IntegrityError` is
    caught here rather than left to crash that server's startup — the same
    pattern `CatalogService.create_table` uses for its own insert race.

    Records `PRINCIPAL_CREATED` when it actually creates the principal —
    first boot only, never a restart that finds the row already there
    . No other principal exists yet the
    first time this runs, so the new principal is recorded as its own
    actor; `session.flush()` runs before the audit row is added, so the
    principal's id already resolves for `AuditEvent.principal_id`'s
    foreign key before that insert is attempted in the same transaction.

    Parameters
    ----------
    session
        Database session.
    name
        The principal's name, from `Settings.bootstrap_admin`.

    Returns
    -------
    PrincipalRef
        A reference to the principal, suitable for passing into
        `CatalogService`.

    Raises
    ------
    ConflictError
        If the insert fails for a reason other than a name collision — the
        re-select after rollback still finds no row with this name.
    """
    existing = _lookup_principal(session, name)
    if existing is None:
        candidate = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
        session.add(candidate)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            existing = _lookup_principal(session, name)
            if existing is None:
                # A fixed message, not `str(exc)`: SQLAlchemy's
                # `IntegrityError` string includes the failed statement and
                # its bound parameters. `exc` is still chained via
                # `from exc`, so it remains visible in logs and tracebacks.
                raise ConflictError(
                    f"Could not create bootstrap principal {name!r}: the write "
                    "failed for a reason other than a name collision."
                ) from exc
        else:
            AuditService(session).record(
                candidate.id,
                AuditEventType.PRINCIPAL_CREATED,
                detail={"principal": name, "type": candidate.type.value},
            )
            session.commit()
            existing = candidate
    return PrincipalRef(id=existing.id, name=existing.name, type=existing.type)


def ensure_bootstrap_admin_grant(
    session: Session, principal: PrincipalRef, authz: AuthorizationService, *, commit: bool = True
) -> None:
    """Grant the bootstrap principal `admin` at instance scope, if it has none yet.

    Idempotent in the sense that matters — calling it again when the grant
    already exists is a no-op — but, unlike `ensure_bootstrap_principal`,
    this is a plain check-then-insert with no unique constraint backing
    it: nothing about "one admin grant for this principal" is unique at
    the schema level. Two servers racing to insert this grant at once
    startup produce a harmless duplicate row, not a correctness issue,
    since `GrantAuthorization` only checks for *existence* of a covering
    grant, never counts them. A revoked grant does not count as existing
    (the query below filters `revoked_at.is_(None)`), so this also
    re-grants whenever the only prior grant was revoked — which is what
    `bootstrap_if_needed` relies on for lockout recovery.

    Parameters
    ----------
    session
        Database session.
    principal
        The bootstrap principal, as `ensure_bootstrap_principal` returned
        it.
    authz
        Authorization service to construct `GrantService` with. Always
        `AllowAllAuthorization()` in production (see `app.py`'s lifespan)
        — bootstrapping must succeed before any grant can exist to
        authorize it.
    commit
        Forwarded to `GrantService.grant`. `False` when
        `bootstrap_if_needed` folds this grant into its own single,
        larger commit alongside the recovery PAT and the audit event.
    """
    existing = session.execute(
        select(Grant).where(
            Grant.principal_id == principal.id,
            Grant.role == Role.ADMIN,
            Grant.kind == SecurableKind.INSTANCE,
            Grant.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    GrantService(session, authz).grant(
        principal,
        principal=principal.name,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
        commit=commit,
    )


def _pat_key_id_exists(session: Session, key_id: str) -> bool:
    """Check whether any PAT, of any principal or revocation status, already uses a key_id.

    A revoked row still occupies its `key_id` in the unique index — revoking
    it does not free the value for reuse — so this is what
    `bootstrap_if_needed` checks before trying to reuse a configured
    `key_id` from an earlier boot.

    Parameters
    ----------
    session
        Database session.
    key_id
        The key_id to look for.

    Returns
    -------
    bool
        True if some `PersonalAccessToken` row already has this `key_id`.
    """
    return (
        session.execute(
            select(PersonalAccessToken.id).where(PersonalAccessToken.key_id == key_id)
        ).scalar_one_or_none()
        is not None
    )


def write_token_file(path: Path, token: str) -> None:
    """Write a freshly minted token to a file only its owner can read.

    The file is created exclusively (`O_CREAT | O_EXCL`), so an existing file is never overwritten
    and a symlink at the path is never followed (`O_NOFOLLOW` as well); its mode is 0600
    whatever the umask; the content is flushed to disk before this returns. Missing parent
    directories are created with mode 0700.

    Parameters
    ----------
    path
        Where to write the token.
    token
        The full token; written with a trailing newline, and nothing else.

    Raises
    ------
    ConflictError
        If something already exists at `path` (a file, a directory or a symlink), or the file
        cannot be written for another reason. The message names the path and the next step.
    """
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ConflictError(
            f"Could not write the first administrator's token to {path}: {exc.strerror}. Point "
            "LAKANA_BOOTSTRAP_TOKEN_FILE at a path in a directory the server's user can write."
        ) from exc
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except FileExistsError as exc:
        raise ConflictError(
            f"The first administrator's token cannot be written: {path} already exists (a file "
            "or a symlink). It may hold an earlier token; remove it after use, or point "
            "LAKANA_BOOTSTRAP_TOKEN_FILE at a path that does not exist, then start the "
            "server again."
        ) from exc
    except OSError as exc:
        raise ConflictError(
            f"Could not write the first administrator's token to {path}: {exc.strerror}. Point "
            "LAKANA_BOOTSTRAP_TOKEN_FILE at a path in a directory the server's user can write."
        ) from exc
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        with contextlib.suppress(OSError):
            path.unlink()
        raise ConflictError(
            f"Could not write the first administrator's token to {path}: {exc.strerror}. Point "
            "LAKANA_BOOTSTRAP_TOKEN_FILE at a path on a disk that has room and accepts writes."
        ) from exc


def admin_exists(session: Session) -> bool:
    """Check whether an active instance admin already exists.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    bool
        True if some active principal holds an active, unrevoked,
        instance-scoped ADMIN grant, directly or through a group
        membership. False otherwise, including on a never-bootstrapped
        instance.
    """
    return bool(active_instance_admin_ids(session))


def bootstrap_if_needed(
    session: Session,
    name: str,
    *,
    key_id: str | None = None,
    secret: str | None = None,
    token_file: Path | None = None,
) -> str | None:
    """Restore the instance's admin, but only when none currently exists.

    Runs on every server start. While `admin_exists` finds some active
    admin — the bootstrap principal itself, or any other principal an
    operator has since promoted — this changes nothing: no grant, no PAT,
    no log line. Deliberately revoking the bootstrap principal's own
    grant and PATs (the documented way to retire it once real admins
    exist) is therefore permanent across restarts, closing that hole.

    Only when no active admin exists at all does this act, as lockout
    recovery, entirely in one transaction (the grant used to
    commit on its own before the PAT was minted, so a PAT-minting failure
    left a committed grant with no logged credential and no audit event,
    and — because that principal was then active-admin-with-a-grant — no
    retry on the next restart either):

    1. Ensures the principal named `name` exists, reactivating it if it
       was deactivated; reactivation removes its passkeys, ends its browser sessions and
       withdraws its open sign-in links. **This is the principal `Settings.bootstrap_admin`
       names, not necessarily a freshly created one** — recovery grants
       instance ADMIN to whoever already holds that name, so an operator
       must never point `bootstrap_admin` at the name of an existing,
       unrelated user.
    2. Revokes every unrevoked PAT that principal already has, so the one
       PAT this call mints is the only working credential for it
       afterward.
    3. Grants it instance ADMIN (re-granting even if a prior grant was
       revoked). When an access file manages `instance`, this grant is the
       one change outside the file the guard lets through
       (`lakana_core.access.managed.lockout_recovery`); it is recorded as an
       override (`MANAGED_SCOPE_OVERRIDDEN`), which the next plan lists as drift.
    4. Mints one fresh PAT, via `PatService.issue(..., commit=False)`
       — which itself records `PAT_ISSUED`. A random token is written to `token_file`
       (`write_token_file`) before the commit and never returned to be logged; the file is
       removed again if the commit fails. A supplied `key_id`/`secret` writes no file.
    5. Records `AuditEventType.BOOTSTRAP_ADMIN_RESTORED`.

    Steps 1's reactivation and 2 through 5 share one commit — nothing above
    is durable, and no admin/grant/PAT state has changed, unless every step
    succeeds.

    Parameters
    ----------
    session
        Database session.
    name
        The bootstrap principal's name, from `Settings.bootstrap_admin`.
    key_id
        A specific key_id to issue the recovery PAT with, instead of a
        random one. Set only from `Settings.bootstrap_pat_key_id`
        (test/dev configuration) — `None` in production.
    secret
        A specific secret to issue the recovery PAT with. Same
        restriction as `key_id`.
    token_file
        Where to write a randomly minted token. Required when `key_id` is `None`; unused when a
        credential is supplied. From `Settings.bootstrap_token_file`.

    Returns
    -------
    str or None
        The freshly minted PAT's full token value. It must never be logged: a
        random token is in `token_file`, a supplied one is already known to the caller.
        `None` if an active instance admin already exists, in which case nothing else in
        this function runs.

    Raises
    ------
    ValidationError
        If only one of `key_id` and `secret` is given, or if a random token would be minted and
        no `token_file` is given. Raised before anything changes.
    ConflictError
        If `key_id` is supplied and some PAT already uses it (a collision never mints a
        different token); if `ensure_bootstrap_principal` cannot create or find the
        principal named `name`; or if the token file already exists or cannot be written.
    """
    if admin_exists(session):
        return None
    if (key_id is None) != (secret is None):
        raise ValidationError(
            "Only one of LAKANA_BOOTSTRAP_PAT_KEY_ID and LAKANA_BOOTSTRAP_PAT_SECRET is set, and "
            "a token cannot be made from half a credential. Set both, or neither and set "
            "LAKANA_BOOTSTRAP_TOKEN_FILE to have a new token written to a file."
        )
    if key_id is None and token_file is None:
        raise ValidationError(
            "The server has to create the first administrator and no token was supplied, so "
            "it would write a new token to a file, but LAKANA_BOOTSTRAP_TOKEN_FILE is not "
            "set. Set it to a path only the server's user can reach (for example "
            "/var/lib/lakana/bootstrap-token), or supply LAKANA_BOOTSTRAP_PAT_KEY_ID and "
            "LAKANA_BOOTSTRAP_PAT_SECRET."
        )
    if key_id is not None and _pat_key_id_exists(session, key_id):
        raise ConflictError(
            f"The first administrator cannot be restored: the key id {key_id!r} in "
            "LAKANA_BOOTSTRAP_PAT_KEY_ID is already used by an earlier token (a revoked token "
            "keeps its key id). Set LAKANA_BOOTSTRAP_PAT_KEY_ID and LAKANA_BOOTSTRAP_PAT_SECRET "
            "to a new pair (a key id never used before, for example 16 random hex digits), or "
            "remove both and set LAKANA_BOOTSTRAP_TOKEN_FILE to have a new token written to a "
            "file; then start the server again."
        )
    authz = AllowAllAuthorization()
    principal = ensure_bootstrap_principal(session, name)
    principal_row = session.get(Principal, principal.id)
    if principal_row is None:
        raise ConflictError(
            f"Bootstrap principal {name!r} vanished immediately after being ensured."
        )
    if not principal_row.is_active:
        principal_row.is_active = True
        # Reactivating an account removes its passkeys, ends its sessions and withdraws its
        # links: whoever held them while it was deactivated must not get back in with them.
        session.execute(delete(Passkey).where(Passkey.principal_id == principal.id))
        SessionService(session, authz).end_for_principal(principal.id, "reactivated")
        withdraw_open_links(session, principal.id, now=utc_now(), reason="reactivated")

    now = datetime.now(UTC)
    for pat in (
        session.execute(
            select(PersonalAccessToken).where(
                PersonalAccessToken.principal_id == principal.id,
                PersonalAccessToken.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    ):
        pat.revoked_at = now

    # The one change the access-file guard lets through: recovery must never depend on the
    # file. It is recorded as an override, so the next plan shows the grant as drift.
    with lockout_recovery(session):
        ensure_bootstrap_admin_grant(session, principal, authz, commit=False)

    issued = PatService(session, authz).issue(
        principal,
        principal.name,
        name="bootstrap",
        key_id=key_id,
        secret=secret,
        bootstrap=True,
        commit=False,
    )
    written: Path | None = None
    if key_id is None and token_file is not None:
        try:
            write_token_file(token_file, issued.token)
        except ConflictError:
            session.rollback()
            raise
        written = token_file

    AuditService(session).record(
        principal.id,
        AuditEventType.BOOTSTRAP_ADMIN_RESTORED,
        securable=Securable.instance_scope(),
        detail={"key_id": issued.key_id},
    )
    try:
        session.commit()
    except BaseException:
        if written is not None:
            with contextlib.suppress(OSError):
                written.unlink()
        raise
    return issued.token
