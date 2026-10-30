"""How a notification delivery is shown: on a run, and on its own for a test."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from lakana_core.models.notification import MAX_ATTEMPTS, NotificationDelivery


@dataclass(frozen=True)
class NotificationView:
    """One notification of a run.

    Parameters
    ----------
    id
        The delivery id: what a receiver sees as ``delivery_id`` and what
        ``lakana job notification`` takes.
    event
        ``failed``, ``recovered``, ``columns_changed`` or ``test``.
    state
        ``pending``, ``delivered`` or ``failed``.
    attempts
        Attempts made so far.
    max_attempts
        Attempts before the delivery counts as failed.
    last_result
        A short text on the last attempt; never the URL. `None` before the first attempt.
    delivered_at
        When it was delivered.
    """

    id: uuid.UUID
    event: str
    state: str
    attempts: int
    max_attempts: int
    last_result: str | None
    delivered_at: datetime | None


@dataclass(frozen=True)
class DeliveryView(NotificationView):
    """One delivery on its own, as the command that tests a webhook waits for it.

    Parameters
    ----------
    job
        The job.
    repository
        Repository name.
    next_attempt_at
        When the next attempt is due; `None` once the delivery is over.
    """

    job: str
    repository: str
    next_attempt_at: datetime | None


def notification_view(row: NotificationDelivery) -> NotificationView:
    """Show a delivery as one notification of a run.

    Parameters
    ----------
    row
        The delivery.

    Returns
    -------
    NotificationView
        The view.
    """
    return NotificationView(
        id=row.id,
        event=row.event,
        state=row.state.value,
        attempts=row.attempts,
        max_attempts=MAX_ATTEMPTS,
        last_result=row.last_result,
        delivered_at=row.delivered_at,
    )


def delivery_view(row: NotificationDelivery, repository: str) -> DeliveryView:
    """Show a delivery on its own.

    Parameters
    ----------
    row
        The delivery.
    repository
        Its repository's name.

    Returns
    -------
    DeliveryView
        The view.
    """
    return DeliveryView(
        id=row.id,
        event=row.event,
        state=row.state.value,
        attempts=row.attempts,
        max_attempts=MAX_ATTEMPTS,
        last_result=row.last_result,
        delivered_at=row.delivered_at,
        job=row.job,
        repository=repository,
        next_attempt_at=row.next_attempt_at,
    )
