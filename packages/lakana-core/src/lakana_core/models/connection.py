"""The connection model: a named source system of one environment, and its tests."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.connections.kinds import ConnectionKind
from lakana_core.models.base import Base, TimestampMixin, UTCDateTime, utcnow


class Connection(Base, TimestampMixin):
    """A source system an asset of the environment may read: a kind, an address, settings.

    Per environment (dev and prod each own theirs); code refers to it by `name`, which is
    unique in the environment and, like `kind`, never changes. `address` and `settings` are
    JSON, checked by `lakana_core.connections.kinds.validate_connection` on every write and
    stored with its defaults filled; neither ever holds a secret value. `secret_id` names the
    one secret the connection uses, which is bound to it (`SecretUseKind.CONNECTION`); the
    foreign key empties it when the secret row is removed with its environment.
    """

    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("environment_id", "name", name="uq_connections_environment_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(63))
    kind: Mapped[ConnectionKind] = mapped_column(
        SAEnum(ConnectionKind, native_enum=False, length=16)
    )
    address: Mapped[dict[str, Any]] = mapped_column(JSON)
    """Where the source is (host and port, a URL, a bucket, a root and path), validated."""
    settings: Mapped[dict[str, Any]] = mapped_column(JSON)
    """How to talk to it (user, TLS, auth, CA certificate), validated; never a secret value."""
    secret_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("secrets.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))


class ConnectionTestState(enum.StrEnum):
    """Where a connection test is; the database stores member names."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"


class ConnectionTestOutcome(enum.StrEnum):
    """What a finished connection test found; the database stores member names."""

    OK = "ok"
    DNS = "dns"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    TLS = "tls"
    CREDENTIALS = "credentials"
    NOT_FOUND = "not_found"
    NOT_ALLOWED = "not_allowed"
    ROOT_UNAVAILABLE = "root_unavailable"
    FAILED = "failed"


class ConnectionTest(Base):
    """One requested check that a connection is reachable and its secret works.

    Queued by a person, claimed by a worker under a lease, finished with an outcome and a
    redacted message of at most 1,000 characters.
    """

    __tablename__ = "connection_tests"
    __table_args__ = (
        Index("ix_connection_tests_claim", "state", "lease_expires_at"),
        Index("ix_connection_tests_connection_id_created_at", "connection_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE")
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    state: Mapped[ConnectionTestState] = mapped_column(
        SAEnum(ConnectionTestState, native_enum=False, length=16)
    )
    outcome: Mapped[ConnectionTestOutcome | None] = mapped_column(
        SAEnum(ConnectionTestOutcome, native_enum=False, length=16), nullable=True
    )
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    """What the check found, redacted; never a secret value."""
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
