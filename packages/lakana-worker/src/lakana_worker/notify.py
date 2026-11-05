"""The delivery of notifications: claim a due row, send it outside any transaction, record it.

`deliver_due` is one step of the loop and `DeliveryLoop` runs it on a thread of its own, so a
slow or dead webhook never holds up the task loop or the scheduler. Delivery is at least once:

- One row at a time is claimed, in its own short transaction and with a fresh database time, with
  ``FOR UPDATE SKIP LOCKED``: this worker's lease (about a minute) starts when its send starts,
  so it covers that send, and no claimed row ever waits unsent behind a slow one. The claim adds
  one attempt; counting it there means a worker that dies mid-send still uses one of the five, so
  a row that kills its worker cannot do so for ever.
- The send runs with no transaction open. The webhook's secret is decrypted for that attempt only
  and goes out of scope with it.
- The result is recorded in a new transaction that matches the row only while this worker still
  holds the lease it set. A worker that lost it (it was slow, a drain left the row, another
  worker took it over) records nothing, and the delivery id is the same on the next attempt, so a
  receiver can drop the duplicate.
- Nothing here writes to a run or its tasks; a failed notification is a row and an audit event.

The URL and the signing key appear in no row, log line, exception text or audit detail: every
failure is reduced to a fixed reason before it is stored (or to the use refusal, which holds
names only), and the exception of an unexpected failure is never logged with its text.
"""

from __future__ import annotations

import dataclasses
import logging
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine, or_, select, update
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Securable
from lakana_core.config import WebhookConfig
from lakana_core.db import db_now, rowcount
from lakana_core.models import (
    AuditEventType,
    Environment,
    NotificationDelivery,
    NotificationState,
    Run,
    Secret,
)
from lakana_core.models.notification import MAX_ATTEMPTS
from lakana_core.notifications import payload
from lakana_core.secrets.crypto import CURRENT_FORMAT, decrypt_secret_row
from lakana_core.secrets.keys import KeyProvider
from lakana_core.secrets.uses import SecretUse, SecretUseRefused, require_use
from lakana_worker import egress
from lakana_worker.egress import DeliveryResult, EgressRefused, check_destination, post

logger = logging.getLogger(__name__)

LEASE_SECONDS = 60
"""How long a claimed row belongs to its worker; an attempt takes at most about 20 s."""

ATTEMPT_TIMEOUT_SECONDS = 10.0
"""The time one attempt may take to connect and be answered."""

RETRY_SECONDS = (30, 120, 600, 1800)
"""The wait after the first, second, third and fourth attempt."""

JITTER = 0.2
"""Each wait is multiplied by a factor in ``[1 - JITTER, 1 + JITTER]``, as for task retries."""

IDLE_SECONDS = 2.0
"""How long the loop sleeps when no row is due."""

BATCH = 10
"""Rows one step claims at most."""

BUDGET_SECONDS = 30.0
"""Seconds after which a step claims no further row, so the loop looks at the database's clock,
the drain and the next failure again."""

FAILURE_LOG_SECONDS = 60.0
"""Least time between two log lines about a failing step."""

MAX_IDLE_SECONDS = 60.0
"""The longest sleep after consecutive failing steps."""

SECRET_MISSING = (  # nosec B105 -- a message about a secret, not a credential
    "secret `{}` does not exist in this environment (create it with `lakana secret create`)"
)
SECRET_REVOKED = (  # nosec B105 -- a message about a secret, not a credential
    "secret `{}` is revoked, and a revoked name cannot be created or rotated again (create a "
    "secret under another name, put it in the job's `notify.webhook` and deploy)"
)
NOT_A_WEBHOOK = (
    "secret `{}` does not hold a webhook URL (it needs a `url` that starts with https://)"
)
CANNOT_DECRYPT = (
    "secret `{}` could not be decrypted by this worker (check that LAKANA_SECRETS__MASTER_KEY is "
    "the same on every worker, or set the secret again)"
)
OLD_FORMAT = (  # nosec B105 -- a message about a secret, not a credential
    "secret `{}` is stored in the old format, which this worker refuses; set it again with "
    "`lakana secret rotate`"
)
NEWER_FORMAT = (  # nosec B105 -- a message about a secret, not a credential
    "secret `{}` was written by a newer Lakana than this worker; upgrade this worker"
)
UNEXPECTED = "the sender failed in a way that was not expected (see the worker's log)"
UNSENDABLE = "the notification could not be built (unknown format or event)"
GAVE_UP = "the result of the last attempt was never recorded (its worker stopped or was too slow)"

_PERMANENT_REFUSALS = (egress.INVALID, egress.NOT_HTTPS, egress.NOT_443, egress.PRIVATE)

Sender = Callable[[str, bytes, dict[str, str], WebhookConfig, float], DeliveryResult]
"""Checks a destination and sends one attempt: URL, body, headers, operator settings, timeout."""


def send_webhook(
    url: str, body: bytes, headers: dict[str, str], webhook: WebhookConfig, timeout: float
) -> DeliveryResult:
    """Check a webhook's destination and send one attempt.

    Parameters
    ----------
    url
        The webhook URL; it is never kept, logged or quoted.
    body
        The request body.
    headers
        Request headers.
    webhook
        The operator's allow-list, proxy and CA bundle.
    timeout
        Seconds for the attempt.

    Returns
    -------
    DeliveryResult
        The outcome. A refused destination is permanent when waiting cannot change the rule
        (the address, scheme or port is not allowed) and retryable when only the lookup failed.
    """
    try:
        destination = check_destination(url, webhook)
    except EgressRefused as refusal:
        reason = refusal.reason
        return DeliveryResult(
            ok=False, permanent=reason in _PERMANENT_REFUSALS, reason=reason, status=None
        )
    return post(destination, body, headers, timeout=timeout)


@dataclass(frozen=True)
class NotifySettings:
    """What the delivery loop needs.

    Parameters
    ----------
    owner
        This worker's id; it names the leases.
    key_provider
        Unwraps secrets. The worker starts the loop only with a usable master key.
    webhook
        The operator's webhook settings (`LAKANA_WEBHOOK__*`).
    lease_seconds
        The lease of a claimed row.
    timeout
        Seconds for one attempt.
    send
        Sends one attempt; tests replace it.
    rand
        Jitter source returning a float in ``[0, 1]``.
    """

    owner: str
    key_provider: KeyProvider
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    lease_seconds: int = LEASE_SECONDS
    timeout: float = ATTEMPT_TIMEOUT_SECONDS
    send: Sender = send_webhook
    rand: Callable[[], float] = random.SystemRandom().random


@dataclass(frozen=True)
class _Claim:
    """A row this worker holds the lease of.

    Parameters
    ----------
    id
        The delivery id.
    expires
        The lease expiry this worker set; a record matches it exactly.
    now
        The database time of the claim, used for the signed timestamp and the recorded times.
    """

    id: uuid.UUID
    expires: datetime
    now: datetime


@dataclass(frozen=True)
class _Unusable:
    """A webhook that cannot be sent to, and whether another attempt could change that.

    Parameters
    ----------
    reason
        The fixed reason.
    retry
        `True` when the cause may be this worker's (its key, its version) and not the data's.
    """

    reason: str
    retry: bool = False


@dataclass(frozen=True)
class _Request:
    """A request ready to send. Its text form hides the URL and the key.

    Parameters
    ----------
    url
        The webhook URL.
    body
        The request body.
    headers
        Request headers, with the signature when there is one.
    """

    url: str = field(repr=False)
    body: bytes = field(repr=False)
    headers: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class _Outcome:
    """What an attempt came to, as it is recorded.

    Parameters
    ----------
    delivered
        Whether the webhook answered 2xx.
    final
        Whether no further attempt follows (permanent, or the last one).
    reason
        The fixed reason.
    """

    delivered: bool
    final: bool
    reason: str


def _wait_seconds(attempt: int, rand: Callable[[], float]) -> float:
    """Return the wait after an attempt.

    Parameters
    ----------
    attempt
        The attempt that just failed (1 for the first).
    rand
        Returns a float in ``[0, 1]``; 0.5 means no jitter.

    Returns
    -------
    float
        The base wait from `RETRY_SECONDS` times the jitter factor.
    """
    base = RETRY_SECONDS[min(attempt, len(RETRY_SECONDS)) - 1]
    return base * (1 + JITTER * (2 * rand() - 1))


def _audit_failure(
    session: Session, row: NotificationDelivery, environment: str, reason: str
) -> None:
    """Write the audit event of a notification that will not be delivered.

    The detail names the run, job, event and reason and never the URL or the signing key.

    Parameters
    ----------
    session
        The recording transaction.
    row
        The delivery.
    environment
        The environment's name.
    reason
        The fixed reason.
    """
    AuditService(session).record(
        None,
        AuditEventType.NOTIFICATION_FAILED,
        securable=Securable.environment_scope(environment),
        detail={
            "run_id": str(row.run_id) if row.run_id is not None else None,
            "job": row.job,
            "event": row.event,
            "reason": reason,
        },
    )


def _claim(engine: Engine, now: datetime, cfg: NotifySettings) -> _Claim | None:
    """Claim the oldest due row: set the lease, count the attempt, commit.

    A row that is claimed again after its lease ran out with the last attempt already used is
    failed here, not sent again: nobody recorded that attempt's result.

    Parameters
    ----------
    engine
        The metadata database.
    now
        The time of this claim; the lease runs from it.
    cfg
        The settings.

    Returns
    -------
    _Claim or None
        The row to send; `None` when no row is due.
    """
    expires = now + timedelta(seconds=cfg.lease_seconds)
    while True:
        with Session(engine) as session:
            row = session.scalars(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.state == NotificationState.PENDING,
                    NotificationDelivery.next_attempt_at <= now,
                    or_(
                        NotificationDelivery.lease_owner.is_(None),
                        NotificationDelivery.lease_expires_at.is_(None),
                        NotificationDelivery.lease_expires_at <= now,
                    ),
                )
                .order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if row is None:
                return None
            if row.attempts >= MAX_ATTEMPTS:
                environment = session.get(Environment, row.environment_id)
                row.state = NotificationState.FAILED
                row.last_result = GAVE_UP
                row.next_attempt_at = None
                row.lease_owner = None
                row.lease_expires_at = None
                _audit_failure(session, row, environment.name if environment else "", GAVE_UP)
                session.commit()
                continue
            row.lease_owner = cfg.owner
            row.lease_expires_at = expires
            row.attempts += 1
            claim = _Claim(row.id, expires, now)
            session.commit()
            return claim


def _webhook(
    session: Session, row: NotificationDelivery, environment: Environment, cfg: NotifySettings
) -> tuple[str, str | None] | _Unusable:
    """Read the webhook's URL and signing key from the row's secret.

    Parameters
    ----------
    session
        Database session.
    row
        The delivery.
    environment
        The environment whose secret scope is read.
    cfg
        The settings.

    Returns
    -------
    tuple[str, str | None] or _Unusable
        The URL and the signing key, or why this secret cannot be used.
    """
    name = row.secret_name
    secret = session.scalars(
        select(Secret).where(
            Secret.environment_id == environment.id,
            Secret.name == name,
        )
    ).one_or_none()
    if secret is None:
        return _Unusable(SECRET_MISSING.format(name))
    if secret.revoked_at is not None:
        return _Unusable(SECRET_REVOKED.format(name))
    if secret.format_version == 1 and not cfg.key_provider.accepts_format_1:
        return _Unusable(OLD_FORMAT.format(name))
    if secret.format_version > CURRENT_FORMAT:
        return _Unusable(NEWER_FORMAT.format(name), retry=True)
    try:
        require_use(session, secret, SecretUse.environment(), environment_name=environment.name)
    except SecretUseRefused as exc:
        # The text names the secret, its use and the command; it never holds a value.
        return _Unusable(str(exc))
    try:
        values = decrypt_secret_row(
            secret,
            cfg.key_provider,
            environment_name=environment.name,
            accept_format_1=cfg.key_provider.accepts_format_1,
        )
    except Exception:  # the cryptography's own text is never read
        # Another worker, with the right key or a newer version, may succeed.
        return _Unusable(CANNOT_DECRYPT.format(name), retry=True)
    url = values.get("url")
    key = values.get("signing_key")
    if not isinstance(url, str) or not url.lower().startswith(("https://", "http://")):
        return _Unusable(NOT_A_WEBHOOK.format(name))
    return url, key if isinstance(key, str) and key else None


def _facts(session: Session, row: NotificationDelivery, environment: Environment) -> payload.Facts:
    """Read the facts of the row's message.

    Parameters
    ----------
    session
        Database session.
    row
        The delivery.
    environment
        The delivery's environment.

    Returns
    -------
    Facts
        The facts of the run, or of the fired health signal it names; invented ones for a test.
    """
    if row.health_signal_id is not None:
        return payload.facts_for_health(session, row, environment.name, row.id)
    if row.run_id is None:
        return dataclasses.replace(
            payload.sample_facts(row.id), job=row.job, environment=environment.name
        )
    # The row is deleted with its run (ON DELETE CASCADE), so the run exists while the row does.
    run = session.scalars(select(Run).where(Run.id == row.run_id)).one()
    return payload.facts_for_run(session, run, row.id)


def _prepare(
    session: Session,
    row: NotificationDelivery,
    environment: Environment,
    cfg: NotifySettings,
    now: datetime,
) -> _Request | _Unusable:
    """Decrypt the secret and build the signed request; the secret lives only in this call.

    Parameters
    ----------
    session
        Database session.
    row
        The delivery.
    environment
        The delivery's environment.
    cfg
        The settings.
    now
        The time, whose Unix seconds are signed with the body.

    Returns
    -------
    _Request or _Unusable
        The request, or why it cannot be made.
    """
    webhook = _webhook(session, row, environment, cfg)
    if isinstance(webhook, _Unusable):
        return webhook
    url, key = webhook
    facts = _facts(session, row, environment)
    try:
        body, headers = payload.build(row.format, row.event, facts)
    except ValueError:
        return _Unusable(UNSENDABLE)
    if row.format == "json" and key is not None:
        headers = {**headers, **payload.sign(body, key, int(now.timestamp()))}
    return _Request(url, body, headers)


def _classify(result: DeliveryResult, attempt: int) -> _Outcome:
    """Turn an attempt's result into what is recorded.

    Parameters
    ----------
    result
        The result of the send.
    attempt
        The attempt number, counted from 1.

    Returns
    -------
    _Outcome
        Delivered, final, or to be retried.
    """
    if result.ok:
        return _Outcome(True, True, result.reason)
    return _Outcome(False, result.permanent or attempt >= MAX_ATTEMPTS, result.reason)


def _record(
    engine: Engine,
    claim: _Claim,
    now: datetime,
    cfg: NotifySettings,
    attempt: int,
    outcome: _Outcome,
) -> bool:
    """Record an attempt's outcome while the lease is still this worker's.

    Parameters
    ----------
    engine
        The metadata database.
    claim
        The claim; the update matches its owner and exact expiry.
    now
        The time.
    cfg
        The settings.
    attempt
        The attempt number.
    outcome
        What the attempt came to.

    Returns
    -------
    bool
        `False` when the lease was lost and nothing was recorded.
    """
    with Session(engine) as session:
        held = (
            NotificationDelivery.id == claim.id,
            NotificationDelivery.state == NotificationState.PENDING,
            NotificationDelivery.lease_owner == cfg.owner,
            NotificationDelivery.lease_expires_at == claim.expires,
        )
        if outcome.delivered:
            values: dict = {
                "state": NotificationState.DELIVERED,
                "delivered_at": now,
                "next_attempt_at": None,
            }
        elif outcome.final:
            values = {"state": NotificationState.FAILED, "next_attempt_at": None}
        else:
            wait = _wait_seconds(attempt, cfg.rand)
            values = {"next_attempt_at": now + timedelta(seconds=wait)}
        result = session.execute(
            update(NotificationDelivery)
            .where(*held)
            .values(last_result=outcome.reason, lease_owner=None, lease_expires_at=None, **values)
            .execution_options(synchronize_session=False)
        )
        if rowcount(result) != 1:
            session.rollback()
            return False
        if not outcome.delivered and outcome.final:
            row = session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.id == claim.id)
            ).one()
            environment = session.get(Environment, row.environment_id)
            _audit_failure(session, row, environment.name if environment else "", outcome.reason)
        session.commit()
    return True


def _attempt(engine: Engine, cfg: NotifySettings, claim: _Claim) -> bool:
    """Send one claimed row and record the result.

    Parameters
    ----------
    engine
        The metadata database.
    cfg
        The settings.
    claim
        The row to send, with the time of its claim.

    Returns
    -------
    bool
        `True` when the result was recorded; `False` when the lease had changed hands.
    """
    with Session(engine) as session:
        row = session.get(NotificationDelivery, claim.id)
        if (
            row is None
            or row.state is not NotificationState.PENDING
            or row.lease_owner != cfg.owner
            or row.lease_expires_at != claim.expires
        ):
            return False
        attempt = row.attempts
        environment = session.get(Environment, row.environment_id)
        prepared = (
            _prepare(session, row, environment, cfg, claim.now)
            if environment is not None
            else _Unusable(SECRET_MISSING.format(row.secret_name))
        )
    # No transaction is open from here until the result is recorded.
    if isinstance(prepared, _Unusable):
        outcome = _Outcome(False, not prepared.retry or attempt >= MAX_ATTEMPTS, prepared.reason)
    else:
        try:
            result = cfg.send(
                prepared.url, prepared.body, prepared.headers, cfg.webhook, cfg.timeout
            )
        except Exception as exc:  # whatever it raises may quote the URL
            logger.warning("A notification attempt failed (%s).", type(exc).__name__)
            result = DeliveryResult(ok=False, permanent=False, reason=UNEXPECTED, status=None)
        del prepared
        outcome = _classify(result, attempt)
    recorded = _record(engine, claim, claim.now, cfg, attempt, outcome)
    if not recorded:
        logger.info("A notification's lease was lost during its attempt; nothing was recorded.")
    return recorded


def deliver_due(
    engine: Engine,
    now: datetime | Callable[[], datetime],
    cfg: NotifySettings,
    *,
    limit: int = BATCH,
    stop: threading.Event | None = None,
    budget: float = BUDGET_SECONDS,
) -> int:
    """Deliver the notifications that are due, one row at a time.

    Each row is claimed in its own transaction, with a fresh time, right before its send; a
    drain therefore leaves no claimed row behind and charges no attempt for a row it never sent.

    Parameters
    ----------
    engine
        The metadata database.
    now
        The database's time, or a function that reads it (called once per claim).
    cfg
        The settings.
    limit
        The most rows to claim.
    stop
        A drain: when set, nothing more is claimed.
    budget
        Seconds after which no further row is claimed.

    Returns
    -------
    int
        The number of rows whose result was recorded (rows whose lease was lost are not counted).
    """
    clock: Callable[[], datetime] = (lambda: now) if isinstance(now, datetime) else now
    deadline = time.monotonic() + budget
    recorded = 0
    for _ in range(limit):
        if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
            break
        claim = _claim(engine, clock(), cfg)
        if claim is None:
            break
        recorded += _attempt(engine, cfg, claim)
    return recorded


class DeliveryLoop:
    """Run `deliver_due` on a daemon thread until it is stopped.

    Parameters
    ----------
    engine
        The metadata database.
    cfg
        The settings.
    idle_seconds
        The sleep when nothing was due, and after a failing step.
    stop
        The event that ends the loop; a new one when `None`.
    """

    def __init__(
        self,
        engine: Engine,
        cfg: NotifySettings,
        *,
        idle_seconds: float = IDLE_SECONDS,
        stop: threading.Event | None = None,
    ) -> None:
        self._engine = engine
        self._cfg = cfg
        self._idle = idle_seconds
        self._stop = stop if stop is not None else threading.Event()

    def run(self) -> None:
        """Deliver until `stop`.

        A failing step (the database is down, say) is logged by its exception's type only, at
        most once a minute, and the sleep grows to a minute while it keeps failing.
        """
        failures = 0
        last_logged = -FAILURE_LOG_SECONDS
        while not self._stop.is_set():
            worked = 0
            try:
                worked = deliver_due(self._engine, self._now, self._cfg, stop=self._stop)
                if failures:
                    logger.info(
                        "Notification delivery is delivering again after %d failed steps.", failures
                    )
                failures = 0
            except Exception as exc:
                failures += 1
                # The text of a database or HTTP error could quote a URL.
                if time.monotonic() - last_logged >= FAILURE_LOG_SECONDS:
                    last_logged = time.monotonic()
                    logger.warning(
                        "A notification delivery step failed (%s); further failures are "
                        "logged once a minute.",
                        type(exc).__name__,
                    )
            if failures:
                self._stop.wait(
                    min(self._idle * 2 ** min(failures, 10), max(self._idle, MAX_IDLE_SECONDS))
                )
            elif not worked:
                self._stop.wait(self._idle)

    def _now(self) -> datetime:
        """Read the database's time.

        Returns
        -------
        datetime
            Aware UTC time.
        """
        with Session(self._engine) as session:
            return db_now(session)

    def start(self) -> threading.Thread:
        """Run `run` in a daemon thread.

        Returns
        -------
        threading.Thread
            The started thread, for the caller to join after `stop`.
        """
        thread = threading.Thread(target=self.run, name="lakana-notifications", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        """Ask the thread to end after the row it is sending."""
        self._stop.set()
