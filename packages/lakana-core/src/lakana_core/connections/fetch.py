"""A running task's fetch of a connection its asset declares: the one way a secret reaches it.

`fetch_for_task` answers only a bound task token whose attempt is running (authentication
resolves `TaskScope.bound` from the database on every request, and answers 401 once the attempt
is over), for a name the deployed manifest entry declares, in the token's own environment. The
checks run in that order so that an answer never says whether a connection exists anywhere the
task may not look: an undeclared name is refused the same way whether or not a connection of
that name exists, and a declared name the environment lacks is refused without a word about
other environments.

`connection_secret` is the one place a connection's secret is decrypted, for the fetch and for
the worker's redaction of the task's output; it calls `require_use` first, so a secret that is
not bound to this very connection is never decrypted for it, whatever `Connection.secret_id`
says.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.audit.service import AuditService
from lakana_core.auth import PrincipalRef, Securable, TaskScope
from lakana_core.connections.endpoints import Endpoint, endpoints
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.errors import LakanaError, PermissionDenied
from lakana_core.identifiers import shown
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    AuditEventType,
    Connection,
    Environment,
    Secret,
    TaskRun,
)
from lakana_core.secrets.crypto import decrypt_secret_row
from lakana_core.secrets.keys import KeyProvider
from lakana_core.secrets.uses import SecretUse, SecretUseRefused, require_use

logger = logging.getLogger(__name__)

NOT_BOUND = "only a running task can fetch a connection"
"""The refusal for any caller that is not a bound task token of a running attempt."""
NOT_DECLARED = "connection `{name}` is not declared by asset `{asset}`"
"""The refusal for a name the task's deployed asset does not declare."""
NOT_IN_ENVIRONMENT = "connection `{name}` is not in environment `{environment}`"
"""The refusal for a declared name the task's environment has no connection of."""

FETCH_ACTION = "FETCH_CONNECTION"
"""The prefix of the action names the denial limiter counts refusals under:
``FETCH_CONNECTION:<reason>``, so that the first refusal of each reason is recorded."""


@dataclass(frozen=True)
class TaskConnection:
    """What a running task is told about one of its connections.

    Parameters
    ----------
    name
        The connection's name.
    kind
        Its kind.
    address
        Its address, with defaults filled in.
    settings
        Its settings, a ``ca_certificate`` in full (the helper needs it to trust the server).
    endpoints
        Every host and port the connection reaches (`lakana_core.connections.endpoints`).
    secret
        The decrypted values of its secret; `None` when it has none, or none it may use.
        Left out of `repr`.
    """

    name: str
    kind: ConnectionKind
    address: dict[str, Any]
    settings: dict[str, Any]
    endpoints: tuple[Endpoint, ...]
    secret: dict[str, str] | None = field(repr=False)


def connection_secret(
    session: Session, key_provider: KeyProvider, row: Connection, environment_name: str
) -> tuple[dict[str, str] | None, str | None]:
    """Decrypt the secret a connection uses, if it is active and bound to this connection.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Unwraps the secret's data key.
    row
        The connection.
    environment_name
        The connection's environment.

    Returns
    -------
    tuple
        The values, or `None`; and why there are none (``secret_not_bound``,
        ``secret_unreadable``), or `None` when there are values or the connection simply has
        no (active) secret. Neither text holds a value.
    """
    secret = None if row.secret_id is None else session.get(Secret, row.secret_id)
    if secret is None or secret.revoked_at is not None:
        return None, None
    try:
        require_use(session, secret, SecretUse.connection(row), environment_name=environment_name)
    except (SecretUseRefused, ValueError):
        # `ValueError`: the secret belongs to another environment. Either way it is not this
        # connection's: fail closed, with no value and no secret name in the answer.
        logger.warning(
            "connection %s of environment %s names a secret that is not bound to it; "
            "it is used as if it had none",
            shown(row.name),
            shown(environment_name),
        )
        return None, "secret_not_bound"
    try:
        values = decrypt_secret_row(
            secret,
            key_provider,
            environment_name=environment_name,
            accept_format_1=key_provider.accepts_format_1,
        )
    except LakanaError:
        # `decrypt_secret_row` has logged why, with the secret's id and never a value.
        return None, "secret_unreadable"
    return values, None


def _detail(scope: TaskScope | None, name: str, task: TaskRun | None) -> dict[str, object]:
    """Build an audit event's detail from what is known about the caller.

    Parameters
    ----------
    scope
        The caller's task scope, if any.
    name
        The connection's name, as asked.
    task
        The bound attempt's task run, if any.

    Returns
    -------
    dict
        ``connection``, then ``environment``, and for a bound attempt ``repository``,
        ``asset``, ``run_id``, ``task_run_id`` and ``attempt`` (the claim counter).
    """
    detail: dict[str, object] = {"connection": shown(name)}
    if scope is None:
        return detail
    detail["environment"] = scope.environment
    bound = scope.bound
    if bound is None:
        return detail
    detail["repository"] = bound.repository
    detail["asset"] = bound.asset
    if task is not None:
        detail["run_id"] = str(task.run_id)
    detail["task_run_id"] = str(bound.task_run_id)
    detail["attempt"] = bound.claim
    return detail


def _refuse(
    session: Session,
    p: PrincipalRef,
    limiter: DenialAuditLimiter,
    detail: dict[str, object],
    reason: str,
    message: str,
) -> NoReturn:
    """Record a refusal, at most the limiter's rate, and refuse.

    Parameters
    ----------
    session
        Database session; the row is committed.
    p
        The caller.
    limiter
        The process's denial limiter.
    detail
        The fields known about the request.
    reason
        ``not_bound``, ``not_declared`` or ``not_in_environment``.
    message
        The fixed text.

    Raises
    ------
    PermissionDenied
        Always, with `message`.
    """
    admitted, suppressed = limiter.admit(str(p.id), f"{FETCH_ACTION}:{reason}")
    if admitted:
        recorded = {**detail, "reason": reason}
        if suppressed:
            recorded["suppressed"] = suppressed
        environment = detail.get("environment")
        AuditService(session).record(
            p.id,
            AuditEventType.CONNECTION_CREDENTIAL_REFUSED,
            securable=None
            if not isinstance(environment, str)
            else Securable.environment_scope(environment),
            detail=recorded,
            commit=True,
        )
    else:
        logger.info(
            "CONNECTION_CREDENTIAL_REFUSED not recorded in the audit table, because principal "
            "%s is over LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE: reason=%s connection=%s "
            "asset=%s task_run_id=%s",
            p.id,
            reason,
            detail.get("connection"),
            detail.get("asset"),
            detail.get("task_run_id"),
        )
    raise PermissionDenied(message)


def fetch_for_task(
    session: Session,
    key_provider: KeyProvider,
    p: PrincipalRef,
    name: str,
    *,
    limiter: DenialAuditLimiter,
) -> TaskConnection:
    """Answer a running task's request for one of its declared connections.

    The checks run in an order that reveals nothing: first that the caller is a bound task
    token of a running attempt, then that the asset declares the name, and only then whether
    the environment has it. Every refusal is audited (through the denial limiter); a success is
    audited and committed before the caller sends the body.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Unwraps the secret's data key.
    p
        The authenticated caller; its `task_scope.bound` was resolved from the database at
        authentication, which refuses a token whose attempt is over.
    name
        The connection's name, as the task asked.
    limiter
        The audit denial limiter.

    Returns
    -------
    TaskConnection
        The connection, its endpoints and its decrypted secret (or `None` without one it may
        use).

    Raises
    ------
    PermissionDenied
        With one of the three fixed texts (`NOT_BOUND`, `NOT_DECLARED`, `NOT_IN_ENVIRONMENT`).
    """
    scope = p.task_scope
    bound = None if scope is None else scope.bound
    if scope is None or bound is None:
        _refuse(session, p, limiter, _detail(scope, name, None), "not_bound", NOT_BOUND)
    task = session.get(TaskRun, bound.task_run_id)
    detail = _detail(scope, name, task)
    if name not in bound.connections:
        # `bound.connections` is empty for a manifest value that cannot be read.
        _refuse(
            session,
            p,
            limiter,
            detail,
            "not_declared",
            NOT_DECLARED.format(name=shown(name), asset=shown(bound.asset)),
        )
    row = session.execute(
        select(Connection)
        .join(Environment, Environment.id == Connection.environment_id)
        .where(Environment.name == scope.environment, LIVE_ENVIRONMENT, Connection.name == name)
    ).scalar_one_or_none()
    if row is None:
        _refuse(
            session,
            p,
            limiter,
            detail,
            "not_in_environment",
            NOT_IN_ENVIRONMENT.format(name=shown(name), environment=shown(scope.environment)),
        )
    secret, why = connection_secret(session, key_provider, row, scope.environment)
    found = TaskConnection(
        name=row.name,
        kind=ConnectionKind(row.kind),
        address=dict(row.address),
        settings=dict(row.settings),
        endpoints=endpoints(row.kind, row.address, row.settings),
        secret=secret,
    )
    AuditService(session).record(
        p.id,
        AuditEventType.CONNECTION_CREDENTIAL_FETCHED,
        securable=Securable.environment_scope(scope.environment),
        detail=detail if why is None else {**detail, "reason": why},
    )
    session.commit()
    return found
