"""AuditingAuthorization: wraps any AuthorizationService to audit its denials."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence

from sqlalchemy.orm import Session

from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.audit.service import ACCESS_APPLY_KEY, AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, permits_each
from lakana_core.errors import PermissionDenied
from lakana_core.identifiers import NamespacePath, shown
from lakana_core.models import AuditEventType

logger = logging.getLogger(__name__)

DEFERRED_DENIALS_KEY = "lakana.deferred_denials"
"""`Session.info` key of the denials met inside an access apply, recorded after its rollback."""


def record_deferred_denials(session: Session) -> None:
    """Record the denials an access apply met, each in its own commit.

    Inside `lakana_core.access.managed.applying` a denial cannot commit its audit row: the
    apply commits once, at its end, and a refused apply is rolled back, row included. So
    `AuditingAuthorization.record_denial` keeps it in `Session.info` instead, and the apply
    calls this after its rollback.

    Parameters
    ----------
    session
        The apply's session, already rolled back and outside `applying`.
    """
    for record in session.info.pop(DEFERRED_DENIALS_KEY, []):
        record()


def _describe(securable: Securable) -> str:
    """Write a securable for a log line, escaped and at most 200 characters of names.

    Parameters
    ----------
    securable
        The object a denied action targeted; its names came from a request.

    Returns
    -------
    str
        The kind, then the environment, namespace and table joined by dots.
    """
    names = [securable.environment, securable.namespace, securable.table]
    parts = [
        ".".join(name.parts) if isinstance(name, NamespacePath) else name
        for name in names
        if name is not None
    ]
    return f"{securable.kind.value} {shown('.'.join(parts))}".rstrip()


class AuditingAuthorization:
    """Records the denials from a wrapped `AuthorizationService`, at most a rate per principal.

    Every existing `authorize()` call site in this codebase already passes
    through some `AuthorizationService`; wrapping the service itself is how
    this plan records `AUTHORIZATION_DENIED` events without touching any of
    those ~15+ call sites individually. A permitted call passes through
    unchanged and records nothing — only a `PermissionDenied` raised from
    `authorize` is audited. `permits` is a non-raising visibility check, not
    a denied action, so it is always delegated and never recorded.

    Parameters
    ----------
    wrapped
        The real `AuthorizationService` to delegate every check to.
    session
        Database session used to record a denial. A denial's audit row
        commits immediately (there is no other action already about to
        commit — the checked action never happens).
    limiter
        Bounds the rows written per principal (`DenialAuditLimiter`). Required: the
        owner of the process builds one and passes the same instance to every wrapper, because a
        limiter made per request would never limit. A denial it refuses still raises; its count
        is added to the next row recorded, as `detail["suppressed"]`, and each one is written to
        the process log at INFO (`logger`), so the object is on record even when no row is.
    """

    def __init__(
        self, wrapped: AuthorizationService, session: Session, limiter: DenialAuditLimiter
    ) -> None:
        self._wrapped = wrapped
        self._session = session
        self._limiter = limiter

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Delegate to the wrapped service; record and re-raise on denial.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested. Recorded in `detail["action"]` on denial,
            so a denied-events review can tell what was attempted, not just
            what it targeted.
        securable
            The object acted upon.

        Raises
        ------
        PermissionDenied
            Whatever the wrapped service raised, unchanged.
        """
        try:
            self._wrapped.authorize(principal, action, securable)
        except PermissionDenied:
            self.record_denial(principal, action, securable)
            raise

    def record_denial(
        self,
        principal: PrincipalRef,
        action: Action,
        securable: Securable,
        *,
        reason: str | None = None,
    ) -> None:
        """Record one denial, at most the limiter's rate per principal.

        `authorize` calls it for a denial of the wrapped service. A rule
        that refuses beyond what the wrapped service checks (the `ADMIN`
        role for `MANAGE_GRANT`, `MANAGE_GROUP` and `MANAGE_TOKEN`) calls it
        too, so that its refusals share the one limiter.

        Parameters
        ----------
        principal
            The denied actor.
        action
            The action requested; recorded in `detail["action"]`.
        securable
            The object acted upon.
        reason
            Why, when it was not the wrapped service that refused; recorded
            in `detail["reason"]`.
        """
        if ACCESS_APPLY_KEY in self._session.info:
            # Inside an access apply nothing may commit; the apply records it after its rollback.
            self._session.info.setdefault(DEFERRED_DENIALS_KEY, []).append(
                lambda: self.record_denial(principal, action, securable, reason=reason)
            )
            return
        admitted, suppressed = self._limiter.admit(str(principal.id), action.value)
        if admitted:
            detail: dict[str, str | int] = {"action": action.value}
            if reason is not None:
                detail["reason"] = reason
            if suppressed > 0:
                detail["suppressed"] = suppressed
            AuditService(self._session).record(
                principal.id,
                AuditEventType.AUTHORIZATION_DENIED,
                securable=securable,
                detail=detail,
                commit=True,
            )
        else:
            logger.info(
                "AUTHORIZATION_DENIED not recorded in the audit table, because principal %s "
                "is over LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE: action=%s object=%s",
                principal.id,
                action.value,
                _describe(securable),
            )

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Delegate to the wrapped service; record nothing.

        Hiding an object from a listing is a visibility filter, not a
        denied action, so it never writes `AUTHORIZATION_DENIED`. Were it
        to, every list call would write one false denial per hidden row.

        Parameters
        ----------
        principal
            The actor the question is about.
        action
            The action in question.
        securable
            The object in question.

        Returns
        -------
        bool
            Whatever the wrapped service's `permits` returns.
        """
        return self._wrapped.permits(principal, action, securable)

    def permits_each(
        self, principal: PrincipalRef, questions: Sequence[tuple[Action, Securable]]
    ) -> list[bool]:
        """Delegate many visibility questions at once; record nothing, as `permits`.

        Parameters
        ----------
        principal
            The actor the questions are about.
        questions
            ``(action, securable)`` pairs.

        Returns
        -------
        list of bool
            Whatever the wrapped service answers (`lakana_core.auth.permits_each`).
        """
        return permits_each(self._wrapped, principal, questions)


FLUSH_MAX_PRINCIPALS = 100
"""Most principals whose summary row `flush_suppressed` writes when a process stops."""

FLUSH_MAX_SECONDS = 3.0
"""About the longest `flush_suppressed` runs; a container's stop timeout is 10 s by default."""


def flush_suppressed(
    limiter: DenialAuditLimiter,
    session: Session,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Write one summary row per principal for denials no recorded row has carried.

    Called when the server or the worker stops, so that a count held in memory is not lost with
    the process. A row has no object (each denial's object is in the process log) and
    `detail["suppressed"]`. Each row is a commit, so the work is bounded: at most
    `FLUSH_MAX_PRINCIPALS` principals and about `FLUSH_MAX_SECONDS` in all, because a clean stop
    with a slow or unavailable database would otherwise outlast the container's stop timeout and
    end in a kill. The counts of the principals left out are dropped, and one WARNING says how
    many principals that is (the process log still names each denial). A failure is logged,
    never raised: shutdown goes on.

    Parameters
    ----------
    limiter
        The process's limiter; its pending counts are taken from it.
    session
        A session to write with; each row is committed.
    clock
        Seconds, monotonic; a test injects its own.
    """
    started = clock()
    pending = list(limiter.drain().items())
    written = 0
    for principal, count in pending:
        if written >= FLUSH_MAX_PRINCIPALS or clock() - started >= FLUSH_MAX_SECONDS:
            break
        written += 1
        try:
            AuditService(session).record(
                uuid.UUID(principal),
                AuditEventType.AUTHORIZATION_DENIED,
                detail={
                    "suppressed": count,
                    "note": "denials not recorded one by one; the process log names each",
                },
                commit=True,
            )
        except Exception:
            session.rollback()
            logger.warning(
                "The %d AUTHORIZATION_DENIED events of principal %s that were not recorded could "
                "not be written as a summary row; the process log names each one.",
                count,
                principal,
                exc_info=True,
            )
    skipped = len(pending) - written
    if skipped:
        logger.warning(
            "Stopping: the counts of AUTHORIZATION_DENIED events not recorded were written for "
            "%d principals; those of %d principals were not written (at most %d principals and "
            "%.0f s). The process log names each denial.",
            written,
            skipped,
            FLUSH_MAX_PRINCIPALS,
            FLUSH_MAX_SECONDS,
        )
