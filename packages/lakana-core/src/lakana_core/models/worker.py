"""The worker model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class Worker(Base):
    """A worker process that registered itself.

    What it is doing is not stored: it follows from the `RUNNING` query and deploy runs that
    carry its id and the `RUNNING` task runs whose `lease_owner` it is.
    """

    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    host: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    version: Mapped[str] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime)
    """The database's time of the worker's last heartbeat."""
    takes: Mapped[str] = mapped_column(String(16), default="all", server_default="all")
    """What the worker takes: `all`, `interactive` or `pipeline`.

    The server default is what a row inserted by code that does not set the column means.
    """
    cache_environments_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """The size of the worker's Python environments of projects, as it last measured them."""
    cache_packages_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """The size of uv's package download cache, as the worker last measured it."""
    cache_tasks_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """The size of the working directories of the Python tasks, as the worker last measured it."""
    cache_complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """Whether the last measurement finished; a measurement cut off by its time limit is a floor."""
    cache_measured_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    """When the figures were written (the database's time); `None` before the first measurement."""
    memory_used_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """The resident memory of the worker and the processes it started, as it last reported it."""
    memory_limit_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """The worker's memory limit when it reported its use; `None` for no limit."""
    memory_measured_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    """When the memory figures were written (the database's time); `None` before the first."""
