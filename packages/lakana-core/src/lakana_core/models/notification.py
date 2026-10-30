"""The record of one notification of a job's run, and the queue its delivery works from."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime, utcnow

MAX_ATTEMPTS = 5
"""Attempts before a delivery counts as failed."""


class NotificationState(enum.StrEnum):
    """Where a notification is on its way to the webhook."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class NotificationDelivery(Base):
    """One notification: the event of one run of a job and where it goes.

    The row is written in the transaction that finishes the run, so a run that ended has its
    notification queued and a run that did not has none. It is both the queue the delivery loop
    claims from and the record the run view shows. The unique key on (`run_id`, `event`) lets a
    run queue an event only once. A test notification has no run (`run_id` is null, and null
    values do not collide) and the event ``test``.

    The row never holds the webhook's URL or a signing key, only the name of the secret.
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("run_id", "event"),
        Index("ix_notification_deliveries_state_next_attempt_at", "state", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    """The delivery id, which a receiver can use to drop a duplicate."""
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=True
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    job: Mapped[str] = mapped_column(String(255))
    event: Mapped[str] = mapped_column(String(16))
    """``failed``, ``recovered``, ``columns_changed`` or ``test``."""
    format: Mapped[str] = mapped_column(String(8))
    """``json``, ``slack`` or ``teams``, as the project version declared it."""
    secret_name: Mapped[str] = mapped_column(String(255))
    """The secret of the environment that holds the URL (and the signing key)."""
    state: Mapped[NotificationState] = mapped_column(
        SAEnum(NotificationState, native_enum=False, length=9), default=NotificationState.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    """A short text on the last attempt; never the URL."""
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
