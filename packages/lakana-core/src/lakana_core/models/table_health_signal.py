"""The table health signal model."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from lakana_core.models.base import Base, UTCDateTime


class TableHealthSignal(Base):
    """One observation of one health signal of a table (#340).

    A commit adds one row per signal and column (`lakana_core.catalog.health_record`); a check
    that sets the table back marks the rows of the snapshots it set back (`set_back_at`), which
    then stay in the history only.

    Attributes
    ----------
    id : uuid.UUID
        Unique identifier.
    environment_id : uuid.UUID
        The table's environment; the rows go with it.
    table_id : uuid.UUID
        The table; the rows go with it.
    signal : str
        ``freshness``, ``volume``, ``null_rate``, ``range`` or ``schema``.
    column_name : str or None
        The column of a ``null_rate`` or ``range`` observation; `None` for the others and for a
        signal that is off for the whole table.
    snapshot_id : int or None
        The snapshot observed; for ``freshness``, the table's last snapshot.
    sequence_number : int or None
        The Iceberg sequence number of `snapshot_id` (0 for a format v1 table), which orders a
        table's commits where ``observed_at`` cannot: two commits can share a time. `None` for
        a row without a snapshot.
    observed : dict or None
        What was observed (`docs/site/reference/table-health.md` lists the keys per signal).
    band : dict or None
        The usual values it was compared with; `None` without a band.
    state : str
        ``ok``, ``fired``, ``no_history``, ``no_metrics`` or ``off``.
    effect : str
        ``warn``, ``block`` or ``off``, as the table's `health:` said when it was observed.
    observed_at : datetime.datetime
        When, by the database's clock.
    set_back_at : datetime.datetime or None
        When a check set the observed snapshot back; `None` while it is the table's data. A
        marked row is neither the current state, nor part of a band, nor where a band's window
        starts; the history shows it as set back.
    """

    __tablename__ = "table_health_signals"
    __table_args__ = (
        # The history pages and the time windows.
        Index("ix_table_health_signals_table_id_observed_at", "table_id", "observed_at"),
        # The newest commit of each signal, found with one ordered seek (`health_read`), and the
        # rows of that commit.
        Index(
            "ix_table_health_signals_table_id_signal_sequence_number",
            "table_id",
            "signal",
            "sequence_number",
            "observed_at",
            "id",
        ),
        # The last firing of each signal and column, among the fired rows only, partitioned as
        # the window ranks them.
        Index(
            "ix_table_health_signals_table_id_state", "table_id", "state", "signal", "column_name"
        ),
        # The rows of the snapshots a set-back takes out (`health_record.forget_set_back`).
        Index("ix_table_health_signals_table_id_snapshot_id", "table_id", "snapshot_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"))
    signal: Mapped[str] = mapped_column(String(16))
    column_name: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    snapshot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sequence_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    observed: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    band: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    state: Mapped[str] = mapped_column(String(16))
    effect: Mapped[str] = mapped_column(String(8))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    set_back_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
