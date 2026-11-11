"""A repository check a worker runs: fetching a host's SSH keys, or testing access."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime, utcnow


class RepositoryProbeKind(enum.StrEnum):
    """What a repository check does; the database stores member names."""

    HOST_KEYS = "host_keys"
    ACCESS = "access"


class RepositoryProbeState(enum.StrEnum):
    """Where a repository check is; the database stores member names."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"


class ProbeOutcome(enum.StrEnum):
    """What a finished repository check found; the database stores member names."""

    OK = "ok"
    HOST_KEY_CHANGED = "host_key_changed"
    HOST_KEY_UNKNOWN = "host_key_unknown"
    AUTH_REFUSED = "auth_refused"
    NOT_FOUND = "not_found"
    UNREACHABLE = "unreachable"
    REFUSED_ADDRESS = "refused_address"
    TIMEOUT = "timeout"
    NO_WORKER = "no_worker"
    FAILED = "failed"


class RepositoryProbe(Base):
    """One requested repository check, queued by a person and run by a worker.

    The server never runs git or ssh: it queues the row, and a worker
    (`lakana_worker.repository_probes.RepositoryProbeLoop`) claims it under a lease and records
    an outcome, a fixed-text message of at most 1,000 characters and, for a host-key check, the
    keys it found. Only the requester reads it, and every probe is deleted 24 hours after it was
    requested (`lakana_core.projects.probes.PROBE_KEEP`).

    Attributes
    ----------
    id : uuid.UUID
        The probe's id.
    kind : RepositoryProbeKind
        ``host_keys`` or ``access``.
    url : str
        The URL checked: the one given for a host-key check, the repository's for an access check.
    repository_id : uuid.UUID or None
        The repository of an access check; the probe goes with it.
    environment_id : uuid.UUID or None
        The environment whose credential an access check uses; the probe goes with it.
    requested_by : uuid.UUID
        The person who asked; the only one who may read it.
    state : RepositoryProbeState
        ``queued``, ``running`` or ``done``.
    outcome : ProbeOutcome or None
        What it found once done.
    message : str or None
        What it found, in Lakana's own words; never a tool's raw output.
    host_keys : list or None
        The keys of a host-key check: ``{"type", "fingerprint", "line"}`` each.
    lease_owner : str or None
        The worker running it.
    lease_expires_at : datetime.datetime or None
        When that worker's claim ends.
    created_at : datetime.datetime
        When it was requested (the database's clock).
    finished_at : datetime.datetime or None
        When it was done.
    """

    __tablename__ = "repository_probes"
    __table_args__ = (Index("ix_repository_probes_claim", "state", "lease_expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[RepositoryProbeKind] = mapped_column(
        SAEnum(RepositoryProbeKind, native_enum=False, length=16)
    )
    url: Mapped[str] = mapped_column(Text)
    repository_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=True
    )
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), nullable=True
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    state: Mapped[RepositoryProbeState] = mapped_column(
        SAEnum(RepositoryProbeState, native_enum=False, length=16)
    )
    outcome: Mapped[ProbeOutcome | None] = mapped_column(
        SAEnum(ProbeOutcome, native_enum=False, length=16), nullable=True
    )
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    host_keys: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
